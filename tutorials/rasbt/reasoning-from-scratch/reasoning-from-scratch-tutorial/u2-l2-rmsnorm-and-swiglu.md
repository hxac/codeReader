# 讲义 u2-l2：RMSNorm 与 SwiGLU 前馈网络

## 1. 本讲目标

上一讲（u2-l1）我们画出了 Qwen3 0.6B 的整体架构地图：嵌入层 → 28 个 TransformerBlock → 最终归一化 → 输出投影。本讲我们放大镜头，钻进每个 TransformerBlock 内部，读完并读懂其中两个「基础零件」：

1. **RMSNorm**：一种比 LayerNorm 更省计算的归一化层，Qwen3 用它做了三件事（注意力前归一化、前馈前归一化、最终归一化），还被复用来做 QK-Norm。
2. **FeedForward（SwiGLU）**：一个由三个线性层组成的门控前馈网络，是每个 TransformerBlock 中参数量最大的部件（约占每层参数的 60%）。
3. **TransformerBlock 前向**：看这两个零件如何被 Pre-Norm 残差骨架拼装起来。

学完本讲，你应该能够：

- 写出 RMSNorm 的公式，并解释它相比 LayerNorm 省掉了什么、为什么可以省。
- 解释 `qwen3_compatible=True` 时「先升 fp32 再归一化」的数值稳定性动机。
- 推导 SwiGLU 中 `fc1`/`fc2`/`fc3` 三个线性层的输入输出形状与门控相乘过程。
- 独立实例化 `RMSNorm` 和 `FeedForward`，用随机张量验证形状守恒，并观察 fp16 输入下的数值行为。

## 2. 前置知识

本讲默认你已学过 u2-l1（知道 `QWEN_CONFIG_06_B` 配置字典和 Pre-Norm 残差骨架），另外补充三个基础概念：

- **归一化（Normalization）**：神经网络每一层的输入数值范围会随训练不断变化，归一化层的作用是把每个位置的向量「拉回」到一个稳定的尺度，让深层网络的训练更平稳。你可以把它想象成给每层输入做一次「重新校准」。
- **LayerNorm（层归一化）**：经典的归一化做法。对每个 token 的 \( d \) 维向量，先减去均值 \( \mu \)，再除以标准差 \( \sigma \)，最后乘一个可学习的缩放 \( \gamma \)、加一个可学习的偏移 \( \beta \)：

  \[ y = \frac{x - \mu}{\sqrt{\sigma^2 + \epsilon}} \odot \gamma + \beta \]

- **激活函数**：神经网络中的非线性部件。没有它，再多线性层叠起来也等价于一个线性层。本讲会遇到 SiLU（也叫 swish），定义为 \( \text{SiLU}(z) = z \cdot \sigma(z) \)，其中 \( \sigma \) 是 sigmoid 函数。

- **形状约定**：沿用上一讲的记号，Transformer 内部张量形状为 `(batch_size, num_tokens, emb_dim)`，简写 `(b, t, d)`。归一化只作用在最后一维 `emb_dim` 上。

- **半精度浮点数**：`bfloat16`/`float16` 只用 16 位表示一个数，其中尾数位分别只有 8 位和 10 位（`float32` 有 23 位）。尾数位越少，能表示的有效数字越少，连续运算的舍入误差越大。这是理解本讲 fp32 上抛动机的关键。

## 3. 本讲源码地图

本讲的全部核心代码都在 `reasoning_from_scratch/qwen3.py` 一个文件里（该文件共 662 行）：

| 位置 | 内容 | 本讲角色 |
|---|---|---|
| [reasoning_from_scratch/qwen3.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L15-L27) | `QWEN_CONFIG_06_B` 配置字典 | 提供 `emb_dim=1024`、`hidden_dim=3072` 等尺寸 |
| [reasoning_from_scratch/qwen3.py:L108-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L108-L136) | `TransformerBlock` | 拼装两个组件的 Pre-Norm 残差骨架 |
| [reasoning_from_scratch/qwen3.py:L139-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L139-L150) | `FeedForward`（SwiGLU） | 最小模块 2 |
| [reasoning_from_scratch/qwen3.py:L323-L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L323-L344) | `RMSNorm` | 最小模块 1 |
| [reasoning_from_scratch/qwen3.py:L177-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L177-L181) | GQA 中的 `q_norm`/`k_norm` 创建 | RMSNorm 的第三处复用（细节留给 u2-l3） |
| [tests/test_qwen3.py:L42-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L42-L61) | `Qwen3RMSNorm`（从 HF transformers 抄来的参考实现） | 验证本仓库 RMSNorm 与官方实现等价 |
| [tests/test_qwen3.py:L137-L156](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L137-L156) | `test_rmsnorm_equivalence` | 等价性测试 |

阅读建议：`qwen3.py` 中类的定义顺序是 `Qwen3Model` → `TransformerBlock` → `FeedForward` → `GroupedQueryAttention` → RoPE 工具 → `RMSNorm`，并不按调用层次排列，用上面的地图按需跳转即可。

