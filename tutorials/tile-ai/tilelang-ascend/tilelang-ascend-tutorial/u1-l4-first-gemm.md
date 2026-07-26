# 第一个算子：运行并读懂 GEMM

## 1. 本讲目标

本讲是整个学习路线里「第一次真正动手写并看懂一个 Ascend 算子」的环节。读完本讲后，你应该能够：

1. 成功运行 `examples/gemm/example_gemm.py`，看到终端打印 `Kernel Output Match!`。
2. 逐行读懂这份不到 60 行的 GEMM 代码，说清每一个 `T.xxx` 原语在做什么。
3. 理解「block 切分、逻辑核绑定（cid）、K 维分块累加」这三件把数学公式 \(C = A \times B\) 变成可运行 kernel 的关键事情。
4. 知道 `block_M / block_N / K_L1` 这些可调参数的约束，并能动手修改它们观察行为。

本讲只聚焦一个最小模块：[examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)。编译器内部如何把这些原语变成 Ascend C 代码、再编成 `.so`，是第 5 讲（u1-l5）和第六单元（u6）的内容，本讲只需知道「调用 `func(a, b)` 会触发这条链路」即可。

## 2. 前置知识

### 2.1 GEMM 是什么

GEMM（GEneral Matrix Multiply，通用矩阵乘）计算 \(C = A \times B\)，其中 A 形状为 \((M, K)\)，B 形状为 \((K, N)\)，C 形状为 \((M, N)\)。每个输出元素是：

\[
C[i, j] = \sum_{k=0}^{K-1} A[i, k] \cdot B[k, j]
\]

GEMM 是深度学习里最核心的计算（全连接层、注意力里的 QK^T 和 PV、卷积经 im2col 后都是 GEMM），所以它被选作 tilelang-ascend 的「Hello World」。

### 2.2 Ascend 的片上存储层级

承接第 1 讲的内存层级映射，运行一个 GEMM 需要这三层存储配合：

| 存储层级 | tilelang 中的 scope | 在 GEMM 里放什么 |
| :--- | :--- | :--- |
| GM（global memory） | 默认 | 输入矩阵 A、B 和输出 C（大，但慢） |
| L1（Cube 核的片上缓存） | `shared.l1` | A、B 的小块，靠 GM 更近、更快 |
| L0C（Cube 核的累加器） | `wmma.accumulator` | 累加结果 C 的小块（寄存器级，最快） |

数据流是：**GM → L1 →（在 L0C 里累加）→ GM**。tilelang-ascend 用 `T.copy` 控制搬运，用 `T.gemm_v0` 控制 Cube 核做乘加。

### 2.3 三个关键直觉

- **切分（tiling）**：矩阵太大装不进 L1/L0C，所以要切成小块（tile），一块一块地算。
- **分块累加（blocked accumulation）**：K 维很长，L1 一次也放不下整条 K，所以沿 K 方向也切块，每次读一小段 K，算出部分和，累加进 L0C。
- **逻辑核（core）并行**：把 C 切成很多块，每块交给一个 Ascend 逻辑核（AI Core）独立计算，互不干扰。

## 3. 本讲源码地图

本讲只涉及一个核心文件，外加 README 作为注释版参考：

| 文件 | 作用 |
| :--- | :--- |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py) | 本讲主角：一个完整可运行的 GEMM |
| [README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md) | 里有这份 GEMM 的带注释版本和运行说明，可对照阅读 |

为了让「源码精读」里引用的原语落到实处，本讲还会顺带指向这些**原语的 Python 定义文件**（不需要现在读懂，知道「点进去能查到」即可）：

- `tilelang/jit/__init__.py` —— `@tilelang.jit` 装饰器。
- `tilelang/language/kernel.py` —— `T.Kernel`。
- `tilelang/language/allocate.py` —— `T.alloc_L1` / `T.alloc_L0C`。
- `tilelang/language/copy_op.py` —— `T.copy`（在 Ascend 上解析为 `npu_copy_v2`）。
- `tilelang/language/gemm.py`... 注意：示例用的是 `T.gemm_v0`，它定义在 `tilelang/language/ascend.py`。
- `tilelang/language/ascend.py` —— `T.gemm_v0`、`T.barrier_all`。
- `tilelang/language/warpgroup.py` —— `T.Scope`。

