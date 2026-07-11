# PyTorch 参考实现 train_gpt2.py

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 `train_gpt2.py` 在 llm.c 项目里扮演的三重角色：**算法参考、正确性标尺、`.bin` 权重的生产者**。
2. 把 PyTorch 的 `GPTConfig / GPT / Block / CausalSelfAttention / MLP / NewGELU` 与 C 版 `train_gpt2.c` 中的结构体和函数**逐一对应**起来。
3. 理解 `configure_optimizers` 的**分组权重衰减（grouped weight decay）**，并指出它与 C 版 `gpt2_update` 的结构性差异。
4. 看懂 PyTorch 训练主循环（前向 / 反向 / 优化）与 C 版四步循环的对应，并能识别哪些是 autograd **自动完成**的。
5. 完成一份 `CausalSelfAttention` 与 `attention_forward/_backward` 的逐行对照表。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l1～u1-l4**：知道仓库有 `train_gpt2.py`（PyTorch 参考）、`train_gpt2.c`（C/CPU 参考）、`train_gpt2.cu`（CUDA 主线）三套实现，以及 `.bin` 数据格式。
- **u2-l1～u2-l7**：理解 GPT-2 前向的每一层（encoder、layernorm、matmul、attention、gelu、residual、softmax/crossentropy）以及它们如何被 `gpt2_forward` 串起来。
- **u3-l1、u3-l2**：理解 `gpt2_backward` 的反向组装，以及 `gpt2_update` 实现的 AdamW。

几个本讲会反复用到的术语，先用一句话复习：

- **autograd（自动微分）**：PyTorch 的核心机制。你只要写前向，PyTorch 在前向时**自动**记录一个计算图，调用 `loss.backward()` 时自动沿图反向，把每个参数的梯度填到 `.grad` 里。这正是 C 版要**手写** `*_backward` 的部分。
- **nanoGPT 风格**：`train_gpt2.py` 的模型结构来自 Karpathy 的 nanoGPT——用最少的 PyTorch 算子把 GPT-2 写清楚。llm.c 的 C 版可以看作「去掉 autograd、全部手写的 nanoGPT」。
- **下一个 token 预测**：模型输入一段 token，输出每个位置对词表的概率分布，目标是预测下一个 token。损失是交叉熵。
- **权重绑定（weight tying）**：输入端的 token embedding 表 `wte` 和输出端的分类头 `lm_head` **共享同一份权重**，GPT-2 用这个技巧省参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train_gpt2.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py) | PyTorch 参考实现。定义模型、训练循环，并把权重与「标准答案」写成 `.bin`。本讲的**主角**。 |
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | C/CPU 参考实现。把同一套算法**手写**出来，是与 PyTorch 版逐行对照的**标尺**。 |

本讲只引用这两个文件。`train_gpt2.py` 中我们会重点看四块：

- **模型定义**：`NewGELU`、`CausalSelfAttention`、`MLP`、`Block`、`GPTConfig`、`GPT`（第 40–190 行）。
- **优化器配置**：`GPT.configure_optimizers`（第 241–270 行）。
- **生成**：`GPT.generate`（第 272–297 行）。
- **Python→C 桥接**：`write_model` / `write_state`（第 449–507 行）与训练主循环（第 703–829 行）。

## 4. 核心概念与源码讲解

### 4.1 为什么需要 PyTorch 参考实现：autograd 与「正确性标尺」

#### 4.1.1 概念说明

读到这里你已经会用 C 手写前向和反向了。一个自然的问题是：**既然 C 版已经能训练，为什么还要维护一份 PyTorch 版？**

答案有三个：

1. **它是「正确性标尺」**。手写反向极易出错（符号错、累加漏、索引偏），而 PyTorch 的 autograd 经过大规模验证，几乎不可能错。所以 llm.c 的策略是：让 PyTorch 跑一次前向 + 反向，把输入、logits、loss、16 个梯度**全部导出**成一个二进制「标准答案」`gpt2_124M_debug_state.bin`，C 版的 `test_gpt2.c` 拿它逐元素比对（详见 u3-l4）。**PyTorch 版是判官，C 版是被判的。**

2. **它是 `.bin` 权重的生产者**。C 版没有内置的随机初始化与预训练权重加载逻辑；它直接从 PyTorch 导出的 `gpt2_124M.bin` 读权重起跑（u1-l2、u4-l2）。`GPT.from_pretrained` 还能从 HuggingFace 拉取官方 GPT-2 权重再导出。

3. **它把 autograd「翻译」成显式代码**。PyTorch 一行 `loss.backward()` 背后做的事，C 版要用十几个 `*_backward` 函数手写。对照两者，你能**看穿 autograd 的黑盒**——这正是本讲的主线。

> 关键直觉：**PyTorch 的 forward = C 的 forward；PyTorch 的 `backward()` = C 的全部 `*_backward` 手写代码。** 本讲的对照都围绕这一句话展开。

