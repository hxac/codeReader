# torch.compile 算子融合

## 1. 本讲目标

学完本讲，你应当能够：

- 在源码里**识别** nano-vllm 中所有使用了 `@torch.compile` 的层（RMSNorm、RotaryEmbedding、SiluAndMul、Sampler）。
- 说清楚**算子融合**到底省了什么：它同时削减了「kernel launch 开销」和「中间张量在显存（HBM）上的往返带宽」。
- 逐行读懂这四个层在被融合前的算子链条，并指出融合后的边界。
- 推导并验证 **Sampler 的「指数分布一步采样」等价于 Gumbel-max 技巧**，理解它为什么禁止 `temperature=0`。

本讲是 [u4-l4 Qwen3 模型结构详解](./u4-l4-qwen3-architecture.md) 的直接续篇：那一讲里你见过的若干 `@torch.compile`，本讲统一拆解它们的融合收益。同时也与 [u5-l1 CUDA Graph 捕获与回放](./u5-l1-cuda-graph.md) 紧密相关——融合后生成的小 kernel 正是 CUDA Graph 想要「打包一次提交」的对象。

## 2. 前置知识

### 2.1 一次 PyTorch 算子 = 一次 kernel launch

在 GPU 上，绝大多数 PyTorch 张量算子（如 `pow`、`mul`、`rsqrt`、`softmax`）都对应**一个独立的 CUDA kernel**。调用一个算子，CPU 就要向 GPU 提交一次 kernel launch。每次 launch 都有固定开销（几微秒级），叫 **kernel launch overhead**。

这本身不致命，致命的是两个连锁问题：

1. **小算子太多**：像 RMSNorm 这种归一化，写成 PyTorch 代码是「平方 → 求均值 → 加 eps → 开方倒数 → 乘权重」一长串，每一步都是一个 kernel。decode 阶段每步只算几十个 token，每个 kernel 的实际计算量极小，GPU 大部分时间在等 CPU 一个个 launch。
2. **中间张量要写回显存**：每个 kernel 的输出默认写到 HBM（显存），下一个 kernel 再从 HBM 读回来。一次「平方」写出整张张量，下一次「求均值」又读回来——这些中间结果本来可以留在芯片的寄存器/共享内存里。

### 2.2 算子融合（fusion）解决什么

`torch.compile`（由 TorchDynamo 捕获计算图 + Inductor 后端生成代码）会把**一串逐元素（pointwise）算子合并成一个 kernel**。好处正是对症下药：

- **少 launch**：7 个小算子融合成 1~2 个 kernel，launch 次数骤降。
- **省带宽**：中间值留在寄存器里，不写回 HBM。对于「内存受限（memory-bound）」的逐元素算子，省带宽 ≈ 提速。

> 直觉：逐元素算子的瓶颈是「把数据从显存搬进搬出」，而不是「做加减乘除」。融合让多个算子**共享同一次显存读取**。

有一个重要例外：**规约（reduction，如 `mean`、`sum`、`softmax`）**会形成融合边界。Inductor 通常会把「规约本身 + 它之前的逐元素」融成一个 kernel，把「规约之后的逐元素」融成另一个 kernel。所以 RMSNorm 大致会融合成「计算方差」+「归一化」两个 kernel，而不是一个。

### 2.3 nano-vllm 里的用法

在本项目中，`@torch.compile` 被当成**方法装饰器**直接贴在层的 `forward`（或等价方法）上，编译粒度是「单个层」。被编译的四个层全部集中在 `nanovllm/layers/`：

| 层 | 文件 | 融合的算子 |
|---|---|---|
| `RMSNorm` | `layers/layernorm.py` | 平方、均值、rsqrt、乘权重、（可选）加残差 |
| `RotaryEmbedding` | `layers/rotary_embedding.py` | gather cos/sin、rotate-half 旋转 |
| `SiluAndMul` | `layers/activation.py` | chunk、silu、逐元素乘 |
| `Sampler` | `layers/sampler.py` | 除温度、softmax、除指数、argmax |

### 2.4 与 CUDA Graph 的协作

