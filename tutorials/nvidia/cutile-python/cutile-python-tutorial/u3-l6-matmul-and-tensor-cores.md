# 矩阵乘与张量核：mma、matmul 与 tfloat32

## 1. 本讲目标

矩阵乘（GEMM）是深度学习与科学计算里最吃算子的运算，也是 GPU 上最能榨干硬件的一类运算。本讲把前面几讲学到的 **load–compute–store 范式（u3-l1）**、**tile 算术与类型转换（u3-l2）**、**归约与累加（u3-l4）** 串起来，落到一个真实可运行的高性能内核上：`samples/MatMul.py`。

学完后你应该能够：

1. 用 cuTile 写出一个完整的分块（tiled）GEMM 内核，理解「沿 K 维循环、每轮累加一个 tile」的结构。
2. 区分 `ct.mma`（乘累加，走张量核）与 `ct.matmul`（纯矩阵乘、会做类型提升）两个 API 的语义差异与适用场景。
3. 理解为什么 float32 输入要先 `astype(ct.tfloat32)` 才能高效喂给张量核，以及这样做带来的精度取舍。
4. 读懂 sample 里的 **swizzle（瓦片重排）** 与 **persistent kernel（持久内核）** 两种调度技巧，理解它们各自解决什么性能问题。

## 2. 前置知识

本讲默认你已经掌握以下概念（前几讲已建立）：

- **tile 与 array 的区别（u2-l2、u2-l3）**：array 是 host 分配的全局可读写显存，tile 是 kernel 内不可变、形状编译期已知的「瓦片」。
- **load / store（u3-l1）**：`ct.load(array, index, shape)` 按 **tile space 索引** 取出一块 tile；`ct.store` 是其逆操作。
- **astype 类型转换（u3-l2、u2-l4）**：受 `RoundingMode` 影响、不改 shape 的数值转换。
- **控制流里的携带值（u3-l3）**：循环体内被重新赋值的变量是「carried values」，本质是函数式 fold——这正是 GEMM 累加器的形态。
- **归约的单位元（u3-l4）**：归约从单位元起算，例如 sum 从 0 起。

本讲新引入两个硬件概念，先一句话建立直觉：

- **张量核（Tensor Core）**：GPU 上专门做「小矩阵乘累加」的硬件单元，一次完成 \(D = A \times B + C\)。它比通用 CUDA 核心快得多，是 GEMM 高性能的来源。`ct.mma` 就映射到它。
- **tfloat32（tf32）**：一种「32 位容器、19 位有效表示（1 符号 + 8 指数 + 10 尾数）」的浮点格式，专门用来给 Ampere 及以后的张量核喂数据。精度比 float32 低，但吞吐高得多。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `samples/MatMul.py` | 主角：一个完整的、带 swizzle 与 persistent 两种形态的 GEMM 示例，以及 host 端封装 `cutile_matmul`。 |
| `src/cuda/tile/_stub.py` | 定义 `mma`、`matmul`、`num_tiles`、`num_blocks`、`full` 等本讲用到的全部内置操作的签名与文档。 |
| `test/test_mma.py` | `mma` / `matmul` 的正确性测试，覆盖 fp16/bf16/fp32/tf32/fp8/int 各类型与多种错误情形，是理解语义的最佳参考。 |
| `src/cuda/tile/_datatype.py` | 定义 `tfloat32`、`float8_e4m3fn` 等受限浮点类型。 |
| `docs/source/data.rst` | 解释「tile space（瓦片空间）」概念，`num_tiles` 与 persistent 遍历的基础。 |
| `src/cuda/tile/_compiler_options.py` | 定义 `num_ctas`、`ByTarget` 等 kernel 配置项。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 mma（乘累加与 K 维循环结构）**、**4.2 matmul（单次矩阵乘）**、**4.3 tfloat32（float32 的张量核格式）**、**4.4 swizzle 与 persistent kernel（调度优化）**。

### 4.1 张量核乘累加 ct.mma 与 K 维累加结构

#### 4.1.1 概念说明

矩阵乘的数学定义是：

\[ C[i,j] = \sum_{k=0}^{K-1} A[i,k] \cdot B[k,j] \]

朴素地在 GPU 上逐元素算，每算一个 \(C[i,j]\) 都要扫描整条 K，既慢又浪费访存。**分块（tiling）** 的思路是：把输出 \(C\) 切成 \(t_m \times t_n\) 的瓦片，每个 block 只负责算一块 \(C\) 瓦片；同时把内积维度 K 也切成大小 \(t_k\) 的瓦片，分批累加。

设 \(T_K\) 为 K 方向的瓦片数，则：