#### 4.1.2 核心流程

PyTorch 与 C 在「一次训练步」上的职责划分：

```text
PyTorch 训练步                         C 训练步（train_gpt2.c main）
─────────────────────                  ─────────────────────────────
logits, loss = model(x, y)   ──┐       gpt2_forward(...)      ──┐  前向（都要手写）
                               │       (内含各层 *_forward)      │
loss.backward()              ──┤  反向  gpt2_zero_grad(...)      │  清零梯度
  ↑ autograd 自动算所有梯度     │       gpt2_backward(...)      ──┤  反向（都要手写）
                               │       (内含各层 *_backward)      │
optimizer.step()            ──┘       gpt2_update(...)        ──┘  AdamW（都要手写）
```

- 左边 `loss.backward()` 一行，等于右边 `crossentropy_softmax_backward` + 各层 `*_backward` + 残差合流等**几百行手写代码**。
- 两边的 `optimizer.step()` / `gpt2_update()` 都是显式的 AdamW，逻辑几乎逐行一致（见 4.4）。

#### 4.1.3 源码精读

PyTorch 版的「导出标准答案」逻辑在训练开始前执行一次：

- [train_gpt2.py:685-698](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L685-L698)：在主进程里取一个 batch，做**一次**前向 + 反向，然后调用 `write_model`（导出权重）和 `write_state`（导出 x、y、logits、loss、16 个梯度作为 debug state）。注意此时**优化器还没动**，所以导出的梯度与 `lr / wd / betas` 无关。
- [train_gpt2.py:479-507](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L479-L507)：`write_state` 的实现。魔数 `20240327`、版本 2、一律 fp32，写顺序为「header(256 int32) → x(B,T) → y(B,T) → logits → loss → 16 个梯度」。C 端的 `test_gpt2.c` 用同一魔数对称读取。

这一段是理解「PyTorch 是标尺」的钥匙：debug state 是 PyTorch 在**优化器介入之前**产出的快照，因此纯粹反映前向 + 反向的数值正确性。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「debug state 与优化器超参无关」。

**操作步骤**：

1. 打开 [train_gpt2.py:685-698](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L685-L698)，确认 `write_state` 在 `loss.backward()` 之后、训练循环之前被调用。
2. 打开 [train_gpt2.py:479-507](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L479-L507)，确认它写入的是 `param.grad`（梯度），不涉及 `m / v` 动量或 `lr`。

**预期结果**：你会看到 debug state 只含前向（logits、loss）与反向（梯度）的产物，**不**含任何优化器状态。这就解释了为什么 u3-l4 里 C 版 `test_gpt2.c` 即使把 `betas`、`weight_decay` 调成与 PyTorch 主循环不同的值，step-0 的逐元素比对仍然成立——优化器超参只影响第 1 步**之后**的 loss 曲线。

> 待本地验证：若你手头有生成的 `gpt2_124M_debug_state.bin`，可用 `ls -l` 核对其大小与 `1024(头) + B*T*4*2 + ...` 的预算是否吻合（B、T 由 header 给出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 llm.c 选择「PyTorch 导出标准答案、C 去比对」，而不是反过来「C 导出、PyTorch 比对」？

> **参考答案**：因为 autograd 经过工业级验证，可信度高，适合做判官；而 C 版的手写反向正是**被检验对象**。让被检验者当标尺会循环论证。

**练习 2**：`write_state` 导出的梯度，是「一步 AdamW 更新后」的还是「更新前」的？

> **参考答案**：更新前的。它在 `loss.backward()` 之后、训练循环之前导出，此时 `param.grad` 刚由 autograd 填好，优化器尚未介入。

---

### 4.2 GPTConfig 与 GPT 顶层模块对照

#### 4.2.1 概念说明

模型的所有张量形状，都由少数几个**超参数**决定。两套实现各有一个配置结构：

- PyTorch 用 `@dataclass` 的 `GPTConfig`。
- C 用 `typedef struct {...} GPT2Config`。

它们字段一一对应，只是命名和「词表填充」的处理方式不同。本模块把它们对齐，并对照两套实现的**顶层 `forward`**：`GPT.forward`（PyTorch）与 `gpt2_forward`（C）。

#### 4.2.2 核心流程

字段对照表（GPT-2 124M 取值）：

| 含义 | PyTorch `GPTConfig` | C `GPT2Config` | 124M 取值 |
| --- | --- | --- | --- |
| 最大序列长度 | `block_size` | `max_seq_len` | 1024 |
| 真实词表大小 | `vocab_size` | `vocab_size` | 50257 |
| 对齐后词表大小 | （导出时填充） | `padded_vocab_size` | 50304 |
| 层数 | `n_layer` | `num_layers` | 12 |
| 头数 | `n_head` | `num_heads` | 12 |
| 通道（嵌入维） | `n_embd` | `channels` | 768 |