## 4. 核心概念与源码讲解

我们把这份 60 行的代码拆成 5 个最小模块，逐一吃透。先用一张「全景图」定位每个模块在代码里的位置：

```
@tilelang.jit(out_idx=[-1])          ← 模块 4.1
def matmul(M, N, K, block_M, block_N, K_L1, ...):
    @T.prim_func
    def main(A, B, C):               ← 模块 4.1：参数与张量
        with T.Kernel(...) as (cid, _):  ← 模块 4.2：逻辑核与 block 切分
            A_L1 = T.alloc_L1(...)       ← 模块 4.3：片上存储分配
            C_L0 = T.alloc_L0C(...)
            with T.Scope("C"):           ← 模块 4.4：执行域
                for k in T.serial(loop_k):  ← 模块 4.5：主循环
                    T.copy(A[...], A_L1)    ← 模块 4.4：数据搬运
                    T.copy(B[...], B_L1)
                    T.barrier_all()        ← 模块 4.5：同步
                    T.gemm_v0(...)         ← 模块 4.5：乘加
                    T.barrier_all()
                T.copy(C_L0, C[...])       ← 模块 4.4：写回
    return main
```

### 4.1 从 Python 函数到 kernel：`@tilelang.jit` 与 `@T.prim_func`

#### 4.1.1 概念说明

tilelang-ascend 用 Python 写算子，但这段 Python 不是「运行时逐行解释执行」，而是被**捕获**成一种中间表示（TIR），再编译成 Ascend 上能跑的机器码。这套机制叫 **JIT（Just-In-Time，即时编译）**：第一次调用 `func(a, b)` 时，才会真正触发「捕获 → 编译 → 加载」。

两个装饰器各司其职：

- `@tilelang.jit(...)`：把下面这个**返回 `prim_func` 的工厂函数**包装成一个可调用对象 `func`。它负责记录 `out_idx`、`target` 等编译选项，并在首次调用时调用 `compile()`。
- `@T.prim_func`：标记 `main` 是一个「TileLang 原语函数」，它的函数体会被 TVM 捕获成 TIR，而不是当作普通 Python 执行。

#### 4.1.2 核心流程

1. 你调用 `func = matmul(M, N, K, 128, 256, 64)`，得到一个被 JIT 包装的可调用对象。
2. 你调用 `c = func(a, b)`：
   - JIT 用你传入的形状参数 `M/N/K/block_M/block_N/K_L1` 调用工厂函数，得到 `main` 这个 `prim_func`。
   - JIT 调用 `compile(main, ...)`：进入 lowering → codegen → bisheng 编译 → 加载 `.so`。
   - 编译产物被缓存，下次同样参数直接复用。
3. 加载好的 kernel 接收 `a, b`，输出 `c` 返回。

`out_idx=[-1]` 是一个很关键的细节：`-1` 表示参数列表里**最后一个张量（C）是输出**，由运行时**自动分配**，调用者只需传入输入 `a, b`，函数返回值就是 C。

#### 4.1.3 源码精读

装饰器和工厂函数：

[examples/gemm/example_gemm.py:20-21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L20-L21) —— `@tilelang.jit(out_idx=[-1])` 标注 C 为输出；`matmul` 接收 6 个可调参数。

[examples/gemm/example_gemm.py:25-30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L25-L30) —— `@T.prim_func` 标记 `main`；参数 A、B、C 用 `T.Tensor((M, K), dtype)` 声明形状与数据类型。

实例化与调用：

[examples/gemm/example_gemm.py:56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L56) —— `func = matmul(M, N, K, 128, 256, 64)`，即 `block_M=128, block_N=256, K_L1=64`。

[examples/gemm/example_gemm.py:60-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L60-L65) —— 用 `torch.randn(...).half().npu()` 在 NPU 上建 fp16 张量；`c = func(a, b)` 触发首次 JIT 编译并运行，返回自动分配的 C。

