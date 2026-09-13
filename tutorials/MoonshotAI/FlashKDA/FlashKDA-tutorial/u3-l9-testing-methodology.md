# u3-l9 测试方法学：bit-exact 参考、FLA 对拍与参数扫描

## 1. 本讲目标

前几讲我们把两个 CUDA kernel 的每一相位都拆开了。本讲换一个视角——**如果 kernel 改了一行，我们凭什么知道它没算错？**

学完本讲，你应该能够：

1. 说清 FlashKDA 的三层测试防线：`torch.equal` 级 bit-exact 对拍、FLA fp64 金标对拍、pytest 全组合参数扫描，以及它们各自回答什么问题。
2. 掌握「参考实现模拟硬件行为」这一范式的具体技巧：用 fp64 中间量模拟 FMA 单次舍入、用 `exp2`+清零模拟 `ex2.approx.ftz`、用即时编译的小 kernel 暴露 `tanh.approx.f32`、用固定顺序的循环复刻 warp shuffle 归约与前代换消元顺序；同时理解这一范式的代价与局限。
3. 看懂 `test_fwd.py` 中 g/bias 扫描用例、fp64 金标构造与 matplotlib 误差可视化的设计意图。
4. 会用 `pytest.mark.parametrize` 组织 580 个 exact-match 用例，理解 `conftest.py` 的 xdist 多 GPU 分配逻辑，并能用 `run_test_full.sh` 跑一次全量回归。
5. 能独立为新场景（新的序列长度、新的 head 数）补充 exact-match 测试用例。

## 2. 前置知识

本讲假设你已读过 u2-l1（chunk 化算法骨架）与 u1-l3（构建与 `tests/test.sh`）。在此之上，补充几个测试视角的概念：

- **bit-exact（逐位相等）与容差比较的区别**。`torch.allclose(a, b, rtol, atol)` 回答「两者是否足够接近」；`torch.equal(a, b)` 回答「两者的每一个比特是否完全相同」。前者是常规数值测试，后者是强得多的断言——它要求参考实现复刻的不只是数学，还有**舍入的顺序**。
- **FMA（fused multiply-add）的单次舍入**。硬件的融合乘加指令计算 \( c + a \cdot b \) 时只在最后做一次舍入；而朴素地先算乘法再算加法会做两次舍入。两者对同样的 fp32 输入可能给出不同的最后一位（ulp）。
- **FTZ（flush to zero，冲零）**。`--use_fast_math` 下部分近似指令会把低于最小规格化数的中间结果直接置零。参考实现必须连这个行为一起模拟。
- **`torch.utils.cpp_extension.load_inline`**。在 Python 里内嵌一段 CUDA 源码、运行时 JIT 编译成扩展模块的机制。首次编译较慢，之后按源码哈希缓存。
- **pytest 的参数化与 xdist**。`@pytest.mark.parametrize` 把一个测试函数展开成一批独立用例（各有可读的 id、可独立失败）；`pytest-xdist` 的 `-n N` 启动 N 个 worker 进程并行执行，每个 worker 是独立进程、独立 CUDA 上下文。
- **FLA（flash-linear-attention）**。FlashKDA 要替换的上游库；其 `fused_recurrent_kda` 是逐 token 串行递推实现，本讲被用作 fp64 金标（u3-l11 会讲后端分发的另一半故事）。

## 3. 本讲源码地图

| 文件 | 规模 | 作用 |
| --- | --- | --- |
| `tests/torch_ref.py` | 248 行 | bit-exact 参考实现 + 数值工具箱（fp32_fma / fp32_ex2_ftz / l2_normalize_kernel_match / inv_fwd_subst_16 / tanh-sigmoid 扩展） |
| `tests/test_fwd.py` | 431 行 | 两组测试：kernel↔torch_ref 的 exact match；kernel↔FLA 金标/基线的容差对拍与误差可视化 |
| `tests/test_fwd_full.py` | 275 行 | pytest 参数化全组合扫描：4 种 state IO × 2 种 dtype × 多 H/T/varlen/batched/长序列，共 580 个用例 |
| `tests/conftest.py` | 14 行 | xdist worker → GPU 的分配钩子 |
| `tests/run_test_full.sh` | 4 行 | 全量回归入口（`-n 16` 并行） |
| `tests/test.sh` | 5 行 | 快速测试入口（u1-l3 已讲，此处对照） |

一句话概括关系：`torch_ref.py` 是「kernel 行为的规格书」，`test_fwd.py` 用它做逐位验收、再用 FLA 做数值层面的交叉验证，`test_fwd_full.py` 把前者的 exact match 断言铺满参数空间，`conftest.py` + `run_test_full.sh` 解决 580 个用例如何在多 GPU 上并行跑完。

## 4. 核心概念与源码讲解

### 4.1 bit-exact 参考实现技巧：让 PyTorch 逐 FMA 复刻 GPU kernel

#### 4.1.1 概念说明

常规的 kernel 测试写法是「写一个数学正确的朴素实现，用 `allclose` 给容差」。FlashKDA 没有这么做——它的参考实现 `torch_ref` 与 kernel 之间用的是 `torch.equal`，**一个比特都不能差**。

为什么敢这么苛刻？因为 kernel 的数值行为是**完全确定**的：

- 没有浮点原子加、没有顺序不定的跨块归约；所有归约（L2 范数、HMMA 累加）都有固定的推进顺序。
- 所有近似指令（`ex2.approx.ftz`、`tanh.approx.f32`、`rsqrt.approx`）在给定输入下输出确定。
- 所有 bf16↔fp32 量化点都在编译期固定（u3-l8 的「fp32 保持点 / bf16 量化点」总账）。

于是参考实现的任务不是「算对」，而是**逐操作复刻硬件的舍入序列**。它故意写得很慢——Python 层对 N 条序列 × H 个 head × 每个 chunk 三重循环——因为它只在测试里跑，慢无所谓，**确定**才是第一诉求。

这个范式有三类复刻通道（承接 u3-l8 的结论，这里从实现技巧角度展开）：

