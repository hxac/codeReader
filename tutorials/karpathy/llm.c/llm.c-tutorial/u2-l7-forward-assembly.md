# 前向组装：gpt2_forward 串起整个模型

## 1. 本讲目标

本讲是「前向各层」单元的收束篇。前面六讲（u2-l1 ~ u2-l6）我们分别学会了 GPT-2 的每一块积木：encoder、LayerNorm、MatMul、Attention、GELU、残差、Softmax、CrossEntropy。但这些积木单独放着不会自动变成一个会「读句子、算损失」的模型。

本讲的目标是：

- 把上一讲学过的单个算子，按 GPT-2 的 **pre-norm Transformer 块顺序**串成一个完整的 12 层前向网络。
- 理解贯穿整个模型的**残差流（residual stream）**：数据如何在层与层之间流动，为什么需要 `residual2` 和 `residual3` 两个缓冲区，以及它们的层索引 `l` 偏移是如何计算的。
- 看懂前向的最后一步：最终 LayerNorm `lnf`、logits 投影、softmax、crossentropy，以及标量 `mean_loss` 是怎么得到的。

学完本讲，你应当能画出 GPT-2 一个 Transformer block 的完整前向数据流图，并能解释 `gpt2_forward` 里几乎每一行调用的作用。

## 2. 前置知识

本讲默认你已经读过前置六讲，熟悉以下概念（这里只做一句话回顾）：

- **encoder**：把 (B,T) 整数 token id 查 `wte`/`wpe` 表相加，得到 (B,T,C) 的嵌入，即残差流的第 0 层。
- **LayerNorm**：对每个位置的 C 维向量做「去均值、除标准差、再缩放平移」，缓存 `mean`/`rstd` 供反向用。
- **MatMul**：线性层 `out = inp @ weight^T + bias`。
- **Attention**：多头因果自注意力，输出 `atty(B,T,C)`。
- **GELU / 残差**：MLP 里的非线性；残差连接是逐元素相加，反向做梯度分流。

本讲会频繁用到 GPT-2 的尺寸缩写，再次列出以便对照（GPT-2 124M 的取值）：

| 缩写 | 含义 | 124M 取值 |
|------|------|-----------|
| B | batch size | 运行时（如 4） |
| T | sequence length | 运行时（如 64 或 1024） |
| C | channels（特征维） | 768 |
| V | vocab_size（真实词表） | 50257 |
| Vp | padded_vocab_size（对齐到 128） | 50304 |
| NH | num_heads | 12 |
| L | num_layers | 12 |

还需要的两个工程概念（来自 u1-l3）：

- **一次性 malloc + 指针排布**：把一整块内存切成多个张量，用「基地址 + 偏移」寻址，避免多次分配。
- **懒分配**：参数在 `gpt2_build_from_checkpoint` 时分配，而激活、梯度因为依赖运行时的 B、T，推迟到首次前向时才分配。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| `train_gpt2.c` | CPU fp32 参考实现，本讲的全部前向组装代码都在这里 |

具体涉及 `train_gpt2.c` 的以下代码点：

- `ActivationTensors` 结构体：23 个激活缓冲的声明（含 `residual2`/`residual3`）。
- `fill_in_activation_sizes` / `malloc_and_point_activations`：激活缓冲的尺寸与一次性分配。
- `gpt2_forward`：本讲的绝对主角，把所有层组装起来。
- `main` 中的训练四行：调用 `gpt2_forward` 的上下文。

## 4. 核心概念与源码讲解

### 4.1 pre-norm Transformer 块的前向顺序

#### 4.1.1 概念说明

GPT-2 是一个 **decoder-only Transformer**，由 L 个结构完全相同的「Transformer block」堆叠而成（124M 中 L=12）。每个 block 内部又分成两个子层：

1. **因果自注意力子层**：让序列中不同位置相互「看见」（受因果约束，只能看过去）。
2. **MLP（前馈）子层**：对每个位置独立地做一次「升维 → 非线性 → 降维」。

GPT-2 用的是 **pre-norm**（前置归一化）结构：LayerNorm 放在每个子层的**最前面**，而不是最后面。这样残差主路（residual stream）上传递的是**未归一化**的、干净的累加结果，有利于深层网络训练稳定。