## 4. 核心概念与源码讲解

### 4.1 RMSNorm：省掉均值的归一化

#### 4.1.1 概念说明

RMSNorm（Root Mean Square Normalization）是 LayerNorm 的简化版。对每个 token 的 \( d \) 维向量 \( x \)：

\[ y = \frac{x}{\sqrt{\dfrac{1}{d}\sum_{i=1}^{d} x_i^2 + \epsilon}} \odot \gamma \]

对比 LayerNorm 的公式，RMSNorm 做了三处精简：

1. **不减均值 \( \mu \)**：只把向量除以它的均方根（RMS），不再做中心化。研究者发现归一化收益主要来自「把尺度拉回稳定范围」这一步，减均值带来的额外收益很小，却能省掉一次对全维度的求和。
2. **没有偏移 \( \beta \)**：本仓库的 `RMSNorm` 默认 `bias=False`，只保留一个可学习的逐维缩放参数 `scale`（即公式中的 \( \gamma \)，初始化为全 1）。
3. **计算更快**：少了均值和偏移，逐 token 的计算量明显下降，这对有 28 层、每层多处归一化的模型是可观的节省。

**`qwen3_compatible=True` 是干什么的？** 这是本仓库实现的一个细节开关：归一化前先把输入升到 `float32`，算完再转回原 dtype。动机是数值精度——Qwen3 配置里 `dtype=torch.bfloat16`（见 [qwen3.py:L26](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L26)），bfloat16 只有 8 位尾数，对 1024 个元素求平方和均值时舍入误差会被放大；先升 fp32 再算，能让本仓库的输出与 Hugging Face 官方 `Qwen3RMSNorm` 逐位对齐（官方实现同样先 `.to(torch.float32)`），从而权重可以无缝互换。

RMSNorm 在 Qwen3 0.6B 中出现在**四个位置**：

| 位置 | 归一化的维度 | 作用 |
|---|---|---|
| 每个 block 的 `norm1` | `emb_dim=1024` | 注意力子层前归一化（Pre-Norm） |
| 每个 block 的 `norm2` | `emb_dim=1024` | 前馈子层前归一化（Pre-Norm） |
| 模型的 `final_norm` | `emb_dim=1024` | 28 层输出后、投影到词表前的最终归一化 |
| GQA 的 `q_norm`/`k_norm` | `head_dim=128` | 对每个注意力头的 Q/K 单独归一化（QK-Norm，u2-l3 详讲） |

#### 4.1.2 核心流程

`RMSNorm.forward(x)` 的执行过程（`x` 形状 `(b, t, d)`）：

1. 记录输入 dtype 为 `input_dtype`（最后要转回来）。
2. 若 `qwen3_compatible=True`，把 `x` 升到 `float32`。
3. 沿最后一维计算均方：`variance = mean(x², dim=-1)`，形状 `(b, t, 1)`。
4. 用 `rsqrt(variance + eps)` 逐元素除回去（`rsqrt` 是「先开方再取倒数」的一步算子，比分开算更快）。
5. 乘上可学习缩放 `scale`（若有 `shift` 再加上，默认没有）。
6. 把结果转回 `input_dtype` 返回。

伪代码：

```text
def rmsnorm(x):
    dtype = x.dtype
    x = x.float()                      # 仅 qwen3_compatible=True
    inv = rsqrt(mean(x*x, dim=-1) + 1e-6)
    y = x * inv * scale                # scale: 可学习, 形状 (d,)
    return y.to(dtype)
```

注意 eps 的去向：它加在**方差**上而不是开方结果上，即分母是 \( \sqrt{\text{mean}(x^2) + \epsilon} \)。这样即使某个 token 的向量全零，也不会除零。

#### 4.1.3 源码精读

先看构造函数：

> [reasoning_from_scratch/qwen3.py:L323-L329](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L323-L329)
>
> 这段定义了 RMSNorm 的三个属性：`eps`（默认 `1e-6`）、开关 `qwen3_compatible`（默认 `True`）、可学习参数 `scale`（初始化为全 1）。`bias` 默认 `False`，所以 `shift` 是 `None`。注意构造函数**没有** `dtype` 参数——与文件里 `nn.Linear(..., dtype=cfg["dtype"])` 的写法不同，`scale` 在构造时按全局默认类型（通常是 float32）创建。

```python
class RMSNorm(nn.Module):
    def __init__(self, emb_dim, eps=1e-6, bias=False, qwen3_compatible=True):
        super().__init__()
        self.eps = eps
        self.qwen3_compatible = qwen3_compatible
        self.scale = nn.Parameter(torch.ones(emb_dim))
        self.shift = nn.Parameter(torch.zeros(emb_dim)) if bias else None
```

再看前向：

> [reasoning_from_scratch/qwen3.py:L331-L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L331-L344)
>
> `forward` 先记住输入 dtype；`qwen3_compatible=True` 时升 fp32（第 334-335 行）；第 337-338 行按最后一维求均方并用 `rsqrt` 归一化；第 339 行乘 `scale`；最后一行**无条件** `.to(input_dtype)` 转回输入 dtype。

