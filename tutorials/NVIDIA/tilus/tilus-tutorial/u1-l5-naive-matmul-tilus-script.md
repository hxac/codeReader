# 从 naive matmul 理解 Tilus Script 全貌

## 1. 本讲目标

本讲以 `examples/matmul/matmul_v0.py` 为对象，把前面四讲学到的零散知识（Script 骨架、数据类型、指针类型、`global_view`/`load_global`/`store_global`）第一次完整地串成一个「能跑、能验证、能测性能」的真实内核——矩阵乘法。

学完后你应当能够：

- 说清 `block_m` / `block_n` / `block_k` 三个分块超参各自的含义，以及它们如何决定网格（grid）的形状；
- 解释为什么用一个 `float32` 的 `register_tensor` 当累加器，并在 K 维循环里反复 `dot`；
- 理解 `dot` 完成后为什么要 `cast` 成 `float16` 再 `store_global` 回显存；
- 会用 `blockIdx` 计算当前线程块负责的输出子矩阵（tile）偏移；
- 看懂 `main()` 里的正确性校验与 TFLOPS 计算方法，并能动手改超参做一次小 benchmark。

本讲是整个 matmul 进阶系列（v0 → v5）的起点。v0 故意写得「能对就行」，不做任何性能优化，目的是先建立正确的心智模型。

## 2. 前置知识

阅读本讲前，请确认你已经掌握前四讲建立的术语：

- **Script 骨架**：一个内核就是继承 `tilus.Script` 的类，`__init__` 写编译期超参，`__call__` 写算子逻辑；`Script.__new__` 会拦截构造，返回已 JIT 编译、可直接调用的 `InstantiatedScript`（见 u1-l3）。
- **网格与线程块**：`attrs.blocks` 决定线程块数量，`attrs.warps` 决定每个线程块的 warp 数；`blockIdx.x/y/z` 标识当前块。
- **数据流四件套**：`global_view`（把裸指针包装成全局张量视图，不搬运）、`load_global`（全局 → 寄存器）、`store_global`（寄存器 → 全局）、元素级运算只发生在寄存器张量上（见 u1-l3）。
- **类型标注**：`int` 为编译期常量（换值重编译），`int32` 为运行时参数，`~float16` 为指向 `float16` 数组的指针（见 u1-l4）。

如果你对矩阵乘法的分块（tiling）思想本身还不熟，下面这段直觉会有帮助：

一个 \([M, K] \times [K, N]\) 的矩阵乘，可以按输出维度切。我们把输出矩阵 \(C\) 切成若干 \(\text{block\_m} \times \text{block\_n}\) 的小块，每个小块由一个线程块负责；而每个小块又需要沿着 \(K\) 维做累加，\(K\) 太长就再切成若干 \(\text{block\_k}\) 的段，循环累加。这样三个超参就对应「输出的行分块、列分块、规约维的分段」。

矩阵乘的总浮点运算量（FLOPs）是：

\[
\text{FLOPs} = 2 \cdot M \cdot N \cdot K
\]

