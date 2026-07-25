# 归一化归约内核：rms_norm 与 layer_norm

## 1. 本讲目标

本讲以 cuTile 版 `rms_norm` 为主样本、`layer_norm` 为对照样本，讲解 TileGym 中「跨列归约类内核」的写法。读完本讲，你应当能够：

- 说清楚 RMSNorm 与 LayerNorm 在数学上的区别，以及它们为什么都要在最后一维上做**归约（reduction）**。
- 看懂内核里 mean / rstd 是怎么用 `ct.sum` 沿某一轴算出来的，以及为什么必须升到 fp32 再算。
- 理解归一化之后的「仿射变换（affine）」——`weight`/`bias`（以及 Gemma3 用的 `offset`）是如何施加的。
- 掌握 `_RMSNorm.forward` 如何用 `mode` 在 `static_persistent`、`multi_wave_reload`、`multi_wave_cached` 三种调度之间选择，以及默认启发式的判据。
- 理解 `get_rms_norm_module` 这个「模块工厂」是如何按模型名（Llama / Gemma3）返回不同的 `nn.Module` 类的。

本讲承接 [u3-l2 数据搬运原语](u3-l2-data-movement.md)：那里讲过 `gather/scatter` 与 `load/store` 两种加载原语、`ct.arange` 偏移基底、`check_bounds` 边界、`astype` 类型转换。本讲会反复用到这些原语，但不再重复讲解，重点放在「归约 + 仿射 + 调度」上。

## 2. 前置知识

### 2.1 什么是归一化（Normalization）

Transformer 里每一层都会做归一化，目的是让每个 token（一行）的特征数值分布稳定，避免训练时数值爆炸或消失。常见的两种：

- **LayerNorm**：减均值、除标准差。需要先算均值 \(\mu\)，再算方差 \(\sigma^2\)，**两遍扫描**。
- **RMSNorm**：LayerNorm 的简化版，不减均值，只除「均方根（RMS）」。**只需一遍扫描**算 \(\sum x_i^2\)，更快，是 Llama / Qwen / DeepSeek 等现代 LLM 的主流选择。

### 2.2 归约（reduction）是什么

「归约」就是把一个向量的多个元素压成一个数。归一化需要沿**最后一维（列）**做归约：

- RMSNorm 需要算 \(\sum_{i} x_i^2\)（一个标量）。
- LayerNorm 需要算 \(\sum_i x_i\)（均值）和 \(\sum_i (x_i-\mu)^2\)（方差）。

这和 [u4-l1 silu_and_mul](u4-l1-elementwise-kernel.md) 的「逐元素内核」有本质区别：逐元素内核里每个输出元素只依赖对应的输入元素，没有跨元素依赖；归一化内核里每个输出元素都依赖**同一行所有输入元素**（因为要先算出该行的均值/方差）。这种「先扫一遍算统计量、再扫一遍写输出」的模式，正是本讲的核心。

### 2.3 数学记号

本讲用 \(\) 和 \[\] 写公式。设输入为一行 \(N\) 个元素 \(x_1,\dots,x_N\)，权重 \(w\)，偏置 \(b\)，小常数 \(\epsilon\)。

RMSNorm：

\[
\text{rms} = \frac{1}{\sqrt{\dfrac{1}{N}\sum_{i=1}^{N} x_i^2 + \epsilon}},\qquad
y_i = x_i \cdot \text{rms} \cdot (\text{offset} + w_i)
\]

LayerNorm：

\[
\mu = \frac{1}{N}\sum_i x_i,\qquad
\sigma^2 = \frac{1}{N}\sum_i (x_i-\mu)^2,\qquad
\text{rstd} = \frac{1}{\sqrt{\sigma^2 + \epsilon}},\qquad
y_i = (x_i-\mu)\cdot \text{rstd}\cdot w_i + b_i
\]

注意：cuTile 内核里 `rms`/`rstd` 存的就是这里的倒数 \(\text{rms}\) / \(\text{rstd}\)（由 `ct.rsqrt` 直接算出），所以后续是**乘法**而非除法。

### 2.4 前置术语回顾