融合后的 kernel 仍是「若干个小 kernel」。decode 阶段这些 kernel 在 [u5-l1](./u5-l1-cuda-graph.md) 里会被 CUDA Graph 整张图录下来、一次性 `replay()` 提交。所以**融合降 kernel 数、CUDA Graph 降 launch 数**，两者叠加。注意一个细节：`Sampler` 是在 `ModelRunner.run` 里、CUDA Graph **之外**被调用的（见 [4.4 节](#44-sampler基于指数分布的一步采样)），它的 `@torch.compile` 是独立的加速，不进图。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [nanovllm/layers/layernorm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py) | `RMSNorm`，含 `rms_forward`（纯归一化）与 `add_rms_forward`（融合残差）两个被编译方法 |
| [nanovllm/layers/rotary_embedding.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py) | `RotaryEmbedding` 与自由函数 `apply_rotary_emb`，位置编码旋转 |
| [nanovllm/layers/activation.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/activation.py) | `SiluAndMul`，SwiGLU 门控激活 |
| [nanovllm/layers/sampler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py) | `Sampler`，基于指数分布的一步采样 |
| [nanovllm/models/qwen3.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py) | 调用方：展示这四个层在 decoder 层里如何被串起来 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 调用方：展示 `Sampler` 在推理主循环里的位置 |

---

## 4. 核心概念与源码讲解

### 4.1 RMSNorm：归一化与残差的融合

#### 4.1.1 概念说明

RMSNorm（Root Mean Square Normalization）是 LayerNorm 的简化版：它不减均值，只用均方根做缩放。对一个向量 \(x\in\mathbb{R}^{H}\)：

\[
\mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{H}\sum_{i} x_i^2 + \varepsilon}} \odot w
\]

其中 \(\odot\) 是逐元素乘，\(w\) 是可学习权重。\(\varepsilon\) 防止除零。

注意 nano-vllm 的实现里多了一个细节：方差计算**先升精度到 fp32**（`x.float()`），算完再降回原精度。这是因为 bf16/fp16 下做「平方再求和」极易溢出或丢精度。升精度本身是逐元素拷贝，融合后几乎零成本——这正是融合的价值之一。

此外，nano-vllm 把「**加残差**」也融合进了归一化（`add_rms_forward`），这是 [u4-l4](./u4-l4-qwen3-architecture.md) 讲过的「残差被推迟到下一个 norm」结构的落地。

#### 4.1.2 核心流程

`RMSNorm.forward` 根据「是否传入 `residual`」分流：

- 无 `residual`：走 `rms_forward`，纯归一化。
- 有 `residual`：走 `add_rms_forward`，先 `x + residual`，再归一化，同时返回**更新后的残差**。

`add_rms_forward` 里残差的更新值得画清楚：

1. `x = x.float() + residual.float()`（累加残差，fp32）
2. `residual = x.to(原精度)`（**归一化之前**就把加完残差的值存为残差——这是 pre-norm 残差的标准写法）
3. 然后才对方差归一化、乘权重。

所以返回的 `residual` 是「未归一化、但已加完残差」的值，下一个 decoder 层会继续往它上面加。

#### 4.1.3 源码精读

`forward` 的分流逻辑：

[nanovllm/layers/layernorm.py:42-50](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L42-L50) —— `forward` 根据 `residual` 是否为 `None` 分流到两个 `@torch.compile` 方法：

```python
def forward(self, x, residual=None):
    if residual is None:
        return self.rms_forward(x)
    else:
        return self.add_rms_forward(x, residual)
```

纯归一化版本，整段是一个 `@torch.compile` 方法：

[nanovllm/layers/layernorm.py:16-26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L16-L26) —— `rms_forward`，融合了「升精度→平方→均值→rsqrt→乘权重→降精度」整条链：

```python
@torch.compile
def rms_forward(self, x):
    orig_dtype = x.dtype
    x = x.float()
    var = x.pow(2).mean(dim=-1, keepdim=True)
    x.mul_(torch.rsqrt(var + self.eps))
    x = x.to(orig_dtype).mul_(self.weight)
    return x
```

融合前，这一段会展开成约 7 个 kernel（pow、mean、add eps、rsqrt、mul、cast、mul weight），每个都把整张张量写回 HBM 再读回。融合后大致是两个 kernel：一个算 `var`（规约，吸收了 `pow`），一个做 `rsqrt → mul → cast → mul weight`（逐元素）。中间的平方值、rsqrt 值都不落盘。