`@tilelang.jit` 本身就是普通 Python 装饰器，可在源码里查到它的定义：

[tilelang/jit/__init__.py:265-278](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L265-L278) —— `jit` 函数签名，可见 `out_idx`、`target`、`pass_configs` 等选项。

#### 4.1.4 代码实践

1. **实践目标**：观察「首次调用触发编译、第二次调用复用缓存」。
2. **操作步骤**：
   - 打开 `examples/gemm/example_gemm.py`，在第 65 行 `c = func(a, b)` 之前加一行 `import time; t0 = time.time()`，之后加 `print("first call(s):", time.time() - t0)`。
   - 再加一次 `t0 = time.time(); c2 = func(a, b); print("second call(s):", time.time() - t0)`。
3. **需要观察的现象**：第一次调用耗时应显著长于第二次（首次含编译时间）。
4. **预期结果**：第一次明显更慢；两次结果一致（缓存命中）。
5. 由于本机不一定有 Ascend 卡，运行耗时**待本地验证**。

#### 4.1.5 小练习与答案

- **Q1**：如果把 `out_idx=[-1]` 改成 `out_idx=[2]`，行为会变吗？
  - **答**：不变。`C` 是参数列表（A, B, C）的第 3 个，下标 `2`；`-1` 也是指向同一个 C。两者等价。
- **Q2**：为什么 `c = func(a, b)` 只传了两个输入，C 却有值？
  - **答**：`out_idx=[-1]` 告诉 JIT「C 是输出」，运行时自动分配 C 的内存并把算出的结果作为返回值返回。

### 4.2 逻辑核绑定与 block 切分：`T.Kernel`、`cid`、`bx/by`

#### 4.2.1 概念说明

Ascend NPU 上有很多 AI Core（逻辑核）。tilelang-ascend 用 `T.Kernel` 来声明「这个 kernel 要在多少个逻辑核上并行启动」。每个逻辑核拿到一个唯一编号 `cid`，据此决定自己负责 C 的哪一块。

在本示例里，每个核算 C 的一个 \((\text{block\_M} \times \text{block\_N})\) 块，不同核之间互不通信、互不干扰。

#### 4.2.2 核心流程

1. 计算行列方向的块数：
   \[
   m\_num = M / \text{block\_M}, \quad n\_num = N / \text{block\_N}
   \]
2. 启动核数 = `m_num * n_num`，每个核拿到 `cid ∈ [0, m_num*n_num)`。
3. 把一维的 `cid` 映射回二维坐标：
   \[
   bx = cid \,/\, n\_num \quad (\text{行块号}), \qquad by = cid \bmod n\_num \quad (\text{列块号})
   \]
4. 该核负责的 C 块起始坐标是 \((bx \cdot \text{block\_M},\; by \cdot \text{block\_N})\)。

以默认参数 \(M=N=1024,\ \text{block\_M}=128,\ \text{block\_N}=256\) 为例：\(m\_num=8,\ n\_num=4\)，共 32 个核；`cid=5` 对应 \(bx=5/4=1,\ by=5\%4=1\)。

#### 4.2.3 源码精读

[examples/gemm/example_gemm.py:22-23](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L22-L23) —— 计算 `m_num` 和 `n_num`（注意是整除 `//`，要求 block 能整除 M/N）。

[examples/gemm/example_gemm.py:31-33](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L31-L33) —— `T.Kernel(m_num * n_num, is_npu=True) as (cid, _)` 启动核；`bx = cid // n_num`、`by = cid % n_num` 还原二维坐标。

`T.Kernel` 的 Python 定义里能看到 `is_npu` 分支怎么处理：

[tilelang/language/kernel.py:215-223](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L215-L223) —— `Kernel` 函数签名，含 `is_npu`、`threads` 等参数。

[tilelang/language/kernel.py:247-263](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L247-L263) —— `is_npu=True` 且 `threads=None` 时走 NPU 主路径，断言「NPU kernel 必须只有一维 block 维度」。

