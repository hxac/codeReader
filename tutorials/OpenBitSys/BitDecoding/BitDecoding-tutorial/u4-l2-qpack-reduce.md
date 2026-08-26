# 归约原语：warp_reduce、allreduce_ 与分组 max/min

> 本讲所有行号与永久链接基于 HEAD `ae0d83630d6292453355ced498db2ac87f56ec62`。

## 1. 本讲目标

上一讲（u4-l1）我们打通了 QPack kernel 的**启动链**：从 `run_kvcache_qpack` 到 `flash_qpack_kernel` 的网格与共享内存。本讲下潜一层，精读这个 kernel 里最核心的计算基础设施——**分组归约原语**。

量化打包的第一步永远是：对每个量化组求 `max` 与 `min`，据此算出 `scale` 和 `zero`。这一步做不快、做不对，后面的打包全白搭。BitDecoding 在 [qpack.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h) 里实现了一套三段式归约：

1. `thread_reduce_`：每线程先在**自己的寄存器**里按组归约；
2. `warp_reduce`：用**异或蝴蝶洗牌**只与「`lane % 4` 相同」的 8 个线程合并；
3. `allreduce_`：借共享内存 `reduce_tmp` 跨 4 个 warp 汇总。

学完本讲你应当能够：

- 解释 `warp_reduce` 中 `lane_id % 4` 分组与 `__shfl_xor_sync` 蝴蝶模式如何配合，为什么 mask 取 16/8/4 时才真正合并；
- 描述 `allreduce_` 借助 `reduce_tmp` 跨 warp 归约的三阶段流程，以及最容易被忽略的细节——**最终读回结果按 `lane_id % 4` 区分，各线程组拿到的是各自的答案**；
- 理解 `reduce_max` / `reduce_min` 输出的形状（`4 * num_params` 个标量）如何一一对应量化分组，并了解 k-tensor 模式使用的另一套 `*_g` 姊妹实现。

## 2. 前置知识

### 2.1 为什么量化需要「分组 max/min」

回顾 u2-l1：线性量化把一段 FP16 数值映射到 `num_bits` 位整数。对一个量化组（`group_size` 个元素共享一组参数）：

\[ \text{scale} = \frac{\max - \min}{2^{b}-1}, \qquad \text{zero} = \min, \qquad q = \mathrm{round}\!\left(\frac{x - \text{zero}}{\text{scale}}\right) \]

所以**归约的粒度 = 量化组的粒度**。组内 `max/min` 求错一个，整组元素的还原值就全错。本讲的主角就是「如何在 GPU 上高效、正确地算出每个组的 `max/min`」。

### 2.2 warp、lane 与蝴蝶洗牌

- GPU 线程以 32 个为一组构成一个 **warp**，warp 内线程编号 `lane_id ∈ [0,32)`，可写作 `lane_id = 8·(lane/4 对应 bit2-4) ... ` 更有用的分解是按位看：`lane%4` 由低 2 位决定，`lane/4 ∈ [0,8)` 由高 3 位决定。
- `__shfl_xor_sync(mask, val, m)`：让 lane `l` 与 lane `l ^ m` 交换寄存器值 `val`，且是 warp 内**全员同步**的集体操作。用 `m = 16, 8, 4, 2, 1` 逐次异或，即得经典「蝴蝶归约」：\(\log_2 32 = 5\) 步内全 warp 得到同一个归约结果。
- 共享内存 + `__syncthreads()`：跨 warp 通信必须经由共享内存，且每次读写之间要 barrier。

### 2.3 CuTe 的「每线程寄存器片段」

QPack kernel 用 CuTe 的 `thr_mma.partition_fragment_B(sK)` 把整块 K tile（`kBlockN × kHeadDim`）**切到 128 个线程的寄存器**里。对于 SM80 的 `m16n8x16` MMA，B 操作数的标准片段布局是：

- 片段形状 `((2,2), MMA_N, MMA_K)`：每线程在每个 `(n块, k块)` 位置持有 **4 个 FP16**；
- 在一个 8-token × 16-dim 的原子 tile 内：**token 由 `lane/4` 选定（每线程 1 个 token），4 个寄存器 = 该 token 的 4 个通道维**（由 `lane%4` 决定是哪 4 个）。

也就是 `lane%4`（类）和「4 个寄存器行」共同把 16 个通道维铺满：4 类 × 每类 4 维 = 16。记住这个结构，第 4.2 节的归约设计会立刻显得理所当然。

### 2.4 与上一讲的衔接

- `Flash_qpack_traits` 的常量（`kBlockN`、`num_params`、`kNThreads=128`）见 [kernel_traits.h:450-492](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L450-L492)；
- kernel 主体 `compute_qpack_1rowblock` 在 [flash_fwd_kernel.h:1272-1302](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1272-L1302)。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [csrc/bit_decode/src/include/qpack.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h) | 本讲主角：`quant::` 命名空间的 `thread_reduce_` / `warp_reduce` / `allreduce_` / `reduce_max` / `reduce_min`，以及 k-tensor 模式的 `*_g` 家族 |
| [csrc/bit_decode/src/include/softmax.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h) | 出身对照：FlashAttention 原版 `flash::thread_reduce_` / `quad_allreduce_` 等，`quant::` 版本由它改造而来 |
| [csrc/bit_decode/src/include/utils.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L103-L153) | `MaxOp` / `MinOp` 算子与 `Allreduce<N>` 蝴蝶模板 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L535-L550) | `num_params` 常量与 `SmemLayoutReduce_tmp`（32×32 共享内存） |
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1450-L1477) | 调用现场：`compute_qpack_1rowblock` 与残余 kernel 里的 `qpack_Kchannel_Vtensor` / `quant_Ktensor` |
| [csrc/bit_decode/src/include/dequantize.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L176-L194) | 消费端：`load_params_Kchannel` 用与写入端对称的公式读回参数 |