> 注意 **`padded_vocab_size` 只在 C 侧显式存在**：PyTorch 训练时词表就是 50257，只有在 `write_model` 导出给 C 时，才用 `pad_vocab` 把它填充到 50304（128 的倍数，便于 GPU 上做对齐的矩阵乘）。这是 u4-l2 的主题，这里先记住「填充发生在 Python→C 边界」即可。

顶层 forward 的对照（前向主线）：

```text
GPT.forward (PyTorch)                       gpt2_forward (C)
─────────────────────────                   ─────────────────────────
tok_emb = wte(idx)        ┐                 encoder_forward(…)        ┐
pos_emb = wpe(pos)        ├ embedding       (wte 查表 + wpe 查表相加)  ├ encoder
x = tok_emb + pos_emb     ┘                                            ┘
for block in h: x = block(x)  ┐ n_layer 个块  for l in 0..L: …各层…   ┐ for 循环
x = ln_f(x)                   │             layernorm_forward(lnf,…)   │
logits = lm_head(x)           │             matmul_forward(logits,…wte, NULL,…)
loss = F.cross_entropy(...)   ┘             softmax_forward + crossentropy_forward
```

两处关键约定：

- **权重绑定**：PyTorch `GPT` 在初始化时让 `lm_head.weight = wte.weight`；C 版没有单独的 `lm_head`，而是在 `gpt2_forward` 里直接把 `params.wte` 当作 logits 投影的权重、`bias=NULL`。
- **损失融合**：PyTorch 一行 `F.cross_entropy` 内部是 log_softmax+NLL（数值稳定的融合实现）；C 版把它拆成 `softmax_forward` + `crossentropy_forward`，但在反向里用 `crossentropy_softmax_backward` **融合**回来（u2-l6）。

#### 4.2.3 源码精读

- [train_gpt2.py:120-126](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L120-L126)：`GPTConfig` 数据类，6 个字段（注：PyTorch 版**没有** `padded_vocab_size`，填充在导出时处理）。
- [train_gpt2.py:134-142](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L134-L142)：`GPT.__init__`。`transformer` 是一个 `ModuleDict`，含 `wte`（Embedding）、`wpe`（Embedding）、`h`（n_layer 个 `Block`）、`ln_f`（LayerNorm）。第 140 行 `lm_head` 显式 `bias=False`，第 142 行把 `wte.weight` 和 `lm_head.weight` 绑定为同一份张量——这就是权重绑定。
- [train_gpt2.py:162-190](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L162-L190)：`GPT.forward`。第 169–171 行 embedding 相加；第 173–174 行跑 `n_layer` 个 block；第 175 行 `ln_f`；第 177–184 行根据 `targets` 是否为 None 走「算 loss」或「只取最后一位算 logits」两条路。
- [train_gpt2.c:526-533](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L526-L533)：C 版 `GPT2Config`，6 个字段，多了 `padded_vocab_size`。
- [train_gpt2.c:825-877](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L825-L877)：`gpt2_forward` 的主线。第 825 行 `encoder_forward` 对应 embedding 相加；第 826–873 行是对应 `for block in h` 的层循环；第 875–877 行是 `ln_f` + logits 投影（用 `params.wte`、`NULL` bias）+ softmax，对应 PyTorch 第 175–180 行。

> 一个值得注意的差异：PyTorch 在**推理**（`targets=None`）时有个小优化——第 183 行 `self.lm_head(x[:, [-1], :])` 只对最后一个位置算 logits。C 版**没有**这个优化，生成时是对整段 `B×T` 重算前向（u3-l3）。换言之，PyTorch 版更省，C 版更「老实」。

#### 4.2.4 代码实践

**实践目标**：亲手验证「PyTorch 的权重绑定 == C 版用 `wte` 当 logits 投影」。

**操作步骤**：

1. 读 [train_gpt2.py:140-142](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L140-L142)，确认 `lm_head` 的权重就是 `wte.weight`（二者是同一个张量对象）。
2. 读 [train_gpt2.c:876](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L876)，确认 logits 投影的权重参数传的是 `params.wte`，bias 传的是 `NULL`。
3. 读 [train_gpt2.c:538](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L538)，确认 `wte` 的形状注释是 `(V, C)`——既是 token embedding 表，又是 logits 投影的权重（`C→V`，转置后即 `(V, C)`）。

**预期结果**：你能解释「为什么 `wte` 的梯度会被两处累加」（输入端查表 + 输出端投影），并联系 u2-l1 里讲过的 `encoder_backward` 与 u3-l1 里讲过的 `gpt2_backward` 对 `dwte` 的两路 `+=`。

#### 4.2.5 小练习与答案

**练习 1**：PyTorch 的 `GPTConfig` 没有 `padded_vocab_size`，那 C 版 `gpt2_forward` 里 logits 的形状 `(B, T, Vp)` 中的 `Vp=50304` 是从哪来的？

