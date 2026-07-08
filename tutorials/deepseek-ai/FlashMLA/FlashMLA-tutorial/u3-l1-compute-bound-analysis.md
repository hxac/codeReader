# MLA 解码的 compute-bound 理论分析

> 本讲属于 **Unit 3：Dense Decoding Kernel（SM90）核心** 的第 1 讲。
> 在正式钻进 `splitkv_mla.cuh` 的 seesaw 调度之前，我们先用一篇纯理论的讲义回答一个最根本的问题：
> **为什么一个"解码阶段"的注意力 kernel，瓶颈居然在算力（compute）而不在带宽（memory）？**
> 这个结论会直接决定后续 u3-l2、u3-l3 的优化方向——一切优化都以"喂饱 Tensor Core"为目标。

## 1. 本讲目标

学完本讲，你应当能够：

1. 对任意一个 `(b, h_q, s_q, s_k, d_k, d_v)` 配置，独立推导出 MLA 解码的 **FLOPs（浮点运算量）** 与 **访存量（bytes）**。
2. 算出该配置的 **算术强度（arithmetic intensity，FLOP/byte）**，并用 **Roofline 模型** 判定它是 compute-bound 还是 memory-bound。
3. 说清楚为什么 DeepSeek 在线推理的解码（`h_q = 128, s_q = 1`，且不做 Tensor Parallel）会落在 compute-bound 一侧，分界点为何是 `h_q·s_q ≈ 128`。
4. 理解这个判定如何解释后续讲义中"为什么 FlashMLA 新 kernel 要把 CUDA Core 与 Tensor Core 交错起来"的设计动机。

本讲 **不涉及 CUDA 代码细节**，只做理论分析，是后续 u3-l2（config/traits）、u3-l3（seesaw 调度）的理论铺垫。

## 2. 前置知识

本讲承接 u1-l1（项目定位与 MLA 背景）和 u2-l1（调用链全景）。在进入推导前，先回顾几个在本讲会被反复使用的概念。

### 2.1 compute-bound 与 memory-bound

任何一段在 GPU 上运行的代码，其运行时间最终由两类硬件资源中的"较慢者"决定：

- **计算资源（Tensor Core / CUDA Core）**：每秒能完成多少浮点运算，单位 TFlops。
- **访存资源（HBM 带宽）**：每秒能从显存搬运多少字节，单位 GB/s（或 TB/s）。

如果一个 kernel 的时间主要花在"算"上，称为 **compute-bound**；如果主要花在"等数据从显存搬过来"上，称为 **memory-bound**。

判定方法是经典的 **Roofline 模型**：定义 **算术强度**

\[ I \;=\; \frac{\text{FLOPs}}{\text{bytes}} \quad (\text{单位：FLOP/byte}) \]

它表示"每从显存搬运 1 字节，能换来多少次浮点运算"。设 GPU 的峰值算力为 \(P\)（FLOP/s）、峰值带宽为 \(B\)（byte/s），则 **平衡点（balance point）** 为

\[ I^\ast \;=\; \frac{P}{B} \quad (\text{单位：FLOP/byte}) \]

- 当 \(I \ge I^\ast\)：算得过来但搬不过来 → **compute-bound**，理论上限受 \(P\) 制约。
- 当 \(I < I^\ast\)：搬得过来但算不过来 → **memory-bound**，理论上限受 \(B\) 制约。

> 直觉理解：算术强度越高，说明"一次性搬进来的数据能反复用好多次"，搬运就不亏，瓶颈自然转到算力上。

### 2.2 MLA 解码的形状参数（承接 u1-l1）

MLA（Multi-head Latent Attention）与普通 MHA 的关键区别是：**K 和 V 同源**——KV cache 里只缓存压缩后的潜在向量，K 和 V 只是同一段数据的不同名字/不同切片。这在 u2-l1、u1-l4 中都已建立。本讲用到的形状参数（与 `tests/test_flash_mla_dense_decoding.py` 的 `TestParam` 一致）：

| 符号 | 含义 | MLA 解码典型值 |
|------|------|----------------|
| \(b\) | batch（请求数） | 128 |
| \(h_q\) | q 头数 | **128**（DeepSeek 不做 TP） |
| \(h_{kv}\) | kv 头数 | 1（MQA 形式） |
| \(s_q\) | 每条请求的 q token 数 | 1（关闭 MTP / 投机解码时） |
| \(s_k\) | 每条请求的 kv token 数 | 数千 ~ 数万 |
| \(d_k\) | K 的头维度 | 576（含 64 维 RoPE） |
| \(d_v\) | V 的头维度 | 512 |

