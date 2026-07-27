# Puzzle 08 GEMV：从归约到矩阵-向量乘

## 1. 本讲目标

本讲是矩阵乘法家族的起点。我们将实现 **GEMV**（GEneral Matrix–Vector multiply，矩阵乘向量），即对矩阵 \(A\in\mathbb{R}^{M\times K}\) 和向量 \(B\in\mathbb{R}^{K}\) 计算 \(C=A\,B\)，得到向量 \(C\in\mathbb{R}^{M}\)。

学完后你应当掌握：

- 把「点积」看作「逐元素乘 + reduce_sum」，从而把 GEMV 理解为 Puzzle 05 Reduce Sum 的直接扩展；
- 理解**累加器（accumulator）**的角色，掌握为什么累加器要用高精度 `accum_dtype = float32` 而非输入的 `float16`；
- 学会用 `.astype(accum_dtype)` 在相乘**之前**把操作数升精度，以及 `T.Serial` 沿 \(K\) 维分块、`reduce_sum(dim=1, clear=False)` 跨块累加的写法。

本讲**不**引入新原语，重点是把上一讲的 `T.reduce_sum` 接到第一个真实线性代数算子上，并为下一讲 Puzzle 08 GEMM（用 `T.gemm` / Tensor Core）做铺垫。

## 2. 前置知识

本讲建立在前几讲已确立的认知上，下面只做要点回顾，不再展开：

- **归约 TileOp `T.reduce_sum`**（[u3-l2]）：沿 `dim` 把 fragment 归约成更小维度，**必须在 fragment（寄存器）上执行**；`clear` 控制每次调用是否清零，跨块累加时必须用 `clear=False`。
- **`T.Serial` 串行累加**（[u3-l2]）：当归约维度过大时，逐块加载、归约、累加进同一累加器；必须串行（各块有写依赖）。
- **`T.alloc_fragment`**（[u2-l2]）：把「一个 block 内所有线程的寄存器」抽象成一块可按下标操作的 buffer，框架自动完成线程映射。
- **`T.Parallel` 元素级运算**（[u2-l1]）：`for i in T.Parallel(N)` 声明一个可并行的元素级迭代空间，循环体里做加减乘除。
- **kernel 骨架与 `T.Kernel` 块索引**（[u1-l2]、[u1-l3]）：`with T.Kernel(block 数..., threads=N) as (块索引...)`，块索引（如 `pid_m`）即 CUDA 的 `blockIdx`；分块大小是编译期超参，`compile` 时绑定。
- **float16 累加会丢精度**：[u3-l2] 末尾埋下的伏笔——本讲正面解决它。

