# LayerNorm：均值、方差与 rstd

## 1. 本讲目标

本讲是「前向各层」单元的第二篇，聚焦 GPT-2 里出现频率最高的一层——**Layer Normalization（层归一化，LayerNorm）**。学完本讲，你应该能够：

- 说清楚 LayerNorm 的数学定义：均值 \(\mu\)、方差 \(\sigma^2\)、以及为什么需要小常数 \(\epsilon\) 和「倒数标准差」rstd。
- 理解 GPT-2 采用的 **pre-norm（预归一化）** 位置，以及它对训练稳定性的意义。
- 把 PyTorch 的「手动实现」逐行翻译成 C，并理解「一维内存 + 行主序指针」的寻址方式。
- 推导出 LayerNorm 的反向梯度公式，看懂为什么 C 代码里反向只需要做**两次归约（reduce）**就能完成。
- 在 `train_gpt2.c` 里定位三处前向、三处反向调用，理解 mean/rstd 缓冲区是如何被前向写出、反向读取的。

本讲只讲 LayerNorm 这一个算子的前向与反向，不涉及它如何嵌入到整个 Transformer 块（那属于 [u2-l7 前向组装](u2-l7-forward-assembly.md)）。

## 2. 前置知识

在开始前，请确认你已了解以下概念（它们在前置讲义中已经建立）：

- **token、B/T/C/V/L 尺寸缩写**：B 是 batch，T 是序列长度（时间步），C 是通道数（特征维度），V 是词表大小，L 是 Transformer 层数。GPT-2 124M 的典型设置是 B=8、T=1024、C=768、L=12。见 [u1-l1](u1-l1-project-overview.md)。
- **张量就是「一维数组 + 形状视图」**：三维张量 `(B,T,C)` 在内存里是一段连续的 `B*T*C` 个 float，访问 `x[b,t,c]` 等价于 `x[b*T*C + t*C + c]`，且通道维 C 是最内层。见 [u2-l1](u2-l1-layernorm-layer.md)（编码层讲的指针算术）。
- **前向/反向/链式法则**：前向算输出，反向把「上游梯度」`dout` 一路传回每个输入。涉及「同一个变量被多次使用时梯度要累加（`+=`）」的规则。见 [u1-l3](u1-l3-cpu-reference-overview.md)。
- **参数张量与激活张量的结构体**：`ParameterTensors` 用指针排布把 16 个参数张量钉进一块内存；`ActivationTensors` 同理排布 23 个激活缓冲区。见 [u1-l3](u1-l3-cpu-reference-overview.md)。