1. **fp64 中间量模拟 FMA 单次舍入**（`fp32_fma`）；
2. **近似指令复刻**（`fp32_ex2_ftz`、tanh-sigmoid CUDA 扩展、蝶形归约顺序、前代换消元顺序）;
3. **量化点复刻**（bf16 边界与 `out_dtype=fp32` 的矩阵乘，分别对应 kernel 的 bf16 出口与 fp32 累加器）。

#### 4.1.2 核心流程

`torch_ref` 主体的执行流程（chunk 级算法本身 u2-l1 已详讲，这里只列「哪些步骤必须走特殊 helper」）：

```text
输入 [B,T,H,D] → reshape 成 [B*T,H,D]
① q/k L2 归一化        → l2_normalize_kernel_match（复刻蝶形归约顺序，不能用 F.normalize）
② 门控激活             → fp32_ex2_ftz(A_log*LOG2E) + sigmoid_ext（tanh.approx）+ log2 域换底
对每条序列 seq、每个 chunk、每个 head：
    ③ padding 到 16 行（尾块补零）
    ④ cumsum + 四个衰减变体 → 每次乘衰减前 ex2 都走 fp32_ex2_ftz，出口量化 bf16
    ⑤ L / Mqk            → L 用 mm(out_dtype=fp32)（kernel 全程 fp32）；
                           Mqk 用普通 matmul（出口即 bf16）
    ⑥ beta 激活          → sigmoid_ext（tanh.approx），量化 bf16
    ⑦ INV                → inv_fwd_subst_16（复刻前代换的 FMA 消元顺序）
    ⑧ u 修正 / U / out   → 普通 matmul（bf16 矩阵乘，累加 fp32）
    ⑨ 状态更新           → delta_s 用 mm(out_dtype=fp32)，最终
                           fp32_fma(delta_s, s, e^{g_total}) 一次舍入后量化 bf16
⑩ final_state 按 fp32/bf16 拷出
```

#### 4.1.3 源码精读

**（1）`fp32_fma`：fp64 中间量模拟 FMA。**

[tests/torch_ref.py:L55-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59) 把三个 fp32 量升到 fp64 里做乘加、再一次性降回 fp32：

```python
def fp32_fma(c, a, b):
    ...
    return (c.to(torch.float64) + a.to(torch.float64) * b.to(torch.float64)).to(torch.float32)
```

原理：两个 fp32 的尾数各 24 位，乘积至多 48 位有效位，在 53 位尾数的 fp64 里**精确可表**；`c + a·b` 在 fp64 中只有一次舍入，最后 `.to(float32)` 再舍入一次，整体表现为「近似单次舍入」：

\[
\mathrm{FMA}(c,a,b) = \mathrm{fl}_{32}\!\big(c + a\cdot b\big)
\quad\neq\quad
\mathrm{fl}_{32}\!\big(\mathrm{fl}_{32}(a\cdot b) + c\big)
\]

而 PyTorch 的普通 fp32 表达式 `c + a * b` 恰好是右边那种双重舍入。kernel 里凡是「乘加后一次量化」的地方（L2 归一化的累加、状态更新的 FFMA），参考实现都必须走这个 helper——它在本文件里被 `l2_normalize_kernel_match` 与主循环的状态更新（L239）各用到一次。

**（2）`fp32_ex2_ftz`：模拟 `ex2.approx.ftz`。**

[tests/torch_ref.py:L47-L52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L47-L52)：

```python
def fp32_ex2_ftz(x):
    ...
    ret = torch.special.exp2(x)
    ret = torch.where(ret.abs() < torch.finfo(torch.float32).tiny, torch.zeros_like(ret), ret)
    return ret
```

kernel 在 log2 域工作（u2-l2 已讲 host 侧把 `gate_scale` 预乘了 \(\log_2 e\)），衰减系数全部用 `ex2.approx.ftz` 计算。这里的两步模拟：`torch.special.exp2` 对应 ex2，`where(... < tiny, 0, ...)` 把次规格化（subnormal）结果显式冲零，对应 `.ftz` 后缀。门控 cumsum、`g_total`、四个衰减变体（L208-L215、L237）全部经过它。

**（3）tanh-sigmoid：PyTorch 没有的指令，就自己编一个 kernel。**

`sigmoid(x) = 0.5·tanh(x/2) + 0.5` 是恒等式，但 kernel 用的是 `tanh.approx.f32` 这条近似指令——PyTorch 从 Python 层拿不到它。于是 [tests/torch_ref.py:L7-L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L7-L38) 用 `load_inline` 现场编译一个单 kernel 扩展，内嵌 PTX：

```cuda
float xh = input[idx] * 0.5f;
float th;
asm("tanh.approx.f32 %0, %1;" : "=f"(th) : "f"(xh));
output[idx] = th * 0.5f + 0.5f;
```

这个 `sigmoid_ext` 在两处被调用：门控激活（L169）与 beta 激活（L220）——正是 kernel 里两处 tanh 近似 sigmoid 的位置。注意它在**模块 import 时**编译，首次运行 `tests/test.sh` 会多等一段 JIT 时间，之后按源码哈希缓存在 torch 扩展目录里。

**（4）`l2_normalize_kernel_match`：复刻 warp shuffle 树归约。**

[tests/torch_ref.py:L62-L78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L62-L78)：

```python
x_f32 = x.float()
groups = x_f32.reshape(*x_f32.shape[:-1], 16, 8)
partials = torch.zeros(*x_f32.shape[:-1], 16, dtype=torch.float32, ...)
for i in range(8):
    partials = fp32_fma(partials, groups[..., i], groups[..., i])
for offset in [8, 4, 2, 1]:
    indices = torch.arange(16, device=x.device) ^ offset
    partials = partials + partials[..., indices]
inv_norm = torch.rsqrt(partials[..., 0:1] + 1e-6)
return (x_f32 * inv_norm).to(x.dtype)
```

这段代码是 u2-l7 讲过的 K1 L2 归一化（每线程 8 元素 + 16-lane xor 蝶形）的逐位镜像：