带残差版本：

[nanovllm/layers/layernorm.py:28-40](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L28-L40) —— `add_rms_forward`，把「加残差」与「归一化」融进同一个编译单元：

```python
@torch.compile
def add_rms_forward(self, x, residual):
    orig_dtype = x.dtype
    x = x.float().add_(residual.float())      # 先加残差
    residual = x.to(orig_dtype)               # 残差在归一化前定值
    var = x.pow(2).mean(dim=-1, keepdim=True)
    x.mul_(torch.rsqrt(var + self.eps))
    x = x.to(orig_dtype).mul_(self.weight)
    return x, residual
```

对照公式，`var` 即 \(\frac{1}{H}\sum x_i^2\)，`rsqrt(var + eps)` 即分母的倒数，最后 `mul_(weight)` 对应 \(\odot w\)。被融合的算子里多出了 `add_`（加残差）与一次 `to`（写回残差）。

> 这两个方法之所以拆开而不是写成一个 `if`，是因为 `@torch.compile` 按「输入签名」分别编译缓存——拆成两个方法让两条路径各自有一份高效的融合 kernel，避免在融合图里塞入条件分支。

#### 4.1.4 代码实践

**实践目标**：量化 `@torch.compile` 对 RMSNorm 的加速，并亲眼看到融合后的 kernel 数变少。

**操作步骤**（示例代码，待本地在 GPU 环境验证）：

```python
# bench_rmsnorm.py （示例代码）
import time, torch
from nanovllm.layers.layernorm import RMSNorm

torch.manual_seed(0)
device, H, N = "cuda", 1024, 4096
norm = RMSNorm(H).to(device)
x = torch.randn(N, H, device=device)
w = norm.weight

# 手写 eager 版（无 @torch.compile）作为对照
def eager_rms(x, w, eps=1e-6):
    orig = x.dtype
    x = x.float()
    var = x.pow(2).mean(-1, keepdim=True)
    x = x.mul(torch.rsqrt(var + eps))
    return x.to(orig).mul(w)

def bench(fn, arg):
    for _ in range(5): _ = fn(arg)          # 预热（同时触发 compile）
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(1000): _ = fn(arg)
    torch.cuda.synchronize()
    return time.perf_counter() - t0

t_eager  = bench(lambda a: eager_rms(a, w), x)
t_compile = bench(lambda a: norm(a), x)
print(f"eager  : {t_eager*1000:.1f} ms")
print(f"compile: {t_compile*1000:.1f} ms")
print(f"speedup: {t_eager/t_compile:.2f}x")
```

**需要观察的现象**：编译版（`norm(a)`）首次调用会有明显的一次性编译耗时（几秒），预热之后单次延迟应低于 eager 版。

**预期结果**：`speedup > 1`，通常在小张量、逐元素密集的场景下能拿到数倍提升（具体数值待本地验证）。想看 Inductor 生成的 Triton 代码，可加环境变量运行：`TORCH_LOGS=output_code python bench_rmsnorm.py`，你会看到融合后的 kernel 数明显少于 7。

#### 4.1.5 小练习与答案

**练习 1**：`rms_forward` 里为什么先 `x.float()` 再算方差，最后又 `to(orig_dtype)`？

**参考答案**：bf16/fp16 下「平方求和」会溢出且精度差，需在 fp32 下做规约；算完再降回原精度以匹配后续层的 dtype。融合后这次升精度只是寄存器内的 cast，不产生额外 HBM 往返，所以「免费」。

**练习 2**：`add_rms_forward` 返回的 `residual` 是什么时候的值？为什么不是归一化后的值？

**参考答案**：是「加完残差、但归一化之前」的 fp32→原精度值。因为残差链要保存的是「原始信号累加」，归一化后的值是给本层 attention/mlp 用的「标准化输入」，两者职责不同——把残差留到下一层再累加，正是 pre-norm Transformer 的稳定性来源。

---

### 4.2 RotaryEmbedding：RoPE 的逐元素融合

#### 4.2.1 概念说明

