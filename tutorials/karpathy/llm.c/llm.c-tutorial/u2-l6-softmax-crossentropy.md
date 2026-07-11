# Softmax、CrossEntropy 与融合反向

## 1. 本讲目标

前面几讲我们把 Transformer 一层一层搭到了 `lnf`（最终层归一化）之后的 `logits`。但 `logits` 只是一堆「还没归一化的分数」，模型要训练还缺两样东西：**把这些分数变成概率**，以及**用一个标量损失衡量好坏**。本讲就完成这最后一步，并把它的反向传播讲透。

学完本讲你应该能够：

- 说清楚「数值稳定的 softmax」为什么必须先减去最大值，以及 `V` 与 `Vp`（词表真实大小与填充大小）在本讲函数里的区别。
- 写出单点交叉熵损失 \( -\log p_{\text{target}} \) 的来历，并理解 `mean_loss` 如何在 `gpt2_forward` 里聚合。
- **亲手推导**出 softmax + crossentropy 融合后的梯度 \( \partial L/\partial x_k = p_k - \mathbb{1}_{k=\text{target}} \)，并理解为什么这个「融合」既更省算力、又更数值稳定。
- 在 `train_gpt2.c` 中定位目标 token 处「梯度减一」的那一行，并用有限差分程序验证整个公式。

本讲只涉及 `train_gpt2.c` 一个文件，三个最小模块，篇幅不大但含一个重要的数学推导。

---

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。

**logits 是什么？** 模型最后一层线性层 `matmul_forward(acts.logits, acts.lnf, params.wte, NULL, ...)` 用 `wte`（token 嵌入表，权重绑定，回顾 [u2-l1](u2-l1-encoder-layer.md)）把每个位置 \( (b,t) \) 的 \( C \) 维向量投映成 \( V_p \) 维向量。这 \( V_p \) 个数就是 logits——「这个词作为下一个 token 的未归一化打分」。分数越大越可能，但它们可以是任意实数（含负数），而且不归一。

**为什么要 softmax？** 训练和采样都需要「概率」：一个和为 1、非负的分布。softmax 就是把 logits 变概率的标准做法。

**为什么要损失函数？** 反向传播需要一个标量目标来「求导」。我们希望「正确 token 的概率尽量大」，等价于「正确 token 的负对数概率尽量小」，于是有了交叉熵损失。

**链式法则（回顾）**：前几讲里每层的反向都是「拿上游梯度，乘以本层的局部导数，传给下游」。本讲的特别之处在于：把 softmax 和 crossentropy 两层合在一起求导后，公式会极大地简化——这是本讲的核心看点。

**`V` 与 `Vp`**：真实词表 \( V=50257 \)，填充到 128 对齐的 \( V_p=50304 \)（回顾 [u1-l3](u1-l3-cpu-reference-overview.md)）。填充区是为访存对齐预留的，本讲所有「真正计算」都只走前 \( V \) 个元素，填充区会被显式置零或保持零。

---

## 3. 本讲源码地图

本讲全部源码集中在 [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c)，涉及三个算子函数与两处调用点：

| 位置 | 作用 |
|------|------|
| `softmax_forward`（L449–L484） | 把 `logits(B,T,Vp)` 转成 `probs(B,T,Vp)`，数值稳定 |
| `crossentropy_forward`（L486–L500） | 由 `probs` 与 `targets` 算每个位置的逐点损失 `losses(B,T)` |
| `crossentropy_softmax_backward`（L502–L521） | **融合反向**：一步从 `dlosses` 算出 `dlogits` |
| `gpt2_forward` 内调用（L876–L886） | 前向串起 softmax→crossentropy→mean_loss |
| `gpt2_backward` 内启动（L928–L934） | 用 \( 1/(BT) \) 填充 `dlosses`，调用融合反向 |

对应的激活张量在结构体里定义为 `logits (B,T,Vp)`、`probs (B,T,Vp)`、`losses (B,T)`（[train_gpt2.c:623-625](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L623-L625)），大小分别为 `B*T*Vp`、`B*T*Vp`、`B*T`（[train_gpt2.c:653-655](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L653-L655)）。

---

## 4. 核心概念与源码讲解

