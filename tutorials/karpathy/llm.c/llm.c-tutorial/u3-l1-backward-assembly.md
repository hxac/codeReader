# 反向组装：gpt2_backward

## 1. 本讲目标

前向单元（u2）已经把每一层算子的前向实现讲透了，并在 u2-l7 里用 `gpt2_forward` 把它们串成了一条完整的 12 层前向链路。本讲要做的事，是把这条链路**反过来走一遍**。

读完后你应该能够：

1. 说清楚 `gpt2_backward` 为什么必须**严格按前向的逆序**调用各层的 `*_backward`，以及这个「镜像」关系体现在代码的哪些行。
2. 理解**残差梯度的合流**：残差连接在反向时，为什么残差缓冲区的梯度来自「主流直通」和「子层回流」两路之和，以及为什么这两路都用 `+=` 累加。
3. 解释 `gpt2_zero_grad` 在每个训练步里不可省略的原因——它和 `+=` 累加、权重绑定（weight tying）是如何绑在一起的。
4. 看懂反向链路是如何被「点燃」的：从 `1/(B*T)` 填充 `dlosses` 开始，到 `encoder_backward` 收尾写回 `dwte/dwpe`。

本讲只讲**组装**（assembly），即各层 `*_backward` 的调用顺序与残差/梯度的接力方式，不重复各算子反向的内部数学推导（那已经在 u2 各讲里完成）。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（它们在前置讲义中已建立）：

- **前向组装**（u2-l7）：`gpt2_forward` 里每个 Transformer block 的前向顺序是 `ln1 → qkv → attention → attproj → residual2 → ln2 → fch → gelu → fcproj → residual3`，并最终经 `lnf → logits → softmax → crossentropy` 得到 `mean_loss`。残差主流用 `residual2`/`residual3` 两个按层偏移寻址的缓冲区传递。
- **`+=` 累加约定**（贯穿 u2）：所有 `*_backward` 写梯度时都用 `+=` 而非 `=`，因为同一个张量会被多处读取（如 `wte`），其梯度必须累加而非覆盖。
- **权重绑定**（u2-l1、u2-l7）：最终的 logits 投影复用词嵌入表 `wte` 作权重，`bias` 传 `NULL`。因此 `wte` 同时是「输入查表」和「输出投影」的权重。
- **融合反向 `crossentropy_softmax_backward`**（u2-l6）：把交叉熵与 softmax 两步求导融合，得招牌结论 `dlogits = (probs - onehot)/N`，反向链路正是从这里出发。

本讲引入的关键术语：

- **反向链路的「点燃」（kickoff）**：反向传播需要一个起点梯度。在 llm.c 里，这个起点是把最终标量 `mean_loss` 对每个位置损失的梯度 `1/(B*T)` 填进 `grads_acts.losses`。
- **残差梯度的合流**：在残差加法节点，下游传回的梯度与子层（attention/MLP）回流的梯度在此汇合相加。

## 3. 本讲源码地图