Rotary Position Embedding（RoPE）通过**旋转**把位置信息编进 query/key：对每一对相邻维度（或前后两半），按位置 \(m\) 旋转一个角度 \(\theta\)。nano-vllm 采用 **rotate-half** 写法：把维度切两半 \(x_1, x_2\)，做

\[
y_1 = x_1\cos\theta - x_2\sin\theta,\qquad y_2 = x_2\cos\theta + x_1\sin\theta
\]

再把 \(y_1, y_2\) 拼回。整个过程全是**逐元素运算**（乘、加减、拼接），是融合的理想对象。

`cos`/`sin` 表在 `__init__` 里一次性预算好，缓存成 `cos_sin_cache`，前向时按 `positions` 做 gather（按位置索引取行）。

#### 4.2.2 核心流程

1. `__init__`：算 `inv_freq`、频率矩阵 `freqs`，再算 `cos`/`sin`，拼接成 `cos_sin_cache`（形状 `[max_position, 2, rotary_dim/2]`，注意中间 `unsqueeze_(1)` 是为多 head 广播）。
2. `forward`（被编译）：`cos_sin_cache[positions]` 取出本批所需行 → 切出 `cos`/`sin` → 对 `query` 和 `key` 各调 `apply_rotary_emb`。

`apply_rotary_emb` 是一个**未被单独编译**的普通函数，但因为被编译过的 `forward` 调用，它的算子会被一起内联进融合图。

#### 4.2.3 源码精读

频率表预计算（不进融合，只在构造时跑一次）：

[nanovllm/layers/rotary_embedding.py:29-35](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L29-L35) —— 用 `einsum` 算外积得到每位置每频率的角度，再算 cos/sin 并缓存：

```python
inv_freq = 1.0 / (base**(torch.arange(0, rotary_dim, 2, dtype=torch.float) / rotary_dim))
t = torch.arange(max_position_embeddings, dtype=torch.float)
freqs = torch.einsum("i,j -> ij", t, inv_freq)
cos, sin = freqs.cos(), freqs.sin()
cache = torch.cat((cos, sin), dim=-1).unsqueeze_(1)
self.register_buffer("cos_sin_cache", cache, persistent=False)
```

被编译的前向：

[nanovllm/layers/rotary_embedding.py:37-48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L37-L48) —— gather cos/sin、对 q/k 做 rotate-half 旋转，整段融合：

```python
@torch.compile
def forward(self, positions, query, key):
    cos_sin = self.cos_sin_cache[positions]
    cos, sin = cos_sin.chunk(2, dim=-1)
    query = apply_rotary_emb(query, cos, sin)
    key = apply_rotary_emb(key, cos, sin)
    return query, key
```

rotate-half 的本体（自由函数，被内联进上面的融合图）：

[nanovllm/layers/rotary_embedding.py:6-14](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L6-L14) —— 切两半、做旋转、拼回，全是逐元素算子：

```python
def apply_rotary_emb(x, cos, sin):
    x1, x2 = torch.chunk(x.float(), 2, dim=-1)
    y1 = x1 * cos - x2 * sin
    y2 = x2 * cos + x1 * sin
    return torch.cat((y1, y2), dim=-1).to(x.dtype)
```

对照公式：`y1 = x1*cos - x2*sin`、`y2 = x2*cos + x1*sin` 正是上文 \(y_1, y_2\)。融合前，仅 `apply_rotary_emb` 一趟就有 chunk、4 次乘、2 次加减、cat、cast 约 8 个 kernel；融合后这些逐元素运算 + gather 被压成很少的几个 kernel，且 `x1`/`x2`/`cos`/`sin` 的中间结果不再落盘。

> 全模型只共享一份 `cos_sin_cache`：`get_rope` 用 `@lru_cache(1)` 缓存了 `RotaryEmbedding` 实例（[rotary_embedding.py:51-59](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L51-L59)），所以每个 decoder 层的 `self.rotary_emb` 拿到的是同一个对象，省了 N 份重复缓存。

#### 4.2.4 代码实践

**实践目标**：把 `RotaryEmbedding.forward` 的融合拆开看，确认融合边界在「gather/规约」与「逐元素旋转」之间。

**操作步骤**（源码阅读型实践）：