> **参考答案**：来自 checkpoint 头部。`write_model` 导出时把填充后的词表大小写进 header 第 8 项（[train_gpt2.py:472](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L472)），C 版 `gpt2_build_from_checkpoint` 读出来填进 `padded_vocab_size`。

**练习 2**：`GPT.forward` 里 `targets is None` 的分支，对应 C 版什么场景？

> **参考答案**：对应生成/采样场景。C 版 `gpt2_forward(..., targets=NULL, ...)` 时不算 loss（`mean_loss` 置为 `-1.0f`），见 [train_gpt2.c:887-890](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L887-L890)。但 C 版不做 PyTorch 的「只算最后一位」优化。

---

### 4.3 Block 子模块：注意力、MLP 与残差对照

#### 4.3.1 概念说明

Transformer 的每一层（一个 `Block`）结构相同：**先注意力子层，再 MLP 子层，各带一个残差连接，且都用 pre-norm**。PyTorch 把它写得极简，正好用来对照 C 版那一长串函数调用。

```python
# train_gpt2.py:112-115  —— 整个 Block 的前向只有两行
def forward(self, x):
    x = x + self.attn(self.ln_1(x))   # 注意力子层 + 残差
    x = x + self.mlp(self.ln_2(x))    # MLP 子层 + 残差
    return x
```

这两行里藏了大量 C 版的「手写展开」：

- `self.ln_1(x)` → `layernorm_forward`
- `self.attn(…)` → 一次 `matmul_forward`（c_attn）+ `attention_forward`（核心注意力）+ 一次 `matmul_forward`（c_proj）
- `x + …` → `residual_forward`
- `self.ln_2(x)` → `layernorm_forward`
- `self.mlp(…)` → `matmul_forward`（c_fc）+ `gelu_forward`+ `matmul_forward`（c_proj）
- 再一次 `x + …` → `residual_forward`

反向时，PyTorch 这两行的梯度**全部由 autograd 自动算出**；C 版则要按 u3-l1 的逆序手写对应 `*_backward`。

#### 4.3.2 核心流程

**注意力子层对照**（本讲实践的主角）：

| 步骤 | PyTorch `CausalSelfAttention.forward` | C 对应 |
| --- | --- | --- |
| QKV 投影 | `qkv = self.c_attn(x)`（一次 `Linear(C, 3C)`） | `matmul_forward(l_qkv, l_ln1, l_qkvw, l_qkvb, B,T,C, 3C)` |
| 拆 Q/K/V + 多头 | `q,k,v = qkv.split(...)` + `view/transpose` | `attention_forward` 内部用指针偏移 `+0 / +C / +2C` 与 `h*hs` 虚拟切分 |
| 打分 + 因果 mask + softmax | `(q@k.T)/sqrt(hs)` → `masked_fill(-inf)` → `softmax` | `attention_forward` 的 pass1~3（maxval 稳定 + 因果置零） |
| 加权求和 value | `y = att @ v` | `attention_forward` 的 pass4 |
| 输出投影 | `y = self.c_proj(y)` | `matmul_forward(l_attproj, l_atty, l_attprojw, l_attprojb, B,T,C, C)` |

**MLP 子层对照**：

| 步骤 | PyTorch `MLP.forward` | C 对应 |
| --- | --- | --- |
| 升维 | `x = self.c_fc(x)`（`Linear(C, 4C)`） | `matmul_forward(l_fch, l_ln2, l_fcw, l_fcb, B,T,C, 4C)` |
| 激活 | `x = self.gelu(x)`（`NewGELU`） | `gelu_forward(l_fch_gelu, l_fch, B*T*4C)` |
| 降维 | `x = self.c_proj(x)`（`Linear(4C, C)`） | `matmul_forward(l_fcproj, l_fch_gelu, l_fcprojw, l_fcprojb, B,T,4C, C)` |

**GELU 公式**：两版都用 tanh 近似（与 OpenAI 官方一致）：

\[
\mathrm{GELU}(x) = 0.5\,x\left(1 + \tanh\!\left(\sqrt{\tfrac{2}{\pi}}\,\bigl(x + 0.044715\,x^3\bigr)\right)\right)
\]

#### 4.3.3 源码精读