本讲几乎只依赖一个文件：

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.c` | CPU 参考实现。本讲精读其中的 `gpt2_backward`、`gpt2_zero_grad`，并对照 `gpt2_forward` 与 `main` 中的训练步。 |

具体涉及的函数与行号：

- `gpt2_forward`（[train_gpt2.c:765-891](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L765-L891)）：前向组装，反向要与之严格镜像。
- `gpt2_zero_grad`（[train_gpt2.c:893-896](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L893-L896)）：每步清零权重梯度与激活梯度。
- `gpt2_backward`（[train_gpt2.c:898-1005](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L898-L1005)）：本讲主角。
- `main` 中的训练步（[train_gpt2.c:1162-1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1162-L1168)）：前向 → 清零 → 反向 → 更新的四行。
- `residual_backward`（[train_gpt2.c:442-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L442-L447)）：残差反向，用 `+=` 把 `dout` 累加到两路输入。
- `encoder_backward`（[train_gpt2.c:60-76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L60-L76)）：反向链路的终点，把梯度累加回 `dwte/dwpe`。
- 激活张量结构体 `ActivationTensors`（[train_gpt2.c:601-626](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L601-L626)）：反向时要复用的前向缓存与对应的梯度缓冲。
- `GPT2` 结构体中的梯度字段（[train_gpt2.c:685-698](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L685-L698)）：`grads`、`grads_memory`、`grads_acts`、`grads_acts_memory`。

---

## 4. 核心概念与源码讲解

### 4.1 反向顺序与残差合流

#### 4.1.1 概念说明

反向传播的核心法则是**链式法则**：若前向是 `y = f(g(x))`，那么 `∂L/∂x = (∂L/∂y) · (∂y/∂g) · (∂g/∂x)`。要把这个乘积算对，**必须按前向相反的顺序**逐层回传——因为某一层要算自己的输入梯度，必须先拿到它输出端的梯度，而那个输出端梯度来自前向中「更靠后」的层。

`gpt2_backward` 的全部工作，就是把 u2 里实现的 10 个 `*_backward`（外加 `crossentropy_softmax_backward`）按 `gpt2_forward` 的严格逆序调用一遍。这件事在概念上很朴素，真正的难点在于**残差连接处的梯度合流**。

回顾前向里每个 block 的两处残差加法（来自 u2-l7）：

\[ \text{residual2}^{(l)} = \text{residual}^{(l)} + \text{attproj}^{(l)} \]
\[ \text{residual3}^{(l)} = \text{residual2}^{(l)} + \text{fcproj}^{(l)} \]

以 `residual2` 为例：它既是第一个残差加法的**输出**（来自 attention 子层之后的加法），又是第二个 LayerNorm（`ln2`）的**输入**（喂给 MLP 子层）。因此损失 `L` 对 `residual2` 的梯度天然有两路：

\[ \frac{\partial L}{\partial \text{residual2}^{(l)}} = \underbrace{\frac{\partial L}{\partial \text{residual3}^{(l)}}}_{\text{主流直通：来自更深层}} + \underbrace{\frac{\partial L}{\partial \text{residual2}^{(l)}}\Big|_{\text{via MLP 子层}}}_{\text{子层回流：经 ln2 倒回来}} \]

第一项里用到了 `residual3 = residual2 + fcproj` 对 `residual2` 的偏导为 1（这就是残差「梯度直通」的数学来源）。这两路梯度在同一个缓冲区 `dl_residual2` 里**相加**，这就是「合流」。

#### 4.1.2 核心流程

`gpt2_backward` 的整体执行顺序可以概括为三段：

```text
1. 点燃链路：
   grads_acts.losses[i] = 1/(B*T)          # mean_loss 对各位置损失的梯度

2. 循环外：先反传「不分层的尾巴」(严格逆序于前向尾巴)
   crossentropy_softmax_backward   # logits 梯度（融合反向）
   matmul_backward   (logits→lnf)  # 顺带写 grads.wte（权重绑定！）
   layernorm_backward(lnf)         # 写入最后一层的 dresidual

3. for l = L-1 downto 0：逐层反传（block 内严格逆序）
   residual_backward  (dl_residual2 += dl_residual3)        # 残差合流·主流
   matmul_backward    (fcproj)
   gelu_backward      (fch_gelu)
   matmul_backward    (fch)
   layernorm_backward (ln2 → dl_residual2 += ...)           # 残差合流·MLP 回流
   residual_backward  (dresidual += dl_residual2)           # 残差合流·主流
   matmul_backward    (attproj)
   attention_backward (qkv)
   matmul_backward    (qkv)
   layernorm_backward (ln1 → dresidual += ...)              # 残差合流·attention 回流

4. 循环外：反传「不分层的头部」
   encoder_backward   # 把 dencoded 累加回 dwte / dwpe（终点）