## 4. 核心概念与源码讲解

先给一张总览图（文字版数据流）：

```text
K tile 的一个 16 维切片 (kBlockN token × 16 dim, 已在 128 线程寄存器中)
   │
   ▼ thread_reduce_          每线程: 对自己的 (4 寄存器行 × MMA_N 列) 按 num_params 分组归约
   │   summary(4g + r) = op over 该线程第 r 行、第 g 组的列     （部分和仍在寄存器）
   ▼ warp_reduce             每 8 个 lane%4 相同的线程: 蝴蝶洗牌合并（3 步有效）
   │   现在 8 个 token 位 × 全部组 都已合并到类内
   ▼ allreduce_              lanes<4 写 reduce_tmp → 跨 4 warp 合并 → 全员按 lane%4 读回
   │
   ▼ reduce_max / reduce_min = thread_reduce_ + allreduce_（分别配 MaxOp / MinOp）
```

### 4.1 `thread_reduce_`：寄存器内的按组归约

#### 4.1.1 概念说明

`thread_reduce_` 解决的问题是：**每个线程手里攥着一大把寄存器值，先把同一量化组的值在本地折叠成一个部分和**，尽量减少后续需要跨线程通信的数据量。它是纯寄存器操作，零通信、零同步。

注意它与 FlashAttention 原版 `flash::thread_reduce_`（[softmax.h:22-36](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L22-L36)，整行归约成一个 summary）的关键区别：量化版多了一个 `num_params` 参数，**行方向不再是 1 个摘要，而是 `num_params` 个分组摘要**。

#### 4.1.2 核心流程

输入是切片后的 2D 寄存器张量 `tensor(4, MMA_N)`（4 = MMA 原子片段的寄存器行数，`MMA_N = kBlockN / 8`，每一列对应 8 个 token 的原子 tile）：

1. `pack_num = size<1>(tensor) / num_params`：每组占多少**列**；
2. 对每个摘要下标 `mi ∈ [0, 4·num_params)`：
   - 组号 `g = mi / 4`，寄存器行 `r = mi % 4`；
   - 该组覆盖列区间 `[g·pack_num, (g+1)·pack_num)`；
   - `summary(mi) = op` 折叠该线程第 `r` 行在这个区间的所有值。

用公式表达：

\[ \text{summary}(4g + r) = \mathop{\text{op}}_{n_i \,\in\, [g\cdot p,\,(g+1)\cdot p)} \text{tensor}(r,\, n_i), \qquad p = \text{pack\_num} \]

**算术自检**（每组覆盖的 token 数 = `pack_num × 8` 必须等于 `group_size`）：

| 配置 | kBlockN | MMA_N | num_params | pack_num | 每组 token = pack_num×8 |
| --- | --- | --- | --- | --- | --- |
| 4-bit, g=128 | 128 | 16 | 1 | 16 | 128 ✓ |
| 4-bit, g=32 | 128 | 16 | 4 | 4 | 32 ✓ |
| 2-bit, g=128 | 256 | 32 | 2 | 16 | 128 ✓ |
| 2-bit, g=32 | 256 | 32 | 8 | 4 | 32 ✓ |

其中 `num_params = kBlockN_pack / group_size` 定义于 [kernel_traits.h:487](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L487)。

> ⚠️ **命名冲突警告**：qpack.h 局部变量 `pack_num`（= 每组占的 MMA_N 列数 = `group_size/8`）与 traits 里的常量 `pack_num = 16/num_bits`（= 一个 uint16 装几个整数，[kernel_traits.h:462](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L462)）**是两个不同的东西**，读代码时务必区分。

#### 4.1.3 源码精读

[thread_reduce_ 的完整实现，qpack.h:11-28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L11-L28)：

```cpp
const int pack_num = size<1>(tensor) / num_params;
CUTE_UNROLL
for (int mi = 0; mi < size<0>(summary); ++mi) {
    int col_start = (mi / 4) * pack_num;      // 组号 g = mi/4 决定列起点
    summary(mi) = tensor(mi % 4, col_start);  // 寄存器行 r = mi%4
    CUTE_UNROLL
    for (int ni = col_start; ni < col_start + pack_num; ++ni) {
        summary(mi) = op(summary(mi), tensor(mi % 4, ni));
    }
}
```

- 第 14 行：`pack_num` 即每组列数；
- 第 18-19 行：`mi/4` 定组、`mi%4` 定行——**摘要下标布局是 `s = 4·g + r`（组优先、行靠后）**，这一点在后面读回（`channel_zeros`）与写盘时反复出现，务必记住；
- 第 22-24 行：组内折叠。首元素被 `op` 了两次（先赋值再和自己合并），对 max/min 是幂等的，无害。

调用现场：[qpack_kc_vt<4>::apply 的 k 循环，qpack.h:233-237](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L233-L237)。外层 `for (k < size<2>(src))` 每次 `reduce_max`/`reduce_min` 处理 **16 个通道维** 的切片（`size<2>(src) = kHeadDim/16 = 8` 次循环覆盖 128 维），`src(_, _, k)` 就是切片后的 `(4, MMA_N)` 视图：