```python
    def forward(self, x):
        input_dtype = x.dtype

        if self.qwen3_compatible:
            x = x.to(torch.float32)

        variance = x.pow(2).mean(dim=-1, keepdim=True)
        norm_x = x * torch.rsqrt(variance + self.eps)
        norm_x = norm_x * self.scale

        if self.shift is not None:
            norm_x = norm_x + self.shift

        return norm_x.to(input_dtype)
```

三个值得注意的实现细节：

- `mean(dim=-1, keepdim=True)` 保留最后一维（形状 `(b, t, 1)`），依赖广播机制与 `(b, t, d)` 的 `x` 相乘。
- `torch.rsqrt` 一个算子完成「开方 + 取倒数」，比 `1 / torch.sqrt(...)` 少一次核启动。
- 即使 `qwen3_compatible=False`，第 339 行 `norm_x * self.scale` 也会因为 `scale` 是 fp32 参数而被类型提升为 fp32 中间结果——但方差计算已经发生在低精度里了，精度损失已经产生。

然后看它的三个使用点：

> [reasoning_from_scratch/qwen3.py:L119-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L119-L121)
>
> `TransformerBlock.__init__` 中创建 `norm1`、`norm2` 两个 RMSNorm（`eps=1e-6`），分别在注意力子层和前馈子层之前使用，即 Pre-Norm 结构。

```python
        self.ff = FeedForward(cfg)
        self.norm1 = RMSNorm(cfg["emb_dim"], eps=1e-6)
        self.norm2 = RMSNorm(cfg["emb_dim"], eps=1e-6)
```

> [reasoning_from_scratch/qwen3.py:L40](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L40)
>
> `Qwen3Model.__init__` 中的 `final_norm`：28 层 TransformerBlock 全部走完后、送入 `out_head` 投影到词表之前的最后一次归一化，同样是 RMSNorm（这里用默认 `eps=1e-6`）。

> [reasoning_from_scratch/qwen3.py:L177-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L177-L181)
>
> 当配置 `qk_norm=True`（0.6B 配置里就是 True）时，GQA 给每个注意力头创建 `q_norm`/`k_norm`，归一化维度是 `head_dim=128` 而不是 `emb_dim`——对每个头单独归一化。它们在 forward 中的调用见 [qwen3.py:L196-L200](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L196-L200)，细节留到 u2-l3。

最后看官方等价性是怎么被测试锁定的：

> [tests/test_qwen3.py:L42-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L42-L61)
>
> 测试文件里内嵌了一个从 Hugging Face `transformers` 的 `modeling_qwen3.py` 抄来的参考实现 `Qwen3RMSNorm`：它同样是「升 fp32 → 均方归一化 → 乘 weight → 转回输入 dtype」。

> [tests/test_qwen3.py:L137-L156](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L137-L156)
>
> `test_rmsnorm_equivalence` 用同一随机输入分别过两个实现，`torch.testing.assert_close(out1, out2, atol=1e-5, rtol=1e-5)` 断言结果一致。这正是 `qwen3_compatible=True` 分支存在的意义：让本仓库的数值与官方实现对齐。

#### 4.1.4 代码实践

**实践 A：形状守恒与手工复算**

1. **实践目标**：验证 `RMSNorm(8)` 对任意 `(b, t, 8)` 输入形状不变，并手工复算一个具体数值，确认你理解的公式和代码一致。
2. **操作步骤**（以下为示例代码，可直接存成 `rmsnorm_check.py` 用 `python rmsnorm_check.py` 运行）：

   ```python
   import torch
   from reasoning_from_scratch.qwen3 import RMSNorm

   torch.manual_seed(123)
   norm = RMSNorm(8)                      # eps=1e-6, bias=False, qwen3_compatible=True
   x = torch.randn(2, 3, 8)               # (batch, tokens, emb_dim)
   y = norm(x)
   print(x.shape, y.shape)                # 预期都是 torch.Size([2, 3, 8])
   print(norm.scale.shape, norm.shift)    # 预期 torch.Size([8]) 和 None

   # 手工复算第一个 token
   x0 = x[0, 0]                           # 形状 (8,)
   ref = x0 * torch.rsqrt(x0.pow(2).mean() + 1e-6) * norm.scale
   print((y[0, 0] - ref).abs().max())     # 预期 0.0（同一公式、同一精度路径）
   ```

3. **需要观察的现象**：`y.shape` 与 `x.shape` 相同；`shift` 为 `None`；手工复算与模块输出的最大差值为 0。
4. **预期结果**：形状守恒、逐 token 独立归一化、无偏移参数，三点全部成立。（具体打印数值待本地验证）

**实践 B：fp16 输入下 `qwen3_compatible` 的 True/False 对比**