- [train_gpt2.py:48-86](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L48-L86)：`CausalSelfAttention`。第 54 行 `c_attn` 是 `Linear(C, 3C)`；第 68–72 行投影并拆 Q/K/V、做多头 reshape；第 79–82 行是**手写注意力**（PyTorch 版默认走这条，`FLASH=1` 才用 `scaled_dot_product_attention`）；第 85 行 `c_proj` 输出投影。
- [train_gpt2.py:62-63](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L62-L63)：`register_buffer("bias", …)` 注册了一个下三角因果 mask。第 80 行 `att.masked_fill(self.bias[…]==0, -inf)` 用它把「未来」位置在 softmax 前置成 `-inf`。
- [train_gpt2.py:88-101](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L88-L101)：`MLP`，三步 `c_fc → gelu → c_proj`。
- [train_gpt2.py:40-43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L40-L43)：`NewGELU`，注释强调「这是 OpenAI 用的那个精确版本」。
- [train_gpt2.c:271-345](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L271-L345)：`attention_forward`，对应 PyTorch 第 79–82 行。它用 `maxval` 减法做数值稳定（等价于 PyTorch 的 softmax），用「循环只到 `t2<=t`」实现因果 mask（等价于 `masked_fill`）。
- [train_gpt2.c:347-405](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L347-L405)：`attention_backward`——**PyTorch 里这一整段由 autograd 自动完成**。
- [train_gpt2.c:863-872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L863-L872)：`gpt2_forward` 层循环体，把上面那张表里的 C 版调用**按顺序**排成 10 行，正好对应 `Block.forward` 的两行。
- [train_gpt2.c:408-421](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L408-L421)：`gelu_forward`，对应 `NewGELU`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `train_gpt2.py` 的 `CausalSelfAttention` 与 `train_gpt2.c` 的 `attention_forward` / `attention_backward` 做成一份**逐行对照表**，并标注哪些是框架自动完成的。

**操作步骤**：

1. 并排打开 [train_gpt2.py:65-86](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L65-L86)（`CausalSelfAttention.forward`）与 [train_gpt2.c:271-345](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L271-L345)（`attention_forward`）。
2. 按下表逐行填写。**「机制」一列**是本实践的重点：标注每一步在 PyTorch 里是「你写的」还是「autograd / 框架自动完成的」。

参考对照表（你可以补全剩余行）：

| # | PyTorch 代码（行） | C 代码（行） | 机制 |
|---|---|---|---|
| 1 | `qkv = self.c_attn(x)`（68） | `matmul_forward(l_qkv, …, 3C)`（864，在 forward 里） | 两边都手写前向；反向：PyTorch autograd / C 手写 `matmul_backward` |
| 2 | `q,k,v = qkv.split(n_embd, dim=2)`（69） | `attention_forward` 内 `+0/+C/+2C` 偏移（289,296,336） | PyTorch 用 `split`（view，零拷贝）；C 用指针算术 |
| 3 | 多头 `view(B,T,nh,hs).transpose`（70–72） | `hs=C/NH; h*hs` 偏移（282,288） | PyTorch reshape；C 用「虚拟切分」不物理分块 |
| 4 | `att = (q @ k.transpose(-2,-1)) / sqrt(hs)`（79） | pass1 内积循环 × scale（295–309） | 两边都算打分；C 多算 `maxval` 做稳定 |
| 5 | `att.masked_fill(bias==0, -inf)`（80） | 循环上界 `t2<=t`（295）+ 显式置零（326–328） | PyTorch 用 mask buffer；C 用循环边界天然因果 |
| 6 | `att = F.softmax(att, dim=-1)`（81） | pass2 exp/sum + pass3 归一化（313–330） | 两边都手写前向 |
| 7 | `y = att @ v`（82） | pass4 加权求和 value（332–341） | 两边都手写前向 |
| 8 | `y = …transpose/contiguous/view`（83） | 写回 `out + b*T*C + t*C + h*hs`（333） | PyTorch reshape；C 直接指针写回 |
| 9 | `y = self.c_proj(y)`（85） | `matmul_forward(l_attproj, …)`（866） | 两边都手写前向 |
| ★ | `loss.backward()` 里注意力全部反向 | `attention_backward`（347–405） | **PyTorch 自动；C 手写** |

3. 重点回答两个问题（写进你的对照表备注）：
   - **PyTorch 第 80 行的 `-inf` 因果 mask，在 C 版是用什么机制实现的？** 答：C 版前向**根本不计算** `t2>t` 的打分（循环上界是 `t2<=t`），并在 pass3 把 `t2>t` 的 `att` 显式置零（仅为方便对齐调试）。
   - **PyTorch 的 `attention_backward` 写在哪？** 答：**没有写**。它由 autograd 在 `loss.backward()` 时自动生成。C 版则必须手写 [train_gpt2.c:347-405](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L347-L405)，且是前向的严格逆序（value→softmax 雅可比→q@k）。

**预期结果**：你得到一张表，清楚地显示出——**前向**两边都要写（PyTorch 写法更短），而**反向**只有 C 版需要手写；这就是 autograd 帮 PyTorch 省掉的全部工作量。

> 待本地验证：若你装了 PyTorch，可在一个 2×4×8 的 toy 张量上同时跑 PyTorch 的 `CausalSelfAttention`（`FLASH=0`）与一份照搬 `attention_forward` 的 numpy 实现，断言两者输出最大绝对差 < 1e-5。

#### 4.3.5 小练习与答案