- `reshape(..., 16, 8)`：16 个「lane」，每 lane 顺序持 8 个元素；
- 第一个循环：每 lane 内部按 i=0..7 顺序做 `fp32_fma` 累加平方和——顺序即 kernel 里 8 条 FFMA 的顺序；
- 第二个循环：`indices ^ offset` 模拟 `__shfl_xor_sync` 蝶形，offset 依次 8→4→2→1（u2-l7 的树归约）；
- `rsqrt(和 + 1e-6)`：epsilon 也与 kernel 一致；
- 出口量化回 bf16。

如果这里换成 `F.normalize`，数学上等价，但归约顺序不同 → 平方和的最后一位可能不同 → 归一化结果差 1 ulp → 整个 exact match 崩塌。这就是「参考实现模拟硬件」的含义。

**（5）`inv_fwd_subst_16`：复刻前代换的消元顺序。**

[tests/torch_ref.py:L87-L122](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L87-L122)（算法本身 u3-l1 已详讲，这里看它的「测试视角」）：

```python
for s in range(7):
    row_scale = -inv8[:, :, s]
    for p in range(s):
        inv8[:, s + 1:, p] = fp32_fma(
            inv8[:, s + 1:, p], row_scale[:, s + 1:], inv8[:, s, p:p + 1])
    inv8[:, s + 1:, s] = row_scale[:, s + 1:]
```

注意 `s`（主元行）与 `p`（已定列）的双重循环顺序、以及更新公式里 `fp32_fma` 的使用——它们逐一对应 kernel 单 warp 前代换中「第 s 步广播主元行、对下方行做 rank-1 消元」的 FMA 序列。合并段（L112-L121）则用 `torch.mm(..., out_dtype=torch.float32)` 模拟 HMMA 的 fp32 累加，且只在 HMMA 输入边界量化 bf16（`P`、`M`、`(-dc).to(bfloat16)`），与 kernel 的量化点一致。

**（6）主循环中的量化点分工。**

看几组对照鲜明的写法（[tests/torch_ref.py:L216-L239](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216-L239)）：

```python
L = torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32)   # L 全程 fp32（求逆种子）
Mqk = torch.matmul(q_decayed, k_inv.t())                        # Mqk 出口即 bf16
...
delta_s = torch.mm(k_restored.t(), U, out_dtype=torch.float32)  # 状态增量 fp32
work_state[seq_idx, h] = fp32_fma(delta_s, state_slice.to(torch.float32).t(),
                                  g_total_exp).to(torch.bfloat16).t()   # 单次舍入后量化
```

`mm(..., out_dtype=torch.float32)` 表示「bf16 输入、fp32 累加与输出」，对应 kernel 中留在 fp32 累加器里的量（L、delta_s）；普通 `matmul` 则是「bf16 输入、fp32 累加、bf16 输出」，对应 kernel 出口量化的量（Mqk、U、out）。

顺带回答一个自然疑问：**PyTorch 的 matmul（底层 cuBLAS）凭什么与 kernel 的 HMMA 逐位一致？** 机制上的解释是：bf16×bf16 的乘积尾数至多 16 位，在 fp32 中精确可表，因此 fp32 累加的舍入序列只取决于 k 维以多大步长、什么顺序推进；cuBLAS 的 bf16 GEMM 与 kernel 的 HMMA 都按 k=16 的顺序累加，舍入序列相同，逐位一致在经验上稳定成立。（这也是为什么该范式要求参考实现与 kernel 同步维护——任何一侧换了库版本或指令调度，都可能打破这个脆弱的巧合。）

#### 4.1.4 代码实践

**实践目标**：亲手确认「朴素 fp32 运算 ≠ FMA 单次舍入」，从而理解 `fp32_fma` 存在的必要性；再在 GPU 机器上验证「换掉归约顺序就破坏 bit-exact」。

**操作步骤**：

1. 写下面的脚本（示例代码，纯 CPU、只需 numpy，可独立运行）：

```python
# fma_probe.py —— 示例代码
import numpy as np

rng = np.random.default_rng(0)
f32, f64 = np.float32, np.float64

def naive(c, a, b):      # 双重舍入：先舍入乘积，再舍入加法（PyTorch 普通写法的行为）
    return f32(f32(a * b) + c)

def fma_emu(c, a, b):    # torch_ref.fp32_fma 的 numpy 版：fp64 中间量
    return f32(f64(c) + f64(a) * f64(b))

found = 0
for i in range(200000):
    a = f32(rng.standard_normal()); b = f32(rng.standard_normal()); c = f32(rng.standard_normal())
    if naive(c, a, b) != fma_emu(c, a, b):
        print(f"counterexample #{i}: a={a!r} b={b!r} c={c!r}\n"
              f"  naive={naive(c,a,b)!r}  fma_emu={fma_emu(c,a,b)!r}")
        found += 1
        if found >= 5:
            break
print(f"total counterexamples: {found}")
```

2. 在 GPU 机器上（需先按 u1-l3 安装好包）：复制 `tests/torch_ref.py` 为 `tests/torch_ref_naive.py`，把 `l2_normalize_kernel_match` 内部的蝶形循环替换成 `partials.sum(dim=-1)`（数学等价、顺序不同），再把 `test_fwd.py` 顶部的 `from torch_ref import torch_ref` 改为从新文件导入，运行 `python tests/test_fwd.py`。

**需要观察的现象**：步骤 1 应打印出若干「同输入、不同结果」的反例对；步骤 2 中 `assert torch.equal(out_kernel, out_ref)` 应当失败（或至少 Mqk/out 出现非零差异统计）。

**预期结果**：步骤 1 中随机搜索通常能在几万次试验内找到 1 个 ulp 级别的分歧（首个反例的具体编号与数值待本地验证）；步骤 2 中 exact match 失败，`print_error_stats` 打印出非零的 avg_rtol/max_rtol（具体量级待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`fp32_fma` 为什么必须经过 fp64，直接写 `c + a * b`（fp32）为什么不行？

**答案**：fp32 普通表达式先对 \(a\cdot b\) 舍入一次、再对加法舍入一次，是双重舍入；硬件 FMA 只在 \(c + a\cdot b\) 完成后舍入一次。fp64 尾数 53 位足以精确容纳两个 fp32 的乘积（≤48 位），中间加法只引入一次高精度舍入，最后降回 fp32，总体逼近单次舍入行为。kernel 的 FFMA 与朴素写法在部分输入上会差 1 个 ulp，足以摧毁 `torch.equal`。