```

注意第 3 段里，每个 block 的反向把梯度写到「上一层」的残差缓冲（`dresidual` 指向 `residual3[l-1]` 或 `encoded`），从而把链路接力到更浅的层。

#### 4.1.3 源码精读

**点燃链路**（[train_gpt2.c:928-932](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L928-L932)）：前向里 `mean_loss` 是对 `B*T` 个位置损失求平均，所以它的「小小反向」就是把 `1/(B*T)` 撒进每个 `grads_acts.losses`。这是整个链式法则的起点：

```c
float dloss_mean = 1.0f / (B*T);
for (int i = 0; i < B*T; i++) { grads_acts.losses[i] = dloss_mean; }
```

**残差合流的第一处**（[train_gpt2.c:993](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L993) 与 [train_gpt2.c:997](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L997)）：对照 `residual_backward` 的实现（[train_gpt2.c:442-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L442-L447)），它对两路输入都是 `+=`：

```c
void residual_backward(float* dinp1, float* dinp2, float* dout, int N) {
    for (int i = 0; i < N; i++) {
        dinp1[i] += dout[i];
        dinp2[i] += dout[i];
    }
}
```

因此在 block 反向里：

```c
residual_backward(dl_residual2, dl_fcproj, dl_residual3, B*T*C);   // L993: dl_residual2 += dl_residual3
// ... 中间是 fcproj/fch/gelu/ln2 的反向 ...
layernorm_backward(dl_residual2, dl_ln2w, dl_ln2b, dl_ln2, l_residual2, ...); // L997: dl_residual2 += (MLP 回流)
```

`dl_residual2` 这个缓冲区被两行**累加**写入：第 993 行写入「主流直通」项（来自 `dl_residual3`），第 997 行的 `layernorm_backward` 内部再用 `+=` 加上「MLP 子层回流」项。两者相加，正好对应 4.1.1 里公式的那两项。

**残差合流的第二处**（[train_gpt2.c:998](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L998) 与 [train_gpt2.c:1002](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1002)）：block 输入 `residual` 的梯度 `dresidual` 同样是两路之和——

```c
residual_backward(dresidual, dl_attproj, dl_residual2, B*T*C);     // L998: dresidual += dl_residual2
// ... 中间是 attproj/attention/qkv 的反向 ...
layernorm_backward(dresidual, dl_ln1w, dl_ln1b, dl_ln1, residual, ...);  // L1002: dresidual += (attention 回流)
```

这里的 `dresidual` 指向上一层的残差缓冲（`residual3[l-1]` 或 `encoded`），于是这一层的反向结果就被接力成下一层（更浅层）反向时的输入梯度。

> **关键直觉**：因为残差反向用的是 `+=`，残差梯度缓冲**天然适合累加**，但也因此**必须在反向开始前清零**——否则上一训练步残留的梯度会混进来。这正是 4.3 节 `gpt2_zero_grad` 存在的头号理由。

#### 4.1.4 代码实践

**实践目标**：亲手验证「残差合流 = 两路 `+=` 之和」。

**操作步骤**：

1. 打开 `gpt2_backward`，定位到 block 循环体内对 `dl_residual2` 的两次写入（第 993、997 行）。
2. 在纸上画一个 block 的前向数据流图，标出 `residual2` 节点有**两条入边**（来自 `residual + attproj`，以及流向 `ln2`）。
3. 在源码里确认：第 993 行的 `residual_backward` 写 `dl_residual2`，第 997 行的 `layernorm_backward` 也写 `dl_residual2`，且函数内部都是 `+=`。

**需要观察的现象**：同一个缓冲指针 `dl_residual2`（在 [train_gpt2.c:985](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L985) 定义为 `grads_acts.residual2 + l * B * T * C`）被两次累加写入，中间隔着 4 个反向调用。

**预期结果**：你能用一句话说明 `dl_residual2 = dl_residual3 + (MLP 子层经 ln2 回流的梯度)`，并指出若把第 993 行的 `residual_backward` 改成普通赋值 `=`，会导致 MLP 回流量被覆盖丢失。

**可观测实验（可选，需自行改源码观察）**：在 `main` 训练步里把 `gpt2_zero_grad(&model)`（[train_gpt2.c:1166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1166)）注释掉，重新 `make train_gpt2` 跑几步。

**预期结果**：训练 loss 不会正常下降（梯度被历史值污染）。结论：清零是 `+=` 累加约定的「配套基础设施」。实验后请还原源码。

#### 4.1.5 小练习与答案

**练习 1**：前向里 `residual3 = residual2 + fcproj`。请写出 `∂L/∂residual2` 中「来自 residual3」那一项的系数，并说明它为何等于 1。

**参考答案**：因为 `residual3` 对 `residual2` 的偏导为 1（加法节点的直通），所以该项系数为 1，即「主流直通」项就是把 `∂L/∂residual3` 原样搬过来。这正是 `residual_backward` 里 `dinp1[i] += dout[i]` 的数学依据。

**练习 2**：如果把 block 反向里第 997 行的 `layernorm_backward(dl_residual2, ...)` 误写成写到别的缓冲（即漏掉了 MLP 回流），模型还能学吗？为什么？

**参考答案**：梯度虽然方向大致不错（主流直通仍在），但每个 block 缺失了 MLP 子层回流的梯度，相当于让 MLP 的参数收不到来自损失的正确信号，训练效果会显著变差甚至无法收敛。这凸显了「合流」两路缺一不可。

---

### 4.2 各层 backward 调用链

#### 4.2.1 概念说明

「镜像」是本节的关键词。`gpt2_backward` 的 block 循环体里，10 个反向调用的顺序，恰好是 `gpt2_forward` block 循环体里 10 个前向调用的**严格逆序**。这不是巧合，而是链式法则的强制要求：要算某层的输入梯度，必须先拿到它的输出梯度，而输出梯度的生产者在前向里排在该层之后。

此外有两个「循环外」的细节决定了整条链路的起点和终点：

- **起点**：反向必须从 `crossentropy_softmax_backward` 开始，因为损失是在那里被定义出来的——只有它能把 `dlosses` 转成 `dlogits`，之后所有层才有梯度可传。
- **终点**：反向必须以 `encoder_backward` 收尾，因为 `wte`/`wpe` 是网络最前端的参数（查表），它们没有更早的层可依赖。

#### 4.2.2 核心流程

把前向与反向逐行对齐（行号见 4.2.3）：

| 顺序 | 前向（gpt2_forward） | 反向（gpt2_backward） |
| --- | --- | --- |
| 尾巴-3 | `matmul_forward(logits, lnf, wte, NULL, ...)` (L876) | `crossentropy_softmax_backward(dlogits, ...)` (L934) |
| 尾巴-2 | `softmax_forward(probs, logits, ...)` (L877) | `matmul_backward(dlnf, grads.wte, NULL, dlogits, lnf, wte, ...)` (L935) |
| 尾巴-1 | `crossentropy_forward(losses, probs, ...)` (L881) | `layernorm_backward(dresidual, lnfw, lnfb, dlnf, ...)` (L938) |
| block#10 | `residual_forward(residual3, residual2, fcproj)` (L872) | `residual_backward(dl_residual2, dl_fcproj, dl_residual3)` (L993) |
| block#9 | `matmul_forward(fcproj, fch_gelu, ...)` (L871) | `matmul_backward(dl_fch_gelu, dl_fcprojw, dl_fcprojb, dl_fcproj, ...)` (L994) |
| block#8 | `gelu_forward(fch_gelu, fch)` (L870) | `gelu_backward(dl_fch, l_fch, dl_fch_gelu)` (L995) |
| block#7 | `matmul_forward(fch, ln2, ...)` (L869) | `matmul_backward(dl_ln2, dl_fcw, dl_fcb, dl_fch, ...)` (L996) |
| block#6 | `layernorm_forward(ln2, ..., residual2, ...)` (L868) | `layernorm_backward(dl_residual2, dl_ln2w, dl_ln2b, dl_ln2, ...)` (L997) |
| block#5 | `residual_forward(residual2, residual, attproj)` (L867) | `residual_backward(dresidual, dl_attproj, dl_residual2)` (L998) |
| block#4 | `matmul_forward(attproj, atty, ...)` (L866) | `matmul_backward(dl_atty, dl_attprojw, dl_attprojb, dl_attproj, ...)` (L999) |
| block#3 | `attention_forward(atty, preatt, att, qkv, ...)` (L865) | `attention_backward(dl_qkv, dl_preatt, dl_att, dl_atty, ...)` (L1000) |
| block#2 | `matmul_forward(qkv, ln1, ...)` (L864) | `matmul_backward(dl_ln1, dl_qkvw, dl_qkvb, dl_qkv, ...)` (L1001) |
| block#1 | `layernorm_forward(ln1, ..., residual, ...)` (L863) | `layernorm_backward(dresidual, dl_ln1w, dl_ln1b, dl_ln1, ...)` (L1002) |
| 头部 | `encoder_forward(encoded, inputs, wte, wpe, ...)` (L825) | `encoder_backward(grads.wte, grads.wpe, dencoded, inputs, ...)` (L1004) |

注意 `crossentropy` 与 `softmax` 在前向是两个独立调用，但在反向被融合成一个 `crossentropy_softmax_backward`（u2-l6 已推导），所以反向比前向少一行。

#### 4.2.3 源码精读

**循环外的尾巴反向**（[train_gpt2.c:934-938](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L934-L938)）：

```c
crossentropy_softmax_backward(grads_acts.logits, grads_acts.losses, acts.probs, model->targets, B, T, V, Vp);
matmul_backward(grads_acts.lnf, grads.wte, NULL, grads_acts.logits, acts.lnf, params.wte, B, T, C, Vp);
float* residual = acts.residual3 + (L-1) * B * T * C;       // 最后一层的前向残差
float* dresidual = grads_acts.residual3 + (L-1) * B * T * C;// 写到最后一层的残差梯度
layernorm_backward(dresidual, grads.lnfw, grads.lnfb, grads_acts.lnf, residual, params.lnfw, acts.lnf_mean, acts.lnf_rstd, B, T, C);
```

第 935 行的 `matmul_backward` 把 `dlogits` 反传成 `dlnf`，**同时把权重梯度累加进 `grads.wte`**（注意第 3 个参数是 `grads.wte`，第 6 个是前向权重 `params.wte`，`bias` 传 `NULL`）。这是权重绑定的反向体现：`wte` 作为输出投影权重，在这里收到一份梯度。

**逐层反向循环**（[train_gpt2.c:940-1003](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L940-L1003)）：循环变量 `l` 从 `L-1` 递减到 `0`。每层开头（[train_gpt2.c:942-943](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L942-L943)）确定本层的前向残差 `residual` 与「写入目标」`dresidual`：

```c
residual  = l == 0 ? acts.encoded          : acts.residual3 + (l-1) * B * T * C;
dresidual = l == 0 ? grads_acts.encoded    : grads_acts.residual3 + (l-1) * B * T * C;
```

注意一个巧妙的接力：当 `l > 0` 时，`dresidual` 指向 `residual3[l-1]` 的梯度缓冲，而这个缓冲**正是上一层（更浅层 `l-1`）反向时第 993 行要读取的 `dl_residual3`**。也就是说，相邻两层的反向通过共享 `grads_acts.residual3` 缓冲无缝衔接，不需要额外的参数传递。

**循环外的头部反向 / 终点**（[train_gpt2.c:1004](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1004)）：

```c
encoder_backward(grads.wte, grads.wpe, grads_acts.encoded, model->inputs, B, T, C);
```

当循环走到 `l == 0` 时，第 1002 行的 `layernorm_backward` 把梯度写进了 `grads_acts.encoded`（即 `dencoded`）。最后这一行 `encoder_backward`（实现见 [train_gpt2.c:60-76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L60-L76)）用 `dencoded` 与缓存的 `inputs`，把梯度**累加**回 `dwte`/`dwpe`：

```c
for (int i = 0; i < C; i++) {
    float d = dout_bt[i];
    dwte_ix[i] += d;   // 累加到词向量梯度
    dwpe_t[i] += d;    // 累加到位置向量梯度
}
```

到这里，**`grads.wte` 已经被累加了两次**：一次来自第 935 行的 logits 投影反向（作为输出权重），一次来自这里的 `encoder_backward`（作为输入查表）。两份都用 `+=`，加完才是 `wte` 的完整梯度。这也再次说明：反向必须以 `encoder_backward` 收尾——它是 `wte`/`wpe` 梯度的最终贡献者，且没有更早的层可接力。

#### 4.2.4 代码实践

**实践目标**：本讲义规格指定的核心实践——对照 `gpt2_forward` 列出 `gpt2_backward` 的层调用逆序，并解释起点与终点。

**操作步骤**：

1. 打开 `gpt2_forward`（[train_gpt2.c:862-877](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L862-L877)），按调用先后抄下 10 个 block 内前向调用 + 3 个尾巴前向调用。
2. 打开 `gpt2_backward`（[train_gpt2.c:934-1004](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L934-L1004)），按调用先后抄下对应反向调用。
3. 像本节 4.2.2 的表格那样把两者一一对齐，核对顺序是否严格相反。

**需要观察的现象**：反向的每一次调用，其「输出梯度参数」都等于前向里它对应算子的「前向输出」的梯度缓冲；其「输入梯度参数」则对应前向输入。

**预期结果**：你得到一张与 4.2.2 完全一致的对照表，并能回答两个问题：

- **为什么必须从 `crossentropy_softmax_backward` 开始？** 因为损失定义在它身上，只有它能产出第一个「真实」的梯度 `dlogits`，点燃整条链路。
- **为什么必须以 `encoder_backward` 收尾？** 因为 `wte`/`wpe` 是最前端的参数（查表），它们没有更早的层可以接力；`encoder_backward` 是它们梯度的最终归宿。

#### 4.2.5 小练习与答案

**练习 1**：前向里 `softmax_forward` 与 `crossentropy_forward` 是两个独立调用，为什么反向只有一个 `crossentropy_softmax_backward`？

**参考答案**：因为在数学上，`dlogits = (probs - onehot)/N` 把两步求导融合后约掉了危险的 `1/probs` 项（数值更稳），还省去了物化 `dprobs` 中间量。所以反向用一次融合调用即可，对应前向的两步。

**练习 2**：`grads.wte` 在一次 `gpt2_backward` 里被写了几次？分别在哪些行？

**参考答案**：两次。第一次在 [train_gpt2.c:935](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L935) 的 logits 投影 `matmul_backward`（`wte` 作输出权重），第二次在 [train_gpt2.c:1004](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1004) 的 `encoder_backward`（`wte` 作输入查表）。两处都 `+=`，加和才是完整梯度。

**练习 3**：相邻两层 `l` 和 `l-1` 的反向是如何把梯度接力传递的？

**参考答案**：第 `l` 层反向时，`dresidual` 指向 `grads_acts.residual3[l-1]`（见 [train_gpt2.c:943](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L943)），并经第 998、1002 行写入。下一轮 `l-1` 层反向时，第 [train_gpt2.c:990](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L990) 行的 `dl_residual3 = grads_acts.residual3 + (l-1)*B*T*C` 读取的正是这个缓冲。靠共享 `residual3` 梯度缓冲完成跨层接力。

---

### 4.3 零梯度初始化：gpt2_zero_grad

#### 4.3.1 概念说明

`gpt2_zero_grad` 看起来只有两行，却是整个训练循环的「安全阀」。它的作用是：在每一步反向之前，把**所有权重梯度**和**所有激活梯度**清零。

为什么必须每步都清零？因为 u2 建立的全局约定是：所有 `*_backward` 写梯度都用 `+=`。这意味着如果不清零，本步算出的梯度会叠加上一步（甚至更早）的梯度，得到的就不是「当前 batch 损失的梯度」，而是历史梯度之和——优化器会沿着错误的方向更新。

清零的对象有两块（对应 `GPT2` 结构体里 [train_gpt2.c:685-698](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L685-L698) 的两组字段）：

1. **权重梯度 `grads_memory`**（`grads`，参数的同形梯度）：被 `matmul_backward`/`layernorm_backward`/`encoder_backward` 等 `+=` 写入。**每步必清**，否则参数梯度会被历史值污染——这是最致命的。
2. **激活梯度 `grads_acts_memory`**（`grads_acts`，激活的同形梯度）：在残差合流处被多次 `+=` 写入（见 4.1）。清零确保每个合流缓冲从干净的 0 开始累加。

#### 4.3.2 核心流程

`gpt2_zero_grad` 的实现（[train_gpt2.c:893-896](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L893-L896)）：

```c
void gpt2_zero_grad(GPT2 *model) {
    if(model->grads_memory != NULL) { memset(model->grads_memory, 0, model->num_parameters * sizeof(float)); }
    if(model->grads_acts_memory != NULL) { memset(model->grads_acts_memory, 0, model->num_activations * sizeof(float)); }
}
```

它被调用的两个时机：

- **首次分配后初始化**：在 `gpt2_backward` 里第一次懒分配 `grads_memory`/`grads_acts_memory` 后立即调用一次（[train_gpt2.c:907-911](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L907-L911)），因为 `malloc` 不保证清零。
- **每个训练步反向之前**：在 `main` 的训练步里，紧跟 `gpt2_forward` 之后、`gpt2_backward` 之前（[train_gpt2.c:1166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1166)）。

完整的训练步四行（[train_gpt2.c:1165-1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1165-L1168)）：

```c
gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);
gpt2_zero_grad(&model);
gpt2_backward(&model);
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, step+1);
```

#### 4.3.3 源码精读

注意 `gpt2_zero_grad` 用 `if (... != NULL)` 做了空指针保护。这是因为梯度内存是**懒分配**的：第一次 `gpt2_backward` 被调用前，`grads_memory` 还是 `NULL`（前向只分配了激活，没分配梯度）。所以 `main` 里第一次调用 `gpt2_zero_grad` 时（[train_gpt2.c:1166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1166)），这两个指针可能仍是 `NULL`，`memset` 会被跳过——这是安全的，因为紧接着 `gpt2_backward` 会在 [train_gpt2.c:907-911](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L907-L911) 完成首次分配并自己调一次 `gpt2_zero_grad` 做初始化清零。

从第二步起，`grads_memory`/`grads_acts_memory` 已分配，`main` 里的 `gpt2_zero_grad` 就真正发挥「每步清零」的作用。

另一个值得注意的关联：`gpt2_backward` 开头有一个守卫（[train_gpt2.c:901-904](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L901-L904)）：

```c
if (model->mean_loss == -1.0f) {
    printf("Error: must forward with targets before backward\n");
    exit(1);
}
```

它要求反向前必须有一次「带 targets 的前向」。因为反向的起点 `dlosses` 依赖 `mean_loss`，而 `mean_loss == -1.0f` 是前向**没有 targets**（例如生成场景，见 u2-l7）时设置的哨兵。生成路径只前向不算损失，自然也不能反向。

#### 4.3.4 代码实践

**实践目标**：理解「每步清零」对训练正确性的影响。

**操作步骤**：

1. 阅读 `gpt2_zero_grad`（[train_gpt2.c:893-896](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L893-L896)），确认它清零的两块内存分别对应 `grads`（权重梯度）与 `grads_acts`（激活梯度）。
2. 在 `main` 训练步（[train_gpt2.c:1165-1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1165-L1168)）里观察这四行的固定顺序。
3. 思考：如果用 PyTorch 来类比，`gpt2_zero_grad` 等价于哪一行？

**需要观察的现象**：四个调用的顺序是固定的「前向 → 清零 → 反向 → 更新」，缺一不可，顺序也不可换。

**预期结果**：你能指出 `gpt2_zero_grad` 等价于 PyTorch 里的 `optimizer.zero_grad()`（注意 llm.c 里它清零的是模型梯度，而 `gpt2_update` 才对应 `optimizer.step()`）。若省略它，由于所有 `*_backward` 都用 `+=`，梯度会跨步累积，训练发散。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gpt2_zero_grad` 要同时清零 `grads_memory` 和 `grads_acts_memory`，而不能只清其中一个？