**练习 1**：PyTorch 第 79 行打分时**没有**先减 `maxval`，为什么不会数值溢出？C 版为什么又减了？

> **参考答案**：PyTorch 的 `F.softmax` 内部已经做了「减最大值」的稳定化，所以用户代码里不用再减。C 版是**手写** softmax，所以要在 pass1 算 `maxval`、pass2 减去它，自己实现同样的稳定化（u2-l4）。

**练习 2**：把 `MLP.forward`（[train_gpt2.py:97-101](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L97-L101)）的每一行对应到 [train_gpt2.c:869-871](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L869-L871)。

> **参考答案**：`c_fc`→`matmul_forward(l_fch, …4C)`；`gelu`→`gelu_forward(l_fch_gelu, l_fch, B*T*4C)`；`c_proj`→`matmul_forward(l_fcproj, l_fch_gelu, …C)`。

---

### 4.4 configure_optimizers：分组权重衰减

#### 4.4.1 概念说明

`configure_optimizers` 解决一个问题：**哪些参数该被权重衰减（weight decay），哪些不该？**

经验法则：**二维及以上的权重张量（矩阵）要衰减，一维的偏置和 LayerNorm 参数不衰减**。原因是衰减的本质是「向 0 收缩，防过拟合」，对矩阵有意义；而对偏置和 LayerNorm 的 scale/bias 衰减会伤害表达力。

PyTorch 版**显式分组**实现这条规则；而 C 版的 `gpt2_update` **对所有参数一视同仁**。这是两套实现在优化器层面最值得注意的结构性差异。

#### 4.4.2 核心流程

PyTorch 的分组逻辑（伪代码）：

```text
所有参数 → 按 dim 分两组:
  decay_params   = [p for p if p.dim() >= 2]   # 矩阵权重 + embedding → 应用 weight_decay
  nodecay_params = [p for p if p.dim() <  2]   # bias + LayerNorm → weight_decay = 0
两组打包成 optim_groups，交给 torch.optim.AdamW（或 ZeroRedundancyOptimizer）
```

C 版的 `gpt2_update` 则是：

```text
for i in 0..num_parameters:        # 单一循环，所有参数同样处理
    θ[i] -= lr * (m_hat/(√v_hat+ε) + weight_decay * θ[i])
```

差异点：

- **分组 vs 统一**：PyTorch 给不同组不同的 `weight_decay`；C 版所有参数用同一个 `weight_decay`。
- **当前默认值掩盖了差异**：C 版 main 调用 `gpt2_update(..., 0.0f, …)`（`weight_decay=0`），PyTorch 默认 `--weight_decay=0.0`，所以**当前默认配置下两版行为一致**（都不衰减）。差异只在「打开衰减」时才显现。
- **β₂ 不同**（重要！）：PyTorch 主循环用 `betas=(0.9, 0.95)`，C 版 main 用 `beta2=0.999`（见 [train_gpt2.c:1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1168)）。学习率都默认 `1e-4`、`beta1` 都 `0.9`，但 `beta2` 两版不一致——这是阅读源码时容易踩坑的点。

> 为什么 `beta2` 不同不影响正确性测试？因为 `test_gpt2.c` 用的是**自己的**超参 `betas=(0.9, 0.999)`、`wd=0.01`（[test_gpt2.c:172](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L172)）和**自己硬编码的** `expected_losses` 曲线（[test_gpt2.c:89](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L89)），自洽闭环，不依赖 PyTorch 主循环的 `beta2`。

#### 4.4.3 源码精读

- [train_gpt2.py:241-270](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L241-L270)：`configure_optimizers` 全文。
- [train_gpt2.py:248-253](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L248-L253)：分组的核心三行——`decay_params`（`dim>=2`）、`nodecay_params`（`dim<2`）、两个 `optim_groups` 分别设 `weight_decay`。
- [train_gpt2.py:262-269](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L262-L269)：根据 `zero_stage` 选 `ZeroRedundancyOptimizer`（ZeRO Stage 1）或普通 `AdamW`，并尽量用 `fused` 版本。ZeRO 的 C 版对应在 u6-l4。
- [train_gpt2.py:711-713](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L711-L713)：调用处。注意 `betas=(0.9, 0.95)`。
- [train_gpt2.c:1007-1033](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1007-L1033)：C 版 `gpt2_update`，单一循环、统一 `weight_decay`。第 1031 行是核心更新公式（AdamW 解耦权重衰减，u3-l2）。
- [train_gpt2.c:1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1168)：C 版 main 的调用，`weight_decay=0.0f`、`beta2=0.999f`。

#### 4.4.4 代码实践

**实践目标**：搞清「如果打开权重衰减，PyTorch 和 C 版行为会不会一样」。

**操作步骤**：

