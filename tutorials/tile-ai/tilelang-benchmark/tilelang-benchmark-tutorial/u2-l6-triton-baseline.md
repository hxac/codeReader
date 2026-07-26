# Triton 基线与 triton.testing.do_bench

## 1. 本讲目标

上一讲（u2-l5）我们拆解了编号 `0.cublas-benchmark` 的 cuBLAS 参考基线——一个用 C++ 和 `std::chrono` 自行计时的测试床。本讲进入编号 `1.triton-benchmark`，拆解它的"对手"之一：**Triton 基线**。

读完本讲，你应该能够：

1. 看懂一个标准 Triton matmul 内核从 `@triton.autotune` 到 `tl.store` 的完整骨架。
2. 说清楚 `triton.Config`、`@triton.autotune`、`@triton.jit` 各自的职责，以及 `key=[...]` 的触发作用。
3. 理解 Triton「指针运算式」内核（`tl.load` / `tl.dot` / `tl.store`）与 TileLang「块级声明式」内核在抽象层级上的根本差异。
4. 掌握 `triton.testing.do_bench` 这个**跨框架统一计时接口**的约定：它返回什么单位、warmup/rep 怎么用，并能据此算出 TFlops。
5. 辨别 Triton 与（下一单元将讲的）TileLang 在输出格式、单位、措辞上的差异——这些差异在跨框架对比时是真正的「单位陷阱」。

## 2. 前置知识

本讲默认你已经读过：

- **u1-l3 运行一次基准测试**：知道 Triton 走「解释型」路径，`.sh` 对每个 `(m,n,k,dtype)` 调一次 `python xxx.py`，并提到过「Triton 日志文件名被误写成 `tilelang`」这一历史遗留 bug——本讲我们会再次踩到它。
- **u2-l4 性能度量方法论**：知道 GEMM 运算量是 \(2MNK\)，warmup/rep 影响测量稳定性，统一换算公式是 `total_flops / latency(ms) * 1e-9`，且不同框架返回的单位不同（cuBLAS 返回 µs、Triton/TileLang 返回 ms）。
- **u2-l5 cuBLAS 参考基准**：知道 cuBLAS 用 host 侧 `std::chrono + cudaDeviceSynchronize` 自适应重复计时。本讲的 `do_bench` 是另一种「自动计时」思路，我们会在 4.4 节对照。

下面几个术语先建立直觉：

| 术语 | 一句话解释 |
| --- | --- |
| **Triton** | OpenAI 开源的 GPU kernel DSL，用 Python 写内核，底层编译成 PTX/HSACO。 |
| **`@triton.jit`** | Just-In-Time 编译装饰器，把一个 Python 函数编译成 GPU kernel。 |
| **`@triton.autotune`** | 自动调优装饰器：给定一组 `Config`，对每个新输入自动挑选最快的那一个。 |
| **`tl.load` / `tl.store` / `tl.dot`** | Triton language 的三大原语：读显存、写显存、做矩阵乘（自动走 Tensor Core）。 |
| **`do_bench`** | `triton.testing.do_bench`：一个统一的 kernel 计时函数，返回**毫秒**级延迟。 |
| **provider** | 一个算子的某一种实现（cublas / triton / tilelang / bitblas…），参见 u1-l2 的编号目录约定。 |

> 一句话定位：cuBLAS 是「库调用 + C++ 计时」，Triton 是「Python 写内核 + Python 计时」。两者都是 TileLang 的对照标尺。

## 3. 本讲源码地图

本讲只涉及一个 provider 目录下的两个文件：

| 文件 | 作用 |
| --- | --- |
| [`benchmark_triton_matmul_float16.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py) | Triton fp16 GEMM 的全部内容：搜索空间、`@triton.autotune`/`@triton.jit` 内核、host 侧 `matmul()` 封装、`benchmark()` 计时函数。 |
| [`benchmark_float16.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh) | 驱动脚本：对一组 `(m,n,k)` 逐个调用上面的 `.py`，把 stdout 通过 `tee` 写进 `logs/`。 |

同一目录下还有 `benchmark_triton_matmul_int8.py` / `benchmark_int8.sh`（int8 版本，结构同构）、以及 `extract_triton_data.py`（从日志解析数据，这是 u2-l7 的内容，本讲不展开）。

## 4. 核心概念与源码讲解

