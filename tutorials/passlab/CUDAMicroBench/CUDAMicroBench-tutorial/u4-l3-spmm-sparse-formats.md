# CoMem（SpMM 篇）：稀疏矩阵乘中的 CSR/CSC 存储与访问模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 CSR（Compressed Sparse Row，行压缩）与 CSC（Compressed Sparse Column，列压缩）两种稀疏矩阵编码的 `ptr / indices / data` 三数组结构，并能手工推导它们的内容。
2. 精读 `spmm_csr_csr_kernel` 与 `spmm_csc_csr_kernel` 两个 kernel，说清楚它们各自"如何找到一对需要相乘的非零元素"，以及由此产生的工作量差异与访存模式差异。
3. 用一个闭式公式估算两种格式的 kernel 时间比，并与仓库自带的 Carina / Fornax 两台机器的 nvprof 实测结果对照。
4. 理解"换一种数据布局"为什么常常比"优化同一种布局"收益更大：布局决定了哪些工作根本不必做。

本讲承接 u4-l2 的合并访问（coalescing）概念，但会诚实地告诉你：本例的性能差距**不只**来自访存模式，更大一部分来自存储格式决定的**算法工作量**。区分这两者，正是读微基准代码最重要的功力。

## 2. 前置知识

### 2.1 什么是稀疏矩阵、为什么要压缩存储

一个 100×100 的矩阵有 10000 个元素。如果其中只有 1024 个非零（稀疏度约 10%），那么用二维稠密数组存它，就要为 8976 个零白白付出内存、带宽和计算。稀疏格式的思路是：**只存非零元素，外加一点"它在哪里"的索引信息**。

- **CSR（行压缩）**：按"一行接一行"的顺序把非零值摊平存进 `data`，`indices` 同步记录每个值的列号，`ptr` 记录每一行的非零值在摊平数组中的起始偏移。
- **CSC（列压缩）**：完全同构，只是把"行"换成"列"：按列摊平，`indices` 记录行号，`ptr` 记录每列的起始偏移。

两者是对偶的。同一个矩阵，CSR 回答"第 r 行有哪些非零"很便宜（直接查 `ptr[r]`、`ptr[r+1]`），回答"第 c 列有哪些非零"则要扫全表；CSC 正好反过来。

### 2.2 SpMM：稀疏矩阵乘稀疏矩阵

\( C = A \times B \)，其中 \( A \) 是 \( n \times n \) 稀疏矩阵，\( B \) 也是 \( n \times n \) 稀疏矩阵。与 u4-l1 的稠密矩阵乘不同，SpMM 的核心难题变成了：

> 对 \( C \) 的第 \( (r, c) \) 个元素，\( dot = \sum_k A[r][k] \cdot B[k][c] \)。只有当 \( A[r][k] \) 和 \( B[k][c] \)**同时非零**时才贡献乘积。怎么高效地"配对"这些同时非零的元素？

**存储格式就是配对算法**。本讲的两个 kernel 用不同格式给出了两种答案，性能差约 50~70 倍。

### 2.3 你需要带进来的旧知识

来自 u4-l2 的关键结论：合并的判断单位是 **warp 同一时刻发出的 32 个地址**，而不是单个线程的访问轨迹。来自 u2-l1：`i = blockIdx.x * blockDim.x + threadIdx.x` 给出全局线程编号。来自 u1-l4：nvprof 概览表的 `Calls` 列可以反向核对程序结构。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [CoMem_SpMM/SpMM.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM.h) | 接口契约 | `REAL` 宏、两个入口函数的 `extern "C"` 声明 |
| [CoMem_SpMM/SpMM_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c) | host 主程序（实验控制器） | 测试矩阵构造、CSR/CSC 三数组初始化、串行基线、入口调用 |
| [CoMem_SpMM/SpMM_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu) | GPU 实现 | 本讲两个主角 kernel 与两个 host 包装函数 |
| [CoMem_SpMM/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/Makefile) | 单行式构建 | `nvcc -o SpMM_cuda SpMM_cuda.c SpMM_cudakernel.cu` |
| [CoMem_SpMM/SpMM_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt) | 归档实测 | Carina 集群上的 nvprof 转录 |
| [CoMem_SpMM/SpMM_cuda.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt) | 归档实测 | Fornax 集群上的 nvprof 转录 |

注意：**本目录没有 `test.sh`**（对照 u1-l1 提到的"6 个目录缺 test.sh"），规模不是命令行参数，而是写死在源码全局变量里的。归档输出里作者手工执行的命令也是 `nvprof ./SpMM_cuda`，不带参数。

## 4. 核心概念与源码讲解

### 4.1 稀疏格式的构造：从稠密矩阵到 ptr/indices/data 三数组

#### 4.1.1 概念说明

这个模块解决的问题是：**给定一个稠密矩阵，如何产出 CSR 和 CSC 两组三数组**。它是理解后面两个 kernel 的地基——kernel 里的每一次数组下标计算，都在消费这里产出的结构。

#### 4.1.2 核心流程