1. **实践目标**：亲手观察这个开关到底改变了什么——先读代码做出预测，再用实验检验。
2. **操作步骤**（示例代码）：

   ```python
   x16 = torch.randn(4, 16, dtype=torch.float16)

   n_on  = RMSNorm(16, qwen3_compatible=True)
   n_off = RMSNorm(16, qwen3_compatible=False)
   n_off.scale.data = n_on.scale.data.clone()   # 对齐参数，只比计算路径

   y_on, y_off = n_on(x16), n_off(x16)
   print(y_on.dtype, y_off.dtype)               # 两者都是 torch.float16？
   print((y_on.float() - y_off.float()).abs().max())
   print(torch.allclose(y_on.float(), y_off.float(), atol=1e-3))
   ```

3. **需要观察的现象**：
   - 两个输出的 dtype **都是** `torch.float16`。读 [qwen3.py:L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L344) 就能预测到：最后一行 `return norm_x.to(input_dtype)` 无条件把结果转回输入 dtype，所以**这个开关并不改变输出 dtype**。
   - 两个输出的**数值**存在细微差别：`qwen3_compatible=False` 时，第 337 行的方差计算发生在 fp16（10 位尾数）里；`True` 时升到 fp32 算完再转回。差值通常出现在小数点后第 3~4 位量级。
4. **预期结果 / 解释**：这个标志的真正意义是**中间计算的数值保真**，让归一化结果与 Hugging Face 官方实现对齐（对照实践 C）。差值的具体大小随随机种子不同而不同，待本地验证。

**实践 C：跑官方等价性测试**

1. **实践目标**：确认本仓库 RMSNorm 与 HF transformers 的参考实现等价。
2. **操作步骤**：在仓库根目录安装 `transformers` 后运行：

   ```bash
   pytest tests/test_qwen3.py::test_rmsnorm_equivalence -v
   ```

3. **需要观察的现象**：测试通过（`PASSED`）。
4. **预期结果**：通过；若未安装 `transformers`，该测试文件中的部分用例会被跳过，属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：手工计算 RMSNorm（`scale` 全 1、`eps` 忽略）对 `x = [1, 2, 3, 4]` 的输出。

**答案**：均方 \( = \frac{1+4+9+16}{4} = 7.5 \)，\( \text{rsqrt}(7.5) = 1/\sqrt{7.5} \approx 0.3651 \)，输出 \( \approx [0.365,\ 0.730,\ 1.095,\ 1.460] \)。注意乘 `scale` 前输出向量的均方根恰好被归一到 1。

**练习 2**：为什么 `eps=1e-6` 加在方差上（`variance + eps`）而不是加在开方结果上？如果去掉 `eps`，什么输入会让程序出问题？

**答案**：两种加法都能防除零，但加在方差上与 HF 官方实现一致（等价性测试要求），且数值上更稳定。去掉 `eps` 后，当某个 token 的向量全零（例如被置零的 padding 位置）时 `rsqrt(0) = +inf`，输出变成 NaN 并向下游传播。

**练习 3**：`RMSNorm(1024)` 有多少个可学习参数？对比带 bias 的 LayerNorm 少多少？

**答案**：`RMSNorm(1024)` 只有 `scale` 一个参数张量，1024 个。带 bias 的 LayerNorm 是 `gamma + beta` 共 2048 个，且每次前向还多算一次均值。对 0.6B 模型，每个 TransformerBlock 有 `norm1`/`norm2` 各 1024、模型级 `final_norm` 1024、GQA 的 `q_norm`/`k_norm` 各 128——归一化参数总量不到 6 万，相比全模型约 5.96 亿可以忽略，省下的主要是计算而非参数。

### 4.2 FeedForward：SwiGLU 门控前馈网络

#### 4.2.1 概念说明

注意力子层负责「在 token 之间搬运信息」，前馈网络（FFN）则负责「在每个 token 内部加工信息」——它对每个位置独立地做一次升维、非线性、降维。

经典 GPT 风格的 FFN 用两个矩阵和一个激活：

\[ \text{FFN}_{\text{GPT}}(x) = W_2 \cdot \text{GELU}(W_1 x) \]

Qwen3 换成了 **SwiGLU**（带 GLU 门控的 Swish 激活），用**三个**矩阵：

\[ \text{FFN}_{\text{SwiGLU}}(x) = W_3 \big( \text{SiLU}(W_1 x) \odot W_2 x \big) \]

其中 \( \odot \) 是逐元素相乘，\( \text{SiLU}(z) = z \cdot \sigma(z) \)。对照仓库代码的命名：`fc1` 对应 \( W_1 \)（门控支路，过 SiLU），`fc2` 对应 \( W_2 \)（内容支路，保持线性），`fc3` 对应 \( W_3 \)（降维投影）。

**为什么要有门控？** 直觉上：`fc2(x)` 产生「候选内容」，`silu(fc1(x))` 产生一个逐维度的「阀门」——SiLU 的输出以约 -0.278 为下界、无上界，大部分输入落在 0 附近或为正。两者相乘，等于让网络**逐维度地决定放行多少信息**，比「先激活再统一投影」的表达力更强。实践上，引入 GLU 门控的 FFN 在同等参数量下普遍取得更好的效果，代价是需要第三个矩阵。