1. 打开 [rotary_embedding.py:37-48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L37-L48)。
2. 列出 `forward` 里出现的所有 PyTorch 算子：`__getitem__`（gather）、`chunk`、`chunk`、`*`、`-`、`*`、`+`、`*`、`-`……（在 `apply_rotary_emb` 内）、`cat`、`to`。
3. 判断哪些是「逐元素」、哪些是「需规约/需索引」：gather（按 `positions` 取行）是索引类，`chunk`/`cat` 是视图类（Inductor 常能消解为偏移），其余加减乘都是逐元素。

**需要观察的现象**：理论上 Inductor 会把 gather 单独成步，把其后对 q/k 的逐元素旋转各自融成 1 个 kernel。

**预期结果**：融合后 kernel 数 ≈ gather + 2（q、k 各一个旋转 kernel），远少于逐算子展开。运行 `TORCH_LOGS=output_code python -c "..."` 可验证（具体 kernel 划分待本地确认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_rotary_emb` 没有自己贴 `@torch.compile`，却仍享受了融合？

**参考答案**：它被已编译的 `RotaryEmbedding.forward` 调用，TorchDynamo 会顺着调用链追踪进去，把它的算子内联进同一张融合图。给每个小函数都贴装饰器反而会切断融合机会。

**练习 2**：`cos_sin_cache` 为什么用 `register_buffer(..., persistent=False)`？

**参考答案**：它是「由超参数确定性推出」的派生量，不需要存进 state_dict 占磁盘；`persistent=False` 让它不参与序列化，但仍在 `model.to(device)` 时随模型搬到 GPU。

---

### 4.3 SiluAndMul：门控激活的融合

#### 4.3.1 概念说明

Qwen3 的 MLP 是 **SwiGLU** 结构（见 [u4-l4](./u4-l4-qwen3-architecture.md)）：

\[
\mathrm{SiluAndMul}([x,\,y]) = \mathrm{SiLU}(x)\odot y,\qquad \mathrm{SiLU}(x)=x\odot\sigma(x)
\]

输入是 `gate_up_proj` 输出的「gate 与 up 拼接张量」，沿最后一维切两半：前半做 gate（过 SiLU），后半做 up（直通），相乘得最终激活。这是最经典、收益最直接的融合场景——三步全逐元素。

#### 4.3.2 核心流程

1. `x, y = input.chunk(2, -1)`：把拼接的 `gate_up` 切成 gate 和 up。
2. `return F.silu(x) * y`：gate 过 SiLU 再与 up 逐元素乘。

#### 4.3.3 源码精读

整个类只有这三行有效代码：

[nanovllm/layers/activation.py:6-11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/activation.py#L6-L11) —— `SiluAndMul.forward`，把「chunk + silu + 乘」融成单个 kernel：

```python
class SiluAndMul(nn.Module):
    @torch.compile
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x, y = x.chunk(2, -1)
        return F.silu(x) * y
```

融合前，`F.silu(x)` 会算 `sigmoid(x)` 再乘 `x`，本身约 2~3 个 kernel，再与 `y` 相乘又 1 个，且中间 `sigmoid` 结果要写回 HBM。融合后，对每个输出元素只需读取一次 `x_i`、一次 `y_i`，在寄存器里完成 `sigmoid → 乘 x → 乘 y`，写出一次结果。SiLU 的中间 sigmoid 值完全不落盘——这是融合省带宽的典型例子。

它在模型里的挂载点：

[nanovllm/models/qwen3.py:111](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L111) —— `self.act_fn = SiluAndMul()`，被 MLP 的 `forward` 调用（[qwen3.py:113-117](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L113-L117)）：`gate_up = self.gate_up_proj(x); x = self.act_fn(gate_up); x = self.down_proj(x)`。

#### 4.3.4 代码实践

**实践目标**：直观感受「中间 sigmoid 不落盘」带来的带宽节省。

**操作步骤**（示例代码，待本地验证）：

```python
# bench_silu.py （示例代码）
import time, torch, torch.nn.functional as F
torch.manual_seed(0)
device, D, N = "cuda", 4096, 4096
gate_up = torch.randn(N, D*2, device=device)

# 融合版
class SiluAndMul(torch.nn.Module):
    @torch.compile
    def forward(self, x):
        x, y = x.chunk(2, -1)
        return F.silu(x) * y
