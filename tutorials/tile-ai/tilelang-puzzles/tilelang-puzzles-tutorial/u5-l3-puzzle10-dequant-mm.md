# Puzzle 10 Dequant MM：INT4 量化矩阵乘

## 1. 本讲目标

本讲是手册的收官之作，对应仓库里最后一个、也是唯一标注为 `hard` 的 Puzzle 10。学完后你应当能够：

- 说清 **INT4 量化** 为什么能把权重显存压缩到原来的 1/4，以及两个 INT4 如何**打包进一个 `uint8` 字节**。
- 写出**解量化**公式 \(\hat{w}=q-8\)，并解释它为何等价于 `scale=1, zero_point=8` 的仿射量化特例，而非有符号补码 int4。
- 在 TileLang 里用位运算 `& 0x0F` / `>> 4` 配合 `T.cast` 把 `uint8` 解包成 `float16`。
- 把「解包 + 反量化」**融合**进一个 `T.Pipelined` 软件流水线 GEMM kernel，让反量化的额外开销被访存/计算重叠所掩盖。

本讲不引入新的算子语义，而是把 **u4-l4 的共享内存 GEMM + 软件流水线**这条骨架，嫁接上一个真实的工程需求：权重以压缩格式存储、在 kernel 内部即时解压再相乘。这正是 GPTQ、AWQ、llama.cpp 等 LLM 推理框架的核心算子。

## 2. 前置知识

本讲建立在 **u4-l4（GEMM 优化：共享内存与软件流水线）** 之上，下述概念默认你已掌握，这里只做一句话回顾：

- **三级内存**：global（显存，大而慢）→ shared（block 内共享）→ fragment（寄存器，快而小）。性能工程的目标是让数据尽量待在快的存储里。
- **`T.gemm`**：封装了 Tensor Core 的 MMA 指令，语义为 `C += A @ B`，以 **shared memory 为高效输入来源**，累加器 `C_local` 用 fragment。
- **`T.Pipelined(K // BLOCK_K, num_stages=3)`**：替换 `T.Serial`，让「搬运下一段 tile」与「计算当前段」重叠，用计算掩盖访存延迟。
- **三件套**：`test_puzzle`（正确性）、`compile().print_source_code()`（生成代码检视）、`bench_puzzle`（性能计时）。

此外需要一点点**位运算**直觉：按位与 `&`、右移 `>>`、十六进制掩码 `0x0F = 00001111₂`。一个字节（byte）= 8 位（bit），一个 nibble = 4 位，所以一个字节正好装两个 nibble，也就是两个 INT4。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [puzzles/10-dequant-mm.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/10-dequant-mm.py) | 题目：含 `ref_dequant_matmul` 参考实现、`tl_dequant_matmul` 带 TODO 的骨架、`run_dequant_matmul` 运行入口。 |
| [ans/10-dequant-mm.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py) | 参考答案：完整实现了融合解量化 + `T.gemm` 的软件流水线 kernel。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle` / `bench_puzzle` 框架；`rand_torch_tensor` 说明 `uint8` 输入如何被随机生成。 |
| [ans/08-matrix.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py) | 对照基线：纯 FP16 的 `tl_matmul_opt`，本讲的 kernel 正是它「换上压缩 B」的变体。 |

> 说明：题目与答案文件的内容除 TODO 外几乎逐行相同（含相同的 `ref_dequant_matmul` 与 `run_dequant_matmul`），因此下文引用代码时主要指向 `ans/`，骨架行号在 `puzzles/` 中位置一致。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先理解**为什么要打包、怎么还原**（4.1），再看**单个字节如何用位运算拆成两个浮点数**（4.2），最后把整件事**塞进 GEMM 流水线**（4.3）。

### 4.1 INT4 / uint8 打包与有符号解量化

#### 4.1.1 概念说明

大语言模型（LLM）推理时，瓶颈往往是**显存带宽**而不是算力：权重太大，从显存搬到 SM 的速度跟不上计算单元的胃口。量化（quantization）就是把权重从 16 位浮点（FP16）压到更低位整数来减小体积。

**INT4 量化**把每个权重压到 4 位整数。但 4 位不是一个可寻址的存储单位——GPU 最小的访存粒度是字节（8 位）。于是我们**把两个 INT4 打包进一个 `uint8`**：

```
一个 uint8 字节 = 8 bit = 高 nibble (bit 7:4) + 低 nibble (bit 3:0)
                        = B_high           + B_low