来自前几讲、本讲直接使用的术语：`@ct.kernel`、`ConstInt`（编译期常量）、`ct.bid(0)`/`ct.num_blocks(0)`、grid-stride 持久化调度、`.contiguous()`、`ct.launch(stream, grid, kernel, args)`、`@register_impl`、`@dispatch`、`torch.autograd.Function`。如有遗忘，可回看 [u3-l1](u3-l1-cutile-kernel-basics.md) 与 [u3-l3](u3-l3-launch-patterns.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | `rms_norm`、`get_rms_norm_module`、`layer_norm_legacy`、`persistent_layer_norm` 的统一接口 stub（只抛 `NotImplementedError`）。 |
| [src/tilegym/ops/cutile/rms_norm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py) | cuTile 版 RMSNorm 全部实现：三种前向内核、反向内核、`_RMSNorm` autograd 封装、`_TileRMSNorm`/`_RMSNormForGemma3` 模块、`get_rms_norm_module` 工厂。**本讲主样本。** |
| [src/tilegym/ops/cutile/layer_norm_legacy.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/layer_norm_legacy.py) | cuTile 版 LayerNorm（legacy + persistent），作为「带均值/偏置」的对照样本。 |
| [src/tilegym/ops/cutile/utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py) | `next_power_of_2` 等工具（用于决定 TILE_SIZE）。 |
| [tests/ops/test_rms_norm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_rms_norm.py) | RMSNorm 的正确性 + 性能测试，参数化 `mode` 与 `backend`，是最权威的用法范例。 |

> 说明：`layer_norm_legacy.py` 在本讲只作对照（展示 mean/var 两遍扫描与 bias 仿射），rms_norm.py 才是主线。

---

## 4. 核心概念与源码讲解

### 4.1 归约计算：mean / rstd 是怎么算出来的

#### 4.1.1 概念说明

归一化的第一步是算统计量。RMSNorm 只需要 \(\sum x_i^2\)，LayerNorm 还需要均值 \(\mu\)。这些统计量都是「把一行 \(N\) 个数压成一个标量」，这就是归约。

关键约束：

1. **必须升到 fp32 再归约**。LLM 输入通常是 fp16/bf16，若直接在低精度下累加 \(N\) 个平方项（\(N\) 可能上万），累加器会丢精度，导致方差算错、归一化结果失真。所以内核里固定出现 `xj = ct.astype(xj, ct.float32)` 再累加。
2. **累加器要用 `ct.full(..., 0.0)` 初始化为零**，然后在循环里 `+= x*x`。这与 [u3-l4 softmax](u3-l4-softmax-deep-dive.md) 的 `exp` 累加器是同一种写法。
3. **`ct.rsqrt` 一步到位算出倒数**。内核里存的 `rms`/`rstd` 已经是 \(1/\sqrt{\dots}\)，后续直接相乘，省一次除法（GPU 上除法比乘法贵）。

#### 4.1.2 核心流程

RMSNorm 前向归约的伪代码：

```
row = ct.bid(0)                     # 这个 block 负责第 row 行
_rms = 0.0  (一个长度 TILE_SIZE 的 fp32 累加器)
for 每个列块 j:
    xj = gather(x, (row, 列块偏移))   # 加载一段列
    xj = astype(xj, fp32)
    _rms += xj * xj                  # 累加平方
rms = rsqrt( sum(_rms) / N + eps )   # 标量：这一行的倒数均方根
scatter(Rstd, row, rms)              # 留给反向用
```

注意 `sum(_rms, axis=0)` 这一步——把长度 `TILE_SIZE` 的累加器沿轴 0 归约成**一个标量**，就是这一行的 \(\sum x_i^2\)（分块累加后的总和）。

#### 4.1.3 源码精读

**样本一：multi_wave_reload 版的归约**（最直观的分块累加）。

[src/tilegym/ops/cutile/rms_norm.py:74-88](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L74-L88) 是 `_rms_norm_kernel_gather` 的归约段：先 `num_tiles = ct.cdiv(N, TILE_SIZE)` 算出要分多少块，再 `for j in range(0, num_tiles)` 逐块 `gather` 出列段、升 fp32、`_rms += xj*xj`，最后 `rms = ct.rsqrt(ct.sum(_rms, axis=0, keepdims=False) / N + EPS)`。这一段完美对应上面的伪代码。

```python
row = ct.bid(0)
_rms = ct.full((TILE_SIZE,), 0.0, dtype=ct.float32)
num_tiles = ct.cdiv(N, TILE_SIZE)
offsets = ct.arange(TILE_SIZE, dtype=ct.int32)
for j in range(0, num_tiles):
    offs = j * TILE_SIZE + offsets
    xj = ct.gather(x, (row, offs), check_bounds=check_bound, latency=1)
    xj = ct.astype(xj, ct.float32)
    _rms += xj * xj
rms = ct.rsqrt(ct.sum(_rms, axis=0, keepdims=False) / N + EPS)
ct.scatter(Rstd, row, rms)
```

**样本二：static_persistent 版的归约**（一次加载整个 TILE_M×TILE_N 的二维瓦片）。

[src/tilegym/ops/cutile/rms_norm.py:154-163](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L154-L163) 用 `ct.load` 一次取 `(TILE_SIZE_M, TILE_SIZE_N)` 的二维瓦片（多行），然后 `x_squared = x * x`，再用 `ct.sum(x_squared, axis=1, keepdims=True)` 沿**轴 1（列）**归约，得到每行的方差。注意这里归约轴是 1 而不是 0——因为数据是二维瓦片，行在轴 0、列在轴 1，要按行归约就必须沿轴 1 求和。这正是 [u3-l2](u3-l2-data-movement.md) 强调过的「换加载原语（gather→load）必须同步换归约轴」的规则。

```python
x_squared = x * x
x2_sum = ct.sum(x_squared, axis=1, keepdims=True)   # 每行一个值：[TILE_SIZE_M, 1]
variance = x2_sum / N_f32
eps_tensor = ct.full((TILE_SIZE_M, 1), EPS, dtype=ct.float32)
rsqrt_var = ct.rsqrt(variance + eps_tensor)
```

**样本三：LayerNorm 的两遍扫描（对照）**。

RMSNorm 只算 \(\sum x^2\) 一遍；LayerNorm 要先算均值，再算方差。看 [src/tilegym/ops/cutile/layer_norm_legacy.py:80-99](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/layer_norm_legacy.py#L80-L99)：第一遍循环算 `mean = sum(_mean)/N`，第二遍循环用 `x = where(mask, x-mean, 0)` 减均值再算 `_var += x*x`，得 `var = sum(_var)/N`，最后 `rstd = rsqrt(var+EPS)`。这里用了一个常见技巧：把越界列（`cols < N` 不成立）置零，避免 padding 元素污染均值/方差。

persistent LayerNorm 内核还用了更巧妙的「单遍」方差公式 \( \sigma^2 = E[x^2] - (E[x])^2 \)，见 [src/tilegym/ops/cutile/layer_norm_legacy.py:162-167](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/layer_norm_legacy.py#L162-L167)，只需一遍循环同时累加 `sum(x*x)` 和 `sum(x)`。

#### 4.1.4 代码实践

**实践目标**：验证「升 fp32 归约」的必要性，亲手看到低精度累加会出错。

**操作步骤**（纯 PyTorch，不依赖 GPU 内核，本地 CPU 也能跑出趋势）：

1. 写一个长度 \(N=32768\) 的 fp16 随机向量 `x`。
2. 分别用两种方式算 \(\sum x_i^2\)：(a) 在 fp16 下 `.pow(2).sum()`；(b) 先 `.float()` 再 `.pow(2).sum()`。
3. 比较两者的相对误差。

**需要观察的现象**：fp16 直接累加上万项时，结果与 fp32 基准有明显偏差。

**预期结果**：相对误差非零（数量级取决于具体值，但通常可见）。这正好解释了内核里为何处处 `ct.astype(..., ct.float32)`。

**示例代码**（这是示例代码，不是项目原有代码）：

```python
import torch
N = 32768
x = torch.randn(N, dtype=torch.float16)
s_lo = (x.pow(2)).sum().item()                    # fp16 累加
s_hi = (x.float().pow(2)).sum().item()            # 升 fp32 累加（基准）
print(f"fp16={s_lo:.3f}  fp32={s_hi:.3f}  rel_err={abs(s_lo-s_hi)/s_hi:.3e}")
```

> 如果你的机器没有可用 GPU 或不确定具体数值，请标注「待本地验证」，不要假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：`_rms_norm_kernel_gather` 里 `ct.sum(_rms, axis=0, keepdims=False)` 的 `axis=0` 改成 `axis=1` 会怎样？

**答案**：会编译/运行出错或语义错误。`_rms` 是一维 `(TILE_SIZE,)` 瓦片，只有一个轴（轴 0），沿不存在的轴 1 归约无意义。一维瓦片归约永远沿轴 0；只有像 static_persistent 那样的二维 `(TILE_SIZE_M, TILE_SIZE_N)` 瓦片才有轴 1（列）可归约。

**练习 2**：为什么 `rms = ct.rsqrt(...)` 之后，后续用的是乘法 `xj * rms` 而不是除法 `xj / sqrt(...)`？

**答案**：`rsqrt` 一次性算出 \(1/\sqrt{\cdot}\)（倒数），省掉一次除法。GPU 上除法指令比乘法慢，预先取倒数再用乘法是归一化内核的标准优化。

---

### 4.2 仿射变换：weight / bias / offset 如何施加

#### 4.2.1 概念说明

归一化得到的 \(\hat{x}\) 只是「分布稳定」了，但每个特征维度还需要单独的缩放和偏移，这就是**仿射变换（affine）**：

- LayerNorm：\(y_i = \hat{x}_i \cdot w_i + b_i\)（有 weight 和 bias 两个可学参数）。
- RMSNorm（Llama）：\(y_i = \hat{x}_i \cdot w_i\)（只有 weight，无 bias）。
- RMSNorm（Gemma3）：\(y_i = \hat{x}_i \cdot (1 + w_i)\)（weight 初始化为 0，等价于乘一个「1 + 偏移」）。这就是 `offset` 参数的来源。

cuTile 把这统一写成 \(y_i = \hat{x}_i \cdot (\text{offset} + w_i)\)：Llama 传 `offset=0.0`，Gemma3 传 `offset=1.0`，内核代码完全相同。这是一个很漂亮的设计——同一份内核服务两种模型族。

仿射是逐元素操作（每列一个 \(w_i\)），不涉及归约，所以它和归约可以融合在同一个内核里：归约算完 `rms` 后，紧接着再扫一遍（或复用已加载的瓦片）做 `yj = xj * rms * (offset + wj)` 写回。**融合**避免了把中间 \(\hat{x}\) 写回显存再读出来，省一次往返。

#### 4.2.2 核心流程

```
# 归约完成后，rms 已知
for 每个列块 j:
    wj = gather(w, 列块偏移)          # 该列的权重
    wj = astype(wj, fp32)
    xj = gather(x, (row, 列块偏移))   # 重新加载（或复用缓存的）输入
    xj = astype(xj, fp32)
    yj = xj * rms * (offset + wj)     # 归一化 + 仿射，融合计算
    yj = astype(yj, x.dtype)          # 存回原精度
    scatter(out, (row, 列块偏移), yj)
```

注意 `multi_wave_reload` 版（gather 内核）会**重新加载**一遍 `x`（先加载算归约，再加载做仿射），因为它的累加器只存了平方和、没缓存原始 `x`；而 `multi_wave_cached` 版会把 `x` 缓存到寄存器里复用（见 4.3）。

#### 4.2.3 源码精读

**RMSNorm 仿射（multi_wave_reload 版）**：[src/tilegym/ops/cutile/rms_norm.py:90-99](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L90-L99)。第二个 `for j` 循环重新 gather `w` 和 `x`，做 `yj = xj * rms * (OFFSET + wj)`，astype 回原精度后 scatter。

```python
for j in range(0, num_tiles):
    offs = j * TILE_SIZE + offsets
    wj = ct.gather(w, offs, check_bounds=check_bound, latency=1)
    wj = ct.astype(wj, ct.float32)
    xj = ct.gather(x, (row, offs), check_bounds=check_bound, latency=1)
    xj = ct.astype(xj, ct.float32)
    yj = xj * rms * (OFFSET + wj)          # offset=0(Llama) 或 1(Gemma3)
    yj = ct.astype(yj, x.dtype)
    ct.scatter(out, (row, offs), yj, latency=1)
```

**RMSNorm 仿射（static_persistent 版，二维广播）**：[src/tilegym/ops/cutile/rms_norm.py:168-186](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L168-L186)。这里 `weight` 在循环外只加载一次（所有 tile 共享），用 `ct.reshape(w, (1, TILE_SIZE_N))` 广播成 `(1, N)` 与 `(TILE_SIZE_M, N)` 的 `x_normalized` 相乘，最后 `ct.store(...allow_tma=False, latency=3)` 写回。注释 `# +30% perf` 表明关闭 TMA 写回反而更快——这是经验性的调优提示。

**LayerNorm 仿射（带 bias）**：[src/tilegym/ops/cutile/layer_norm_legacy.py:110-121](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/layer_norm_legacy.py#L110-L121)。`x_hat = (x - mean) * rstd`，再 `y = x_hat * w + b`，多了一个 bias 项，其余结构同上。

#### 4.2.4 代码实践

**实践目标**：验证 `offset` 参数如何让同一内核支持 Llama 与 Gemma3 两种语义。

**操作步骤**：

1. 用 PyTorch 手写两种 RMSNorm 参考：(a) Llama 风格 `y = x*rms * w`；(b) Gemma3 风格 `y = x*rms * (1 + w)`，`w` 初始化为 0。
2. 构造一组 `x`，让两种参考都跑一遍。
3. 阅读内核公式 `yj = xj * rms * (offset + wj)`，确认 `offset=0` 还原 (a)、`offset=1` 还原 (b)。

**需要观察的现象**：Gemma3 在 `w=0` 时 `y = x*rms * 1 = x*rms`（恒等缩放），而 Llama 在 `w=0` 时 `y = 0`（输出全零）。这解释了为何 Gemma3 把 weight 初始化为 0、配 `offset=1`。

**预期结果**：两套公式与 `offset` 取值一一对应。

**示例代码**（示例代码）：

```python
import torch
def rmsnorm_ref(x, w, offset):
    var = x.float().pow(2).mean(-1, keepdim=True)
    rms = torch.rsqrt(var + 1e-6)
    return (x.float() * rms * (offset + w.float())).to(x.dtype)

x = torch.randn(4, 64)
print("llama(w=1):",  rmsnorm_ref(x, torch.ones(64), 0.0).abs().mean().item())
print("gemma(w=0):",  rmsnorm_ref(x, torch.zeros(64), 1.0).abs().mean().item())
```

#### 4.2.5 小练习与答案

**练习 1**：`multi_wave_reload` 版内核为什么要加载两遍 `x`（归约一遍、仿射一遍）？能否只加载一遍？

**答案**：归约循环只累加了 `xj*xj`，累加器里没有保留原始 `xj`（存全部 `xj` 会占太多寄存器，尤其 \(N\) 很大时）。所以仿射循环必须重新 gather `x`。`multi_wave_cached` 版正是为了消除这次重载——它一次只处理一个 TILE_SIZE 的列块，把 `xj` 留在寄存器里直接复用，代价是只适合 \(N \le\) TILE_SIZE 的场景。

**练习 2**：static_persistent 版里 weight 为什么在循环外只加载一次？

**答案**：weight 是长度 \(N\) 的向量，对所有行都相同。static_persistent 内核用持久化 grid-stride 调度，一个 block 要处理多行（多个 `(current_bid, 0)` tile），但每行用的 weight 都一样，所以进循环前 `ct.load(W, ...)` 一次、循环内反复用，省掉重复加载。

---

### 4.3 mode 调度选择：三种内核与启发式

#### 4.3.1 概念说明

同一个 RMSNorm 数学公式，rms_norm.py 里给了**三种**前向内核，对应 `mode` 参数的三个取值（外加 `None` 表示自动选择）：

| mode | 内核函数 | 调度方式 | 适用规模 |
| --- | --- | --- | --- |
| `multi_wave_reload` | `_rms_norm_kernel_gather` | grid=行数，一块一行；分块 gather，归约与仿射各扫一遍 | 中小规模、列宽可能很大（`MAX_FUSED_SIZE` 限制 tile） |
| `multi_wave_cached` | `_rms_norm_kernel_multi_wave_cached` | grid=行数，一块一行；整行一次性缓存到寄存器，单 tile | 列宽 \(N\) 适中（能塞进一个 tile），最省访存 |
| `static_persistent` | `_rms_norm_kernel_static_persistent` | 持久化 grid-stride，块数=min(SM, tiles)，一块跨步处理多行 tile；二维 `(TILE_M, TILE_N)` 瓦片 + TMA load | 行数很多（\(M > \text{NUM\_SMS}\times 2\)，要跑超过两波） |

「multi-wave」指 grid 大于 SM 数、需要多「波」才能跑完（一波 = 一次铺满所有 SM）；「static persistent」指块数固定为 SM 数、每个块持久存活、用 grid-stride 循环领养多个 tile（回顾 [u3-l1](u3-l1-cutile-kernel-basics.md) 的静态持久化调度）。

为什么要有多种？因为不同形状下瓶颈不同：

- **行少列宽**：一行一 block 足够铺满 SM，且能缓存整行——用 `multi_wave_cached`。
- **行多列窄**：一行一 block 会让 grid 远超 SM、跑很多波——改用 `static_persistent`，让固定数量（=SM 数）的 block 持久循环处理多行，减少启动/收尾开销、提升 L2 局部性。

#### 4.3.2 核心流程

`_RMSNorm.forward` 的分发逻辑：

```
if mode is None:
    if M > NUM_SMS * 2:        # 行数太多，要跑超过两波
        mode = "static_persistent"
    else:
        mode = "multi_wave_reload"   # 默认
分配 rstd（留给反向）
if mode == "static_persistent":
    选 TILE_SIZE_M/N，grid=min(NUM_SMS, tiles)，启动 _rms_norm_kernel_static_persistent
elif mode == "multi_wave_cached":
    TILE_SIZE=next_power_of_2(N)，grid=(M,)，启动 _rms_norm_kernel_multi_wave_cached
elif mode == "multi_wave_reload":
    TILE_SIZE=min(MAX_FUSED_SIZE, next_power_of_2(N))，grid=(M,)，启动 _rms_norm_kernel_gather
save_for_backward(x, weight, rstd)
```

三个分支都先把输入 reshape 成 2D `(M, N)`、`torch.empty_like` 分配输出、算出 `TILE_SIZE`，然后 `ct.launch` 对应内核，最后统一 `save_for_backward`。

#### 4.3.3 源码精读

**启发式选择**：[src/tilegym/ops/cutile/rms_norm.py:361-366](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L361-L366)。当 `mode is None`，以 `M > NUM_SMS * 2` 为界选 `static_persistent` 或 `multi_wave_reload`。注释明确写了「if we need run over 2 waves, use static persistent mode」。

```python
if mode is None:
    if M > NUM_SMS * 2:
        mode = "static_persistent"
    else:
        mode = "multi_wave_reload"
```

**static_persistent 分支**：[src/tilegym/ops/cutile/rms_norm.py:371-398](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L371-L398)。关键点：`TILE_SIZE_M` 随列宽调整（列窄→行多 `TILE_SIZE_M=16`，列宽 `>=16384`→`TILE_SIZE_M=2` 控制寄存器占用）；`grid_size = min(NUM_SMS, ceil_div(M,TILE_SIZE_M)*ceil_div(N,TILE_SIZE_N))`——块数封顶在 SM 数，这就是「持久化」。对应的内核循环 `for current_bid in range(bid, upper_bound, num_tile_blocks)` 是典型的 grid-stride。

**multi_wave_cached 分支**：[src/tilegym/ops/cutile/rms_norm.py:399-411](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L399-L411)。`TILE_SIZE = next_power_of_2(N)`（整行一个 tile），`grid = (M,)`（一块一行）。对应内核 [src/tilegym/ops/cutile/rms_norm.py:34-54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L34-L54) 把 `xj` 一次 gather 进寄存器，归约后直接复用做仿射，只写一次显存。

**multi_wave_reload 分支**：[src/tilegym/ops/cutile/rms_norm.py:412-434](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L412-L434)。`MAX_FUSED_SIZE = 4096 // element_size()`（fp16 时为 2048），`TILE_SIZE = min(MAX_FUSED_SIZE, next_power_of_2(N))`——列宽超过这个值就强制分块，调用 `_rms_norm_kernel_gather`（见 4.1.3）。

**反向内核**（顺带一提）：[src/tilegym/ops/cutile/rms_norm.py:189-229](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L189-L229) 是持久化 grid-stride 的反向内核，用 `dw_partial` 累加权重梯度（避免 `M×N` 大缓冲），被 `@experimental_kernel` 标记（见 [src/tilegym/experimental.py:25-40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L25-L40)，每次首次启动打印一次性实验告警）。反向只支持 `offset=0`（Gemma3 无反向），见 [src/tilegym/ops/cutile/rms_norm.py:455-459](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L455-L459)。autograd 的完整套路与 [u4-l2](u4-l2-autograd-backward.md) 一致，此处不展开。

#### 4.3.4 代码实践

**实践目标**：通读三个 launch 分支，解释 `static_persistent` 与 `multi_wave_cached` 两种调度的区别与适用规模（这是本讲指定的实践任务）。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/tilegym/ops/cutile/rms_norm.py:371-411](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L371-L411)，对比两个分支的 `grid` 计算与 `TILE_SIZE` 计算。
2. 回答下面三个问题（参考答案见后）：
   - 两者的 `grid` 分别怎么算？哪个依赖 `NUM_SMS`？
   - 两者的 `TILE_SIZE` 分别怎么算？`cached` 版为何是 `next_power_of_2(N)` 而非受 `MAX_FUSED_SIZE` 限制？
   - 假设 \(M=4096, N=4096\)，SM=108，启发式会选哪个？为什么？
3.（可选，需 GPU）跑 `pytest tests/ops/test_rms_norm.py -k "test_op" -m "not slow"`，观察不同 `(m,n,mode)` 组合的行为。

**需要观察的现象 / 参考答案**：

- `static_persistent` 的 grid 是 `min(NUM_SMS, tiles)`，**依赖 NUM_SMS**，块数封顶在 SM 数、用 grid-stride 循环领养多 tile；`multi_wave_cached` 的 grid 是 `(M,)`，**不依赖 NUM_SMS**，一块一行。
- `cached` 版要在一个 tile 里放下整行（寄存器缓存），所以 `TILE_SIZE = next_power_of_2(N)` 必须等于/覆盖整行；若列宽过大寄存器装不下就不适用。`reload` 版受 `MAX_FUSED_SIZE` 限制是为了控制单个累加器瓦片大小、允许列宽超过 tile 时分块。
- \(M=4096 > 108\times2=216\)，启发式选 `static_persistent`。

**预期结果**：能在不看代码的情况下说清三种调度的 grid 来源与适用规模。

> 若无 GPU，跳过第 3 步并标注「待本地验证」，不要伪造测试输出。

#### 4.3.5 小练习与答案

**练习 1**：`static_persistent` 分支里，为什么 `TILE_SIZE_N >= 16384` 时要把 `TILE_SIZE_M` 调小到 2？

**答案**：二维瓦片占用的寄存器/共享内存约正比于 `TILE_SIZE_M * TILE_SIZE_N`。列宽很大时（`TILE_SIZE_N` 大），若 `TILE_SIZE_M` 也大，单 tile 会撑爆寄存器/共享内存，降低 occupancy 甚至编译失败。所以列宽越大、行块越小，用乘积换可用性。对照 [src/tilegym/ops/cutile/rms_norm.py:382-386](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L382-L386)。

**练习 2**：默认启发式 `mode=None` 时，会不会选到 `multi_wave_cached`？

**答案**：不会。启发式只在 `static_persistent` 与 `multi_wave_reload` 之间二选一（[L361-366](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L361-L366)）。`multi_wave_cached` 必须由调用者显式传 `mode="multi_wave_cached"` 才会启用，且只有 cutile 后端实现（测试里非 cutile 后端会 skip，见 [tests/ops/test_rms_norm.py:61-63](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_rms_norm.py#L61-L63)）。

---

### 4.4 get_rms_norm_module 模块工厂：Llama vs Gemma3

#### 4.4.1 概念说明

到此为止讲的都是「算子函数」`rms_norm(input, ...)`。但在真实模型里（见 [u8-l1 monkey-patch](u8-l1-transformer-monkey-patch.md)），归一化是以 `nn.Module`（模块）的形式被嵌入 Transformer 的——模块持有可学参数 `self.weight`，`forward` 里调用算子。

问题来了：不同模型的 RMSNorm 模块**细节不同**——

- **Llama**：`weight` 初始化为 1，`offset=0.0`。
- **Gemma3**：`weight` 初始化为 0，`offset=1.0`，构造参数名是 `dim` 而非 `hidden_size`。

如果给每个模型写一个独立模块类，monkey-patch 时要写一堆 if-else。TileGym 的做法是提供一个**模块工厂（module factory）**：`get_rms_norm_module(model)` 接收模型名，返回对应的模块**类**（注意是类，不是实例）。这样上层只需 `RMSNormCls = get_rms_norm_module("gemma3")`，再用 `RMSNormCls(dim=...)` 实例化即可，模型差异被封装在工厂里。

这个工厂本身也是一个 TileGym 算子——它通过 `@dispatch` 注册、由 `@register_impl` 在 cutile 后端实现，走的是和 `rms_norm` 完全一样的分发机制（回顾 [u2-l2 dispatcher.py](u2-l2-backend-dispatcher.md)）。

#### 4.4.2 核心流程

```
ops.py:  @dispatch("get_rms_norm_module")  →  stub（抛 NotImplementedError）
cutile 后端:  @register_impl("get_rms_norm_module", backend="cutile")
              def get_rms_norm_module(model="llama"):
                  if model == "gemma3": return _RMSNormForGemma3   # offset=1, w 初始化 0
                  else:                 return _TileRMSNorm         # offset=0, w 初始化 1
```

两个模块类都继承自 `nn.Module`，`forward` 最终都调用 4.1-4.3 讲的 `rms_norm` 算子，只是构造时的 `offset` 与 `weight` 初始化不同。

#### 4.4.3 源码精读

**接口 stub**：[src/tilegym/ops/ops.py:162-169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L162-L169)。注意这个 stub **没有 `fallback_backend`**（对照 [u2-l1](u2-l1-unified-op-interface.md)，意味着别的后端没有实现就会直接报错，不降级到 triton）。

```python
@dispatch("get_rms_norm_module")
def get_rms_norm_module(model: str = "llama"):
    """Returns the RMSNorm module class."""
    raise NotImplementedError(f"get_rms_norm_module is not implemented for {get_current_backend()}")
```

**工厂实现**：[src/tilegym/ops/cutile/rms_norm.py:609-614](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L609-L614)。按 `model` 字符串返回类。

```python
@register_impl("get_rms_norm_module", backend="cutile")
def get_rms_norm_module(model: str = "llama"):
    if model == "gemma3":
        return _RMSNormForGemma3
    else:
        return _TileRMSNorm
```

**基础模块 `_TileRMSNorm`**：[src/tilegym/ops/cutile/rms_norm.py:489-521](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L489-L521)。构造时 `self.weight = nn.Parameter(torch.ones(hidden_size))`、`self.offset = offset`；`forward` 调用 `rms_norm(hidden_states, None, self.weight, self.variance_epsilon, mode=mode, offset=self.offset)`——把 4.1~4.3 的算子接进来。类里还附带了 `forward_torch`（PyTorch 参考实现，供对照/测试）和两个静态的 backward 辅助方法。

**Gemma3 子类 `_RMSNormForGemma3`**：[src/tilegym/ops/cutile/rms_norm.py:591-606](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L591-L606)。继承 `_TileRMSNorm`，构造时传 `offset=1.0`、`hidden_size=dim`（适配 Gemma3 的 `dim` 参数名），并 override `self.weight = nn.Parameter(torch.zeros(dim))`。注释说明：Gemma3 用 `dim` 而非 `hidden_size`、weight 初始化为 0、`offset=1.0`。

```python
class _RMSNormForGemma3(_TileRMSNorm):
    def __init__(self, dim, eps=0.000001, offset=1.0, casting_mode="gemma", init_fn="zeros", in_place=False):
        super().__init__(hidden_size=dim, eps=eps, offset=offset)
        self.weight = nn.Parameter(torch.zeros(dim))   # Gemma3: 初始化为 0
```

#### 4.4.4 代码实践

**实践目标**：用模块工厂实例化两种 RMSNorm 模块，验证 `offset` 与初始化的差异。

**操作步骤**（需可用 cutile 后端的 GPU；若无则改为阅读 `_TileRMSNorm.__init__` 与 `_RMSNormForGemma3.__init__` 比较源码）：

1. `import tilegym`，确认 cutile 可用：`tilegym.get_available_backends()`。
2. 取两个类：`LlamaNorm = tilegym.ops.get_rms_norm_module("llama")`、`GemmaNorm = tilegym.ops.get_rms_norm_module("gemma3")`。
3. 分别实例化 `LlamaNorm(64)` 与 `GemmaNorm(64)`，打印 `module.weight[:4]` 与 `module.offset`。

**需要观察的现象**：Llama 模块 weight 全 1、offset 0；Gemma3 模块 weight 全 0、offset 1。

**预期结果**：与 4.4.3 的源码描述一致。若无 GPU，标注「待本地验证」，直接读源码得出同样结论。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `get_rms_norm_module` 返回的是**类**而不是**实例**？

**答案**：因为上层（monkey-patch / 模型构建代码）需要用这个类去 `__init__`（传入 `hidden_size`/`dim`、加载预训练权重），实例化的时机和参数由调用方决定。工厂只负责「选哪个类」，把构造留给调用方，这是工厂模式的常见分工。

**练习 2**：`get_rms_norm_module` 的 stub 没有 `fallback_backend="triton"`，而 `rms_norm` 的 stub 有（[ops.py:134-137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L137)）。这意味着什么？

**答案**：意味着如果当前后端（如 triton）没有注册 `get_rms_norm_module` 的实现，调用会直接抛 `NotImplementedError`，不会降级。而 `rms_norm` 在当前后端无实现时会降级到 triton。差别源于设计：归一化算子本身各后端都有实现、值得兜底；而「返回哪个模块类」是后端特定的装配逻辑，没有统一的兜底语义。

---

## 5. 综合实践

把本讲四块知识（归约、仿射、调度、模块工厂）串起来：

**任务**：给一组形状，亲手预测并验证 `_RMSNorm.forward` 会选哪种 mode、用哪种加载原语，并与 PyTorch 参考对齐。

**步骤**：

1. 选三个形状：(a) `(256, 768)`（行少列窄）；(b) `(31072, 4096)`（行多列宽）；(c) `(256, 18432)`（行少列超宽）。
2. 对每个形状，根据 [rms_norm.py:361-366](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py#L361-L366) 的启发式（`M > NUM_SMS*2`？）**预测**默认 mode。
3. 预测该 mode 下用的是 gather（一维）还是 load（二维 TMA）加载原语，以及归约沿哪条轴。
4.（需 GPU）跑 `tilegym.ops.rms_norm(x, ..., mode=None)` 与 `mode="multi_wave_cached"`，分别与 `tests/ops/test_rms_norm.py` 里的 `reference` 对照（容差 `atol=5e-2`，见 [test_rms_norm.py:83-97](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_rms_norm.py#L83-L97)）。

**需要观察的现象 / 参考结论**：

- (a) `256` 行通常 \(\le\) `NUM_SMS*2`，启发式选 `multi_wave_reload`（gather，轴 0 归约）。
- (b) `31072` 行 \(\gg\) `NUM_SMS*2`，选 `static_persistent`（load 二维瓦片，轴 1 归约）。
- (c) 列宽 18432 超 `MAX_FUSED_SIZE`，`multi_wave_reload` 会分块；但测试里 `n>16384` 时 static_persistent 路径会被 skip 以避免显存爆炸（[test_rms_norm.py:65-69](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_rms_norm.py#L65-L69)）。

> 无 GPU 时，把第 1~3 步当作纯源码阅读题完成，第 4 步标注「待本地验证」。

## 6. 本讲小结

- RMSNorm/LayerNorm 的内核本质是「沿最后一维**归约**算统计量」——RMSNorm 一遍（\(\sum x^2\)），LayerNorm 两遍（均值+方差，或用 \(E[x^2]-E[x]^2\) 一遍）；归约前必须 `astype` 升 fp32，结果用 `ct.rsqrt` 取倒数存为 `rms`/`rstd`。
- 归一化之后是**仿射变换** \(y=\hat{x}\cdot(\text{offset}+w)\)（RMSNorm）或 \(y=\hat{x}\cdot w+b\)（LayerNorm）；仿射与归约融合在同一个内核里，省一次显存往返；`offset` 参数让同一内核同时支持 Llama（offset=0）与 Gemma3（offset=1）。
- **换加载原语必须换归约轴**：gather 取一维瓦片沿轴 0 归约，load 取二维 `(M,N)` 瓦片沿轴 1 归约——这是 [u3-l2](u3-l2-data-movement.md) 规则在归一化内核里的体现。
- 同一公式有三种调度：`multi_wave_reload`（一块一行、分块重载，通用）、`multi_wave_cached`（一块一行、整行缓存，省访存）、`static_persistent`（持久化 grid-stride、二维瓦片，适合行多）；默认启发式以 `M > NUM_SMS*2` 为界二选一。
- `get_rms_norm_module` 是一个走 `@dispatch`/`@register_impl` 分发的**模块工厂**算子，按模型名返回 `_TileRMSNorm`（Llama）或 `_RMSNormForGemma3`（Gemma3）类，把「选哪个类」与「实例化」解耦。
- 反向内核是持久化 grid-stride，用 `dw_partial` 累加权重梯度、被 `@experimental_kernel` 标记，且只支持 `offset=0`（Gemma3 无反向）；autograd 套路与 [u4-l2](u4-l2-autograd-backward.md) 一致。

## 7. 下一步学习建议

- **继续归约类内核**：本讲的「分块归约 + 融合仿射」模板，在 [u5-l1 分块 GEMM](u5-l1-tiled-matmul.md) 里会以更复杂的 `_swizzle_2d` 与 K-tile 累加再次出现，建议接着读 matmul，体会归约/累加器模式如何推广到矩阵乘。
- **深入 autograd**：本讲对反向内核只点到为止，完整的「前向存 rstd、反向重算」模式在 [u4-l2 silu_and_mul 反向](u4-l2-autograd-backward.md) 里有详细推导，可对照阅读。
- **调度进阶**：`static_persistent` 的持久化 grid-stride 是 cuTile 性能内核的通用骨架，[u5-l2 持久化 matmul](u5-l2-persistent-matmul.md) 会讲它与 `num_ctas`/`replace_hints` 的配合，是理解高阶调度的下一站。
- **看测试学用法**：[tests/ops/test_rms_norm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_rms_norm.py) 是 rms_norm 最全的参数化用例，留意它如何对 `mode`、`backend`、`(m,n,dtype)` 做笛卡尔积并按架构 skip——这是 [u9-l1 测试框架](u9-l1-test-benchmark-framework.md) 的活样本。