一个关键的不等式贯穿整篇推导：

\[ s_k \;\gg\; h_q \, s_q \]

即 KV 序列远比 Q 序列长。这正是"解码"阶段的特征（一次只生成很少几个 token，但要 attend 到很长的上下文）。

## 3. 本讲源码地图

本讲的论据只有两份文件：一份是讲"为什么 compute-bound"的官方博客，一份是把 FLOPs/bytes 落地成可测公式、并打印 TFlops/GB/s 的测试代码。

| 文件 | 作用 |
|------|------|
| [docs/20250422-new-kernel-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md) | 官方深度博客。第 1 节 "A Theoretical Analysis of the MLA Algorithm" 给出 FLOPs、bytes、算术强度、H800 峰值与 `h_q·s_q ≈ 128` 分界点的全部原始推导。 |
| [tests/test_flash_mla_dense_decoding.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py) | dense 解码测试。`TestParam` 固化 MLA 形状常量；`test_flash_mla` 的性能分支把博客公式翻译成可执行的 `compute_volume_flop` / `memory_volume_B` / `achieved_tflops` / `achieved_gBps`。 |

> 一句话：博客给"思想"，测试给"可复算的公式"。本讲会把两者对齐，确保你看到的每个数字都能在仓库里找到出处。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先算 FLOPs 与访存量（4.1），再算它们的比值即算术强度（4.2），最后用 Roofline 判定 bound 并得出分界点（4.3）。

### 4.1 FLOPs 与访存量推导

#### 4.1.1 概念说明

标准注意力（缩放点积注意力）对一个请求的计算分三步：

1. \(S = Q K^\intercal\)：query 与 key 做点积，得到注意力分数。
2. \(P = \mathrm{softmax}(S)\)：对分数做 softmax，得到注意力权重。
3. \(O = P V\)：用权重对 value 加权求和，得到输出。

对 MLA 解码，我们要算的是 **一个请求** 的总浮点运算量和总访存量，然后再推广到 batch。需要特别注意的是：**softmax 本身的 exp/求和几乎不贡献 FLOPs**（相对两个矩阵乘法而言），所以业界惯用"两次矩阵乘的 FLOPs"来近似整个注意力的 FLOPs——博客和测试都是这么做的。

#### 4.1.2 核心流程

记一个请求里 Q 的形状为 \([h_q, s_q, d_k]\)，K 的形状为 \([s_k, d_k]\)（MQA 下 \(h_{kv}=1\)），V 取这段潜在向量的前 \(d_v\) 维。

**FLOPs（两次矩阵乘）：**

- \(Q K^\intercal\)：形状 \([h_q, s_q, d_k] \times [d_k, s_k] \to [h_q, s_q, s_k]\)。每个输出元素需要 \(d_k\) 次乘加（1 次 multiply-add = 2 FLOPs），共 \(2 \cdot h_q s_q d_k s_k\) FLOPs。
- \(P V\)：形状 \([h_q, s_q, s_k] \times [s_k, d_v] \to [h_q, s_q, d_v]\)，共 \(2 \cdot h_q s_q s_k d_v\) FLOPs。
- 合计：

\[ \boxed{\;\text{FLOPs} \;=\; 2\,h_q s_q s_k (d_k + d_v)\;} \]

**访存量（bytes，bf16 即每个元素 2 字节）：**

- 读 Q：\(h_q s_q d_k\) 元素。
- 读 K：\(s_k d_k\) 元素。**MLA 的关键**——V 就是 K 的前 \(d_v\) 维，K 只需加载一次，V 不产生额外访存。
- 写 O：\(h_q s_q d_v\) 元素。

\[ \text{bytes} \;=\; 2\bigl(\,h_q s_q d_k \;+\; s_k d_k \;+\; h_q s_q d_v\,\bigr) \]

由于解码阶段 \(s_k \gg h_q s_q\)，与 \(s_k d_k\) 相比，\(h_q s_q d_k\) 和 \(h_q s_q d_v\) 都是高阶小量，于是：

\[ \boxed{\;\text{bytes} \;\approx\; 2\,s_k d_k\;} \]

直觉：解码时大部分访存都花在"把那一条长长的 KV 潜在向量从显存搬进来"上，Q 和输出相对可以忽略。

#### 4.1.3 源码精读

博客用一段紧凑的文字给出完全相同的两个公式（FLOPs 与 bytes），并把 \(2\,\text{sizeof}(\text{bfloat16})\) 直接写成系数 2：