```
init_matrix(matrix, nnz)        # 造一个恰好有 nnz 个非零的随机矩阵
        │
        ├── init_data_csr(data, indices, matrix)   # 按行扫描 → data 值, indices 列号
        ├── init_data_csc(data, indices, matrix)   # 按列扫描 → data 值, indices 行号
        ├── init_ptr_csr(ptr, matrix, nnz)         # 每行非零计数的递推前缀和
        └── init_ptr_csc(ptr, matrix, nnz)         # 转置后同样递推（即按列计数）
```

一个 \( n \times n \)、非零数 \( z \) 的矩阵：

- `data`、`indices` 长度为 \( z \)；
- `ptr` 长度为 \( n+1 \)，且 `ptr[0] = 0`、`ptr[n] = z`；
- 第 \( r \) 行（CSR）的非零落在摊平数组的 `[ptr[r], ptr[r+1])` 区间，长度为 `ptr[r+1] - ptr[r]`。

#### 4.1.3 源码精读

**矩阵规模写死在全局变量里**，这是本目录与 CoMem_AXPY（用 `argv` 传规模）最显著的差异：

[SpMM_cuda.c:L21-L24](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L21-L24) —— 定义 \( n = 100 \)，\( z_A = z_B = 1024 \)。矩阵 100×100 共 10000 元素，**稀疏度 = 1024 / 10000 = 10.24%**，平均每行（每列）约 10.24 个非零。这两个数字是后面所有估算的输入。

[SpMM_cuda.c:L55-L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L55-L73) —— `init_matrix` 用 Fisher–Yates 洗牌造一个 \( 0 \ldots n^2-1 \) 的排列放进 `d`，然后 `d[i*n+j] >= nnz` 的位置填 0、否则填 `drand48()+1`。因为 `d` 是排列，恰好有 `nnz` 个位置的值小于 `nnz`，**非零个数精确等于 1024**，且非零值落在 \( (1, 2) \) 区间。注意两个种子：`srand48(1<<12)` 在 `main` 里（值序列确定），而 `srand(time(NULL))` 在这里（**非零位置每次运行都不同**）——这对可复现性的影响见 4.4.4。

[SpMM_cuda.c:L27-L38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L27-L38) 与 [SpMM_cuda.c:L41-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L41-L52) —— `init_data_csr` 外层循环是 `i`（行）、内层是 `j`（列），所以 `indices[tmp] = j` 存的是**列号**；`init_data_csc` 把两层循环对调，`indices[tmp] = i` 存的是**行号**。两函数除循环顺序与这一行外逐字相同。

[SpMM_cuda.c:L76-L95](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L76-L95) —— `init_ptr_csr` 先数出每行非零个数（内层 `tmp` 遮蔽了外层未用的 `tmp`，一个小的代码噪音），再做 `ptr[i] = ptr[i-1] + non_zero_elements[i-1]` 的递推。[SpMM_cuda.c:L98-L125](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L98-L125) 的 `init_ptr_csc` 先花 \( n^2 \) 代价把矩阵转置进 `matrixT`，然后套用完全相同的计数-递推逻辑——**CSC 就是转置矩阵的 CSR**，这句话记住，两个 kernel 的对称性就自明了。

[SpMM_cuda.c:L260-L269](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L260-L269) —— `main` 里的调用序列值得逐行看：矩阵 \( A \) 只构造了 CSR 一套；矩阵 \( B \) **同时构造了 CSR 和 CSC 两套**。这就是两个被测 kernel 的唯一实验变量：`spmm_csr_cuda` 拿 `ptrB_csr/indicesB_csr/dataB_csr`，`spmm_csc_cuda` 拿 `ptrB_csc/indicesB_csc/dataB_csc`，其余输入（包括 \( A \) 的 CSR）完全一致。教科书级的控制变量。

#### 4.1.4 代码实践

1. **实践目标**：不运行程序，手工推出一个小矩阵的两组三数组。
2. **操作步骤**：
   - 取 \( n=3 \)，\( matrix = \begin{pmatrix} 0 & 5 & 0 \\ 0 & 0 & 7 \\ 0 & 0 & 9 \end{pmatrix} \)（\( z = 3 \)）。
   - 纸上分别模拟 `init_data_csr` / `init_ptr_csr` 与 `init_data_csc` / `init_ptr_csc` 的双层循环。