### 4.1 数值稳定的 softmax_forward

#### 4.1.1 概念说明

softmax 把一个 \( V \) 维向量 \( x \)（logits）变成概率向量 \( p \)：

\[
p_i = \frac{e^{x_i}}{\sum_{j=1}^{V} e^{x_j}}
\]

结果是每个 \( p_i \in (0,1) \) 且 \( \sum_i p_i = 1 \)。但直接按这个公式写代码会**数值溢出**：若某个 \( x_i=1000 \)，则 \( e^{1000} \) 远超 `float` 上限，变成 `inf`，整个计算崩溃。

解决办法是利用 softmax 的一个「平移不变」性质：给所有 logits 加同一个常数 \( m \)，结果不变。

\[
p_i = \frac{e^{x_i - m}}{\sum_{j} e^{x_j - m}}
\]

只要令 \( m = \max_j x_j \)，那么所有指数里的 \( x_j - m \le 0 \)，于是 \( e^{x_j-m} \in (0,1] \)，永远不会溢出。这就是「数值稳定的 softmax」。

#### 4.1.2 核心流程

对每个位置 \( (b,t) \) 独立处理一个 \( V_p \) 维向量（与其它位置互不依赖，天然可并行）：

1. **求最大值**：在前 \( V \) 个真实 logits 里找 `maxval`（只在真实词表里找，不去碰填充区）。
2. **求和**：对前 \( V \) 个元素算 `exp(logit - maxval)` 并累加 `sum`，同时把结果暂存到 `probs`。
3. **归一化**：前 \( V \) 个 `probs` 除以 `sum`。
4. **清零填充区**：把 \( [V, V_p) \) 显式置 0，确保填充维度概率为 0。

外层用 `#pragma omp parallel for collapse(2)` 把 \( b,t \) 两个循环一起并行（回顾 [u2-l3](u2-l3-matmul-layer.md) 里 OpenMP 的用法）。

#### 4.1.3 源码精读

`softmax_forward` 的核心四步（[train_gpt2.c:449-484](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L449-L484)）：

```c
float maxval = -10000.0f; // TODO something better
for (int i = 0; i < V; i++) {
    if (logits_bt[i] > maxval) { maxval = logits_bt[i]; }
}
float sum = 0.0f;
for (int i = 0; i < V; i++) {
    probs_bt[i] = expf(logits_bt[i] - maxval);   // 减 maxval 保证不溢出
    sum += probs_bt[i];
}
for (int i = 0; i < V; i++) { probs_bt[i] /= sum; }
for (int i = V; i < Vp; i++) { probs_bt[i] = 0.0f; } // 填充区强制清零
```

要点解读：

- `maxval` 初值 `-10000.0f` 是一个权宜写法（注释里写了 `TODO something better`，更稳妥的是用 `-INFINITY` 或第一个元素）。在实际训练里 logits 量级不大，这个初值够用。
- **三段循环都只到 `V`**：求最大、求和、归一化都只在真实词表上进行；填充区最后单独清零。这样填充维度既不参与归一化（不会稀释概率），也保证概率严格落在真实词上。
- `logits_bt = logits + b*T*Vp + t*Vp`：标准「一维数组 + 行主序指针算术」，回顾 [u2-l1](u2-l1-encoder-layer.md) 末尾的寻址公式。

#### 4.1.4 代码实践

**实践目标**：亲手验证「减最大值」不改变 softmax 结果，并体会不加它会溢出。

**操作步骤**（示例代码，非仓库原有）：

```c
// 示例代码：观察 maxval 减法的必要性
#include <stdio.h>
#include <math.h>
int main(void) {
    float x[3] = {1000.0f, 1001.0f, 1002.0f};
    // 不减 maxval：直接算 exp
    float raw0 = expf(x[0]), raw1 = expf(x[1]), raw2 = expf(x[2]);
    printf("raw exp = %f %f %f\n", raw0, raw1, raw2); // 预期全 inf
    // 减 maxval 后再算
    float m = x[2];
    float s = expf(x[0]-m) + expf(x[1]-m) + expf(x[2]-m);
    printf("stable softmax = %f %f %f\n", expf(x[0]-m)/s, expf(x[1]-m)/s, expf(x[2]-m)/s);
    return 0;
}
```