1. 读 [train_gpt2.py:248-253](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L248-L253)，列出会落入 `decay_params` 的参数类型（提示：所有 `nn.Linear` 的 `.weight`、两个 `nn.Embedding.weight`）和落入 `nodecay_params` 的类型（所有 `.bias`、`LayerNorm` 的 weight/bias）。
2. 读 [train_gpt2.c:537-554](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L537-L554)（`ParameterTensors`），数一数其中有多少是一维的 bias/LayerNorm（如 `ln1b`、`qkvb`、`lnfb` 等）。
3. 读 [train_gpt2.c:1031](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1031)，确认 C 版对这些一维参数**也**会施加 `weight_decay * param`。

**预期结果**：你能得出结论——**若把 C 版的 `weight_decay` 调成非 0，它会错误地衰减 bias 和 LayerNorm**，这与 PyTorch 的分组语义不一致。因此 C 版 main 才默认传 `0.0`，避免这个不一致暴露。

> 待本地验证：这一步只需源码阅读，无需运行。若你想动手，可在 C 版把 `weight_decay` 改成 `0.01`、跑几步，观察 LayerNorm 参数是否被收缩（理论上会）。

#### 4.4.5 小练习与答案

**练习 1**：`configure_optimizers` 里 `p.dim() >= 2` 的判断，对 `lm_head`（= `wte`）会判成 decay 还是 no-decay？

> **参考答案**：decay。`wte` 形状 `(V, C)`，`dim()==2`。注意因为权重绑定，`wte` 和 `lm_head` 是同一个张量，只会被计入一次。

**练习 2**：为什么 C 版不实现「分组」也不会出大问题？

> **参考答案**：因为 C 版 main 默认 `weight_decay=0`，分组与否结果相同。分组只在「想衰减矩阵但不衰减 bias」时才必要，而 C 版当前没启用这个用法。

---

### 4.5 训练循环对照与超参差异

#### 4.5.1 概念说明

最后把视角拉到「整个训练循环」。两版的训练步都是「前向 → 反向 → 优化」三段，但 PyTorch 版多了 **梯度累积（gradient accumulation）**、**DDP 多卡**、**学习率调度**、**梯度裁剪**等工程特性，而 C 版 main 是单卡、单 batch 的极简版本（这些高级特性主要在 `train_gpt2.cu` 里，见 u6）。

本模块的目标不是逐行讲 PyTorch 训练循环（那是另一个话题），而是**对照**出「C 版四步循环对应 PyTorch 的哪几行」。

#### 4.5.2 核心流程

```text
PyTorch 训练步（train_gpt2.py:791-829）              C 训练步（train_gpt2.c main，约 1155-1168）
─────────────────────────────────────────────        ──────────────────────────────────────
optimizer.zero_grad(set_to_none=True)                gpt2_zero_grad(&model)              ← 清零
for micro_step in range(grad_accum_steps):           （C 版 main 无梯度累积，单 batch）
    _, loss = model(x, y)                            gpt2_forward(&model, inputs, targets, B, T)  ← 前向
    loss = loss / grad_accum_steps
    loss.backward()                                  gpt2_backward(&model)              ← 反向
clip_grad_norm_(model.parameters(), grad_clip)       （C 版 main 无显式裁剪）
optimizer.step()                                     gpt2_update(&model, lr, b1, b2, …) ← 优化
```

要点：

- **梯度累积**：PyTorch 把 `loss / grad_accum_steps`，再在多个 micro-step 上累加 `backward()`，使**总 batch = `B*T*world_size*grad_accum_steps`**。C 版 main 没有这层（多卡/累积在 `train_gpt2.cu`，u6-l4）。
- **学习率调度**：PyTorch 用 `get_lr(it)`（cosine + warmup）每步改写 `param_group['lr']`。C 版 main 固定 `lr=1e-4`（调度在 `train_gpt2.cu` + `llmc/schedulers.h`，u6-l3）。
- **梯度裁剪**：PyTorch 用 `clip_grad_norm_`；C 版 main 没有（`global_norm` 裁剪在 `train_gpt2.cu`，u6-l2）。
- **生成**：PyTorch 用 `GPT.generate`（[train_gpt2.py:272-297](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L272-L297)），用 `torch.multinomial` 采样、支持 `temperature` 和 `top_k`；C 版对应 u3-l3 的 `sample_mult` + 自回归循环（无 top_k）。

#### 4.5.3 源码精读

- [train_gpt2.py:791-829](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L791-L829)：PyTorch 训练段。第 793 行清零，第 799–819 行是 micro-batch 循环（前向 + 反向），第 823 行梯度裁剪，第 825–827 行设置学习率，第 829 行 `optimizer.step()`。
- [train_gpt2.py:807-815](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L807-L815)：梯度累积的关键——只在最后一个 micro-step 让 DDP 同步梯度，并把 loss 除以 `grad_accum_steps` 以把「求和」变成「求平均」。
- [train_gpt2.py:716-728](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L716-L728)：`get_lr`，cosine 衰减 + linear warmup。C 版的等价物是 `llmc/schedulers.h`（u6-l3）。
- [train_gpt2.c:1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1168)：C 版 main 的优化步调用——一行完成 PyTorch 第 829 行 `optimizer.step()` 的工作。