**尺寸怎么定？** 0.6B 配置里 `emb_dim=1024`、`hidden_dim=3072`（[qwen3.py:L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L18) 与 [qwen3.py:L21](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L21)），即中间层取 3 倍嵌入维。经典两矩阵 FFN 惯例取 4 倍；换成三矩阵的 SwiGLU 后，常见做法是把中间层适当缩小以抵消第三个矩阵的参数增长（LLaMA 系取约 8/3 倍），Qwen3 0.6B 取的是 3 倍。

#### 4.2.2 核心流程

`FeedForward.forward(x)` 的形状流（`d = emb_dim`，`h = hidden_dim`）：

```text
x: (b, t, d)
 ├─ fc1 → x_fc1: (b, t, h)     升维，门控支路
 ├─ fc2 → x_fc2: (b, t, h)     升维，内容支路
 ├─ silu(x_fc1) * x_fc2 → (b, t, h)   逐元素门控相乘
 └─ fc3 → (b, t, d)            降维回 emb_dim
```

三个线性层都 `bias=False`，与文件中其他投影层一致——现代 LLM 惯例是去掉小投影层的 bias 以省参数和计算。

参数量（0.6B 真实配置）：每个矩阵 \( 1024 \times 3072 = 3{,}145{,}728 \) 个参数，三个共 \( 9{,}437{,}184 \approx 9.44 \text{M} \)/层；28 层共约 264M。加上注意力子层的约 6.29M/层，FeedForward 约占每个 TransformerBlock 参数的 **60%**——它是 Transformer 里最「重」的部件。

#### 4.2.3 源码精读

> [reasoning_from_scratch/qwen3.py:L139-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L139-L150)
>
> `FeedForward` 全部代码只有 12 行：构造函数从 `cfg` 读取 `emb_dim`/`hidden_dim`/`dtype` 创建三个无 bias 线性层；前向就是「silu(fc1(x)) 乘 fc2(x) 再过 fc3」。

```python
class FeedForward(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.fc1 = nn.Linear(cfg["emb_dim"], cfg["hidden_dim"], dtype=cfg["dtype"], bias=False)
        self.fc2 = nn.Linear(cfg["emb_dim"], cfg["hidden_dim"], dtype=cfg["dtype"], bias=False)
        self.fc3 = nn.Linear(cfg["hidden_dim"], cfg["emb_dim"], dtype=cfg["dtype"], bias=False)

    def forward(self, x):
        x_fc1 = self.fc1(x)
        x_fc2 = self.fc2(x)
        x = nn.functional.silu(x_fc1) * x_fc2
        return self.fc3(x)
```

逐行拆解：

- 第 142-144 行：`fc1` 和 `fc2` 都是 `d → h` 的升维投影，输入相同、各自有独立权重；`fc3` 是唯一的 `h → d` 降维投影。三者的 `dtype` 都取自配置（0.6B 为 bfloat16），与 RMSNorm 刻意保持 fp32 参数形成对比——大矩阵占显存大头，用低精度存储收益显著；归一化的少量参数和计算则保精度。
- 第 147-148 行：两条支路分别前向，形状都变成 `(b, t, h)`。
- 第 149 行：`nn.functional.silu(x_fc1) * x_fc2`——SiLU 只作用在门控支路上，然后与内容支路逐元素相乘。注意与经典 FFN 的关键差异：**激活在乘法之前、且只乘其中一支**。
- 第 150 行：`fc3` 把门控结果降回 `(b, t, d)`，保证整个 FeedForward 对外形状守恒，可以无缝嵌进残差骨架。

顺带一提，这三个层与 Hugging Face 权重名的对应关系是 `fc1 ↔ gate_proj`、`fc2 ↔ up_proj`、`fc3 ↔ down_proj`，加载预训练权重时按此映射（u10-l3 详讲）。

#### 4.2.4 代码实践

1. **实践目标**：用缩小配置实例化 `FeedForward`，验证形状流和参数量推导；再用真实 0.6B 尺寸核对参数量。
2. **操作步骤**（示例代码）：

   ```python
   import torch
   from reasoning_from_scratch.qwen3 import FeedForward

   torch.manual_seed(123)
   small_cfg = {"emb_dim": 8, "hidden_dim": 16, "dtype": torch.float32}
   ffn = FeedForward(small_cfg)
   for name, p in ffn.named_parameters():
       print(name, tuple(p.shape))
   # 预期：
   # fc1.weight (16, 8)   d→h
   # fc2.weight (16, 8)   d→h
   # fc3.weight (8, 16)   h→d

   x = torch.randn(2, 3, 8)
   y = ffn(x)
   print(x.shape, y.shape)        # 预期 (2,3,8) → (2,3,8)

   # 手工复算，确认对公式的理解
   ref = ffn.fc3(torch.nn.functional.silu(ffn.fc1(x)) * ffn.fc2(x))
   print((y - ref).abs().max())   # 预期 0.0

   # 真实 0.6B 尺寸的参数量
   big_cfg = {"emb_dim": 1024, "hidden_dim": 3072, "dtype": torch.float32}
   big_ffn = FeedForward(big_cfg)
   print(sum(p.numel() for p in big_ffn.parameters()))   # 预期 9437184
   ```