一句话直觉：残差流像一条「高速公路」，每个 block 往这条路上加两笔「修正」（一笔来自注意力、一笔来自 MLP），而 LayerNorm 只是对进入子层前的支路做归一化，不污染高速公路本身。

#### 4.1.2 核心流程

一个 block 的前向顺序（pre-norm）如下，输入是上一层的残差 `residual`：

```
# —— 注意力子层 ——
ln1   = LayerNorm(residual)            # 归一化后送入注意力
qkv   = ln1 @ qkvw^T + qkvb            # 投影出 Q/K/V
atty  = Attention(qkv)                 # 因果自注意力
attproj = atty @ attprojw^T + attprojb # 输出投影
residual2 = residual + attproj         # ← 第一笔修正，写回残差流

# —— MLP 子层 ——
ln2    = LayerNorm(residual2)          # 对更新后的残差再归一化
fch    = ln2 @ fcw^T + fcb             # 升维 C → 4C
fch_gelu = GELU(fch)                   # 非线性
fcproj = fch_gelu @ fcprojw^T + fcprojb # 降维 4C → C
residual3 = residual2 + fcproj         # ← 第二笔修正，写回残差流
```

整个 block 的数据流可以画成：

```
residual ──┬──► LayerNorm(ln1) ──► QKV ──► Attention ──► AttProj ──┐
           │                                                       ▼
           └──────────────────────────────────────────────► (+) ──► residual2
                                                               │
                                                               ├──► LayerNorm(ln2) ──► FC ──► GELU ──► FCProj ──┐
                                                               │                                                 ▼
                                                               └─────────────────────────────────────────► (+) ──► residual3
```

注意两个残差相加节点 `(+)`：`residual2 = residual + attproj`，`residual3 = residual2 + fcproj`。这就是为什么 block 内部需要**两个**残差缓冲（`residual2` 和 `residual3`），分别保存注意力之后、MLP 之后的状态。

#### 4.1.3 源码精读

`gpt2_forward` 把上面的流程逐行写成 C 代码。先看 block 的核心前向调用（共 10 行，对应 4.1.2 的 10 步）：

