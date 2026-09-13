# u2-l1 chunk 化算法骨架：bit-exact 的 torch 参考实现

## 1. 本讲目标

本讲精读 FlashKDA 唯一的"算法规格书"——[tests/torch_ref.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py)。读完本讲你应该能够：

1. 把 u1-l2 学到的**逐 token 递推**等价改写成 **CHUNK=16 的矩阵形式**，独立推导出 `k_decayed / q_decayed / k_inv / k_restored` 四个衰减变体以及 `L、Mqk、INV、U` 的来历；
2. 顺着 `torch_ref` 的三重循环说出每个 chunk、每个 head 上发生的完整计算序列；
3. 理解为什么这份参考实现要**逐操作模拟 kernel 的数值行为**（`fp32_fma`、`fp32_ex2_ftz`、`tanh.approx` sigmoid、bf16 量化点），以及它是如何支撑 `torch.equal` 级别 exact-match 测试的。

本讲是整个单元二的地基：后续 u2-l6/u2-l7 读 Kernel 1、u3-l4 读 Kernel 2 的 MMA 相位时，每一段 CUDA 代码都能回到本讲的某一行 PyTorch 上找对应。

## 2. 前置知识

### 2.1 你应该已经知道（来自前置讲义）

- **KDA 逐 token 递推**（u1-l2）：按通道遗忘 \( \tilde S_t = S_{t-1}\,\mathrm{diag}(e^{g_t}) \)，求差 \( u_t = \beta_t\,(v_t - \tilde S_t k_t^\top) \)，写入 \( S_t = \tilde S_t + u_t k_t^\top \)，再读出 \( o_t = S_t q_t^\top \)。注意顺序是"**先擦后写、写完再读**"——这决定了后文矩阵形式里对角线的去留。
- **flash_kda.fwd 的接口**（u1-l5）：`q/k/v/g` 是 `[B,T,H,128]` 的 bf16，`g/beta` 传激活前 logits，激活在 kernel 内完成。
- **bit-exact 测试**（u1-l3）：`tests/test.sh` 用 `torch.equal`（逐位相等，不是 `allclose`）比较 kernel 输出与 torch 参考输出。

### 2.2 本讲新需要的概念

| 术语 | 通俗解释 |
|---|---|
| **FMA（fused multiply-add）** | 一条指令算 \( c + a \times b \)，乘法和加法之间**不做中间舍入**，只在最后舍入一次。GPU 的 MMA/FMA 指令都是这样。`c + a * b` 写成两条 fp32 指令则舍入两次，结果可能差 1 个 ulp。 |
| **bf16（bfloat16）** | 1 位符号 + 8 位指数 + 7 位尾数。指数位与 fp32 相同（动态范围一样大），但尾数只有 8 位精度（含隐含位），相对精度约 \(2^{-8}\)。 |
| **ftz（flush-to-zero）** | 把下溢的次正规（subnormal）数直接冲成 0。PTX 的 `ex2.approx.ftz.f32` 就是带 ftz 的 2 为底指数近似指令。 |
| **ulps 与 exact-match** | 两个浮点数相差"最后几个单位"。`torch.equal` 要求 0 个 ulp 差别，因此参考实现必须复刻 kernel 的**每一步舍入**，包括加法的结合顺序。 |
| **inclusive cumsum** | `torch.cumsum` 默认包含当前元素：\( \mathrm{gc}_i = \sum_{j \le i} g_j \)。后文所有衰减因子都建立在 inclusive cumsum 上。 |
| **warp shuffle 蝶形归约** |一组线程用 `__shfl_xor_sync` 按 XOR 距离 8→4→2→1 两两相加求和。求和**顺序**影响 fp32 舍入，因此参考实现要按同样顺序求和。 |

### 2.3 为什么参考实现必须"逐操作模仿"

普通 PyTorch 代码（`torch.sigmoid`、`torch.exp`、fp32 分立的乘加）在 ulp 级别上与 GPU kernel 使用的近似指令（`tanh.approx.f32`、`ex2.approx.ftz.f32`、HMMA 的 fp32 单次舍入累加）**必然不一致**。而 KDA 的 chunk 计算里有矩阵求逆 \((I+L)^{-1}\)——一个对误差高度敏感的算子，1 ulp 的输入差异会被前代换放大成可观测的输出差异。所以 `torch.equal` 级别的测试只有一条路：让参考实现的每一步运算在**数值上与 kernel 完全同路径**。这就是 `torch_ref.py` 存在的意义——它不是"另一个实现"，而是**用 PyTorch 语法书写的 kernel 行为规格**。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [tests/torch_ref.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py)（248 行） | **主角**：bit-exact 参考实现 | 数值工具箱（L7-L122）+ 主循环 `torch_ref`（L129-L247） |
| [tests/test_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py) | 参考实现的消费者 | `test_fwd` 用 `torch.equal` 断言 kernel 与 ref 逐位一致（L260-L261） |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | 被模仿对象（Kernel 1） | 门控激活/cumsum（L306-L338）、decay 家族（L360-L475）、L/Mqk（L481-L507） |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | 被模仿对象（近似指令） | `ex2_approx_ftz_f32`（L38-L42）、`sigmoid_tanh_approx_f32`（L50-L53） |

一句话定位：`torch_ref.py` 从下往上分三段——**数值工具箱**（模拟硬件指令）→ **16×16 求逆**（模拟 warp 前代换）→ **主循环**（模拟 K1+K2 合起来的逐 chunk 语义）。

## 4. 核心概念与源码讲解

### 4.1 bit-exact 工具箱：用 PyTorch 复刻硬件指令

#### 4.1.1 概念说明

kernel 里用到的三条"非标准"数值指令，PyTorch 没有直接等价物：

1. **sigmoid 的 tanh 近似**：kernel 用 `tanh.approx.f32` 实现 \( \sigma(x) = \tanh(x/2)/2 + 1/2 \)；
2. **2 为底的指数近似**：kernel 用 `ex2.approx.ftz.f32`，且通过**预乘 \( \log_2 e \)** 把自然指数换底成 ex2；
3. **单次舍入的乘加**：kernel 的累加都是 FMA。

工具箱逐一给出对应物。这一节也顺带解释 `torch_ref` 主循环开头为什么长那样。

#### 4.1.2 核心流程

```text
tanh.approx.f32        ←→  sigmoid_ext（内联 CUDA，逐位同源）
ex2.approx.ftz.f32     ←→  fp32_ex2_ftz（exp2 + 次正规冲零）
一条 FMA c+a*b         ←→  fp32_fma（fp64 中间计算 + 一次舍入回 fp32）
warp 蝶形求和顺序       ←→  l2_normalize_kernel_match（分组 FMA + XOR 蝶形）
inv_fwd_subst_fused_1warp ←→ inv_fwd_subst_16（本讲当黑盒，u3-l1 精读）
```

#### 4.1.3 源码精读

**① tanh 近似的 sigmoid。** 参考实现干脆用 `load_inline` 编了一个小 CUDA 扩展，内联汇编与 kernel 完全相同：