3. **需要观察的现象**：三个权重形状分别为 `(16,8)`、`(16,8)`、`(8,16)`（`nn.Linear` 权重形状是 `(out, in)`）；输入输出形状相同；手工复算差值为 0；真实尺寸参数量等于 \( 3 \times 1024 \times 3072 \)。
4. **预期结果**：全部成立。打印数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `hidden_dim` 从 3072 改成 4096，每层 FeedForward 参数量变成多少？变化幅度多大？

**答案**：\( 3 \times 1024 \times 4096 = 12{,}582{,}912 \approx 12.58\text{M} \)，比原来的 9.44M 增加约 33.3%（随 `hidden_dim` 线性增长）。

**练习 2**：如果把 SiLU 同时也作用在 `fc2` 支路上（即 `silu(fc1(x)) * silu(fc2(x))`），会破坏什么性质？

**答案**：形状和可运行性都没问题，但门控语义被破坏：门控结构的价值在于一支保持线性「内容」、一支变成有界的「阀门」；两支都过激活后，两路都被非线性压缩，等价于一个更普通的双激活组合，丧失了 GLU 家族「乘性调制」的设计初衷。经验上效果通常变差。

**练习 3**：`SiLU(0)` 等于多少？SiLU 的最小值大约是多少、在哪里取得？

**答案**：\( \text{SiLU}(0) = 0 \times \sigma(0) = 0 \)。当 \( z \to -\infty \) 时 \( \sigma(z) \to 0 \) 且 \( z \to -\infty \)，乘积先降后升，最小值约 \( -0.278 \)，在 \( z \approx -1.28 \) 处取得。所以门控支路的输出有下界（约 -0.278）而无上界，大部分区域非负——这是它适合当「阀门」的原因之一。

### 4.3 TransformerBlock 前向：两个组件的拼装

#### 4.3.1 概念说明

有了 RMSNorm 和 FeedForward，再加上下一讲才细讲的 `GroupedQueryAttention`，就可以拼出一个完整的 `TransformerBlock`。回顾 u2-l1 介绍的 **Pre-Norm 残差骨架**：

```text
x ─┬─ norm1 → attention ─┐
  │                      +(逐元素相加)
  └──────────────────────┘
    ┬─ norm2 → FeedForward ┐
    │                      +
    └──────────────────────┘
```

「Pre-Norm」指归一化放在子层**之前**（先 norm 再进 attention/ff），残差分支上不做任何变换。这样设计的核心好处：恒等映射（shortcut）让梯度可以不经过任何变换直接回传，28 层堆叠时训练更稳定。归一化本身不改变形状，所以两个子层都必须**形状守恒**（`(b, t, d)` 进、`(b, t, d)` 出），残差相加才能成立——这正是 4.1、4.2 两个组件共同遵守的契约。

#### 4.3.2 核心流程

`TransformerBlock.forward(x, mask, cos, sin, start_pos, cache)` 的完整流程：

1. 保存 `shortcut = x`（注意力子层的残差）。
2. `x = norm1(x)`：RMSNorm 归一化。
3. `x = att(x, mask, cos, sin, ...)`：过注意力，同时拿到本层新的 KV 缓存 `next_cache`。
4. `x = x + shortcut`：残差相加。
5. 再来一遍：`shortcut = x` → `norm2` → `ff` → 相加。
6. 返回 `(x, next_cache)`——注意除了隐藏状态还要返回缓存，供上层逐层更新（见 [qwen3.py:L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98) 的循环：`x, new_blk_cache = block(...)` 后 `cache.update(i, new_blk_cache)`）。

#### 4.3.3 源码精读

> [reasoning_from_scratch/qwen3.py:L108-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L108-L121)
>
> 构造函数创建四个子模块：注意力 `att`（读 `emb_dim`/`n_heads`/`head_dim`/`n_kv_groups`/`qk_norm`/`dtype` 六个配置）、前馈 `ff`、以及两个 RMSNorm。一个 block 需要的配置项到这里全部到齐。

```python
class TransformerBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.att = GroupedQueryAttention(
            d_in=cfg["emb_dim"],
            num_heads=cfg["n_heads"],
            head_dim=cfg["head_dim"],
            num_kv_groups=cfg["n_kv_groups"],
            qk_norm=cfg["qk_norm"],
            dtype=cfg["dtype"]
        )
        self.ff = FeedForward(cfg)
        self.norm1 = RMSNorm(cfg["emb_dim"], eps=1e-6)
        self.norm2 = RMSNorm(cfg["emb_dim"], eps=1e-6)
```