按最小模块拆成四节：搜索空间（4.1）、网格映射（4.2）、内核主体（4.3）、计时接口（4.4）。

### 4.1 `triton.Config` 与 `@triton.autotune`：搜索空间与触发键

#### 4.1.1 概念说明

GPU 上一个 GEMM 想跑得快，要调很多「旋钮」：每个 block 算多大的 \(M \times N\) 子块（`BLOCK_SIZE_M/N`）、K 方向每次搬多少（`BLOCK_SIZE_K`）、软流水几级（`num_stages`）、一个 block 开几个 warp（`num_warps`）、以及 L2 复用分组大小（`GROUP_SIZE_M`）。

这些旋钮的组合就是一个**搜索空间**。一个 `triton.Config` 就是这个空间里的**一个点**：它把若干「元参数」（kernel 里能当常量用的值，如 `BLOCK_SIZE_M`）和「编译选项」（如 `num_warps`、`num_stages`）打包在一起。

`@triton.autotune` 的作用是：给一堆 `Config`，再给一个「触发键」`key`。当输入里 `key` 涉及的值发生变化时，就**重新把所有 Config 都跑一遍**，挑出最快的那个缓存起来；同一个 `key` 值再来调用时，直接复用上次的最佳 Config。这样既保证了不同 shape 各得其所，又不会每次都重搜。

> 对比 cuBLAS：cuBLAS 内部也会按 problem size 选算法（`cublasGemmEx` 的 algo），但那是黑盒；Triton 把搜索空间**显式地交给你**列在代码里。

#### 4.1.2 核心流程

```text
get_cuda_autotune_config()      # 1. 列出 16 个候选 Config（人工经验拼出的搜索空间）
        │
        ▼
@triton.autotune(configs=…,     # 2. 装饰器接住这些 Config
                 key=['M','N','K'])  #    并声明 (M,N,K) 变化时重新挑选
        │
        ▼
@triton.jit                      # 3. 真正的内核函数（下一节）
def matmul_kernel(...): ...
```

首次调用 `matmul_kernel[grid](M=4096,N=4096,K=4096)` 时，`key` 命中一个新值 `(4096,4096,4096)`，于是 16 个 Config 各编译、各跑一遍，记下最快者；之后再以同样的 `(M,N,K)` 调用就直接复用。

#### 4.1.3 源码精读

搜索空间定义在 `get_cuda_autotune_config()`，返回一个**列表的 Config**：

[benchmark_triton_matmul_float16.py:L18-L56](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L18-L56) —— 列出 16 个候选配置。

其中一个典型 Config 长这样：

[benchmark_triton_matmul_float16.py:L20-L21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L20-L21) —— 一个 `triton.Config`：`BLOCK_SIZE_M=128, BLOCK_SIZE_N=256, BLOCK_SIZE_K=64, GROUP_SIZE_M=8`，外加编译选项 `num_stages=3, num_warps=8`。

注意 Config 字典里的键（`BLOCK_SIZE_M` 等）必须在内核签名里声明为 `tl.constexpr`，否则 Triton 没法把它当编译期常量织进 kernel。`num_stages` / `num_warps` 不写在字典里，而是作为 `Config` 的关键字参数——它们是编译器旋钮，不进 kernel 体。

装饰器紧贴内核之上：

[benchmark_triton_matmul_float16.py:L64-L67](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L64-L67) —— `@triton.autotune(configs=get_autotune_config(), key=['M','N','K'])`。

`key=['M','N','K']` 的含义：只要这三个运行时参数中任一个变了，就触发新一轮调优。你可以把它理解为一个**缓存键**。

#### 4.1.4 代码实践

**实践目标**：理解搜索空间规模与 `key` 的触发作用。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `benchmark_triton_matmul_float16.py`，数一下 `get_cuda_autotune_config()` 里到底列了几个 `triton.Config`。
2. 找到所有 `num_warps` 取值，列出去重后的集合。
3. 假设把 `key=['M','N','K']` 改成 `key=['M','N']`，思考：对一个新 shape `(M=4096,N=4096,K=8192)`，会不会重新调优？

**需要观察的现象 / 预期结果**：