[docs/20250422-new-kernel-deep-dive.md:11](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L11) —— 给出 `FLOPs ≈ 2 h_q s_q s_k (d_k+d_v)`、`bytes ≈ 2 s_k d_k`，以及它们的比值。这段是本讲全部推导的原始出处。

> 注意博客在 bytes 里写的是 `sizeof(bfloat16) × (h_q s_q d_k + s_k d_k + h_q s_q d_v)`，与我们 4.1.2 的展开完全一致；随后用 `\approx 2 s_k d_k` 丢掉了 Q 与 O 项，正是"解码阶段 \(s_k \gg h_q s_q\)"的体现。

测试代码则把博客的 FLOPs 公式落地成可执行表达式。注意它对一个请求用 `mean_attended_seqlens` 代替固定的 \(s_k\)（因为 varlen 下每条请求实际 attend 的长度不同），并按 batch 求和：

[tests/test_flash_mla_dense_decoding.py:178-181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L178-L181) —— `compute_volume_flop = b * h_q * s_q * [2*d*seq + 2*seq*dv]`，即 `b·h_q·s_q·seq·2·(d+dv)`。这里 `d` 就是 \(d_k=576\)，`dv` 就是 \(d_v=512\)，与博客公式逐项对应。

而 MLA 的形状常量被固化在 `TestParam` 的默认值里，方便后面所有性能用例直接复用：

[tests/test_flash_mla_dense_decoding.py:22-26](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L22-L26) —— `h_q=128, h_kv=1, d=576, dv=512, block_size=64`。这组默认值就是 DeepSeek 解码的标准配置。

> 对照点：博客说"in MLA, K and V are the same with different names"（见 [docs/20250422-new-kernel-deep-dive.md:44](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L44)），这正是 4.1.2 中"K 只加载一次、V 不额外访存"的依据。如果没有这条性质，bytes 里会多出一项 \(s_k d_v\)，整个 bound 判定都会变。

#### 4.1.4 代码实践

**实践目标**：用纯 Python 复刻测试里的 `compute_volume_flop` 和 `memory_volume_B` 公式，对一组配置算出 FLOPs 与 bytes，确认你能把博客公式和代码公式对齐。**本步无需 GPU，纯数值计算。**

**操作步骤**：把下面的示例代码存成一个 `.py` 文件并运行（`elem = 2` 就是 bf16 的字节数，无需导入 torch）。

```python
# 示例代码：复刻 tests/test_flash_mla_dense_decoding.py 的体积公式
b, s_q, s_k = 128, 2, 8192
h_q, h_kv   = 128, 1
d, dv       = 576, 512      # d = d_k, dv = d_v
mean_seq    = s_k           # 简化：假设每条请求实际 attend 长度 == s_k

# —— FLOPs：照搬测试 L178-L181 ——
compute_volume_flop = b * h_q * s_q * sum([
    2 * d * mean_seq,        # Q * K^T
    2 * mean_seq * dv,       # attention * V
])
# 等价于：b * h_q * s_q * mean_seq * 2 * (d + dv)

# —— bytes：照搬测试 L182-L188 ——
elem = 2                     # sizeof(bfloat16)
kv_token_size = d * elem     # MLA 潜在向量只按 d_k 计字节数
memory_volume_B = b * sum([
    s_q * h_q * (d  * elem),          # Q
    mean_seq * h_kv * kv_token_size,  # K/V（潜在向量只加载一次）
    s_q * h_q * (dv * elem),          # Output
])

print("compute_volume_flop =", compute_volume_flop)
print("memory_volume_B     =", memory_volume_B)
print("FLOPs / batch       = %.4g" % (compute_volume_flop / b))
print("bytes  / batch      = %.4g" % (memory_volume_B / b))
```

**需要观察的现象**：

- `compute_volume_flop` 应为 \(128 \times 128 \times 2 \times 8192 \times 2 \times 1088 = 584\,115\,552\,256\)，即整个 batch 约 0.584 TFLOPs 的总运算量。
- `memory_volume_B` 应为 \(128 \times (294\,912 + 9\,437\,184 + 262\,144) = 1\,279\,262\,720\)，即整个 batch 约 1.279 GB 的总访存量。
- 注意"每请求"的访存里，K/V 那一项（\(9.44\times10^6\) 字节）远大于 Q（\(2.95\times10^5\)）和 O（\(2.62\times10^5\)），印证"解码访存被 KV 主导"。

**预期结果**：脚本输出