关于 `as (cid, _)` 返回值：当 `is_npu=True` 且未设 `threads` 时，`KernelLaunchFrame.__enter__` 返回两个值，第一个是核号 `cid`、第二个是向量子号 `vid`（本例未切分向量核，`vid` 恒为 0，所以用 `_` 忽略）：

[tilelang/language/kernel.py:101-104](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L101-L104) —— `maybe_npu` 分支返回 `[frames[0], frames[1]]`，即 `(cid, vid)`。

#### 4.2.4 代码实践

1. **实践目标**：验证「不同 cid 算的是 C 的不同块」。
2. **操作步骤**：
   - 在 `with T.Scope("C"):` 内、`for k` 循环之前，加一行 `T.printf("cid=%d bx=%d by=%d\n", cid, bx, by)`（`T.printf` 的用法见第 4 讲 u7-l4，本讲只需照抄）。
   - 用 `--m 256 --n 256` 跑一个小规模版本（修改默认或加命令行参数）。
3. **需要观察的现象**：设备端打印里出现多组不同的 `(cid, bx, by)`，且 `bx*n_num+by == cid`。
4. **预期结果**：每组三元组满足上述等式，覆盖 `0..m_num*n_num-1`。
5. 打印行为依赖 `TL_PTO_DEBUG` 等环境，**待本地验证**。

#### 4.2.5 小练习与答案

- **Q1**：默认参数下 `cid` 的取值范围是多少？`cid=9` 对应哪个 `(bx, by)`？
  - **答**：范围 `0..31`；`cid=9 → bx=9//4=2, by=9%4=1`，即 C 的第 2 行块、第 1 列块。
- **Q2**：为什么 `T.Kernel` 的 block 维度必须是一维（`m_num * n_num`），而不是直接写二维？
  - **答**：NPU 的核调度按一维 `cid` 分配（见 `kernel.py:248` 的断言），所以 tilelang-ascend 强制 NPU kernel 用一维 block，再在代码里手动 `//` 和 `%` 还原二维坐标。

### 4.3 片上存储分配：`T.alloc_L1` 与 `T.alloc_L0C`

#### 4.3.1 概念说明

Cube 核要做矩阵乘，必须先把数据搬到片上。tilelang-ascend 提供了「按物理存储命名」的分配原语，名字直接告诉你数据放在哪：

- `T.alloc_L1(shape, dtype)`：在 **L1** 上分配一块缓冲（A、B 的小块）。
- `T.alloc_L0C(shape, dtype)`：在 **L0C 累加器**上分配（放乘加结果 C 的小块）。

注意：**累加精度** `accum_dtype="float"` 比 `dtype="float16"` 更宽。这是因为累加很多次后会放大误差，用 fp32 累加能保证数值精度，最后写回 GM 时再转回 fp16。

#### 4.3.2 核心流程

1. 每个核进入自己的 scope 后，在片上分配三块缓冲：
   - `A_L1`：形状 \((\text{block\_M},\ K\_L1)\)，放一块 A。
   - `B_L1`：形状 \((K\_L1,\ \text{block\_N})\)，放一块 B。
   - `C_L0`：形状 \((\text{block\_M},\ \text{block\_N})\)，累加器，dtype 是 fp32。
2. 这些缓冲只在「本核的一次 kernel 调用」内有效，生命周期由编译器管理。
3. `shape` 必须和后续 `T.copy` 的源/目、`T.gemm_v0` 的输入严格对齐。

#### 4.3.3 源码精读

[examples/gemm/example_gemm.py:35-38](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L35-L38) —— 分配 `A_L1`、`B_L1`（fp16）和 `C_L0`（fp32 累加）。

这些原语只是给 TVM 的 `alloc_buffer` 套了一层「指定 scope」的壳：

[tilelang/language/allocate.py:140-141](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L140-L141) —— `alloc_L1` 用 `scope="shared.l1"`。

[tilelang/language/allocate.py:152-153](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L152-L153) —— `alloc_L0C` 用 `scope="wmma.accumulator"`。