1. 共 **16** 个 Config。
2. `num_wargs` 取值为 `{2, 4, 8}`。
3. 改成 `key=['M','N']` 后，`(4096,4096,*)` 共用同一最佳 Config——K 变化不再触发重搜。好处是省调优时间；坏处是对 K 差异很大的 shape 可能拿不到最优块大小（`BLOCK_SIZE_K`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLOCK_SIZE_*` 写在 Config 字典里，而 `num_warps` / `num_stages` 写在字典外当关键字参数？

**答案**：`BLOCK_SIZE_*` 是 kernel **体内**会用到的元参数（决定循环范围、共享内存大小），必须作为 `tl.constexpr` 进入 kernel 签名，所以放进字典；`num_warps` / `num_stages` 是**编译器/调度**旋钮，kernel 体不引用它们，由 Triton 编译器消费，所以作为 `Config` 的关键字参数传入。

**练习 2**：`GROUP_SIZE_M` 也是 `tl.constexpr`，它和 `BLOCK_SIZE_M` 的作用有什么本质不同？

**答案**：`BLOCK_SIZE_M` 决定**一个 block 算多大**的输出子块（计算粒度）；`GROUP_SIZE_M` 不改变计算内容，只改变**多个 block 之间的调度顺序**（见 4.2 节的 grouped pid），用于提升 L2 缓存命中率。前者是「算什么」，后者是「按什么顺序算」。

### 4.2 `@triton.jit` 与 grouped pid 网格映射

#### 4.2.1 概念说明

`@triton.jit` 把下面的 Python 函数编译成 GPU kernel。在 kernel 内部，你写的是「**一个 block**」要干的事；至于一共有多少个 block、它们如何排布，由调用方在 host 侧用 `grid` 给出。

每个 block 通过 `tl.program_id(axis=0)` 拿到自己的编号 `pid`，再自己算出「我负责输出矩阵 C 的哪一块」。最朴素的映射是按行排：`pid_m = pid // num_pid_n`、`pid_n = pid % num_pid_n`。

但本内核用的是 **grouped pid**（也叫 L2 cache optimization）：把连续的 `GROUP_SIZE_M * num_pid_n` 个 block 编成一组，组内先固定一小段 M、扫遍所有 N。这样同一块 A 在被逐 N 复用的窗口内还热乎地待在 L2 里，显著提升命中率。这是 Triton 官方 matmul tutorial 的经典优化，本项目原样沿用。

#### 4.2.2 核心流程

设输出被切成 `num_pid_m × num_pid_n` 个块。

朴素映射（**本内核没用**）：

\[
\text{pid\_m} = \text{pid} // \text{num\_pid\_n},\quad
\text{pid\_n} = \text{pid} \bmod \text{num\_pid\_n}
\]

grouped 映射（**本内核使用**）：

\[
\text{num\_pid\_in\_group} = \text{GROUP\_SIZE\_M} \times \text{num\_pid\_n}
\]
\[
\text{group\_id} = \text{pid} // \text{num\_pid\_in\_group}
\]
\[
\text{first\_pid\_m} = \text{group\_id} \times \text{GROUP\_SIZE\_M}
\]
\[
\text{group\_size\_m} = \min(\text{num\_pid\_m} - \text{first\_pid\_m},\ \text{GROUP\_SIZE\_M})
\]
\[
\text{pid\_m} = \text{first\_pid\_m} + ((\text{pid} \bmod \text{num\_pid\_in\_group}) \bmod \text{group\_size\_m})
\]
\[
\text{pid\_n} = (\text{pid} \bmod \text{num\_pid\_in\_group}) // \text{group\_size\_m}
\]

直觉：pid 每加 1，`pid_n` 先在组内累加（扫 N 方向），攒满 `num_pid_n` 后才让 `pid_m` 进一格。于是一块 A 连续被 `num_pid_n` 个 block 复用，L2 命中率上升。

#### 4.2.3 源码精读

内核签名（`@triton.jit` 装饰）：

[benchmark_triton_matmul_float16.py:L68-L84](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L68-L84) —— 内核接收三个**指针**（`a_ptr/b_ptr/c_ptr`）、维度 `M/N/K`、六个 **stride**、以及五个 `tl.constexpr` 元参数（其中 `ACTIVATION` 本基线恒为空串，可融合激活但未启用）。

注意一个关键风格：Triton 传的是**裸指针和 stride**，而不是 tensor 对象。这是 Triton「指针式」内核的标志，下一节会看到它如何用指针算术来定位元素。