如果你对反向传播的微积分还不太熟，Karpathy 的《Becoming a Backprop Ninja》视频（在 [doc/layernorm/layernorm.md](doc/layernorm/layernorm.md) 中被推荐）会有帮助。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.c` | CPU 参考主线。包含本讲主角 `layernorm_forward`（78–118 行）与 `layernorm_backward`（120–161 行），以及在 `gpt2_forward` / `gpt2_backward` 中的三处前向、三处反向调用点。还包含 `ParameterTensors` / `ActivationTensors` 结构体定义。 |
| `doc/layernorm/layernorm.md` | Karpathy 写的 LayerNorm 教程正文，讲解 pre-norm、手动实现、反向推导思路、以及与 llama2.c 中 RMSNorm 的对比。本讲大量承接它的叙述。 |
| `doc/layernorm/layernorm.py` | 用「更初等的 PyTorch 算子」手写一遍 LayerNorm 的前向与反向，并与 PyTorch autograd 比对误差；同时把参考数据写进 `ln.bin`。这是 C 版的正确性标尺。 |
| `doc/layernorm/layernorm.c` | 一个**自包含**的小程序：读 `ln.bin`、跑自己的前向/反向、逐元素比对。`gcc layernorm.c -o layernorm -lm && ./layernorm` 即可看到全部 OK。本讲用它做可运行实践。 |

记忆要点：`train_gpt2.c` 里的 `layernorm_*` 和 `doc/layernorm/layernorm.c` 里的几乎是**逐行相同**的代码——前者嵌在完整模型里，后者抽出来单独可跑、单独可验证。先读懂后者，再回到前者。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 LayerNorm 的数学定义与 PyTorch 手动实现对照**：建立直觉，回答「它是什么、为什么 GPT-2 把它放在每个块的最前面」。
- **4.2 前向：mean / var / rstd 的计算与缓存**：把公式翻译成 C 的三重循环，理解为什么要缓存 mean 和 rstd。
- **4.3 反向梯度推导与实现**：推导 `dweight` / `dbias` / `dinp`，看懂反向为什么只需要两次归约。

### 4.1 LayerNorm 的数学定义与 PyTorch 手动实现对照

#### 4.1.1 概念说明

LayerNorm 由 [Ba et al. 2016](https://arxiv.org/abs/1607.06450) 提出，被 [Vaswani et al.](https://arxiv.org/abs/1706.03762) 的 Transformer 采用。它的作用很朴素：**对每一个位置 `(b,t)` 的 C 维向量单独做「去均值、除标准差、再缩放平移」**，让每个「纤维（fibre）」的数值分布稳定在零均值、单位方差附近，再由可学习的 `weight`、`bias` 恢复表达能力。

数学定义为：

\[
\text{LayerNorm}(x) = w \odot \frac{x - \mu}{\sqrt{\sigma^2 + \epsilon}} + b
\]

其中 \(\odot\) 是逐元素乘，\(\epsilon\) 是防止除零的小常数（本仓库取 `1e-5`），

\[
\mu = \frac{1}{C}\sum_{i=0}^{C-1} x_i, \qquad
\sigma^2 = \frac{1}{C}\sum_{i=0}^{C-1} (x_i - \mu)^2
\]

为了在代码里少算一次开方和除法，我们预先计算**倒数标准差（reciprocal standard deviation）**：

\[
\text{rstd} = \frac{1}{\sqrt{\sigma^2 + \epsilon}}
\]

于是归一化结果就是 \(\text{norm}_i = (x_i - \mu) \cdot \text{rstd}\)，输出 \(\text{out}_i = \text{norm}_i \cdot w_i + b_i\)。**rstd 是本讲最重要的中间量**，前向算出来、反向要复用。

**关于 pre-norm 位置**：原始 Transformer 是「post-norm」（残差相加之后再做 LayerNorm）。GPT-2 把它改成了 **pre-norm**——LayerNorm 是每个块**第一层**，残差主路保持「干净」（残差相加不再经过 LayerNorm）。这一改动显著提升了训练稳定性。在 `train_gpt2.c` 的前向里，你能看到每个块的第一个调用就是 `layernorm_forward`（见 4.2.3）。教程正文对这一点的说明见 [doc/layernorm/layernorm.md 的第 4 段](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L4)。

> 顺带一提：LayerNorm 的近亲 **RMSNorm**（Llama 系列使用）更简单——不减均值、无 bias，只按「均方根」归一化。`doc/layernorm/layernorm.md` 结尾贴了 llama2.c 的 RMSNorm 实现并做了对比，可作为延伸阅读，本讲不展开。

#### 4.1.2 核心流程

LayerNorm 前向对每个 `(b,t)` 位置独立处理，流程是「统计量 → 归一化 → 仿射」：

```text
对每个 batch b、每个时间步 t：
    1. 取出该位置的 C 维向量 x = inp[b, t, :]
    2. 算均值       mu      = mean(x)
    3. 算方差       var     = mean((x - mu)^2)
    4. 算倒数标准差 rstd    = 1 / sqrt(var + eps)
    5. 归一化       norm    = (x - mu) * rstd
    6. 缩放平移     out     = norm * weight + bias
    7. 缓存 mean[b,t] = mu, rstd[b,t] = rstd   # 留给反向用