**参考答案**：权重梯度 `grads` 被 `+=` 累加，不清零会让参数收到历史污染，这是最致命的，必须清。激活梯度 `grads_acts` 在残差合流等处也被多次 `+=` 累加（4.1 节），若不清零，合流缓冲会带着上一步的残值开始累加，导致整条反向链路的梯度错误。两者都必须清。

**练习 2**：`gpt2_zero_grad` 里的 `if (... != NULL)` 判断是为了应对什么情况？

**参考答案**：梯度内存是懒分配的——第一次反向之前 `grads_memory` 还是 `NULL`。`main` 训练步第一次调用 `gpt2_zero_grad` 时（[train_gpt2.c:1166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1166)）这两块可能尚未分配，判断避免对 `NULL` 做 `memset`；真正的初始化清零由 `gpt2_backward` 内部首次分配后自己调用一次完成（[train_gpt2.c:910](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L910)）。

---

## 5. 综合实践

**任务**：为本讲主角 `gpt2_backward` 绘制一张完整的「前向—反向镜像图」，并把三个最小模块串起来验证。

具体做法：

1. **列逆序**（对应 4.2）：以本讲 4.2.2 的表格为模板，自己从源码重新抄写一遍 `gpt2_forward`（[train_gpt2.c:825-881](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L825-L881)）与 `gpt2_backward`（[train_gpt2.c:934-1004](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L934-L1004)）的全部算子调用，确认严格一一对应、顺序相反。
2. **标合流点**（对应 4.1）：在反向表里，用高亮标出 `dl_residual2`（第 993、997 行）与 `dresidual`（第 998、1002 行）这两个「被两次 `+=` 写入」的合流缓冲，并在图上画清两路梯度来源。
3. **标零依赖**（对应 4.3）：在图的最上方画一个 `gpt2_zero_grad` 节点，用箭头指出它清零 `grads` 与 `grads_acts`，并说明「所有 `+=` 都依赖这个清零」。
4. **回答总问题**：用一段话说明——反向链路为什么必须从 `crossentropy_softmax_backward` 开始、以 `encoder_backward` 收尾？中间为什么必须严格逆序？残差处的合流为什么需要 `+=` 且依赖清零？