**练习 2**：`l2_normalize_kernel_match` 中 `for offset in [8, 4, 2, 1]` 与 `indices ^ offset` 复刻的是 kernel 里的什么？

**答案**：K1 中 16 个 lane 之间的 `__shfl_xor_sync` 蝶形树归约——offset 从 8 到 1 依次折半，每步把配对 lane 的部分和相加，4 步之后所有 lane 持有相同的总和。参考实现用「按异或下标取数再相加」在张量层面重现同一棵归约树，保证每个部分和的舍入顺序与 kernel 一致。

**练习 3**：`torch.matmul(q_decayed, k_inv.t())` 与 `torch.mm(k_decayed, k_inv.t(), out_dtype=torch.float32)` 分别对应 kernel 里哪个中间量、什么精度策略？

**答案**：前者是 Mqk——fp32 累加但出口量化 bf16（K1 会把它 TMA store 到 workspace）；后者是 L——从构造、乘 beta 到送入求逆全程保持 fp32（u2-l7 / u3-l1 的精度主线）。`out_dtype` 参数就是参考实现表达「累加器精度」与「出口精度」分离的语言。

### 4.2 FLA 金标与误差可视化：量化「差多少才算对」

#### 4.2.1 概念说明

bit-exact 测试有一个逻辑缺口：它只证明 **kernel == 我们自己写的参考**。如果参考实现本身在数学上错了（比如公式推导有误），两边逐位相等也只能是「错得一致」。`test_fwd.py` 的第二组测试补上这一层：

- **金标（gold）**：用 FLA 的 `fused_recurrent_kda` 以 **fp64** 跑逐 token 递推。它不做 chunk 化，就是 KDA 数学定义的直译，因此它的误差只来自 fp64 舍入（≈可忽略），可以作为「真值」。
- **对照基线**：FLA 的 `chunk_kda`（Triton 实现、bf16 计算）——FlashKDA 要替换的那个实现。把 flash_kda 与它放在同一把尺子下量，回答「我们至少不比被替换者差」。

对拍的容差不再是 0：bf16 计算相对 fp64 真值本来就有 \(10^{-3}\) 量级的固有误差，所以断言用 `fla.utils.assert_close`，rtol 约 5e-3。同时测试还输出两类可视化证据：逐窗口误差曲线（诊断误差是否随 token 位置累积）与状态误差统计。

为什么这组测试只用 H=1？因为 fp64 逐 token 递推是 O(T) 串行、且在 GPU 上单算子并行度极低，H=96 会慢到不可接受——**金标可以慢，但必须有界地慢**。

#### 4.2.2 核心流程

`test_fwd_vs_fla` / `test_fwd_varlen_vs_fla` 的流程：

```text
make_test_cases → 10 个 (名称, g, dt_bias) 扫描用例
   6 个 g 场景（常数 -8/-4/0/8、U(-8,8)、N(0,8)，bias=0）
 + 4 个 bias 场景（{-4,4}、{-8,8}、U(-8,8)、N(0,8)，g=0）
对每个用例：
   ① run_fla_gold_reference:
        金标  = fused_recurrent_kda(fp64, 门控/beta 在 Python 里以 fp64 激活后传入)
        基线  = chunk_kda(bf16, kernel 内激活)
   ② run_flash_kda_batched:
        flash_kda.fwd(bf16, 传激活前 logits, 状态转 bf16)
   ③ 打印两者相对金标的 Output/State err_ratio
   ④ collect_windowed_errors: 每 WINDOW=8 个 token 一窗，记录 max/mean/RMSE 比
   ⑤ plot_error_comparison: 5 行 × 用例数 列 的图 + 底部状态误差文本
最后统一断言: assert_close(rtol=0.005) —— 输出硬断言, 状态用 warning=True 降级
存图: plot.png（fixed）/ plot_varlen.png（varlen）
```

误差比值的定义（[tests/test_fwd.py:L13-L16](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L13-L16)）：

\[
\text{err\_ratio}(x, y) \;=\; \frac{\sqrt{\tfrac{1}{n}\textstyle\sum_i (x_i - y_i)^2}}{\sqrt{\tfrac{1}{n}\textstyle\sum_i x_i^2} + 10^{-8}}
\]

即「误差的 RMS 占信号自身 RMS 的比例」，无量纲、便于跨用例比较。

#### 4.2.3 源码精读

**（1）g/bias 扫描用例：专打门控激活的极端区间。**

[tests/test_fwd.py:L45-L68](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L45-L68) 构造 10 个用例：g 取常数 -8、-4、0、8、均匀 U(-8,8)、高斯 N(0,8)（bias 全零），再补 4 组 g=0、dt_bias 取 {−4,4}、{−8,8}、U(-8,8)、N(0,8) 的组合。这些是**激活前 logits**（u1-l2 的约定），覆盖了 `e^{A_log}(g+dt_bias)` 进入 sigmoid 的饱和区与线性区——正是 `tanh.approx.f32` 与精确 sigmoid 可能分歧的区间，也是门控 cumsum 的动态范围被拉到极限的区间。

**（2）金标构造：把「激活」搬回 Python、用 fp64 直译数学定义。**

[tests/test_fwd.py:L71-L104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L71-L104)：

```python
g_fp64 = g.clone().to(torch.float64) + dt_bias.to(torch.float64).unsqueeze(0).unsqueeze(0)
g_activated_fp64 = lower_bound * torch.sigmoid(torch.exp(A_log_fp64.view(1, 1, H, 1)) * g_fp64)
beta_activated_fp64 = torch.sigmoid(beta.clone().to(torch.float64))
tri, tri_ht = fused_recurrent_kda(
    q=q.clone().to(torch.float64), ..., g=g_activated_fp64, beta=beta_activated_fp64,
    A_log=None, dt_bias=None, scale=scale, initial_state=h0.clone().to(torch.float64),
    output_final_state=True, use_qk_l2norm_in_kernel=True, use_gate_in_kernel=False,
    lower_bound=None, transpose_state_layout=True, ...)
```