```python
asm("tanh.approx.f32 %0, %1;" : "=f"(th) : "f"(xh));
output[idx] = th * 0.5f + 0.5f;
```

这段代码用 `tanh.approx.f32` 实现 \( \sigma(x)=\frac{1}{2}\tanh(\frac{x}{2})+\frac{1}{2} \)，与 kernel 侧 [csrc/smxx/utils.cuh:50-53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L50-L53) 的 `sigmoid_tanh_approx_f32` 逐位一致；见 [tests/torch_ref.py:11-20](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L11-L20)（kernel 函数体）与 [tests/torch_ref.py:31-38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L31-L38)（`load_inline` 注册）。它被用在两处：门控激活和 beta 激活。

**② ex2 + ftz。** [tests/torch_ref.py:47-52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L47-L52)：

```python
def fp32_ex2_ftz(x):
    ret = torch.special.exp2(x)
    ret = torch.where(ret.abs() < torch.finfo(torch.float32).tiny,
                      torch.zeros_like(ret), ret)
    return ret
```

模拟 `ex2.approx.ftz.f32`（[csrc/smxx/utils.cuh:38-42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L38-L42)）的两个特征：以 2 为底、结果小于 fp32 最小正常数（`tiny`≈1.18e-38）时冲零。文件顶部的常数 [tests/torch_ref.py:44](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L44) `LOG2E = 1.4426950408889634` 就是换底用的 \( \log_2 e \)：\( e^x = 2^{x \log_2 e} \)，预乘一次乘法就能用单条 ex2 指令完成自然指数。

**③ 单次舍入的 FMA。** [tests/torch_ref.py:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)：

```python
def fp32_fma(c, a, b):
    return (c.to(torch.float64) + a.to(torch.float64) * b.to(torch.float64)).to(torch.float32)
```

先在 fp64 里算 \( c + a b \)（fp64 精度足够高，中间不产生 fp32 可见误差），最后**一次性舍入**到 fp32——数值上等价于一条 fp32 FMA 指令。注意 `c + a * b` 直接写 fp32 是两次舍入，不等价。

**④ L2 归一化的求和顺序复刻。** [tests/torch_ref.py:62-78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L62-L78)：

```python
groups = x_f32.reshape(*x_f32.shape[:-1], 16, 8)      # 每行 128 → 16 组 × 8 元素
for i in range(8):
    partials = fp32_fma(partials, groups[..., i], groups[..., i])  # 线程内 8 元素 FMA 链
for offset in [8, 4, 2, 1]:
    indices = torch.arange(16, device=x.device) ^ offset
    partials = partials + partials[..., indices]      # XOR 蝶形归约
inv_norm = torch.rsqrt(partials[..., 0:1] + 1e-6)
return (x_f32 * inv_norm).to(x.dtype)
```

对照 kernel 的 [csrc/smxx/fwd_kernel1.cuh:265-303](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L265-L303)：每个线程管一行里连续的 8 个元素（`ELEMS_PER_THREAD=8`，一行 128 元素由 16 个线程分担），先在寄存器里做 8 步 FMA 得到部分和（kernel 的 `q_sq += qv * qv`），再用 `__shfl_xor_sync` 以距离 8→4→2→1 蝶形归约——参考实现的 reshape(16,8) + 两层循环**精确复刻了这个求和顺序**，连 `rsqrt(partials + 1e-6)` 里的 epsilon 都与 kernel 的 `rsqrtf(q_sq + 1e-6f)` 一致。归一化后乘回原值、舍入回 bf16。

**⑤ 16×16 求逆（黑盒）。** [tests/torch_ref.py:87-122](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L87-L122) 的 `inv_fwd_subst_16` 计算 \( (I+L)^{-1} \)：对角两个 8×8 块用 fp32 前代换（按 kernel 确定的 FMA 顺序），非对角块用两次 bf16 HMMA 合并。文件注释（[tests/torch_ref.py:81-85](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L81-L85)）明说它与 kernel 的 `inv_fwd_subst_fused_1warp` 逐位一致。本讲只把它当黑盒调用，算法细节留给 u3-l1。

#### 4.1.4 代码实践

**实践目标**：亲眼看到"两次舍入 vs 一次舍入"确实产生 ulp 差异，理解 `fp32_fma` 为什么必须存在。

**操作步骤**（示例代码，纯 CPU 即可运行）：

```python
# fp32_fma_demo.py —— 示例代码
import torch

def fp32_fma(c, a, b):
    return (c.double() + a.double() * b.double()).float()

torch.manual_seed(0)
c = torch.randn(1 << 20, dtype=torch.float32)
a = torch.randn(1 << 20, dtype=torch.float32)
b = torch.randn(1 << 20, dtype=torch.float32)

naive  = c + a * b        # 乘、加各舍入一次
fused  = fp32_fma(c, a, b)  # 只舍入一次
diff = (naive != fused)
print("不一致的元素个数:", diff.sum().item(), "/", c.numel())
print("最大绝对差:", (naive - fused).abs().max().item())
```

**需要观察的现象**：部分随机样本上 `naive` 与 `fused` 不相等，最大绝对差在 fp32 ulp 量级（约 1e-7 相对量级）；多数元素相等。

**预期结果**：差异数量大于 0——证明如果参考实现随手写 `c + a*b`，exact-match 必然失败。具体差异数目与平台相关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么不用 `torch.sigmoid` 而要专门编一个 CUDA 扩展？
**答案**：`torch.sigmoid` 走的是库厂商的精确/多项式实现，与 `tanh.approx.f32` 在 ulp 级别不同；而门控值 \( g \) 会进入 cumsum、再进入 exp，误差会沿着 chunk 内 16 个 token 累积并被 \((I+L)^{-1}\) 放大，`torch.equal` 会失败。内联汇编是保证逐位一致的唯一可靠办法。

**练习 2**：`fp32_fma` 为什么用 fp64 做中间计算就能模拟一条 fp32 FMA？
**答案**：fp32 FMA 的定义是"乘积与加数在无限精度下相加后只做一次 fp32 舍入"。fp64 有 52 位尾数，足以无损容纳两个 fp32 数的乘积（24+24=48 位）及加法结果，因此 `(fp64 算完) → round 到 fp32` 与硬件 FMA 的结果一致。

**练习 3**：数一数 4.1 节里参考实现一共复刻了哪几类"硬件行为"？
**答案**：四类——(a) 近似指令（`tanh.approx`、`ex2.approx.ftz`，含 ftz 冲零）；(b) 单次舍入的 FMA；(c) 归约/求和的**结合顺序**（L2 归一化的 FMA 链 + 蝶形顺序）；(d) 数据格式的量化点（bf16 存储边界，见 4.2-4.4 节）。

### 4.2 门控激活、cumsum 与 g_total

#### 4.2.1 概念说明

门控 \( g \) 是 KDA 的"遗忘旋钮"：每个 token、每个通道一个负数，\( e^{g} \) 就是该通道状态的保留率。u1-l2 已经给出激活公式，本讲关注它的**数值路径**与 chunk 内的**累积形式**：

