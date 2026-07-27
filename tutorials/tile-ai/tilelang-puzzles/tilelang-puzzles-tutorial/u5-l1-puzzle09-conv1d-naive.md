# Puzzle 09 Conv1D Naive：滑动窗口与 Halo 区域

## 1. 本讲目标

本讲是「卷积、量化与性能工程」单元的第一讲。我们将用 Puzzle 09 的**朴素 1D 卷积（Conv1D Naive）**把前几讲学到的积木（分块、shared memory、`T.Parallel` / `T.Serial`、高精度累加）组装成一个全新的算子——卷积。

学完后你应当能够：

1. 说清 1D 卷积的**滑动窗口**计算过程，以及它带来的**数据复用 / 数据依赖**（相邻输出共享输入）。
2. 理解为什么卷积天然适合用 **shared memory**：把一段输入连同一个 **halo（重叠）区域**一次性搬进 block 内共享内存，让多个输出位置复用同一份数据。
3. 写出 `T.Parallel` 外层 + `T.Serial` 内层的嵌套循环结构，并解释为什么内层必须串行、以及 `if j + k < L` 边界检查的作用。
4. 独立补全 `puzzles/09-conv.py` 中的 `tl_conv1d_naive`，并通过 `test_puzzle` 与 PyTorch 参考结果比对。

> 本讲只做**朴素**实现（手写乘加循环）。把卷积转成矩阵乘的 im2col 写法、以及用 `T.gemm` 加速的版本，留给下一讲（u5-l2）。

## 2. 前置知识

本讲假定你已经学过 u4-l4（GEMM 优化）。我们会复用其中几个关键概念，但会指出**用法上的关键区别**：

- **shared memory（共享内存）**：GPU 片上、单个 block 内所有线程可见、比 global memory 快得多的存储。在 u4-l4 里，我们把 GEMM 的输入 tile 放进 shared memory，主要动机是「缓解寄存器压力 + 对齐 `T.gemm` 的输入来源」。本讲里 shared memory 的动机**不同**：卷积有天然的**数据复用**，把被多次复用的输入放进 shared memory 能成倍减少对显存的访问。同一个 `T.alloc_shared`，两种动机。
- **`T.alloc_fragment`（寄存器片段）**：block 内每个线程的寄存器被统一抽象成一块可按下标访问的 buffer。本讲里它用来放**不被跨输出复用**的数据（卷积核 K、输出累加器 O）。
- **`T.Parallel` vs `T.Serial`**：`T.Parallel` 声明「这些迭代互相独立、可并行」；`T.Serial` 声明「必须串行」。在 u3-l2（reduce sum）和 u4-l2（GEMV）里我们用过 `T.Serial` 做串行累加，原因都是「多段结果累加进同一个累加器，存在写依赖」。本讲的内层循环同样如此。
- **混合精度累加**：输入输出 `float16`，累加器 `float32`（`accum_dtype`），`.astype(accum_dtype)` 要放在**相乘之前**。这条来自 u4-l2 / u4-l3，本讲原样沿用。
- **`test_puzzle` / `bench_puzzle`**：项目共享的「正确性 + 性能」验证框架，详见 u1-l2。