```cpp
for (int k = 0; k < size<2>(src); ++k) {
    quant::reduce_max(src(_, _, k), channel_max, reduce_tmp, num_params);
    quant::reduce_min(src(_, _, k), channel_min, reduce_tmp, num_params);
    ...  // 用 channel_max/min 算 scale/zero，然后量化打包（u4-l3 的内容）
}
```

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，用 NumPy 复刻 `thread_reduce_` 的下标算术，验证摘要布局 `s = 4g + r` 与分组列区间。

**操作步骤**（示例代码）：

```python
import numpy as np
R, MMA_N, num_params = 4, 16, 4          # 4-bit, group_size=32
pack_num = MMA_N // num_params            # = 4 列/组

tensor = np.random.rand(128, R, MMA_N).astype(np.float32)  # 128 线程各自的寄存器视图

def thread_reduce_(tensor, op):
    n_thread = tensor.shape[0]
    summary = np.zeros((n_thread, R * num_params), dtype=np.float32)
    for mi in range(R * num_params):
        col_start = (mi // 4) * pack_num
        summary[:, mi] = op(tensor[:, mi % 4, col_start:col_start + pack_num], axis=1)
    return summary

partial = thread_reduce_(tensor, np.max)

# 交叉验证：直接按 (g, r) 语义重算线程 0 的部分和
g, r = 2, 3
expect = tensor[0, r, g*pack_num:(g+1)*pack_num].max()
print(partial[0, 4*g + r] == expect)      # 应打印 True
```

**需要观察的现象**：`partial[t, s]` 只聚合了线程 `t` 自己的寄存器——不同线程之间还没有任何合并。

**预期结果**：断言为 `True`；`summary` 形状为 `(128, 16)`（= 4·num_params）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `num_params` 误传成 `2`（配置实为 4），`pack_num` 变成多少？会发生什么？

**答案**：`pack_num = 16/2 = 8`，每组覆盖 8×8 = 64 个 token，是 `group_size=32` 的两倍。归约会跨组取 max/min，导致 scale 被撑大、量化精度下降，且写出的 params 数量与布局都对不上。

**练习 2**：为什么 `thread_reduce_` 里不需要任何 `__syncthreads()` 或 shuffle？

**答案**：它只读写**本线程**的寄存器片段与摘要（`summary` 是 `make_fragment_like` 出来的寄存器 fragment），完全没有跨线程数据流；同步开销被刻意推迟到后面最少的两处。

### 4.2 `warp_reduce`：lane%4 分组与异或蝴蝶洗牌

#### 4.2.1 概念说明

`thread_reduce_` 之后，每线程有了 `4·num_params` 个部分和，但**一个量化组的 token 分布在多个线程手里**（2.3 节：一个 8-token 原子 tile 内，token 由 `lane/4` 选定）。于是需要跨线程合并。

普通蝴蝶归约会**把整个 warp 的 32 个线程合并成一个值**——但这里不行！`lane%4` 不同的类持有**不同的通道维切片**（每类 4 个维），量化参数是**逐维**的，四类的 max 绝不能混。所以 `warp_reduce` 的设计是：**只与 `lane%4` 相同的 8 个线程合并**，warp 内同时进行 4 路独立的归约。

#### 4.2.2 核心流程

对 mask 序列 `16, 8, 4, 2, 1` 逐一执行 `__shfl_xor_sync(0xffffffff, val, mask)`，但只在 `(lane ^ mask) % 4 == lane % 4` 时才合并：

- `mask = 16/8/4`：翻转 lane 的 bit4/bit3/bit2（即 `lane/4` 部分），**不改变低 2 位** → 条件为真，执行合并。3 步蝴蝶恰好覆盖 8 个同类线程：\(\log_2 8 = 3\)；
- `mask = 2/1`：翻转 bit1/bit0，**必然改变 `lane%4`** → 条件为假，洗牌照做（维持 warp 收敛，`__shfl` 是集体操作不能陷入分歧分支），但值被丢弃。

合并后的语义（结合 2.3 节的片段布局）：类 `c = lane%4` 的 8 个线程分别持有 8 个 token 位，蝴蝶合并后，类内每个线程都得到

\[ \text{val} = \max_{\text{组 } g \text{ 的所有 token}} x[\text{token},\ D_r(c)] \]

即「组 `g` 的全部 token、在类 `c` 的第 `r` 个通道维」上的极值——**恰好是一个 (组, 维) 参数需要的归约范围**。

#### 4.2.3 源码精读

[warp_reduce 的完整实现，qpack.h:30-46](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L30-L46)：

```cpp
const int lane_id = threadIdx.x % 32;
const int group_pos = lane_id % 4;          // 本线程所属的类
for (int mask = 16; mask > 0; mask >>= 1) {
    T other = __shfl_xor_sync(0xffffffff, val, mask);   // 全员执行，保持 warp 收敛
    if ((lane_id ^ mask) < 32 && ((lane_id ^ mask) % 4 == group_pos)) {
        val = op(val, other);               // 只与同类线程合并
    }
}
return val;
```

- 第 33-34 行：类号就是 `lane_id % 4`；
- 第 38 行：`__shfl_xor_sync` 放在 `if` **外**——若移进分支会造成 warp 分歧下执行集体洗牌，虽然现代架构对全掩码 shuffle 容忍分歧，保持在外是规范写法；
- 第 41 行：`(lane_id ^ mask) < 32` 对 `mask < 32` 恒真（防御式写法，无实际过滤作用）；真正的过滤器是 `(lane_id ^ mask) % 4 == group_pos`。