> [reasoning_from_scratch/qwen3.py:L123-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L123-L136)
>
> 前向就是 4.3.2 的六步：两段「norm → 子层 → 加残差」，最后把本层 KV 缓存随隐藏状态一起返回。注释中的 `Shape [batch_size, num_tokens, emb_size]` 提醒注意力输出已回到 `(b, t, d)`。

```python
    def forward(self, x, mask, cos, sin, start_pos=0, cache=None):
        # Shortcut connection for attention block
        shortcut = x
        x = self.norm1(x)
        x, next_cache = self.att(x, mask, cos, sin, start_pos=start_pos, cache=cache)
        x = x + shortcut  # Add the original input back

        # Shortcut connection for feed-forward block
        shortcut = x
        x = self.norm2(x)
        x = self.ff(x)
        x = x + shortcut  # Add the original input back

        return x, next_cache
```

值得体会的两点：

- **归一化的作用对象是子层，不是残差**。`x + shortcut` 中进残差的是未归一化的原始 `x`，`norm1` 只服务注意力子层。如果把 norm 放在相加之后（Post-Norm），深层梯度就必须穿过归一化算子，训练稳定性变差。
- **两段结构完全对称**，只是把 attention 换成 ff。这种「子层可替换、骨架不变」的模式让架构实验（换注意力变体、换 FFN 变体）非常便宜——u10-l1 的 `qwen3_optimized.py` 正是保持接口不变、只替换内部实现来做性能优化。

#### 4.3.4 代码实践

1. **实践目标**：用缩小配置单独实例化一个 `TransformerBlock`，手动准备好它需要的四个输入（`mask`、`cos`、`sin`），跑通一次前向并验证形状守恒。
2. **操作步骤**（示例代码；`cos`/`sin` 的构造方式抄自 [qwen3.py:L48-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L48-L54)，`mask` 抄自 [qwen3.py:L78-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L78-L90)）：

   ```python
   import torch
   from reasoning_from_scratch.qwen3 import TransformerBlock, compute_rope_params

   torch.manual_seed(123)
   cfg = {
       "emb_dim": 32, "n_heads": 4, "head_dim": 16,
       "n_kv_groups": 2, "qk_norm": True, "hidden_dim": 64,
       "dtype": torch.float32,
   }
   block = TransformerBlock(cfg)

   t = 5                                    # 序列长度
   x = torch.randn(1, t, 32)
   mask = torch.triu(torch.ones(t, t, dtype=torch.bool), diagonal=1)[None, None]
   cos, sin = compute_rope_params(head_dim=16, theta_base=1_000_000.0, context_length=64)

   out, next_cache = block(x, mask, cos, sin)      # start_pos=0, cache=None
   print(x.shape, out.shape)                        # 预期都是 (1, 5, 32)
   print(type(block.norm1).__name__, block.norm1.eps)   # RMSNorm, 1e-06
   print(next_cache[0].shape)                       # 本层 K 缓存 (1, n_kv_groups=2, t, 16)
   ```

3. **需要观察的现象**：输出与输入形状相同 `(1, 5, 32)`；`norm1` 是 `RMSNorm` 且 `eps=1e-6`；返回的缓存里 K 的形状是 `(1, 2, 5, 16)`（batch、KV 组数、token 数、头维）。
4. **预期结果**：形状守恒验证通过——这是残差相加成立的前提。缓存形状的详细含义留给 u2-l3/u2-l5，这里只确认「block 返回两个值」。具体打印待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`TransformerBlock.forward` 为什么返回 `(x, next_cache)` 两个值，而不是只返回 `x`？

**答案**：因为 KV 缓存逐层独立，每层的注意力都产生自己的新 K/V；上层 `Qwen3Model.forward` 在循环里对每层调用 `cache.update(i, new_blk_cache)`（[qwen3.py:L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98)）。这也解释了 u2-l1 提到的：块序列必须用 `ModuleList` + 手写循环，`nn.Sequential` 无法传递多输入多输出。

**练习 2**：如果把 `norm2` 删掉、让 `ff` 直接吃注意力子层的输出，会有什么风险？

**答案**：注意力输出的数值尺度没有任何约束就进入 `fc1`/`fc2` 的升维投影，逐层累加后激活值可能不断增长（尺度失控），轻则训练困难，重则溢出成 inf/NaN。RMSNorm 把每个位置的向量拉回单位均方根尺度，是深层残差网络能堆到 28 层的前提。

**练习 3**：残差相加 `x = x + shortcut` 会不会改变输出的均方根尺度？

**答案**：会。两个向量相加后均方根一般变大；若两向量独立且同尺度，均方根约为原来的 \( \sqrt{2} \) 倍（能量翻倍）。这就是为什么残差分支上的尺度控制很重要——也是「下一个子层之前必须再归一化」的原因之一。

## 5. 综合实践

把本讲三个最小模块串成一个自查脚本 `u2_l2_check.py`（示例代码），一次验证所有关键结论：