\[ g_t = \text{lower\_bound} \cdot \sigma\!\left(e^{A_{log}} \odot (g^{raw}_t + \text{dt\_bias})\right), \qquad g_t \in (\text{lower\_bound},\, 0) \]

在 chunk 视角下，真正参与后续计算的是**包含式累积和**：

\[ \mathrm{gc}_i = \sum_{j \le i} g_j \quad (\text{inclusive cumsum}), \qquad g_{total} = \mathrm{gc}_{C-1} = \sum_{j} g_j \]

- \( e^{\mathrm{gc}_i} \)：token i 的 key/query 相对 chunk 起点"前向衰减"到时刻 i 的系数；
- \( e^{g_{total}} \)：整个 chunk 对**出口状态**的一次性衰减系数；
- 由于 \( g < 0 \)，cumsum 单调递减，\( e^{\mathrm{gc}} \le 1 \)。

为什么是 inclusive？因为递推顺序是"先擦（decay）后写、写完再读"——token i 自己的 \( g_i \) 在它自己的读写之前就已生效，所以自己的 key/query 也要乘上 \( e^{g_i} \)。这直接决定了 4.3 节里 `Mqk` 保留对角线、而 `L` 不保留。

#### 4.2.2 核心流程

```text
g_raw [*, H, D] bf16
  │  + dt_bias [H, D] fp32（广播到每个 token）        → fp32
  │  × a_log_exp = exp2(A_log·log2e) [H, 1]           → 换底：一次乘法 + 一条 ex2
  │  × sigmoid_tanh（tanh.approx）                     → fp32
  │  × (lower_bound·log2e)                             → 换底合并到线性系数
  ▼
g ∈ (lower_bound, 0)，逐 token 逐通道
  │  每 16 个 token 一组 cumsum(dim=0)（尾块补零行 g=0，cumsum 走平）
  ▼
g_cumsum [16, D]，g_total = g_cumsum[-1] [D]
```

#### 4.2.3 源码精读

**① 预处理与激活**（主循环之前，对整个序列一次性完成）：[tests/torch_ref.py:161-169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L161-L169)

```python
g = g.to(torch.float32) + dt_bias.unsqueeze(0)
a_log_exp = fp32_ex2_ftz(A_log * LOG2E).unsqueeze(0).unsqueeze(-1)
scale = lower_bound * LOG2E
g = scale * sigmoid_ext.sigmoid_tanh_fp32(a_log_exp * g)
```

三个细节都与 kernel 对齐：

- `a_log_exp = fp32_ex2_ftz(A_log * LOG2E)` 对应 kernel 侧的 `float a_log_exp = expf(A_log_ptr[head_idx]);`（[csrc/smxx/fwd_kernel1.cuh:258](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L258)）——项目用 `--use_fast_math` 编译，`expf` 会降级为 ex2 近似序列，参考实现用"预乘 \( \log_2 e \) + ex2 + ftz"复刻它（exact-match 测试为这个等价性背书）；
- `lower_bound * LOG2E` 被乘在 sigmoid **外面**，对应 launch 侧预计算的 `gate_scale`（u2-l2 会看到 `flash_kda.cpp` 里 `gate_scale = lower_bound * log2(e)`）：把换底因子合并进线性系数，kernel 内就不用再做底数转换；
- 乘法顺序 `scale * sigmoid(a_log_exp * g)` 与 kernel 的 `gate_scale * sigmoid_tanh_approx_f32(a_log_exp * g_val)` 一步步对应（[csrc/smxx/fwd_kernel1.cuh:321-323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L321-L323)）——顺序不同则舍入不同。

**② chunk 循环骨架与补零**：[tests/torch_ref.py:185-206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L185-L206)。三重循环 `for seq_idx → for chunk_idx → for h`：先按 `cu_seqlens` 切出序列，再按 `CHUNK=16` 切块（`n_chunks = ceil(seq_len/16)`），每个 (chunk, head) 准备一组**零填充**的 `[16, D]` 局部矩阵，只拷贝 `[:actual_len]` 行：

```python
g_chunk = torch.zeros(CHUNK, D, ...)
g_chunk[:actual_len] = g[t0:t0 + actual_len, h, :]
```

kernel 侧对应两件事：128 个线程做门控激活时对 `row >= actual_len` 直接令 `g_val = 0.0f`，另外 128 个线程把 k 的尾部行清零（[csrc/smxx/fwd_kernel1.cuh:306-338](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L306-L338)）。补零行的 g=0 使 cumsum 在尾部"走平"，而 k=0 使这些行对任何矩阵乘贡献为零——**正确性靠补零，输出靠裁剪**（`_out[:actual_len]`，L241）。

**③ cumsum 与 g_total**：[tests/torch_ref.py:208-209](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L208-L209)

```python
g_cumsum = g_chunk.cumsum(dim=0)      # inclusive
g_total  = g_cumsum[-1:]              # [1, D]，尾块时等于 actual_len 行之和
```

kernel 侧是激活循环里顺手做的串行累加（每个线程负责一列，16 行连加，`sum` 落盘即为 `g_total`，[csrc/smxx/fwd_kernel1.cuh:316-330](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L316-L330)）。注意 cumsum 的 fp32 加法顺序（从上到下逐行）两边一致。

还有一个容易踩坑的细节：workspace 里的 `g_total` 段存的其实是 **\( e^{g_{total}} \)** 而不是 \( g_{total} \)——kernel 在 `decay_apply` 之前就把 smem 里的 g_total 原地换成了 `ex2_approx_ftz_f32(g_total)`（[csrc/smxx/fwd_kernel1.cuh:353-357](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L353-L357)），这样 Kernel 2 拿到就能直接当状态衰减系数乘。参考实现里对应 L237 的 `g_total_exp = fp32_ex2_ftz(g_total)`。

#### 4.2.4 代码实践

**实践目标**：验证两个设计约束——\( g \) 的取值范围、以及 CHUNK=16 下 \( e^{\mathrm{gc}} \) 不会下溢。

**操作步骤**（示例代码，CPU 可跑）：

```python
# gate_range.py —— 示例代码
import math
LOG2E = 1.4426950408889634
lo, hi = -8.0, 8.0
lb = -5.0
# 最坏情况：sigmoid 输出无限逼近 0 或 1，g 逼近 (lb, 0)
print("g 的理论范围: (", lb, ", 0)")
# 一个 chunk 内 16 个 token 全取 g≈lb 时 cumsum 最小：
gc_min = 16 * lb
print("gc 下界 =", gc_min, " → exp(gc) =", math.exp(gc_min))
print("bf16/fp32 最小正常数 ≈", 2.0 ** -126)
print("是否仍为正常数:", math.exp(gc_min) > 2.0 ** -126)
```

**需要观察的现象**：`exp(-80) ≈ 1.8e-35`，大于 \( 2^{-126} \approx 1.18 \times 10^{-38} \)。