对照：FlashAttention 原版在 warp 内的归约有 `warp_reduce_acc`（[softmax.h:47-67](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L47-L67)，`__shfl_down_sync` 归约连续 4 线程组再广播）和 `Allreduce<4>`（[utils.h:133-153](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L133-L153)，对连续 4 线程做蝴蝶）。`quant::warp_reduce` 的特殊之处在于按 `lane%4`（**跨步 4 的类**）而不是连续 4 线程归约——这正是量化片段「token = lane/4、维度 = lane%4」布局的直接产物。

#### 4.2.4 代码实践

**实践目标**：在纸面上验证蝴蝶掩码的类保持性，并体会「为什么恰好 3 步有效」。

**操作步骤**：

1. 取 lane = 13（二进制 `01101`），`group_pos = 13 % 4 = 1`；
2. 对 mask ∈ {16, 8, 4, 2, 1} 分别计算 `13 ^ mask` 与 `(13 ^ mask) % 4`，填表；
3. 画出 8 个同类 lane {1, 5, 9, 13, 17, 21, 25, 29} 在 3 步蝴蝶中的配对过程。

**需要观察的现象**：mask=16/8/4 时 `(13^mask) % 4 == 1` 成立；mask=2/1 时不成立。

**预期结果**：3 步有效蝴蝶把这 8 个 lane 按 (01,101)→(…) 的树状结构两两合并，\(\log_2 8 = 3\) 步后 8 个 lane 全部持有类内最大值。此实践为纸面推导，无需运行（也可在 4.3.4 的程序里加打印验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 41 行的 guard 整个删掉，变成普通全 warp 蝴蝶，结果会怎样？

**答案**：4 个类（4 组不同的通道维）的极值被合并成一个全 warp 极值，`summary` 的每个条目都变成「4 个维度的联合 max」。量化仍能运行（数值域没越界），但 scale 被无关维度的极值撑大，有效精度明显下降——最坏时某些维度的值全部塌缩到同一个量化码字。

**练习 2**：为什么循环写成 `for (mask = 16; mask > 0; mask >>= 1)` 而不是只写 `{16, 8, 4}` 三步？

**答案**：功能上三步即可；保留完整 5 步模板让函数对任意「按 `lane%4` 分类」的归约保持统一的蝴蝶骨架（也沿袭了 FlashAttention 的写法）。后两步的洗牌结果被 guard 丢弃，只付出两条 SHFL 指令的代价。

### 4.3 `allreduce_`：`reduce_tmp` 共享内存上的跨 warp 汇总

#### 4.3.1 概念说明

`warp_reduce` 之后，归约仍停留在**每个 warp 内部**。QPack kernel 有 4 个 warp（`kNWarps=4`，见 [flash_fwd_launch_template.h:200-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L200-L206) 中 `Flash_qpack_traits<Headdim, kBlockN, 4, ...>` 的第 3 个模板参数），warp 之间只能借共享内存通信。`allreduce_` 用一块 32×32 的 float 共享内存 `reduce_tmp` 完成汇总。

还有个容易看漏的要点：**最终结果不是全线程统一的**。第 79 行 `dst(i) = reduce_tmp(i, lane_id % 4)` 按 lane 的类读回——每个类拿到属于自己那 4 个通道维的最终极值。这是量化特有的语义（逐维参数），与 FlashAttention softmax 需要「所有人拿到同一个 max」完全不同。

> 补充一个事实：由于 `TiledMma` 以 `Layout<Shape<_1,_4,_1>>` 排布（[kernel_traits.h:489-492](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L489-L492)），每个线程的 B 片段都覆盖完整 `(kBlockN × 16)` 切片（可从上表 `pack_num×8=group_size` 反推 `size<1> = kBlockN/8`），即 4 个 warp 各持一份冗余副本——跨 warp 的 max 合并在数学上是幂等的，但代码必须合并，因为每个 warp 只有自己寄存器里的那份部分和。

#### 4.3.2 核心流程

对 `summary` 的每个条目 `i`（共 `size(dst)` 个）执行三阶段：

```text
阶段 ①  warp_reduce(src(i), op)
         每线程对第 i 个条目做 4.2 节的类内蝴蝶
阶段 ②  if (lane_id < 4):
             reduce_tmp(i, warp_id*4 + lane_id) = val        # 每 warp 写 4 个类槽位
         __syncthreads()                                     # 16 个槽位就绪 (4 warp × 4 类)
阶段 ③  if (lane_id < 4):
             final = op over w∈[0,4) of reduce_tmp(i, w*4 + lane_id)   # 跨 warp 合并同类
             reduce_tmp(i, lane_id) = final
         __syncthreads()
阶段 ④  dst(i) = reduce_tmp(i, lane_id % 4)                  # 全员按自己的类读回
```

写回布局：`reduce_tmp` 第二维的 16 个槽位按 `(warp_id, class)` 编码为 `warp_id*4 + class`；最终每个类的答案留在第 `class` 列。

一个正确性细节：阶段 ③ 的 guard 是 `lane_id < 4`，**4 个 warp 的前 4 个 lane 都会执行**这段合并（4× 冗余计算、写同一位置同值），正确但非最优——对一次性运行的 qpack kernel 无所谓。

#### 4.3.3 源码精读

[allreduce_ 的完整实现，qpack.h:48-84](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L48-L84)：