```

这样一来，权重的存储体积从 `K×N×2` 字节（FP16）降到 `K×(N/2)×1` 字节（uint8），正好是原来的 **1/4**，节省 75% 的权重显存与带宽。

但 4 位整数是无符号的 nibble，取值范围是 \([0, 15]\)，而权重应当是**有符号**的、以 0 为中心的小整数。本 puzzle 采用最简单的**偏移还原**：

\[
\hat{w} = q - 8, \qquad q \in [0,15] \;\Rightarrow\; \hat{w} \in [-8, 7]
\]

即把每个 nibble \(q\) 减去 8，映射到 \([-8, 7]\)。这正是通用**仿射反量化**公式

\[
\text{real\_weight} = \text{scale} \cdot (q - \text{zero\_point})
\]

在 \(\text{scale}=1,\ \text{zero\_point}=8\) 时的特例。项目文档明确指出：这**不是**所有 INT4 量化格式的统一定义，而是本 puzzle 的教学版规则；真实 GPTQ/AWQ 还会引入 per-channel / per-group 的 `scale` 与显式 `zero_point`。

> ⚠️ 一个容易踩的坑：\([-8, 7]\) 这个范围恰好和「4 位二补码有符号整数」相同，但**码点到值的映射完全不同**。二补码下 nibble `8`（1000₂）= −8、nibble `0` = 0；而本 puzzle 的 `q−8` 下 nibble `8` = 0、nibble `0` = −8。两者只是「碰巧同区间」。题目参考实现用的是 `q−8`，你的 kernel 必须与之匹配，否则 `torch.allclose` 会失败。

#### 4.1.2 核心流程

整个反量化矩阵乘的数据流，可以用三段描述：

1. **加载（load）**：从显存把一段 A tile（FP16）和一段**打包的** B tile（uint8，列数只有目标列数的一半）搬进共享内存。
2. **解包 + 反量化（dequant）**：在共享内存里，把每个 uint8 字节拆成两个 nibble，各自减 8、转 FP16，写进一个「解包后」的 B 缓冲（列数恢复为完整宽度）。
3. **矩阵乘（gemm）**：用解包后的 B 与 A 做 `T.gemm`，累加进 FP32 累加器。

用伪代码概括单段 tile 的处理：

```
for k in 分块(K):
    A_shared  <- global A      # FP16, [BLOCK_M, BLOCK_K]
    B_shared  <- global B      # uint8, [BLOCK_K, BLOCK_N//2]   ← 只有半宽
    # dequant: 每个 uint8 拆 2 个 fp16
    for (i, j) in Parallel(BLOCK_K, BLOCK_N//2):
        B_dequant[i, j*2]   = (B_shared[i,j] & 0x0F)       as fp16 - 8
        B_dequant[i, j*2+1] = ((B_shared[i,j] >> 4) & 0x0F) as fp16 - 8
    C_local += A_shared @ B_dequant   # T.gemm, FP32 累加
```

注意循环里 j 遍历的是**打包后的半宽** `BLOCK_N//2`，而写入时用 `j*2` 与 `j*2+1` 把它「展开」回完整宽度 `BLOCK_N`。这正是「打包使列数减半」在索引上的直接体现。

#### 4.1.3 源码精读

先看题目给的**参考实现**，它定义了「正确答案」长什么样：

[puzzles/10-dequant-mm.py:55-70](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/10-dequant-mm.py#L55-L70) —— `ref_dequant_matmul`。关键三行：

```python
B_dequantized[:, ::2] = B[:, :] & 0x0F            # 偶数列 = 低 nibble
B_dequantized[:, 1::2] = (B[:, :] >> 4) & 0x0F    # 奇数列 = 高 nibble
B_dequantized = B_dequantized.to(torch.float16) - 8.0   # 偏移还原到 [-8,7]
```

这段代码确认了三件事：①低 nibble 进偶数列、高 nibble 进奇数列（**交错排布**）；②还原统一是 `q − 8`；③还原发生在转成 FP16 **之后**。你的 kernel 必须复刻这个语义，只是把 `torch.matmul` 换成 `T.gemm`、把全局解包换成「按 tile 在共享内存里即时解包」。

再看 kernel 的**形状声明**，这是本 puzzle 与普通 GEMM 最显眼的差别：

[puzzles/10-dequant-mm.py:73-85](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/10-dequant-mm.py#L73-L85)（答案在 [ans/10-dequant-mm.py:73-81](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L73-L81)）

```python
A: T.Tensor((M, K), A_dtype)              # A_dtype = float16
B: T.Tensor((K, N // 2), B_storage_dtype) # B_storage_dtype = uint8，注意列数是 N//2
C = T.empty((M, N), A_dtype)              # 输出仍是完整 [M, N] 的 FP16
accum_dtype = T.float32
```

要点：B 的第二维是 `N // 2` 而不是 `N`，因为它存的是**打包后的权重**；输出 C 的维度却恢复为 `N`，因为解包后每个字节贡献两个输出列。`accum_dtype` 用 FP32 保累加精度（回忆 u4-l2/u4-l3：FP16 有效位不足以支撑 K=4096 的长求和）。

#### 4.1.4 代码实践

**实践目标**：用一个极小的 Python 脚本，亲手验证「打包 → 解包 → 还原」的数值关系，建立对 `q−8` 映射的直觉，不依赖 GPU。

**操作步骤**（在仓库根目录运行）：

```bash
python3 -c "
b = 0x6D          # 一个打包字节
low  = b & 0x0F           # 低 nibble
high = (b >> 4) & 0x0F    # 高 nibble
print('low  nibble =', low,  '-> dequant', low  - 8)
print('high nibble =', high, '-> dequant', high - 8)
"
```

**需要观察的现象**：输出应为 `low nibble = 13 -> dequant 5` 与 `high nibble = 6 -> dequant -2`。

**预期结果**：`0x6D = 0110_1101₂`，低 nibble `1101₂ = 13`，还原 `13−8 = 5`；高 nibble `0110₂ = 6`，还原 `6−8 = −2`。这正是项目文档给出的示例字节。

**对比练习（可选）**：把还原公式改成「二补码有符号 int4」（`v-16 if v>=8 else v`），重新算同一个字节，会得到 `low→-3, high→6`，与本 puzzle 的参考结果不同——以此体会两种编码的差别。

#### 4.1.5 小练习与答案

**练习 1**：给定打包字节 `B[k,j] = 0xAB`，求还原后的 `B_low` 与 `B_high`。
**答案**：`0xAB & 0x0F = 0x0B = 11`，还原 `11−8 = 3`；`(0xAB>>4)&0x0F = 0x0A = 10`，还原 `10−8 = 2`。

**练习 2**：`q−8` 与「4 位二补码有符号整数」的范围都是 \([-8,7]\)，为什么本 puzzle 不能用二补码？
**答案**：范围相同只是巧合，码点映射不同。二补码下 nibble `8` = −8、nibble `0` = 0；而 `q−8` 下 nibble `8` = 0、nibble `0` = −8。参考实现 `ref_dequant_matmul` 用的是 `q−8`（`−8.0`），kernel 必须与之匹配，否则 `torch.allclose` 失败。

**练习 3**：如果把 `−8` 误写成 `+8`，`test_puzzle` 会怎样？
**答案**：还原值整体落在 \([8, 23]\)，与参考结果存在系统性偏移，`torch.allclose`（atol=rtol=1e-2）必然返回 `❌`。

---

### 4.2 位运算与 T.cast

#### 4.2.1 概念说明

上节讲清了「为什么」，本节讲「单个字节怎么拆」。核心是两条位运算 + 一次类型转换：

- **提取低 nibble**：`B & 0x0F`。`0x0F = 00001111₂`，按位与会把高 4 位清零、保留低 4 位。
- **提取高 nibble**：`(B >> 4) & 0x0F`。先右移 4 位把高 nibble 挪到低位，再按位与兜底。
- **类型转换**：`T.cast(x, dtype)` 把整数结果转成 FP16，随后 `− 8.0` 在浮点域完成。

一个细节：对 `uint8` 而言，右移 4 位后最大值是 `0xFF >> 4 = 15`，已经天然落在低 4 位，所以 `(B >> 4) & 0x0F` 里的 `& 0x0F` **其实是多余的**。保留它属于防御性写法（万一存储类型更宽也不会出错），并且与参考实现逐字一致——这种「冗余但安全」的对称写法在工程代码里很常见。

`T.cast(x, dtype)` 与早期 puzzle（如 GEMV）里见到的 `x.astype(dtype)` 是**同一件事的两种写法**，都把一个标量/张量在指定 dtype 间转换。这里之所以必须先 `cast` 再减 8：`B_shared` 是 `uint8`，若直接做整数减法得到的是整数，无法直接喂给 FP16 的 `T.gemm`；先 `T.cast` 成 FP16，`− 8.0` 就在浮点域完成，结果自然落进 FP16 的 `B_dequantized`，与参考 `.to(float16) − 8.0` 语义一致。

#### 4.2.2 核心流程

单个打包字节 `B_shared[i, j]` 解成两个 FP16 值的过程：

\[
\begin{aligned}
q_{\text{low}}  &= B \mathbin{\&} \text{0x0F}, & \hat{w}_{\text{low}}  &= \text{cast}(q_{\text{low}},\, \text{fp16}) - 8 \\
q_{\text{high}} &= (B \gg 4) \mathbin{\&} \text{0x0F}, & \hat{w}_{\text{high}} &= \text{cast}(q_{\text{high}},\, \text{fp16}) - 8
\end{aligned}
\]

两个结果分别写入 `B_dequantized[i, j*2]` 与 `B_dequantized[i, j*2+1]`，即一个打包列展开成相邻的两个输出列（偶数列装 low、奇数列装 high）。这正是参考实现里 `[:, ::2]` / `[:, 1::2]` 切片在 kernel 层的等价物。

#### 4.2.3 源码精读

答案 kernel 里这段解包代码是本节的全部核心：

[ans/10-dequant-mm.py:98-100](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L98-L100)

```python
for i, j in T.Parallel(BLOCK_K, BLOCK_N // 2):              # 遍历打包后的半宽
    B_dequantized[i, j * 2]     = T.cast(B_shared[i, j] & 0x0F,        A_dtype) - 8.0
    B_dequantized[i, j * 2 + 1] = T.cast((B_shared[i, j] >> 4) & 0x0F, A_dtype) - 8.0
```

要点逐条对照：

1. **`T.Parallel(BLOCK_K, BLOCK_N // 2)`**：迭代空间是 `(K, N//2)`，对应 `B_shared` 的打包形状，**不是** `(K, N)`。框架把这个二维迭代空间并行划分给 block 内的线程（回忆 u2-l1：`T.Parallel` 只声明迭代次数与可并行性，线程分摊由框架完成）。
2. **`j * 2` / `j * 2 + 1`**：把半宽索引 `j` 展开成全宽的两个相邻列——一个字节贡献两个输出列。
3. **`T.cast(..., A_dtype) − 8.0`**：先转 FP16 再减 8，结果直接是 FP16 写进 `B_dequantized`。
4. **两行的对称性**：低 nibble 写偶数列、高 nibble 写奇数列，与 `ref_dequant_matmul` 的 `[:, ::2]` / `[:, 1::2]` 完全对应。

#### 4.2.4 代码实践

**实践目标**：用 PyTorch 复现「交错展开」，确认 kernel 里的 `j*2` / `j*2+1` 写法与参考切片一一对应。

**操作步骤**（在仓库根目录运行，CPU 即可）：

```bash
python3 -c "
import torch
# 构造一个 1x2 的打包 B（两个字节），随便取值
B = torch.tensor([[0x6D, 0xAB]], dtype=torch.uint8)
K, Nh = B.shape
N = Nh * 2
out = torch.zeros((K, N), dtype=torch.float16)
out[:, ::2]  = B & 0x0F              # 偶数列 = low
out[:, 1::2] = (B >> 4) & 0x0F       # 奇数列 = high
print('解包后（未还原）:', out)
print('还原后(-8):', (out.to(torch.float16) - 8))
"
```

**需要观察的现象**：`out` 应为 `[[13, 6, 11, 10]]`（4 列，由 2 个字节展开而来），还原后为 `[[5, -2, 3, 2]]`。

**预期结果**：第 0 字节 `0x6D` 贡献 `(13, 6)`、第 1 字节 `0xAB` 贡献 `(11, 10)`，恰好交错排布。这验证了「一个打包列 → 两个相邻输出列」的索引关系，与 kernel 里 `j*2` / `j*2+1` 完全一致。

**待本地验证**：以上为 CPU 上的纯数值推演，行为可确定；若要在 GPU 上验证 kernel 本身，请见 4.3.4。

#### 4.2.5 小练习与答案

**练习 1**：对 `uint8`，`(B >> 4) & 0x0F` 中的 `& 0x0F` 是否多余？为什么答案仍保留它？
**答案**：对 `uint8` 是多余的——右移 4 位后最大值 `255>>4 = 15`，已落在低 4 位。保留它是防御性写法（存储类型更宽时也不会出错），且与参考实现逐字一致。

**练习 2**：为什么是 `T.cast(...) − 8.0`（先 cast 再减），而不是先在整数上减 8？
**答案**：`B_shared` 是 `uint8`，整数减 8 得整数，无法直接参与 FP16 的 `T.gemm`。先 `T.cast` 成 FP16，`− 8.0` 在浮点域完成，结果自然是 FP16 落进 `B_dequantized`，与参考 `.to(float16) − 8.0` 语义一致；同时也保证 nibble `0..15` 在减 8 后能正确得到负数（uint8 减法会下溢）。

**练习 3**：`T.Parallel(BLOCK_K, BLOCK_N // 2)` 的第二个维度为什么不是 `BLOCK_N`？
**答案**：因为遍历的是**打包后**的 `B_shared`，其列数只有 `BLOCK_N // 2`（每列含两个 nibble）。`BLOCK_N` 是**展开后**的输出宽度，通过 `j*2` / `j*2+1` 在写入时才体现。

---

### 4.3 融合解量化 + GEMM 的流水线 kernel

#### 4.3.1 概念说明

如果按朴素思路，反量化矩阵乘要分两步：先跑一个 kernel 把整个 B 解包成 FP16 矩阵，再跑一个普通 GEMM。这样会多一次「写回完整 FP16 的 B 到显存、再读进来」的昂贵往返——而压缩 B 的全部意义就在于少搬数据，岂能解包后又原样放大写回？

正确做法是**融合（fusion）**：把解包 + 反量化**塞进 GEMM 的 K 维流水线循环内部**，每搬进一段打包的 B tile，就在共享内存里**就地**解包，紧接着喂给 `T.gemm`。解包后的 FP16 B 只存在于 shared memory，从不落回显存。这就是本 puzzle 相对普通 GEMM 的唯一新增逻辑，也是它在真实推理框架里的价值所在。

这里有一个值得注意的内存层级选择（承接 u4-l4）：解包后的 `B_dequantized` 用 `T.alloc_shared` 而非 `T.alloc_fragment`。原因是它是 `T.gemm`（Tensor Core MMA）的**输入**之一，而 MMA 以共享内存为高效输入来源；只有高频读写的累加器 `C_local` 才留在 fragment。整张内存分配如下：

| 缓冲 | 分配方式 | dtype | 形状 | 角色 |
|------|----------|-------|------|------|
| `A_shared` | shared | fp16 | `[BLOCK_M, BLOCK_K]` | GEMM 输入 A |
| `B_shared` | shared | **uint8** | `[BLOCK_K, BLOCK_N//2]` | 打包的 B（压缩态） |
| `B_dequantized` | shared | fp16 | `[BLOCK_K, BLOCK_N]` | 解包后的 B（GEMM 输入） |
| `C_local` | **fragment** | fp32 | `[BLOCK_M, BLOCK_N]` | 累加器（高频读写） |

注意 `B_shared` 与 `B_dequantized` 一压一展：前者只有半宽、1 字节/元素；后者全宽、2 字节/元素。它们都活在 shared memory 里，但生命周期错开——`B_shared` 在解包后即可被下一段覆盖，`B_dequantized` 仅在当段 `T.gemm` 期间被读。

#### 4.3.2 核心流程

整个 kernel 是「二维分块 + K 维流水线」的标准 GEMM 骨架，唯一差别是 B 的加载与使用之间插了一个解包步骤：

```
with T.Kernel(ceildiv(M,BLOCK_M), ceildiv(N,BLOCK_N)) as (pid_m, pid_n):
    分配 A_shared, B_shared(uint8), B_dequantized, C_local
    T.clear(C_local)
    for k in T.Pipelined(ceildiv(K, BLOCK_K), num_stages=3):   # 软件流水线
        T.copy(A[pid_m*BLOCK_M, k*BLOCK_K],          A_shared)   # 搬 A
        T.copy(B[k*BLOCK_K, pid_n*BLOCK_N//2],       B_shared)   # 搬【打包】B
        for i,j in T.Parallel(BLOCK_K, BLOCK_N//2):             # 就地解包
            B_dequantized[i, j*2]   = cast(B_shared[i,j] & 0x0F,        fp16) - 8
            B_dequantized[i, j*2+1] = cast((B_shared[i,j] >> 4) & 0x0F, fp16) - 8
        T.gemm(A_shared, B_dequantized, C_local)                # FP32 累加
    T.copy(C_local, C[pid_m*BLOCK_M, pid_n*BLOCK_N])           # 写回
```

为什么解包能被流水线「免费」掩盖？`T.Pipelined` 的稳态里，「加载下一段 A/B」「解包当前段 B」「`T.gemm` 当前段」三件事被编译器调度成重叠执行（prologue → 稳态 → epilogue 三段，回忆 u4-l4）。解包只是一些 CUDA Core 上的位运算与算术，恰好填补 `T.gemm`（跑在 Tensor Core 上）留下的空隙——计算单元与访存单元本来就不冲突，融合进来几乎不增加关键路径。

#### 4.3.3 源码精读

逐段拆答案 kernel。

**启动配置与内存分配**：

[ans/10-dequant-mm.py:84-93](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L84-L93)

```python
with T.Kernel(T.ceildiv(M, BLOCK_M), T.ceildiv(N, BLOCK_N), threads=128) as (pid_m, pid_n):
    A_shared      = T.alloc_shared((BLOCK_M, BLOCK_K), A_dtype)
    B_shared      = T.alloc_shared((BLOCK_K, BLOCK_N // 2), B_storage_dtype)  # uint8, 半宽
    B_dequantized = T.alloc_shared((BLOCK_K, BLOCK_N), A_dtype)               # fp16, 全宽
    C_local       = T.alloc_fragment((BLOCK_M, BLOCK_N), accum_dtype)         # fp32 累加器

    T.clear(C_local)
```

和 u4-l4 的 `tl_matmul_opt` 比，这里多了一个 `B_dequantized`，而 `B_shared` 的 dtype 换成了 `uint8`、列数减半。`pid_m`/`pid_n` 是二维分块的块索引（M、N 并行，K 串行累加），`threads=128` 与 GEMM 一致。

**K 维流水线主体**：

[ans/10-dequant-mm.py:94-102](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L94-L102)

```python
for k in T.Pipelined(T.ceildiv(K, BLOCK_K), num_stages=3):
    T.copy(A[pid_m * BLOCK_M, k * BLOCK_K], A_shared)
    T.copy(B[k * BLOCK_K, pid_n * BLOCK_N // 2], B_shared)   # 注意偏移是 BLOCK_N//2

    for i, j in T.Parallel(BLOCK_K, BLOCK_N // 2):           # 4.2 讲过的解包
        B_dequantized[i, j * 2]     = T.cast(B_shared[i, j] & 0x0F,        A_dtype) - 8.0
        B_dequantized[i, j * 2 + 1] = T.cast((B_shared[i, j] >> 4) & 0x0F, A_dtype) - 8.0

    T.gemm(A_shared, B_dequantized, C_local)                 # 一段 tile 的乘加
```

两个细节：

1. **B 的加载偏移 `pid_n * BLOCK_N // 2`**：因为 B 是沿 N 打包的，输出上 `BLOCK_N` 列只对应打包后的 `BLOCK_N//2` 列，所以沿打包维的起点要除以 2（`BLOCK_N=128` 为偶数，`(pid_n*BLOCK_N)//2 == pid_n*(BLOCK_N//2)`，二者等价）。
2. **`T.Pipelined` 的循环次数用 `T.ceildiv(K, BLOCK_K)`** 而非 `K // BLOCK_K`：本题 `K=4096, BLOCK_K=64` 能整除，两者无差别；但 `ceildiv` 更稳健（项目文档提醒：若不能整除又无 mask，最后一段会越界，需要单独处理）。

**写回输出**：

[ans/10-dequant-mm.py:104](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L104)

```python
T.copy(C_local, C[pid_m * BLOCK_M, pid_n * BLOCK_N])
```

累加器 `C_local`（FP32）在写回时一次性降精度到输出 dtype（FP16），与普通 GEMM 完全一致——降精度只在写回显存这一步发生一次。

**运行入口**（题目与答案相同）：

[ans/10-dequant-mm.py:109-133](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/10-dequant-mm.py#L109-L133) 设定 `M=N=K=4096, BLOCK_M=BLOCK_N=128, BLOCK_K=64`，调用 `test_puzzle` 比对 `ref_dequant_matmul`。注意它**只调了 `test_puzzle` 没有 `bench_puzzle`**——性能对比留给你在综合实践里补上。

> 输入张量如何生成？看 [common/utils.py:34-47](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L34-L47)：`uint8` 走 `torch.randint(0, 255, ...)`，所以两个 nibble 都能覆盖 `[0, 15]` 全域，验证充分。

#### 4.3.4 代码实践

**实践目标**：补全 `puzzles/10-dequant-mm.py` 的 `tl_dequant_matmul`（即把 `# TODO` 替换成答案逻辑），跑通 `test_puzzle` 验证与 torch 一致。这是本讲的主任务。

**操作步骤**：

1. 打开 `puzzles/10-dequant-mm.py`，定位 [第 83 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/10-dequant-mm.py#L83) 的 `# TODO: Implement this function`。
2. 按下面四步填入（顺序就是思考顺序）：
   - **① 启动配置**：`with T.Kernel(T.ceildiv(M, BLOCK_M), T.ceildiv(N, BLOCK_N), threads=128) as (pid_m, pid_n):`
   - **② 分配四块缓冲**：`A_shared`（fp16）、`B_shared`（**uint8**，半宽 `BLOCK_N//2`）、`B_dequantized`（fp16，全宽）、`C_local`（fp32，fragment）。
   - **③ 流水线主体**：`T.clear(C_local)` 后 `for k in T.Pipelined(T.ceildiv(K, BLOCK_K), num_stages=3):`，循环内依次 `T.copy(A...)`、`T.copy(B...)`、解包两行、`T.gemm(A_shared, B_dequantized, C_local)`。
   - **④ 写回**：`T.copy(C_local, C[pid_m * BLOCK_M, pid_n * BLOCK_N])`。
3. 运行验证：

```bash
python3 puzzles/10-dequant-mm.py
```

**需要观察的现象**：终端打印 `=== Dequantized Matrix Multiplication ===`，随后是 `✅ Results match: True`。

**预期结果**：`test_puzzle` 以 `atol=rtol=1e-2` 比对通过（FP16 输入、K=4096 长累加，容差较宽松是正常的）。若得 `❌`，优先排查三处常见错误：还原写成 `+8`、`B_shared` 的列宽漏了 `//2`、解包的 `j*2`/`j*2+1` 写反或漏掉一个。

**待本地验证**：本实践需要可用的 CUDA GPU 与已安装的 tilelang（见 u1-l1 的环境检查）。若环境未就绪，可先按 4.2.4 的 CPU 推演确认数值逻辑无误。

#### 4.3.5 小练习与答案

**练习 1**：`B_dequantized` 为什么用 `T.alloc_shared` 而不是 `T.alloc_fragment`？
**答案**：它是 `T.gemm`（Tensor Core MMA）的输入之一，而 MMA 以共享内存为高效输入来源；只有高频读写的累加器 `C_local` 才用 fragment。这复用了 u4-l4 的内存层级取舍。

**练习 2**：解包代码写在 `T.Pipelined` 循环内部，它会被流水线「吃掉」吗？
**答案**：会。`T.Pipelined` 的稳态把「加载下一段 A/B」「解包当前段 B」「`T.gemm` 当前段」重叠调度。解包是 CUDA Core 上的轻量位运算/算术，恰好填补 Tensor Core 计算留下的空隙，几乎不增加关键路径——这正是融合的价值。

**练习 3**：如果把 `num_stages` 从 3 调到 8，一定更快吗？
**答案**：不一定。每多一级流水线都要多缓冲一组 A/B tile，共享内存需求随级数线性增长，可能超出 block 可用 shared memory 上限（A100 约 164 KB）导致编译失败或占用率下降。典型经验值是 2~4，非越大越好（与 u4-l4 结论一致）。

---

## 5. 综合实践

把本讲三个模块（打包/还原 → 位运算解包 → 融合流水线）串成一份「正确性 + 生成代码 + 性能」的迷你实验报告。建议固定 `M=N=K=4096`，完成以下三件事：

1. **正确性**：完成 4.3.4 的主任务，确认 `python3 puzzles/10-dequant-mm.py` 输出 `✅ Results match: True`。

2. **生成代码检视**：仿照 `ans/08-matrix.py` 的 `run_matmul_opt`，给本 puzzle 写一段：

   ```python
   args = {"M":4096,"N":4096,"K":4096,"BLOCK_M":128,"BLOCK_N":128,"BLOCK_K":64}
   kernel = tl_dequant_matmul.compile(**args)
   kernel.print_source_code()
   ```

   在打印出的 CUDA 里寻找三处证据：①`B_shared` 的 `__shared__` 缓冲是 `uint8_t` 类型；②解包处的按位与 `& 15` 与右移 `>> 4`、以及减 `8` 的指令；③`T.gemm` 落到的 `mma`（Tensor Core）指令。这能直观确认「解包被融合进了主循环、没有单独的解包 kernel」。

3. **性能对比**：把 `run_dequant_matmul` 里补一个 `bench_puzzle` 调用（参考 `ans/08-matrix.py:97-102` 的用法，注意本 puzzle 的 `ref_dequant_matmul` 已含解包，可直接作为 `bench_torch=True` 的对照）。记录 TileLang kernel 与 torch（先解包再 `matmul`）的耗时，并思考：尽管本 kernel 多了反量化计算，为什么仍可能不慢于（甚至快于）「先全量解包成 FP16 再 matmul」的朴素两步法？

   **提示**：朴素两步法要把完整 FP16 的 B（`K×N×2` 字节）写回显存再读进来；融合版让解包后的 B 只活在 shared memory，省掉了这次往返，而压缩态 B 的加载量只有原来的 1/4。

   **待本地验证**：具体耗时数字依赖你的 GPU 型号与驱动，请以本地 `bench_puzzle` 输出为准；本任务只要求你能解释趋势，不要求固定数值。

> **进阶（可选）**：把还原公式从 `q − 8` 推广到真实的仿射反量化 `scale * (q − zero_point)`——额外传入一个 per-output-channel 的 `scale: T.Tensor((N,), float16)`，在解包两行后乘上 `scale[j*2]` / `scale[j*2+1]`，并自写一个 torch 参考验证。这是迈向真实 GPTQ/AWQ kernel 的下一步。

## 6. 本讲小结

- **INT4 量化 + uint8 打包**：两个 INT4 打包进一个字节，权重体积压到 FP16 的 **1/4**；B 的声明列数因此是 `N//2`，而输出 C 恢复为 `N`。
- **还原公式 `q − 8`**：把无符号 nibble \([0,15]\) 映射到有符号 \([-8,7]\)，等价于 `scale=1, zero_point=8` 的仿射量化特例——它**不是**二补码有符号 int4，别用错。
- **位运算 + T.cast**：`& 0x0F` 取低 nibble、`>> 4` 取高 nibble，`T.cast` 转 FP16 后减 8；低/高 nibble 交错写入相邻两列（`j*2` / `j*2+1`）。
- **融合流水线**：解包 + 反量化塞进 `T.Pipelined` 的 K 维循环内部，解包后的 FP16 B 只活在 shared memory、从不落回显存，额外计算被访存/计算重叠掩盖。
- **内存层级取舍沿用 u4-l4**：`A_shared`/`B_shared`/`B_dequantized` 用 shared（`T.gemm` 的高效输入），累加器 `C_local` 用 fragment（FP32 高精度累加），降精度只在写回显存时发生一次。
- **三件套验证**：`test_puzzle` 验正确性、`compile().print_source_code()` 看融合是否生效、`bench_puzzle` 量带宽收益——这套方法论贯穿 u2-l2 / u4-l4 / u5-l4。

## 7. 下一步学习建议

- **横向扩展：真实量化格式**。本 puzzle 是 `scale=1, zero_point=8` 的最简特例。要理解生产级 kernel，建议阅读 GPTQ、AWQ 的权重打包格式（per-group/per-channel scale、显式 zero_point），并尝试在综合实践的进阶任务里把 `scale` 接入本 kernel。
- **纵向深挖：性能工程**。本单元最后一讲 **u5-l4 性能与生成代码检视** 会系统总结 `bench_puzzle` 计时方法学、`print_source_code` 诊断、以及 block size / `num_stages` / shared-vs-fragment 的调参直觉——正好把本讲的「进阶任务」与「综合实践」收口。
- **回顾整条主线**。本 puzzle 把全书的关键决策串了起来：三级内存（u2-l2）、归约与高精度累加（u3-l2、u4-l2）、`T.gemm`/Tensor Core（u4-l3）、共享内存与软件流水线（u4-l4）。建议合上讲义，凭记忆画出本 kernel 的数据流图（global → shared → dequant → gemm → fragment → global），以此作为整本手册的结业自测。