grouped pid 映射：

[benchmark_triton_matmul_float16.py:L92-L100](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L92-L100) —— 把 `pid` 解码成 `(pid_m, pid_n)`，注释明确写了 "in a grouped ordering to promote L2 data reuse"。

`group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)` 这行是为了处理**最后一组不满 GROUP_SIZE_M** 的边界——分组走到 M 末尾时不足 `GROUP_SIZE_M` 行，要按实际剩余行数算，否则 `pid_m` 会越界。

host 侧的 `grid` 用 lambda 延迟求值（拿到 autotune 选中的 `BLOCK_SIZE_*` 后才算总 block 数）：

[benchmark_triton_matmul_float16.py:L155-L156](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L155-L156) —— `grid = lambda META: (triton.cdiv(M, META['BLOCK_SIZE_M']) * triton.cdiv(N, META['BLOCK_SIZE_N']), )`，总 block 数 = M 方向块数 × N 方向块数，一维启动。

#### 4.2.4 代码实践

**实践目标**：亲手把一个 pid 翻译成它负责的 C 子块坐标。

**操作步骤**：

1. 设 `M=N=4096`，`BLOCK_SIZE_M=BLOCK_SIZE_N=128`，`GROUP_SIZE_M=8`。
2. 算出 `num_pid_m`、`num_pid_n`、`num_pid_in_group`。
3. 取 `pid = 0` 和 `pid = 100`，分别套用 L96–L100 的公式算出 `(pid_m, pid_n)`，再换算成 C 的行列起始坐标（`pid_m*128`, `pid_n*128`）。

**预期结果**：

- `num_pid_m = num_pid_n = 32`，`num_pid_in_group = 8 * 32 = 256`。
- `pid=0`：`group_id=0`，`pid_m=0, pid_n=0` → C 子块起点 `(0,0)`。
- `pid=100`：`group_id=0`，`100 mod 256 = 100`，`group_size_m=8`，`pid_m = 0 + (100 mod 8) = 4`，`pid_n = 100 // 8 = 12` → C 子块起点 `(4*128, 12*128) = (512, 1536)`。

#### 4.2.5 小练习与答案

**练习**：把 grouped 映射换回朴素行主序映射（`pid_m = pid // num_pid_n; pid_n = pid % num_pid_n`），计算结果会变吗？性能通常如何变化？

**答案**：计算结果**完全不变**（每个 C 子块仍被恰好一个 block 算一次，只是分配给不同 pid）；性能通常会**下降**，因为行主序下相邻 pid 跑同一行、不同列，A 块的复用窗口变短，L2 命中率降低。这就是 grouped pid 存在的意义——它只改调度顺序，不改数值。

### 4.3 `tl.load` / `tl.dot` / `tl.store`：指针式内核主体

#### 4.3.1 概念说明

Triton 内核的核心是「**指针运算 + 块级原语**」：

- `tl.load(ptr, mask, other)`：按指针加载一个**块**的数据进寄存器/共享内存，越界位置用 `mask` 屏蔽并填 `other`。
- `tl.dot(a, b, acc, out_dtype=…)`：块级矩阵乘 `a @ b`，结果累加进 `acc`。底层自动映射到 Tensor Core 的 MMA 指令。
- `tl.store(ptr, val, mask)`：把一个块写回显存，越界位置用 `mask` 跳过。

和 TileLang 的区别（预告下一单元）：TileLang 用 `alloc_shared` / `alloc_fragment` **显式声明**缓冲，用 `T.copy` / `T.gemm` **显式**搬数据、做乘法；而 Triton 把这些细节**藏在 `tl.load/dot/store` 内部**，由编译器决定哪里放共享内存、怎么排流水线。一句话：**Triton 更贴近「写循环 + 指针」，TileLang 更贴近「声明张量 + 块级算子」**。

#### 4.3.2 核心流程

一个 block 的 GEMM 主体三步：

```text
1. 用 offs_am/offs_bn/offs_k + stride 算出 A、B 首块指针 a_ptrs/b_ptrs
2. for k in 0..ceil(K/BLOCK_SIZE_K):
       a = tl.load(a_ptrs, mask=…, other=0)   # 取 A 的一个 K 块
       b = tl.load(b_ptrs, mask=…, other=0)   # 取 B 的一个 K 块
       accumulator = tl.dot(a, b, accumulator)# 累加进 accumulator（走 Tensor Core）
       a_ptrs += BLOCK_SIZE_K * stride_ak     # 指针前进一个 K 块
       b_ptrs += BLOCK_SIZE_K * stride_bk
3. 把 accumulator 写回 C 的对应子块（带越界 mask）
```