```

注意第 7 步：前向顺手把 `mean` 和 `rstd` 写进两个 `(B,T)` 形状的缓冲区。它们很小（只有 B*T 个数，相比激活的 B*T*C 小一个 C 倍），反向直接读，省去重新统计。这就是教程正文里强调的「checkpointing（检查点）」权衡——存什么、重算什么，是显存与算力的取舍。

#### 4.1.3 源码精读：PyTorch 手动实现

`doc/layernorm/layernorm.py` 用最朴素的 PyTorch 算子（`sum`、`mean`、逐元素四则运算）把上面的公式写了一遍，这正是我们理解 C 版的「桥」：

[doc/layernorm/layernorm.py:8-18](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.py#L8-L18) —— 前向：`mean`、`var`、`rstd`、`norm`、`out`，并把 `(x, w, mean, rstd)` 作为 `cache` 返回。注意它**没有**把 `norm` 存进 cache，而是在反向里重算 `norm = (x - mean) * rstd`，用一点算力换内存。

数学公式与代码的逐项对应：

| 公式符号 | 代码 |
| --- | --- |
| \(\mu\) | `mean = x.sum(-1, keepdim=True) / C` |
| \(\sigma^2\) | `var = (xshift**2).sum(-1, keepdim=True) / C` |
| rstd | `rstd = (var + eps) ** -0.5` |
| \(\text{norm}\) | `norm = xshift * rstd` |
| out | `out = norm * w + b` |

对应的 C 版前向签名在 [train_gpt2.c:78-80](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L78-L80)，注释明确指出 `inp`、`out` 是 `(B,T,C)`，而 `mean`、`rstd` 是 `(B,T)` 缓冲区，留给反向使用。

#### 4.1.4 代码实践：跑通 doc/layernorm 的自检程序

`doc/layernorm/` 提供了一个最小的、不依赖模型的可运行示例，最适合用来建立对 LayerNorm 数值行为的直觉。

1. **实践目标**：亲手跑通「PyTorch 生成参考数据 → C 复现 → 逐元素比对」的闭环，确认 LayerNorm 前向/反向实现正确。
2. **操作步骤**（命令来自 [doc/layernorm/layernorm.md:243-254](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L243-L254)）：
   ```bash
   cd doc/layernorm
   python layernorm.py     # 生成 ln.bin，并打印 dx/dw/db 与 autograd 的误差
   gcc layernorm.c -o layernorm -lm
   ./layernorm             # 读 ln.bin，逐元素比对，打印 OK / NOT OK
   ```
3. **需要观察的现象**：
   - `python layernorm.py` 会打印三行误差（`dx error`、`dw error`、`db error`），数值应在 `1e-7` 量级。
   - `./layernorm` 会为 `out`、`mean`、`rstd`、`dx`、`dw`、`db` 每个元素打印 `OK a b`（a 是 C 算的、b 是 Python 写入的参考值），容差是 `1e-5`（见 [doc/layernorm/layernorm.c:94](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.c#L94)）。
4. **预期结果**：全部打印 `OK`，证明手写的前向/反向与 PyTorch autograd 在数值上一致。
5. 若本机没有 PyTorch / gcc 环境，则该项为「待本地验证」；可退而阅读 [doc/layernorm/layernorm.c:105-162](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.c#L105-L162) 的 `main`，理解它如何 `fread` 逐块读入张量并调用 `check_tensor` 比对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LayerNorm 要在方差里加 `eps = 1e-5`，而不是直接除以标准差？

> **答案**：当某个位置的 C 维向量所有元素几乎相等时，\(\sigma^2 \approx 0\)，标准差趋于 0，会出现「除以零」。加一个很小的 \(\epsilon\) 把分母抬离零，保证数值稳定；`1e-5` 足够小，对正常输入几乎不影响结果。

**练习 2**：教程正文说 `mean` 和 `rstd` 都存进了 cache，却把 `norm` 丢掉。为什么这样取舍？

> **答案**：`mean`、`rstd` 的形状是 `(B,T)`，很小；`norm` 的形状是 `(B,T,C)`，大一个 C 倍（GPT-2 里 C=768）。存小不存大，是用少量重算换可观的显存节省——这就是「checkpointing」权衡。

---

### 4.2 前向：mean / var / rstd 的计算与缓存

#### 4.2.1 概念说明

把 4.1 的 PyTorch 实现翻译成 C，关键有三点：

1. **没有张量抽象**：三维 `(B,T,C)` 直接用一维指针 + 行主序偏移表示。取 `inp[b,t,:]` 的基地址就是 `inp + b*T*C + t*C`。
2. **通道维最内层**：偏移每加 1，就在通道维上移动一格，所以一个位置的 C 个元素是**连续**的，可以直接用 `x[i]` 遍历。
3. **缓存写在哪**：`mean`、`rstd` 是 `(B,T)` 的，按 `mean[b*T + t]` 寻址写入，供反向读取。

#### 4.2.2 核心流程

前向是一个 `b → t → 内层 i` 的三重循环，对每个位置先做两个独立的「遍历求统计量」、再做一次「遍历写出输出」：

```text
for b in 0..B:
  for t in 0..T:
    x     = inp + b*T*C + t*C        # 定位到该位置 C 个连续元素
    m     = sum(x[i]) / C            # 第 1 次遍历：均值
    v     = sum((x[i]-m)^2) / C      # 第 2 次遍历：方差
    s     = 1 / sqrt(v + eps)        # rstd
    out_bt = out + b*T*C + t*C
    for i in 0..C:                   # 第 3 次遍历：归一化 + 仿射
        out_bt[i] = (s*(x[i]-m))*weight[i] + bias[i]
    mean[b*T+t] = m                  # 缓存
    rstd[b*T+t] = s                  # 缓存