[decoder的前向组装: train_gpt2.c#L862-L872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L862-L872)

```c
layernorm_forward(l_ln1, l_ln1_mean, l_ln1_rstd, residual, l_ln1w, l_ln1b, B, T, C);
matmul_forward(l_qkv, l_ln1, l_qkvw, l_qkvb, B, T, C, 3*C);
attention_forward(l_atty, l_preatt, l_att, l_qkv, B, T, C, NH);
matmul_forward(l_attproj, l_atty, l_attprojw, l_attprojb, B, T, C, C);
residual_forward(l_residual2, residual, l_attproj, B*T*C);
layernorm_forward(l_ln2, l_ln2_mean, l_ln2_rstd, l_residual2, l_ln2w, l_ln2b, B, T, C);
matmul_forward(l_fch, l_ln2, l_fcw, l_fcb, B, T, C, 4*C);
gelu_forward(l_fch_gelu, l_fch, B*T*4*C);
matmul_forward(l_fcproj, l_fch_gelu, l_fcprojw, l_fcprojb, B, T, 4*C, C);
residual_forward(l_residual3, l_residual2, l_fcproj, B*T*C);
```

这 10 行和 4.1.2 的伪代码**一一对应**，阅读时注意几个规律：

- 每行的第一个参数几乎都是**输出**缓冲（`l_ln1`、`l_qkv`、`l_atty`、…），中间参数是**输入**，最后是一串尺寸（`B, T, C, ...`）。这是 train_gpt2.c 全局统一的函数签名风格。
- `matmul_forward` 的最后一个参数是**输出通道数 OC**：`qkv` 是 `3*C`（要切出 Q/K/V 三份），`attproj` 是 `C`，`fch` 是 `4*C`（升维），`fcproj` 是 `C`（降维）。对照 u2-l3 的 MatMul，OC 决定权重的行数。
- `residual_forward(out, in1, in2, N)` 就是逐元素 `out[i] = in1[i] + in2[i]`，`N = B*T*C` 是元素总数。

每个 block 前还有一段「为当前层 `l` 取出本层专用的权重和激活指针」，我们放到 4.2 详细讲，这里先看它出现在循环里：

[block 循环骨架: train_gpt2.c#L826-L873](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L826-L873)

```c
encoder_forward(acts.encoded, inputs, params.wte, params.wpe, B, T, C); // encoding goes into residual[0]
for (int l = 0; l < L; l++) {
    residual = l == 0 ? acts.encoded : acts.residual3 + (l-1) * B * T * C;
    // ... 取本层权重、激活指针（见 4.2）...
    // ... 10 行前向调用（见上面）...
}
```

> 说明：encoder 在循环**外**调用一次，它的输出 `acts.encoded` 就是第 0 层 block 的输入残差。循环内每个 `l` 处理一个 block。

#### 4.1.4 代码实践

**实践目标**：亲手把一个 block 的「10 行前向调用」与数据流图对应起来，确认 pre-norm 顺序。

**操作步骤**：

1. 打开 `train_gpt2.c`，定位到 862–872 行（即 4.1.3 的链接）。
2. 准备一张纸，画出 4.1.2 的数据流图骨架（两个 `(+)` 节点、两条支路）。
3. 逐行把这 10 行调用**标到图上**的对应箭头上，例如：
   - 第 1 行 `layernorm_forward(... residual ...)` → 标在 `residual → ln1` 那条边上；
   - 第 5 行 `residual_forward(l_residual2, residual, l_attproj, ...)` → 标在第一个 `(+)` 节点上。
4. 检查：是否每一条边都恰好对应一个 `*_forward` 调用？

**需要观察的现象**：你会确认这 10 行调用**严格按**「归一化 → 投影 → 注意力 → 投影 → 残差 → 归一化 → 投影 → 激活 → 投影 → 残差」的顺序排列，且两个 `residual_forward` 分别落在注意力子层和 MLP 子层的末尾。

**预期结果**：图中每个箭头都有唯一对应的 C 代码行，顺序与 pre-norm 定义一致。这一步不需要运行任何程序，是纯源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 1 行 `layernorm_forward(l_ln1, ..., residual, ...)` 改成对 `residual2` 做 LayerNorm（即变成 post-norm），整个 block 还是不是 GPT-2 的结构？为什么？

**答案**：不再是。这样 LayerNorm 会跑到注意力子层的**后面**，残差主路上传递的就不是「干净累加」而是归一化后的结果，属于 post-norm 结构，和 GPT-2 的 pre-norm 不同，训练稳定性会变差。

**练习 2**：`attention_forward` 这一行并没有把 `residual` 或 `attproj` 当输入，它的输入只有 `l_qkv`。这和残差连接矛盾吗？

**答案**：不矛盾。注意力**子层本身**的计算（QKV 打分、加权求和）只依赖 `qkv`；残差连接是在子层**外部**由第 5 行 `residual_forward` 单独完成的。注意力算的是「修正量」，残差负责把它「加回」主路。

---

### 4.2 残差流与层间激活寻址

#### 4.2.1 概念说明

这是本讲最容易绕晕、也最值得弄懂的部分。问题可以这样提出：

> L 个 block 堆叠，每个 block 内部又有 `ln1, qkv, atty, preatt, att, attproj, residual2, ln2, fch, fch_gelu, fcproj, residual3` 等 12 个激活缓冲。124M 模型有 12 层，那难道要声明 12×12=144 个缓冲指针吗？

答案是：**不**。train_gpt2.c 的做法是——对每个「会随层数 L 变化」的缓冲，只声明**一个指针**，但按 `L` 份连续布局，再用「基地址 + 层偏移」寻址。这正是 u1-l3 讲过的「一次性 malloc + 指针排布」技巧在**层这个维度**上的应用。

理解这一点要抓住三个关键事实：

1. **`residual2` 和 `residual3` 是两个独立的、各自有 L 份的缓冲**，分别保存「注意力之后的残差」和「MLP 之后的残差」。它们在 `ActivationTensors` 里各占一个指针，但实际内存是 `L*B*T*C` 大小。
2. **block 的输入残差**对第 0 层是 `acts.encoded`，对第 `l≥1` 层是「上一层的 `residual3`」，即 `acts.residual3 + (l-1)*B*T*C`。
3. **block 内部的 `residual2` / `residual3`** 属于**当前层** `l`，寻址为 `acts.residual2 + l*B*T*C` 和 `acts.residual3 + l*B*T*C`。

之所以需要把 `residual2`、`residual3`（以及 `ln1`、`qkv` 等所有层相关缓冲）都完整保存 L 份，是因为**反向传播**要复用每一层的前向激活（例如 `attention_backward` 要读 `att`、`matmul_backward` 要读 `l_ln1`/`l_ln2` 等）。前向算完不能丢，否则反传就算不出梯度。

#### 4.2.2 核心流程

层相关缓冲的寻址公式可以总结为一个表格。设某缓冲每层大小为 `E`（例如 `residual2` 每层 `E = B*T*C`，`qkv` 每层 `E = B*T*3*C`）：

| 量 | 含义 | 寻址公式 |
|----|------|----------|
| `acts.<buf>` | 整个缓冲的基地址（L 份连在一起） | — |
| 第 `l` 层的 `l_<buf>` | 本层这一份的起点 | `acts.<buf> + l * E` |

对 `residual2`、`residual3`（`E = B*T*C`）：

- 第 `l` 层的 `residual2` 起点：`acts.residual2 + l*B*T*C`
- 第 `l` 层的 `residual3` 起点：`acts.residual3 + l*B*T*C`

而 **block 的输入残差**是特殊的：它不是某个缓冲的「第 l 层」，而是要跨 `residual3` 取「上一层」：

\[
\text{input\_residual}(l) =
\begin{cases}
\text{acts.encoded} & l = 0 \\
\text{acts.residual3} + (l-1)\cdot B \cdot T \cdot C & l \geq 1
\end{cases}
\]

对应的，最后一个 block 输出的残差（喂给最终 `lnf`）是第 L-1 层的 `residual3`：

\[
\text{final\_residual} = \text{acts.residual3} + (L-1)\cdot B \cdot T \cdot C
\]

`residual3` 与 `residual2` 的关系（block 内部）：

\[
\text{residual3}[l] = \text{residual2}[l] + \text{fcproj}[l]
\]
\[
\text{residual2}[l] = \text{input\_residual}(l) + \text{attproj}[l]
\]

把两条合起来，每个 block 对残差流的净贡献是 `attproj[l] + fcproj[l]`，这正是「高速公路上两笔修正」。

#### 4.2.3 源码精读

先看 `ActivationTensors` 里 `residual2` 和 `residual3` 的声明，注意它们的注释标注的形状是 `(L, B, T, C)`，即 **L 份**连排：

[ActivationTensors 中 residual2/residual3 的声明: train_gpt2.c#L612-L619](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L612-L619)

```c
float* residual2; // (L, B, T, C)
float* ln2; // (L, B, T, C)
...
float* fcproj; // (L, B, T, C)
float* residual3; // (L, B, T, C)
```

再看 `fill_in_activation_sizes` 中它们的尺寸都是 `L * B * T * C`，证实「一整块连续内存容纳 L 份」：

[residual2/residual3 的尺寸定义: train_gpt2.c#L642-L649](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L642-L649)

```c
act_sizes[9] = L * B * T * C; // residual2
...
act_sizes[16] = L * B * T * C; // residual3
```

现在看本讲最关键的一行——**block 输入残差的层偏移计算**：

[block 输入残差的层偏移: train_gpt2.c#L828](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L828)

```c
residual = l == 0 ? acts.encoded : acts.residual3 + (l-1) * B * T * C;
```

这一行实现了 4.2.2 的分段公式：第 0 层用 `encoded`；其他层取「上一层的 `residual3`」，偏移 `(l-1)*B*T*C`。

然后是循环内为当前层 `l` 取出**本层专属**的激活指针（节选与残差相关的三行）：

[本层激活指针设置（含 residual2/residual3）: train_gpt2.c#L853-L860](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L853-L860)

```c
float* l_residual2 = acts.residual2 + l * B * T * C;
...
float* l_residual3 = acts.residual3 + l * B * T * C;
```

对比 4.2.3 上面两段，能看到一个清晰的对照：

- **本层**输出缓冲 `l_residual2`/`l_residual3` 用偏移 `l*B*T*C`（当前层）；
- **输入**残差用偏移 `(l-1)*B*T*C`（上一层），第 0 层特判为 `encoded`。

层相关的**权重**也用同样的「基地址 + 层偏移」方式取，只是权重不带 batch/time 维，偏移按该层权重的元素数算（如 `qkvw` 每层 `3*C*C` 个元素）：

[本层权重指针设置: train_gpt2.c#L831-L842](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L831-L842)

```c
float* l_ln1w = params.ln1w + l * C;
...
float* l_qkvw = params.qkvw + l * 3*C * C;
...
float* l_fcw = params.fcw + l * 4*C * C;
float* l_fcprojw = params.fcprojw + l * C * 4*C;
```

> 提示：注意 `fcw` 每层是 `4*C*C`、`fcprojw` 每层是 `C*4*C`，虽然乘出来数值一样（`4*C*C`），但写法上前者对应「升维权重 (4C, C)」、后者对应「降维权重 (C, 4C)」，偏移写法和权重的逻辑形状一致，便于阅读。

最后看循环结束后，最终残差的取法——取第 `L-1` 层的 `residual3`：

[最终残差取自 residual3[L-1]: train_gpt2.c#L874](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L874)

```c
residual = acts.residual3 + (L-1) * B * T * C; // last residual is in residual3
```

这正是 4.2.2 的 `final_residual` 公式。注释 `// last residual is in residual3` 点明：最后一个 block 的输出存在 `residual3`（而不是 `residual2`），因为 MLP 子层在 block 末尾。

#### 4.2.4 代码实践

**实践目标**：画出 `residual` 在 12 层间的流动图，并验证 `residual2`/`residual3` 的层偏移公式。

**操作步骤**：

1. 假设 GPT-2 124M：`L=12, B=4, T=64, C=768`，于是每层残差元素数 `B*T*C = 4*64*768 = 196608`。
2. 画一张纵向的「层栈」图，从上到下标 `l=0` 到 `l=11`：
   ```
   encoded (= residual[0])                ← 828 行 l==0 分支
       └─ block0 → residual3 + 0*196608
              └─ block1 → residual2 + 1*196608, residual3 + 1*196608
                     ...
                      └─ block11 → residual3 + 11*196608   ← 874 行
   ```
3. 对层 `l=0,1,2,11`，分别在图上标出：
   - 输入残差的地址（`encoded` 或 `residual3 + (l-1)*196608`）；
   - 本层 `l_residual2` 地址（`residual2 + l*196608`）；
   - 本层 `l_residual3` 地址（`residual3 + l*196608`）。
4. 检查关键链：`l=1` 的输入残差（`residual3 + 0*196608`）是否正好等于 `l=0` 的 `l_residual3`？`l=2` 的输入残差是否等于 `l=1` 的 `l_residual3`？

**需要观察的现象**：你会确认「第 l 层的输入残差 = 第 l-1 层的 `residual3` 输出」，即层与层之间通过 `residual3` 串联，而 `residual2` 只在 block **内部**出现（MLP 子层要用它）。

**预期结果**：链路验证通过——`input_residual(l) == residual3[l-1]` 对所有 `l≥1` 成立。最后一个 block 的输出 `residual3[11]` 被循环外的 874 行取走，送入最终 `lnf`。本实践为源码阅读型，无需运行程序。

> 拓展思考：为什么不在 block 内部就地用 `residual` 缓冲覆盖、省掉 `residual2`？因为反向传播（下一单元 u3-l1）要同时用到 block 的输入残差和 `residual2`/`residual3`，前向算出的每一份都必须保留，所以这里宁可多存也要可寻址。

#### 4.2.5 小练习与答案

**练习 1**：`acts.residual2 + l*B*T*C` 和 `acts.residual3 + (l-1)*B*T*C`（当 `l≥1`）有可能指向同一块内存吗？为什么这是安全的？

**答案**：不会指向同一块内存。`residual2` 和 `residual3` 是 `malloc_and_point_activations` 里**两个不同**的、各自 `L*B*T*C` 大小的连续缓冲，地址完全不重叠。即使偏移相同，它们也是两个独立数组的不同位置，所以安全。

**练习 2**：如果把循环外的 874 行 `residual = acts.residual3 + (L-1)*B*T*C` 误写成 `+ (L)*B*T*C`，会发生什么？

**答案**：会越界访问——`residual3` 总共只有 L 份（下标 0..L-1），偏移 `L*B*T*C` 跑到了 `residual3` 缓冲的尾部之外，属于越界读，行为未定义（可能读到下一个缓冲 `lnf` 的区域或更后面）。正确下标是 `L-1`。

---

### 4.3 最终 lnf、logits 与 mean_loss

#### 4.3.1 概念说明

L 个 block 跑完后，残差流走到 `residual3[L-1]`，形状 (B,T,C)。但这还不能用来预测下一个词——词表有 Vp 个词，而残差是 C 维。需要最后三步把 C 维残差变成「每个位置对每个词的打分」和「损失」：

1. **最终 LayerNorm（lnf）**：对残差做最后一次归一化（和块内的 ln1/ln2 同样的算子，但只有一份、不分层）。
2. **logits 投影**：用一次 MatMul 把 C 维投影到 Vp 维，得到每个位置对每个词的原始打分 `logits(B,T,Vp)`。这里复用 `wte` 作权重（**权重绑定 weight tying**，见 u2-l1/u2-l3），bias 传 `NULL`。
3. **softmax + crossentropy**：把 logits 归一化成概率 `probs(B,T,Vp)`，再对目标 token 取负对数概率得到 `losses(B,T)`，最后对 B*T 个位置求平均，得到标量 `mean_loss`。

「最终 lnf」不分层（只有一份），体现在结构体里它的形状是 `(B,T,C)` 而不是 `(L,B,T,C)`：

[lnf/logits/probs/losses 均不分层: train_gpt2.c#L620-L625](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L620-L625)

```c
float* lnf; // (B, T, C)
float* lnf_mean; // (B, T)
float* lnf_rstd; // (B, T)
float* logits; // (B, T, V)
float* probs; // (B, T, V)
float* losses; // (B, T)
```

#### 4.3.2 核心流程

最终阶段的前向（循环外）：

```
final_residual = residual3[L-1]            # 874 行
lnf      = LayerNorm(final_residual)       # 用 lnfw/lnfb（不分层）
logits   = lnf @ wte^T  (+ 无 bias)        # 投影到词表维 Vp
probs    = softmax(logits)                 # 数值稳定 softmax（真实 V 内减最大值）
losses   = crossentropy(probs, targets)    # 每个位置 -log(probs[target])
mean_loss = mean(losses)                   # 标量，对所有 B*T 位置平均
```

关于 `mean_loss` 的两点约定：

- 若调用 `gpt2_forward` 时传入了 `targets`，则计算并填入 `model->mean_loss`，这是训练和验证都要用的损失值。
- 若 `targets == NULL`（例如纯生成场景），则不计算损失，`model->mean_loss` 置为 `-1.0f` 作为哨兵值（表示「没有损失」）。这种区分让同一个 `gpt2_forward` 既能用于训练（算损失），又能用于生成（只要 logits/probs，不算损失）。

#### 4.3.3 源码精读

最终阶段四行核心调用：

[循环外的最终前向: train_gpt2.c#L874-L877](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L874-L877)

```c
residual = acts.residual3 + (L-1) * B * T * C; // last residual is in residual3
layernorm_forward(acts.lnf, acts.lnf_mean, acts.lnf_rstd, residual, params.lnfw, params.lnfb, B, T, C);
matmul_forward(acts.logits, acts.lnf, params.wte, NULL, B, T, C, Vp);
softmax_forward(acts.probs, acts.logits, B, T, V, Vp);
```

逐行说明：

- 第 1 行：取最后一个 block 的输出残差作为输入。
- 第 2 行：`layernorm_forward`，权重 `params.lnfw`/`params.lnfb` 是**不分层**的单份参数（在 `ParameterTensors` 中形状就是 `(C)`，见 u2-l3 中 `lnfw/lnfb`）。
- 第 3 行：`matmul_forward(acts.logits, acts.lnf, params.wte, NULL, ...)`，把 `wte(Vp,C)` 当权重矩阵，**bias 传 `NULL`**（权重绑定，无分类偏置）。输出通道数是 `Vp`。注意这里 `wte` 同时是 encoder 的查表（u2-l1）和这里的输出投影——这就是「权重绑定」在源码里的直接体现。
- 第 4 行：`softmax_forward` 同时接收 `V` 和 `Vp`（对照 u2-l6），只在真实词表 `[0,V)` 上算 softmax，填充区 `[V,Vp)` 清零。

接下来是根据是否有 `targets` 决定是否算损失：

[mean_loss 的计算与哨兵值: train_gpt2.c#L879-L890](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L879-L890)

```c
if (targets != NULL) {
    crossentropy_forward(model->acts.losses, model->acts.probs, targets, B, T, Vp);
    float mean_loss = 0.0f;
    for (int i=0; i<B*T; i++) { mean_loss += model->acts.losses[i]; }
    mean_loss /= B*T;
    model->mean_loss = mean_loss;
} else {
    model->mean_loss = -1.0f;
}
```

要点：

- `crossentropy_forward` 先算出每个位置（共 B*T 个）的损失 `losses[i]`（即 `-log(probs[i][target_i])`）。
- 再用一个简单循环把这些标量求和并除以 `B*T`，得到 `mean_loss`。这就是训练时反复看到的那个 loss 数值（从约 5.3 开始随训练下降）。
- `else` 分支把 `mean_loss` 置为 `-1.0f`，与 u3-l3 采样生成时 `targets` 传 `NULL` 的用法呼应（见下面 4.3.4 的 main 调用）。

为了体会 `targets` 是否为 `NULL` 的两种用法，看 `main` 里两处对 `gpt2_forward` 的调用：

[训练步与生成步分别调用 gpt2_forward: train_gpt2.c#L1165-L1165](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1165-L1165)

```c
gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);   // 训练：targets 非空，算 loss
```

以及生成时（u3-l3 会详讲）：

```c
gpt2_forward(&model, gen_tokens, NULL, B, T);   // 生成：targets=NULL，不算 loss，只要 probs
```

> 说明：同一个 `gpt2_forward` 通过 `targets` 是否为 `NULL` 同时服务训练和生成两种场景，这正是源码精炼之处。

#### 4.3.4 代码实践

**实践目标**：追踪一个 batch 走完整个 `gpt2_forward`，确认最终得到一个标量 `mean_loss`。

**操作步骤**：

1. 打开 `train_gpt2.c`，从 `gpt2_forward` 的签名 [train_gpt2.c#L765](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L765) 开始。
2. 用笔按顺序列出函数从头到尾的「阶段」，并标注每段对应的行号区间：
   - 校验 inputs/targets 范围（L781–L787）
   - 懒分配激活 + 缓存 inputs/targets（L789–L819）
   - encoder（L825）
   - L 个 block 循环（L826–L873）
   - 最终 lnf/logits/softmax（L874–L877）
   - crossentropy + mean_loss（L879–L890）
3. 想象 `B=4, T=64`：算出 `mean_loss` 是对 `4*64=256` 个位置的损失求平均。
4. （可选，待本地验证）按 u1-l2 的方法编译运行：`make train_gpt2` 然后 `OMP_NUM_THREADS=4 ./train_gpt2`，观察每步打印的 loss。

**需要观察的现象**：源码阅读层面，你会确认「logits 的最后一个 matmul 复用了 `wte`、且 bias 为 `NULL`」「`mean_loss` 来自对 `B*T` 个 `losses` 求平均」「没有 targets 时返回 -1.0f」。运行层面（若执行），loss 从约 5.3 起逐步下降。

**预期结果**：能完整复述 `gpt2_forward` 的六个阶段，并解释 `mean_loss` 的数值含义。若无法本地运行，明确记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：最终 logits 投影那行 `matmul_forward(acts.logits, acts.lnf, params.wte, NULL, ...)`，为什么第 4 个参数（bias）是 `NULL`？对照 `matmul_forward` 的实现，传 `NULL` 会怎样？

**答案**：因为 GPT-2 的分类头与 token embedding 共享权重（weight tying），没有独立的分类偏置。对照 u2-l3 的 `matmul_forward`，当 bias 为 `NULL` 时偏置累加那段会被跳过（`if (bias != NULL)`），即只做 `out = inp @ wte^T`。

**练习 2**：假设某次前向 `B=1, T=1, V=50257, Vp=50304`，且目标 token 处的概率恰好是 `probs[0]=0.1`，那么该位置的 `losses[0]` 是多少？若这是 batch 里唯一的位置，`mean_loss` 是多少？

**答案**：`losses[0] = -log(0.1) ≈ 2.3026`。由于只有一个位置，`mean_loss = losses[0]/1 ≈ 2.3026`。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，徒手画出 GPT-2 整个前向（从 token id 到 `mean_loss`）的端到端数据流图，并在图上标注所有激活缓冲的形状与寻址偏移。

具体要求：

1. 画一条从上到下的「主干」，依次经过：`inputs(B,T)` → `encoder` → `encoded` → **block 0..11** → `residual3[11]` → `lnf` → `logits` → `softmax` → `probs` → `crossentropy` → `losses` → `mean_loss`。
2. 把 **block l** 内部展开成 4.1.2 的小图，标出 `residual(输入) → ln1 → qkv → atty → attproj → (+) → residual2 → ln2 → fch → fch_gelu → fcproj → (+) → residual3`。
3. 在 `block 0` 和 `block 1` 的交界处，标出：`block 1` 的输入残差 = `acts.residual3 + 0*B*T*C`（即 `block 0` 的 `l_residual3`）。
4. 在最终阶段标出：`final_residual = acts.residual3 + (L-1)*B*T*C`，以及 logits 投影复用 `wte`、bias 为 `NULL`。
5. 用一句话说明：为什么整个前向过程中，所有层相关激活（含 `residual2`/`residual3`）都必须完整保存而不能复用同一块内存。

**预期产出**：一张完整、自洽的数据流图，以及一句关于「反向传播需要复用前向激活」的解释。这是把 u2-l1~u2-l6 单个算子和本讲的组装知识融会贯通的关键练习，也为下一单元 u3-l1（反向组装 `gpt2_backward`）打下基础——你会看到反向几乎就是这张图的镜像。

## 6. 本讲小结

- `gpt2_forward` 把前面六讲的单个算子，按 **pre-norm Transformer 块顺序**（ln1→qkv→attention→attproj→残差→ln2→fc→gelu→fcproj→残差）组装成 12 层前向。
- 每个 block 内部有**两个**残差缓冲：`residual2`（注意力子层后）和 `residual3`（MLP 子层后），各保存 L 份，靠 `l*B*T*C` 偏移寻址。
- block 的**输入残差**对第 0 层是 `acts.encoded`，对第 l≥1 层是 `acts.residual3 + (l-1)*B*T*C`（即上一层的输出）。
- 层相关缓冲（含 `residual2`/`residual3`、`ln1`、`qkv` 等）都按 `(L,...)` 连续布局、用「基地址 + 层偏移」寻址，是 u1-l3「一次性 malloc + 指针排布」技巧在层维度上的应用。
- 循环外做最终三步：`lnf`（不分层）→ logits 投影（复用 `wte`、bias 为 `NULL`，即权重绑定）→ softmax；再由 `crossentropy` 与对 `B*T` 求平均得到标量 `mean_loss`。
- `gpt2_forward` 通过 `targets` 是否为 `NULL` 同时服务训练（算 loss）和生成（不算 loss、置 -1.0f 哨兵）两种场景。

## 7. 下一步学习建议

- 进入 **Unit 3 反向传播、优化与生成**，先读 **u3-l1 反向组装 gpt2_backward**：你会发现反向几乎就是本讲前向数据流图的**镜像**——从 `dlogits` 出发，按相反顺序、相反的残差分流把梯度一路传回 `dwte/dwpe`，并且复用本讲保存的所有层激活。
- 之后 **u3-l2 AdamW** 讲怎么用这些梯度更新参数，**u3-l3 采样生成** 讲 `targets=NULL` 那条生成分支的细节，**u3-l4 正确性测试** 讲如何用 `debug_state.bin` 验证本讲的 `mean_loss` 数值正确。
- 建议继续精读 `train_gpt2.c` 中 `gpt2_forward`（L765–L891）与 `gpt2_backward`（L898 起）的对照，体会「前向存激活、反向用激活」这对称设计。