```cpp
const int warp_id = threadIdx.x / 32;
const int lane_id = threadIdx.x % 32;
#pragma unroll
for (int i = 0; i < size(dst); i++) {
    float val = quant::warp_reduce(src(i), op);          // ① 类内蝴蝶
    if (lane_id < 4) {
        reduce_tmp(i, warp_id * 4 + lane_id) = val;      // ② 每 warp 写 4 槽
    }
    __syncthreads();
    if (lane_id < 4) {
        float final_val = reduce_tmp(i, 0 + lane_id);
        #pragma unroll
        for (int w = 1; w < 4; w++) {                    // ③ 跨 4 warp 合并
            final_val = op(final_val, reduce_tmp(i, w * 4 + lane_id));
        }
        reduce_tmp(i, 0 + lane_id) = final_val;
    }
    __syncthreads();
    dst(i) = reduce_tmp(i, 0 + lane_id % 4);             // ④ 按类读回！
}
```

- 第 57 行：`size(dst) = 4·num_params`（4 与 16 与 32，见 4.1.2 的表）；
- 第 61-63 行：只有 `lane_id < 4` 的线程写——每个 warp 恰好 4 个类各写一个槽；
- 第 70 行：`w < 4` 硬编码 4 个 warp，与 `kNWarps=4` 绑定（换成 8 warp 的 kernel 不能直接复用）；
- 第 79 行：**全讲最关键的一行**。对照 `flash::quad_allreduce_` 的 [softmax.h:153](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L153) `dst(i) = reduce_tmp(i, 0)`（全员统一），这里读哪一列由 `lane_id % 4` 决定。

`reduce_tmp` 的来源：

- 布局定义 [kernel_traits.h:535-538](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L535-L538)：`make_layout(make_shape(32, 32), make_stride(32, 1))` —— 32×32 float = 4 KB，行主序；
- 存储声明 [kernel_traits.h:543-550](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L543-L550)：`SharedStorage` 中的 `smem_reduce_tmp`；
- kernel 内构造张量视图 [flash_fwd_kernel.h:1366](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1366)：`Tensor sReduce_tmp = make_tensor(... SmemLayoutReduce_tmp{})`，随后在 [flash_fwd_kernel.h:1459-1477](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1459-L1477) 传给量化函数。

容量核算：第一维要容纳 `size(dst) = 4·num_params ≤ 32`（2-bit、group_size=32 时 `num_params=8` 恰好占满，这也解释了为什么更小的 group_size 需要改这块 smem）；第二维 32 列只用了前 16 列——[kernel_traits.h:535](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L535) 的 `TODO: 32 of x can be determined` 注释正是指这份冗余。

**输出形状如何对应量化分组**：`reduce_max/reduce_min` 的输出 `channel_max/channel_min` 形状为 `(4·num_params,)`，下标 `s = 4·g + r`。经过三段归约后，条目 `s` 在类 `c` 的线程手里的物理含义是：**组 `g` 的全部 token、在（类 `c` 的 4 个通道维中第 `r` 个）维度上的极值**。每个 16 维切片共有 `4 类 × 4 行 × num_params 组 = 16·num_params` 个条目，恰好铺满「组 × 维」参数空间一次。消费端（量化循环）按 `channel_zeros(r + 4·g)` 的下标读回，见 [qpack.h:161-178](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L161-L178)（`channel_stride = size<0>(src) = 4`）；写盘端与解码读回端使用同一套 `params(j%num_params, 8i + 4*(j/num_params) + tidx%4)` 寻址公式（写：[flash_fwd_kernel.h:1489-1497](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1489-L1497) 与 [qpack.h:516-523](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L516-L523)；读：[dequantize.h:185-193](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L185-L193)），两侧对称保证往返自洽。

> 💡 **读到这里你可能疑惑**：把公式里的列号直接当成 Python 张量的 head_dim 序号对不上？确实如此。params 块内部的具体排列是 kernel 约定的**自洽置换**——只要写入（qpack）与读出（decode 的 load_params）公式镜像对称，数值就正确；Python 侧只按形状分配和 `torch.cat` 拼接，从不解释单个条目。作者也在 [dequantize.h:189](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L189) 留下了 `seems no one can know why is this offset ...` 的注释。不必在此恋战，记住「对称即可」即可继续。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写一个 128 线程的最小 CUDA 程序，**逐字复刻** `warp_reduce + allreduce_`，验证「按 `lane%4` 分类、各类独立求 block 级 max」的语义，并与 CPU 参考结果对比。

**操作步骤**（示例代码，保存为 `qpack_reduce_test.cu`）：