#### 4.3.3 源码精读

指针算术——这一段是「指针式内核」的精髓，用 `offs[:,None]` / `offs[None,:]` 做广播，拼出一个二维指针块：

[benchmark_triton_matmul_float16.py:L109-L113](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L109-L113) —— `a_ptrs = a_ptr + (offs_am[:,None]*stride_am + offs_k[None,:]*stride_ak)`，得到一个 `[BLOCK_SIZE_M, BLOCK_SIZE_K]` 的指针块；B 同理。`offs_am` 用 `% M` 做了廉价越界回绕（最终正确性仍由 K 维 mask 与写回 mask 保证）。

> **单位/精度提醒（真实代码 vs 注释）**：第 118–119 行注释声称「accumulate into a block of **fp32** values for higher accuracy」，但第 120 行实际写的是：

[benchmark_triton_matmul_float16.py:L120](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L120) —— `accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float16)`，累加器其实是 **fp16**，与注释不符。

这是本项目从官方 tutorial（用 fp32 累加）改来的快版——用 fp16 累加换速度、牺牲精度。本讲特意点出，是想强化 u1-l3/u2-l5 反复强调的意识：**读源码以代码为准，注释会过时**。下游做数值正确性比较时，这个累加精度差异会影响可接受的误差阈值。

K 维主循环（load → dot → 前进指针）：

[benchmark_triton_matmul_float16.py:L121-L130](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L121-L130) —— `tl.load` 用 `offs_k < K - k*BLOCK_SIZE_K` 做 K 维尾部 mask；`tl.dot(a, b, accumulator, out_dtype=tl.float16)` 累加；每次循环后 `a_ptrs += BLOCK_SIZE_K*stride_ak` 把指针推到下一个 K 块。注意 `tl.dot` 把 `a @ b` 累加进 `accumulator` 这一语义——它既做了乘加，又复用了同一份累加器。

写回 C（带二维越界 mask）：

[benchmark_triton_matmul_float16.py:L139-L143](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L139-L143) —— `tl.store(c_ptrs, c, mask=c_mask)`，`c_mask = (offs_cm[:,None] < M) & (offs_cn[None,:] < N)` 保证输出矩阵边缘（M、N 不是块大小整数倍时）不会越界写。

host 侧 `matmul()` 封装：检查连续性、分配输出、组装参数并启动：

[benchmark_triton_matmul_float16.py:L146-L164](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L146-L164) —— 注意它把 `a.stride(0)/stride(1)` 等逐个传进 kernel，呼应 4.2 节「传指针 + stride」的风格；`ACTIVATION=activation` 默认空串，所以内核里 `if ACTIVATION == "leaky_relu"` 分支不会被触发。

#### 4.3.4 代码实践

**实践目标**：把内核里每一次指针操作和块原语画成数据流。

**操作步骤**（源码阅读型）：

1. 列一张表，三列分别是「源 / 操作 / 目的」。例如：`a_ptr(显存) / tl.load / a(寄存器块)`、`a,b / tl.dot / accumulator`、`accumulator / .to(tl.float16) / c`、`c / tl.store / c_ptr(显存)`。
2. 标出 K 循环里哪些值是**跨迭代不变**（`offs_am`、`offs_bn`）、哪些**每次前进**（`a_ptrs`、`b_ptrs`）。

**预期结果**：你会看到一条清晰的「显存→寄存器块→累加器→显存」的数据搬运路径；跨迭代只有指针在 K 方向滑动，`offs` 模板不变。这正是 TileLang 用 `T.copy`/`T.gemm` 显式表达的同一件事，只是 Triton 把它压进了指针运算里。

#### 4.3.5 小练习与答案

**练习**：`tl.load` 的 `other=0.0` 起什么作用？如果把它改成 `other=1.0`，结果会怎样？