要点：

- 金标侧把门控激活在 Python 里用 fp64 精确算好（`use_gate_in_kernel=False`、`A_log=None`、`dt_bias=None`、`lower_bound=None`），把「激活函数近似误差」与「递推结构误差」解耦——金标代表纯数学。
- beta 传**激活后**值；而 flash_kda 一侧传激活前 logits（kernel 内做 sigmoid）。接口约定的差异必须在适配层弥合。
- [tests/test_fwd.py:L106-L124](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L106-L124) 的注释点明：上游 chunk_kda 尚未实现 `use_beta_sigmoid_in_kernel`，所以对基线也显式传 post-sigmoid beta。三个实现在「谁负责激活」上各不相同，但对拍时数学语义被拉到同一基准。

**（3）flash_kda 侧的适配。**

[tests/test_fwd.py:L129-L141](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L129-L141) 把 FLA 布局的张量喂给 `flash_kda.fwd`：fp32 的 h0 转 bf16、out 预分配、返回 `final_state_fk.float()` 以便与 fp32 的金标比较。它示范了「对照测试的样板间」——被测系统与参照系之间总要有一层显式的语义对齐。

**（4）窗口化误差与可视化。**

[tests/test_fwd.py:L28-L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L28-L38) 的 `collect_windowed_errors` 把 T 维切成窗口（`WINDOW=8`），逐窗记录 `(起点, 终点, 最大误差, 平均误差, RMSE 比)`。只看全局均值会掩盖「误差沿位置单调增长」（递推系统典型病灶）；窗口统计一眼就能看出来。

[tests/test_fwd.py:L144-L215](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L144-L215) 的 `plot_error_comparison` 为每个用例画 5 行子图：原始 max 误差、20 点滑动平均后的 max、mean、RMSE 比直方图，蓝线是 flash_kda、红线是 chunk_kda；图下方以等宽字体追加每个用例的**状态**误差三联（max/mean/ratio）。读图时核心问题就一个：**蓝线是否始终压着或贴着红线**。

**（5）断言分层：输出硬、状态软。**

[tests/test_fwd.py:L359-L362](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L359-L362)：

```python
for r in results:
    assert_close(f"{r['name']} o", r["tri"], r["out"], 0.005)
    assert_close(f"{r['name']} ht", r["tri_ht"], r["final_state"], 0.005, warning=True)
    assert_close(f"{r['name']} chunk_kda ht", r["tri_ht"], r["chunk_ht"], 0.005, warning=True)
```

输出（o）相对金标是硬断言；最终状态（ht）——经历 T 步递推累积、且基线 chunk_kda 自身也在同一误差量级——用 `warning=True` 降级为告警，避免非回归性的数值波动把 CI 打红。varlen 版本（[L421-L423](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L421-L423)）把输出容差放宽到 0.006，为变长拼接多留一点余量。「哪个比较硬、哪个软、各给多少容差」本身就是测试设计决策，不是随手数字。

#### 4.2.4 代码实践

**实践目标**：理解窗口化误差统计能揭示「误差随位置增长」这一形态；并在 GPU 机器上产出并读懂一张对拍图。

**操作步骤**：

1. 先做 CPU 部分（示例代码，无需 GPU）——复刻指标函数并喂一个人造的「误差线性增长」信号：

```python
# err_window_demo.py —— 示例代码
import numpy as np

def windowed_stats(gold, pred, window):
    T = gold.shape[1]
    out = []
    for i in range(0, T, window):
        s = slice(i, min(i + window, T))
        d = np.abs(gold[:, s] - pred[:, s])
        out.append((i, d.max(), d.mean()))
    return out

rng = np.random.default_rng(0)
T = 512
gold = rng.standard_normal((1, T, 4)).astype(np.float32)
drift = np.linspace(0, 3e-2, T, dtype=np.float32)      # 人造: 误差随位置线性放大
pred = (gold + drift.reshape(1, T, 1)).astype(np.float32)

for i, mx, mn in windowed_stats(gold, pred, 8)[::8]:   # 每 64 token 抽样打印
    print(f"window@{i:4d}  max={mx:.4f}  mean={mn:.4f}")
```

2. GPU 部分：在装好 flash-linear-attention 与 matplotlib 的机器上直接运行 `python tests/test_fwd.py`（即 `tests/test.sh` 的第 4 步），等待生成 `tests/plot.png` 与 `tests/plot_varlen.png`。

**需要观察的现象**：步骤 1 打印出的 max/mean 随窗口起点单调上升；步骤 2 的图中蓝线（flash_kda）与红线（chunk_kda）的相对位置，以及底部等宽字体的状态误差三联。

**预期结果**：步骤 1 的窗口统计呈明显递增（本机 CPU 可直接验证）；步骤 2 中 flash_kda 的输出误差曲线应与 chunk_kda 同量级或更低、状态误差相当（`plot.png` 的具体数值待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么金标选 `fused_recurrent_kda`（逐 token 串行）而不是另一个 chunk 化实现？

**答案**：金标必须与被测实现**独立**且**更接近数学定义**。逐 token 递推不含任何 chunk 化近似（分块求逆、衰减变体拆分都是 chunk 化引入的结构），fp64 下它的结果就是数学真值；再套一个 chunked 实现做金标只能验证「两个 chunked 实现彼此一致」，无法发现双方共享的算法性错误。

**练习 2**：这组对拍测试为什么把 H 固定为 1，而 exact match 测试敢用 H=96？

**答案**：fp64 逐 token 递推是 O(T) 串行、GPU 并行度低，耗时与 T 成正比且无法靠 H 摊薄太多（H=8192 个 token 串行已经分钟级）；而 torch_ref 虽慢，但 chunk 循环内是 16×16/16×128 的批量 matmul，H=96 只是线性增加循环次数。测试预算决定了「金标对拍窄而深、exact match 广而全」的分工。

**练习 3**：`assert_close(..., warning=True)` 用在哪两类比较上？为什么不把它们也做成硬断言？