```cpp
#include <cstdio>
#include <cstdlib>
#include <cmath>

// 复刻自 qpack.h:30-46（把模板与 op 换成 float/max 内联）
__device__ __forceinline__ float warp_reduce(float val) {
    const int lane_id = threadIdx.x % 32;
    const int group_pos = lane_id % 4;
    for (int mask = 16; mask > 0; mask >>= 1) {
        float other = __shfl_xor_sync(0xffffffff, val, mask);
        if ((lane_id ^ mask) < 32 && ((lane_id ^ mask) % 4 == group_pos)) {
            val = fmaxf(val, other);
        }
    }
    return val;
}

// 复刻自 qpack.h:48-84（reduce_tmp 换成裸数组，布局同 32x32 行主序）
__global__ void allreduce_test(const float* data, float* out, int n_entries) {
    __shared__ float reduce_tmp[32][32];
    const int warp_id = threadIdx.x / 32;
    const int lane_id = threadIdx.x % 32;

    for (int i = 0; i < n_entries; i++) {
        float val = warp_reduce(data[threadIdx.x * n_entries + i]);   // ①
        if (lane_id < 4) reduce_tmp[i][warp_id * 4 + lane_id] = val;  // ②
        __syncthreads();
        if (lane_id < 4) {                                            // ③
            float final_val = reduce_tmp[i][0 + lane_id];
            for (int w = 1; w < 4; w++)
                final_val = fmaxf(final_val, reduce_tmp[i][w * 4 + lane_id]);
            reduce_tmp[i][0 + lane_id] = final_val;
        }
        __syncthreads();
        out[threadIdx.x * n_entries + i] = reduce_tmp[i][lane_id % 4]; // ④ 按类读回
    }
}

int main() {
    const int n_threads = 128, n_entries = 4;   // 模拟 num_params=1 时 summary 的 4 个条目
    float *h_data = new float[n_threads * n_entries];
    for (int t = 0; t < n_threads * n_entries; t++) h_data[t] = (float)(rand() % 1000) / 7.0f;

    float *d_data, *d_out;
    cudaMalloc(&d_data, n_threads * n_entries * sizeof(float));
    cudaMalloc(&d_out,   n_threads * n_entries * sizeof(float));
    cudaMemcpy(d_data, h_data, n_threads * n_entries * sizeof(float), cudaMemcpyHostToDevice);
    allreduce_test<<<1, 128>>>(d_data, d_out, n_entries);

    float* h_out = new float[n_threads * n_entries];
    cudaMemcpy(h_out, d_out, n_threads * n_entries * sizeof(float), cudaMemcpyDeviceToHost);

    int bad = 0;  // CPU 参考：类 c 的第 i 条目 = 所有 t%4==c 线程 data[t][i] 的最大值
    for (int t = 0; t < n_threads && !bad; t++)
        for (int i = 0; i < n_entries; i++) {
            float ref = -INFINITY;
            for (int tt = 0; tt < n_threads; tt++)
                if (tt % 4 == t % 4) ref = fmaxf(ref, h_data[tt * n_entries + i]);
            if (fabsf(h_out[t * n_entries + i] - ref) > 1e-5) { bad = 1; break; }
        }
    printf(bad ? "FAIL\n" : "PASS: class-wise block max correct (128 threads, 4 entries)\n");
    return bad;
}
```

编译运行：`nvcc -arch=sm_80 -o qpack_reduce_test qpack_reduce_test.cu && ./qpack_reduce_test`（`-arch` 按本机 GPU 调整为 sm_70 及以上均可，`__shfl_xor_sync` 无架构特殊依赖）。

**需要观察的现象**：输出 `PASS`；把第 ④ 步改成 `reduce_tmp[i][0]`（模拟 `flash::quad_allreduce_` 的统一读回）再运行，与参考结果比较。

**预期结果**：原版 `PASS`；改成统一读回后 `FAIL`——因为类 1/2/3 的条目会被类 0 的答案覆盖。这一对比正是 `quant::allreduce_` 与 FA 原版的本质区别。若无 GPU 环境，本实践**待本地验证**（也可把 4.1.4 的 NumPy 代码扩展成 `warp_reduce` 的纯软件模拟先行验证下标逻辑）。

#### 4.3.5 小练习与答案

**练习 1**：`reduce_tmp` 为什么是 32×32？两个维度各卡住了什么配置？

**答案**：第一维（条目数）需 `4·num_params ≤ 32`，即 `num_params ≤ 8`。对 2-bit（`kBlockN_pack=256`）意味着 `group_size ≥ 256/8 = 32`——当前最小 group_size=32 恰好占满；第二维 32 列只需 16（4 warp × 4 类），一半是冗余（对应源码 TODO 注释）。

**练习 2**：一次 `qpack_Kchannel_Vtensor<4>` 调用（group_size=32）里，`allreduce_` 总共触发多少次 `__syncthreads()`？

**答案**：`size(dst) = 4·num_params = 16`，每次 `allreduce_` 内 2 次屏障；`reduce_max` + `reduce_min` 各一次 `allreduce_`，外层 k 循环 `kHeadDim/16 = 8` 次：`8 × 2 × 16 × 2 = 512` 次。QPack kernel 只在 prefill 时运行一次，这些屏障不是性能热点，但解释了为什么这套实现以「正确、简单」为先。

**练习 3**：`allreduce_` 假设了 4 个 warp。如果 `kNWarps` 改成 8，哪几行必须动？

**答案**：`reduce_tmp(i, warp_id * 4 + lane_id)` 的槽位编码、`for (w = 1; w < 4; w++)` 的循环上界（改 8），且第二维至少需要 `8×4 = 32` 列（恰好占满）。第一维不受影响。

### 4.4 `reduce_max` / `reduce_min` 封装与两套姊妹实现

#### 4.4.1 概念说明

前三个原语是「积木」，真正被量化代码调用的是封装与两套变体：

| 家族 | 归约范围 | 跨线程方式 | 服务对象 |
| --- | --- | --- | --- |
| `quant::reduce_max/min`（kc 路径） | 逐通道维、逐组 | `lane%4` 类蝴蝶 + `reduce_tmp` 跨 warp | k-channel 模式的 K，以及所有模式的 V |
| `quant::reduce_max_g/min_g`（`*_g` 家族） | 逐组 | 连续 4 线程组（quad）`__shfl_sync` 广播合并 | k-tensor 模式的 K（`quant_Ktensor`） |
| `flash::reduce_max` 等（softmax.h） | 全线程统一 | quad + `reduce_tmp` | FlashAttention 在线 softmax |

三者外形相似，但「谁和谁合并、结果给谁」各不相同——这正是本讲反复强调的两条轴：**分组轴**（类的划分）与**结果分发轴**（统一值 vs 按类值）。