因为每个输出元素要做 \(K\) 次「乘 + 加」，共 \(2K\) 次浮点操作，再乘上 \(M \cdot N\) 个输出元素。本讲 `main()` 里的 `tflops = 2 * m * n * k / latency * 1e-9` 正是据此计算吞吐。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/matmul/matmul_v0.py` | 本讲主角：naive matmul 内核 + 正确性校验 + benchmark，逐行精读对象。 |
| `examples/matmul/matmul_v1.py` | 对照组：在 v0 基础上引入共享内存（`shared_tensor`/`store_shared`/`load_shared`/`sync`），用于体会「下一步优化方向」。 |
| `python/tilus/lang/script.py` | `Script` 基类与 `Attributes`（`blocks`/`cluster_blocks`/`warps`），解释网格属性如何声明。 |
| `python/tilus/lang/instructions/root.py` | `RootInstructionGroup`，提供 `blockIdx`/`global_view`/`load_global`/`store_global`/`register_tensor`/`dot`/`cast` 等本讲用到的全部通用指令。 |
| `python/tilus/utils/py.py` | `cdiv`（向上取整除法），用于把总维度换算成网格大小。 |
| `python/tilus/utils/bench_utils.py` | `benchmark_func`，带 L2 缓存清理的计时函数，`main()` 用来测延迟。 |

## 4. 核心概念与源码讲解

### 4.1 分块超参与网格划分

#### 4.1.1 概念说明

naive matmul 的第一步不是写计算，而是回答两个问题：

1. 一共要启动多少个线程块？（网格形状）
2. 当前这个线程块，负责输出矩阵的哪一块？（偏移计算）

答案都封装在三个编译期超参里。`__init__` 里把 `block_m`/`block_n`/`block_k` 写死为常量，它们决定了「每个线程块处理的 tile 大小」：

```python
self.block_m = 64   # 每块处理输出 C 的 64 行
self.block_n = 64   # 每块处理输出 C 的 64 列
self.block_k = 16   # 每次沿 K 维搬运/计算 16 的段
```

为什么 `block_m`/`block_n`/`block_k` 放在 `__init__`？因为它们是**编译期超参**——改变它们会改变生成的 IR（tile 形状变了），所以必须 JIT 时确定（回顾 u1-l4 对编译期常量的说明）。

#### 4.1.2 核心流程

给定输入维度 \(M, N, K\) 与分块 \(\text{block\_m}, \text{block\_n}, \text{block\_k}\)：

1. **网格形状**：沿 M 维有 \(\lceil M / \text{block\_m} \rceil\) 个块，沿 N 维有 \(\lceil N / \text{block\_n} \rceil\) 个块，二者构成二维网格。
2. **块索引到偏移**：当前块 `(blockIdx.x, blockIdx.y)` 负责输出子矩阵
   \[
   C[\,\text{block\_m} \cdot \text{blockIdx.x}\;:\;,\;\text{block\_n} \cdot \text{blockIdx.y}\;:\,]
   \]
3. **K 维不并行**：每个块内部用一个 `for` 循环沿 K 维遍历，循环次数为 \(\lceil K / \text{block\_k} \rceil\)。

用伪代码表示：

```text
grid  = (cdiv(M, block_m), cdiv(N, block_n))     # 二维网格
对每个线程块 (bx, by):
    offset_m = block_m * bx
    offset_n = block_n * by
    acc = 0                          # [block_m, block_n] 累加器
    对 k = 0 .. cdiv(K, block_k):
        offset_k = block_k * k
        a = A[offset_m: , offset_k:] 的 [block_m, block_k] 切片
        b = B[offset_k: , offset_n:] 的 [block_k, block_n] 切片
        acc += a @ b
    C[offset_m: , offset_n:] = acc   # 写回 [block_m, block_n]