3. **需要观察的现象**：CSR 应得 `data = [5,7,9]`、`indices = [1,2,2]`、`ptr = [0,1,2,3]`；CSC 应得 `data = [5,7,9]`、`indices = [0,1,2]`、`ptr = [0,0,1,3]`。
4. **预期结果**：CSC 的 `ptr[0] == ptr[1] == 0`（原矩阵第 0 列没有非零，所以这个区间是空的）。如果推出来一致，说明你已经能把三数组在脑内"展开"成矩阵。
5. 本实践无需 GPU，纸笔即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ptr` 必须是 `n+1` 个元素而不是 `n` 个？
**答案**：因为第 \( r \) 行的区间是 `[ptr[r], ptr[r+1])`，需要用"下一行的起点"表示"本行的终点"。最后一行没有下一行，所以额外补一个 `ptr[n] = nnz` 作为哨兵（见 `init_ptr_csr` 里 `ptr[num_rows] = nnz` 那一行）。这样任何一行的长度都能统一写成 `ptr[r+1] - ptr[r]`，不需要特判。

**练习 2**：`init_matrix` 里 `d[i*num_rows+j] >= nnz` 这个判断为什么能保证非零个数恰好是 `nnz`？
**答案**：`d` 经过 Fisher–Yates 洗牌后仍是 \( 0 \ldots n^2-1 \) 的一个排列，所以值小于 `nnz` 的格子恰好有 `nnz` 个（无重复、无遗漏）。洗牌只改变"哪些格子非零"，不改变"有几个"。

**练习 3**：如果把这个矩阵的 `data` 数组按 CSR 摊平后直接线性扫描，能不能还原出第 \( c \) 列有哪些非零？
**答案**：能，但必须扫完整个 `indices` 数组逐个判断 `indices[j] == c`，代价 \( O(z) \)。这正是 4.2 里 `spmm_csr_csr_kernel` 被迫采用的做法；而 CSC 把同样的查询变成 `O(ptr[c+1]-ptr[c])`，这正是 4.3 的优化本质。

---

### 4.2 spmm_csr_csr_kernel：把"配对"做成全数组线性扫描（反模式）

#### 4.2.1 概念说明

这个 kernel 是本讲的反模式（anti-pattern）样本。\( A \)、\( B \) 都用 CSR 存，于是求 \( C[r][c] \) 时，"第 \( c \) 列的 \( B \) 元素"没有直接索引可用。代码的应对方式是最朴素也最昂贵的：**对每一个 \( A \) 的非零，把 \( B \) 的整个 `indices` 数组从头到尾扫一遍**，靠一个复合 `if` 条件筛出需要相乘的那一对。

它演示的通用教训是：**当数据布局没有提供你要的寻址维度时，代码会用线性搜索去补，代价被放大 \( z \) 倍**。

#### 4.2.2 核心流程

线程映射沿用 u2-l1 的"每线程一行"：

```
row = threadIdx.x + blockIdx.x * blockDim.x          # 本线程负责输出矩阵的第 row 行
if row < num_rows:
    row_start, row_end = ptrA[row], ptrA[row+1]      # A 第 row 行的非零区间
    for k in 0..num_rows-1:                          # 遍历 B 的列 = 输出的列
        dot = 0
        for i in [row_start, row_end):               # A 行内的每个非零 A[row][indicesA[i]]
            for j in 0..nnzB-1:                      # ← 扫 B 的全部非零！
                if indicesB[j] == k                    #   B 的这个元素在第 k 列
                   and ptrB[indicesA[i]] <= j < ptrB[indicesA[i]+1]:   #   且在第 indicesA[i] 行
                    dot += dataA[i] * dataB[j]
        result[row*num_rows + k] = dot
```

配对条件拆开看就是矩阵乘的定义：\( A[row][\,indicesA[i]\,] \) 要乘上 \( B[indicesA[i]][k] \)。CSR 的 `indicesB[j]` 是列号，`ptrB` 按行切区间，所以"行 = `indicesA[i]` 且列 = `k`"只能靠两个条件的**与**去全表过滤。

工作量（每线程的内层迭代次数）：

\[
W_{csr} \;=\; \text{num\_rows} \times \overline{rowA} \times nnzB
\]

代入 \( \overline{rowA} = nnzA / \text{num\_rows} = 1024/100 \approx 10.24 \)：

\[
W_{csr} \approx 100 \times 10.24 \times 1024 \approx 1.05 \times 10^{6}
\]

访存模式上有三点值得单独指出（对照 u4-l2 的"warp 集体地址"视角）：

- `indicesB[j]` 与 `dataB[j]`：`j` 是循环变量，warp 内所有线程同一时刻取同一个 `j` → **同址广播**，事务便宜。
- `ptrB[indicesA[i]]`：下标是**从内存里读出来的值**（数据依赖的间接寻址，gather）。相邻线程的 `indicesA[i]` 互不相同，warp 集体地址分散 → **非合并**。且它是关于 `j` 和 `k` 的循环不变量，却被重复求值了约 100 次。
- `indicesA[i]` / `dataA[i]`：相邻线程承包相邻行，行区间在摊平数组上首尾相接，warp 集体地址大体连续 → 基本合并。

另一个结构性观察：启动配置是 `<<<256,256>>>`，共 65536 个线程，但只有 `row < 100` 的**第 0 个 block 的前 100 个线程**真正干活，线程利用率 0.15%。本基准测的是访存与搜索代价，不是并行规模，所以这个"浪费"不影响结论，但读代码时应当一眼看出来。

#### 4.2.3 源码精读

[SpMM_cudakernel.cu:L31-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L31-L52) —— `spmm_csr_csr_kernel` 全文。三层嵌套中最内层的 `for(int j = 0; j < nnzB; j++)` 与注释 `//nnz should be number of non-zero element of B` 是整个反模式的所在：`nnzB = 1024` 次迭代里，对每组 \( (k, i) \) 至多只有**一个** `j` 能同时满足 `if` 的两个条件。

[SpMM_cudakernel.cu:L44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L44) —— 配对条件本体。`indicesB[j] == k` 选列，`j >= ptrB[indicesA[i]] && j < ptrB[indicesA[i]+1]` 选行；两次 `ptrB[indicesA[i]]` 的间接寻址在同一个表达式里重复出现，且对内层 `j` 循环完全不变。