一句话：本讲 = 「`T.Parallel` 做逐元素乘」 + 「`reduce_sum` 做归约」 + 「float32 累加器保精度」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`puzzles/08-matrix.py`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py) | 题目：`tl_gemv` 是带 `# TODO` 的空壳，需要你补全；`ref_gemv` 是 PyTorch 参考实现；`run_gemv` 是运行入口（设 M=K=4096、BLOCK_M=128、BLOCK_K=32，跑测试与基准）。 |
| [`ans/08-matrix.py`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py) | 参考答案：`tl_gemv` 完整实现，本讲「源码精读」主要引用它。 |
| [`common/utils.py`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle`（正确性比对，`atol=rtol=1e-2`）与 `bench_puzzle`（CUDA Event 计时）。 |

> 说明：本讲只覆盖 `08-matrix.py` 中 **GEMV**（08-1）部分。同文件的 GEMM（`tl_matmul_naive` / `tl_matmul_opt`）属于后续讲义 u4-l3 / u4-l4，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 GEMV 作为分块归约

#### 4.1.1 概念说明

GEMV 的数学定义是对每一行做一个**点积（dot product）**：

\[
C[i] = \sum_{k=0}^{K-1} A[i,k]\cdot B[k], \quad i=0,\dots,M-1
\]

一个点积天然拆成两步：先把 \(A[i,k]\) 与 \(B[k]\) **逐元素相乘**，再把 \(K\) 个乘积**求和**。而「求和」正是 Puzzle 05 学过的 `reduce_sum`。所以：

> **GEMV = 逐元素乘（带广播）+ reduce_sum**

这跟 Puzzle 05 的唯一区别，是 reduce 之前多了一步「乘上向量 \(B\)」。Puzzle 05 是 \(\sum_k A[i,k]\)，本讲是 \(\sum_k A[i,k]\cdot B[k]\)。

注意 \(B[k]\) 只依赖 \(k\)，与行号 \(i\) 无关——这意味着同一个 \(B[k]\) 会被所有 \(M\) 行复用。在 kernel 里这表现为：加载一段 \(B\) 的 tile，在二维循环中跨所有行重复使用，相当于「向量 \(B\) 广播到矩阵的每一行」。

#### 4.1.2 核心流程

问题规模 \(M=K=4096\) 远超单个 block / fragment 能一次装下的量，需要两级切分：

- **\(M\) 维 → 并行**：不同行互相独立，用多 block 并行，每 block 负责 `BLOCK_M` 行。
- **\(K\) 维 → 串行归约**：同一行的 \(K\) 个乘积要累加，用 `T.Serial` 分块串行累加，每块 `BLOCK_K`。

单个 block 的伪代码：

```text
每个 block 用 pid_m 定位自己负责的 BLOCK_M 行：
    C_local = 0                          # 累加器清零（float32）
    对 K 维按 BLOCK_K 串行分块（T.Serial）：
        加载 A_local = A 的 [BLOCK_M, BLOCK_K] 子块
        加载 B_local = B 的 [BLOCK_K] 段（跨所有行复用）
        AB_temp[i,j] = A_local[i,j] * B_local[j]     # 逐元素乘
        C_local += reduce_sum(AB_temp, dim=1)        # 沿 K 归约成 BLOCK_M，累加
    写回 C[pid_m 对应行] = C_local
```

把 \(K\) 分成 \(K/\text{BLOCK\_K}\) 块后，行 \(i\) 的点积被重组成「先分块求和、再跨块累加」：

\[
C[i] = \sum_{c=0}^{K/\text{BLOCK\_K}-1} \underbrace{\sum_{j=0}^{\text{BLOCK\_K}-1} A[i,\,cB{+}j]\cdot B[cB{+}j]}_{\text{第 }c\text{ 块的部分和}}, \quad B\triangleq\text{BLOCK\_K}
\]

内层求和是每块的 `reduce_sum`，外层求和是跨块的「累加进 `C_local`」。这与 Puzzle 05 的分块归约结构**完全一致**，只多了乘 \(B\) 这一步。

#### 4.1.3 源码精读

先看题目里的空壳与参考实现。题目声明了输入 `A: [M,K]`、`B: [K]`、输出 `C: [M]`，并约定了一个 `accum_dtype = float32`（本讲 4.2 详解）：

[puzzles/08-matrix.py:56-67](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L56-L67) — `tl_gemv` 的声明骨架与 `# TODO`，`return C` 表明 `C` 是输出（沿用「输出放最后」约定，详见 [u1-l2]）。

[puzzles/08-matrix.py:48-53](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L48-L53) — PyTorch 参考 `ref_gemv`，直接调 `torch.matmul(A, B)`，它是正确性金标准。

参考答案把本节 4.1.2 的伪代码几乎逐行落地：

[ans/08-matrix.py:66-83](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L66-L83) — GEMV 的完整 device 计算。逐段说明：

- `T.Kernel(T.ceildiv(M, BLOCK_M), threads=128) as pid_m`：**一维 grid**，只解包一个块索引 `pid_m`，每个 block 负责 `BLOCK_M` 行；`M=4096, BLOCK_M=128` 时共 32 个 block。
- `for k in T.Serial(K // BLOCK_K)`：沿 \(K\) 串行分块，每块 `BLOCK_K`（这里 32），`K/BLOCK_K = 128` 次迭代。
- `T.copy(A[pid_m*BLOCK_M, k*BLOCK_K], A_local)` / `T.copy(B[k*BLOCK_K,], B_local)`：加载本块的 A 子块与 B 段。
- `for i, j in T.Parallel(BLOCK_M, BLOCK_K)`：二维并行做逐元素乘（4.3 详解 `astype`）；注意 `B_local[j]` 只用 `j` 索引，跨所有 `i` 行复用——这就是向量 \(B\) 的广播。
- `T.reduce_sum(AB_temp, C_local, dim=1, clear=False)`：沿 `dim=1`（\(K\) 维）归约，`clear=False` 表示**累加**进 `C_local` 而非覆盖——这正是 [u3-l2] 强调的跨块累加语义。
- `T.copy(C_local, C[pid_m*BLOCK_M,])`：把累加结果写回 global（写回时自动降回 `float16`）。

#### 4.1.4 代码实践

**目标**：动手补全 GEMV，并验证「逐元素乘 + reduce_sum」与 `torch.matmul` 完全一致。

**步骤**：

1. 打开 `puzzles/08-matrix.py`，在 `tl_gemv` 的 `# TODO` 处，按 4.1.2 的流程补全 `with T.Kernel(...)` 内的代码（先不纠结 `astype`，可写 `A_local[i,j] * B_local[j]`，4.3 再优化）。
2. 运行 GEMV：`python3 puzzles/08-matrix.py`（或在 `__main__` 里只保留 `run_gemv()`）。
3. 观察终端：`test_puzzle` 打印 `✅ Results match: True`；`bench_puzzle` 打印 TileLang 与 Torch 两次耗时。

**需要观察的现象**：

- `✅` 表示与 `torch.matmul` 在 `atol=rtol=1e-2` 下一致；若用 4.3 之前的「朴素相乘」版本，**可能**仍能通过（取决于机器），但 4.3 的 `astype` 版本误差更小、更稳健。
- `bench_puzzle` 输出形如 `Tilelang time: x.xxx ms` 与 `Torch time: x.xxx ms`。

**预期结果**：`Results match: True`。**待本地验证**你的 GPU 上 TileLang 与 Torch 的具体耗时数值。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `T.reduce_sum(..., clear=False)` 改成 `clear=True`，结果会怎样？为什么？

> **答案**：每次 `reduce_sum` 都会先把 `C_local` 清零再写入本块的部分和，于是 `C_local` 只保留**最后一块**的部分和，前面所有块的累加全部丢失，结果严重错误。这正是 [u3-l2] 的「`clear` 坑」。

**练习 2**：为什么 \(K\) 维用 `T.Serial` 而不是 `T.Parallel`？

> **答案**：各 \(K\) 块的乘积要累加进**同一个** `C_local`，存在写依赖与竞态；`T.Parallel` 假设迭代间无依赖可乱序并行，会引入数据竞争。`T.Serial` 保证按块顺序累加，归约内部（`reduce_sum`、`T.Parallel(BLOCK_M,BLOCK_K)`）的并行由框架安全地处理。

---

### 4.2 累加器与 accum_dtype

#### 4.2.1 概念说明

GEMV 要把 \(K=4096\) 个乘积加起来。题目注释里专门写明：

> Modern AI workloads usually use float16 as the default data type … with a separate high-precision accumulator dtype like float32.

这里有两个不同角色的数据类型：

- **`dtype`（float16）**：输入 \(A\)、\(B\) 与输出 \(C\) 的存储类型。float16 省显存、算得快，是现代 GPU 上 ML 的默认类型。
- **`accum_dtype`（float32）**：**累加器** `C_local`（以及中间乘积 `AB_temp`）的类型，专门用来做求和。

为什么要分开？因为 **float16 不适合做大长度求和**。float16 的尾数只有 10 个存储位（+1 隐含位 = 11 位有效），约 3.3 位十进制精度；float32 有 23+1 = 24 位，约 7.2 位。求和时，随着部分和不断变大、而每个加项相对变小，小的加项会被「吸收（absorption）」而丢失。

粗略估计：累加 \(K\) 项，要让相对误差可控，累加器需要比加项多大约 \(\log_2 K\) 位裕量。\(K=4096=2^{12}\) 需要约 12 位裕量：

- float16 仅 11 位有效位，**连所需的裕量都不够**，长求和误差很大；
- float32 有 24 位，扣除 12 位裕量后仍剩余约 12 位表示有效精度，**绰绰有余**。

因此让累加器独立用 float32，是数值正确性的关键。

#### 4.2.2 核心流程

在 TileLang 里，累加器的精度由**分配 fragment 时传入的 dtype 参数**决定：

```text
C_local = T.alloc_fragment((BLOCK_M,), accum_dtype)   # 注意是 accum_dtype=float32，不是 dtype=float16
T.clear(C_local)                                       # 累加前清零
for k in T.Serial(K // BLOCK_K):
    ...
    T.reduce_sum(AB_temp, C_local, dim=1, clear=False) # 在 float32 上累加
T.copy(C_local, C[...])                                # 写回时自动降回 float16
```

关键点：

1. `C_local` 用 `accum_dtype` 分配 → reduce_sum 在 float32 上进行。
2. `T.clear(C_local)` 把累加器清零（reduce_sum 的初值）。注意这里能用 `T.clear`，因为求和的初值就是 0；求 max 时才需要 `T.fill(-inf)`（见 [u3-l3] softmax）。
3. 写回 `C` 时，`C` 是 `float16` 张量，`T.copy` 会把 float32 的 `C_local` 自动降回 float16——精度损失只发生在这最后一步，累加过程全程在 float32。

#### 4.2.3 源码精读

[ans/08-matrix.py:59-60](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L59-L60) — 题目里就声明了两个类型常量：`dtype = T.float16`（输入输出）、`accum_dtype = T.float32`（累加器）。

[ans/08-matrix.py:69](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L69) — 累加器用 `accum_dtype` 分配：`C_local = T.alloc_fragment((BLOCK_M,), accum_dtype)`。注意它是一维 `(BLOCK_M,)`，每行一个 float32 累加值，而非二维。

[ans/08-matrix.py:71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L71) — 中间乘积 `AB_temp` 也用 `accum_dtype` 分配（`(BLOCK_M, BLOCK_K)`，float32），让逐元素乘积先落进高精度 buffer，再被 reduce_sum 累加（4.3 详解为何连乘积都要升精度）。

[ans/08-matrix.py:73](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L73) — `T.clear(C_local)`：累加前清零，给 reduce_sum 一个干净的 0 初值。

#### 4.2.4 代码实践

**目标**：直观感受「float16 累加会丢精度」。

**步骤**：

1. 复制一份答案版 `tl_gemv`，把 `C_local` 的分配改成 `T.alloc_fragment((BLOCK_M,), dtype)`（即 float16），同时把 `AB_temp` 也改成 `dtype`，其余不变。
2. 运行 `test_puzzle(tl_gemv, ref_gemv, {"M":4096,"K":4096,"BLOCK_M":128,"BLOCK_K":32})`。
3. 失败时，`test_puzzle` 会自动打印 `Max diff` 与 `Mean diff`（见 [common/utils.py:91-106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L91-L106)）。

**需要观察的现象**：float16 累加版本大概率打印 `❌`，且 `Max diff` 明著大于 float32 版本（float32 版本通常 `Max diff` 在 1e-3 量级或更小）。

**预期结果**：float16 累加误差显著放大，验证「累加器必须高精度」。若你的机器上 float16 版本恰好仍通过 1e-2 容差，可把 `K` 调大（如 8192）或把 `test_puzzle` 的 `atol/rtol` 临时改小观察 `Max diff`。**待本地验证**具体数值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `C_local` 的形状是 `(BLOCK_M,)` 而不是 `(BLOCK_M, BLOCK_K)` 或 `(BLOCK_M, K)`？

> **答案**：`C_local` 存的是**每行的累加结果**，每个 block 只产出 `BLOCK_M` 个输出（对应 `BLOCK_M` 行），所以只需一维 `BLOCK_M`。归约把 `(BLOCK_M, BLOCK_K)` 的 `AB_temp` 沿 `dim=1` 压成 `(BLOCK_M,)`，正好放进 `C_local`。

**练习 2**：求和用 `T.clear(C_local)` 清零，而 softmax 求 max 要用 `T.fill(C_local, -inf)`（[u3-l3]）。GEMV 这里能用 `T.fill(..., 0)` 代替 `T.clear` 吗？

> **答案**：可以等价。求和的单位元是 0，`T.clear` 就是置 0，与 `T.fill(..., 0)` 效果相同；项目用 `T.clear` 更简洁、更明确表达「清零累加器」的意图。求 max 时单位元是 \(-\infty\)，置 0 会把全负行的基准钉死在 0，所以那里**必须** `T.fill(-inf)`。

---

### 4.3 astype 与高精度累加

#### 4.3.1 概念说明

4.2 解决了「累加器精度」，但还有一个更隐蔽的精度陷阱：**乘法本身的精度**。

考虑两种写法（`A_local`、`B_local` 都是 float16，`AB_temp` 是 float32）：

```python
# 写法 X：先在 float16 里相乘，再赋给 float32
AB_temp[i, j] = A_local[i, j] * B_local[j]

# 写法 Y：先把操作数升到 float32，再相乘
AB_temp[i, j] = A_local[i, j].astype(accum_dtype) * B_local[j].astype(accum_dtype)
```

差别在「乘法发生在哪种精度」：

- 写法 X：两个 float16 相乘，**乘法在 float16 里完成**，乘积已被舍入到 float16（约 3.3 位精度），之后才转 float32 存进 `AB_temp`——精度已经在乘法那一刻损失了。
- 写法 Y：`.astype(accum_dtype)` 把每个操作数**先升到 float32**，乘法在 float32 里完成（约 7.2 位精度），存进 `AB_temp` 时无额外损失。

`.astype(target_dtype)` 就是显式类型转换（ cast），它返回一个指定类型的新值。把 `.astype(accum_dtype)` 放在相乘**之前**，确保「升精度 → 高精度相乘 → 高精度累加」全链路都在 float32。

直觉上：两个 float16 数相乘，结果的有效信息其实比 float16 能表示的更细（乘积的相对误差可达 \(2^{-11}\)），若在 float16 里舍入一次再累加 \(K\) 次，误差会累积；在 float32 里相乘则保留了这份信息。

> 对 \(K=4096\)、输入 \(\sim\mathcal N(0,1)\) 的情况，写法 X 常常仍能通过 1e-2 容差，但 `Max diff` 会比写法 Y 大；写法 Y 是「教科书正确」的高精度写法，也是项目参考答案的选择。

#### 4.3.2 核心流程

高精度逐元素乘 + 归约的最小骨架：

```text
# AB_temp 已用 accum_dtype=float32 分配（见 4.2）
for i, j in T.Parallel(BLOCK_M, BLOCK_K):
    AB_temp[i, j] = A_local[i, j].astype(accum_dtype) * B_local[j].astype(accum_dtype)
T.reduce_sum(AB_temp, C_local, dim=1, clear=False)   # 在 float32 上归约累加
```

要点：

- `.astype(accum_dtype)` 在**乘法之前**对每个操作数生效，决定乘法精度。
- `B_local[j]` 只用 `j` 索引，跨 `BLOCK_M` 行复用——向量 \(B\) 的广播在这里实现，且因为 `B_local` 是 fragment，重复读取走寄存器，开销很低。
- `T.Parallel(BLOCK_M, BLOCK_K)` 把 `BLOCK_M*BLOCK_K`（如 \(128\times32=4096\)）次逐元素乘分布到 block 内线程上并行。

#### 4.3.3 源码精读

[ans/08-matrix.py:78-81](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L78-L81) — 逐元素乘与归约。两行注释：

- L79：`AB_temp[i, j] = A_local[i, j].astype(accum_dtype) * B_local[j].astype(accum_dtype)`——两个操作数都先 `.astype(float32)` 再相乘，乘积直接进 float32 的 `AB_temp`；`B_local[j]` 跨 `i` 复用即广播。
- L81：`T.reduce_sum(AB_temp, C_local, dim=1, clear=False)`——在 float32 上沿 `dim=1` 归约并累加进 `C_local`。

整条精度链路：`A_local/B_local`(float16) → `.astype` 升精度 → float32 相乘 → `AB_temp`(float32) → `reduce_sum` 在 float32 累加 → `C_local`(float32) → 写回 `C` 时降回 float16。唯一主动降精度发生在写回输出那一步。

#### 4.3.4 代码实践

**目标**：对比「写法 X（朴素相乘）」与「写法 Y（astype 高精度）」的数值误差。

**步骤**：

1. 写两个版本的 `tl_gemv`：版本 A 在 L79 用 `A_local[i,j] * B_local[j]`；版本 B 用答案的 `.astype(accum_dtype)` 写法。两者都保持 `C_local`/`AB_temp` 为 float32。
2. 分别用 `test_puzzle(...)` 跑，并在调用时传 `print_log=True`，让 `test_puzzle` 打印 `Max diff` / `Mean diff`（见 [common/utils.py:66-106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L66-L106)）。

**需要观察的现象**：版本 B 的 `Max diff` 应不大于版本 A（通常更小）。两者都应通过 `atol=rtol=1e-2`。

**预期结果**：`✅ Results match: True`，且版本 B 误差更小。**待本地验证**两者 `Max diff` 的具体差值（取决于输入随机种子与硬件）。

> 小贴士：`test_puzzle` 默认 `print_log=False`，只有不匹配或显式传 `print_log=True` 时才打印 `Max diff` 等详情（[common/utils.py:91](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L91)）。本实践需要主动打开它。

#### 4.3.5 小练习与答案

**练习 1**：把 `.astype(accum_dtype)` 放在相乘**之后**，即 `AB_temp[i,j] = (A_local[i,j] * B_local[j]).astype(accum_dtype)`，与写法 X 有何区别？能提升精度吗？

> **答案**：没有区别，不能提升精度。括号内的乘法仍发生在 float16（两个 float16 操作数），乘积已被舍入；`.astype` 只是把已经损失精度的 float16 结果转成 float32，无法找回丢失的信息。`.astype` 必须在**乘法之前**作用于操作数才有意义。

**练习 2**：既然 `AB_temp` 已经是 float32，为什么 `reduce_sum` 还要强调「在 float32 上累加」？会不会出现 reduce 内部又降回 float16？

> **答案**：`reduce_sum` 的累加精度由**输入 fragment 的 dtype** 决定。这里输入 `AB_temp` 是 float32，所以归约全程在 float32；输出写进 float32 的 `C_local`，链路一致。若误把 `AB_temp` 分配成 float16，即使 `C_local` 是 float32，乘积与初次汇总仍发生在 float16，精度受损——这正是 4.2 同时要求 `AB_temp` 与 `C_local` 都用 `accum_dtype` 的原因。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「调参 + 精度观察」的小任务：

1. **补全并跑通**：在 `puzzles/08-matrix.py` 的 `tl_gemv` 中实现 GEMV（逐元素乘用 `.astype(float32)`、`reduce_sum(dim=1, clear=False)`、float32 累加器），运行 `run_gemv()` 看到 `✅`。
2. **调 BLOCK_K**：固定 `M=K=4096`、`BLOCK_M=128`，把 `BLOCK_K` 在 `{16, 32, 64, 128}` 间切换，分别记录 `bench_puzzle` 的 TileLang 耗时。
   - 观察：`BLOCK_K` 增大时，单块乘加变多、`T.Serial` 迭代次数（`K//BLOCK_K`）变少，但 fragment 占用（`AB_temp` 为 `BLOCK_M*BLOCK_K` 个 float32）增大。思考二者对性能的拉扯。
3. **精度核验**：选一个 `BLOCK_K`，用 `print_log=True` 记录 `Max diff`；再把 `C_local` 改为 float16（4.2 实践），记录 `Max diff`，对比验证累加器精度的影响。
4. **生成代码自检（可选）**：仿照 `run_matmul_opt` 里的用法，用 `tl_gemv.compile(M=4096,K=4096,BLOCK_M=128,BLOCK_K=32).print_source_code()` 查看生成的 CUDA，确认 `.astype(float32)` 是否对应一条 `__half2float` 之类的升精度指令。**待本地验证**生成代码细节。

交付物：一份简短表格，列出不同 `BLOCK_K` 下的耗时与 `Max diff`，并写一句话结论（如「在本机上 BLOCK_K=32 性能最优」「float16 累加器使 Max diff 增大约 N 倍」）。

## 6. 本讲小结

- **GEMV = 逐元素乘 + reduce_sum**：点积就是「乘完再求和」，所以 GEMV 是 Puzzle 05 Reduce Sum 加一步「乘 \(B\)」的直接扩展；\(M\) 维并行（多 block、`pid_m`），\(K\) 维串行分块（`T.Serial`）。
- **累加器必须高精度**：输入输出用 float16 省显存，但累加器 `C_local`（及中间 `AB_temp`）必须用 `accum_dtype=float32`，否则长求和（\(K=4096\)）误差显著放大。
- **`T.clear` 给初值、`clear=False` 跨块累加**：循环前 `T.clear(C_local)` 清零，循环内 `reduce_sum(..., clear=False)` 把每块部分和累加进 `C_local`（沿用 [u3-l2] 的语义）。
- **`.astype` 要放在相乘之前**：`A.astype(float32) * B.astype(float32)` 让乘法本身在 float32 完成；放在乘法之后则无效，精度已在 float16 乘法中丢失。
- **向量 \(B\) 的广播**：`B_local[j]` 在 `T.Parallel(BLOCK_M, BLOCK_K)` 中只用 `j` 索引、跨所有行复用，即向量广播到矩阵每一行；fragment 内重复读取走寄存器。
- **写回时自动降精度**：`T.copy(C_local, C[...])` 把 float32 结果写回 float16 张量，唯一主动降精度只发生在输出这一步。

## 7. 下一步学习建议

本讲用「CUDA Core 上的 reduce_sum」实现了矩阵乘。下一步有两个方向：

- **u4-l3 Puzzle 08 GEMM Naive：`T.gemm` 与 Tensor Core**：当右侧从「向量 \(B\)」升级为「矩阵 \(B\)」，逐元素乘 + reduce_sum 的写法不再适用；本讲的 `T.Parallel` + `reduce_sum` 将被封装矩阵乘的 **`T.gemm` TileOp（Tensor Core / MMA 指令）** 取代，进入二维分块（`BLOCK_M×BLOCK_N×BLOCK_K`）。本讲的 float32 累加器、`T.Serial` 沿 \(K\) 累加、`clear` 语义会**原样复用**，只是把「手写乘加」换成一行 `T.gemm`。
- **u4-l4 Puzzle 08 GEMM 优化**：在 GEMM 基础上引入 `T.alloc_shared`（共享内存）与 `T.Pipelined`（软件流水线），理解「朴素 vs 优化」的性能差距。

建议继续阅读：[`ans/08-matrix.py`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py) 中 `tl_matmul_naive`（下一个要补全的 TODO），与本讲的 `tl_gemv` 对比，体会「reduce_sum 路线 → T.gemm 路线」的演进。