**预期结果**：CHUNK=16、lower_bound=−5 时 \( 16 \times (-5) = -80 \)，\( e^{-80} \) 仍是可表示的正常数——这正是 u1-l1 里"CHUNK=16 保证门控累积和落在 bf16 表示范围内"的定量出处（bf16 与 fp32 共享 8 位指数，范围相同）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 CHUNK 增大到 64（lower_bound 仍为 −5），\( e^{\mathrm{gc}} \) 的下界是多少？会发生什么？
**答案**：\( 64 \times (-5) = -320 \)，\( e^{-320} \approx 10^{-139} \)，远小于最小正常数 \( 2^{-126} \)，fp32/bf16 下都会下溢为 0（再被 ftz 显式冲零），`k_decayed` 尾部 token 的信息整体丢失。这是 CHUNK 不能随意放大的硬约束。

**练习 2**：参考实现为什么在**整个序列**上先做完门控激活，再进 chunk 循环，而 kernel 是在每个 tile 内部做激活？
**答案**：数学上两种做法等价（激活是逐元素运算，与分块无关）；kernel 在 tile 内做是为了让 K1 的单个 block 自包含（免掉一次全局 materialize）。参考实现怎么省事怎么来——它只需要每一步的**数值**与 kernel 相同，不需要执行结构与 kernel 相同。

**练习 3**：`g_total` 在尾块（actual_len < 16）时等于什么？
**答案**：等于前 `actual_len` 行 g 之和。因为补零行 g=0，cumsum 在其后保持不变，`g_cumsum[-1]` 恰是有效行之和；kernel 里则是循环直接跳过 `row >= actual_len`（g_val=0）得到同样的 sum。

### 4.3 decay 家族与 L / Mqk / INV

#### 4.3.1 概念说明

chunk 化的核心技巧：**用四种"带衰减系数的 key/q"把 16 个 token 之间的时序耦合变成矩阵乘**。设 \( A = k_{decayed} \)、\( B = k_{inv} \)、\( Q = q_{decayed} \)、\( K_r = k_{restored} \)（逐通道乘法 \(\odot\)）：

| 变量 | 定义 | 含义（衰减到哪个时刻） | 用途 |
|---|---|---|---|
| \( A_i = k_i \odot e^{\mathrm{gc}_i} \) | `k_decayed` | 时刻 i（含自己的 \( g_i \)） | 读旧状态、构造 L |
| \( Q_i = q_i \odot e^{\mathrm{gc}_i} \cdot scale \) | `q_decayed` | 时刻 i | 输出的两处读 |
| \( B_i = k_i \odot e^{-\mathrm{gc}_i} \) | `k_inv` | 时刻 0（chunk 起点） | 块内两两交互的"未衰减"一侧 |
| \( K_{r,i} = k_i \odot e^{g_{total} - \mathrm{gc}_i} \) | `k_restored` | 时刻 C（chunk 末尾） | 状态增量 |

关键恒等式：对任意 \( i \ge j \)，衰减系数都能分解成"两端各挂一半"：

\[ e^{\mathrm{gc}_i - \mathrm{gc}_j} = e^{\mathrm{gc}_i} \cdot e^{-\mathrm{gc}_j} \quad\Longrightarrow\quad (A B^\top)_{ij} = \langle k_i \odot e^{\mathrm{gc}_i},\, k_j \odot e^{-\mathrm{gc}_j}\rangle = k_i^\top \mathrm{diag}(e^{\mathrm{gc}_i-\mathrm{gc}_j})\, k_j \]

由此把 u1-l2 的递推展开成块内矩阵方程（状态 \( S \in \mathbb{R}^{V \times K} \)，入口状态 \( s \)）：

\[ u_i = \beta_i v_i - \beta_i\, A_i s^\top - \beta_i \sum_{j < i} (A B^\top)_{ij}\, u_j \;\;\Longrightarrow\;\; (I + L)\, U = \mathrm{diag}(\beta)\,\bigl(V - A s^\top\bigr) \]

其中写入强度已经吸收进 \( L \)：

\[ L = \mathrm{diag}(\beta) \cdot \mathrm{tril}(A B^\top,\ -1), \qquad M_{qk} = \mathrm{tril}(Q B^\top,\ 0), \qquad INV = (I + L)^{-1} \]

两个对角线问题务必分清：

- **L 严格下三角**（`diagonal=-1`）：擦除项只含**严格更早**的写入 \( j < i \)——token i 自己的 \( u_i \) 在求差时还没定义，不能出现在自己的方程里；
- **Mqk 保留对角线**（`diagonal=0`）：输出是"写完再读"，token i 能立刻看到自己的写入 \( j \le i \)。

#### 4.3.2 核心流程

```text
k_chunk, q_chunk [16, D] bf16（L2 归一化后）
 g_cumsum [16, D] fp32
  │ exp 换底: ex2(g·log2e) → fp32 → 舍入 bf16        ← 量化点①
  ├─ A  = k ⊙ bf16(e^{+gc})                          ← bf16 乘，量化点②
  ├─ Q  = q ⊙ bf16(e^{+gc}) ⊙ bf16(scale)            ← scale 本身先量化成 bf16
  ├─ B  = k ⊙ bf16(e^{−gc})
  └─ Kr = B ⊙ bf16(e^{g_total})  =  k ⊙ e^{g_total − gc}
        │
L    = mm(A, Bᵀ, out_dtype=fp32)      → tril(−1) × β(fp32)   ← 全程 fp32
Mqk  = matmul(Q, Bᵀ)                  → bf16 结果 → tril(0) 上三角清零
INV  = inv_fwd_subst_16(L)            → bf16（内部：8×8 fp32 前代换 + bf16 HMMA 合并）
```

#### 4.3.3 源码精读

**① 四个衰减变体**：[tests/torch_ref.py:210-215](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L210-L215)

```python
k_decayed = k_chunk * fp32_ex2_ftz(g_cumsum).to(torch.bfloat16)
q_decayed = q_chunk * fp32_ex2_ftz(g_cumsum).to(torch.bfloat16) * scale_bf16
neg_g_cumsum_bf16 = fp32_ex2_ftz(-g_cumsum).to(torch.bfloat16)
k_inv = k_chunk * neg_g_cumsum_bf16
g_total_exp_bf16 = fp32_ex2_ftz(g_total).to(torch.bfloat16)
k_restored = k_inv * g_total_exp_bf16
```

每个变体的舍入路径都值得逐个对：exp 在 **fp32** 里算（含 ftz），先**舍入到 bf16**，再做 **bf16 乘法**（又一次舍入）。`scale` 在循环外就被量化成 `scale_bf16`（[tests/torch_ref.py:156](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L156)），且只乘给 q、不乘给 k；乘法顺序是 `(q · e^{gc}) · scale`（从左到右）。kernel 的 `decay_apply` 一字不差地复现这一切（[csrc/smxx/fwd_kernel1.cuh:446-471](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L446-L471)）：

```cpp
BF16 exp_cumsum = BF16(ex2_approx_ftz_f32(g));
r_qd(0, v) = q * exp_cumsum * BF16(scale);   // q_decayed：scale 也是 BF16
r_kd(0, v) = k * exp_cumsum;                 // k_decayed
BF16 inv_cumsum = BF16(ex2_approx_ftz_f32(-g));
r_ki(0, v) = k * inv_cumsum;                 // k_inv
r_kr(0, v) = k * inv_cumsum * BF16(reg_gt);  // k_restored，reg_gt = e^{g_total}
```