fused = SiluAndMul().to(device)

# eager 对照：显式分步，sigmoid 中间值会落盘
def eager(x):
    a, b = x.chunk(2, -1)
    return torch.sigmoid(a) * a * b

def bench(fn):
    for _ in range(5): _ = fn(gate_up)
    torch.cuda.synchronize(); t0 = time.perf_counter()
    for _ in range(1000): _ = fn(gate_up)
    torch.cuda.synchronize(); return (time.perf_counter()-t0)*1000

print("eager :", round(bench(eager), 2), "ms")
print("fused :", round(bench(fused), 2), "ms")
```

**需要观察的现象**：fused 版应快于 eager 版；若用 `torch.cuda.memory_allocated()` 对比峰值，融合版通常不增加中间张量显存。

**预期结果**：`fused < eager`，具体倍数待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SiLU 比「先 ReLU 再乘」更适合 LLM？

**参考答案**：SiLU（\(x\sigma(x)\)）处处光滑、对负值有非零但较小的梯度，训练更稳；ReLU 在零点不可导且对负输入完全截断。现代 LLM（Llama、Qwen）普遍用 SwiGLU。

**练习 2**：如果把 `F.silu(x) * y` 改写成两个语句 `s = F.silu(x); return s * y`，融合效果会变吗？

**参考答案**：不会。TorchDynamo 追踪的是数据流图而非源码语句数，只要 `@torch.compile` 覆盖到，`s` 这个中间值仍会被融合掉、不落盘。变量拆分只是可读性差异。

---

### 4.4 Sampler：基于指数分布的一步采样

#### 4.4.1 概念说明

`Sampler` 是这四个被编译的层里最巧妙的一个。给定每条序列的 logits 和温度 \(T\)，它要在 GPU 上**一步**采出一个 token，且采样过程完全向量化（没有 CPU 上的 `multimomial` 循环）。

核心三行：

```python
logits = logits.float().div_(temperatures.unsqueeze(1))   # 缩放
probs  = torch.softmax(logits, dim=-1)                    # 概率
sample = probs.div_(torch.empty_like(probs).exponential_(1).clamp_min_(1e-10)).argmax(-1)
```

最后一行是关键：`argmax(probs / e)`，其中 \(e_i\sim\mathrm{Exp}(1)\) 独立同分布。这看起来奇怪，但它恰好是 **Gumbel-max 技巧**的等价改写，能精确地从 \(\mathrm{Categorical}(p)\) 里采样。

#### 4.4.2 核心流程

**第一步：缩放**。`logits / T`：温度高→分布变平（更随机），温度低→分布变尖（更确定）。

**第二步：softmax** 得概率 \(p_i\)。

**第三步：Gumbel-max 采样**。下文用一节专门推导。

最终 `argmax` 给出每条序列的 token id，`.tolist()` 拉回 CPU。

#### 4.4.3 源码精读

[nanovllm/layers/sampler.py:5-12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py#L5-L12) —— 整个采样器本体，整段被编译：

```python
class Sampler(nn.Module):
    @torch.compile
    def forward(self, logits, temperatures):
        logits = logits.float().div_(temperatures.unsqueeze(dim=1))
        probs = torch.softmax(logits, dim=-1)
        sample_tokens = probs.div_(
            torch.empty_like(probs).exponential_(1).clamp_min_(1e-10)
        ).argmax(dim=-1)
        return sample_tokens