#### 4.4.2 核心流程

- kc 路径：`reduce_ = thread_reduce_ + allreduce_`，`reduce_max/reduce_min` 只是分别注入 `flash::MaxOp/MinOp`；
- `*_g` 路径：`thread_reduce_g`（[qpack.h:339-355](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L339-L355)，按 `k` 切出第 `k` 组的 `num_params` 个条目逐组折叠）+ `quad_allreduce_g`（[qpack.h:314-337](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L314-L337)，`group_id = tid/4` 的**连续** 4 线程组内，用 4 次 `__shfl_sync` 主动收集组内 4 个值合并后广播）。k-tensor 模式整块共享极小范围内的参数，quad 归约就够了，不必动用共享内存；
- flash 原版：`flash::thread_reduce_`（整行折叠）+ `flash::quad_allreduce_`（[softmax.h:120-155](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L120-L155)，`Allreduce<4>` 蝴蝶 + 每 warp 槽位 + lane0 汇总，**全员读同一个值**），服务于 `softmax_rescale` 的行最大值。

#### 4.4.3 源码精读

[reduce_ / reduce_max / reduce_min 封装，qpack.h:86-105](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L86-L105)：

```cpp
void reduce_(...) {
    quant::thread_reduce_(tensor, summary, op, num_params);
    quant::allreduce_(summary, summary, reduce_tmp, op);
}
void reduce_max(...) { flash::MaxOp<float> max_op; quant::reduce_(tensor, max, reduce_tmp, max_op, num_params); }
void reduce_min(...) { flash::MinOp<float> min_op; quant::reduce_(tensor, min, reduce_tmp, min_op, num_params); }
```

- `MaxOp/MinOp` 是极简仿函数，定义在 [utils.h:103-122](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L103-L122)（float 特化直接用 `max()/min()` 内建，注释标明更快）；
- `allreduce_(summary, summary, ...)` 源与目的同一个 fragment，原地完成。

`*_g` 家族的 quad 合并核心，[qpack.h:324-334](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L324-L334)：

```cpp
const int group_id = threadIdx.x / 4;
const int group_base = group_id * 4;
auto val = __shfl_sync(uint32_t(-1), src(i), group_base);      // 先取组内 0 号
for (int offset = 1; offset < 4; offset++) {                   // 再合并 1/2/3 号
    val = op(val, __shfl_sync(uint32_t(-1), src(i), group_base + offset));
}
dst(i) = val;                                                  // 全组广播
```

调用现场一（qpack kernel，[flash_fwd_kernel.h:1459-1463](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1459-L1463)）：`quant_mode == 1` 走 k-channel 的 `qpack_Kchannel_Vtensor`（内部用 `reduce_max/reduce_min`），否则走 `quant_Ktensor`（内部用 `reduce_max_g/reduce_min_g`，见 [qpack.h:394-398](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L394-L398)）；V 恒走 kc 路径（[flash_fwd_kernel.h:1477](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1477)，但片段是转置布局，组沿通道方向——细节留待 u4-l3）。

调用现场二（残余 kernel，[flash_fwd_kernel.h:403](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L403) 与 [468](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L468)）：decode 阶段残余区攒满一块时，**在注意力 kernel 内原地复用**这套归约原语完成再量化——同一份代码服务 prefill 与 decode 两条链路。

> ⚠️ 2-bit 有一个作者标注的特例：`qpack_kc_vt<2>` 里 `num_params_2 = size<1>(src)==4 ? num_params/2 : num_params`（[qpack.h:116-117](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L116-L117)，旁边就是 `TODO: check 4` / `seems hard code?`），用于残余路径较小 tile 时把分组数减半。读到时不必慌，这是已知硬编码，u5-l4 会回到它。

#### 4.4.4 代码实践

**实践目标**：通过对照阅读，固化「三套归约家族」的差异，并确认归约原语被复用的证据链。

**操作步骤**：

1. 打开 [softmax.h:120-155](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L120-L155)（`flash::quad_allreduce_`）与 [qpack.h:48-84](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L48-L84)（`quant::allreduce_`）并排对比，逐行回答：写入槽位的 guard、汇总线程的范围、最终读回的列号有何不同？
2. 在 [flash_fwd_kernel.h:403](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L403) 处向上滚动，找到残余 kernel 中 `sReduce_tmp` 的构造行，确认它与 qpack kernel 用的是同一种 `SmemLayoutReduce_tmp`（注意区分：解码主 kernel 的 traits 里还有另一个 8×32、带 swizzle 的版本，见 [kernel_traits.h:279-282](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L279-L282)，那是给 softmax 用的）。

**需要观察的现象**：`flash` 版槽位是 `(i, warp_id)`（每 warp 一槽、4 槽）、lane0 单线程汇总、全员读 `(i,0)`；`quant` 版槽位是 `(i, warp_id*4+lane_id)`（16 槽）、16 线程冗余汇总、按 `lane%4` 读回。

**预期结果**：你能用自己的话写出三行总结（分组轴 / 槽位编码 / 结果分发）。本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`quant::thread_reduce_` 与 `flash::thread_reduce_` 同名不同义，最本质的签名差异是什么？

**答案**：量化版多一个 `num_params` 参数，输出 summary 长度是 `size<0>(tensor) × num_params`（每组一个摘要）；flash 版每行只产出一个摘要（`summary(mi) = op over 整行`），因为 softmax 只需要行级统计量。

**练习 2**：`quad_allreduce_g` 用 `__shfl_sync`（定向收集）而不用 `__shfl_xor_sync`（蝴蝶），各自适合什么场景？