```

注意方差用的是「总体方差（除以 C）」而不是「样本方差（除以 C-1）」，代码注释里写得很明确：`// calculate the variance (without any bias correction)`。

#### 4.2.3 源码精读

**前向函数本体**：[train_gpt2.c:78-118](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L78-L118) —— 这就是上面伪代码的 C 原貌。三个内层循环分别算均值、方差、输出；最后两行 `mean[b*T+t] = m; rstd[b*T+t] = s;` 把统计量缓存。它与 [doc/layernorm/layernorm.c:9-44](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.c#L9-L44) 逐行相同。

**mean / rstd 缓冲区从哪来**：在 `ActivationTensors` 结构体里，每个 LayerNorm 都配了一对 `(B,T)` 缓冲区。例如第 1 个块前的归一化对应 `ln1_mean`、`ln1_rstd`，形状都是 `(L, B, T)`：[train_gpt2.c:605-606](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L605-L606)。最终输出前的归一化对应 `lnf_mean`、`lnf_rstd`，形状 `(B, T)`：[train_gpt2.c:621-622](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L621-L622)。对应的可学习参数 `ln1w/ln1b`、`ln2w/ln2b`、`lnfw/lnfb` 在 `ParameterTensors` 里：[train_gpt2.c:540-553](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L540-L553)。

**它在模型里被调用三次**（每个 Transformer 块两次 + 最后一层一次），全部位于 `gpt2_forward`：

- 块内 attention 之前的 `ln1`：[train_gpt2.c:863](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L863)
- 块内 MLP 之前的 `ln2`：[train_gpt2.c:868](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L868)
- 进入分类头之前的 `lnf`：[train_gpt2.c:875](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L875)

这三处正好对应「pre-norm」：每一层的第一个动作都是 LayerNorm，残差主路 `residual` 始终保持干净。

#### 4.2.4 代码实践：追踪一次前向的指针寻址

1. **实践目标**：亲手验证 `(B,T,C)` 张量如何用一维指针访问，并确认 `inp[b,t,:]` 的 C 个元素确实连续。
2. **操作步骤**：写一个最小 C 程序（**示例代码，非项目源码**），构造一个 `B=1, T=2, C=3` 的数组，依次填入 `0,1,...,5`，然后打印 `inp + b*T*C + t*C` 指向的 3 个元素：
   ```c
   float inp[6] = {0,1,2,3,4,5};   // (1,2,3)
   int B=1, T=2, C=3;
   float* x = inp + 0*T*C + 1*C;   // 即 inp[b=0, t=1, :]
   // 打印 x[0], x[1], x[2]  =>  3, 4, 5
   ```
3. **需要观察的现象**：`x[0..2]` 应为 `3, 4, 5`，即第二行（t=1）的三个通道，验证了 `t*C` 偏移的正确性。
4. **预期结果**：输出 `3.0 4.0 5.0`。若把 `C` 改成 4，偏移含义不变，但每行覆盖 4 个元素。
5. 无法编译时，可改为阅读 [train_gpt2.c:90](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L90) 与 [doc/layernorm/layernorm.md:113](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L113) 中「`b*T*C + t*C + c`」的说明，在纸上手算一遍偏移。

#### 4.2.5 小练习与答案

**练习 1**：前向对每个位置 `(b,t)` 遍历了几次 C 维向量？为什么不能合并成一次？

> **答案**：三次（均值、方差、输出）。算方差必须先有均值，算输出又必须有方差（rstd），所以这三步有数据依赖，无法合并成单趟循环。这是 LayerNorm 前向的固有开销。

**练习 2**：`mean` 和 `rstd` 缓冲区大小是 `L*B*T` 还是 `L*B*T*C`？为什么？

> **答案**：是 `L*B*T`（见 [train_gpt2.c:635-636](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L635-L636)）。因为每个位置 `(b,t)` 只有一个均值和一个 rstd，与通道数 C 无关。

---

### 4.3 反向梯度推导与实现

#### 4.3.1 概念说明

反向的目标：给定上游梯度 `dout`（形状 `(B,T,C)`，即损失对 `out` 的偏导），求三个梯度：

- `dweight[i]` = \(\partial L / \partial w_i\)
- `dbias[i]` = \(\partial L / \partial b_i\)
- `dinp[b,t,i]` = \(\partial L / \partial x_i\)

其中 `dweight`、`dbias` 形状都是 `(C,)`（对所有 `b,t` 求和），`dinp` 形状是 `(B,T,C)`。

`dweight`、`dbias` 很简单，难点在 `dinp`：因为 \(x_i\) 不仅直接出现在 \(\text{norm}_i\) 里，还通过 \(\mu\) 和 \(\text{rstd}\) **间接**耦合了同一位置的所有通道。盲目地逐行反向会很繁琐——教程正文（[doc/layernorm/layernorm.md:67](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L67)）强调，这些表达式**可以解析地化简**，最终 `dinp` 只依赖两个「对通道维的归约量」。这正是 C 反向只做两次 reduce 的原因。

#### 4.3.2 核心流程：反向的推导

记 \(\text{norm}_i = (x_i - \mu)\cdot\text{rstd}\)，\(\text{out}_i = w_i\cdot\text{norm}_i + b_i\)，并令上游传到「归一化之后」的梯度为 \(\text{dnorm}_i = w_i \cdot \text{dout}_i\)。**以下所有求和都对通道维 \(i\) 进行，并除以 C 取平均。**

**第一步：weight 与 bias 的梯度。** 因为 out 对 w、b 是线性的：

\[
\text{db}_i = \text{dout}_i, \qquad
\text{dw}_i = \text{dout}_i \cdot \text{norm}_i
\]

再对 `(B,T)` 维求和（代码里用 `+=` 累加到 `(C,)` 上）。

**第二步：dinp 的推导。** 对固定的 `(b,t)` 位置，把 \(x_k\) 的梯度展开（链式法则中 \(\mu\)、\(\text{rstd}\) 都依赖所有 \(x\)）。解析化简后得到（推导细节见下方小框）：

\[
\frac{\partial L}{\partial x_k} = \text{rstd}\cdot\Big(\text{dnorm}_k \;-\; \underbrace{\frac{1}{C}\sum_i \text{dnorm}_i}_{\text{term 2}} \;-\; \text{norm}_k \cdot \underbrace{\frac{1}{C}\sum_i \text{dnorm}_i\cdot\text{norm}_i}_{\text{term 3}}\Big)
\]

三个 term 的直觉：

- **term 1（\(\text{dnorm}_k\)）**：\(x_k\) 直接出现在 \(\text{norm}_k\) 里的「直通」梯度。
- **term 2（\(\text{dnorm}\) 的均值）**：修正「减去均值 \(\mu\)」带来的耦合——\(\mu\) 牵动了所有通道，所以每个 \(x_k\) 都要扣掉一份平均梯度。
- **term 3（\(\text{norm}\cdot\text{dnorm}\) 的均值）**：修正「除以标准差（rstd）」带来的耦合——rstd 也依赖所有通道。

> **推导要点（供核对）**：记 \(y_i = x_i - \mu\)（注意 \(\sum_j y_j = 0\)），则 \(\text{norm}_i = y_i\cdot\text{rstd}\)。
> \(\partial y_i/\partial x_k = \delta_{ik} - 1/C\)；
> 由 \(v = \frac1C\sum_j y_j^2\) 可得 \(\partial v/\partial x_k = \frac{2}{C}y_k\)，进而 \(\partial\text{rstd}/\partial x_k = -\frac{1}{2}\text{rstd}^3\cdot\frac{2}{C}y_k = -\frac{\text{rstd}^2}{C}\text{norm}_k\)。
> 代入 \(\partial\text{norm}_i/\partial x_k = \text{rstd}(\delta_{ik}-1/C) - \frac{\text{rstd}}{C}\text{norm}_i\text{norm}_k\)，对 \(i\) 用 \(\text{dnorm}_i\) 加权求和，整理即得上式。

**关键收益**：term 2、term 3 都是「先归约成标量、再广播」——所以反向只需**两次对 C 的 reduce**，就能得到全部 \(C\) 个 `dinp`。C 代码因此先做一次循环算两个累加和，再做一次循环写出梯度。

#### 4.3.3 源码精读

**反向函数本体**：[train_gpt2.c:120-161](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L120-L161) —— 与推导完全对应。它与 [doc/layernorm/layernorm.c:46-87](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.c#L46-L87) 逐行相同。逐段对应如下：

- 读取缓存的统计量：`mean_bt = mean[b*T+t]; rstd_bt = rstd[b*T+t];`（[L128-L129](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L128-L129)）——这就是前向缓存的复用点。
- **第一次循环（两次归约）**：[L131-L141](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L131-L141) —— 计算 `dnorm_mean = (1/C)Σ dnorm_i`（term 2）和 `dnorm_norm_mean = (1/C)Σ dnorm_i·norm_i`（term 3）。
- **第二次循环（写梯度）**：[L143-L158](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L143-L158) ——
  - `dbias[i] += dout_bt[i];`（[L148](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L148)）对应 \(\text{db}_i\)；
  - `dweight[i] += norm_bti * dout_bt[i];`（[L150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L150)）对应 \(\text{dw}_i\)；
  - `dval = dnorm_i - dnorm_mean - norm_bti*dnorm_norm_mean; dval *= rstd_bt;`（[L152-L156](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L152-L156)）对应 \(\text{dinp}_k\) 的三个 term 与最后的 rstd 缩放。

注意所有梯度都是 `+=`（[L148](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L148)、[L150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L150)、[L157](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L157)）——这与 [u2-l1](u2-l1-encoder-layer.md) 讲的「同一变量被多处使用时梯度累加」一致，依赖 `gpt2_zero_grad` 在反向前把梯度清零。

**模型里的反向调用三次**，位于 `gpt2_backward`，顺序与前向**严格相反**：

- 最后一层 `lnf` 的反向（最先反传）：[train_gpt2.c:938](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L938)
- 块内 `ln2` 的反向：[train_gpt2.c:997](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L997)
- 块内 `ln1` 的反向（最后反传）：[train_gpt2.c:1002](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1002)

对照前向调用顺序（`ln1` → `ln2` → `lnf`），反向正是镜像（`lnf` → `ln2` → `ln1`）。

#### 4.3.4 代码实践：纸笔推导并对照 C 代码

这是本讲的核心实践，也是规格要求的任务。

1. **实践目标**：自己推导 `dweight`、`dbias`、`dinp` 的公式，再与 `train_gpt2.c` 的 C 代码逐行核对，确认理解无误。
2. **操作步骤**：
   - 在纸上写出前向：\(\text{norm}_i = (x_i-\mu)\cdot\text{rstd}\)，\(\text{out}_i = w_i\cdot\text{norm}_i + b_i\)。
   - 由 \(\text{out}_i\) 分别对 \(b_i\)、\(w_i\) 求偏导，得到 `dbias`、`dweight`（别忘了要对 `(B,T)` 求和）。
   - 令 \(\text{dnorm}_i = w_i\cdot\text{dout}_i\)，按 4.3.2 的三个 term 写出 `dinp`，并标注哪一项对应「减均值的耦合」、哪一项对应「除标准差的耦合」。
   - 打开 [train_gpt2.c:120-161](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L120-L161)，把你纸上的 term 1/2/3 与代码注释 `// term 1`、`// term 2`、`// term 3`（[L153-L155](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L153-L155)）一一对应。
   - 再对照 [doc/layernorm/layernorm.py:20-32](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.py#L20-L32) 的 PyTorch 版反向，确认 `dx = dnorm - dnorm.mean(-1) - norm*(dnorm*norm).mean(-1)` 与 C 版三个 term 完全等价。
3. **需要观察的现象**：纸面公式的每一项都能在 C 代码里找到对应的一行；PyTorch 的向量化写法（`.mean(-1, keepdim=True)`）与 C 的「先 reduce 成标量、再广播」是同一件事的两种表达。
4. **预期结果**：你能在代码里指出：term 2 的 `dnorm_mean` 由第一次循环的累加 `/C` 得到（[L140](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L140)），term 3 的 `dnorm_norm_mean` 同理（[L141](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L141)）；最后的 `*= rstd_bt` 对应公式最外层的 rstd（[L156](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L156)）。
5. 推导若有不确定处，回到 [doc/layernorm/layernorm.md:63-83](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L63-L83) 的原文，Karpathy 给出了与 BatchNorm 类比的思路。

#### 4.3.5 小练习与答案

**练习 1**：反向为什么需要**两次**遍历 C 维，而不是一次？

> **答案**：term 2、term 3 需要全通道的归约量（`dnorm_mean`、`dnorm_norm_mean`），它们是标量，必须先遍历一遍算出来；第二次遍历才能用这两个标量写出每个通道的 `dinp`。除非把这些归约换成在线算法，否则两遍不可合并。

**练习 2**：把代码里所有 `+=` 改成 `=` 会出什么问题？

> **答案**：`dbias`、`dweight`、`dinp` 都是对 `(B,T)` 或多个调用点累加的。用 `=` 会覆盖之前累计的梯度，导致同一参数被多处使用时梯度丢失。所以仓库里坚持用 `+=`，并在反向前用 `gpt2_zero_grad` 清零（见教程正文 [doc/layernorm/layernorm.md:213](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.md#L213) 的说明）。

**练习 3**：反向里 `dnorm_i = weight[i] * dout_bt[i]` 这一步对应的物理含义是什么？

> **答案**：它把上游梯度从 `out` 传到「归一化之后」的 `norm`。因为 \(\text{out}_i = w_i\cdot\text{norm}_i + b_i\)，对 \(\text{norm}_i\) 求偏导就是 \(w_i\)，所以 \(\text{dnorm}_i = w_i\cdot\text{dout}_i\)。这一步把 weight 的影响「剥」出来，之后推导 `dinp` 时就只需关心 norm → x 这段路径。

---

## 5. 综合实践

把本讲的「前向缓存 + 反向复用」串起来，完成下面这个贯穿任务：

**任务**：以 `doc/layernorm/layernorm.c` 为蓝本，回答下列问题，并把答案写成一个简短笔记。

1. **格式确认**：`ln.bin` 里 `mean`、`rstd` 各占多少字节？（提示：形状是 `(B,T)=(2,3)`，`float` 为 4 字节。）对照 [doc/layernorm/layernorm.py:60-70](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.py#L60-L70) 的写入顺序与 [doc/layernorm/layernorm.c:128-137](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/doc/layernorm/layernorm.c#L128-L137) 的读取顺序，确认两者对称。
2. **去掉缓存会怎样**：假设前向不写 `mean`/`rstd`，反向需要从 `inp` 重新算一遍均值方差。请说明这会让反向多出几次对 C 的遍历，并指出代码里哪几行会变成重算（参考 [train_gpt2.c:128-L129](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L128-L129)）。
3. **误差来源**：运行 `./layernorm` 后，挑一个打印为 `OK` 的 `out` 元素，手算 `a` 与 `b` 的差值，确认它 ≤ `1e-5`；再回答：这个差值主要来自哪里？（提示：`1e-5` 既是 `eps` 也是 `check_tensor` 的容差，但误差主要来自浮点累加顺序，而非 eps。）
4. **回到主线**：在 [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) 里画出第 0 层 Transformer 块中 LayerNorm 的数据流：`residual → ln1_forward(l_ln1, l_ln1_mean, l_ln1_rstd, ...) → matmul(qkv)`，并标出反向时这三个缓冲区如何被 `layernorm_backward` 读取。

完成本任务后，你应该能向别人讲清楚：「LayerNorm 前向算 mean/rstd 并缓存，反向只靠两个 reduce 就把梯度传回输入；这套代码在 `train_gpt2.c` 里被调用三次，且前向/反向严格镜像。」

## 6. 本讲小结

- LayerNorm 对每个 `(b,t)` 位置的 C 维向量做「去均值、除标准差、再缩放平移」，数学上是 \(w\odot\frac{x-\mu}{\sqrt{\sigma^2+\epsilon}}+b\)；GPT-2 用 pre-norm，把它放在每个块最前面。
- 前向用三重循环算 \(\mu\)、\(\sigma^2\)、rstd 并写出 out，同时把 `mean`、`rstd` 缓存进 `(B,T)` 缓冲区，供反向复用——这是「显存 vs 算力」的 checkpointing 取舍。
- 反向 \(\text{dinp}\) 经解析化简后只需**两次对通道维的归约**（`dnorm_mean` 与 `dnorm_norm_mean`），对应公式里的 term 2、term 3；term 1 是直通梯度。`dweight`、`dbias` 直接由 `dout`、`norm` 给出。
- `train_gpt2.c` 的 `layernorm_forward/backward` 与 `doc/layernorm/layernorm.c` 逐行相同；在模型里被调用三次（ln1/ln2/lnf），前向顺序与反向顺序严格镜像。
- 所有梯度都使用 `+=`，依赖 `gpt2_zero_grad` 清零；这与编码层等其它层保持一致的累加风格。
- `mean`、`rstd` 缓冲区在 `ActivationTensors` 里成对出现（如 `ln1_mean`/`ln1_rstd` 形状 `(L,B,T)`），权重在 `ParameterTensors` 里（如 `ln1w`/`ln1b` 形状 `(L,C)`）。

## 7. 下一步学习建议

- **横向**：继续本单元后续讲义。[u2-l3 MatMul](u2-l3-matmul-layer.md) 讲紧挨着 LayerNorm 的线性投影；[u2-l5 GELU 与残差](u2-l5-gelu-residual.md) 讲另一个带前向/反向的算子；它们的代码风格与 LayerNorm 完全一致，读完会形成「手写前向/反向」的肌肉记忆。
- **纵向（组装）**：学完各层后，进入 [u2-l7 前向组装](u2-l7-forward-assembly.md)，看 `gpt2_forward` 如何把本讲的 LayerNorm 调用串成完整 Transformer。
- **对照 GPU**：本讲的 CPU 版是「最清楚的参考」。日后学 [u5-l4 CUDA 各层 kernel](u5-l1-cuda-mainline-architecture.md) 时，会看到 `llmc/layernorm.cuh` 如何用 **warp 级归约**替代本讲的「对 C 的内层循环」——到那时再回看本讲的两次 reduce，就能立刻明白 GPU 版优化了什么。
- **延伸阅读**：[doc/layernorm/layernorm.md](doc/layernorm/layernorm.md) 结尾的 RMSNorm 对比，以及 Karpathy 的《Becoming a Backprop Ninja》视频，适合想深入反向推导的读者。