```

**为什么 `argmax(probs / e)` 是正确的分类采样？——Gumbel-max 推导**

记 \(p_i=\mathrm{softmax}(\mathrm{logits}/T)_i\)。目标是按概率 \(p_i\) 取下标。Gumbel-max 技巧说：

\[
\arg\max_i\bigl(\log p_i + g_i\bigr) \sim \mathrm{Categorical}(p),\qquad g_i\sim\mathrm{Gumbel}(0,1)\ \text{独立}
\]

而一个标准事实是：若 \(e_i\sim\mathrm{Exp}(1)\)，则 \(-\log e_i\sim\mathrm{Gumbel}(0,1)\)。代入：

\[
\log p_i + g_i = \log p_i - \log e_i = \log\!\left(\frac{p_i}{e_i}\right)
\]

因为 \(\log\) 单调递增，

\[
\arg\max_i\log\!\left(\frac{p_i}{e_i}\right) = \arg\max_i\frac{p_i}{e_i}
\]

这正是代码里的 `probs.div_(e).argmax()`。也就是说，**代码用「除指数再取 argmax」一步实现了 Gumbel-max 采样**。好处：

- **完全并行**：每个候选 token 独立生成一个噪声、做一次除法，无需像逆 CDF 那样做前缀和扫描，非常适合 GPU。
- **免 CPU 往返**：传统 `torch.multinomial` 走 CPU 侧算法，这里全程留在 GPU。

两个细节：

- `.clamp_min_(1e-10)`：防止 `exponential_()` 抽到极接近 0 的值导致 `probs/e` 爆炸到 inf。
- `.exponential_(1)` 是**原地**填充：`torch.empty_like(probs)` 不初始化就直接被覆盖，省一次清零。

**为什么禁止 `temperature=0`**（呼应 [u1-l4](./u1-l4-config-and-sampling-params.md)）：`div_(temperatures...)` 会除以 0，产生 inf/NaN。想要近似贪心解码，应使用极小的正温度（如 `1e-5`），此时 logits 放大、softmax 趋近 one-hot，采样几乎总是落在最大 logits 上。

**采样器在主循环里的位置**：

[nanovllm/engine/model_runner.py:190-193](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L190-L193) —— `prepare_sample` 把每条序列的温度打包成一维张量（仅 rank 0 做）：

```python
def prepare_sample(self, seqs):
    temperatures = [seq.temperature for seq in seqs]
    temperatures = torch.tensor(temperatures, dtype=torch.float32, pin_memory=True).cuda(non_blocking=True)
    return temperatures
```

[nanovllm/engine/model_runner.py:214-220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220) —— `Sampler` 在 `run` 里、`run_model`（CUDA Graph）**之外**被调用，只有 rank 0 真正采样：

```python
def run(self, seqs, is_prefill):
    input_ids, positions = self.prepare_prefill(seqs) if is_prefill else self.prepare_decode(seqs)
    temperatures = self.prepare_sample(seqs) if self.rank == 0 else None
    logits = self.run_model(input_ids, positions, is_prefill)
    token_ids = self.sampler(logits, temperatures).tolist() if self.rank == 0 else None
    reset_context()
    return token_ids
```

注意 `self.sampler(...)` 拿到的是全 batch 的 logits（`run_model` 返回 `[num_seqs, vocab]`），一次调用就采出所有序列的 token——采样开销摊到整个 batch 上极低。多卡张量并行时只 rank 0 采样（logits 已在 rank 0 上 gather 齐全，见 [u4-l5](./u4-l5-parallel-linear-tp.md)）。

#### 4.4.4 代码实践

**实践目标**：经验性地验证 `argmax(probs / e)` 确实按 \(p\) 采样。

**操作步骤**（示例代码，待本地验证）：

```python
# verify_sampler.py （示例代码）
import torch
torch.manual_seed(0)

# 构造一个已知分布，例如 3 个 token，概率 [0.1, 0.3, 0.6]
probs = torch.tensor([0.1, 0.3, 0.6])
N = 200_000
batch = probs.unsqueeze(0).expand(N, -1).clone()
e = torch.empty_like(batch).exponential_(1).clamp_min_(1e-10)
samples = batch.div_(e).argmax(-1)