**答案**：`other` 是 mask 屏蔽位置（K 维越界）填入的占位值。GEMM 里越界的 A/B 元素填 0，保证它们对累加无贡献（\(x \cdot 0 = 0\)），从而等价于在 K 不足处补零。若改成 `other=1.0`，越界位置会变成真实乘 1，导致累加结果偏大，输出错误——所以 GEMM 里这个值必须是 0。

### 4.4 `triton.testing.do_bench`：统一计时接口

#### 4.4.1 概念说明

cuBLAS 基线（u2-l5）自己用 `std::chrono + cudaDeviceSynchronize` 计时；Triton 不必这么麻烦，它提供了 `triton.testing.do_bench`——一个**统一的、跨 kernel 的计时函数**。你只要把被测调用包成一个无参 lambda 传进去，它就帮你处理 warmup、重复、同步、取统计量。

`do_bench` 的契约（与 u2-l4 的方法论对齐）：

- **输入**：一个无参可调用对象 `fn`（如 `lambda: matmul(a, b)`），以及 `warmup`、`rep`。
- **返回**：延迟，单位是**毫秒**（这是关键，跨框架对比时常踩坑）。
- **机制**：先 warmup 若干次（预热缓存、触发 JIT 编译），再 rep 次计时，取统计量（中位数）。

本基线的 `benchmark()` 函数就是把 `do_bench` 包了一层，再加上 TFlops 换算。

#### 4.4.2 核心流程

```text
benchmark(M, N, K, provider='triton'):
    warmup=5; rep=10
    a,b = torch.randn(…)                      # 准备输入
    ms = triton.testing.do_bench(             # 统一计时
            lambda: matmul(a, b),
            warmup=warmup, rep=rep)           # 返回毫秒
    perf = lambda ms: 2*M*N*K * 1e-12 / (ms*1e-3)   # ms→s，FLOPS→TFlops
    return ms, perf(ms)
```

TFlops 换算的数学（承接 u2-l4）：

\[
\text{total\_flops} = 2MNK
\]
\[
\text{TFlops} = \frac{\text{total\_flops} \times 10^{-12}}{\text{ms} \times 10^{-3}}
= \frac{\text{total\_flops}}{\text{ms}} \times 10^{-9}
\]

末尾的 \(\times 10^{-9}\) 正是 u2-l4 给出的统一换算因子（`ms→s` 的 \(10^{-3}\) 与 `FLOPS→TFlops` 的 \(10^{-12}\) 之积）。

#### 4.4.3 源码精读

计时函数本体：

[benchmark_triton_matmul_float16.py:L172-L182](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L172-L182) —— `benchmark(M,N,K,provider)`：`warmup=5, rep=10`；用 `triton.testing.do_bench(lambda: matmul(a,b), warmup=warmup, rep=rep)` 得到 `ms`；再用 `perf` lambda 换算 TFlops，返回 `(ms, perf(ms))`。

注意 `provider` 参数虽然写了 `if provider == 'triton':` 分支，但本文件只有 Triton 一条路径，`provider` 形同摆设——这是从更通用的模板裁剪下来的痕迹。

`warmup` / `rep` 取值：

[benchmark_triton_matmul_float16.py:L173-L174](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L173-L174) —— `warmup = 5; rep = 10`。

> **跨框架对比的一致性提醒**：u2-l4 指出 TileLang 的 matmul 用 `warmup=3, rep=20`，而这里 Triton 用 `warmup=5, rep=10`。两者 warmup/rep **不同**，严格意义上测量窗口并不完全等价。做精细对比时这是已知的小瑕疵；好在 `do_bench` 取的是统计量，对稳态 kernel 影响通常可忽略，但要心里有数。

入口与输出格式（**本讲的重点对照项**）：

[benchmark_triton_matmul_float16.py:L184-L195](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L184-L195) —— `argparse` 解析 `--m/--n/--k`，调用 `benchmark(...)`，最后 `print(f"Mean Latency {ms} ms, Mean performance: {tflop} TFLOPS")`。

这一行 `print` 决定了日志的最终样貌，也是和 TileLang 输出对照的关键（见综合实践）。

最后看驱动脚本如何把每个 shape 喂给这个 `.py`：

[benchmark_float16.sh:L20-L33](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L20-L33) —— 对若干 `(m,n,k)` 各跑一次 `python ./benchmark_triton_matmul_float16.py --m … --n … --k …`，stdout 经 `tee` 写进 `logs/`。