**预期结果**：你得到一张能挂在墙上的「GPT-2 反向总览图」，它同时解释了反向顺序、残差合流与零梯度初始化三件事的内在联系。

**待本地验证**（可选）：如果你有 GPU 或愿意在 CPU 上等几分钟，可以参考 u1-l2 跑 `make train_gpt2` 并观察前几步 loss 是否从约 5.3 下降；再尝试注释掉 `gpt2_zero_grad` 重新编译，观察 loss 不再正常下降，以验证清零的必要性（实验后请还原源码）。

## 6. 本讲小结

- `gpt2_backward` 把 u2 的 10 个 `*_backward`（外加融合的 `crossentropy_softmax_backward`）按 `gpt2_forward` 的**严格逆序**调用一遍，这是链式法则的强制要求。
- 反向链路由 `grads_acts.losses = 1/(B*T)` **点燃**，经 `crossentropy_softmax_backward` 产出第一个真实梯度 `dlogits`，因此必须从这里开始。
- **残差梯度的合流**：在残差加法节点，残差缓冲的梯度等于「主流直通」（来自更深层）与「子层回流」（经 LayerNorm 倒回来）两路之和；`residual_backward` 用 `+=` 累加，与 `layernorm_backward` 的 `+=` 共同完成合流。
- 相邻两层通过共享 `grads_acts.residual3` 缓冲接力传递残差梯度，无需额外参数；`wte` 的梯度在 `matmul_backward`（logits 投影）与 `encoder_backward`（输入查表）两处累加，体现权重绑定。
- 反向以 `encoder_backward` **收尾**，它是 `wte`/`wpe` 梯度的最终归宿，且没有更早的层可接力。
- `gpt2_zero_grad` 在每步反向前清零权重梯度与激活梯度，是 `+=` 累加约定的配套基础设施；省略它会让梯度跨步累积、训练发散。

## 7. 下一步学习建议

- **下一篇 u3-l2（AdamW 优化器：gpt2_update）**：本讲结束时，`grads` 里已经攒好了当前 batch 的完整梯度。下一讲讲解 `gpt2_update` 如何用这些梯度（配合一阶/二阶动量、偏差修正、解耦权重衰减）去更新 `params`，完成「前向 → 清零 → 反向 → 更新」四步曲的最后一环。
- **u3-l4（数值正确性测试）**：如果你想验证本讲的反向链路在数值上是对的，可以读 `test_gpt2.c`，它跑 10 步训练并与 PyTorch 参考的 `debug_state.bin` 逐 loss 比对，是反向正确性的端到端回归测试。
- **延伸阅读**：在进入 CUDA 主线（u5）之前，可以再翻一遍 u2 各讲的算子反向推导，确认本讲里每个 `*_backward` 的「内部数学」你都理解；这样到 u5 看 CUDA 版的 `gpt2_backward` 时，只需关注并行化与显存，而非算法本身。