```
compute_volume_flop = 584115552256
memory_volume_B     = 1279262720
FLOPs / batch       = 4.563e+09
bytes  / batch      = 9.994e+06
```

如果手头有 H800，可进一步在测试里把 `b=128, s_q=2, s_k=8192` 这组配置真正跑起来，对比脚本算出的"体积"与实测 kernel 时间反推出的"达成 TFlops/GB/s"（见 4.3.4）。**无 GPU 时，体积公式的数值是确定可复算的；实测耗时则待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：若把 dtype 从 bf16 换成 fp16，FLOPs 与 bytes 各自如何变化？

**答案**：FLOPs **不变**（矩阵乘的运算次数与精度无关）；bytes 也**不变**（fp16 与 bf16 都是 2 字节/元素）。因此本讲的 bound 判定对 bf16/fp16 通用——这也正是测试支持 `--dtype fp16` 的原因。

**练习 2**：为什么 bytes 公式里没有单独的 "读 V" 这一项？

**答案**：因为 MLA 中 V 是 K 的前 \(d_v\) 维（K、V 同源），K 被加载一次后，V 已经在寄存器/共享内存里了，不需要再从显存搬运。如果换成普通 MHA（K、V 各自独立），bytes 里就要再加一项 \(s_k d_v\)。

---

### 4.2 计算访存比：算术强度

#### 4.2.1 概念说明

有了 FLOPs 和 bytes，下一步是把它们相除得到 **算术强度** \(I = \text{FLOPs}/\text{bytes}\)（回顾 2.1）。算术强度是一个 **与 batch、与 \(s_k\) 几乎无关** 的量——这是 MLA 解码最反直觉、也最重要的性质，是"compute-bound"结论的真正来源。

#### 4.2.2 核心流程

用 4.1 的两个 boxed 公式直接相除：

\[ I \;=\; \frac{\text{FLOPs}}{\text{bytes}} \;\approx\; \frac{2\,h_q s_q s_k (d_k+d_v)}{2\,s_k d_k} \;=\; h_q s_q \cdot \frac{d_k+d_v}{d_k} \]

代入 MLA 的维度 \(d_k=576,\ d_v=512\)：

\[ \frac{d_k+d_v}{d_k} \;=\; \frac{576+512}{576} \;=\; \frac{1088}{576} \;\approx\; 1.889 \]

所以 MLA 解码的精确算术强度约为

\[ I \;\approx\; 1.889 \times h_q s_q \]

博客为了手算方便，把 \((d_k+d_v)/d_k\) 进一步近似成 2（即把 \(d_v\) 也当成 \(\approx d_k\)）：

[docs/20250422-new-kernel-deep-dive.md:11](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L11) —— 博客结尾写 `compute-memory ratio ... ≈ 2 h_q s_q`。这就是著名的"`2 h_q s_q`"近似。

> 两种写法的差异：精确 MLA 是 \(1.889\,h_q s_q\)，博客近似是 \(2\,h_q s_q\)，相对误差约 6%。对"判定 bound"这种数量级问题完全可以接受，但你在自己复算时要心里有数（4.3 节会同时给出两个阈值）。

**关键洞察**：\(I\) 只依赖 \(h_q s_q\)（和常数比例），**既不依赖 batch \(b\)，也不依赖序列长度 \(s_k\)**。换句话说：

- 把 batch 开大、把上下文开长，FLOPs 和 bytes 会 **同比例** 增长，算术强度不变。
- 真正能改变 bound 归属的，只有"一次解码里有多少 query 头 × 多少 query token"，也就是 \(h_q s_q\)。

这就是为什么 DeepSeek 的"不做 TP（\(h_q=128\)）"会从根上把解码推向 compute-bound。

#### 4.2.3 源码精读

测试代码并没有显式算算术强度，但它在性能分支里分别算了 `compute_volume_flop` 和 `memory_volume_B`，再用 **实测 kernel 时间** 反推出"达成 TFlops"和"达成 GB/s"——这两个达成率的相对大小，就是 bound 归属的实测判据：

[tests/test_flash_mla_dense_decoding.py:182-188](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L182-L188) —— `memory_volume_B` 的计算。注意 K/V 行用的是 `kv_token_size = d * itemsize`（按 \(d_k\) 计），与 4.1 的 MLA 性质一致；Q 行用 \(d_k\)、Output 行用 \(d_v\)。