[SpMM_cudakernel.cu:L33-L37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L33-L37) —— 线程映射与 `A` 行区间的获取。`row_start/row_end` 体现了 CSR 的本职：**按行取区间是 O(1)**，这部分格式选对了。

[SpMM_cudakernel.cu:L49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L49) —— 结果写回 `result[row*num_rows+k]`。注意输出矩阵 \( C \) 是**稠密**存储的（`num_rows*num_rows` 个元素），即使 \( C \) 本身可能很稀疏——这是另一个可以讨论的布局决策，本基准未做优化。

[SpMM_cuda.c:L127-L148](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L127-L148) —— `spmm_csr_serial` 与 kernel 逐行同构，只是把 `row` 从线程编号换成了外层 `for` 循环变量。它让 GPU 结果有了同算法的 CPU 参照。

#### 4.2.4 代码实践

1. **实践目标**：不动源码，仅用纸笔和源码行号，量化这个 kernel 的"搜索浪费率"。
2. **操作步骤**：
   - 数出 [L41-L47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L41-L47) 中 `j` 循环体一次完整执行所做的工作：2 次数组读（`indicesB[j]`、可能的 `dataB[j]`）、2 次 `ptrB` 间接读、若干次比较。
   - 对一组固定的 \( (k, i) \)，问：满足条件的 `j` 有几个？（提示：`ptrB` 的区间 `[ptrB[c], ptrB[c+1])` 与 "`indicesB[j] == k`" 的交集，在本数据下通常恰好 0 或 1 个。）
   - 由此算出"有效迭代 / 总迭代"的比率。
3. **需要观察的现象**：比率约为 \( 1/1024 \) 量级——即**99.9% 的内层迭代在做无用功**。
4. **预期结果**：写出一句结论，形如"每 1024 次 `j` 迭代中平均只有约 1 次真正产生乘积"。这个比率就是 4.3 优化的全部来源。
5. 本实践无需 GPU，纯源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：把 `ptrB[indicesA[i]]` 提到 `j` 循环外面（缓存成局部变量 `lo`/`hi`），能省下什么、省不下什么？
**答案**：能省下的是 `j` 循环每迭代的 2 次间接访存（`ptrB[indicesA[i]]` 与 `ptrB[indicesA[i]+1]`），并且编译器很可能本来就会做这种循环不变量外提；省不下的是 **1024 次迭代本身**——`indicesB[j] == k` 的判断仍要逐一做。所以这种"实现级"优化只能带来常数级改善，改变不了数量级。

**练习 2**：既然 `indicesB[j] == k` 也与 `i` 无关，能否先把"第 `k` 列的 `j` 集合"提出来？
**答案**：可以，而且这正是关键。把 `j` 的候选集从"全部 `nnzB`"收缩到"第 `k` 列的那一小段"，就等价于**按列建立索引**——也就是 CSC 的 `ptr`。可见"提取公共子表达式"走到尽头，就是"换数据布局"。这是本讲最想传达的一句话。

**练习 3**：为什么说 `ptrB[indicesA[i]]` 是"非合并"访问，而 `indicesB[j]` 是"广播"？
**答案**：合并性看 warp 集体地址。`j` 是循环变量，warp 内 32 个线程在同一时刻用同一个 `j`，32 个地址重合成 1 个，硬件一次事务即可（广播）；而 `indicesA[i]` 是每个线程从自己那行数据里读出来的值，线程间互不相同，`ptrB` 的 32 个下标随之散开，落在不同的事务里（gather）。前者便宜是因为"地址相同"，不是因为"地址连续"。

---

### 4.3 spmm_csc_csr_kernel：换一种布局，让匹配区间可直接寻址（优化）

#### 4.3.1 概念说明

优化版的思路只有一步：把 \( B \) 改成 CSC 存储，让 `ptrB` 直接按**列**切区间。于是"`B` 的第 `column` 列有哪些非零"从 \( O(nnzB) \) 的全表过滤，变成读两个整数 `ptrB[column]`、`ptrB[column+1]`。配对从"搜索"退化成"两个有序小数组的归并式对撞"。

需要强调：这个 kernel 与反模式版本**算法语义完全相同**（算出同一个 \( C \)，`check` 值也相同），差异只在 \( B \) 的存储格式。这正是"布局即算法"的示范。

#### 4.3.2 核心流程

```
row = threadIdx.x + blockIdx.x * blockDim.x
if row < num_rows:
    row_start, row_end = ptrA[row], ptrA[row+1]        # A 仍是 CSR：按行取区间
    for column in 0..num_rows-1:
        column_start = ptrB[column]                    # ← B 是 CSC：按列取区间，O(1)
        column_end   = ptrB[column+1]
        dot = 0
        for i in [row_start, row_end):                 # A 行内非零，列号 = indicesA[i]
            for j in [column_start, column_end):       # ← 只扫这一列的 ~10.24 个非零
                if indicesA[i] == indicesB[j]:         #   A 的列号 == B 的行号 → 内维匹配
                    dot += dataA[i] * dataB[j]
        result[row*num_rows + column] = dot
```