```python
import torch
from reasoning_from_scratch.qwen3 import RMSNorm, FeedForward, QWEN_CONFIG_06_B

torch.manual_seed(123)

# --- 1. 组件级：形状守恒 + 手工复算 ---
d, h = 8, 16
norm, ffn = RMSNorm(d), FeedForward({"emb_dim": d, "hidden_dim": h, "dtype": torch.float32})
x = torch.randn(2, 3, d)
assert norm(x).shape == x.shape
assert ffn(x).shape == x.shape
assert torch.allclose(ffn(x), ffn.fc3(torch.nn.functional.silu(ffn.fc1(x)) * ffn.fc2(x)))

# --- 2. 数值级：fp16 下两种归一化路径的精度差异 ---
x16 = torch.randn(64, d, dtype=torch.float16)
n_on, n_off = RMSNorm(d, qwen3_compatible=True), RMSNorm(d, qwen3_compatible=False)
n_off.scale.data = n_on.scale.data.clone()
y_on, y_off = n_on(x16), n_off(x16)
assert y_on.dtype == y_off.dtype == torch.float16      # 开关不改变输出 dtype
print("max diff:", (y_on.float() - y_off.float()).abs().max().item())

# --- 3. 规模级：真实 0.6B 尺寸的 FeedForward 参数量 ---
ffn_big = FeedForward({"emb_dim": 1024, "hidden_dim": 3072, "dtype": torch.float32})
ff_params = sum(p.numel() for p in ffn_big.parameters())
print(f"FeedForward 参数量: {ff_params:,} (预期 9,437,184)")
print(f"配置字典确认: emb_dim={QWEN_CONFIG_06_B['emb_dim']}, "
      f"hidden_dim={QWEN_CONFIG_06_B['hidden_dim']}, dtype={QWEN_CONFIG_06_B['dtype']}")
```

任务清单：

1. 运行脚本，记录三个断言是否全部通过、`max diff` 的量级（预期非零但很小，具体数值待本地验证）。
2. 把第 2 部分改成对比「`qwen3_compatible=True` 的 fp16 输出」与「同一输入先升 fp32 归一化后不再转回 fp16 的结果」，观察舍入发生在哪一步，用一句话解释原因。
3. （可选，需安装 transformers）运行 `pytest tests/test_qwen3.py::test_rmsnorm_equivalence -v`，确认与官方实现对齐。
4. 在笔记里用 3~5 句话回答：如果面试官问「RMSNorm 相比 LayerNorm 省了什么、SwiGLU 相比普通 FFN 多了什么」，你怎么答？

## 6. 本讲小结

- **RMSNorm**（[qwen3.py:L323-L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L323-L344)）= 均方根归一化 + 可学习缩放 `scale`：省掉 LayerNorm 的均值中心化和偏移参数，计算更快；`eps=1e-6` 加在方差上防除零。
- **`qwen3_compatible=True`** 先升 fp32 再归一化、算完转回输入 dtype：不改变输出 dtype，只保证中间计算精度，使数值与 Hugging Face 官方 `Qwen3RMSNorm` 对齐（由 `test_rmsnorm_equivalence` 锁定）。
- RMSNorm 在模型中出现四处：每层 `norm1`/`norm2`、模型级 `final_norm`（emb_dim=1024），以及 GQA 的 `q_norm`/`k_norm`（head_dim=128）。
- **FeedForward 是 SwiGLU**：`fc3(silu(fc1(x)) * fc2(x))`，三个无 bias 线性层（1024→3072、1024→3072、3072→1024），`silu(fc1)` 当门、`fc2` 当内容、`fc3` 降维；每层约 9.44M 参数，占单个 TransformerBlock 的约 60%。
- 两个组件都遵守**形状守恒**契约 `(b, t, d) → (b, t, d)`，这是 Pre-Norm 残差骨架（[qwen3.py:L123-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L123-L136)）中 `x + shortcut` 能成立的前提。
- `TransformerBlock` 返回 `(x, next_cache)`：KV 缓存逐层产生、逐层更新，这决定了上层必须用 `ModuleList` 手写循环。

## 7. 下一步学习建议

本讲填好了 TransformerBlock 的一半，剩下那块最大的拼图是 `GroupedQueryAttention`。下一讲 **u2-l3（分组查询注意力 GQA 与 QK-Norm）** 将逐行阅读 [qwen3.py:L153-L225](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L153-L225)：Q/K/V 投影、四维头形状变换、本讲提到的 `q_norm`/`k_norm` 如何逐头归一化、KV 组如何 `repeat_interleave` 展开，以及缩放点积注意力与因果掩码。你只需要带着本讲的一个结论过去：`norm1` 之后交给注意力的张量形状是 `(b, t, 1024)`、每个位置均方根约为 1。

如果想在下一讲之前热身，可以先把 `GroupedQueryAttention.__init__`（[qwen3.py:L153-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L153-L181)）里 `W_query`/`W_key`/`W_value` 三个线性层的输出维度算出来，对照配置 `n_heads=16`、`n_kv_groups=8`、`head_dim=128` 检验你的答案。