> **历史遗留 bug（再次踩到）**：这些日志文件名全是 `benchmark_tilelang_m…_float16.log`（例如 [L20](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L20)），**明明是 Triton 的日志却被命名成 tilelang**——这正是 u1-l3 提到的「Triton 日志文件名误写为 tilelang」。`extract_triton_data.py`（u2-l7）也按这个错误的 `tilelang` 文件名去读，所以能跑通；但人来读目录时极易混淆。读脚本/日志务必以实际内容为准，别被文件名带偏。

另外 [L3-L19](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L3-L19) 是一大段被注释掉的旧 shape 列表，实际生效的只有 L20–L33。这也是仓库里常见的「注释保留历史」现象，别误以为它们会执行。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对照同一 shape 下 Triton 与 TileLang 两个 provider 的**输出格式**，找出 latency/TFlops 打印上的全部差异——这是后续跨框架对比最容易出错的地方。

**操作步骤**：

1. 打开 Triton 的输出语句：`benchmark_triton_matmul_float16.py` 第 195 行。

   ```python
   print(f"Mean Latency {ms} ms, Mean performance: {tflop} TFLOPS")
   ```

2. 打开 TileLang 的输出语句：`hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` 末尾（文件末尾的 4 行 `print`），分别是：

   ```python
   print(f"Best latency (s): {best_latency}")
   print(f"Best TFlops: {total_flops / best_latency * 1e-9:.3f}")
   print(f"Best config: {best_config}")
   print(f"Reference TFlops: {total_flops / ref_latency * 1e-9:.3f}")
   ```

3. 列一张对比表，维度包括：行数、latency 措辞、**latency 单位**、TFlops 小数位、是否打印 config、是否打印 reference、是否打印 kernel 源码。

**需要观察的现象 / 预期结果**（这是源码阅读型实践，下表即预期结论；实际数值**待本地验证**）：

| 维度 | Triton（`1.triton-benchmark`） | TileLang（`3.tilelang-benchmark`） |
| --- | --- | --- |
| 行数 | **1 行** | **多行** |
| latency 措辞 | `Mean Latency` | `Best latency` |
| **latency 单位** | **毫秒（`ms`）** | **秒（`(s)`）** ⚠️ 单位陷阱 |
| TFlops 措辞/精度 | `Mean performance`，无固定小数位 | `Best TFlops`，固定 `.3f` |
| 是否打印 config | 否 | 是（`Best config: …`） |
| 是否打印 reference | 否 | 是（`Reference TFlops`） |
| 是否打印 kernel 源码 | 否 | 是（先 `print(best_result.kernel.get_kernel_source())`） |
| 用词差异的来源 | `do_bench` 返回的是多次重复的统计量 → "Mean" | autotune 在搜索空间里挑出最快者 → "Best" |

**关键结论**：两者**单位不同**（Triton 是 ms、TileLang 是 s）！若你写脚本同时读这两种日志、直接把 latency 字符串 `float()` 出来再相除，会差 \(10^3\) 倍。这正是 u2-l4 反复强调的「跨框架对比前必须统一单位」。TFlops 那一栏两边都是 TFlops、量纲一致，可放心直接比；latency 则要先换算。

#### 4.4.5 小练习与答案

**练习 1**：给定 `M=N=K=8192`，`do_bench` 返回 `ms=0.5`，用本文件的 `perf` 公式算 TFlops。

**答案**：

\[
\text{TFlops} = \frac{2 \times 8192^3 \times 10^{-12}}{0.5 \times 10^{-3}}
= \frac{1.0995 \times 10^{12} \times 10^{-12}}{5 \times 10^{-4}}
= \frac{1.0995}{5 \times 10^{-4}}
\approx 2199 \text{ TFlops}
\]

（即约 2.2 × 10³ TFlops。）

**练习 2**：为什么 `do_bench` 的入参是 `lambda: matmul(a, b)` 这种**无参 callable**，而不是直接传 `matmul(a, b)` 的返回值？

**答案**：`do_bench` 需要**反复执行**被测函数来计时，所以它要拿到「调用动作」本身，而不是一次调用的结果。传 `lambda`（闭包）等于把「调用 `matmul(a,b)`」这件事打包成一个可重复触发的零参函数交给 `do_bench`；若直接传 `matmul(a,b)`，Python 会先求值一次、把结果（矩阵 C）传进去，`do_bench` 拿到的是张量而非可调用对象，无法计时。