注意 `k_restored = k_inv * bf16(e^{g_total})` 是**两次 bf16 舍入**（先得 k_inv 再乘），而不是一次算 \( k \odot e^{g_{total}-\mathrm{gc}} \)——两者数学等价、数值不等价，参考实现选择了与 kernel 相同的两次舍入路径。

**② L 与 Mqk 的构造**：[tests/torch_ref.py:216-217](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216-L217) 先做**全量**矩阵乘（不做掩码），再在 L219-L223 统一掩码：

```python
L = torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32)   # fp32 输出
Mqk = torch.matmul(q_decayed, k_inv.t())                       # bf16 输出
beta_activated = sigmoid_ext.sigmoid_tanh_fp32(beta_chunk.to(torch.float32))
beta_val_bf16 = beta_activated.to(torch.bfloat16).unsqueeze(-1)
L = torch.tril(L, diagonal=-1) * beta_activated.unsqueeze(-1)  # fp32 域乘 β
Mqk = torch.tril(Mqk)                                          # 保留对角线
```

两处精度刻意不同，都与 kernel 对齐：

- `L` 用 `out_dtype=torch.float32`（bf16 输入、fp32 累加输出）——对应 kernel 的 `mma_m16n16_bf16bf16fp32_1warp`（[csrc/smxx/fwd_kernel1.cuh:483](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L483)）；β 也以 **fp32** 乘进去（[csrc/smxx/fwd_kernel1.cuh:493-503](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L493-L503)：`L_fp32(i,j) * sigmoid_tanh_approx_f32(float(beta_tile(...)))`）。原因：L 是求逆的输入，精度敏感。
- `Mqk` 用普通 bf16 matmul（内部 fp32 累加、输出舍回 bf16）——对应 `mma_m16n16_bf16bf16bf16_1warp`（[csrc/smxx/fwd_kernel1.cuh:485](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L485)）；它只做一次输出乘 `Mqk @ U`，bf16 够用。
- β 的激活走同一个 `sigmoid_ext`（tanh 近似），但注意它**同时**以 fp32（乘 L）和 bf16（`beta_val_bf16`，下一节乘给 v 路径）两种精度存在——kernel 里同样是 sigmoid 在 K1 算 L 时用 fp32、在 K2 的 u 路径用 bf16。

**③ INV**：[tests/torch_ref.py:225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L225) 一行 `INV = inv_fwd_subst_16(L)`，输入是 fp32 的 L，输出是 bf16 的 \( (I+L)^{-1} \)。为什么 16×16 求逆可以"精确"做：\( I+L \) 是**单位下三角**，求逆就是解 16 组前代换方程，没有迭代、没有级数截断；u3-l1 会展开它的分块实现与被它替代的 fp16 Neumann 级数（在 \( |L| \to 1 \) 近共线 key 时灾难性失效，见 git 提交 7afb9f4）。

#### 4.3.4 代码实践

**实践目标**：验证 decay 家族的恒等式 \( K_r = k \odot e^{g_{total} - \mathrm{gc}} \)（bf16 容差内），并检查 L/Mqk 的三角结构。

**操作步骤**（示例代码，CPU 可跑；`fp32_ex2_ftz` 从 `tests/torch_ref.py` 导入）：

```python
# decay_check.py —— 示例代码
import sys, torch
sys.path.insert(0, "tests")
from torch_ref import fp32_ex2_ftz

torch.manual_seed(0)
CHUNK, D = 16, 128
g = -5.0 * torch.sigmoid(torch.randn(CHUNK, D))      # 伪门控，落在 (-5, 0)
k = torch.randn(CHUNK, D).to(torch.bfloat16)

gc = g.cumsum(dim=0)
g_total = gc[-1:]
k_inv  = k * fp32_ex2_ftz(-gc).to(torch.bfloat16)
k_restored = k_inv * fp32_ex2_ftz(g_total).to(torch.bfloat16)

# 直接近路：k * exp(g_total - gc)，在 fp32 里算再舍入 bf16
kr_direct = (k.float() * fp32_ex2_ftz(g_total - gc)).to(torch.bfloat16)

diff = (k_restored.float() - kr_direct.float()).abs()
rel = diff / (kr_direct.float().abs() + 1e-9)
print("最大绝对差:", diff.max().item(), " 最大相对差:", rel.max().item())
print("bf16 相对精度量级 2^-8 =", 2.0 ** -8)
```

**需要观察的现象**：最大相对差在 bf16 精度量级（约 \( 2^{-8} \approx 0.4\% \)，个别元素到两三个 ulp）——因为 `k_restored` 经历了两次 bf16 舍入，`kr_direct` 只有一次。

**预期结果**：`rel.max()` 是非零但 ~1e-2 以内的小数；若改成在 fp64 里计算近路，结论相同。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Mqk` 保留对角线而 `L` 用 `diagonal=-1`？
**答案**：`L` 来自"擦除"方程 \( u_i = \beta_i(v_i - \tilde S_i k_i^\top) \)，\( \tilde S_i \) 只包含**严格早于** i 的写入（\( j<i \)），否则 u_i 出现在自己的定义里成循环；`Mqk` 来自"读出" \( o_i = S_i q_i^\top \)，而 KDA 是写完再读，i 自己的写入 \( j=i \) 立即可见，所以对角线保留。

**练习 2**：`torch.matmul(q_decayed, k_inv.t())` 与 `torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32)` 的输出 dtype 分别是什么？为什么必须一个 bf16 一个 fp32？
**答案**：前者 bf16（PyTorch 默认输出与输入同 dtype，内部 fp32 累加后舍回 bf16），后者显式 fp32。Mqk 只参与一次输出乘法，bf16 足够且省带宽/寄存器；L 是 \( (I+L)^{-1} \) 的输入，求逆对误差敏感，kernel 侧分别用 bf16-out 与 fp32-out 两个 HMMA 变体对应（`...bf16bf16bf16_1warp` vs `...bf16bf16fp32_1warp`）。

**练习 3**：写出 \( (AB^\top)_{ij} \)（\( i \ge j \)）的解析式并说明它为什么恰好是"j 的写入衰减到 i 时刻"的系数。
**答案**：\( (AB^\top)_{ij} = k_i^\top \mathrm{diag}(e^{\mathrm{gc}_i - \mathrm{gc}_j}) k_j \)。j 时刻写入的 \( u_j k_j^\top \) 到时刻 i 已被 \( g_{j+1},\dots,g_i \) 衰减，逐通道系数 \( e^{\mathrm{gc}_i - \mathrm{gc}_j} \)，与 \( k_i \) 做内积时恰好表现为把该系数挂在 \( k_j \) 上——即 \( (k_j \odot e^{-\mathrm{gc}_j}) \) 与 \( (k_i \odot e^{+\mathrm{gc}_i}) \) 配对相乘。

### 4.4 U 计算、输出与状态更新

#### 4.4.1 概念说明

有了 \( INV \)，chunk 的剩余计算就是"解方程 → 算输出 → 推状态"三步。记入口状态 \( s \in \mathbb{R}^{V \times K} \)（参考实现里 `work_state[seq_idx, h]` 的布局是 `[V, K]`）：

\[ U = INV \cdot \mathrm{diag}(\beta)\,\bigl(V - A s^\top\bigr) \qquad\text{（块内写入矩阵，每行 } u_i \text{）} \]

\[ O = Q s^\top + M_{qk}\, U \qquad\text{（输出：旧状态部分 + 块内交互部分）} \]

\[ s' = s\,\mathrm{diag}(e^{g_{total}}) + K_r^\top U \qquad\text{（出口状态：整体衰减 + 写入增量）} \]

第三式的直觉：chunk 内每个写入 \( u_i k_i^\top \) 到 chunk 结束时已被后面的 token 衰减了 \( e^{g_{total} - \mathrm{gc}_i} \)，把这个系数提前折进 \( K_r \)，下一 chunk 只需对整个状态乘一次 \( e^{g_{total}} \)。

#### 4.4.2 核心流程

```text
s = work_state[seq, h] [V=128, K=128] bf16
v_chunk [16, 128]
  │ v_chunk − matmul(A, sᵀ)         ← bf16 matmul：S̃ᵢkᵢ 读旧状态（擦除项）
  │ × beta_val_bf16 [16,1]           ← β 以 bf16 参与 u 路径（注意与 L 的 fp32 β 不同！）
  ▼