**需要观察的现象**：第一行 `raw exp` 三个值全是 `inf`（`float` 溢出）；第二行能正常打印出三个和为 1 的概率。

**预期结果**：`inf inf inf`，随后是约 `0.090 0.245 0.665` 的一组归一化概率。**待本地验证**具体小数位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `maxval` 的循环只到 `V` 而不是 `Vp`？如果填充区里恰好有一个很大的数会怎样？

**答案**：填充区对应不存在的词，不应参与 softmax。本实现把填充区最后清零，所以即使填充区有噪声也不会影响 `maxval`/`sum`（因为循环不读它们）。若错误地把循环改成到 `Vp`，填充区的值会污染最大值与归一化，把概率「分走」给不存在的词。

**练习 2**：softmax 具有平移不变性 \( \text{softmax}(x) = \text{softmax}(x+c) \)。请用一句话解释为什么。

**答案**：因为分子分母同时乘以 \( e^c \)，约掉后不变；这正是「减去 maxval 不改变结果」的数学依据。

---

### 4.2 crossentropy_forward 损失

#### 4.2.1 概念说明

有了概率，还要衡量「模型预测得好不好」。监督信号是 `targets`：每个位置 \( (b,t) \) 给出正确下一个 token 的下标 \( \text{ix} \)（回顾 [u1-l4](u1-l4-data-and-tokenizer.md) 里「错位一位」的下个 token 预测）。

我们希望正确 token 的概率 \( p_{\text{ix}} \) 越大越好。最大化 \( p_{\text{ix}} \) 等价于最大化 \( \log p_{\text{ix}} \)，等价于最小化负对数似然：

\[
\text{loss} = -\log(p_{\text{ix}})
\]

这就是单点交叉熵损失（当目标是 one-hot 时，交叉熵与负对数似然等价）。几个直观数值：

- 若 \( p_{\text{ix}} = 1 \)（完全确信且正确），\( \text{loss} = -\log 1 = 0 \)。
- 若 \( p_{\text{ix}} = 0.5 \)，\( \text{loss} \approx 0.693 \)。
- 若 \( p_{\text{ix}} = 0.01 \)，\( \text{loss} \approx 4.605 \)。

概率越小，损失越大，且以对数速度增长——这会强烈惩罚「把正确词预测得几乎不可能」的情况。

整个 batch 有 \( B \times T \) 个位置，最终报告的 `mean_loss` 是它们的平均：

\[
\text{mean\_loss} = \frac{1}{BT}\sum_{b,t}\text{losses}[b,t]
\]

#### 4.2.2 核心流程

1. 遍历每个位置 \( (b,t) \)。
2. 取出目标下标 `ix = targets[b*T+t]`。
3. 写入 `losses[b,t] = -logf(probs_bt[ix])`（注意是取**目标处**那一个概率）。
4. 在 `gpt2_forward` 里把这 \( B \times T \) 个损失求平均得到 `model->mean_loss`。

注意：这一步**不**写梯度，只产生一个前向标量（及逐点损失缓冲），梯度全部留给下一节的融合反向。

#### 4.2.3 源码精读

`crossentropy_forward`（[train_gpt2.c:486-500](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L486-L500)）：

```c
for (int b = 0; b < B; b++) {
    for (int t = 0; t < T; t++) {
        float* probs_bt = probs + b * T * Vp + t * Vp;
        int ix = targets[b * T + t];
        losses[b * T + t] = -logf(probs_bt[ix]);   // 只取目标处的概率
    }
}
```

`mean_loss` 的聚合发生在 `gpt2_forward` 里（[train_gpt2.c:881-886](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L881-L886)）：

```c
crossentropy_forward(model->acts.losses, model->acts.probs, targets, B, T, Vp);
float mean_loss = 0.0f;
for (int i=0; i<B*T; i++) { mean_loss += model->acts.losses[i]; }
mean_loss /= B*T;
model->mean_loss = mean_loss;
```

这段只有当传入了 `targets`（训练/验证时）才执行；采样生成时 `targets==NULL`，`mean_loss` 被设为 `-1.0f` 作为「没有损失」的哨兵（[train_gpt2.c:887-890](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L887-L890)），这个哨兵后面会被 `gpt2_backward` 用作「是否前向过」的检查。