emp = torch.bincount(samples, minlength=3).float() / N
print("经验分布 :", emp.tolist())
print("理论分布 :", probs.tolist())
```

**需要观察的现象**：经验分布应接近 `[0.1, 0.3, 0.6]`，样本量越大越接近。

**预期结果**：三者数值在统计误差内吻合（待本地验证）。这便反证了 Gumbel-max 推导的正确性。

#### 4.4.5 小练习与答案

**练习 1**：若把 `.exponential_(1)` 换成 `.uniform_(0,1)`，采样还正确吗？

**参考答案**：不正确。Gumbel-max 要求噪声是 Gumbel 分布，等价地要求 `−log(e)` 是 Gumbel，即 `e` 必须是 Exp(1)。换成均匀分布后 `argmax(probs/u)` 不再服从 \(\mathrm{Categorical}(p)\)。

**练习 2**：为什么 `exponential_` 用 `torch.empty_like` 而非 `torch.zeros_like`？

**参考答案**：`exponential_(1)` 是原地填充，会覆盖每个元素，无需先清零。`empty_like` 跳过初始化，省一次写显存。

**练习 3**：温度非常小（如 `1e-5`）时，输出近似贪心，这违反「采样」语义吗？

**参考答案**：不违反，只是分布极度尖锐。此时 softmax 几乎 one-hot，argmax 极大概率落在最大 logit 上，行为近似 `argmax(logits)`。真正想关掉随机性，正解是用极小正温度而非 `temperature=0`（后者会触发除零）。

---

## 5. 综合实践

把本讲四个层串起来，做一次「**融合收益体检**」：

1. 用本项目跑一次推理（参考 [u1-l1](./u1-l1-project-overview.md) 的 `example.py`），确认环境正常。
2. 写一个小脚本，依次 benchmark `RMSNorm`、`SiluAndMul` 两个层的 eager 版与 `@torch.compile` 版（参照 [4.1.4](#414-代码实践) 与 [4.3.4](#434-代码实践) 的模板），记录各自的加速比。
3. 用 `TORCH_LOGS=output_code` 环境变量重新跑，定位 Inductor 为 `RMSNorm.rms_forward` 生成的 Triton kernel，数一数融合后还剩几个 kernel，与你预期的「2 个左右」对比。
4. 运行 [4.4.4](#444-代码实践) 的采样验证脚本，确认经验分布匹配理论分布。
5. **进阶**：把 `RMSNorm` 的 `@torch.compile` 装饰器**临时去掉**（仅在本地实验脚本里复制一份改写，**不要改动项目源码**），重跑推理，对比单步 decode 延迟是否有可感知变化。

> 实验中所有数值结果请以本地实测为准；本讲给出的均为方法与预期方向，未声称具体数字。

## 6. 本讲小结

- nano-vllm 用 `@torch.compile` 作为方法装饰器，编译了 **RMSNorm、RotaryEmbedding、SiluAndMul、Sampler** 四个层，集中在 `nanovllm/layers/`。
- 算子融合同时削减两类开销：**kernel launch 次数** 与 **中间张量在 HBM 的往返带宽**；对逐元素算子，后者往往是更大头。
- `RMSNorm` 有两条编译路径：`rms_forward`（纯归一化）与 `add_rms_forward`（融合加残差），归一化在 fp32 规约后降精度，残差在归一化**之前**定值。
- `RotaryEmbedding` 把 gather 与 rotate-half 旋转融合；全模型经 `get_rope` 的 `lru_cache` 共享一份 `cos_sin_cache`。
- `SiluAndMul` 是融合最彻底的典型：`chunk + silu + 乘` 一气呵成，中间 sigmoid 不落盘。
- `Sampler` 用 `argmax(probs / exponential)` 一步实现 **Gumbel-max 采样**，完全并行、全程 GPU；这也解释了为何 `temperature=0` 被禁止（除零）。

## 7. 下一步学习建议

- 本讲的融合 kernel 在 decode 阶段会被 CUDA Graph 整图录下，若还没读，请回到 [u5-l1 CUDA Graph 捕获与回放](./u5-l1-cuda-graph.md) 理解「融合降 kernel 数、Graph 降 launch 数」的叠加。
- 想看 `@torch.compile` 在多卡下的行为，继续读 [u5-l3 张量并行运行时](./u5-l3-tp-runtime-mp-shm.md)：每个 rank 各自编译各自的融合 kernel，`Sampler` 只在 rank 0 运行。
- 若对权重加载好奇（被融合层之前的 `gate_up_proj`/`qkv_proj` 是怎么拼出来的），读 [u5-l4 权重加载与 packed_modules_mapping](./u5-l4-weight-loading.md)。
- 进阶方向：阅读 PyTorch Inductor 文档，了解 `fullgraph=True`、`mode="max-autotune"` 等选项；尝试在本项目里给 `SiluAndMul` 加 `mode="reduce-overhead"` 观察是否进一步降延迟（待本地验证）。