[tilelang/language/allocate.py:128-137](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L128-L137) —— 注释列出了 TIR scope 与 Ascend 物理存储的对应表（`shared.l1 → L1`、`wmma.accumulator → L0C` 等），是最权威的对照表。

#### 4.3.4 代码实践

1. **实践目标**：体会「累加器用更宽的精度」。
2. **操作步骤**：把 `accum_dtype="float"` 改成 `accum_dtype="float16"`，重新运行 `example_gemm.py`。
3. **需要观察的现象**：`torch.testing.assert_close` 可能会因为 K=1024 次累加导致 fp16 溢出/精度损失而失败。
4. **预期结果**：fp16 累加时大概率数值误差变大甚至 `inf`，校验失败；改回 `"float"` 后恢复 `Kernel Output Match!`。
5. **待本地验证**（取决于硬件与 dtype 模板支持）。

#### 4.3.5 小练习与答案

- **Q1**：`C_L0` 为什么是 \((\text{block\_M}, \text{block\_N})\) 而不是 \((M, N)\)？
  - **答**：每个核只算 C 的一个块，所以累加器只需容纳本核负责的 \(\text{block\_M}\times\text{block\_N}\) 区域。
- **Q2**：`A_L1` 的第二个维度为什么是 `K_L1` 而不是 `K`？
  - **答**：沿 K 方向分块，L1 一次只放一小段 K（长度 `K_L1`），整条 K 靠外层循环逐段搬入。

### 4.4 数据搬运与执行域：`T.copy` 与 `T.Scope("C")`

#### 4.4.1 概念说明

数据在 GM↔L1↔L0C 之间的移动由 `T.copy(src, dst)` 描述。在 Ascend 上，`T.copy` 实际解析为 `npu_copy_v2`（一个搬运 intrinsic `tl.ascend_copy`），编译器会根据 `src`/`dst` 的 scope 自动选择正确的 AscendC 搬运指令（如 `DataCopy`）。

`T.copy` 的切片语法 `A[bx * block_M, k * K_L1]` 表示从 A 的某个起点开始，搬一块和 `A_L1` 同样大小的数据——起点由你给，大小由目标 buffer（`A_L1`）的形状决定。

`T.Scope("C")` 则划定**执行域**：`"C"` 表示 Cube 域。把它包在搬运和计算外面，告诉编译器「这些操作在 Cube 核上执行」。本例整个 kernel 都是 Cube 计算，所以用一个 `T.Scope("C")` 包住。

#### 4.4.2 核心流程

1. **搬入**：`T.copy(A[bx*block_M, k*K_L1], A_L1)` —— 从 GM 把 A 的一块搬到 L1。
2. **搬入**：`T.copy(B[k*K_L1, by*block_N], B_L1)` —— 同理搬 B 的一块。
3. （在 Cube 域里做 `gemm_v0`，见 4.5）
4. **搬出**：`T.copy(C_L0, C[bx*block_M, by*block_N])` —— 把累加结果从 L0C 写回 GM 的对应位置。

切片坐标的含义：`A[bx*block_M, k*K_L1]` 表示 A 的行从 `bx*block_M` 开始、列从 `k*K_L1` 开始，取一块和目标 `A_L1`（\(\text{block\_M}\times K\_L1\)）等大的区域。

#### 4.4.3 源码精读

[examples/gemm/example_gemm.py:40-44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L40-L44) —— `with T.Scope("C"):` 进入 Cube 域；循环内 `T.copy` 把 A、B 的块从 GM 搬到 L1。

[examples/gemm/example_gemm.py:51](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L51) —— `T.copy(C_L0, C[bx * block_M, by * block_N])` 把最终结果写回 GM。

`T.copy` 在 `__init__.py` 里被重命名指向 Ascend 版：

[tilelang/language/__init__.py:53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L53) —— `from .copy_op import copy, c2d_im2col, npu_copy_v2 as copy`，即 `T.copy` 实为 `npu_copy_v2`。