## 5. 综合实践

把本讲四个模块串起来，做一次「**只读源码的端到端追踪**」。

**任务**：跟踪从命令行到日志文件的完整链路，并产出一份「Triton 基线说明卡」。

**步骤**：

1. **从 shape 到日志**：在 `benchmark_float16.sh` 里挑一行，例如 L24：
   `python ./benchmark_triton_matmul_float16.py --m 8192 --n 1024 --k 8192 2>&1 | tee ./logs/benchmark_tilelang_m8192_n1024_k8192_float16.log`
   写出这条命令：调用了哪个 `.py`、传了什么参数、stdout 写到哪个文件（注意文件名里的 `tilelang` 是命名 bug，实际内容是 Triton 结果）。

2. **从参数到内核**：`--m/--n/--k` 经 `argparse` 进入 `benchmark()`（L193）→ `do_bench(lambda: matmul(a,b))`（L180）→ `matmul()`（L156）启动 `matmul_kernel[grid]`。在这条链上标出每一跳的文件:行号。

3. **从内核到计时**：在内核里指出①搜索空间由谁给（`@triton.autotune` + 16 个 `Config`）、②block 坐标由谁算（grouped pid，L92–L100）、③数据搬运靠哪三个原语（`tl.load/dot/store`）、④延迟由谁测（`do_bench`，L180）、⑤单位是什么（毫秒）。

4. **跨框架对照**：用 4.4.4 的对比表，写出若要把 Triton 的 `ms` 和 TileLang 的 `(s)` 放在同一张图里，需要做的单位换算（`ms → ×1e-3 → s`，或反过来）。

**预期产出**：一张含「命令 → .py → kernel 四要素 → do_bench(ms) → 日志」的链路图，以及一行单位换算公式。无需真实运行，数值待本地验证。

## 6. 本讲小结

- Triton 基线用 **`@triton.autotune` + 一组 `triton.Config`** 把 GEMM 的调优搜索空间显式列在代码里，靠 `key=['M','N','K']` 决定何时重搜。
- 内核是**指针式**的：传裸指针 + stride，用 `offs[:,None]` 广播拼出二维指针块，靠 `tl.load` / `tl.dot` / `tl.store` 三原语完成「取 A、取 B、累加、写 C」。
- **grouped pid** 只改 block 调度顺序（不改数值），让一块 A 在 L2 里被连续复用 `num_pid_n` 次，提升命中率。
- 计时统一交给 **`triton.testing.do_bench`**，它返回**毫秒**级延迟；TFlops 用 \(2MNK \times 10^{-12} / (\text{ms}\times 10^{-3})\) 换算，等价于 u2-l4 的 `total_flops/ms * 1e-9`。
- **单位陷阱**：Triton 输出 latency 单位是 `ms`，TileLang 是 `(s)`，跨框架对比前必须统一；措辞上 Triton 用 "Mean"（统计量），TileLang 用 "Best"（autotune 最优）。
- 读源码以**代码**为准：本内核注释声称 fp32 累加、实际却是 fp16 累加（L120）；日志文件名误写成 `tilelang`——都是真实历史遗留，别被带偏。

## 7. 下一步学习建议

下一讲 **u2-l7 数据提取与可视化** 会接着本讲的日志往下走：讲解 `extract_triton_data.py` 如何用正则 `\d+\.\d+` 配合 `[-2]` 从「`Mean Latency … ms, Mean performance: … TFLOPS`」这一行里抠出 latency，再交由 `data/*.py` 和 `plot/*.py` 生成对比图。建议你先把本讲的输出格式（尤其是 ms 单位）记牢，因为 u2-l7 的正则正是按这个格式写的。

再往后进入**第 3 单元 TileLang 语言核心**：u3-l8 会讲 TileLang 的 `@autotune`/`@jit` 装饰器骨架，u3-l9 会逐行解剖块级 GEMM 内核。届时你可以不断回到本讲做对照——**Triton 的指针式 `tl.dot` 对应 TileLang 的声明式 `T.gemm`，Triton 的 `triton.Config` 对应 TileLang 的 `get_configs`/Roller hints**——这套对照会把「GPU kernel DSL 的两个抽象层级」彻底讲透。