U = matmul(INV, ·)                   ← bf16
_out = matmul(Q, sᵀ) + matmul(Mqk, U) ← bf16 + bf16 累加
delta_s = mm(K_rᵀ, U, out_dtype=fp32) ← fp32
work_state = bf16( delta_s + e^{g_total} ⊙ s )   ← fp32_fma 单次舍入，再量化 bf16
out[t0 : t0+actual_len] = _out[:actual_len]
```

#### 4.4.3 源码精读

**① 擦除 + 写入强度**：[tests/torch_ref.py:227-229](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L227-L229)

```python
state_slice = work_state[seq_idx, h]                     # [V, K] bf16
v_chunk = v_chunk - torch.matmul(k_decayed, state_slice.t())
v_chunk = v_chunk * beta_val_bf16
```

`matmul(k_decayed, sᵀ)` 即 \( A s^\top \)（每行是 \( \tilde S_i k_i^\top \)），bf16 输出；减法与乘 β 都在 bf16 域。kernel 侧对应 K2 的 Phase 1/3（`k_decayed @ s` 的双 GEMM 与 `u = (v - u) * beta`，u3-l4 精读）。注意这里的 β 是 **bf16**（`beta_val_bf16`），与 4.3 节 L 路径的 fp32 β 形成对照——同一个 β 激活值，两条路径两种精度。

**② 解 U 与输出**：[tests/torch_ref.py:231-233](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L231-L233)

```python
U = torch.matmul(INV, v_chunk)
_out = torch.matmul(q_decayed, state_slice.t())
_out = _out + torch.matmul(Mqk, U)
```

三个 bf16 matmul 分别对应 \( INV \cdot \mathrm{diag}(\beta)(V - As^\top) \)、\( Q s^\top \)、\( M_{qk} U \)；最后一个 `+` 是 bf16 加法，对应 kernel Phase 4 把 `Mqk@U` 累加进 out 寄存器。

**③ 状态更新（精度最讲究的一步）**：[tests/torch_ref.py:235-239](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L235-L239)

```python
delta_s = torch.mm(k_restored.t(), U, out_dtype=torch.float32)   # fp32
g_total_exp = fp32_ex2_ftz(g_total).squeeze(0).unsqueeze(-1)      # [K,1]
work_state[seq_idx, h] = fp32_fma(delta_s, state_slice.to(torch.float32).t(),
                                  g_total_exp).to(torch.bfloat16).t()
```

逐项对应 \( s' = \mathrm{bf16}\bigl(\underbrace{K_r^\top U}_{\text{fp32 累加}} + e^{g_{total}} \odot s\bigr) \)：

- `delta_s` 用 `out_dtype=fp32`（bf16 输入 fp32 输出）——kernel Phase 6 的 HMMA 也是 fp32 累加；
- `fp32_fma(delta_s, s_fp32, e^{g_total})` 把"衰减乘旧状态 + 加增量"压成**一次舍入**的 FMA——这正是 u1-l1 "bf16 存状态、fp32 更新"设计决策的落地：状态平时以 bf16 存储（省一半片上容量），更新瞬间升到 fp32 做单次舍入的融合运算，再量化回 bf16；
- 最后的 `.t()` 只是把 `[K,V]` 的计算结果转置回 `[V,K]` 存储，纯布局操作。

**④ 输出写回与收尾**：[tests/torch_ref.py:241](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L241) 只写 `_out[:actual_len]`（尾块裁剪）；[tests/torch_ref.py:243-247](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L243-L247) 在全部序列处理完后按 `final_state` 的 dtype 拷出（fp32 模式只是存储精度的差别，计算路径与 bf16 完全一致——docstring [tests/torch_ref.py:134-137](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L134-L137) 写明"converted to bf16 for compute, back to fp32 for output"）。

**⑤ 这份参考实现同时是 workspace 契约的语义定义**。K1 末尾把这 6 个量 TMA store 进 workspace（[csrc/smxx/fwd_kernel1.cuh:515-583](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L515-L583)）：`k_decayed / q_decayed / k_restored [16,128] bf16`、`g_total (=e^{g_total}) [128] fp32`、`INV / Mqk [16,16] bf16`——正好是本讲 4.2/4.3 的产物。注意 **U、L、v 不进 workspace**：U 由 K2 在寄存器里现场算（4.4 的①②），L 用完即弃。这也解释了为什么 `torch_ref` 的 chunk 循环顺序（chunk 外层、head 内层）与 K2 的 warp 组织不同却没有数值差别：**每个 (chunk, head) 的计算是封闭的**，只通过 `work_state` 顺序传递，天然可重排。

#### 4.4.4 代码实践

**实践目标**：用 fp64 逐 token 递推验证 chunk 矩阵形式的**数学正确性**（这一步不要求 bit-exact，只验证公式）。

**操作步骤**（示例代码，CPU 可跑）：

```python
# chunk_vs_token.py —— 示例代码
import torch
torch.manual_seed(0)
C, V, K = 16, 12, 12                      # 小维度便于观察；D=128 时同理
q = torch.randn(C, K, dtype=torch.float64)
k = torch.randn(C, K, dtype=torch.float64)
v = torch.randn(C, V, dtype=torch.float64)
g = -5.0 * torch.sigmoid(torch.randn(C, K, dtype=torch.float64))  # 逐通道门控
beta = torch.sigmoid(torch.randn(C, dtype=torch.float64))
s0 = torch.randn(V, K, dtype=torch.float64)