```

#### 4.1.3 源码精读

先看 `__init__` 里三个超参的定义：

> 三个分块超参 `block_m/block_n/block_k` 写死为编译期常量。
>
> [examples/matmul/matmul_v0.py:49-55](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L49-L55)

接着是网格与线程块数的声明。`attrs.blocks` 是一个列表，给出网格各维大小；`attrs.warps` 给出每个线程块的 warp 数（必须是编译期常量）。注意 `m_size` 标注为 `int32`（运行时参数），所以 `cdiv(m_size, self.block_m)` 是一个运行时表达式——这正是 v0 用二维网格、且网格大小可随 `m_size` 变化的关键：

> `attrs.blocks = [cdiv(m_size, block_m), cdiv(n_size, block_n)]` 声明二维网格；`attrs.warps = 1` 表示每个块只用 1 个 warp。
>
> [examples/matmul/matmul_v0.py:66-70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L66-L70)

`attrs.blocks` 与 `attrs.warps` 的声明能力来自 `Attributes` 数据类，`blocks` 默认 `None`（必须在 `__call__` 里赋值），`cluster_blocks` 默认 `(1,1,1)`（本讲用不到集群），`warps` 默认 `None`（也必须显式设置）：

> `Attributes` 定义了 `blocks`/`cluster_blocks`/`warps` 三类网格属性。
>
> [python/tilus/lang/script.py:29-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L29-L38)

`blockIdx` 是 `RootInstructionGroup` 暴露的属性，背后是 CUDA 内置变量 `blockIdx.x/y/z`，封装成 `Dim3`（一个带 `x/y/z` 的三元组）：

> `blockIdx` 属性返回封装了 CUDA `blockIdx.x/y/z` 的 `Dim3`。
>
> [python/tilus/lang/instructions/root.py:33-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L33-L36)

于是当前块负责的输出偏移就是简单的乘法（注意它们标注为 `int32`，是运行时变量）：

> `offset_m = block_m * blockIdx.x`、`offset_n = block_n * blockIdx.y` 计算当前块负责的输出子矩阵左上角坐标。
>
> [examples/matmul/matmul_v0.py:73-74](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L73-L74)

`cdiv` 是「向上取整除法」，定义极简：

> `cdiv(a, b) = (a + (b - 1)) // b`，即 `⌈a/b⌉`。
>
> [python/tilus/utils/py.py:26-27](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/py.py#L26-L27)

> **越界约定（重要）**：与 vector_add（u1-l3）一样，v0 **不做越界检查**。它隐含要求 \(M \% \text{block\_m} == 0\)、\(N \% \text{block\_n} == 0\)、\(K \% \text{block\_k} == 0\)。`main()` 里用的 `4096` 恰好能被 `64/64/16` 整除，所以不会越界。后面 v2+ 才会处理边界。

#### 4.1.4 代码实践

**实践目标**：亲手验证「网格形状 = cdiv(维度, 分块)」，建立对网格划分的直觉。

**操作步骤**（纯 Python，不依赖 GPU）：

1. 在一个普通 Python 脚本里 `from tilus.utils import cdiv`。
2. 取 \(M=N=K=4096\)，分块 `64/64/16`，打印网格形状与 K 维循环次数。
3. 再改成 \(M=4000\)（不能被 64 整除），观察 cdiv 的结果。

参考代码（**示例代码**，非项目原有）：

```python
from tilus.utils import cdiv

M, N, K = 4096, 4096, 4096
block_m, block_n, block_k = 64, 64, 16

grid = (cdiv(M, block_m), cdiv(N, block_n))
k_iters = cdiv(K, block_k)
print("grid:", grid, "k_iters:", k_iters, "total blocks:", grid[0] * grid[1])

# 不能整除的情形
M2 = 4000
print("cdiv(4000, 64) =", cdiv(M2, block_m))  # 最后一块会越界 -> v0 不能直接用于此形状
```

**需要观察的现象**：`4096/64 = 64`，所以网格是 `(64, 64)`，共 4096 个线程块；K 维循环 `4096/16 = 256` 次。当 `M=4000` 时 `cdiv(4000,64)=63`，但第 63 块会读到 A 的越界行——这解释了为什么 v0 要求维度整除。

**预期结果**：输出 `grid: (64, 64) k_iters: 256 total blocks: 4096`，以及 `cdiv(4000, 64) = 63`。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：若把 `block_n` 从 64 改成 128（M=N=K=4096 不变），网格形状和总块数如何变化？

**答案**：N 维块数变为 `cdiv(4096,128)=32`，网格变为 `(64, 32)`，总块数 `64*32=2048`（减半）。每个块要算的列数翻倍，所以块数减半。

**练习 2**：为什么 `attrs.warps` 必须是编译期常量，而 `attrs.blocks` 可以依赖运行时的 `m_size`？

**答案**：`attrs.warps` 决定每个线程块内的线程划分，影响生成的 IR 与寄存器布局，必须在编译期固定；`attrs.blocks` 只是启动时的网格维度参数，是运行时传给 launch 的值，不改变单个线程块内的代码，因此可以随 `m_size` 变化。

---

### 4.2 register_tensor 累加器与 K 维循环

#### 4.2.1 概念说明

网格划分解决了「谁算哪块输出」，但每个输出小块 \(C_{\text{tile}}\) 还需要沿 K 维累加：

\[
C_{\text{tile}} = \sum_{k=0}^{\lceil K/\text{block\_k}\rceil - 1} A_{\text{tile},k} \cdot B_{\text{tile},k}
\]

这就需要一个**累加器**：在 K 维循环之前创建一次，初值为 0，循环里每步都把新的乘积累加上去。在 Tilus 里，累加器是一个 `register_tensor`（寄存器张量），原因有二：

- 寄存器是离计算最近的存储，`dot` 直接消费寄存器张量；
- 累加需要高精度，所以累加器用 `float32`，而输入/输出是 `float16`。

`register_tensor` 的 `init=0.0` 表示把所有元素初始化为 0，这正是累加器需要的起点。

#### 4.2.2 核心流程

1. **创建累加器**：`acc = register_tensor(dtype=float32, shape=[block_m, block_n], init=0.0)`，在循环**之外**创建一次。
2. **K 维循环**：`for k in range(cdiv(k_size, block_k))`，每轮算出 `offset_k = k * block_k`。
3. **取切片**：`load_global` 从 `ga`/`gb` 取出 `[block_m, block_k]` 与 `[block_k, block_n]` 的切片到寄存器张量 `a`/`b`。
4. **累加**：`self.dot(a, b, acc, out=acc)`，即 `acc = a @ b + acc`，原地写回 `acc`。

注意第 4 步的 `out=acc`：`dot` 的语义是 `out = a @ b + c`，这里 `c` 和 `out` 都是 `acc`，所以是**原地累加**，不会每轮分配新张量。

#### 4.2.3 源码精读

先看 `global_view` 怎么把三个裸指针变成带形状的全局张量视图（不搬运数据，只是描述「这个指针指向一个 [M,K] 的 fp16 矩阵」）：

> `ga = global_view(a_ptr, dtype=float16, shape=[m_size, k_size])` 把 `a_ptr` 包装成形状为 `[M,K]` 的全局张量视图。
>
> [examples/matmul/matmul_v0.py:77-78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L77-L78)

`global_view` 的实现：未给 `strides` 时默认按紧凑行优先（row-major）构造 `GlobalLayout`，然后委托给 builder：

> `global_view` 默认按行优先紧凑布局构造全局张量视图。
>
> [python/tilus/lang/instructions/root.py:422-470](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L422-L470)

接着是累加器创建（循环之外，只创建一次）：

> `acc = register_tensor(dtype=float32, shape=[block_m, block_n], init=0.0)` 创建初值为 0 的 fp32 累加器。
>
> [examples/matmul/matmul_v0.py:81-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L81-L83)

`register_tensor` 的签名与 `init` 语义：`init` 可以是标量（全部元素初始化为该值），也可以是按索引返回表达式的回调。这里传 `0.0`，所有元素清零：

> `register_tensor(dtype, shape, init=None)`：`init` 为标量时把所有元素初始化为该值。
>
> [python/tilus/lang/instructions/root.py:294-344](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L294-L344)

然后是 K 维循环体的核心——`load_global` 取切片 + `dot` 累加：

> 循环里 `load_global` 取出 A/B 的 tile，再 `self.dot(a, b, acc, out=acc)` 原地累加。
>
> [examples/matmul/matmul_v0.py:86-99](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L86-L99)

`load_global` 从全局张量按 `offsets` 取出指定 `shape` 的切片到寄存器张量；`offsets` 长度必须等于全局张量的维数：

> `load_global(src, offsets=[...], shape=[...])` 按偏移取切片到寄存器张量。
>
> [python/tilus/lang/instructions/root.py:472-522](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L472-L522)

`dot` 的语义与形状约束（`a:[m,k]`、`b:[k,n]`、`c/out:[m,n]`，三者必须 2D 且形状匹配）：

> `dot(a, b, c=None, out=None)` 计算 `out = a @ b + c`；不传 `c` 时需给 `acc_dtype`。
>
> [python/tilus/lang/instructions/root.py:852-932](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L852-L932)

> **v0 vs v1 的累加写法对比**：v0 用 `self.dot(a, b, acc, out=acc)`（原地累加，`acc` 始终是同一个张量）；v1 用 `acc = self.dot(a, b, acc)`（不传 `out`，`dot` 内部分配新张量并返回，再重新绑定给 `acc`）。两者数学等价，但 v0 的写法更省寄存器分配。详见 [examples/matmul/matmul_v1.py:82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v1.py#L82)。

#### 4.2.4 代码实践

**实践目标**：体会「fp32 累加器」的必要性——如果用 fp16 累加会损失精度。

**操作步骤**（源码阅读 + 思考型，无需 GPU）：

1. 阅读 [examples/matmul/matmul_v0.py:81-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L81-L83)，确认 `acc` 的 `dtype=float32`。
2. 假设把累加器改成 `dtype=float16`（即 `register_tensor(dtype=float16, shape=[block_m, block_n], init=0.0)`，并把 `dot` 的累加也落在 fp16），思考 K=4096、block_k=16 共 256 次累加时，fp16 的有限精度（约 3 位有效十进制）会如何影响结果。
3. 对照 `main()` 里的容差：`torch.testing.assert_close(c_expect, c_actual, atol=1e-2, rtol=1e-2)`。

**需要观察的现象 / 预期结果**：fp16 累加 256 次后，误差会显著放大，可能突破 `atol=1e-2` 的容差而校验失败；这正是 v0 坚持「输入 fp16、累加 fp32、输出再 cast 回 fp16」的原因。**若你本地有 GPU，可实际把 `acc` 改成 fp16 跑一次复现该现象（待本地验证）。**

#### 4.2.5 小练习与答案

**练习 1**：`acc` 为什么在 `for` 循环**外面**创建，而不是每轮 `load_global` 后新建？

**答案**：因为 `acc` 是跨所有 K 段的累加器，必须在整个 K 维循环期间持续保存部分和。若每轮新建，上一轮的累加结果就丢了。`init=0.0` 只在创建时清零一次。

**练习 2**：`load_global` 取出的 `a` 形状是 `[block_m, block_k]`，`b` 是 `[block_k, block_n]`，`acc` 是 `[block_m, block_n]`。请用矩阵乘形状规则验证 `dot(a, b, acc)` 合法。

**答案**：`a:[m,k] @ b:[k,n] = [m,n]`，其中 `m=block_m, k=block_k, n=block_n`，结果 `[block_m, block_n]` 与 `acc` 形状一致，满足 `dot` 对 `a.shape[1]==b.shape[0]`、`a.shape[0]==c.shape[0]`、`b.shape[1]==c.shape[1]` 的约束。

---

### 4.3 dot、cast 与 store_global 收尾

#### 4.3.1 概念说明

K 维循环结束后，`acc` 里已经是当前线程块负责的 `[block_m, block_n]` 完整结果，但它是 `float32`，而输出矩阵 `C` 要求 `float16`。所以收尾做两件事：

1. **`cast`**：把 `acc` 从 `float32` 转成 `float16`（寄存器层 dtype 转换，回顾 u1-l4 提到的「`cast` 在寄存器层改变 dtype」）。
2. **`store_global`**：把 cast 后的寄存器张量写回输出矩阵 `C` 的对应 tile，偏移正是第 4.1 节算出的 `[offset_m, offset_n]`。

这一步还顺带创建输出视图 `gc = global_view(c_ptr, ...)`，与输入视图的创建方式完全对称。

#### 4.3.2 核心流程

```text
# 循环结束后
acc_f16 = cast(acc, dtype=float16)              # fp32 -> fp16（寄存器层）
gc = global_view(c_ptr, dtype=float16, shape=[M, N])   # 输出视图
store_global(gc, acc_f16, offsets=[offset_m, offset_n])  # 写回对应 tile
```

`store_global` 的 `offsets` 与 `load_global` 对称：`offsets` 给出目标全局张量每个维度的起始偏移，寄存器张量的形状决定了写入的 tile 大小。

#### 4.3.3 源码精读

收尾三行：

> `cast(acc, float16)` 把累加结果转成 fp16，`global_view` 建输出视图，`store_global` 按 `[offset_m, offset_n]` 写回。
>
> [examples/matmul/matmul_v0.py:102-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L102-L104)

`cast` 的签名极简，只做 dtype 转换：

> `cast(x, dtype)` 把寄存器张量转到目标 dtype。
>
> [python/tilus/lang/instructions/root.py:934-953](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L934-L953)

`store_global` 把寄存器张量写入全局张量的切片，`offsets` 长度须等于目标全局张量维数：

> `store_global(dst, src, offsets=[...])` 把寄存器张量写入全局张量的对应切片。
>
> [python/tilus/lang/instructions/root.py:524-566](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L524-L566)

**正确性校验与性能测量**在 `main()` 里：先用 `a @ b` 得到期望值，再启动内核、`torch.cuda.synchronize()`、`assert_close` 校验，最后用 `benchmark_func`（带 warmup、repeat、L2 缓存清理）测延迟，并按 `2*m*n*k/latency*1e-9` 算 TFLOPS：

> `main()` 创建内核实例、启动、用 `assert_close` 校验、用 `benchmark_func` 测延迟并算 TFLOPS。
>
> [examples/matmul/matmul_v0.py:142-174](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L142-L174)

`benchmark_func` 的签名（`warmup`/`repeat`/`clear_l2_cache`），它会在每次计时前清 L2，避免数据驻留缓存造成乐观偏差：

> `benchmark_func(run_func, warmup=1, repeat=5, clear_l2_cache=True)`。
>
> [python/tilus/utils/bench_utils.py:70-90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/bench_utils.py#L70-L90)

> **v1 在收尾前还多了什么？** 对照 [examples/matmul/matmul_v1.py:85-90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v1.py#L85-L90)：v1 在 cast/store 之前会 `free_shared(sa)` / `free_shared(sb)` 释放共享内存。v0 没有用共享内存，所以收尾只有 cast + store 两步。这个对比能帮你理解后续优化引入的资源管理负担。

#### 4.3.4 代码实践

**实践目标**：完整跑通 v0，看懂「cast 在校验容差里的体现」。

**操作步骤**（需要 GPU；若无 GPU 则改为源码阅读型）：

1. 确认已按 u1-l2 安装好 Tilus 并能 `import tilus`。
2. 运行 `python examples/matmul/matmul_v0.py`。
3. 观察打印的 pandas DataFrame，记录 `tilus` 行的 `latency (ms)` 与 `tflops`，并与 `torch` 行对比。

**需要观察的现象**：v0 的 TFLOPS 会**远低于** `torch.matmul`（后者走 cuBLAS）。这是预期的——v0 每轮 `load_global` 直接从全局内存取数据到寄存器，既没用共享内存做数据复用，也没用张量核友好的布局，性能很差。本讲只追求「正确」，性能留给 v1+。

**预期结果**：`assert_close` 通过（说明结果正确），但 tilus 的 TFLOPS 显著低于 torch。**待本地验证**（无 GPU 环境可跳过运行，改为阅读 `main()` 代码并口述校验流程）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `cast(acc, dtype=float16)` 这一行删掉，直接 `store_global(gc, acc, offsets=[...])` 会怎样？

**答案**：`acc` 是 `float32`，而 `gc` 的 `dtype=float16`，dtype 不匹配。`store_global` 要求源/目标 dtype 一致（参见其兄弟方法 `store_shared` 里对 dtype 的校验逻辑），会报错或需要显式 cast。所以 cast 这一步不可省。

**练习 2**：`store_global` 的 `offsets=[offset_m, offset_n]` 与第 4.1 节的 `offset_m/offset_n` 是同一对变量吗？为什么必须一致？

**答案**：是的，完全是同一对。当前块在加载（`load_global` 的 `[offset_m, offset_k]`）和写出（`store_global` 的 `[offset_m, offset_n]`）时用的是同一个输出 tile 左上角坐标，保证「算哪块、写哪块」一一对应。若写出的 offset 与累加时对应的输出位置不一致，就会把结果写到错误的 C 子矩阵。

---

## 5. 综合实践

**任务**：把 v0 改造成「分块超参可调」的版本，benchmark 几组配置，记录 TFLOPS，体会分块对性能的影响。

**背景**：v0 的 `__init__` 把 `block_m/block_n/block_k` 写死成 `64/64/16`。要做参数扫描，最干净的做法是模仿 v1，让 `__init__` 接收这三个参数。参考 v1 的签名 [examples/matmul/matmul_v1.py:18-23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v1.py#L18-L23)。

**操作步骤**：

1. 复制 `matmul_v0.py` 为 `matmul_v0_sweep.py`（放在你自己的工作目录，**不要改动仓库原始示例**）。
2. 把 `__init__` 改成可传参：

   ```python
   class MatmulV0(tilus.Script):
       def __init__(self, block_m=64, block_n=64, block_k=16):
           super().__init__()
           self.block_m = block_m
           self.block_n = block_n
           self.block_k = block_k
   ```

   （**示例代码**，基于 v0 改写。）

3. 在 `main()` 里循环若干组配置（建议先用 MMA 友好的形状，避免触发不支持的小块），例如：

   ```python
   configs = [
       (64, 64, 16),
       (64, 64, 32),
       (128, 64, 16),
       (128, 128, 16),
   ]
   ```

   对每组 `(bm, bn, bk)`，确保 `M % bm == 0`、`N % bn == 0`、`K % bk == 0`（用 `M=N=K=4096` 时上述配置都满足），实例化 `MatmulV0(bm, bn, bk)`，校验正确性并测 TFLOPS。

4. 把每组 `(bm, bn, bk)` 与对应 TFLOPS 整理成表格，观察哪个维度（行块、列块、K 段）对性能影响最大。

**需要观察的现象**：

- 增大 `block_m`/`block_n` 通常让单个线程块做更多计算、提高数据复用，TFLOPS 往往上升，但也会占用更多寄存器。
- 增大 `block_k` 让每次 `load_global` 取更多数据、减少 K 维循环次数，但寄存器压力增大。
- 即便如此，v0 系列（无共享内存、无流水线）的 TFLOPS 仍会远低于 cuBLAS，这恰好引出下一讲对共享内存与张量核的优化。

**预期结果**：得到一张「配置 → TFLOPS」表，能看到分块对性能的明显影响趋势；但绝对性能仍然偏低。**待本地验证。**

> **提示**：若某组配置报「shape 不支持」之类的错误，多半是该 block 形状不能直接映射到硬件 MMA 指令（例如 fp16 MMA 通常要求 m/n/k 为 16 的倍数）。可跳过该配置并在报告里注明原因，这正是后续讲义「布局系统」要解决的问题。

## 6. 本讲小结

- naive matmul 用三个编译期超参 `block_m/block_n/block_k` 描述 tile：前两个决定输出分块大小与二维网格形状（`cdiv(M,block_m) × cdiv(N,block_n)`），第三个决定 K 维分段。
- 当前线程块负责的输出 tile 左上角由 `offset_m = block_m * blockIdx.x`、`offset_n = block_n * blockIdx.y` 计算；`blockIdx` 来自 `RootInstructionGroup`。
- 累加器 `acc` 是一个 `float32` 的 `register_tensor`，在 K 维循环外用 `init=0.0` 创建一次；循环里 `load_global` 取 A/B 切片，`self.dot(a, b, acc, out=acc)` 原地累加。
- `dot` 的语义是 `out = a @ b + c`，要求 `a:[m,k]`、`b:[k,n]`、`c/out:[m,n]` 且形状匹配。
- 收尾用 `cast` 把 fp32 累加结果转成 fp16，再用 `store_global` 按 `[offset_m, offset_n]` 写回输出矩阵；`main()` 用 `assert_close` 校验、用 `benchmark_func` 测延迟、按 `2*M*N*K` 算 FLOPs。
- v0 不做越界检查（要求维度整除），也不用共享内存/流水线——它只建立正确的心智模型，性能优化留给 v1+。

## 7. 下一步学习建议

本讲只解决了「能跑对」的问题，性能很差。接下来建议：

1. **精读 `examples/matmul/matmul_v1.py`**：它引入 `shared_tensor`/`store_shared`/`load_shared`/`sync`/`free_shared`，把每轮数据先搬到共享内存再喂给寄存器，是理解「数据复用」的第一步。本讲的 v0/v1 对比已经为它铺好了路。
2. **进入第 U2 单元**：系统学习 Tilus Script 编程模型——`Script.__new__` 与 `InstantiatedScript` 的实例化机制（u2-l1）、指令与指令组的分层（u2-l2）、控制流与线程组（u2-l3）、以及 `@autotune` 如何把本讲手动扫配置的工作自动化（u2-l4）。
3. **带着问题读下去**：为什么 v0 直接 `load_global` 到寄存器会很慢？共享内存如何让一个 tile 被多次复用？`dot` 到底映射到了哪条 MMA 指令？这些都会在后续 IR、布局系统与后端代码生成的讲义里逐一回答。