#### 4.2.4 代码实践

**实践目标**：用对数关系体会「概率越小损失越大」，并确认目标位置的选取。

**操作步骤**：

1. 阅读 [train_gpt2.c:494-497](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L494-L497)，确认损失只读了 `probs_bt[ix]` 这一个数。
2. 在纸上算：某位置 `probs` 在目标处为 0.5、0.1、0.001 时，损失分别约为多少。
3. 找到 `gpt2_forward` 里产生 `logits` 的那行 `matmul_forward(acts.logits, acts.lnf, params.wte, NULL, ...)`（[train_gpt2.c:876](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L876)），回忆这里用 `wte` 当权重、`bias` 传 `NULL`（权重绑定，见 [u2-l1](u2-l1-encoder-layer.md)），所以 `logits` 与 embedding 共享同一张表。

**需要观察的现象 / 预期结果**：损失分别为约 0.693、2.303、6.908。可以看到当概率从 0.1 掉到 0.001（缩小 100 倍），损失只增加约 4.6（对数尺度），这正是交叉熵「惩罚错得离谱」但仍可训练的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossentropy_forward` 里取 `logf` 而不是 `log10`？两者会差一个常数倍，对训练有影响吗？

**答案**：自然对数 \( \ln \) 是信息论与最大似然的惯例。\( \log_{10} \) 与 \( \ln \) 只差一个常数因子 \( 1/\ln 10 \)，对「梯度方向」无影响，只会等比缩放学习率的有效大小，因此数学上等价；工程上统一用自然对数以和论文/框架对齐。

**练习 2**：如果某次前向 `probs_bt[ix]` 由于数值误差变成 0，`-logf(0)` 会得到什么？这会带来什么风险？

**答案**：`-logf(0) = +inf`，会让 `mean_loss` 变成 `inf` 并污染梯度。实践中 softmax 输出严格大于 0（指数永不为 0），所以前向一般安全；真正的「除零」风险在反向，下一节的融合反向正是为了规避它。

---

### 4.3 融合反向 dlogits：crossentropy_softmax_backward

#### 4.3.1 概念说明

这是本讲最关键的一节。直觉上，反向传播要分两步：先对 crossentropy 求导得到 `dprobs`（对 softmax 输出的梯度），再对 softmax 求导得到 `dlogits`。但把这两步合起来推导后，公式会异常简洁，而且**避免了数值灾难**。

设单点损失 \( L = -\log p_{\text{ix}} \)，其中 \( p_i = \frac{e^{x_i}}{\sum_j e^{x_j}} \)。先看「分两步」会得到什么：

- 第一步，对 crossentropy：\( \frac{\partial L}{\partial p_i} = -\frac{\mathbb{1}_{i=\text{ix}}}{p_{\text{ix}}} \)。注意它在目标处出现一个 \( 1/p_{\text{ix}} \)——训练初期正确 token 概率可能极小（如 \( 10^{-6} \)，即 vocab 均匀分布量级），这个梯度会**爆炸成百万量级**。
- 第二步，对 softmax，要用到 softmax 的雅可比 \( \frac{\partial p_i}{\partial x_k} = p_i(\mathbb{1}_{i=k} - p_k) \)。

把两步用链式法则合并：

\[
\frac{\partial L}{\partial x_k}
= \sum_i \frac{\partial L}{\partial p_i}\frac{\partial p_i}{\partial x_k}
= \sum_i \left(-\frac{\mathbb{1}_{i=\text{ix}}}{p_{\text{ix}}}\right) p_i(\mathbb{1}_{i=k} - p_k)
\]

求和式中只有 \( i=\text{ix} \) 那一项非零（因为前面的指示函数），代入：

\[
\frac{\partial L}{\partial x_k}
= -\frac{1}{p_{\text{ix}}}\cdot p_{\text{ix}}\cdot(\mathbb{1}_{\text{ix}=k} - p_k)
= -(\mathbb{1}_{k=\text{ix}} - p_k)
= p_k - \mathbb{1}_{k=\text{ix}}
\]

这就是著名的结论：

\[
\boxed{\;\frac{\partial L}{\partial x_k} = p_k - \mathbb{1}_{k=\text{ix}}\;}
\]

融合带来两个好处：

1. **数值稳定**：中间那个危险的 \( 1/p_{\text{ix}} \) 与雅可比里的 \( p_{\text{ix}} \) **精确约掉**，再也不出现除以小概率的爆炸。
2. **省算力省显存**：不需要真的去物化一个完整的 `dprobs`（\( B \times T \times V_p \)），一遍循环就算完。

再叠加上游梯度。因为最终损失是 `mean_loss = 平均(losses)`，对第 \( (b,t) \) 个逐点损失而言，上游梯度是 \( \frac{\partial \text{mean\_loss}}{\partial \text{losses}[b,t]} = \frac{1}{BT} \)。所以代码里每个位置的 `dloss = 1/(B*T)`，最终：

\[
\text{dlogits}_k = (p_k - \mathbb{1}_{k=\text{ix}})\cdot \text{dloss}
\]

这正是规格里要推导的 \( \text{dlogits} = (\text{probs} - \text{onehot})/N \)，其中 \( N = BT \)。

#### 4.3.2 核心流程

反向由 `gpt2_backward` 启动（顺序与 [u3-l1](u3-l1-backward-assembly.md) 详述，本讲只看开头）：

1. **填上游梯度**：把 `grads_acts.losses` 全部填成 \( 1/(BT) \)——这其实是「对 mean 操作」的一次微型内联反向（[train_gpt2.c:931-932](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L931-L932)）。
2. **融合反向**：对每个位置 \( (b,t) \)，取 `dloss` 与目标 `ix`，对前 \( V \) 个元素写 `dlogits[i] += (probs[i] - indicator)*dloss`。
3. **填充区不动**：循环只到 \( V \)，\( [V,V_p) \) 的 `dlogits` 保持为 0（由 `gpt2_zero_grad` 清零保证）。
4. 之后 `matmul_backward` 接力，把 `dlogits` 反传回 `lnf` 与 `wte` 梯度（[train_gpt2.c:935](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L935)）。

注意第 2 步用的是 `+=`（累加），与全仓库其它反向算子一致；它依赖每步开头的 `gpt2_zero_grad` 把 `grads_acts_memory` 清零（回顾 [u2-l1](u2-l1-encoder-layer.md) 末尾对 `+=` 与清零的讨论）。

#### 4.3.3 源码精读

`crossentropy_softmax_backward`（[train_gpt2.c:502-521](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L502-L521)）：

```c
for (int b = 0; b < B; b++) {
    for (int t = 0; t < T; t++) {
        float* dlogits_bt = dlogits + b * T * Vp + t * Vp;
        float* probs_bt   = probs + b * T * Vp + t * Vp;
        float dloss = dlosses[b * T + t];     // = 1/(B*T)
        int ix = targets[b * T + t];
        for (int i = 0; i < V; i++) {
            float p = probs_bt[i];
            float indicator = i == ix ? 1.0f : 0.0f;   // one-hot
            dlogits_bt[i] += (p - indicator) * dloss;  // (probs - onehot)/N
        }
    }
}
```

**目标 token 处的「梯度减一」**：当 `i == ix` 时，`indicator = 1`，该位置贡献 `(p - 1)*dloss`；而非目标位置 `indicator = 0`，贡献 `p*dloss`。对比可知，**目标位置相对于「单纯按概率成比例」少算了 `1*dloss = 1/(BT)`**——这就是规格所说的「目标 token 处的梯度减一」：`(p - indicator)` 里的那个 `1` 把目标 logit 往下压（鼓励它相对变大）。

反向启动处的上游梯度填充（[train_gpt2.c:928-934](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L928-L934)）：

```c
// kick off the chain rule by filling in dlosses with 1.0f/(B*T)
float dloss_mean = 1.0f / (B*T);
for (int i = 0; i < B*T; i++) { grads_acts.losses[i] = dloss_mean; }
crossentropy_softmax_backward(grads_acts.logits, grads_acts.losses, acts.probs, model->targets, B, T, V, Vp);
```

这一段直接给出了 \( N = BT \)：因为 `mean_loss` 是对 \( B \times T \) 个逐点损失求平均，所以每个 `dlosses` 都等于 \( 1/(BT) \)，与 4.3.1 的推导完全对应。

#### 4.3.4 代码实践

**实践目标**：亲手把 \( \text{dlogits}=(\text{probs}-\text{onehot})/N \) 推一遍，并用「有限差分」数值梯度独立验证它，确认目标 token 处的「减一」。

**操作步骤**：

1. **解析推导**：按 4.3.1 的步骤在纸上重推一遍 \( \partial L/\partial x_k = p_k - \mathbb{1}_{k=\text{ix}} \)，重点确认 \( 1/p_{\text{ix}} \) 是如何与 \( p_{\text{ix}} \) 约掉的。
2. **对照源码**：在 [train_gpt2.c:514-518](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L514-L518) 找到 `indicator` 与 `(p - indicator) * dloss`，确认目标位置 `i==ix` 时比非目标位置多减了 `1*dloss`。
3. **数值验证**（示例代码，非仓库原有；用有限差分估计梯度，再与解析公式比较）：

```c
// 示例代码：有限差分验证 softmax+crossentropy 的融合梯度 = probs - onehot
#include <stdio.h>
#include <math.h>
#define V 5
void softmax(double* p, double* x){           // 单个位置，数值稳定
    double m=-1e9; for(int i=0;i<V;i++) if(x[i]>m)m=x[i];
    double s=0; for(int i=0;i<V;i++){p[i]=exp(x[i]-m); s+=p[i];}
    for(int i=0;i<V;i++) p[i]/=s;
}
double ce(double* p, int t){ return -log(p[t]); }  // 交叉熵
int main(void){
    double x[V]={1.0,2.0,0.5,-1.0,3.0}; int target=2; double h=1e-6;
    double p[V], d[V];
    softmax(p,x);
    for(int k=0;k<V;k++) d[k]=p[k] - (k==target?1.0:0.0); // 解析：probs-onehot
    printf("k | analytic | finite-diff | abs_diff\n");
    for(int k=0;k<V;k++){
        double xp[V],xm[V],pp[V],pm[V];
        for(int i=0;i<V;i++){xp[i]=x[i];xm[i]=x[i];}
        xp[k]+=h; xm[k]-=h; softmax(pp,xp); softmax(pm,xm);
        double fd=(ce(pp,target)-ce(pm,target))/(2*h);     // 中心差分
        printf("%d | %+.6f | %+.6f | %.2e\n",k,d[k],fd,fabs(d[k]-fd));
    }
    return 0;
}
```

**需要观察的现象**：每个 `k` 的 `analytic` 与 `finite-diff` 两列几乎相等，`abs_diff` 在 `1e-9` 量级（双精度）。特别地 `k=2`（target）那行 `analytic = p[2] - 1`，明显比相邻的 `p[k]` 小约 1，肉眼可见「减一」。

**预期结果**：`abs_diff` 全部接近 0（如 `1e-10`），证明融合公式正确。**待本地验证**具体数值。

#### 4.3.5 小练习与答案

**练习 1**：如果不融合、真去算 `dprobs[ix] = -1/p[ix]`，在训练刚开始（模型接近均匀分布，\( p_{\text{ix}}\approx 1/V \)）时会发生什么？融合后又如何？

**答案**：不融合时 `dprobs[ix] ≈ -V ≈ -5e4`，是巨大的数，会与 softmax 雅可比里的 \( p_{\text{ix}} \) 相乘后才变回正常——这个中间巨数既容易溢出/丢精度，也白占显存。融合后 \( 1/p_{\text{ix}} \) 与 \( p_{\text{ix}} \) 在公式里直接约掉，永远不出现这个巨数，数值稳定且省一次物化。

**练习 2**：`dlogits_bt[i] += (p - indicator)*dloss` 用了 `+=` 而不是 `=`。结合 `gpt2_zero_grad` 解释为什么这里其实也可以写成 `=`，以及作者为什么仍然用 `+=`。

**答案**：`crossentropy_softmax_backward` 是反向的第一步，`grads_acts.logits` 此前被 `gpt2_zero_grad` 清零，所以 `+=` 和 `=` 结果相同。作者统一用 `+=` 是全仓库反向算子的一致约定（很多算子的输出梯度会被多个下游路径累加，回顾 [u2-l1](u2-l1-encoder-layer.md) 中 `wte` 同时被 encoder 与 logits matmul 累加的例子），保持风格统一、不易出错。

**练习 3**：填充区 \( [V,V_p) \) 的 `dlogits` 在本函数里没有任何赋值。它最终是多少？为什么这样是对的？

**答案**：保持 `gpt2_zero_grad` 设置的 0。因为填充维度不对应任何真实词，本就不该有梯度；后续 `matmul_backward` 反传到 `wte` 时，填充列的梯度为 0 也正合理——这些列的权重不会被更新。

---

## 5. 综合实践

把本讲三块内容串成一个端到端的小验证任务：**自己实现一遍「logits → softmax → crossentropy → 融合反向」并对照源码**。

任务步骤：

1. **复刻前向**：参照 4.1.3，用 C 实现一个对单位置 \( V=8 \) 的 `softmax_forward`（含减 maxval 与填充区清零，令 `V=8, Vp=12`），打印归一化后的概率并验证前 8 个之和为 1、后 4 个为 0。
2. **复刻损失**：选定一个 `target`，按 4.2.3 算 `loss = -log(probs[target])`，再换两个不同的 `target` 观察损失变化。
3. **复刻融合反向**：按 4.3.3 实现 `dlogits[i] = (probs[i] - (i==target))*dloss`（这里 `dloss=1`，即单位置不取平均），打印 `dlogits`。
4. **独立校验**：用 4.3.4 的中心有限差分重新估计 `dlogits`，逐元素对比解析值；要求两者最大绝对差小于 `1e-5`（float）或 `1e-9`（double）。
5. **定位源码**：在 [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) 里找到三处函数与两处调用点，在你自己的小程序旁边标注对应行号，确认「自己的实现与仓库实现一一对应」。

**预期结果**：第 1 步前 8 个概率和为 1、后 4 个为 0；第 4 步解析梯度与有限差分梯度逐元素吻合。若出现不符，最先排查「是否忘了减 maxval」「有限差分步长是否过大/过小」「target 下标是否取错」。**待本地验证**。

---

## 6. 本讲小结

- softmax 把 logits 变概率；**数值稳定**的关键是先减去 `maxval`，且本实现只在真实词表 \( V \) 上计算、把填充区 \( [V,V_p) \) 清零。
- 单点交叉熵损失 \( -\log p_{\text{target}} \)，在 `gpt2_forward` 里对 \( B\times T \) 个位置求平均得到训练用标量 `mean_loss`；`targets==NULL` 时用 `-1.0f` 作哨兵。
- **融合反向**的招牌结果：\( \partial L/\partial x_k = p_k - \mathbb{1}_{k=\text{target}} \)，代码里再乘以上游 `dloss = 1/(BT)`，即 `dlogits = (probs - onehot)/N`。
- 融合的两个好处：约掉危险的 \( 1/p_{\text{target}} \) 保证数值稳定；省去物化 `dprobs` 的开销。
- 「目标 token 处梯度减一」体现在 `indicator` 上：`i==ix` 时贡献 `(p-1)*dloss`，比非目标位置少 `1/(BT)`。
- 本讲三个算子的前向/反向都遵循全仓库约定：`+=` 累加 + 每步 `gpt2_zero_grad` 清零；填充区梯度恒为 0。

---

## 7. 下一步学习建议

到这里，你已经集齐了 GPT-2 所有单个算子的前向与反向：embedding、layernorm、matmul、attention、gelu、residual，以及本讲的 softmax/crossentropy/融合反向。下一步进入 [u2-l7 前向组装：gpt2_forward 串起整个模型](u2-l7-forward-assembly.md)，看这些算子如何按 pre-norm Transformer 块的顺序被组装成一次完整的前向，以及 `logits/probs/losses` 如何在整条调用链的末端被本讲的三个函数收尾。

建议继续阅读：[train_gpt2.c 的 gpt2_forward](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L843-L891) 完整函数体，把本讲的末端收尾与前面所有层串成一张完整的前向数据流图。