\[ C_\text{tile} = \sum_{t=0}^{T_K-1} A_t \cdot B_t \]

其中 \(A_t\) 是 \(t_m \times t_k\) 的瓦片，\(B_t\) 是 \(t_k \times t_n\) 的瓦片。每一步的「乘完再加到累加器上」恰好是张量核的拿手好戏 \(D = A_t \times B_t + \text{acc}\)，这正是 `ct.mma(a, b, accumulator)` 的语义。

这个结构其实就是一个 **沿 K 维的 fold（归约）**：累加器是携带值（carried value），初始为单位元 0（对应 u3-l4 里 sum 的单位元），每轮用 `mma` 把一块 \(A_t \cdot B_t\) 累加进去，循环结束得到最终瓦片。这样就把 u3-l3（循环携带值）与 u3-l4（归约单位元）在一个真实内核里兑现了。

#### 4.1.2 核心流程

```
1. 用 ct.bid 得到当前 block 的瓦片坐标 (bidx, bidy)
2. 算出 K 方向的瓦片数 num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))
3. accumulator = ct.full((tm, tn), 0, dtype=float32)   # 单位元 0，fp32 累加保精度
4. for k in range(num_tiles_k):
       a = ct.load(A, index=(bidx, k), shape=(tm, tk))   # 取 A 的一块
       b = ct.load(B, index=(k, bidy), shape=(tk, tn))   # 取 B 的一块
       accumulator = ct.mma(a, b, accumulator)           # 张量核乘累加，更新携带值
5. accumulator = ct.astype(accumulator, C.dtype)          # 收尾类型转换
6. ct.store(C, index=(bidx, bidy), tile=accumulator)      # 写回输出瓦片
```

关键点：

- `index` 是 **tile space 索引**（u2-l3、u3-l1），`(bidx, k)` 表示「第 `bidx` 个 M 瓦片、第 `k` 个 K 瓦片」，不是元素下标。
- `shape` 是编译期常量，每维必须是 2 的幂。
- 累加器即使输入是 fp16 也用 float32（`ct.full(..., dtype=ct.float32)`），这是行业惯例——累加阶段保留高精度，最后再 cast 回输出类型。

#### 4.1.3 源码精读

先看 `samples/MatMul.py` 里最核心的 K 维循环（[samples/MatMul.py:66-101](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L66-L101)）：

- [samples/MatMul.py:66](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L66)：`num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))` —— 把 A 看作按 `(tm, tk)` 分块的瓦片空间，取 K 维（axis 1）上的瓦片数。这就是循环上界。
- [samples/MatMul.py:71](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L71)：`accumulator = ct.full((tm, tn), 0, dtype=ct.float32)` —— 用单位元 0 初始化累加器，类型固定 float32。
- [samples/MatMul.py:80-93](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L80-L93)：K 维循环。每轮 `ct.load` 取 \(A_t\)、\(B_t\) 两个瓦片，再 `ct.mma(a, b, accumulator)` 累加。
- [samples/MatMul.py:97-101](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L97-L101)：收尾——cast 回 `C.dtype`，store 回输出瓦片 `(bidx, bidy)`。