配对条件只剩一个：`indicesA[i]`（\( A \) 的列号）等于 `indicesB[j]`（CSC 里存的 \( B \) 的行号）。矩阵乘的内维 \( k \) 不再显式出现——它已经被"区间选择"隐式完成了。

工作量：

\[
W_{csc} \;=\; \text{num\_rows} \times \overline{rowA} \times \overline{colB}
\;=\; \text{num\_rows} \times \frac{nnzA}{\text{num\_rows}} \times \frac{nnzB}{\text{num\_rows}}
\]

两式相除，得到一个非常干净的闭式预测（**工作量比与稀疏度无关，只等于矩阵阶数**）：

\[
\frac{W_{csr}}{W_{csc}} \;=\; \text{num\_rows} \;=\; 100
\]

访存模式的变化：`ptrB[column]` 的下标 `column` 是所有线程共享的循环变量 → warp 集体地址重合为 1 个（广播），**4.2 里那个数据依赖的 gather 彻底消失了**。`indicesB[j]`、`dataB[j]` 同样是列内顺序扫描 + 线程间同址，广播为主。也就是说：优化版既少做了约 100 倍的工作，又把最差的那类访问（随机间接寻址）换成了最好的一类（同址广播）。

#### 4.3.3 源码精读

[SpMM_cudakernel.cu:L76-L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L76-L96) —— `spmm_csc_csr_kernel` 全文。与 [L31-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L31-L52) 的 CSR 版逐行对照：函数签名完全相同（参数名都没改，`nnzA`/`nnzB` 甚至不再被使用），变的只有三层循环的最内两层。

[SpMM_cudakernel.cu:L82-L84](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L82-L84) —— 优化的核心两行：`column_start = ptrB[column]; column_end = ptrB[column+1];`。把这两行与 CSR 版的 `for(int j = 0; j < nnzB; j++)` 放在一起看，就是"O(1) 直达区间"对"O(z) 全表搜索"的替代。

[SpMM_cudakernel.cu:L86-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L86-L92) —— 内层双循环，配对条件只剩 [L88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L88) 的一个等式判断 `indicesA[i] == indicesB[j]`。注意这是两个**有序**小数组（都按内维下标升序）的朴素对撞，理论上还可以用双指针归并做到线性，这里没有做——留作练习。

[SpMM_cudakernel.cu:L139-L179](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L139-L179) —— host 包装 `spmm_csc_cuda`，与 `spmm_csr_cuda`（[L98-L137](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L98-L137)）共享 u2-l3 讲过的五段式骨架：`cudaMalloc` → H2D → warmingup + 同步 → 被测 kernel + 同步 → D2H → `cudaFree`。两份代码也逐行同构，唯一差异是 [L166](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L166) 启动的是 `spmm_csc_csr_kernel`。同样注意 `<<<256,256>>>` 的巨量空闲线程。

[SpMM_cudakernel.cu:L8-L29](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L8-L29) 与 [L54-L74](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L54-L74) —— 两个 warmingup kernel 与被测 kernel **逐字节相同**（函数名不同）。Carina 的数据佐证了 u2-l4 的结论：warmingup 与被测 kernel 时间几乎一致（48.011ms vs 48.007ms），预热的意义在于消化首次启动的一次性开销，而不是让被测那一次变快。