[tests/test_flash_mla_dense_decoding.py:189-192](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e457e089f888280e0/tests/test_flash_mla_dense_decoding.py#L189-L192) —— 用 `time_usage` 把体积换算成 `achieved_tflops` 与 `achieved_gBps` 并打印。

> 想直接拿到算术强度，只要把 4.1.4 脚本里的两个体积相除即可。注意：测试的 `memory_volume_B` 没有丢弃 Q/O 项（不像博客的 `\approx 2 s_k d_k`），所以用测试公式算出的 \(I\) 会比博客的 `2 h_q s_q` 略小——这是"精确"与"近似"之差，不是矛盾。

#### 4.2.4 代码实践

**实践目标**：在 4.1.4 脚本末尾追加几行，算出算术强度，并验证"它几乎不随 \(s_k\) 变化"。

**操作步骤**：

```python
# 示例代码：在 4.1.4 脚本基础上追加
ai = compute_volume_flop / memory_volume_B
print("arithmetic intensity (FLOP/byte) = %.1f" % ai)
print("blog approx  2*h_q*s_q            =", 2 * h_q * s_q)
print("exact MLA   h_q*s_q*(d+dv)/d      = %.1f" % (h_q * s_q * (d + dv) / d))

# 验证：把 s_k 翻倍，算术强度应几乎不变
for s_k_test in [4096, 8192, 16384, 32768]:
    flop = b * h_q * s_q * s_k_test * 2 * (d + dv)
    byt  = b * (s_q * h_q * d * elem + s_k_test * h_kv * d * elem + s_q * h_q * dv * elem)
    print(f"s_k={s_k_test:6d}  I={flop/byt:7.1f} FLOP/byte")
```

**需要观察的现象**：

- 对配置 `b=128, s_q=2, s_k=8192`，用测试公式算出的 \(I \approx 456.6\) FLOP/byte（因为测试保留了 Q/O 项）；博客近似 `2·h_q·s_q = 2·128·2 = 512`；精确 MLA `h_q·s_q·(d+dv)/d = 256·1.889 = 483.6`。三者同数量级。
- 把 `s_k` 从 4096 扫到 32768，\(I\) 几乎不变（在 ~456 附近微小波动，随 \(s_k\) 增大略微趋近 483.6，因为 Q/O 占比被稀释）。

**预期结果**：

```
arithmetic intensity (FLOP/byte) = 456.6
blog approx  2*h_q*s_q            = 512
exact MLA   h_q*s_q*(d+dv)/d      = 483.6
s_k=  4096  I=  432.7 FLOP/byte
s_k=  8192  I=  456.6 FLOP/byte
s_k= 16384  I=  470.0 FLOP/byte
s_k= 32768  I=  476.8 FLOP/byte
```

> （`s_k` 扫描那一栏的具体数字为推算值，待本地验证；定性结论"随 \(s_k\) 增大趋近 483.6"是确定的。）

**预期结论**：算术强度由 \(h_q s_q\) 主导，与 batch、与 \(s_k\) 基本解耦。

#### 4.2.5 小练习与答案

**练习 1**：若一个推理服务把 Q 头数从 128 砍到 32（例如做了 4 路 Tensor Parallel），算术强度会变成多少？

**答案**：\(I\) 与 \(h_q\) 成正比。\(h_q=32,\ s_q=1\) 时 \(h_q s_q = 32\)，精确 \(I \approx 1.889 \times 32 \approx 60\) FLOP/byte——比 128 头时小 4 倍。这正是"做 TP 会把解码拉回 memory-bound"的量化体现，也是 DeepSeek 选择"解码不做 TP"的理论依据（见 4.3.3）。

**练习 2**：为什么增大 batch \(b\) 不能把一个 memory-bound 的解码变成 compute-bound？

**答案**：因为 \(I\) 与 \(b\) 无关。增大 batch 只会让 FLOPs 和 bytes 同比放大，算术强度不变，bound 归属不变。要改变 bound，只能改变 \(h_q s_q\)（或换 dtype/量化以改变 bytes，那是 u5 的话题）。

---

### 4.3 memory/compute bound 判定与分界

#### 4.3.1 概念说明

最后一步：把算术强度 \(I\) 拿去和 H800 的 **平衡点** \(I^\ast = P/B\) 比较。这一步需要知道 H800 SXM5 的两个峰值指标。本节会得出本讲的核心结论——**分界点 \(h_q s_q \approx 128\)**，并解释 DeepSeek 解码为何落在 compute-bound 一侧。

#### 4.3.2 核心流程

**第 1 步：H800 SXM5 的峰值。** 博客给出：

- 峰值带宽 \(B = 3.35\) TB/s。
- 峰值算力：标称 \(P_{\text{theory}} = 990\) TFlops；但由于 **降频**（throttling，实际降到约 1600 MHz），**可用峰值** \(P \approx 865\) TFlops。

[docs/20250422-new-kernel-deep-dive.md:13](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L13) —— H800 峰值与"throttle 到 ~865 TFlops"的原始表述。注意博客用的是 **降频后的 865 TFlops** 而非标称 990，这样得出的分界点才贴近真实部署。

**第 2 步：算平衡点。**

\[ I^\ast \;=\; \frac{P}{B} \;=\; \frac{865\ \text{TFlops}}{3.35\ \text{TB/s}} \;=\; \frac{865}{3.35} \;\approx\; 258\ \text{FLOP/byte} \]

**第 3 步：代入 MLA 算术强度求分界。** 令 \(I = I^\ast\)：

- 用博客近似 \(I \approx 2\,h_q s_q\)：\(2\,h_q s_q = 258 \Rightarrow h_q s_q \approx 129 \approx 128\)。
- 用精确 MLA \(I \approx 1.889\,h_q s_q\)：\(1.889\,h_q s_q = 258 \Rightarrow h_q s_q \approx 137\)。

两种取法都落在同一个数量级。博客采用近似版，写成：

[docs/20250422-new-kernel-deep-dive.md:13](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L13) —— 结论行 `when h_q s_q ≥ (1/2)·(865/3.35) = 128, the kernel is compute-bound; otherwise memory-bound`。式中的 \(1/2\) 正是"算术强度 ≈ \(2 h_q s_q\)"里那个 2 的倒数。

最终判定规则（本讲核心结论）：

\[ \boxed{\;h_q s_q \;\gtrsim\; 128 \;\Rightarrow\; \text{compute-bound};\qquad h_q s_q \;\lesssim\; 128 \;\Rightarrow\; \text{memory-bound}\;} \]

#### 4.3.3 源码精读：为什么 DeepSeek 解码必然 compute-bound

博客用一句话点明了 DeepSeek 在线推理的部署约束，并由此推出 \(h_q\)：

[docs/20250422-new-kernel-deep-dive.md:15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L15) —— 引用 DeepSeek 在线推理系统概览："解码实例不做 Tensor Parallel，因此 \(h_q = 128\)，kernel 是 compute-bound"。

把这条事实串进判定规则：

- 解码阶段 \(s_q = 1\)（关闭 MTP / 投机解码）。
- 不做 TP \(\Rightarrow\) 单卡承担全部 128 个 q 头，\(h_q = 128\)。
- 于是 \(h_q s_q = 128 \times 1 = 128\)，恰好踩在分界点上，工程上按 **compute-bound** 处理。

> 反向印证练习 4.2.5-1：如果改成 4 路 TP，\(h_q = 32\)，\(h_q s_q = 32 < 128\)，立刻跌回 memory-bound。所以"解码不做 TP"不是随便选的，它正是为了让 \(h_q s_q\) 突破 128 这个分界点、把瓶颈稳定地推到算力侧——因为算力侧还有 Tensor Core 这个"大杀器"可以被优化喂饱，而带宽侧已经接近物理极限。

#### 4.3.4 代码实践：对给定配置做 Roofline 判定

**实践目标**：对本讲规格里指定的配置 `b=128, s_q=2, s_k=8192, d=576, dv=512`（\(h_q\) 取 MLA 标准 128），复现博客的 FLOPs/byte 推导，算出理论 TFlops 与 GB/s，判定 bound 归属。**全程无需 GPU，纯 Roofline 推算。**

**操作步骤**：在 4.2.4 脚本基础上继续追加 Roofline 段。

```python
# 示例代码：Roofline 判定（接 4.1.4 / 4.2.4 脚本）
peak_BW   = 3.35e12   # H800 SXM5 峰值带宽 B/s
peak_FLOP = 865e12    # H800 降频后可用峰值 FLOP/s

balance = peak_FLOP / peak_BW
print("balance point I* = %.1f FLOP/byte" % balance)
print("config I        = %.1f FLOP/byte" % ai)
print("compute-bound?  ", ai >= balance)

# 理论下界时间：受限于算力或带宽中的较慢者
t_comp = compute_volume_flop / peak_FLOP
t_mem  = memory_volume_B / peak_BW
print("t_comp = %.3f ms" % (t_comp * 1e3))
print("t_mem  = %.3f ms" % (t_mem * 1e3))

# 在该下界时间下反推两项"理论达成率"
t = max(t_comp, t_mem)
print("theoretical TFlops = %.0f" % (compute_volume_flop / t / 1e12))
print("theoretical GB/s   = %.0f" % (memory_volume_B / t / 1e9))
```

**需要观察的现象**：

- 平衡点 \(I^\ast = 865/3.35 \approx 258\) FLOP/byte。
- 该配置 \(h_q s_q = 128 \times 2 = 256\)，算术强度（测试公式）\(I \approx 456.6\) FLOP/byte，**远大于 258**。
- `t_comp = 0.675 ms` 明显大于 `t_mem = 0.382 ms`，说明算力才是瓶颈。
- 在受限于算力的下界时间下，"理论 TFlops"撑满 865（即峰值），而"理论 GB/s"只能达到约 1894（仅占 3350 峰值的 ~57%）——带宽没用满是 compute-bound 的铁证。

**预期结果**：

```
balance point I* = 258.2 FLOP/byte
config I        = 456.6 FLOP/byte
compute-bound?   True
t_comp = 0.675 ms
t_mem  = 0.382 ms
theoretical TFlops = 865
theoretical GB/s   = 1894
```

**结论**：配置 `b=128, s_q=2, s_k=8192, d=576, dv=512` 是 **compute-bound**。

- 这里的 `theoretical TFlops = 865` 是"如果能把 Tensor Core 喂到降频峰值"的上限；博客报告新 kernel 在 compute-bound 场景实测可达 **约 660 TFlops**（[docs/20250422-new-kernel-deep-dive.md:3](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L3)），约等于 865 峰值的 76%，与博客"up to 80% Tensor Core utilization"（[docs/20250422-new-kernel-deep-dive.md:57](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L57)）自洽。
- 实测的 660 TFlops / 1894-ish GB/s **待本地验证**；本脚本给出的 865/1894 是 Roofline 上界，不是实测值。

#### 4.3.5 小练习与答案

**练习 1**：把 `s_q` 从 2 调成 1（即关闭 MTP，回到 DeepSeek 真实解码），其它不变。判定 bound 归属。

**答案**：\(h_q s_q = 128 \times 1 = 128\)，精确 \(I \approx 1.889 \times 128 \approx 242\) FLOP/byte，略低于平衡点 258；按博客近似 \(2 \times 128 = 256\) 则基本踩在 258 上。工程上 DeepSeek 把它当作 **compute-bound** 来优化（因为已处于分界点附近，且优化算力的收益更大）。这说明 `h_q s_q = 128` 是一个"临界"配置，也解释了为什么博客要把分界点精确地定在 128。

**练习 2**：博客里同时给了"旧版本 3000 GB/s（memory-bound 场景）"和"580/660 TFlops（compute-bound 场景）"两套数字。它们对应的是同一份 kernel 吗？为什么会有两套指标？

**答案**：是同一份 dense decode kernel，只是测的配置不同。当 \(h_q s_q < 128\)（如小 \(h_q\) 或做了 TP）时是 memory-bound，瓶颈在带宽，所以报 GB/s（3000 GB/s）；当 \(h_q s_q \ge 128\)（DeepSeek 部署）时是 compute-bound，瓶颈在算力，所以报 TFlops（660 TFlops）。**一个 kernel 同时被两套指标评价，正是因为它的 bound 归属会随 \(h_q s_q\) 翻转。** 详见 [docs/20250422-new-kernel-deep-dive.md:3](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L3) 与 [:57](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L57)。

**练习 3**：为什么博客强调"需要把 CUDA Core 与 Tensor Core 的操作交错（overlap），让 Tensor Core 一直忙"？

**答案**：因为 DeepSeek 解码是 compute-bound，瓶颈在 Tensor Core 的吞吐。但 attention 的 softmax / rescale 是标量操作，跑在 CUDA Core 上；如果 CUDA Core 和 Tensor Core 串行执行，Tensor Core 就会在 CUDA Core 干活时空转。把两者交错（seesaw 调度），就能在 CUDA Core 做 softmax 的同时让 Tensor Core 算下一个 GEMM，从而逼近 865 TFlops 的峰值。这正是 u3-l3 将要深入的主题，本讲为它提供了"为什么要这么做"的理论依据。

## 5. 综合实践

把本讲三个模块串起来，完成一次 **手算 Roofline 报告**（无需 GPU）：

**任务**：对 DeepSeek 真实解码配置 `b=128, s_q=1, s_k=8192, h_q=128, h_kv=1, d=576, dv=512`，产出一份一页纸的分析，包含：

1. **FLOPs 与 bytes**：写出每请求和整个 batch 的运算量、访存量（对照 4.1）。
2. **算术强度**：分别用"测试公式""精确 MLA""博客近似"三种方式算 \(I\)，解释三者差异（对照 4.2）。
3. **bound 判定**：给出 H800 平衡点 \(I^\ast\)，判定该配置的 bound 归属，并解释为何它"刚好"踩在分界点上（对照 4.3）。
4. **理论下界**：算出 `t_comp` 与 `t_mem`，指出哪个更大，并反推理论 TFlops / GB/s。
5. **设计含义**：用 2–3 句话说明，这个判定如何解释 u3-l3 将要讲的 seesaw 调度"为什么要重叠 CUDA Core 与 Tensor Core"。

**参考要点（用于自查）**：

- 每请求 FLOPs \(= 128 \times 1 \times 8192 \times 2 \times 1088 = 2.28\times10^9\)；bytes（测试公式，含 Q/O）\(\approx 9.72\times10^6\)。
- 整 batch FLOPs \(\approx 0.292\) TFLOPs，bytes \(\approx 1.24\) GB。
- \(I\)：测试公式 \(\approx 235\)、精确 MLA（主项）\(1.889\times128\approx 242\)、博客近似 \(2\times128=256\)，三者都贴着 \(I^\ast=258\)。
- \(I^\ast = 258\)，故该配置处于 **临界/略偏 memory-bound**，工程上仍按 compute-bound 优化（见练习 4.3.5-1）。
- `t_comp`（≈0.34 ms）与 `t_mem`（≈0.37 ms）几乎相等且 `t_mem` 略大——这正是"临界配置"的典型特征。

> 这个综合练习本质上就是把博客第 1 节的推导在 DeepSeek 真实配置上完整重走一遍。能独立做出这份报告，就达成了本讲全部三个学习目标。

## 6. 本讲小结

- 注意力的 FLOPs 主要是两次矩阵乘：\(\text{FLOPs} = 2\,h_q s_q s_k (d_k + d_v)\)；MLA 解码的访存被 KV 主导：\(\text{bytes} \approx 2\,s_k d_k\)（因为 K、V 同源，K 只加载一次）。
- 算术强度 \(I = \text{FLOPs}/\text{bytes} \approx h_q s_q \cdot (d_k+d_v)/d_k \approx 2\,h_q s_q\)，它 **只依赖 \(h_q s_q\)**，与 batch、与 \(s_k\) 基本无关。
- H800 SXM5 的 Roofline 平衡点 \(I^\ast = 865\ \text{TFlops} / 3.35\ \text{TB/s} \approx 258\) FLOP/byte（注意用降频后的 865 而非标称 990）。
- 分界点：\(h_q s_q \gtrsim 128\) 为 compute-bound，否则 memory-bound。
- DeepSeek 解码不做 TP（\(h_q=128\)）、关闭 MTP（\(s_q=1\)），\(h_q s_q = 128\) 踩在分界点上，按 compute-bound 优化。
- 正因 compute-bound，后续 kernel 优化（u3-l3 的 seesaw 调度）才把"重叠 CUDA Core 与 Tensor Core、喂饱 Tensor Core"作为首要目标——本讲是那一讲的理论前提。

## 7. 下一步学习建议

本讲只回答了 **"为什么是 compute-bound"**，还没有碰任何 CUDA 代码。建议按以下顺序继续：

1. **u3-l2 静态配置 config.h 与 traits.h（GMMA）**：看 kernel 为了"喂饱 Tensor Core"在编译期定了哪些 tile 常量（`BLOCK_SIZE_M`、`HEAD_DIM_K/V`），以及 Hopper 的 GMMA `TiledMMA`（ss/rs）如何为 seesaw 调度里两个 warpgroup 的本地/远程访问选型。
2. **u3-l3 Seesaw 调度与 TMA 流水**：本讲 4.3.5-3 留下的"为什么要重叠 CUDA Core 与 Tensor Core"在那里得到完整回答——`splitkv_mla.cuh` 的 seesaw 11 步调度、双 warpgroup 交错、细粒度 TMA 流水与 `EVICT_FIRST` cache hint。
3. **想看实测数字怎么来的**：回看 `tests/test_flash_mla_dense_decoding.py:174-192` 的性能分支，理解 `achieved_tflops`/`achieved_gBps` 是如何把本讲的"体积"除以实测 kernel 时间得到的，并尝试在 H800 上跑一组 `b=128, s_q=2, s_k=8192` 用例，对照本讲 4.3.4 的 Roofline 上界。