`ct.num_tiles` 的语义在 stub 文档里写得很清楚：它返回「数组在给定 tile 形状下的瓦片空间里，沿指定轴的瓦片数」（[src/cuda/tile/_stub.py:1151-1156](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1151-L1156)）。文档对「瓦片空间」的定义见 [docs/source/data.rst:150-152](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/data.rst#L150-L152)：一个数组按给定 tile 形状切分后，所有瓦片构成的多维空间，瓦片索引 `(i, j)` 指第 `i+1`、`j+1` 块。

`ct.mma` 的签名与核心语义（[src/cuda/tile/_stub.py:2040-2046](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2040-L2046)）：

```python
def mma(x, y, /, acc, *, use_fast_acc: bool = False) -> Tile:
    """Matrix multiply-accumulate.
    Computes (x @ y) + acc as a single operation.
    Preserves the dtype of `acc`.
```

注意三个要点：

1. 它计算的是 \((x @ y) + acc\)，**把乘和加合成一条操作**——这正是张量核的指令形态。
2. **结果 dtype 跟随 `acc`**（`Preserves the dtype of acc`），而不是由 x、y 决定。所以累加器选 float32，整个循环就保持 float32 高精度。
3. x、y 若 dtype 不同**不会做类型提升**，而是直接广播到最后两轴（[src/cuda/tile/_stub.py:2079-2080](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2079-L2080)）。这与下一节 `matmul` 截然不同。

mma 支持的输入/累加类型组合见 docstring 的表格（[src/cuda/tile/_stub.py:2057-2077](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2057-L2077)），摘录几行：

| 输入 x/y | 累加/输出 acc |
|----------|---------------|
| f16 | f16 或 f32 |
| bf16 | f32 |
| tf32 | f32 |
| f8e4m3fn / f8e5m2 | f16 或 f32 |
| [u\|i]8 | i32 |

测试 `test_mma.py` 用最小瓦片验证了这条路径：[test/test_mma.py:21-31](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L21-L31) 加载两块 tile 与初始 acc，`acc = ct.mma(tx, ty, acc, use_fast_acc=use_fast_acc)`，再 store 回去；参考值是 `torch.mm(A, B, out_dtype=C.dtype) + C`（[test/test_mma.py:113](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L113)），正是 \((A @ B) + C\)。

#### 4.1.4 代码实践

**实践目标**：亲手跑通一次「单瓦片 mma」，确认 `mma` 的语义就是 \((x @ y) + acc\)。

**操作步骤**：

1. 阅读 [test/test_mma.py:106-117](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L106-L117) 的 `test_mma_regular_float`，它用 `(m,n,k)=(2,8,16)` 的最小瓦片。
2. 在 test 目录下用一条命令只跑这一个用例（项目用 pytest）：

   ```bash
   pytest test/test_mma.py::test_mma_regular_float -v
   ```

3. 把 `mma_kernel` 里的初始 acc 从 `ct.load(C, ...)`（C 全 1）改成注释，改用 `acc = ct.full((tm, tn), 100.0, dtype=ct.float32)`，重跑并对照参考值变成 `torch.mm(A,B) + 100`。

**需要观察的现象**：mma 结果 dtype 跟随 acc（当 acc 为 f32、x/y 为 f16 时结果仍是 f32）；改了 acc 初值后，每个输出元素都整体 +100。

**预期结果**：测试通过；手动改 acc 初值后，输出 = 矩阵乘结果 + 100。（数值正确性「待本地验证」，取决于你的 GPU 与 tileiras 版本。）

#### 4.1.5 小练习与答案

**练习 1**：为什么累加器用 `ct.float32` 而不是直接用输入的 `ct.float16`？

**参考答案**：因为 GEMM 要在 K 方向上做很多次累加，fp16 的动态范围小，多次相加会很快溢出或丢精度。用 fp32 累加可以保留高精度，最后一步再 cast 回输出类型。`mma` 的结果 dtype 跟随 acc，所以 acc 选 f32，整个循环就维持 f32。

**练习 2**：`ct.mma(a, b, accumulator)` 里的 `accumulator` 在 K 循环里扮演什么角色？

**参考答案**：它是循环的携带值（carried value，见 u3-l3）。初始为单位元 0，每轮由 `mma` 回写一个新的累加结果，循环结束时它就是最终的 \(C_\text{tile}\)。形态上等价于一个函数式 fold。

---

### 4.2 ct.matmul：不带累加的矩阵乘

#### 4.2.1 概念说明

`ct.matmul` 是 cuTile 里另一个矩阵乘 API，语义是「纯矩阵乘」，**不带累加**：`matmul(x, y)` 只算 \(x @ y\)，不会加任何 acc。它也是 Python `@` 运算符（`Tile.__matmul__`）背后的实现。

它和 `mma` 的关键区别有两条：

| 维度 | `ct.mma(x, y, acc)` | `ct.matmul(x, y)` |
|------|---------------------|--------------------|
| 是否累加 | 算 \((x@y)+acc\) | 只算 \(x@y\) |
| 输入 dtype 不同时 | **不提升**，直接广播 | **先提升**到公共 dtype |
| 结果 dtype | 跟随 `acc` | 跟随提升后的输入类型 |

什么时候用哪个？

- **GEMM 的 K 维累加循环用 `mma`**：因为每轮都要把结果加到累加器上，`mma` 一步到位、且能锁定 acc dtype。
- **一次性算出整块矩阵乘（例如已经把整条 K 一次性 load 进来）用 `matmul`**：没有累加需求时更直观，也支持 1D（向量点积）、3D（batched）等更多形状。

#### 4.2.2 核心流程

`matmul` 的内部流程可以理解为：

```
1. 若 x、y dtype 不同 → 提升到公共 dtype（遵守 u2-l4 的提升规则）
2. 对 x、y 的最后两轴做矩阵乘，前面的 batch 轴按 NumPy 广播
3. 返回结果 tile，dtype = 提升后的输入 dtype
```

注意：提升会触发 u2-l4 里那些「需要显式 cast」的限制——例如 f16 与 bf16 混合、fp8 与 f16 混合、u8 与 i8 混合，`matmul` 都会直接报错（`TileTypeError: Implicit promotion of ... is not supported`）。

#### 4.2.3 源码精读

`ct.matmul` 的签名（[src/cuda/tile/_stub.py:2254-2265](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2254-L2265)）：

```python
def matmul(x, y, /) -> Tile:
    """Performs matrix multiply on the given tiles.
    ...
    Supported input datatypes: [f16, bf16, f32, f64, tf32, f8e4m3fn, f8e5m2, i8, u8]
    If `x` and `y` have different dtype, they will first be promoted to common
    dtype. The result dtype is the same as the promoted input types.
    Shape of `x` and `y` will be broadcasted to up until the last two axes.
```

它也支持 `@` 语法糖，`Tile.__matmul__` 直接转发到 `matmul`（[src/cuda/tile/_stub.py:700-704](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L700-L704)）。

`matmul` 的多维支持很丰富：1D×1D 是点积，2D×2D 是普通矩阵乘，还支持 batched（3D×2D 等）。测试 `test_matmul_nd`（[test/test_mma.py:353-422](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L353-L422)）按 `A.ndim`/`B.ndim` 的组合分派不同的 load/store 形状，覆盖了 (1,1)、(1,2)、(2,2)、(2,3) 等组合。

类型提升的报错行为由测试钉死（[test/test_mma.py:333-339](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L333-L339) 与 [test/test_mma.py:280-295](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L280-L295)）：例如 f16×bf16 会抛 `Implicit promotion of float16 and bfloat16 is not supported`。对比 `mma`，同样的 f16×bf16 报的却是 `x and y must have the same dtype`（[test/test_mma.py:244](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L244)）——印证了「mma 不做提升、matmul 做提升」的差异。

最朴素的 `matmul` 内核形态见 [test/test_mma.py:263-271](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L263-L271)：

```python
@ct.kernel
def matmul_kernel(A, B, C, tm, tn, tk):
    tx = ct.load(A, index=(0, 0), shape=(tm, tk))
    ty = ct.load(B, index=(0, 0), shape=(tk, tn))
    acc = ct.matmul(tx, ty)          # 一次性算出 (tm,tn)，无累加
    ct.store(C, index=(0, 0), tile=acc)
```

注意它没有 `acc` 参数、没有 K 循环——因为整个 K 维已经被一次性 load 进 `tx`/`ty` 了。

#### 4.2.4 代码实践

**实践目标**：对比同一组输入下 `matmul` 与 `mma`（acc 初值为 0）结果是否一致，体会「带不带累加」的差别。

**操作步骤**：

1. 复制 [test/test_mma.py:263-271](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L263-L271) 的 `matmul_kernel`。
2. 再写一个等价的 mma 版本：把 `acc = ct.matmul(tx, ty)` 换成 `acc = ct.mma(tx, ty, ct.full((tm, tn), 0, dtype=ct.float32))`，并注意结果 dtype 会变成 f32。
3. 用同一对 f32 输入分别启动两个内核，比较输出。

**需要观察的现象**：两者数值相同（mma 的 acc=0 即无累加效果），但 `matmul` 结果 dtype 跟随输入（f32），而 mma 版结果 dtype 跟随 acc（也是 f32，本例恰好一致；若输入是 f16 则会不同）。

**预期结果**：数值一致；dtype 差异取决于输入与 acc 的组合。（「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `samples/MatMul.py` 的 K 循环里用 `ct.mma` 而不是 `ct.matmul`？

**参考答案**：因为 K 维被切成多块，每块算出的 \(A_t @ B_t\) 必须**累加**到同一个累加器上。`mma` 天然支持 \((x@y)+acc\) 且锁定 acc dtype，一步完成；用 `matmul` 的话每轮还得自己写 `accumulator = matmul(a,b) + accumulator`，多一条加法、也无法直接利用张量核的乘累加融合。

**练习 2**：`ct.matmul(f16_tile, bf16_tile)` 会发生什么？

**参考答案**：会抛 `TileTypeError: Implicit promotion of float16 and bfloat16 is not supported`。因为 matmul 会尝试把两输入提升到公共 dtype，而 f16/bf16 的隐式提升被禁止（见 u2-l4）。需要先显式 `astype` 到同一类型。

---

### 4.3 tfloat32：把 float32 喂给张量核

#### 4.3.1 概念说明

张量核的高吞吐是有代价的：它对输入数据的位宽和格式有要求。Ampere（sm_80）及以后的张量核有一条专门为 **tfloat32（tf32）** 设计的高速通道。tf32 用 32 位容器存储，但有效位只有 19 位（1 符号 + 8 指数 + 10 尾数）：

\[ \text{tf32：32 位容器，10 位尾数} \quad\text{vs}\quad \text{float32：10 位（容器），23 位尾数} \]

也就是说，tf32 牺牲了 float32 的尾数精度（23 → 10 位），换来张量核上数倍的吞吐。对于深度学习里的 GEMM，这点精度损失通常可以接受，但需要你**显式选择**——cuTile 不会悄悄把你的 float32 降成 tf32。

所以 sample 里有这一行「魔法」：

```python
dtype = ct.tfloat32 if A.dtype == ct.float32 else A.dtype
```

意思是：如果输入是 float32，就把它 cast 成 tfloat32 再喂给 `mma`，从而走张量核快通道；其他类型（如 fp16）保持原样。

#### 4.3.2 核心流程

```
1. 判断输入 dtype：若为 float32 → 目标 dtype = tfloat32；否则保持
2. load 出 tile 后立刻 .astype(dtype) 把 fp32 tile 截断成 tf32
3. 把 tf32 的 A_t、B_t 喂给 ct.mma（mma 的 tf32→f32 组合走张量核）
4. 累加器仍用 float32（mma 结果跟随 acc，保持高精度累加）
```

精度上的直觉：tf32 的 10 位尾数大致和 fp16 一个量级，所以 tf32 GEMM 的容差通常按 fp16 来设（见测试里的容差处理）。

#### 4.3.3 源码精读

sample 里的类型选择在 K 循环之前一次性确定（[samples/MatMul.py:74-75](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L74-L75)）：

```python
# Convert fp32 to tf32 to use tensorcore
dtype = ct.tfloat32 if A.dtype == ct.float32 else A.dtype
```

随后每次 load 都带 `.astype(dtype)`（[samples/MatMul.py:84](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L84) 与 [samples/MatMul.py:89](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L89)），把 fp32 tile 截成 tf32。

`tfloat32` 的定义在 [src/cuda/tile/_datatype.py:224-226](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_datatype.py#L224-L226)，它属于 `RestrictedFloat`（受限浮点）族——回忆 u2-l4：受限浮点不参与隐式算术提升，必须用 `astype` 显式 cast，这里正是这么做的。mma 的支持表里也列了 `tf32 → f32` 这一行（[src/cuda/tile/_stub.py:2070-2071](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2070-L2071)）。

测试 `test_mma_tf32`（[test/test_mma.py:34-43](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L34-L43)）就是这条路径的最小验证：load 后 `.astype(ct.tfloat32)` 再 `mma`；参考值用 `torch_to_tf32(A) @ torch_to_tf32(B)`，即先把参考矩阵也截断成 tf32 再相乘（[test/test_mma.py:171](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L171)）。注意容差分架构处理（[test/test_mma.py:174-179](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L174-L179)）：Ampere/Ada 的 tf32 数值较松（`5e-3`），更新的架构用 fp16 容差——因为 tf32 的尾数精度和 fp16 相当。

`__main__` 里同样按架构区分 fp32 GEMM 的容差（[samples/MatMul.py:297-301](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L297-L301)）：Ampere（capability ≤ 8）用 `1e-2`，Hopper 及以后用 `2e-4/1e-3`。

#### 4.3.4 代码实践

**实践目标**：直观感受 tf32 的精度损失。

**操作步骤**：

1. 跑 tf32 测试：`pytest test/test_mma.py::test_mma_tf32 -v`。
2. 在 `mma_tf32_kernel`（[test/test_mma.py:34-43](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_mma.py#L34-L43)）里，把 `.astype(ct.tfloat32)` 去掉，让 mma 直接吃 float32（mma 支持 f32→f32）。
3. 比较两种情况下输出与「真值」`torch.mm(A,B)` 的最大绝对误差。

**需要观察的现象**：去掉 tf32（直接 f32）时误差更小；用 tf32 时误差在 1e-3 量级（尾数被截断）。

**预期结果**：tf32 版本误差明显大于 f32 版本，但仍落在测试容差内。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么是「float32 cast 成 tfloat32」，而不是反过来？

**参考答案**：因为输入数据本来是高精度 float32（23 位尾数）。要利用张量核的 tf32 快通道，必须主动**降低**精度到 tf32（10 位尾数），这一步会丢信息，所以必须由程序员显式选择。cuTile 不会替你悄悄降精度。反向（tf32→f32）只是把已经损失的精度装进更宽的容器，并不能恢复信息。

**练习 2**：tf32 属于 u2-l4 里的哪一类型族？这意味着什么？

**参考答案**：tf32 属于 `RestrictedFloat`（受限浮点）族。受限浮点不参与隐式算术提升，两个 tf32 tile 不能直接相加（会报错要求显式 cast），必须用 `astype` 显式转换——这正好解释了 sample 里为什么用 `.astype(ct.tfloat32)` 而不是依赖自动提升。

---

### 4.4 swizzle 与 persistent kernel：调度优化

理解了「怎么算对」（4.1–4.3），本模块讲「怎么算快」。`samples/MatMul.py` 里给了两套调度技巧：**swizzle（瓦片重排）** 和 **persistent kernel（持久内核）**。它们都不改变数学结果，只改变 block 与输出瓦片的映射方式，用来压榨 L2 缓存命中率和减少启动开销。

#### 4.4.1 概念说明

**swizzle（瓦片重排）**：朴素做法是让 1D 的 block 编号线性映射到 2D 输出瓦片 `(bid_m, bid_n)`（例如 `bid_m = bid // num_bid_n; bid_n = bid % num_bid_n`），这样相邻 block 沿 N 方向推进，会同时读 A 的同一块行、却读 B 的不同列块——B 的复用很差。swizzle 改成「分组优先」：把若干连续的 M 行（`GROUP_SIZE_M`）组成一组，先让相邻 block 把这一组里所有 N 列算完，再前进到下一组。这样相邻 block 共享 A 的同一片行，对 L2 缓存更友好。

**persistent kernel（持久内核）**：朴素 GEMM 里 block 数 = 输出瓦片数（可能成千上万），每个 block 只算一块就退出。persistent 改成只启动 `NUM_SMS`（SM 数量）个 block，每个 block 在一个循环里**串行处理多个输出瓦片**。好处是省去了海量 block 的调度/启动开销，并让编译器在瓦片之间复用寄存器与软流水。

#### 4.4.2 核心流程

**swizzle 映射**（[samples/MatMul.py:14-24](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L14-L24)）把一个 1D 的 `bid` 重排成 2D 的 `(bid_m, bid_n)`：

```
num_bid_m = cdiv(M, tm);  num_bid_n = cdiv(N, tn)
num_bid_in_group = GROUP_SIZE_M * num_bid_n
group_id      = bid // num_bid_in_group
first_bid_m   = group_id * GROUP_SIZE_M
group_size_m  = min(num_bid_m - first_bid_m, GROUP_SIZE_M)
bid_m = first_bid_m + (bid % group_size_m)
bid_n = (bid % num_bid_in_group) // group_size_m
```

直觉：在同一组内，相邻 `bid` 只移动 `bid_n`（沿 N 走），跨过 `group_size_m` 行才换组——从而让相邻 block 聚集在同一片 M 行上，提升 A 的 L2 复用。

**persistent 循环**（[samples/MatMul.py:148-149](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L148-L149)）：

```
num_tile_blocks = ct.num_blocks(0)            # = 实际启动的 block 数（grid_size）
for current_bid in range(bid, upper_bound, num_tile_blocks):
    ...处理第 current_bid 个输出瓦片...
```

每个 block 从自己的 `bid` 出发，以 `num_tile_blocks` 为步长跳着处理输出瓦片，直到超过总瓦片数 `upper_bound = num_bid_m * num_bid_n`。host 端把 grid 设成 `min(NUM_SMS, total_tiles)`（[samples/MatMul.py:233-238](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L233-L238)），让启动的 block 数刚好铺满 SM。

注意 `num_blocks(0)` 是 tile code 内取 grid 尺寸的内置操作（[src/cuda/tile/_stub.py:1117-1124](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1117-L1124)），与 `ct.bid`（u3-l1）配对——一个取「我是第几块」，一个取「总共有几块」。

#### 4.4.3 源码精读

- **swizzle 函数族**：`swizzle_2d_from_bid`（[samples/MatMul.py:14-24](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L14-L24)）接受任意 `bid`；`swizzle_2d`（[samples/MatMul.py:27-30](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L27-L30)）封装成「取当前 block 的 `ct.bid(0)` 再调用前者」。两个 kernel 都用它把 1D bid 拆成 2D 瓦片坐标。
- **朴素 kernel** `matmul_kernel`（[samples/MatMul.py:33-101](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L33-L101)）：用 `swizzle_2d` 拿到 `(bidx, bidy)`，跑一次 K 循环算一块输出瓦片。注意装饰器 `@ct.kernel(num_ctas=ct.ByTarget(sm_100=2))`（[samples/MatMul.py:33](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L33)）：`num_ctas` 是 thread block cluster 大小（Hopper+ 的集群启动），`ByTarget(sm_100=2)` 表示仅在 sm_100（Hopper）上设为 2，其它架构保持默认——这是一种架构特化（见 [src/cuda/tile/_compiler_options.py:18](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compiler_options.py#L18)）。
- **persistent kernel** `persistent_matmul_kernel`（[samples/MatMul.py:104-176](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L104-L176)）：核心差别是外层多了一个 `for current_bid in range(bid, upper_bound, num_tile_blocks)`（[samples/MatMul.py:149](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L149)），每个 block 在循环里反复「初始化累加器 → 跑 K 循环 → store 一块」，处理多个输出瓦片。累加器在每轮 `current_bid` 开头重新 `ct.full` 归零（[samples/MatMul.py:153](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L153)）。
- **host 端封装** `cutile_matmul`（[samples/MatMul.py:179-251](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L179-L251)）：
  - 按 dtype 选 tile 尺寸（[samples/MatMul.py:215-218](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L215-L218)）：fp16/bf16（itemsize=2）用 `(128,256,64)` 的大瓦片以贴合张量核；float32 用 `(32,32,32)`。
  - 算 grid（[samples/MatMul.py:230-232](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L230-L232)）：`grid_x * grid_y` 个 block。
  - persistent 时把 grid 截到 SM 数（[samples/MatMul.py:233-237](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L233-L237)）：`grid_size = min(NUM_SMS, grid_size)`。
  - 启动（[samples/MatMul.py:248-249](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L248-L249)）：`tm/tn/tk` 作为 `Constant` 传入（见 u3-l5，常量嵌入）。

#### 4.4.4 代码实践

**实践目标**：感受 persistent 与朴素两种形态在结果上等价、在调度上不同。

**操作步骤**：

1. 直接运行 sample 的主程序（带正确性检查）：

   ```bash
   python samples/MatMul.py --correctness-check
   ```

2. 阅读输出里 Test Case 4（persistent，[samples/MatMul.py:333-342](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/samples/MatMul.py#L333-L342)），确认它与朴素版本（Test Case 2）结果一致。
3. 在 `cutile_matmul` 里打印 `grid_size`：分别看 `persistent=False` 与 `persistent=True` 时启动了多少 block。

**需要观察的现象**：persistent 的 grid_size 远小于朴素版（被截到 NUM_SMS，例如几十到一百多），而朴素版等于输出瓦片总数（对 512×512、tn=256 可达个位数×个位数；对大矩阵可达上千）。两者结果都通过 `torch.testing.assert_close`。

**预期结果**：两种形态数值一致；persistent 启动的 block 数 = SM 数。具体 SM 数「待本地验证」（取决于显卡）。

#### 4.4.5 小练习与答案

**练习 1**：把 `swizzle_2d_from_bid` 换成最朴素的线性映射 `bid_m = bid // num_bid_n; bid_n = bid % num_bid_n`，结果会对吗？性能会变好吗？

**参考答案**：结果仍然正确——swizzle 只是重排了 block 与瓦片的映射，不改变每个瓦片算出的值。但性能通常会变差：线性映射让相邻 block 沿 N 方向推进，B 的列块复用变差、L2 命中率下降。这正是 swizzle 要解决的问题。

**练习 2**：persistent kernel 里为什么要在每个 `current_bid` 开头重新 `ct.full` 归零累加器？

**参考答案**：因为同一个 block 要串行处理多个**不同**的输出瓦片，每个输出瓦片的累加都必须从单位元 0 开始（否则会把上一个瓦片的部分和带进来，算错）。累加器只在单次 K 循环内是携带值，跨输出瓦片必须重置。

---

## 5. 综合实践

把本讲全部内容串起来，亲手实现一个最小 float16 GEMM 并验证。

**任务**：基于 `samples/MatMul.py`，写一个**去掉 swizzle、不用 persistent** 的最朴素分块 GEMM，输入输出为 `torch.float16`，`tm/tn/tk` 用 `Constant[int]` 传入，最后与 `torch.matmul`（即 `A @ B`）做数值一致性校验。

**示例代码**（基于 sample 简化，仅作教学）：

```python
# 示例代码：最小 float16 分块 GEMM
import cuda.tile as ct
import torch
from math import ceil

ConstInt = ct.Constant[int]

@ct.kernel
def my_matmul(A, B, C, tm: ConstInt, tn: ConstInt, tk: ConstInt):
    bidx = ct.bid(0)      # 用 1D grid，先不 swizzle
    # 这里用 1D grid 串联 2D 瓦片：朴素线性映射
    num_bid_n = ct.cdiv(B.shape[1], tn)
    bid_m = bidx // num_bid_n
    bid_n = bidx % num_bid_n

    num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))
    acc = ct.full((tm, tn), 0, dtype=ct.float32)      # fp32 累加保精度
    zero_pad = ct.PaddingMode.ZERO
    for k in range(num_tiles_k):
        a = ct.load(A, index=(bid_m, k),   shape=(tm, tk), padding_mode=zero_pad)
        b = ct.load(B, index=(k,   bid_n), shape=(tk, tn), padding_mode=zero_pad)
        acc = ct.mma(a, b, acc)                         # 张量核乘累加
    acc = ct.astype(acc, C.dtype)                       # 收尾 cast 回 fp16
    ct.store(C, index=(bid_m, bid_n), tile=acc)

def run():
    M, N, K = 512, 512, 256
    tm, tn, tk = 128, 128, 64                           # fp16 用较大瓦片
    A = torch.randn(M, K, dtype=torch.float16, device="cuda")
    B = torch.randn(K, N, dtype=torch.float16, device="cuda")
    C = torch.empty(M, N, dtype=torch.float16, device="cuda")

    grid = (ceil(M / tm) * ceil(N / tn), 1, 1)
    ct.launch(torch.cuda.current_stream(), grid, my_matmul,
              (A, B, C, tm, tn, tk))

    ref = A @ B
    torch.testing.assert_close(C, ref, atol=1e-2, rtol=1e-3)  # fp16 容差
    print("GEMM correctness check passed")

if __name__ == "__main__":
    run()
```

**操作步骤**：

1. 把上面的代码存成 `mini_fp16_gemm.py`（放在仓库任意可 `import cuda.tile` 的位置，例如已 `pip install -e .` 的环境里）。
2. 运行 `python mini_fp16_gemm.py`。
3. 进阶：(a) 把 `tm/tn/tk` 换成 `(32,32,32)`，观察是否仍正确、是否变慢；(b) 把累加器 dtype 从 `ct.float32` 改成 `ct.float16`，观察精度是否还能通过容差；(c) 套上 4.4 的 swizzle，对比是否更快。

**需要观察的现象与预期结果**：

- 默认配置应通过 `assert_close`（fp16 GEMM 在 1e-2 容差内与 torch 一致）。
- 累加器改 fp16 后，大概率**不再**通过容差——印证「累加阶段必须用高精度」。
- 套上 swizzle 后，正确性不变，大矩阵下应观察到加速（可用 `torch.cuda.Event` 计时）。

所有具体计时与通过/不通过的数值都「待本地验证」，取决于你的 GPU 架构与 tileiras 版本。

## 6. 本讲小结

- 分块 GEMM 的结构是「沿 K 维循环、每轮用 `ct.mma(a, b, acc)` 把一块 \(A_t @ B_t\) 累加进 float32 累加器」——累加器是循环携带值、初值为单位元 0，本质是 u3-l3/u3-l4 的 fold。
- `ct.mma` 计算 \((x@y)+acc\)、结果 dtype 跟随 `acc`、且**不做**输入类型提升；它是 K 维累加循环的不二选择，映射到张量核。
- `ct.matmul` 只算 \(x@y\)、**会**做类型提升、结果 dtype 跟随提升后的输入，支持 1D/2D/3D；适合一次性整块矩阵乘、向量点积与 batched 场景。
- float32 输入要先 `astype(ct.tfloat32)` 才能走张量核的 tf32 快通道；tf32 是 10 位尾数的受限浮点，精度与 fp16 相当，需按架构设容差。
- **swizzle** 重排 block→瓦片映射以提升 L2 复用；**persistent kernel** 用 `num_blocks` 个 block 串行处理多个瓦片以减少调度开销；两者都不改变数值结果。
- host 端按 dtype 选 tile 尺寸（fp16 用大瓦片）、按 `cdiv` 算 grid、把 `tm/tn/tk` 作为 `Constant` 嵌入内核。

## 7. 下一步学习建议

- **横向**：把本讲的 GEMM 内核当作 u8-l3（自动调优）的标的——用 `ct.tune.exhaustive_search` 在若干 `(tm,tn,tk)` 组合里搜索最优配置，体会「tile 尺寸选多大」其实是个调参问题。
- **纵向（前端）**：本讲只用了 `mma`/`matmul`/`load`/`store` 这些 stub。若想知道 `ct.mma` 是如何被注册、分派到具体张量核 IR 实现的，进入 u5-l7（Stub 与实现注册）。
- **纵向（后端）**：若想看 K 循环、mma 在字节码与 cubin 阶段如何被编码，参考 u7-l1（IR 到字节码）与 u7-l3（tileiras 与 cubin 生成）。
- **扩展**：`mma_scaled`（块缩放矩阵乘，[src/cuda/tile/_stub.py:2119](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L2119)）是 fp8/fp4 时代的重要变体，建议在掌握本讲后阅读其文档与对应测试。