[tilelang/language/copy_op.py:257-268](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py#L257-L268) —— `npu_copy_v2` 签名，最终发出 `tl.ascend_copy` intrinsic。

`T.Scope` 的定义：

[tilelang/language/warpgroup.py:74-94](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/warpgroup.py#L74-L94) —— `Scope(name)` 转发到 `_ffi_api.Scope(name)`，构造一个执行域 frame。

#### 4.4.4 代码实践

1. **实践目标**：看清 `T.copy` 切片如何定位「搬哪一块」。
2. **操作步骤**：阅读 [README.md:190-241](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L190-L241) 里这份 GEMM 的带注释版本，对照 `T.copy(A[bx * block_M, k * K_L1], A_L1)` 的注释「Copy A and B blocks from global memory to L1 cache」。
3. **需要观察的现象**：注释会确认起点坐标的含义（行起点 = 行块号×块高，列起点 = K 块号×K_L1）。
4. **预期结果**：理解 `T.copy` 的「起点由参数给、大小由目标 buffer 决定」的约定。
5. 这是纯阅读型实践，无需硬件即可完成。

#### 4.4.5 小练习与答案

- **Q1**：`T.copy(C_L0, C[bx*block_M, by*block_N])` 里，写入 C 的「块大小」由谁决定？
  - **答**：由源 `C_L0` 的形状 \((\text{block\_M}, \text{block\_N})\) 决定；目标切片只给起点 `(bx*block_M, by*block_N)`。
- **Q2**：本例为什么整个 kernel 都包在一个 `T.Scope("C")` 里？
  - **答**：本例只有 Cube 计算（搬运 + gemm），没有 Vector 计算，所以全在 Cube 域。当出现 Vector 操作（如 softmax）时才需要 `T.Scope("V")`，详见第四、五单元。

### 4.5 主循环：K 维分块累加、`T.gemm_v0` 与 `T.barrier_all`

#### 4.5.1 概念说明

这是 GEMM 的计算核心。沿 K 方向把整条 K（默认 1024）切成 `loop_k` 段（每段长 `K_L1=64`，所以 `loop_k = 1024/64 = 16`），每次搬一段 A、一段 B 进 L1，做一次 \(\text{block\_M}\times\text{block\_N}\) 的小矩阵乘，把结果**累加**进 `C_L0`。

\[ C_L0 \;{=}\; \sum_{k=0}^{\text{loop\_k}-1} A_{L1}^{(k)} \times B_{L1}^{(k)} \]

两个细节：

- **`init=(k == 0)`**：第一次（k=0）时先把 `C_L0` 清零再累加；之后（k>0）只累加不清零（读-改-写）。这对应硬件 MMA 指令的「init / accumulate」两种模式。
- **`T.barrier_all()`**：搬运用的是 MTE 流水、gemm 用的是 Cube（M）流水，两者异步。`barrier_all` 插一道屏障，保证「搬运完成 → 再算」和「算完 → 再覆盖」，避免数据竞争。

#### 4.5.2 核心流程

```
loop_k = ceil(K, K_L1)            # K 方向的分块数
for k in [0, loop_k):
    搬 A 块 → A_L1                # MTE 流水，异步
    搬 B 块 → B_L1                # MTE 流水，异步
    barrier_all()                 # 等搬运完成
    C_L0 = (k==0 ? 0 : C_L0) + A_L1 @ B_L1   # Cube 流水
    barrier_all()                 # 等计算完成，保护下一轮搬运
搬 C_L0 → GM 的对应块
```

伪代码里 `loop_k` 用 `ceildiv`（向上取整除法）：

\[
\text{loop\_k} = \left\lceil K / K\_L1 \right\rceil
\]

> 注：本示例**没有**处理 K 不能整除 `K_L1` 的尾部，所以实际要求 `K % K_L1 == 0`。尾部处理（tail mask）是第六单元 u6-l6 的内容。

#### 4.5.3 源码精读

[examples/gemm/example_gemm.py:41-49](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L41-L49) —— 主循环本体：`loop_k = T.ceildiv(K, K_L1)`、`for k in T.serial(loop_k)`、两次 `T.copy`、`T.barrier_all()`、`T.gemm_v0(..., init=(k==0))`、再次 `T.barrier_all()`。

关键原语定义：

[tilelang/language/ascend.py:343-380](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L343-L380) —— `gemm_v0` 签名与文档：参数 `init` 控制是否清零累加器；`kL0Size` 控制 L1→L0 的 K 向分块（默认 128，必须是 16 的倍数）。

[tilelang/language/ascend.py:424](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L424) —— `Cptr = _retrieve_ptr(C, "w" if init is True else "rw")`：`init=True` 时 C 以「只写」访问（清零后写），否则「读写」（累加）——这正是 `init=(k==0)` 的语义来源。

[tilelang/language/ascend.py:439-448](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L439-L448) —— 最终发出 `tl.ascend_gemm_v0` intrinsic，模板参数为 `<dtype, accum_dtype, M, N, K, transpose_A, transpose_B, kL0Size>`。

[tilelang/language/ascend.py:200-211](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L200-L211) —— `barrier_all` 发出 `tl.ascend_pipe_barrier` 并以 `"ALL"` 屏蔽所有流水，确保之前所有指令完成。

`T.ceildiv` 与 `T.serial` 来自 TVM 的 TIR 基础设施：

[tilelang/language/tir/op.py:3017-3034](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/op.py#L3017-L3034) —— `ceildiv` 即向上取整除法。

#### 4.5.4 代码实践

1. **实践目标**：体会 `init` 的作用与 K 累加的正确性依赖。
2. **操作步骤**：把 `init=(k == 0)` 改成 `init=True`（每轮都清零），重新运行。
3. **需要观察的现象**：因为每轮都清零，`C_L0` 只会保留**最后一个** K 段的贡献，最终 C 只反映了 \(A_{L1}^{(15)} \times B_{L1}^{(15)}\)，而非完整求和。
4. **预期结果**：`assert_close` 失败，数值与 `ref_c = a @ b` 差距很大。改回 `init=(k==0)` 即恢复正确。
5. **待本地验证**。

#### 4.5.5 小练习与答案

- **Q1**：默认参数下 `loop_k` 等于多少？一共要做多少次 `gemm_v0`？
  - **答**：`loop_k = ceildiv(1024, 64) = 16`；每个核做 16 次 `gemm_v0`。
- **Q2**：两处 `T.barrier_all()` 分别在保护什么？只留一个会怎样？
  - **答**：第一处保护「搬入完成后再 gemm」（避免读到没搬完的数据）；第二处保护「gemm 完成后再进入下一轮搬入」（避免搬运覆盖还在用的 L1）。去掉任一处都可能产生数据竞争或读到脏数据。
- **Q3**：`T.gemm_v0` 的 K 维来自哪个 buffer 的形状？
  - **答**：来自 `A_L1`/`B_L1` 的 K 向维度，即 `K_L1`（见 `ascend.py:414` 的 `K = A_shape[-1]`），所以每次 `gemm_v0` 做的是 \(K\_L1\) 长度的收缩。

## 5. 综合实践

把本讲的知识串起来，完成下面这个调参实验（这是本讲的核心实践任务）：

1. **实践目标**：弄清 `block_M / block_N / K_L1` 三个参数的合法约束，理解哪些组合能跑通。
2. **运行基准**：按 README 指引运行一次原始示例，确认看到 `Kernel Output Match!`：
   ```bash
   cd examples/gemm
   python example_gemm.py
   ```
   （运行说明见 [README.md:143-152](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L143-L152)。）
3. **改参重跑**：修改 [example_gemm.py:56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L56) 的 `matmul(M, N, K, 128, 256, 64)`，依次尝试下表组合，记录每组是否仍打印 `Kernel Output Match!`：

   | 组号 | block_M | block_N | K_L1 | 你的预测 | 实测结果 |
   | :--: | :--: | :--: | :--: | :-- | :-- |
   | 1（基准） | 128 | 256 | 64 |  |  |
   | 2 | 128 | 128 | 64 |  |  |
   | 3 | 128 | 256 | 32 |  |  |
   | 4 | 128 | 256 | 128 |  |  |
   | 5 | 128 | 200 | 64 |  |  |
   | 6 | 64 | 256 | 48 |  |  |
   | 7 | 100 | 256 | 64 |  |  |

4. **解释结果**：根据本讲学到的约束推断每组结果，并与实测对照：
   - **块整除约束**：`M % block_M == 0` 且 `N % block_N == 0`（因为 `m_num = M // block_M` 是整除，不整除会漏算）。`M=N=1024`，所以 `block_N=200`（组 5）会让 `1024/200` 截断，预期失败。
   - **K 整除约束**：`K % K_L1 == 0`（本示例无尾部处理）。`K_L1=48`（组 6）下 `1024/48` 非整除，预期数值错误。
   - **MMA 对齐约束**：`block_M / block_N / K_L1` 一般应是 16 的倍数（Cube 分形粒度），`kL0Size` 还要求是 16 的倍数（见 `ascend.py:419`）。`block_M=100`（组 7）非 16 倍数，预期失败或被模板拒绝。
   - **块数缩放**：`block_N=128`（组 2）合法，`n_num=8`，核数从 32 变 64，应仍 Match；`K_L1=32/128`（组 3、4）合法且整除，应仍 Match。
5. **观察进阶**：对合法组合，留意首次编译耗时和核数变化——块越小，核数越多、单核工作量越少。
6. **预期结果**：组 1/2/3/4 通过；组 5/6/7 失败（分别因 N 不整除、K 不整除、M 非 16 对齐）。**待本地验证**（实际失败原因以硬件与模板报错为准）。

> 说明：本综合实践需要真实 Ascend NPU 与 CANN 环境。若本机没有，可把「解释结果」部分作为纯源码阅读练习完成——你能基于上面的约束推出哪些组合「应当」失败，就达到了本讲的目标。

## 6. 本讲小结

- `@tilelang.jit(out_idx=[-1])` 把一个返回 `@T.prim_func` 的工厂函数包装成可调用 kernel，`out_idx=[-1]` 让最后一个张量 C 由运行时自动分配并返回。
- `T.Kernel(m_num*n_num, is_npu=True) as (cid, _)` 在多个逻辑核上启动 kernel，每个核用 `cid // n_num`、`cid % n_num` 还原到自己负责的 C 块坐标 `(bx, by)`。
- `T.alloc_L1` / `T.alloc_L0C` 在 Cube 核的 L1 与 L0C 上分配缓冲；累加器用更宽的 `accum_dtype="float"` 保精度。
- `T.copy`（实际是 `npu_copy_v2`）负责 GM↔L1↔L0C 的搬运，切片只给起点、大小由目标 buffer 决定；`T.Scope("C")` 划定 Cube 执行域。
- 主循环用 `T.serial` 沿 K 分块，每段 `T.copy` 搬入、`T.barrier_all` 同步、`T.gemm_v0(..., init=(k==0))` 累加，最终 `T.copy` 写回 GM。
- 参数约束：`M%block_M==N%block_N==K%K_L1==0`，且 block 维度一般要 16 对齐——这些决定了综合实践里哪些组合能通过。

## 7. 下一步学习建议

- **本讲只跑了 GEMM，没看生成的 Ascend C 代码**。下一讲 **u1-l5（JIT 即时编译与运行总流程）** 会带你调用 `func.get_kernel_source()`，看到这些 `T.copy / T.gemm_v0` 最终变成了什么样的 C++/AscendC 代码，并打通「lowering → codegen → bisheng 编译 → 加载」全链路。
- 想系统学习 `@T.prim_func`、`T.Tensor`、dtype 等语言基础，进入 **第二单元（u2）**。
- 想深入了解 `T.copy` 的全部搬运路径、`T.gemm_v0` 的 `kL0Size` 调参、`T.barrier_all` 之外的同步原语，进入 **第三、四单元（u3/u4）**。
- 想看一个**带流水线、布局、swizzle 的高性能 GEMM**，可提前阅读 [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) 和 README 的「High Performance GEMM Example」一节（[README.md:243-343](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L243-L343)），它会由 **u7-l2（高性能 GEMM 优化）** 正式讲解。