**答案**：用在 flash_kda 与 chunk_kda 的**最终状态**（ht）比较上。状态是 T 步递推的累积结果，误差天然大于单步输出，且被对照的 chunk_kda 自身状态误差也在同一量级——把它做成硬断言会因与基线同水平的正常数值波动频繁误报。输出 o 是模型直接消费的量、误差不随 T 累积，值得硬断言守住。

### 4.3 pytest 参数扫描与多 GPU 分配：580 个 exact-match 用例的工程组织

#### 4.3.1 概念说明

exact match 断言只有在**铺满参数空间**时才有威慑力：状态 IO 有 4 种组合 × 状态 dtype 2 种（对应 u2-l3 的 7 分支模板分发），序列形状有 fixed/varlen/batched/长序列四大类。如果在一个测试函数里 for 循环遍历，第一个失败就会掩盖后面所有失败；`pytest.mark.parametrize` 把每个组合变成**独立用例**——独立 id（如 `test_fwd_varlen[in+out-bf16-H4-seqs17_33_65]`，形如）、独立失败、独立重跑，还能被 xdist 拆给多张 GPU。

于是产生一个工程问题：**xdist 的每个 worker 是独立进程，默认 16 个 worker 全部挤在 CUDA_VISIBLE_DEVICES 未设时的同一批设备上**（实际上会全部落在 device 0 附近互相争抢）。`conftest.py` 用 14 行解决这个问题：worker 编号对 GPU 数取模，把每张卡分配给固定 worker。

#### 4.3.2 核心流程

用例规模的乘法结构（当前 HEAD 的常量值）：

| 测试函数 | 参数维度 | 用例数 |
| --- | --- | --- |
| `test_fwd_fixed` | H(4) × T(9) × dtype(2) × IO(4) | 288 |
| `test_fwd_varlen` | H(3) × seq_lens(8) × dtype(2) × IO(4) | 192 |
| `test_fwd_batched` | H(3) × (B,T)(4) × dtype(2) × IO(4) | 96 |
| `test_fwd_long` | T ∈ {131072, 1048576} | 2 |
| `test_fwd_long_varlen` | seq_lens ∈ {[131072], [524288, 524288]} | 2 |
| **合计** | | **580** |

GPU 分配流程（conftest 钩子）：

```text
pytest 启动（FLASH_KDA_DIST_GPU=1 时钩子生效）
→ 每个 xdist worker 进程执行 pytest_configure
→ 读环境变量 PYTEST_XDIST_WORKER（形如 "gw0"、"gw7"）
→ worker_id = int(worker 去 "gw") ; gpu_id = worker_id % GPU 数
→ 设置 CUDA_VISIBLE_DEVICES=str(gpu_id)，再 torch.cuda.set_device(0)
→ 该 worker 内所有测试都跑在分配到的物理卡（以 device 0 的面目出现）上
```

`run_test_full.sh` 的三步：可编辑安装 → 装 pytest 与 pytest-xdist → `cd tests && FLASH_KDA_DIST_GPU=1 pytest test_fwd_full.py -x -v -n 16`。`-x` 在首个失败即停（580 个用例失败后继续跑纯属浪费 GPU 时长），`-n 16` 起 16 个 worker——GPU 不足 16 张时，取模分配让多 worker 共享一张卡（正确性测试吞吐下降但结果不受影响）。

#### 4.3.3 源码精读

**（1）文件头与输入构造：固定种子 + 确定性状态。**

[tests/test_fwd_full.py:L1-L12](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L1-L12) 的 docstring 概括了扫描维度（注意它写的是 "H values (up to 256)"，而实际 `H_VALUES = [1, 4, 32, 96]`——**读代码时以常量列表为准**，文档性注释可能滞后）。

[tests/test_fwd_full.py:L26-L44](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L26-L44) 的两个构造器值得注意：

```python
def _make_inputs(T, H, device="cuda"):
    torch.manual_seed(42)          # 固定种子：失败可复现，跨机器可对比
    ...
def _make_state(shape, dtype):
    n_elems = 1
    for s in shape: n_elems *= s
    return torch.arange(n_elems, ...).reshape(shape).to(torch.bfloat16).to(dtype)
```

`_make_state` 用 `arange` 而不是随机数：完全确定、含 0、且 H·D·D 最大到 96×128×128≈157 万，天然覆盖大数值动态范围——状态张量的「测试指纹」不依赖任何随机性。`.to(bf16).to(dtype)` 的中间 bf16 停留点也与 kernel 的状态语义（u3-l6：StateFP32 只改 gmem I/O dtype，片上恒 bf16）对齐：fp32 状态用例里的初值同样是「bf16 可表示的 fp32 值」。

**（2）参数装饰器与 ids。**

[tests/test_fwd_full.py:L47-L69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L47-L69)：

```python
STATE_IO = [(True, True), (True, False), (False, True), (False, False)]
STATE_IO_IDS = ["in+out", "in_only", "out_only", "no_state"]
STATE_DTYPES = ["bf16", "fp32"]
H_VALUES = [1, 4, 32, 96]
T_VALUES = [16, 64, 256, 1024, 4096, 8192, 17, 37, 97]

@pytest.mark.parametrize("H", H_VALUES, ids=[f"H{h}" for h in H_VALUES])
@pytest.mark.parametrize("T", T_VALUES, ids=[f"T{t}" for t in T_VALUES])
@pytest.mark.parametrize("state_dtype", STATE_DTYPES)
@pytest.mark.parametrize("has_in,has_out", STATE_IO, ids=STATE_IO_IDS)
def test_fwd_fixed(T, H, state_dtype, has_in, has_out):
```

T 列表刻意混入 17、37、97 三个奇数——尾块（actual_len<16）路径（u3-l7）由此覆盖；H 覆盖 1（单 head 边界）、4（恰好每 warp 一个列块）、96（实际模型规模）。`ids=` 让失败报告里出现 `H4`、`in+out` 这样可读的片段，而不是 `True-True`。

**（3）双跑 + 带上下文的断言。**

[tests/test_fwd_full.py:L70-L94](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L70-L94) 的 `test_fwd_fixed` 主体就是「kernel 一跑、ref 一跑、torch.equal」：