# --- 路径 A：逐 token 递推（u1-l2 的公式） ---
s = s0.clone()
outs = []
for i in range(C):
    s_tilde = s * torch.exp(g[i])         # 先擦
    u = beta[i] * (v[i] - s_tilde @ k[i]) # 求差
    s = s_tilde + torch.outer(u, k[i])    # 后写
    outs.append(s @ q[i])                 # 写完再读
out_token = torch.stack(outs)             # [C, V]

# --- 路径 B：chunk 矩阵形式（本讲公式，fp64 直算） ---
gc = g.cumsum(dim=0); gt = gc[-1]
A  = k * torch.exp(gc);   Bq = q * torch.exp(gc)
Bi = k * torch.exp(-gc);  Kr = k * torch.exp(gt - gc)
L   = torch.tril(A @ Bi.t(), diagonal=-1) * beta.unsqueeze(-1)
Mqk = torch.tril(Bq @ Bi.t())
INV = torch.linalg.inv(torch.eye(C, dtype=torch.float64) + L)
U   = INV @ ((v - A @ s0.t()) * beta.unsqueeze(-1))
out_chunk = Bq @ s0.t() + Mqk @ U
s_final   = s0 * torch.exp(gt) + (Kr.t() @ U).t()

print("out  max|diff| =", (out_token - out_chunk).abs().max().item())
print("state max|diff| =", (s - s_final).abs().max().item())
```

**需要观察的现象**：两个 max|diff| 都在 1e-14 量级（fp64 舍入噪声级别）。

**预期结果**：逐 token 递推与 chunk 矩阵形式在 fp64 下逐元素一致，证明 4.3/4.4 推导的公式就是 u1-l2 递推的精确改写。具体量级随 seed 略有波动，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：状态更新为什么用 `k_restored.t() @ U` 而不是 `k_decayed.t() @ U`？
**答案**：出口状态需要的是每个写入**衰减到 chunk 末尾**后的贡献，系数是 \( e^{g_{total}-\mathrm{gc}_i} \)，只有 \( K_r \) 把它折进了 key 里；\( A=k_{decayed} \) 挂的是 \( e^{+\mathrm{gc}_i} \)（衰减到各自时刻 i），直接用它会把时序完全算错。

**练习 2**：指出状态更新这一行里的三个数值细节（对应 kernel 行为）。
**答案**：(a) `delta_s` 以 fp32 累加产出（`out_dtype=torch.float32`，对应 HMMA fp32 累加）；(b) "乘衰减 + 加增量"用 `fp32_fma` 合成**单次舍入**（对应 kernel 的 fp32 FMA）；(c) 最终只量化一次 bf16 存回 `work_state`（对应片上状态以 bf16 保存）。

**练习 3**：尾块（actual_len < 16）时状态更新为什么天然安全，不需要特判？
**答案**：补零行的 k 被清成 0，于是 \( K_r \)、\( A \) 的这些行全为 0，`Krᵀ @ U` 中对应行乘出的贡献为 0；同时这些行的 \( u_i \) 是解方程得到的 0 向量行（V 的补零行 − 0 = 0，再乘 β、乘 INV 的相应结构）。所以出口状态只累计有效 token 的写入，无需 mask。（输出侧则相反，必须显式裁剪 `_out[:actual_len]`。）

## 5. 综合实践：dump_chunk.py——把一个 chunk 的全部中间量打出来

这是本讲的旗舰实践：选定一个 chunk 与一个 head，复刻 `torch_ref` 的单块计算路径，打印 9 个中间量，并验证 4.3 节的 decay 恒等式。做完它，u2-l6/u2-l7 读 K1 源码时每个量都有了"实感"。

**实践目标**

1. 对 `chunk_idx=3, h=0`（该 chunk 为整块，actual_len=16）产出：`k_decayed / q_decayed / k_inv / k_restored / g_total / L / Mqk / INV / U`；
2. 验证 `k_restored == k * exp(g_total - g_cumsum)`（bf16 容差内）；
3. （可选，需 GPU）把逐块输出与完整 `torch_ref` 乃至 `flash_kda.fwd` 的结果对照，亲眼看一次 `torch.equal` 成立。

**操作步骤**（示例代码，保存为 `dump_chunk.py` 放在仓库根目录运行；需 GPU 与已安装的 flash_kda）：

```python
# dump_chunk.py —— 示例代码（步骤 1-4 无需安装 flash_kda，只依赖 tests/torch_ref.py）
import sys, math, torch
sys.path.insert(0, "tests")
from torch_ref import (fp32_ex2_ftz, l2_normalize_kernel_match,
                       inv_fwd_subst_16, sigmoid_ext, fp32_fma, torch_ref)

torch.manual_seed(0)
B, T, H, D, CHUNK = 1, 4096, 4, 128, 16
LOWER_BOUND, LOG2E = -5.0, 1.4426950408889634
scale = 1.0 / math.sqrt(D)
dev = 'cuda'

# 1) 构造输入（与 tests/test_fwd.py:test_fwd 同款，规模缩小）
q = torch.nn.functional.normalize(torch.randn(B, T, H, D, device=dev), dim=-1).to(torch.bfloat16)
k = torch.nn.functional.normalize(torch.randn(B, T, H, D, device=dev), dim=-1).to(torch.bfloat16)
v = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
g = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
beta = torch.randn(B, T, H, dtype=torch.bfloat16, device=dev)
A_log = torch.rand(H, dtype=torch.float32, device=dev)
dt_bias = torch.rand(H, D, dtype=torch.float32, device=dev)
initial_state = torch.zeros(1, H, D, D, dtype=torch.bfloat16, device=dev)

# 2) 复刻 torch_ref 的预处理（见 4.1-4.2 节逐行对照）
q2 = l2_normalize_kernel_match(q.reshape(-1, H, D))
k2 = l2_normalize_kernel_match(k.reshape(-1, H, D))
g2 = g.reshape(-1, H, D).to(torch.float32) + dt_bias.unsqueeze(0)
a_log_exp = fp32_ex2_ftz(A_log * LOG2E).unsqueeze(0).unsqueeze(-1)
g2 = (LOWER_BOUND * LOG2E) * sigmoid_ext.sigmoid_tanh_fp32(a_log_exp * g2)
beta2 = sigmoid_ext.sigmoid_tanh_fp32(beta.reshape(-1, H).to(torch.float32))
scale_bf16 = torch.tensor(scale, dtype=torch.bfloat16, device=dev)

# 3) 取 chunk_idx=3, h=0 的单块
ci, h, t0 = 3, 0, 3 * CHUNK
g_chunk  = g2[t0:t0 + CHUNK, h]                       # 整块：actual_len=16
q_chunk, k_chunk = q2[t0:t0 + CHUNK, h], k2[t0:t0 + CHUNK, h]
v_chunk  = v.reshape(-1, H, D)[t0:t0 + CHUNK, h]
beta_c   = beta2[t0:t0 + CHUNK, h]