**答案**：定向收集 `__shfl_sync(..., group_base + offset)` 语义直观，适合**小组、低步数**（quad 只 4 线程、4 次收集）；蝴蝶 `__shfl_xor_sync` 每步让所有参与线程同时交换，\(\log\) 步完成，适合**大组**（本讲 8 线程类用 3 步；warp 级 32 线程用 5 步）。

**练习 3**：为什么 k-tensor 模式可以用 quad（连续 4 线程）归约，而 k-channel 必须用「`lane%4` 类」归约？

**答案**：两种模式的量化分组轴不同。k-tensor 的组落在**连续的通道块**上，恰好由连续 4 线程的寄存器拼成，quad 内合并即覆盖整组；k-channel 的组沿**序列**方向、参数逐通道维，不同通道维散布在 `lane%4` 不同的线程里，必须保类归约（若混类，逐维 scale 就被合并而失真）。

## 5. 综合实践

**任务**：把三段原语串成完整链路，端到端验证「`thread_reduce_` + `warp_reduce` + `allreduce_` 的输出 == 按 (类, 组, 寄存器行) 语义直接计算的参考答案」。

在 4.3.4 程序的基础上扩展（示例代码思路）：

1. 每线程持有一个 `(4, 16)` 的模拟寄存器 tile（`data[t][r][ni]`，随机浮点），设定 `num_params = 4`（4-bit、group_size=32，`pack_num = 4`）；
2. 在 kernel 内先做 `thread_reduce_`（两层 for 循环按 `mi` 折叠，可直接照抄 [qpack.h:16-27](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L16-L27) 的下标算术），得到 16 个部分和，再逐条目送入 4.3.4 已复刻的 `warp_reduce + allreduce_`；
3. CPU 参考答案：

\[ \text{ref}[t][4g+r] = \max_{\substack{t' :\, t'\%4 = t\%4 \\ n_i \in [4g,\,4g+4)}} \text{data}[t'][r][n_i] \]

4. 逐元素比对 kernel 输出 `out[t][s]` 与 `ref[t][s]`，打印最大偏差；
5. （选做）把 `MaxOp` 换成 `MinOp` 再跑一遍，验证同一套骨架对任意满足结合律、幂等的 op 都成立；再换成加法 `SumOp`，观察哪里会出错（提示：`thread_reduce_` 首元素重复 `op`、4 warp 冗余副本被重复相加——这正是该实现只用于 max/min 的原因）。

**预期结果**：max/min 两种 op 全部通过（偏差为 0 或浮点舍入量级）；加法版本失败，且你能定位到两处结构性重复计数。若在无 GPU 环境下完成，用 NumPy 模拟三段流程同样可以得出结论（此时**待本地验证**的只是 CUDA 行为，下标语义已由模拟覆盖）。

## 6. 本讲小结

- 量化前的分组极值由三段原语完成：`thread_reduce_`（寄存器内按 `mi/4` 分组、`mi%4` 定行，摘要布局 `s = 4g + r`）→ `warp_reduce`（`lane%4` 类内 3 步蝴蝶）→ `allreduce_`（`reduce_tmp` 32×32 共享内存跨 4 warp 汇总）。
- `warp_reduce` 的 guard `(lane^mask) % 4 == group_pos` 是灵魂：mask 16/8/4 保留低 2 位（真正合并，覆盖 8 个同类线程），mask 2/1 必然跳类（洗牌执行但丢弃），从而保住「逐通道维」的归约语义。
- `allreduce_` 的读回 `dst(i) = reduce_tmp(i, lane%4)` 决定了**结果是按类分发的**——每个 `lane%4` 类拿到自己 4 个通道维的最终极值；这与 FA 原版 `quad_allreduce_` 的「全员统一值」是本质区别。
- 输出形状 `4·num_params` 与量化分组一一对应：每个 16 维切片产出 `4 类 × 4 行 × num_params 组 = 16·num_params` 个 (组, 维) 参数；写盘与解码读回使用镜像的 `8i + 4·(j/num_params) + tidx%4` 公式保证自洽。
- 同一套原语在 prefill 的 qpack kernel 与 decode 的残余 kernel（原位再量化）两处复用；k-tensor 模式另有 `thread_reduce_g + quad_allreduce_g`（连续 4 线程组、`__shfl_sync` 定向收集）的姊妹实现。
- 工程细节：`reduce_tmp` 第一维上限 32 限制了 `num_params ≤ 8`（即 group_size 下限）；`allreduce_` 硬编码 4 warp；`__syncthreads()` 在循环内高频出现（512 次/次调用）——qpack 非热点，实现以正确简单为先。

## 7. 下一步学习建议

下一讲 **u4-l3（量化与打包落盘：qpack_kc_vt、quant_Ktensor 与 pack 存储）**将沿着本讲的产出继续：拿到 `channel_max/channel_min` 之后如何计算 `scale/zero`（本讲 4.4.3 已剧透一半），如何把 `round + clamp + 位拼接` 的量化值压进 `uint16`，以及 `pack_Kchannel_store / pack_Vtensor_store` 如何把寄存器结果经共享内存写回显存。建议先自行阅读 [qpack.h:219-252](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L219-L252) 中 scale/zero 的计算，带着「`scales_inv = max_val/range`、`zero = min`」的印象去读反量化侧 [dequantize.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h) 的 `__hfma2` 还原公式，验证二者互逆。之后 u5 单元将看到这些打包数据如何在解码 kernel 里被 LOP3 一次反量化进 Tensor Core。