```python
flash_kda.fwd(q, k, v, g, beta, scale, out_kernel, ..., initial_state=init_k, final_state=final_k)
...
torch_ref(q, k, v, g, beta, scale, out_ref, ..., initial_state=init_r, final_state=final_r)
assert torch.equal(out_kernel, out_ref), \
    f"output mismatch: T={T} H={H} dtype={state_dtype} in={has_in} out={has_out}"
if final_k is not None:
    assert torch.equal(final_k, final_r), ...
```

注意 kernel 与 ref 拿到的是**各自克隆的**初态（`init_k`/`init_r`），避免任何一侧原地修改造成交叉污染；断言消息把四个参数全部带上，580 个用例里任何一个失败都能立即定位到具体组合。

**（4）varlen / batched / long 三类变体。**

- [tests/test_fwd_full.py:L101-L150](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L101-L150) `test_fwd_varlen`：`VARLEN_CASES` 从 `[16]`（单序列整块）、`[4, 8, 12]`（全部短于一个 chunk）、`[17, 33, 65]`（全尾块）到 `[1024]*8`（N=8 均匀）与 8192 总长混合，cu_seqlens 由 `cumsum` 现场构造，状态形状是 `[N,H,D,D]`。
- [tests/test_fwd_full.py:L157-L205](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L157-L205) `test_fwd_batched`：B>1、等长，**不传 cu_seqlens**——覆盖 u1-l5 讲过的 batched 模式（C++ 侧自动按 B 切）。
- [tests/test_fwd_full.py:L212-L274](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L212-L274) 长序列组：只测 H=1、in+out、bf16 一种组合——131072 与 1048576（2^17 与 2^20）个 token 主要为了检验长序列下 Python 循环与 workspace 尺寸（`H × total_tiles`）的行为，把参数维度收缩到最小以控制总时长。

**（5）conftest.py：14 行的多 GPU 分配。**