[SpMM.h:L11-L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM.h#L11-L13) —— 两个入口的 `extern "C"` 声明，是 u1-l3 讲过的 C/C++ 混编契约。注意 `REAL` 在 [SpMM.h:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM.h#L6) 定义为 `float`，而 kernel 内累加器写的是 `float dot`（不是 `REAL dot`）——若将来把 `REAL` 切成 `double`，这里会成为精度瓶颈。

#### 4.3.4 代码实践

1. **实践目标**：验证"工作量比 = num_rows"这一闭式预测。
2. **操作步骤**：
   - 在具备 NVIDIA GPU 的机器上：`cd CoMem_SpMM && make`，然后 `nvprof ./SpMM_cuda`。
   - 在 GPU activities 表里找到 `spmm_csr_csr_kernel` 与 `spmm_csc_csr_kernel` 两行，记录 Time。
   - 然后修改 [SpMM_cuda.c:L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L22) 的 `num_rows = 100` 为 `200`，同时把 `nnzA`/`nnzB` 改为 `4096`（保持 10.24% 稀疏度不变，让"矩阵阶数"成为唯一变量；注意 `nnz` 不能超过 \( num\_rows^2 = 40000 \)，且 `init_matrix` 里两个 \( n^2 \) 大小的栈上数组随 `num_rows` 自动扩大），重新 `make` 并 `nvprof ./SpMM_cuda`。
3. **需要观察的现象**：第一次运行中两 kernel 时间之比应在几十倍量级；改到 `num_rows = 200` 后，按 \( W_{csr} = nnzA \times nnzB \) 预计 CSR 版时间上升约 **16 倍**，按 \( W_{csc} = nnzA \times nnzB / \text{num\_rows} \) 预计 CSC 版上升约 **8 倍**，而**比值应从 ~100 附近移动到 ~200 附近**。
4. **预期结果**：比值 ≈ num_rows，与稀疏度无关。若实测比值系统性低于预测（如 Carina 的 52、Fornax 的 69 对比预测 100），请把它归因于 kernel 内与内层无关的固定开销（`k`/`column` 循环、结果写回、启动与同步）以及行/列长度的随机起伏。**待本地验证**（本讲义写作环境无 GPU）。
5. 若无 GPU，可退化为源码阅读型实践：对照 4.2.2 与 4.3.2 的两个工作量公式，自己重新推导一遍比值，并解释为什么 `nnzA`、`nnzB` 在比值中约掉了。

#### 4.3.5 小练习与答案

**练习 1**：优化版 kernel 的参数表里 `nnzA`、`nnzB` 还在，但函数体里用到了吗？
**答案**：没有。CSC 版的最内层循环上界是 `column_end`（来自 `ptrB`），不再需要 `nnzB` 作为扫描边界；`nnzA` 在 CSR 版里也只用于 host 侧的 `cudaMalloc`/`cudaMemcpy` 尺寸计算。参数残留是重构不彻底的痕迹，也提示你读签名时不要默认"参数都在起作用"。

**练习 2**：内层两个数组都按内维下标升序排列，还能再优化吗？
**答案**：可以。把 `if(indicesA[i] == indicesB[j])` 的双重循环换成**双指针归并**：`i`、`j` 各自从区间起点出发，值小的那边前进，相等时累加并双双前进。单次配对代价从 \( \overline{rowA} \times \overline{colB} \)（约 105 次比较）降到 \( \overline{rowA} + \overline{colB} \)（约 21 次）。这是算法级优化的又一层，本基准未实现。

**练习 3**：既然 CSC 这么好，为什么真实项目不"全部用 CSC"？
**答案**：因为格式的好坏取决于**访问方向**。本例中 \( A \) 按"行"消费、\( B \) 按"列"消费，所以 A 用 CSR、B 用 CSC 恰好各得其所；如果换一个按行访问 \( B \) 的算法（比如 SpMV：\( y = Ax \)），CSR 的 \( B \) 才是对的。稀疏格式没有万能解，**先看访问模式，再选布局**——这也是下一单元（u5-l5 数据布局与传输量）会再次出现的主题。

---

### 4.4 对照实验：入口调用、nvprof 读法与跨平台一致性

#### 4.4.1 概念说明

有了两个 kernel，还必须有可信的测量才能下结论。这个模块讲三件事：程序怎么调用两个入口、nvprof 表怎么读、以及仓库自带的两台机器结果是否支持同一个结论。

#### 4.4.2 核心流程

```
main
 ├── 构造 matrixA(B)、CSR/CSC 三数组
 ├── spmm_csr_serial  ─┐
 ├── spmm_csc_serial   ├─ 三条 CPU 参照路径（不计时）
 ├── matmul_serial     ┘   （稠密三重循环，正确性 oracle）
 ├── spmm_csr_cuda ──→ spmm_csr_csr_warmingup + spmm_csr_csr_kernel
 ├── spmm_csc_cuda ──→ spmm_csc_csr_warmingup + spmm_csc_csr_kernel
 └── 4 行 check(...) 输出
```

一个必须先指出的口径问题：[SpMM_cuda.c:L274-L282](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L274-L282) 里 `elapsed` 被计算出来（多轮循环还被注释掉了，却仍除以 `num_runs = 5`），**但从未被 printf**。本程序自身不打印任何时间，唯一的时间来源就是 nvprof——这与 CoMem_AXPY 等基准不同，读归档输出时不要找"time"行。

#### 4.4.3 源码精读

[SpMM_cuda.c:L276-L281](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L276-L281) —— 五次调用的顺序。注意 `spmm_csr_cuda` 与 `spmm_csc_cuda` 传的都是同一套 \( A \) 的 CSR 数组，\( B \) 的数组则一 CSR 一 CSC。两入口在同一次进程里先后执行，所以 nvprof 一次就能拿到 4 个 kernel 的时间，靠函数名区分归属，无需跑两次。

[SpMM_cuda.c:L284-L287](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L284-L287) —— 四行 `check` 输出。`check` 的注释写的是"return percentage of difference"，但看 [L218-L227](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L218-L227) 的实现：算完 `diffsum` 和 `sum` 后**只返回了 `diffsum`**，归一化那一步没做——又一个"注释与代码不符"的例子（u2-l4 讲过 check 只是弱探针）。归档数据里 `check(serial vs serial_csr):0.000000` 而 `check(serial vs cuda_*):0.000288`：串行两版逐项严格为 0，说明它们与稠密 oracle 在**相同的求和顺序**下得到逐位一致的结果；GPU 版的小偏差更可能来自 nvcc 默认把 `dot += a*b` 编译成融合乘加（FMA）带来的舍入差异——此解释合理但**待本地验证**（可用 `-fmad=false` 重编译对照）。

[SpMM_cuda.output.carina.txt:L17-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L17-L22) —— Carina 的 GPU activities 表。关键两行：`spmm_csr_csr_kernel` **48.007ms**，`spmm_csc_csr_kernel` **0.916ms**，比值 **52.4**。warmingup 与被测 kernel 几乎同时间，印证 4.3.3 的判断。

[SpMM_cuda.output.fornax.txt:L18-L19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt#L18-L19) —— Fornax 的同两行：`spmm_csr_kernel`（此表只显示了被测 kernel，warmingup 未单列）**354.30ms**，`spmm_csc_kernel` **5.1618ms**，比值 **68.6**。绝对时间比 Carina 慢约 7 倍，但**结论方向完全一致**。

两台机器并排：

| 指标 | Carina | Fornax |
|---|---|---|
| `spmm_csr_csr_kernel` | 48.007 ms | 354.30 ms |
| `spmm_csc_csr_kernel` | 0.916 ms | 5.162 ms |
| 比值（CSR ÷ CSC） | 52.4× | 68.6× |
| 闭式预测 \( W_{csr}/W_{csc} \) | 100× | 100× |
| `cudaMalloc` 占 API 时间 | 75.16%（305.55ms / 14 次） | 53.41%（423.54ms / 14 次） |
| `check(serial vs cuda_*)` | 0.000288 | 0.000308 |

**跨平台结论**：相对结论（CSC 版远快于 CSR 版，几十倍）在两台机器上都成立，且都低于 100× 的理想预测；绝对时间差异巨大（7 倍）则不可跨机器比较。这正是 u1-l4 讲过的原则——**跨平台只比相对结论，不比绝对值**。

另两个影响可复现性的因素：(1) [SpMM_cuda.c:L59](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L59) 的 `srand(time(NULL))` 使**非零位置每次运行都不同**，各行的非零个数有随机起伏，时间会有小幅波动；(2) Fornax 输出第 10 行有 `Warning: Auto boost enabled`，动态加速会使同一 kernel 的时间不稳定。这两点都是 u6-l4 方法论专题的伏笔。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：找到测试矩阵的构造代码、确定规模与稀疏度，用 nvprof 对比两个入口，并检验跨平台结论。
2. **操作步骤**：
   - **第一步（定位）**：打开 [SpMM_cuda.c:L21-L24](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L21-L24)，记录 `num_rows = 100`、`nnzA = nnzB = 1024`；再读 [L55-L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L55-L73) 确认非零个数精确等于 `nnz`。算出稀疏度 \( 1024/10000 = 10.24\% \)、平均每行每列 \( \approx 10.24 \) 个非零。
   - **第二步（本地测量）**：`cd CoMem_SpMM && make`，然后 `nvprof ./SpMM_cuda`。在 GPU activities 表中按函数名把 4 个 kernel 归到两个入口名下（`spmm_csr_cuda` → csr 两个、`spmm_csc_cuda` → csc 两个）。
   - **第三步（对照）**：把你的 4 行 kernel 时间 + 4 行 `check` 输出，与上面 Carina/Fornax 两列并排抄成一张三列表（你的机器 / Carina / Fornax）。
   - **第四步（讨论）**：回答三个问题——你的比值落在 52~69 区间内还是更靠近 100？两台归档机器之间结论是否一致？绝对时间能否直接比较、为什么？
3. **需要观察的现象**：`spmm_csc_csr_kernel` 比 `spmm_csr_csr_kernel` 快几十倍；`check(serial vs cuda_csr)` 与 `check(serial vs cuda_csc)` 两个值**几乎相同**（Carina 上都是 0.000288），说明两 kernel 数值语义一致，性能差异不是靠"少算了东西"换来的。
4. **预期结果**：得到一张三平台对照表，并写出结论"CSC 版快约 N 倍（N 在 50~100 之间），与理论工作量比 num_rows = 100 同数量级；跨平台相对结论稳定，绝对时间不可比"。若你的机器没有 `nvprof`（CUDA 11 之后的工具链），用 `nsys profile --stats=true ./SpMM_cuda` 或 `ncu` 替代（u1-l2 讲过这一替代关系）。**待本地验证**。
5. 若完全无 GPU：把上表两列归档数据当作"云端实验数据"完成第三、四步的对照与讨论，并补做 4.3.4 的纸笔推导。

#### 4.4.5 小练习与答案

**练习 1**：`nvprof` 表里 Carina 有 4 个 kernel、Fornax 只有 2 个，是不是 Fornax 少跑了 warmingup？
**答案**：不是。两份源码与两入口结构完全相同，Fornax 的转录只是**表格被截断/未完整粘贴**（它的 API calls 里 `cudaLaunchKernel` 显示 `2` 次调用，同样与实际的 4 次不符，且总 API 时间远小于 GPU 时间，明显转录不全）。读归档终端转录时要警惕这类缺口，用 `Calls` 列与源码交叉核对（u1-l4 的方法）。

**练习 2**：为什么 Carina 上 `cudaMalloc` 占了 API 时间的 75%（305ms），比所有 kernel 加起来还久？
**答案**：14 次 `cudaMalloc` 中首次调用会触发 CUDA 上下文创建，这一一次性开销（约 300ms）被计入了 `cudaMalloc` 的 Max 列（305.31ms）。这正是 u1-l4 在 BankRedux 上看到的同一现象，也是所有微基准都需要 warmingup 的根本原因之一。

**练习 3**：如果只看本程序的 stdout（4 行 check），能不能得出"CSC 更快"的结论？
**答案**：完全不能。程序不打印任何时间（`elapsed` 算了没用），stdout 只携带正确性信息。这个例子反向说明了 u1-l4 的教训：**必须知道测量的口径在哪里**，否则你会把"没有输出时间"误当成"没有差异"。

---

## 5. 综合实践

**任务：给 SpMM 补一份"密度—收益"实验报告。**

背景：4.3.2 推出 \( W_{csr}/W_{csc} = \text{num\_rows} \)，与稀疏度无关。这个结论值得被挑战一次。请完成：

1. **准备三组参数**（改 [SpMM_cuda.c:L22-L24](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L22-L24) 后重新 `make`）：
   - A：`num_rows=100, nnzA=nnzB=1024`（稀疏度 10.24%，出厂设置）
   - B：`num_rows=100, nnzA=nnzB=5120`（稀疏度 51.2%）
   - C：`num_rows=100, nnzA=nnzB=9000`（稀疏度 90%，接近稠密）
2. **测量**：每组跑 `nvprof ./SpMM_cuda`，记录 `spmm_csr_csr_kernel` 与 `spmm_csc_csr_kernel` 的时间与比值，同时记录 4 行 `check`。
3. **分析**：按公式，三组的比值都应约等于 100——稀疏度 \( \rho \) 同时出现在分子分母并被约掉。观察偏差往哪个方向走：\( \rho \) 越小，CSC kernel 越快、与它绑定的固定开销（`column` 循环、结果写回、启动同步）占比越大，**实测比值应越低于 100**；\( \rho \) 越大两个 kernel 都变慢，固定开销被摊薄，**比值应越贴近 100**。同时思考一个更根本的问题：\( \rho \to 1 \) 时稀疏格式相对稠密 `matmul_serial` 还剩什么优势？（提示：三数组编码省的是"零元素的存储与计算"，\( \rho = 90\% \) 时已经省不了多少了。）
4. **写报告**：一页以内，含参数表、时间表、比值表，以及一段"CSC 的收益从哪里来"的结论（应同时提到工作量缩减与 gather 消失两个来源）。

**无 GPU 的替代版本**：不改代码，改为精读 [SpMM_cudakernel.cu:L31-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L31-L52) 与 [L76-L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L76-L96)，推导出三组参数下各自的内层迭代次数（用稀疏度 \( \rho = nnz/n^2 \) 表达 \( \overline{rowA} = \rho n \)、\( \overline{colB} = \rho n \)），画出"比值 vs \( \rho \)"的理论曲线并标出它在 \( \rho \to 1 \) 时的行为，最后用 Carina/Fornax 两个已知点校验数量级。**待本地验证**。

## 6. 本讲小结

- 稀疏矩阵用 `ptr / indices / data` 三数组编码：CSR 按行切区间（`indices` 存列号），CSC 按列切区间（`indices` 存行号），`ptr` 长 \( n+1 \) 用哨兵让任何区间都能写成 `ptr[i+1] - ptr[i]`。
- `spmm_csr_csr_kernel` 的反模式：\( B \) 按 CSR 存，求"第 k 列"没有索引可用，于是对每个 \( (k, i) \) 把 `nnzB` 个元素全扫一遍，内层迭代的有效率约 \( 1/1024 \)。
- `spmm_csc_csr_kernel` 的优化：把 \( B \) 换成 CSC，配对区间由 `ptrB[column]` 直接给出，配对条件从"两个条件的全表过滤"退化为"两个有序小数组的等值对撞"。
- 闭式预测：两版工作量之比 \( = \text{num\_rows} = 100 \)，**与稀疏度无关**；Carina 实测 52.4×、Fornax 实测 68.6×，同数量级、方向一致，差额来自固定开销与行列长度的随机起伏。
- 访存模式也随之改善：`ptrB[indicesA[i]]` 这个数据依赖的随机 gather 被替换为 warp 内同址广播；但要诚实区分——本例的主要收益是**工作量缩减（算法级）**，合并性改善是副产品。
- 工程细节：本目录规模写死在源码全局变量、无 `test.sh`、程序不打印时间（`elapsed` 算而未印），nvprof 是唯一时间来源；`check` 注释说返回百分比、实际返回未归一化的 `diffsum`；`srand(time(NULL))` 使非零位置每次运行不同。

## 7. 下一步学习建议

- **同一条主线的下一站是 [u5-l5（MiniTransfer SpMV）](u5-l5-data-layout-spmv.md)**：那里的 dense / CSR / unified 三种布局比较的是**CPU-GPU 传输量**，与本讲比较的**设备端访存与工作量**互为镜像——两讲合起来构成"数据布局决定一切"的完整图景。
- 想继续在设备端深挖访存，接着学 [u4-l4（MemAlign）](u4-l4-memory-alignment.md)（地址对齐）与 [u4-l5（BankRedux）](u4-l5-shared-memory-bank-conflicts.md)（共享内存 bank 冲突），把"全局内存合并 → 对齐 → 共享内存 bank"这条层次链补全。
- 若你对"两个有序小数组的配对"感兴趣，可以把 4.3.5 练习 2 的双指针归并真正写出来，用 `nvprof` 对比它与朴素对撞版——这就是一次属于你自己的微基准迭代。
- 方法论层面，把本讲的"闭式预测 + 双平台归档 + 本地复现"三步法记下来，[u6-l4](u6-l4-benchmark-methodology-and-interpretation.md) 会把它扩展成完整的跨平台实验规范。