# 4) 单块计算（torch_ref L208-L239 的镜像）
g_cumsum = g_chunk.cumsum(dim=0); g_total = g_cumsum[-1:]
k_decayed = k_chunk * fp32_ex2_ftz(g_cumsum).to(torch.bfloat16)
q_decayed = q_chunk * fp32_ex2_ftz(g_cumsum).to(torch.bfloat16) * scale_bf16
k_inv     = k_chunk * fp32_ex2_ftz(-g_cumsum).to(torch.bfloat16)
k_restored = k_inv * fp32_ex2_ftz(g_total).to(torch.bfloat16)
L   = torch.tril(torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32), -1) \
        * beta_c.unsqueeze(-1)
Mqk = torch.tril(torch.matmul(q_decayed, k_inv.t()))
INV = inv_fwd_subst_16(L)
state = initial_state[0, h]                            # [V, K]
v_erase = (v_chunk - torch.matmul(k_decayed, state.t())) * beta_c.to(torch.bfloat16).unsqueeze(-1)
U = torch.matmul(INV, v_erase)

for name, t in [("k_decayed", k_decayed), ("q_decayed", q_decayed),
                ("k_inv", k_inv), ("k_restored", k_restored),
                ("g_total", g_total), ("L", L), ("Mqk", Mqk),
                ("INV", INV), ("U", U)]:
    print(f"{name:>11s} shape={tuple(t.shape)} dtype={t.dtype} "
          f"min={t.float().min():+.4f} max={t.float().max():+.4f}")

# 5) 验证恒等式 k_restored ≈ k * exp(g_total - g_cumsum)
kr_direct = (k_chunk.float() * fp32_ex2_ftz(g_total - g_cumsum)).to(torch.bfloat16)
rel = ((k_restored.float() - kr_direct.float()).abs()
       / (kr_direct.float().abs() + 1e-9))
print(f"\nk_restored 恒等式: max_rel_diff={rel.max():.3e}  (bf16 量级 2^-8={2.0**-8:.3e})")

# 6) （可选）与整体 torch_ref / kernel 对拍
out_ref = torch.zeros_like(v)
fs_ref = torch.zeros_like(initial_state)
torch_ref(q, k, v, g, beta, scale, out_ref, A_log, dt_bias, LOWER_BOUND,
          initial_state=initial_state.clone(), final_state=fs_ref)
_out = torch.matmul(q_decayed, state.t()) + torch.matmul(Mqk, U)
print("单块 _out 与整体 torch_ref 对应行 torch.equal:",
      torch.equal(_out, out_ref.reshape(-1, H, D)[t0:t0 + CHUNK, h]))

import flash_kda
out_k = torch.zeros_like(v); fs_k = torch.zeros_like(initial_state)
flash_kda.fwd(q, k, v, g, beta, scale, out_k, A_log=A_log, dt_bias=dt_bias,
              lower_bound=LOWER_BOUND, initial_state=initial_state.clone(),
              final_state=fs_k)
print("kernel vs torch_ref (out):", torch.equal(out_k, out_ref))
print("kernel vs torch_ref (final_state):", torch.equal(fs_k, fs_ref))
```

**需要观察的现象**

1. 9 个中间量的形状符合预期：三个 `[16,128]` bf16、`g_total [1,128]` fp32、`L [16,16]` fp32、其余 `[16,16]` bf16；
2. `L` 严格下三角（上三角含对角线全 0）、`Mqk` 下三角含对角线、`INV` 下三角（单位下三角的逆仍是下三角，对角线≈1）；
3. `g_total` 全部为负（最多到 −80）；
4. 恒等式 `max_rel_diff` 非零但落在 bf16 精度附近（两次 vs 一次舍入之差）；
5. 步骤 6 的三个 `torch.equal` 均为 `True`。

**预期结果**：以上 5 条全部满足即实践成功。其中第 5 条依赖你本机的 torch/CUDA/GPU 环境与 CI 一致（SM90、项目要求的版本组合）；若某条为 `False`，优先检查 flash_kda 是否为当前源码编译的版本。本讲义写作环境无 GPU，以上运行结论**待本地验证**。

## 6. 本讲小结

- `tests/torch_ref.py` 是**用 PyTorch 书写的 kernel 行为规格**：`sigmoid_ext`（tanh.approx）、`fp32_ex2_ftz`（ex2+ftz+换底）、`fp32_fma`（单次舍入）、`l2_normalize_kernel_match`（FMA 链 + XOR 蝶形顺序）四个工具把"硬件指令 + 求和顺序 + 舍入边界"逐一复刻，从而支撑 `torch.equal` 级别的 exact-match 测试。
- chunk 化建立在 inclusive cumsum 上：\( A=k \odot e^{+\mathrm{gc}} \)、\( Q=q \odot e^{+\mathrm{gc}} \cdot scale \)、\( B=k \odot e^{-\mathrm{gc}} \)、\( K_r=k \odot e^{g_{total}-\mathrm{gc}} \) 四个变体把时序耦合折进矩阵乘，核心恒等式 \( (AB^\top)_{ij} = k_i^\top \mathrm{diag}(e^{\mathrm{gc}_i-\mathrm{gc}_j}) k_j \)。
- 块内三件套：\( L=\mathrm{diag}(\beta)\,\mathrm{tril}(AB^\top,-1) \)（fp32 全程，严格下三角——擦除只看过去）、\( M_{qk}=\mathrm{tril}(QB^\top,0) \)（bf16，保留对角线——写完再读）、\( INV=(I+L)^{-1} \)（16×16 前代换，u3-l1 展开）。
- 输出与状态：\( U=INV\,\mathrm{diag}(\beta)(V-As^\top) \)、\( O=Qs^\top+M_{qk}U \)、\( s'=s\,\mathrm{diag}(e^{g_{total}})+K_r^\top U \)——状态"bf16 存储、fp32 单次舍入更新"；β 在 L 路径是 fp32、在 U 路径是 bf16，同一激活值两种精度。
- K1 写入 workspace 的 6 个量（`k_decayed/q_decayed/k_restored/g_total/INV/Mqk`，其中 g_total 段实际存 \( e^{g_{total}} \)）正是本讲 4.2/4.3 的产物；U、L 不落盘，由 K2 现场计算。

## 7. 下一步学习建议

本讲拿到了"算法语义"，接下来两条线索任意先走：

1. **推荐主线（u2-l2）**：顺着数据流出 Python——精读 `csrc/flash_kda.cpp` 的 workspace 尺寸公式与 `TORCH_CHECK` 校验链，看清本讲 `torch_ref` 开头那些 reshape/转置在 C++ 侧的镜像。
2. **对照主线（u2-l6 / u2-l7）**：带着本讲的量名去读 Kernel 1——门控激活与 cumsum（`fwd_kernel1.cuh` L306-L338）、decay_apply 与 L/Mqk 构造（L360-L507），逐段验证"kernel 每一步都能在 `torch_ref` 找到对应行"。
3. 若对 \( INV \) 的求逆实现好奇，可直接跳 u3-l1（8×8 前代换 + bf16 HMMA 块合并，以及被它替代的 fp16 Neumann 级数为何失效）；workspace 六段切分的字节级布局则在 u2-l8。