[tests/conftest.py:L1-L14](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/conftest.py#L1-L14) 全文：

```python
def pytest_configure(config):
    if os.environ.get("FLASH_KDA_DIST_GPU") != "1":
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", None)
    if worker is not None:
        gpu_count = torch.cuda.device_count()
        worker_id = int(worker.replace("gw", ""))
        gpu_id = worker_id % gpu_count
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        torch.cuda.set_device(0)
```

四个细节：

- `FLASH_KDA_DIST_GPU=1` 门控：普通单进程/单卡运行完全不受影响，只有显式 opt-in 时才改写设备可见性——钩子是无侵入的。
- `pytest_configure` 在测试模块导入与任何 CUDA 调用之前执行，此刻改 `CUDA_VISIBLE_DEVICES` 才生效（CUDA 上下文一旦初始化，这个环境变量就不再被读取）。
- 取模分配意味着 worker 数可以超过 GPU 数：16 worker × 8 卡 = 每卡 2 个 worker 共享；correctness 测试对吞吐不敏感，可接受。
- mask 之后物理卡以 device 0 的身份出现，所以最后要 `torch.cuda.set_device(0)` 把默认设备钉住。

**（6）两个入口脚本的分工。**

[tests/run_test_full.sh:L1-L4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/run_test_full.sh#L1-L4)：`pip install -e .` → `pip install pytest pytest-xdist` → `cd tests && FLASH_KDA_DIST_GPU=1 pytest test_fwd_full.py -x -v -n 16`。`cd tests` 保证 `from torch_ref import torch_ref` 这类同目录导入在任意调用路径下都成立。

对照 [tests/test.sh:L1-L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh#L1-L5)：快速路径只跑 `test_fwd.py`（exact match + FLA 对拍，含依赖安装 `flash-linear-attention>=0.5.0` 与 matplotlib），是日常开发的分钟级反馈环；全量路径 580 个 exact-match 用例是合入前的回归底线。

#### 4.3.4 代码实践

**实践目标**：验证参数乘积、检视用例 id 清单，并观察 conftest 的 GPU 分配行为。

**操作步骤**：

1. 手算或用脚本复核 580：`4*9*2*4 + 3*8*2*4 + 3*4*2*4 + 2 + 2`（任何有 Python 的机器可做）。
2. 在 GPU 机器（已 `pip install -e .`、`pip install pytest pytest-xdist`）上列出用例而不执行：

```bash
cd tests && pytest test_fwd_full.py --collect-only -q | tail -3
```

3. 观察分配行为：分别以 `FLASH_KDA_DIST_GPU=0` 与 `FLASH_KDA_DIST_GPU=1` 运行两个 worker 的小规模扫描：

```bash
FLASH_KDA_DIST_GPU=0 pytest test_fwd_full.py -v -n 2 -k "long and T131072" 2>&1 | head -20
FLASH_KDA_DIST_GPU=1 pytest test_fwd_full.py -v -n 2 -k "long and T131072" 2>&1 | head -20
```

（如需直接看到分配结果，可临时在 conftest 的 `set_device` 前加一行 `print(worker, "->", gpu_id)` 观察，看完还原。）

**需要观察的现象**：步骤 2 输出的用例总数；步骤 3 两种模式下 worker 的设备占用差异（单卡机器上现象是两者都落在卡 0，多卡机器上模式 1 会把 gw0/gw1 分到不同卡）。

**预期结果**：步骤 2 应报告 580 个用例（`580 tests collected` 字样，待本地验证）；步骤 3 在多卡机器上 `nvidia-smi` 应能看到两个进程分布在不同 GPU（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：580 这个数怎么来的？如果给 `VARLEN_CASES` 追加一条 `[129, 63]`，总数变成多少？

**答案**：288（fixed）+ 192（varlen）+ 96（batched）+ 2（long）+ 2（long varlen）= 580。追加一条 varlen 用例：VARLEN_H(3) × 新 case(1) × dtype(2) × IO(4) = 24 个新用例，总数 604。

**练习 2**：16 个 worker、8 张 GPU 时会发生什么？为什么正确性测试可以容忍这种情况？

**答案**：`worker_id % 8` 使每张卡被 2 个 worker 共享。每个 worker 是独立进程、独立 CUDA 上下文，测试只依赖单上下文内的确定性，不涉及跨进程竞态；共享只降低吞吐（SM/显存争抢），不影响结果正确性。这正是把分配逻辑做成「取模」而非「一一对应」的原因——worker 数不必等于 GPU 数。

**练习 3**：为什么 conftest 的分配钩子要用 `FLASH_KDA_DIST_GPU` 环境变量门控，而不是无条件生效？

**答案**：conftest.py 放在 tests 目录下会被**所有** pytest 调用自动加载，包括 CI 上的单卡运行、IDE 里的单用例调试。无条件改写 `CUDA_VISIBLE_DEVICES` 会破坏这些场景的设备选择（例如用户特意用 `CUDA_VISIBLE_DEVICES=2 pytest ...` 指定卡 3 时会被覆盖）。环境变量门控让行为改变成为显式 opt-in，`run_test_full.sh` 里那一句 `FLASH_KDA_DIST_GPU=1` 就是这个 opt-in 的落点。

## 5. 综合实践

**任务**：按本讲规格完成一次「新增 exact-match 用例 → 单点验证 → 全量回归」的完整闭环。这是二开流程「改一处 → 重编译 → exact-match 回归」的最小演练。

**方案 A（推荐）：给 varlen 扫描补一条奇数长度用例。**

1. `git checkout -b tutorial-add-case`（本实践只改测试文件，但养成开分支的习惯）。
2. 编辑 `tests/test_fwd_full.py`，在 [VARLEN_CASES（L101-L110）](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L101-L110) 列表末尾追加：

```python
VARLEN_CASES = [
    ...
    [17, 33, 65],
    [129, 63],      # 新增：129 非 16 的倍数 → 多尾块；63 与 129 互质 → 两个尾块偏移不同
]
```

3. **不需要**改 `_make_inputs` / `_make_state`：前者按 `(T_total, H)` 参数化构造，后者按 `(N, H, D, D)` 形状参数化构造，新用例自动适配——这正是这两个 helper 被设计成「形状全参数化」的原因。
4. 单点验证新用例：

```bash
cd tests && pytest test_fwd_full.py -v -k "seqs129_63"
```

预期收集到 3(H) × 2(dtype) × 4(IO) = 24 个用例并全部通过（exact match）。

5. 全量回归：

```bash
cd <仓库根目录> && bash tests/run_test_full.sh
```

即 `FLASH_KDA_DIST_GPU=1 pytest test_fwd_full.py -x -v -n 16`，总数应从 580 变为 604 且全部通过（GPU 机器上待本地验证）。

6. `git checkout master && git branch -D tutorial-add-case` 还原。

**方案 B（更重）：把 `VARLEN_H` 从 `[1, 4, 96]` 扩成 `[1, 4, 8, 96]`。** 新增 8 cases × 2 × 4 = 64 个用例（总数 644），用于观察 H=8（4 个 MMA warp 恰好人手一个 32 列块之外的非 4 倍数 head 数）的行为；全量回归耗时明显增加，机器紧张时选方案 A 即可。

**如果 exact match 失败**：断言消息会带全 `seq_lens/H/dtype/in/out` 上下文；用 `-x` 停在首个失败，把该参数组合缩小到单 head 单 chunk（借鉴 u2-l1 的 dump 方法打印 `torch_ref` 的中间量），与 workspace 侧 K1 产物逐段比对定位。失败本身也是有价值的产出——它意味着你找到了 kernel 与参考在某个未覆盖路径上的分歧。

## 6. 本讲小结

- FlashKDA 的正确性由三层防线构成：`torch_ref` 的 **bit-exact 对拍**（`torch.equal`，证明 kernel 与规格书逐位一致）、**FLA fp64 金标对拍**（`assert_close` rtol≈5e-3，证明数学上可信且不劣于被替换的 chunk_kda）、**580 用例参数扫描**（把 exact match 铺满 4 IO × 2 dtype × 多形状）。
- 「参考实现模拟硬件行为」的三类技巧：`fp32_fma` 用 fp64 中间量模拟 FMA 单次舍入；`fp32_ex2_ftz`（exp2+次规格化冲零）、`load_inline` 编译的 tanh-sigmoid、蝶形归约与前代换的固定循环顺序复刻近似指令与归约顺序；`mm(out_dtype=fp32)` 与普通 matmul 的分工表达 fp32 累加器与 bf16 出口的量化点差异。
- 该范式的代价：参考实现是 Python 多重循环、只能测试用；依赖 `out_dtype` 等较新的 PyTorch 特性；cuBLAS 与 HMMA 的逐位一致依赖「bf16 乘积在 fp32 精确 + k 维同序累加」这一脆弱巧合，kernel 与参考必须同步演进。
- 金标设计的三个原则：选与被测实现独立的逐 token 递推（无 chunk 化误差）、fp64 + Python 侧精确激活（把激活近似与结构误差解耦）、规模收缩到 H=1 控制时长；断言分层——输出硬断言、累积误差大的状态用 `warning=True` 降级。
- 参数扫描用 `parametrize + ids` 让每个组合成为可独立失败的可读用例；`conftest.py` 用 14 行钩子完成 `worker_id % gpu_count` 的多 GPU 分配，`FLASH_KDA_DIST_GPU` 门控保证无侵入；`run_test_full.sh` 的 `-x -v -n 16` 是「首个失败即停 + 16 worker 并行」的回归标配。

## 7. 下一步学习建议

- **u3-l10（基准与剖析）**：正确性闭环之后自然是性能闭环——`benchmarks/bench_fwd.py` 的 `bench_fn` 计时框架与 `ncu.sh` 抓取两个 kernel 指标的方法，与本讲的 `run_test_full.sh` 构成「先回归后测速」的完整流程。
- **u3-l11（FLA 后端集成）**：本讲把 FLA 用作金标与基线；下一讲看它的另一半角色——上游 `chunk_kda` 如何自动分发到 flashkda，`FLA_FLASH_KDA=0` 如何强制回退 Triton 路径。
- **延伸阅读建议**：对照读 FLA 源码中的 `fla.utils.assert_close`（确认 `warning=True` 的确切语义）；若你将在 u3-l12 做消融实验，先回到本讲把「改一处 → 重编译 → `run_test_full.sh` 回归」的流程练熟——那是所有二开实验的安全网。