如果你对上面任何一条感到陌生，建议先回到对应讲义复习，再继续本讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py) | 题目本体：含数学定义、`ref_conv1d` 参考实现，以及带 `TODO` 的 `tl_conv1d_naive` 骨架与本讲要补全的位置。 |
| [ans/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py) | 参考答案：`tl_conv1d_naive` 的完整实现，是本讲源码精读的主要对象。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle` / `bench_puzzle` 框架：自动造随机输入、跳过最后一个（输出）张量、用 `torch.allclose` 比对。 |
| [docs/zh/9.conv/1.conv.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/9.conv/1.conv.md) | 项目自带的中文讲解，含滑动窗口示意与 im2col 流程图，可作扩展阅读。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块咬合得很紧：模块 4.1 讲清「卷积要算什么、为什么有数据依赖」；模块 4.2 讲清「如何用 shared memory + halo 一次性喂饱这些依赖」；模块 4.3 讲清「循环怎么排、边界怎么查」。

### 4.1 滑动窗口卷积与数据依赖

#### 4.1.1 概念说明

**1D 卷积**是深度学习里的核心算子（CNN、时序模型都在用）。注意：它沿用的是 `torch.conv1d` 的约定——计算顺序上更接近**互相关（cross-correlation）**，即卷积核**不翻转**，直接在输入上滑动相乘求和。项目文档也专门提醒了这一点。

给定输入 `X: [N, L]` 和卷积核 `K: [KL]`，输出 `O: [N, L]` 的定义是：

\[
O[i, j] = \sum_{k=0}^{KL-1} \mathbb{1}[\,j+k < L\,] \cdot X[i,\, j+k] \cdot K[k]
\]

其中 \(\mathbb{1}[j+k < L]\) 是指示函数：当窗口滑出序列右边界（\(j+k \ge L\)）时这一项为 0。

直观地说，对每个输出位置 \(j\)，把长度为 \(KL\) 的窗口「贴」在 \(X[i, j..j+KL-1]\) 上，与卷积核逐位相乘再求和，就是 \(O[i, j]\)。窗口每次右移一格，所以叫**滑动窗口（sliding window）**。

#### 4.1.2 核心流程

把定义展开成伪代码，就是题目里给出的三重循环：

```
for i in range(N):          # 批次 / 行
    for j in range(L):      # 输出位置（滑动窗口左端）
        ACC = 0
        for k in range(KL): # 窗口内的卷积核位置
            if j + k < L:   # 边界检查：窗口不能滑出序列
                ACC += X[i, j+k] * K[k]
        O[i, j] = ACC
```

卷积和前面几讲的元素级算子（Puzzle 02/03 的 vector add）有本质区别，体现在**数据依赖**上：

- **元素级**：每个输出 `O[i,j]` 只读自己那份输入，输出之间互不相干。
- **卷积**：相邻输出**共享**输入。计算 `O[i,j]` 要读 `X[i, j..j+KL-1]`，计算 `O[i,j+1]` 要读 `X[i, j+1..j+KL]`——两者共享 \(KL-1\) 个元素。

这就是卷积的**强数据复用**：每个输入元素会被最多 \(KL\) 个输出位置用到。比如 `KL=32` 时，一个 `X[i,m]` 最多参与 32 个输出的计算。这个特性既是优化的机会（搬一次、用多次），也是实现的约束（一个输出 tile 需要的输入 tile 比自己「长」一截）。

用一个迷你例子手算一遍最直观（取 `X=[1,2,3,4,5]`，`K=[a,b,c]`，`KL=3`）：

```
j=0: O[0] = 1·a + 2·b + 3·c          ← 读 X[0,1,2]
j=1: O[1] = 2·a + 3·b + 4·c          ← 读 X[1,2,3]，复用 X[1,2]
j=2: O[2] = 3·a + 4·b + 5·c          ← 读 X[2,3,4]，复用 X[2,3]
j=3: O[3] = 4·a + 5·b + 0·c          ← X[5] 越界 → 该项为 0（边界）
j=4: O[4] = 5·a + 0·b + 0·c          ← X[5,6] 越界 → 后两项为 0（边界）
```

注意 `j=3, j=4` 这两行：窗口滑出了序列右端，越界项按 0 处理——这就是边界检查的由来。

#### 4.1.3 源码精读

题目的数学定义就写在文件顶部注释里，是我们实现的唯一契约：

[puzzles/09-conv.py:41-49](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L41-L49) —— 定义 `O[i,j]` 的三重循环与 `if j + k < L` 边界检查，是本讲所有实现的「标准答案」。

PyTorch 参考实现用 `torch.conv1d` 实现，先在右侧补 `KL-1` 个 0（等价于把越界项变成 0），再调用卷积：

[puzzles/09-conv.py:60-81](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L60-L81) —— `ref_conv1d`：用 `pad(..., (0, KL-1))` 补零，再用 `torch.conv1d` 计算。`test_puzzle` 会拿它的输出和我们 kernel 的输出做 `torch.allclose` 比对。

> 题目注释里写 `1 <= N <= 64`、`1 <= L <= 1024`、`1 <= KL <= 32`，但 `run_conv1d_naive` 实际跑的是 `N=128, L=128, BLOCK_N=16, BLOCK_L=32, KL=32`（见 [ans/09-conv.py:117-128](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L117-L128)）。注释里的范围是「支持范围」，运行配置是「测试用例」，两者不矛盾。

#### 4.1.4 代码实践

**实践目标**：用 Python 手算一遍滑动窗口，确认你真的理解了定义，再跑参考实现看真实数值。

**操作步骤**：

1. 在 Python 里手动实现定义里的三重循环（纯 Python，不用 TileLang），与 `ref_conv1d` 对比：

```python
# 示例代码：纯 Python 参照定义，用于理解（不是项目原有代码）
import torch
def naive_py(X, K):
    N, L = X.shape
    KL = K.shape[0]
    O = torch.zeros((N, L), dtype=torch.float32)
    for i in range(N):
        for j in range(L):
            acc = 0.0
            for k in range(KL):
                if j + k < L:
                    acc += X[i, j+k].item() * K[k].item()
            O[i, j] = acc
    return O