#### 4.5.4 代码实践

**实践目标**：把「PyTorch 一个训练步」压缩成「C 版四行」。

**操作步骤**：

1. 读 [train_gpt2.py:791-829](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L791-L829)，圈出哪几行对应「清零、前向、反向、优化」。
2. 在 [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) 的 `main` 里定位 `gpt2_zero_grad`、`gpt2_forward`、`gpt2_backward`、`gpt2_update` 四次调用（提示：`gpt2_update` 在第 1168 行）。
3. 列一个三列对照表：**阶段 / PyTorch 行号 / C 版函数**。

**预期结果**：你发现 PyTorch 用 ~40 行（含累积、裁剪、调度、DDP）做的事，C 版 main 用 4 行（无累积、无裁剪、固定 lr、单卡）做最朴素版本；而被「省略」的那些工程特性，正是后续 `train_gpt2.cu` 单元（u6）的主题。

#### 4.5.5 小练习与答案

**练习 1**：PyTorch 训练循环里 `loss = loss / grad_accum_steps` 这一步，C 版 main 有对应吗？

> **参考答案**：没有。C 版 main 不做梯度累积，`grad_accum_steps` 隐式为 1，所以不需要缩放。C 版的累积逻辑在 `train_gpt2.cu`（u6-l4）。

**练习 2**：`GPT.generate`（[train_gpt2.py:272-297](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L272-L297)）和 C 版生成（u3-l3）最大的共同点是什么？

> **参考答案**：都是**自回归**——每生成一个 token 就把整段序列重新喂进前向，取最后一位的分布采样下一个 token。PyTorch 用 `torch.multinomial`，C 版用 `sample_mult`；PyTorch 额外支持 `top_k` 和 `temperature`。

## 5. 综合实践

把本讲所有对照串起来，做一份**「PyTorch→C 翻译清单」**：

1. 给定 PyTorch 一行 `x = x + self.attn(self.ln_1(x))`（[train_gpt2.py:113](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L113)），写出它在 C 版 `gpt2_forward` 中展开后的**全部函数调用**（参考 [train_gpt2.c:863-867](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L863-L867)）。
2. 对你写出的每个 C 函数，标注它在**反向**时的对应：是手写 `*_backward`，还是属于 autograd 自动完成的部分。
3. 写出这一行涉及的两个残差连接点（`residual2`、`residual3`）在反向时如何合流（联系 u3-l1 的「残差梯度合流」）。
4. 最后用一句话总结：**PyTorch 的 `loss.backward()` 自动完成了 C 版的多少行手写代码？**

完成这份清单后，你就真正「看穿」了 autograd 在 GPT-2 训练里到底替你做了什么。

## 6. 本讲小结

- `train_gpt2.py` 在项目中是**算法参考、正确性标尺、`.bin` 权重生产者**三合一；`write_state` 在优化器介入前导出 debug state，故标准答案与 `lr/wd/betas` 无关。
- `GPTConfig` 与 `GPT2Config` 字段一一对应，唯一差别是 PyTorch 版没有 `padded_vocab_size`（填充发生在导出边界）。
- PyTorch 的 `Block.forward` 两行 = C 版层循环体 10 行函数调用；前向两边都要写，反向只有 C 版要手写。
- 注意力的因果 mask：PyTorch 用 `masked_fill(-inf)`，C 版用循环上界 `t2<=t` 实现。
- `configure_optimizers` 按 `dim` **分组**设权重衰减（矩阵衰减、bias/LayerNorm 不衰减）；C 版 `gpt2_update` 对所有参数统一处理，靠默认 `wd=0` 掩盖差异。
- 两版 `beta2` 不同：PyTorch 主循环 `0.95`、C 版 main `0.999`；但 `test_gpt2.c` 自带超参与 `expected_losses`，不受影响。

## 7. 下一步学习建议

- 想深入 `.bin` 权重格式与 `write_model` 的逐张量写出顺序，继续读 **u4-l2（权重 .bin 二进制协议与 checkpoint）**。
- 想看 CUDA 版如何把这套 PyTorch/C 参考搬到 GPU 上，进入 **u5 单元（CUDA 主线 train_gpt2.cu 与 llmc 头文件库）**，从 u5-l1 开始。
- 想理解混合精度、master weights、TF32 这些 PyTorch 训练循环里 `--dtype`、`--tensorcores` 对应的概念，看 **u6-l1（混合精度、master weights 与 TF32）**。
- 复习反向组装建议重读 **u3-l1（gpt2_backward）**，本讲的「autograd == C 的 *_backward」正是建立在那一讲的基础上。