```

2. 造一组小输入（如 `X = torch.randn(2, 8, dtype=torch.float16)`，`K = torch.randn(3, dtype=torch.float16)`），打印 `naive_py` 与 `ref_conv1d` 的结果，确认两者一致（注意 `ref_conv1d` 的输出是 float16）。

**需要观察的现象**：两个实现的逐元素数值应当非常接近（float16 精度内有微小差异）。特别留意序列**右端** `j` 接近 `L-1` 的几行，确认越界项确实按 0 处理。

**预期结果**：`torch.allclose(naive_py(X,K).half(), ref_conv1d(X,K), atol=1e-2)` 为 `True`。若你无法运行，这一步标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若把卷积核长度 `KL` 从 3 改成 1，卷积退化成什么运算？

> **答案**：\(O[i,j] = X[i,j] \cdot K[0]\)，即逐元素标量乘法（且无边界问题，因为窗口长度为 1 永不越界）。这正好说明「元素级乘法是卷积的退化特例」。

**练习 2**：对一个长度为 `L` 的行，所有输出加起来一共要访问 `X` 的元素多少次（不考虑复用，每次都从原数组读）？如果复用呢？

> **答案**：不考虑复用时，每个输出读 `KL` 次（边界处更少），总计约 \(L \cdot KL\) 次。但 `X` 实际只有 `L` 个元素，所以理想复用下每个元素最多被读 \(KL\) 次、全局只需读 \(L\) 次——这正是 shared memory 要消除的 \(KL\) 倍放大。

---

### 4.2 shared memory halo 区域

#### 4.2.1 概念说明

4.1 节告诉我们：卷积里每个输入元素会被最多 \(KL\) 个输出复用。如果每个输出都直接从 global memory 读 `X`，那么同一个 `X[i,m]` 会被反复从显存搬 \(KL\) 次——浪费带宽。

解决办法正是 **shared memory**：在 block 开始时，把这一段输出要用的所有 `X` 元素**一次性**搬进 block 内共享内存，之后所有输出都从 shared memory 读。搬一次，用 \(KL\) 次。

但这里有个关键问题：**一个输出 tile 需要的输入 tile 比自己「长」一截**。

- 假设一个 block 负责连续 `BLOCK_L` 个输出，左端在全局位置 `pid_l * BLOCK_L`。
- 第一个输出（`j=0`）需要输入 `X[pid_l*BLOCK_L + 0 .. +KL-1]`。
- 最后一个输出（`j=BLOCK_L-1`）需要输入 `X[pid_l*BLOCK_L + BLOCK_L-1 .. +BLOCK_L-1+KL-1]`。

所以这个 block 需要的输入范围是全局 `[pid_l*BLOCK_L, pid_l*BLOCK_L + BLOCK_L + KL - 1)`，共 `BLOCK_L + KL - 1` 个元素。比输出多出来的 `KL - 1` 个元素，就是所谓的 **halo（光晕 / 重叠）区域**——它「伸出」输出 tile 右端的那一截，是后续输出窗口探出去要读的数据。

> **关于 halo 长度的一个细节**：理论最小值是 `BLOCK_L + KL - 1`（项目文档 [docs/zh/9.conv/1.conv.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/9.conv/1.conv.md) 里写的 `BLOCK_L + KL - 1`）。而参考答案实际写的是 `BLOCK_L + KL`（多分配 1 列）。多分配一列是无害的「留一点余量」，不影响正确性，只是多占一点点 shared memory。本讲以**真实源码** `BLOCK_L + KL` 为准。

#### 4.2.2 核心流程

朴素 Conv1D 的「数据搬运」三段式：

```
1. 搬入：T.copy(X 的一个 (BLOCK_N, BLOCK_L+KL) 子块, X_local)   # 含 halo
2. 搬入：T.copy(K 整个, K_local)                                  # 卷积核很小，整体搬
3. 计算：在每个输出 (i,j) 上，沿 k 串行累加 X_local[i,j+k]*K_local[k]
4. 搬出：T.copy(O_local, O 的对应子块)
```

关键设计：**哪些数据放 shared，哪些放 fragment？**

| 数据 | 存储类型 | 为什么 |
| --- | --- | --- |
| `X_local`（输入 tile + halo） | `T.alloc_shared` | 被 block 内多个输出位置**复用**，放 shared 让所有线程共享同一份。 |
| `K_local`（卷积核） | `T.alloc_fragment` | 只有 `KL` 个元素，很小；对所有 `(i,j)` 是只读广播，放寄存器即可。 |
| `O_local`（输出累加器） | `T.alloc_fragment` | 每个输出 `O[i,j]` 只被自己那个 `(i,j)` 写入，**无需跨输出共享**，放寄存器（私有、快）。 |

这条取舍和 u4-l4 一脉相承：**被复用的、要被 block 内多线程共享的 → shared；私有的、累加用的 → fragment**。区别在于 u4-l4 的 shared 是为了喂 `T.gemm`，本讲的 shared 是为了喂滑动窗口的复用。

#### 4.2.3 源码精读

参考答案 `tl_conv1d_naive` 的存储分配就在这里：

[ans/09-conv.py:99-106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L99-L106) —— `with T.Kernel(N//BLOCK_N, L//BLOCK_L) as (pid_n, pid_l)` 启动二维 grid（N、L 两维各并行一个 block）；接着三行分配：`X_local = T.alloc_shared((BLOCK_N, BLOCK_L + KL), dtype)`（**shared，含 halo**）、`K_local = T.alloc_fragment((KL), dtype)`、`O_local = T.alloc_fragment((BLOCK_N, BLOCK_L), accum_dtype)`；再用两个 `T.copy` 把 X 子块（含 halo）和整个 K 搬进来，并用 `T.clear(O_local)` 把累加器清零。

注意三处细节：

1. **二维 grid**：`T.Kernel(N//BLOCK_N, L//BLOCK_L)` 的两个位置参数分别是 N 维、L 维的 block 数，解包出块索引 `pid_n`、`pid_l`（这一步和 Puzzle 03 / GEMM 的二维分块完全一致）。
2. **`X_local` 的第二维是 `BLOCK_L + KL`**：这就是 halo。`T.copy(X[pid_n*BLOCK_N, pid_l*BLOCK_L], X_local)` 从全局位置 `(pid_n*BLOCK_N, pid_l*BLOCK_L)` 开始，搬一个大小由 `X_local` 形状决定的子块——即 `BLOCK_L + KL` 列，比输出的 `BLOCK_L` 列多出 `KL` 列的 halo。
3. **`O_local` 用 `accum_dtype = float32`**：累加器高精度，沿用 u4-l2 / u4-l3 的混合精度约定。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `X_local` 确实落在 shared memory，并理解 halo 多出来那一截的作用。

**操作步骤**：

1. 先读 [common/utils.py:50-63](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L50-L63) 的 `_torch_tensor_materialize`，确认 `test_puzzle` 如何根据编译后 kernel 的 `params` 自动构造输入张量（并跳过最后一个输出张量）。这是本讲实践能「一行跑通」的基础。
2. 想象把 `X_local` 的第二维从 `BLOCK_L + KL` 改成 `BLOCK_L`（去掉 halo），然后回答：哪些输出位置会读越界？为什么一定出错？
3. （可选，待本地验证）在能跑 GPU 的环境里，编译答案并打印生成代码：

```python
# 示例代码：用于检视生成的 CUDA（不是项目原有代码）
src = tl_conv1d_naive.compile(N=128, L=128, KL=32, BLOCK_N=16, BLOCK_L=32).print_source_code()
```

**需要观察的现象**：在生成的 CUDA 里，`X_local` 对应一个 `__shared__` 修饰的缓冲区；而 `K_local`、`O_local` 对应寄存器。`X_local` 的第二维大小应为 `BLOCK_L + KL = 64`。

**预期结果**：去掉 halo 后，最后一个输出 `j = BLOCK_L - 1` 的窗口会读到 `X_local[i, BLOCK_L-1 + (KL-1)] = X_local[i, BLOCK_L+KL-2]`，若 buffer 只有 `BLOCK_L` 列则严重越界——程序崩溃或得到错误结果。完整 halo 版本则一切正常（待本地验证生成代码细节）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `K_local` 用 `T.alloc_fragment` 而不是 `T.alloc_shared`？

> **答案**：`K` 只有 `KL`（≤32）个元素，体量极小；而且它对所有 `(i, j)` 是**只读、相同**的值（广播）。放寄存器（fragment）访问最快，且无需 block 内跨线程共享，所以不必占用宝贵的 shared memory。

**练习 2**：若 `KL` 很大（比如接近 32），halo 区域占 shared memory 的比例会怎样？这会带来什么工程上的限制？

> **答案**：halo 占 `KL` 列，输入 tile 占 `BLOCK_L` 列。`KL` 越大，halo 占比越高（极端时 `KL ≈ BLOCK_L`，halo 与主体相当）。GPU 单个 block 的 shared memory 容量有限（通常 48KB～100KB+），halo 越大越容易撞到容量上限，这会反过来限制 `BLOCK_N`、`BLOCK_L` 能取多大。这也是后续 im2col / `T.gemm` 方案要优化的动机之一。

---

### 4.3 Parallel/Serial 嵌套与边界检查

#### 4.3.1 概念说明

数据搬到位之后，剩下一个问题：**计算循环怎么排？**

朴素 Conv1D 的计算是个四重循环：`i`（行）× `j`（输出位置）× `k`（卷积核）。关键判断每一层该并行还是串行：

- **外层 `(i, j)` → `T.Parallel`**：不同的输出 `O[i,j]` 之间互不相干（各自累加到自己的 `O_local[i,j]`），天然可并行。用 `for i, j in T.Parallel(BLOCK_N, BLOCK_L)` 把整个输出 tile 的迭代空间一次性并行划分（多维 `T.Parallel`，参见 Puzzle 03）。
- **内层 `k` → `T.Serial`**：对**同一个** `O_local[i,j]`，要沿 `k` 累加 `KL` 项。这些累加存在**写依赖**（后一项读改写同一个累加器），必须串行，和 u3-l2 / u4-l2 里 K 维串行累加是同一个道理。如果误用 `T.Parallel`，多个 `k` 同时写 `O_local[i,j]` 会产生竞态。

- **边界检查 `if j + k < L`**：直接对应定义里的指示函数 \(\mathbb{1}[j+k<L]\)。当窗口探出序列右端时，跳过这一项（等价于该项为 0）。在参考答案里，循环变量 `j` 是 tile 内的**局部**坐标（`0..BLOCK_L-1`），配合 halo 缓冲保证 `X_local[i, j+k]` 始终是合法的**局部**下标（`j+k ≤ BLOCK_L+KL-2 < BLOCK_L+KL`）。

> **关于边界语义的说明（待本地验证）**：在 `run_conv1d_naive` 的测试配置下（`L=128`，且 `L` 是 `BLOCK_L=32` 的整数倍），`j + k` 的最大值为 `BLOCK_L-1 + KL-1 = 31+31 = 62 < 128 = L`，因此这个边界判断**始终成立**，序列尾部的越界项主要由 **halo 缓冲 + 加载语义**兜底（halo 里超出序列右端的部分按 0 处理，恰好就是卷积尾部想要的 0 贡献）。换句话说，`if j + k < L` 在此配置下是一道与定义对齐的「安全护栏」。若你把 `L` 改成非 `BLOCK_L` 整数倍、或非常小的值，需要自行验证尾部若干输出的正确性——建议用 `test_puzzle(..., print_log=True)` 打印 diff 来确认。

#### 4.3.2 核心流程

把上面的判断写成结构化的伪代码：

```
T.clear(O_local)                           # 累加器先清零（必需）
for i, j in T.Parallel(BLOCK_N, BLOCK_L):  # 外层：每个输出独立 → 并行
    for k in T.Serial(KL):                 # 内层：累加进同一个 O_local[i,j] → 串行
        if j + k < L:                      # 边界：窗口不越界才累加
            O_local[i,j] += X_local[i, j+k].astype(float32) * K_local[k].astype(float32)
T.copy(O_local, O[...])                    # 搬回显存（float32 → float16 在此降精度）
```

三个易踩的坑：

1. **忘了 `T.clear(O_local)`**：`O_local` 是新分配的累加器，初始值未定义；不清零会累加进垃圾值。这是 u3-l2 讲过的「累加语义」铁律。
2. **内层误用 `T.Parallel`**：`KL` 项并行写同一个 `O_local[i,j]` → 竞态 → 结果错乱。累加循环必须 `T.Serial`。
3. **`.astype(accum_dtype)` 放错位置**：必须在**相乘之前**对两个操作数各自转成 float32，让乘法在 float32 完成；放乘法之后（先 fp16 乘再转）无法挽回精度损失。见 u4-l2。

#### 4.3.3 源码精读

参考答案的核心计算循环只有 6 行，但每一行都对应上面的一条判断：

[ans/09-conv.py:106-112](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L106-L112) —— `T.clear(O_local)` 清零；外层 `for i, j in T.Parallel(BLOCK_N, BLOCK_L)` 并行遍历输出 tile；内层 `for k in T.Serial(KL)` 串行累加；`if j + k < L:` 边界护栏；累加体 `O_local[i, j] += X_local[i, j + k].astype(accum_dtype) * K_local[k].astype(accum_dtype)` 在 float32 上做乘加；最后 `T.copy(O_local, O[pid_n*BLOCK_N, pid_l*BLOCK_L])` 把整个输出 tile 一次性搬回显存。

几个值得对照的细节：

- 这版**朴素**实现刻意**没有**用 `T.reduce_sum`（那是 Puzzle 05 的归约 TileOp）或 `T.gemm`（那是 Puzzle 08 的 Tensor Core 路径）。它就是最直白的「手写乘加 + 串行累加」，目的是让你看清卷积的计算骨架。（提交 `de37efb`「Simplify 09 Conv1D」正是把更早的 `temp` 缓冲 + `T.reduce_sum` 版本改成了现在这个直白的嵌套循环版，参见 [ans/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py) 的当前内容。）
- 整个 kernel **没有显式 `threads=`**（对比提交历史里旧版带 `threads=256`），线程分摊由框架根据 `T.Parallel` 自动决定——再次印证「`T.Parallel` 与 `threads` 解耦」（u2-l1）。
- 累加器 `O_local` 是 `float32`，但输出张量 `O` 是 `float16`，唯一的主动降精度发生在最后那行 `T.copy(..., O[...])`。

#### 4.3.4 代码实践

**实践目标**：亲手排好循环结构，并验证「内层必须串行」「累加器必须清零」这两条铁律。

**操作步骤**：

1. 在你的 `tl_conv1d_naive` 草稿里，**故意**把内层改成 `for k in T.Parallel(KL):`，运行 `test_puzzle`，观察结果。
2. 恢复成 `T.Serial` 后，**故意**注释掉 `T.clear(O_local)`，再运行，观察结果。
3. 两次都恢复正确后，确认 `✅ Results match: True`。

**需要观察的现象**：

- 步骤 1（内层并行）：`torch.allclose` 大概率失败（`❌`），因为多个 `k` 并发写同一个累加器，结果不确定、且每次运行可能不同。
- 步骤 2（不清零）：结果系统性偏大或为 `nan`/垃圾值，因为累加进了未初始化的寄存器内容。

**预期结果**：两种错误写法都会让 `test_puzzle` 报错或比对失败；只有 `T.Serial` + `T.clear` 的正确写法稳定通过（`atol=rtol=1e-2`）。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么外层 `(i, j)` 可以用 `T.Parallel`，而内层 `k` 必须用 `T.Serial`？用「写依赖」解释。

> **答案**：外层每个 `(i,j)` 写的是**不同的** `O_local[i,j]`，互不干涉，无写依赖 → 可并行。内层所有 `k` 写的是**同一个** `O_local[i,j]`（`+=` 是「读—改—写」），存在写依赖与竞态 → 必须串行，否则结果不确定。

**练习 2**：如果去掉 `if j + k < L` 这一行，在当前测试配置（`L=128, BLOCK_L=32, KL=32`）下结果会出错吗？为什么？

> **答案**：在**当前配置**下大概率**不会**出错，因为 `j+k` 最大为 62，恒小于 `L=128`，判断恒为真，去掉与否等价；同时尾部由 halo 兜底。但这道判断是「与定义对齐的安全护栏」，一旦换到 `L` 较小或非整数倍的配置就可能出错，所以应当保留。建议你实际改一下 `L` 验证（待本地验证）。

**练习 3**：把 `O_local` 的 dtype 从 `accum_dtype`（float32）改成 `dtype`（float16），会发生什么？

> **答案**：累加在 float16 上进行，`KL`（最多 32）项相加容易超出 float16 的有效精度，导致误差累积、`torch.allclose` 失败。这正是累加器必须高精度的原因（u4-l2 / u4-l3）。

---

## 5. 综合实践

现在把三个模块串起来，完成本讲的主任务：**补全 `puzzles/09-conv.py` 里的 `tl_conv1d_naive`**。

**任务目标**：参照参考答案的思路（但请先自己写，再对照 [ans/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py)），实现朴素 1D 卷积并通过 `test_puzzle`。

**操作步骤**：

1. 打开 [puzzles/09-conv.py:84-100](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L84-L100)，在 `# TODO: Implement this function` 处补全 `with T.Kernel(...)` 块。骨架里 `T.const`、`T.Tensor`、`T.empty`、`return O` 都已就绪，你只需写 device 计算部分。
2. 按以下顺序组织你的代码（对应三个模块）：
   - **分块与分配**：`with T.Kernel(N // BLOCK_N, L // BLOCK_L) as (pid_n, pid_l):`；`X_local = T.alloc_shared((BLOCK_N, BLOCK_L + KL), dtype)`（**含 halo**）；`K_local = T.alloc_fragment((KL,), dtype)`；`O_local = T.alloc_fragment((BLOCK_N, BLOCK_L), accum_dtype)`。
   - **搬入 + 清零**：`T.copy(X[pid_n*BLOCK_N, pid_l*BLOCK_L], X_local)`；`T.copy(K, K_local)`；`T.clear(O_local)`。
   - **计算**：`for i, j in T.Parallel(BLOCK_N, BLOCK_L):` 外层并行；`for k in T.Serial(KL):` 内层串行；`if j + k < L:` 边界；`O_local[i, j] += X_local[i, j+k].astype(accum_dtype) * K_local[k].astype(accum_dtype)`。
   - **搬出**：`T.copy(O_local, O[pid_n*BLOCK_N, pid_l*BLOCK_L])`。
3. 运行验证：

```bash
python3 puzzles/09-conv.py    # 会先跑 run_conv1d_naive，再跑 run_conv1d_im2col（下一讲内容）
# 或只验证本讲：
python3 -c "from puzzles import __init__" 2>/dev/null; python3 ans/09-conv.py
```

> 说明：`puzzles/09-conv.py` 的 `if __name__ == "__main__"` 会同时调用 `run_conv1d_naive()` 和 `run_conv1d_im2col()`。本讲只需关注前者（`run_conv1d_naive`，[puzzles/09-conv.py:103-114](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L103-L114)）；后者依赖你下一讲才补的 `tl_conv1d_multi_outchannel` / `tl_conv1d_im2col`，本讲可暂时忽略它的报错。

4. （可选）加一个基准对比，体会朴素版与 PyTorch 的性能差距。可仿照 `run_conv1d_im2col` 里的 `bench_puzzle(...)` 调用（[ans/09-conv.py:295-308](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L295-L308)）给 `tl_conv1d_naive` 加一次 `bench_puzzle(..., bench_torch=True)`。

**需要观察的现象**：

- `test_puzzle` 打印 `✅ Results match: True`。
- （若跑了 bench）朴素 Conv1D 通常**慢于** `torch.conv1d`——因为它手写乘加、没用 Tensor Core。这个差距正是下一讲 im2col + `T.gemm` 要弥补的。

**预期结果**：正确性通过；性能数据「待本地验证」（取决于你的 GPU）。完成本实践后，你就拥有了一个正确但未优化的 Conv1D kernel，作为下一讲优化的对照基线。

## 6. 本讲小结

- 卷积是**滑动窗口**算子：\(O[i,j]=\sum_k \mathbb{1}[j+k<L]\cdot X[i,j+k]\cdot K[k]\)，相邻输出**共享 \(KL-1\) 个输入**，这是它和元素级算子的本质区别。
- 因为强数据复用，被多次使用的输入 `X` 应放进 **shared memory**；一个输出 tile 需要的输入比自身多出 `KL-1`（实现里取 `KL`）列的 **halo 区域**。
- 存储取舍：`X_local` → shared（复用）、`K_local`/`O_local` → fragment（私有 / 广播）；这与 u4-l4 的 shared memory 用法动机不同但原则一致。
- 计算循环是**嵌套结构**：外层 `(i,j)` 用 `T.Parallel`（输出独立），内层 `k` 用 `T.Serial`（累加写依赖）；`T.clear` 必需、`.astype(float32)` 要在相乘前。
- `if j + k < L` 是与定义对齐的**边界护栏**；在测试配置下主要由 halo 兜底尾部，换配置时需自行验证。
- 本讲朴素版刻意不用 `T.gemm`，只用手写乘加——它正确但不快，是下一讲 im2col + `T.gemm` 优化的基线。

## 7. 下一步学习建议

下一讲 **u5-l2「Puzzle 09 Conv im2col：卷积转 GEMM」** 会把本讲的朴素 Conv1D 升级：

1. 先扩展到**多输出通道**（`tl_conv1d_multi_outchannel`，引入 `F` 维），把朴素写法搬到三维累加器。
2. 再用 **im2col** 变换把卷积重写成矩阵乘：用 `T.if_then_else` 做带边界的 im2col 填充、`T.reshape` 重排布局，最后调用 `T.gemm(clear_accum=True)` 走 Tensor Core。

建议你在进入下一讲前：

- 确保本讲的 `tl_conv1d_naive` 已通过 `test_puzzle`。
- 回顾 u4-l3 / u4-l4 的 `T.gemm`、`T.alloc_shared`、`T.Pipelined`，因为下一讲会重用 `T.gemm` 并对比朴素版与 im2col 版的生成代码与耗时。
- 读一遍项目文档 [docs/zh/9.conv/1.conv.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/9.conv/1.conv.md) 里的 im2col 流程图，建立「卷积 → 矩阵乘」的直觉。
