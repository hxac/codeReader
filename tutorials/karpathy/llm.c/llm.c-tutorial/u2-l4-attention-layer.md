# 因果自注意力 attention forward/backward

## 1. 本讲目标

本讲聚焦 GPT-2 里**唯一一个会跨时间位置混合信息的层**——因果自注意力（causal self-attention）。它是整个 Transformer 的核心，也是 `train_gpt2.c` 里最值得逐行读懂的算子之一。学完后你应当能够：

- 看懂 `attention_forward` 如何从 QKV 出发，算出每个位置对历史位置的注意力权重并加权求和；
- 说清楚 **因果 mask** 在代码里具体落在哪几个位置、为什么是 `t2 <= t`；
- 区分 `preatt`（原始打分）与 `att`（softmax 归一化后的权重）两个缓冲区各自存什么、反向各自扮演什么角色；
- 顺着 `attention_backward` 把梯度从 `dout` 一路反推回 Q、K、V，并能口述 softmax 的雅可比。

本讲是「前向各层」单元的第四篇，前置是 [编码层 encoder](u2-l1-encoder-layer.md)（指针算术与行主序）与 [MatMul](u2-l3-matmul-layer.md)（线性层 `out = inp @ W^T + b`）。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立直觉。

**注意力要解决什么问题。** 到目前为止我们学过的层（encoder、layernorm、matmul）都是「逐位置」计算：对每一个 `(b, t)`，输出只依赖这一个位置的输入。语言模型不能只看当前词就预测下一个词，它需要让位置 `t` 「看见」它之前的所有词。注意力就是干这件事的：给位置 `t` 一个机会，去「打量」`0..t` 这些历史位置，挑出重要的，把信息聚合过来。

**为什么叫「自」注意力。** Q（query，查询）、K（key，键）、V（value，值）三者都由**同一个输入**（`ln1` 的输出）经一次线性投影产生，所以叫「自」。query 代表「我想找什么」，key 代表「我能提供什么」，二者做点积就是「匹配度」，匹配度经 softmax 变成权重，再用权重去取 value 的加权和。

**为什么叫「因果」（causal）。** 训练目标是「预测下一个 token」，所以位置 `t` 只能看 `0..t`，绝不能偷看 `t+1..T-1` 的未来。这个约束在代码里体现为循环上界 `t2 <= t`，以及把 `t2 > t` 的权重显式置零。

**为什么「多头」（multi-head）。** 把 `C` 维向量切成 `NH` 份，每份 `hs = C / NH` 维，让 `NH` 个头**各自独立**地做一次注意力，再把结果拼回 `C` 维。这相当于让模型在多个不同的「表示子空间」里并行地建模关系。GPT-2 124M 中 `C=768`、`NH=12`，所以每个头 `hs=64`。

几个复用前几讲的概念：所有多维张量都是「一维数组 + 行主序指针算术」（见 u2-l1）；线性层 `out = inp @ W^T + b` 就是 matmul（见 u2-l3）；每步开头的 `gpt2_zero_grad` 把所有梯度清零，保证 `+=` 累加的正确性。

## 3. 本讲源码地图

本讲只涉及一个文件，但会用到其中的多个片段：

| 片段 | 位置 | 作用 |
| --- | --- | --- |
| `attention_forward` | `train_gpt2.c:271-345` | 前向：QKV → 打分 → softmax → 加权求和 |
| `attention_backward` | `train_gpt2.c:347-405` | 反向：从 `dout` 反推 `dinp`（即 dqkv） |
| QKV 投影 / attproj | `train_gpt2.c:864-866` | 在 `gpt2_forward` 中调用注意力的上下文 |
| 反向调用链 | `train_gpt2.c:999-1001` | 在 `gpt2_backward` 中调用注意力的上下文 |
| 激活缓冲字段 | `train_gpt2.c:607-610` | `qkv / atty / preatt / att` 的形状声明 |
| `num_heads` 配置 | `train_gpt2.c:531` | `GPT2Config` 里的 `NH` |

## 4. 核心概念与源码讲解

### 4.1 QKV 投影与多头切分

#### 4.1.1 概念说明

注意力的输入不是凭空来的。在 `gpt2_forward` 里，残差流先经过 `ln1`（第一个 LayerNorm）得到 `(B,T,C)` 的归一化张量，然后用一次 matmul 把它投影成 `3C` 维：

\[ \text{qkv} = \text{ln1} \cdot W_{qkv}^\top + b_{qkv}, \quad \text{qkv} \in \mathbb{R}^{B \times T \times 3C} \]

这 `3C` 维按顺序切成三段：前 `C` 维是 **Q**、中 `C` 维是 **K**、后 `C` 维是 **V**。这正是代码里反复出现的偏移量 `+C`（取 key）、`+C*2`（取 value）的由来。

**多头切分**则把每段的 `C` 维再切成 `NH` 段，每段 `hs = C / NH` 维。于是第 `h` 个头的 query/key/value 分别是 `qkv` 里每段第 `h` 个 `hs` 维小块。关键点：**多头在内存里不是物理分块**，而是靠指针偏移 `h * hs` 虚拟切出来的——这正是行主序指针算术的威力。

#### 4.1.2 核心流程

```
ln1 (B,T,C)
   │  matmul_forward(l_qkv, l_ln1, l_qkvw, l_qkvb, B,T,C, 3*C)
   ▼
qkv (B,T,3C)   ── 逻辑切分 ──▶  Q: [+0,   +C)   每个头 h 取 [h*hs, h*hs+hs)
                                K: [+C,   +2C)
                                V: [+2C, +3C)
```

每个头 `h` 在位置 `t` 的三个向量，都是同一个 `qkv[b,t,:]` 数组里不同偏移的 `hs` 个浮点数。

#### 4.1.3 源码精读

先看 QKV 投影与注意力在 `gpt2_forward` 中的三连调用（[train_gpt2.c:863-866](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L863-L866)）：

```c
layernorm_forward(l_ln1, ...);                                  // 残差流 -> ln1 (B,T,C)
matmul_forward(l_qkv, l_ln1, l_qkvw, l_qkvb, B, T, C, 3*C);     // ln1 -> qkv (B,T,3C)
attention_forward(l_atty, l_preatt, l_att, l_qkv, B, T, C, NH); // qkv -> atty (B,T,C)
```

注意第三个参数 `3*C`：matmul 的「输出通道数」`OC=3C`，所以权重 `l_qkvw` 是 `(3C, C)`，一次投影同时产出 Q/K/V。`l_qkv` 的形状在结构体里声明为 `(L, B, T, 3*C)`（[train_gpt2.c:607](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L607)）。

`NH`（头数）来自 `GPT2Config.num_heads`（[train_gpt2.c:531](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L531)），对 GPT-2 124M 为 12。进入 `attention_forward` 后第一件事就是把通道切成头（[train_gpt2.c:281-289](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L281-L289)）：

```c
int C3 = C*3;
int hs = C / NH;          // 头大小，GPT-2 124M 为 768/12 = 64
float scale = 1.0 / sqrtf(hs);
...
float* query_t = inp + b*T*C3 + t*C3 + h*hs;        // Q 段第 h 个头
float* key_t2  = inp + b*T*C3 + t2*C3 + h*hs + C;   // K 段：+C
float* value_t2= inp + b*T*C3 + t2*C3 + h*hs + C*2; // V 段：+2C
```

`hs = C / NH` 是多头切分的核心常量；`scale = 1/sqrt(hs)` 是后面缩放点积用的系数。三个指针 `query_t / key_t2 / value_t2` 共享同一个 `inp`（即 `l_qkv`）基地址，仅靠 `+0 / +C / +2C` 区分段、靠 `h*hs` 区分头。

#### 4.1.4 代码实践

**实践目标**：亲手验证多头切分的指针算术。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 `train_gpt2.c:864`，确认 QKV 投影输出 `OC=3*C`。
2. 在 `attention_forward`（`train_gpt2.c:282-283`）旁，用纸笔算：当 `C=768, NH=12` 时，`hs` 与 `scale` 各是多少。
3. （可选）在 `attention_forward` 函数体第一行临时插入一行：
   ```c
   printf("C=%d NH=%d hs=%d scale=%f\n", C, NH, C/NH, 1.0f/sqrtf((float)(C/NH)));
   ```
   然后 `make train_gpt2` 运行一步，观察打印。

**需要观察的现象**：打印应显示 `hs=64, scale=0.125000`（因为 \(1/\sqrt{64}=0.125\)）。

**预期结果**：你会看到 `scale` 正是 `1/sqrt(hs)`，且 query/key/value 三指针的偏移差恰好是 `C` 与 `2C`。运行结果待本地验证（取决于是否实际编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NH` 从 12 改成 24（其余不变），`hs` 和 `scale` 会怎样变化？
**答案**：`hs = 768/24 = 32`，`scale = 1/sqrt(32) ≈ 0.177`。头更多、每头更小、缩放系数更大。

**练习 2**：为什么 query 取在位置 `t`，而 key/value 取在位置 `t2`（一个会被遍历的循环变量）？
**答案**：位置 `t` 是「当前查询者」，它要去打量历史位置 `t2 ∈ [0,t]` 的 key/value。所以 query 固定在 `t`，key/value 随 `t2` 滑动。

---

### 4.2 缩放点积、因果 mask 与 softmax（preatt / att）

#### 4.2.1 概念说明

有了 Q/K/V，注意力的核心计算分四步：

1. **打分**：位置 `t` 的 query 与历史位置 `t2` 的 key 做点积，得到一个标量分数，再乘 `scale`。
2. **因果 mask**：只允许 `t2 <= t`，未来位置不参与。
3. **softmax 归一化**：把分数变成一组和为 1 的权重（为数值稳定先减去最大值）。
4. **加权求和**：用权重去取 value 的加权和，作为该头的输出。

代码里用两个 `(B, NH, T, T)` 的缓冲区分别保存第 1 步和第 3 步的结果：

- `preatt[b,h,t,t2]`：**原始缩放打分**（未归一化）。
- `att[b,h,t,t2]`：**softmax 归一化后的权重**（和为 1，`t2>t` 处为 0）。

数学上，对每个 `(b,h,t)`：

\[ \text{preatt}_{t,t_2} = \frac{1}{\sqrt{d_h}}\sum_{i=0}^{d_h-1} Q_{t,i}\,K_{t_2,i}, \qquad t_2 \in [0,t] \]

\[ \text{att}_{t,t_2} = \frac{\exp(\text{preatt}_{t,t_2} - m)}{\sum_{t_3=0}^{t}\exp(\text{preatt}_{t,t_3} - m)}, \quad m = \max_{t_2\le t}\text{preatt}_{t,t_2} \]

其中减去最大值 `m` 是为了数值稳定（防止 `exp` 溢出），它不会改变 softmax 的结果（分子分母同除一个常数）。

#### 4.2.2 核心流程

`attention_forward` 对每个 `(b,t,h)` 做「四趟扫描」（[train_gpt2.c:285-344](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L285-L344)）：

```
pass 1:  for t2 in 0..t:   preatt[t2] = scale * dot(query_t, key_t2);  并记录 maxval
pass 2:  for t2 in 0..t:   att[t2] = exp(preatt[t2] - maxval);          并累加 expsum
pass 3:  for t2 in 0..T-1: att[t2] *= 1/expsum   (t2<=t);  att[t2]=0  (t2>t 因果置零)
pass 4:  for t2 in 0..t:   out_bth += att[t2] * value_t2               (加权求和)
```

注意 `pass 3` 的循环上界是 `T` 而非 `t`：它要**显式**把 `t2 > t` 的位置写成 0。代码注释说明这并非数学必需（那些位置本来就不参与），是为了调试时与 PyTorch 逐元素对齐。

#### 4.2.3 源码精读

**pass 1：打分 + 记录最大值**（[train_gpt2.c:293-309](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L293-L309)）：

```c
float maxval = -10000.0f;
for (int t2 = 0; t2 <= t; t2++) {                 // 上界 t2<=t 就是因果 mask
    float* key_t2 = inp + ... + h*hs + C;          // key 段
    float val = 0.0f;
    for (int i = 0; i < hs; i++) { val += query_t[i] * key_t2[i]; }
    val *= scale;                                  // 缩放 1/sqrt(hs)
    if (val > maxval) { maxval = val; }            // 顺手记录最大值
    preatt_bth[t2] = val;                          // 存原始打分
}
```

**pass 2：exp + 求和**（[train_gpt2.c:313-319](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L313-L319)）：

```c
float expsum = 0.0f;
for (int t2 = 0; t2 <= t; t2++) {
    float expv = expf(preatt_bth[t2] - maxval);    // 减最大值，数值稳定
    expsum += expv;
    att_bth[t2] = expv;                            // 暂存未归一化的 exp
}
float expsum_inv = expsum == 0.0f ? 0.0f : 1.0f / expsum;
```

**pass 3：归一化 + 因果置零**（[train_gpt2.c:322-330](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L322-L330)）：

```c
for (int t2 = 0; t2 < T; t2++) {
    if (t2 <= t) {
        att_bth[t2] *= expsum_inv;                 // 归一化成权重
    } else {
        att_bth[t2] = 0.0f;                        // 因果 mask：未来位置置零
    }
}
```

**pass 4：加权求和 value**（[train_gpt2.c:332-341](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L332-L341)）：

```c
float* out_bth = out + b*T*C + t*C + h*hs;
for (int i = 0; i < hs; i++) { out_bth[i] = 0.0f; }
for (int t2 = 0; t2 <= t; t2++) {
    float* value_t2 = inp + ... + h*hs + C*2;       // value 段
    float att_btht2 = att_bth[t2];
    for (int i = 0; i < hs; i++) { out_bth[i] += att_btht2 * value_t2[i]; }
}
```

整段外层用 `#pragma omp parallel for collapse(3)` 在 `(b, t, h)` 三维上并行（[train_gpt2.c:285](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L285)），因为每个 `(b,t,h)` 之间互不依赖。

#### 4.2.4 代码实践（本讲必做）

**实践目标**：在 `attention_forward` 中标出因果 mask 的位置，并解释 `preatt` 与 `att` 两个缓冲区分别存什么、反向各自的真实角色。

**操作步骤**：

1. 打开 [train_gpt2.c:293-341](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L293-L341)。
2. 用笔在以下三处标注「因果 mask 落点」：
   - pass 1 的循环上界 `t2 <= t`（[L295](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L295)）——未来位置的打分根本不计算；
   - pass 2 的循环上界 `t2 <= t`（[L314](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L314)）——未来位置不进 exp；
   - pass 3 的 `else` 分支 `att_bth[t2] = 0.0f`（[L328](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L328)）——把 `t2>t` 的权重显式清零。

**两个缓冲区各存什么：**

| 缓冲区 | 形状 | 存什么 | 在反向中的真实角色 |
| --- | --- | --- | --- |
| `preatt` | `(L,B,NH,T,T)` | 原始缩放打分 `scale·Q·K`（未归一化） | **注意：这个前向值在 CPU 反向里其实没被读取**（见下方说明），保留它主要是为了对称与调试时和 PyTorch 对齐 |
| `att` | `(L,B,NH,T,T)` | softmax 归一化后的注意力权重（和为 1，`t2>t` 处为 0） | **反向必需**：softmax 的雅可比完全由 `att` 本身决定，反向直接读 `att` |

**关于「为什么反向都需要」的精确回答**：很多人会以为反向要同时读 `preatt` 和 `att`。但仔细看 `attention_backward` 的签名（[train_gpt2.c:347-349](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L347-L349)）与反向调用（[train_gpt2.c:1000](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1000)），它只接收前向的 `att`（不是 `preatt`），并**写出** `dpreatt`、`datt` 两个**梯度缓冲区**：

- `att`（前向）：真正被反向读取——softmax 雅可比 `∂att/∂preatt = att·(𝟙 - att)` 只用到 `att`。
- `dpreatt`：是反向内部从 `datt` 推出来的**中间梯度**（局部 scratch），算完后立刻喂给「q@k 那一步」的反向，并不依赖前向存的 `preatt`。
- `datt`：同样是反向内部从「value 加权求和」那一步反推出来的中间梯度。

所以更准确的说法是：**前向的 `att` 是反向必需的输入；`preatt`（前向）在 CPU 实现里并未被反向消费**，它和对应的 `dpreatt` 梯度槽位主要起结构对称与调试对照的作用。这是一个很好的「读源码、不读想象」的例子。

**预期结果**：你能指出 mask 的三处落点，并口述 `att` 必需、前向 `preatt` 在反向未被读取这一事实。

#### 4.2.5 小练习与答案

**练习 1**：`pass 1` 里 `maxval` 初始化为 `-10000.0f`，为什么不用 `-INFINITY`？
**答案**：作用相同——只要它小于任何可能的打分即可。作者用 `-10000.0f` 是一个「够小」的朴素初值（代码注释里还留了 `TODO something better`）。功能上它保证第一轮 `val > maxval` 必然成立。

**练习 2**：去掉 `pass 3` 的 `else` 分支（不显式置零），模型结果会变吗？
**答案**：不会。因为 `pass 1/2/4` 的循环上界都是 `t2 <= t`，`t2 > t` 的位置从未被写入或使用。显式置零纯粹是为了调试对齐（代码注释明确说了）。注意：`preatt` 在 `t2 > t` 处是未初始化的脏数据，这也再次印证它不在反向被读取。

---

### 4.3 attention 反向梯度推导

#### 4.3.1 概念说明

反向就是把前向四趟扫描**倒过来、逐趟求导**。前向顺序是「打分 → softmax → 加权求和」，反向就变成「加权求和的反向 → softmax 的反向 → 打分的反向」。需要求的最终目标是对输入 `inp`（即 `l_qkv`，含 Q/K/V）的梯度 `dinp`，它随后会作为 `dl_qkv` 喂给 QKV 投影的 matmul 反向（[train_gpt2.c:1000-1001](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1000-L1001)）。

softmax 反向有一个重要性质：**它只需要前向的输出 `att`，不需要前向的输入 `preatt`**（代码注释专门提醒了这一点，[train_gpt2.c:380-381](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L380-L381)）。这是因为 softmax 的雅可比可以完全用归一化后的 `att` 表达。

#### 4.3.2 核心流程

```
反向 pass 4（value 加权求和的反向）:
  前向:  out[i] = Σ_t2 att[t2] * value_t2[i]
  反向:  datt[t2]   += Σ_i value_t2[i] * dout[i]
         dvalue_t2[i] += att[t2] * dout[i]

反向 pass 2&3（softmax 的反向，用雅可比）:
  ∂att[t2]/∂preatt[t3] = att[t2] * (𝟙[t2==t3] - att[t3])
  dpreatt[t3] += Σ_t2 datt[t2] * att[t2] * (𝟙[t2==t3] - att[t3])

反向 pass 1（打分 q@k 的反向）:
  前向:  preatt[t2] = scale * Σ_i query_t[i] * key_t2[i]
  反向:  dquery_t[i]  += Σ_t2 key_t2[i] * dpreatt[t2] * scale
         dkey_t2[i]   += query_t[i] * dpreatt[t2] * scale
```

三步串成一条链：`dout → datt → dpreatt → (dquery, dkey, dvalue)`。其中 `dvalue` 直接在 pass 4 算出，`dquery/dkey` 在 pass 1 算出，三者合并写入 `dinp`。

> **关于 `+=` 与 `zero_grad`**：同一个 key/value 位置 `t2` 会被**多个**查询位置 `t >= t2` 反复使用，所以 `dkey_t2 / dvalue_t2` 必须跨 `t` 累加；`dquery_t` 在不同头/位置间也靠累加。这就是为什么所有写梯度的地方都用 `+=`，并依赖每步开头的 `gpt2_zero_grad` 把梯度缓冲清零（见 u2-l3 的说明）。

#### 4.3.3 源码精读

`attention_backward` 签名（[train_gpt2.c:347-349](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L347-L349)）：注意它**读** `dout, inp, att`，**写** `dinp, dpreatt, datt`——没有读前向的 `preatt`。

**反向 pass 4：value 累加的逆**（[train_gpt2.c:367-378](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L367-L378)）：

```c
float* dout_bth = dout + b*T*C + t*C + h*hs;
for (int t2 = 0; t2 <= t; t2++) {
    float* value_t2  = inp   + b*T*C3 + t2*C3 + h*hs + C*2;
    float* dvalue_t2 = dinp  + b*T*C3 + t2*C3 + h*hs + C*2;
    for (int i = 0; i < hs; i++) {
        datt_bth[t2]    += value_t2[i] * dout_bth[i];   // 对 att 的梯度
        dvalue_t2[i]    += att_bth[t2] * dout_bth[i];   // 对 V 的梯度
    }
}
```

代码注释贴心地把前向对应行贴在旁边（`out_bth[i] += att_bth[t2] * value_t2[i]`），便于你逐项对照。

**反向 pass 2&3：softmax 雅可比**（[train_gpt2.c:382-388](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L382-L388)）：

```c
// note that softmax (like e.g. tanh) doesn't need the input (preatt) to backward
for (int t2 = 0; t2 <= t; t2++) {
    for (int t3 = 0; t3 <= t; t3++) {
        float indicator = t2 == t3 ? 1.0f : 0.0f;
        float local_derivative = att_bth[t2] * (indicator - att_bth[t3]);
        dpreatt_bth[t3] += local_derivative * datt_bth[t2];
    }
}
```

这就是把雅可比矩阵 `∂att/∂preatt` 与 `datt` 相乘：外层遍历 `t2`（对应 `datt`），内层遍历 `t3`（对应 `dpreatt`），`local_derivative` 正是 `att[t2]·(𝟙 - att[t3])`。

**反向 pass 1：q@k 的逆**（[train_gpt2.c:391-401](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L391-L401)）：

```c
for (int t2 = 0; t2 <= t; t2++) {
    float* key_t2  = inp  + b*T*C3 + t2*C3 + h*hs + C;
    float* dkey_t2 = dinp + b*T*C3 + t2*C3 + h*hs + C;
    for (int i = 0; i < hs; i++) {
        dquery_t[i] += key_t2[i]  * dpreatt_bth[t2] * scale;
        dkey_t2[i]  += query_t[i] * dpreatt_bth[t2] * scale;
    }
}
```

注意 `scale`（`1/sqrt(hs)`）出现在这里——因为前向打分时乘过 `scale`，反向自然也要带上。`dquery_t` 写入 Q 段、`dkey_t2` 写入 K 段（`+C` 偏移），与 pass 4 写的 `dvalue_t2`（`+2C` 偏移）合起来正好填满 `dinp` 的三段。

#### 4.3.4 代码实践

**实践目标**：验证反向三步与前向四步的镜像关系，并确认 softmax 反向不读 `preatt`。

**操作步骤**：

1. 打开 [train_gpt2.c:347-405](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L347-L405)。
2. 在 `attention_backward` 函数体里搜索 `preatt_bth[`（不带 `d`）——你会发现**没有任何读取**前向 `preatt` 的语句，只有 `dpreatt_bth[` 的写入。这印证了 4.2.4 的结论。
3. 对照前向 pass 1→2→3→4 与反向 pass 4→(2&3)→1，画一张「前向从上往下、反向从下往上」的对照表。

**需要观察的现象**：反向的循环顺序是「value → softmax → q@k」，恰好是前向「q@k → softmax → value」的逆序。

**预期结果**：你能口述「dout 经 pass4 分裂成 datt 与 dvalue；datt 经 softmax 变成 dpreatt；dpreatt 经 pass1 分裂成 dquery 与 dkey」。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dquery_t` 用 `+=` 而不是 `=`？
**答案**：同一个 query 位置 `t` 会与所有 `t2 <= t` 的 key 配对产生 `dpreatt[t2]`，这些贡献必须累加进同一个 `dquery_t[i]`，所以用 `+=`。

**练习 2**：如果忘了在训练步开头调用 `gpt2_zero_grad`，attention 的梯度会出什么问题？
**答案**：`dkey_t2 / dvalue_t2` 等会把上一步（甚至更多步）的残留梯度累加进来，导致梯度错误、训练发散。这正是每步必须先清零的原因。

**练习 3**：softmax 反向的注释说「softmax doesn't need the input (preatt) to backward」，请用雅可比公式解释为什么。
**答案**：因为 \(\partial \text{att}_{t_2}/\partial \text{preatt}_{t_3} = \text{att}_{t_2}(\mathbb{1}_{t_2=t_3} - \text{att}_{t_3})\) 只含 `att`（已归一化的输出），不含 `preatt`（原始输入）。所以反向只需缓存 `att`。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**调用链追踪 + 头维度核算**任务：

1. **定位三连调用**：在 [train_gpt2.c:863-866](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L863-L866) 找到 `ln1 → qkv matmul → attention → attproj matmul`。说明 `l_atty`（attention 输出）如何既被 `attproj` 当输入，又在前向被存进 `acts.atty`（[train_gpt2.c:608](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L608)）供反向使用。

2. **核算 GPT-2 124M 的注意力参数与显存**（取 `B=4, T=64, C=768, NH=12, L=12`）：
   - `hs` 与 `scale`；
   - 一层里 `l_qkv`、`l_preatt`、`l_att`、`l_atty` 各占多少个 float（提示：`qkv = B*T*3C`，`preatt = att = B*NH*T*T`，`atty = B*T*C`）；
   - 据此体会 `preatt`/`att` 这两个 `(B,NH,T,T)` 矩阵为何是注意力层的「显存大头」——这会自然引出后续 CUDA 主线里 Flash Attention 的动机。

3. **因果性自检**：对照 [train_gpt2.c:295](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L295) 与 [train_gpt2.c:328](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L328)，写一句话解释「为什么把 `t2 <= t` 改成 `t2 < T` 会让模型变成双向注意力，从而破坏下一个 token 预测任务」。

**参考答案要点**：
1. `l_atty` 是 `attention_forward` 的第一个输出参数（前向写入），随后立刻作为 `matmul_forward(l_attproj, l_atty, ...)` 的输入；反向时 `attproj` 的 matmul 反向先算出 `dl_atty`，再喂给 `attention_backward` 作 `dout`（[train_gpt2.c:999-1000](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L999-L1000)）。
2. `hs=64, scale=0.125`；`l_qkv = 4·64·2304 = 589,824`，`l_preatt = l_att = 4·12·64·64 = 196,608`，`l_atty = 4·64·768 = 196,608`。
3. 改成 `t2 < T` 后，位置 `t` 会看到 `t+1..T-1` 的未来 key/value，注意力从「因果」变「双向」，模型在训练时能偷看答案，与「预测下一个 token」的目标冲突。

## 6. 本讲小结

- 注意力是 GPT-2 里**唯一跨时间位置混合信息**的层；其余层都只在单个 `(b,t)` 上运算。
- Q/K/V 由 `ln1` 经一次 `OC=3C` 的 matmul 投影得到，存于 `l_qkv (B,T,3C)`；多头靠 `hs = C/NH` 与指针偏移 `h*hs` 虚拟切分，不物理分块。
- 前向四趟扫描：打分（`scale·Q·K`，记 `maxval`）→ `exp(减max)` 求和 → 归一化（`t2>t` 显式置零）→ 用 `att` 对 value 加权求和。
- 因果 mask 体现在三处：pass 1/2 的 `t2 <= t` 与 pass 3 的 `else` 置零；显式置零只为调试对齐，数学上非必需。
- `preatt` 存原始打分、`att` 存归一化权重；**反向真正读取的是 `att`**，前向 `preatt` 在 CPU 反向里未被消费，`dpreatt/datt` 只是反向内部的局部梯度 scratch。
- 反向三步（value → softmax 雅可比 → q@k）是前向的严格逆序，所有梯度用 `+=` 累加并依赖每步 `gpt2_zero_grad` 清零。

## 7. 下一步学习建议

- 下一篇 [GELU 与残差连接](u2-l5-gelu-residual.md) 会讲注意力之后的 MLP 子块里的非线性与残差，与本讲衔接。
- 读完后建议回看 `gpt2_forward`（[train_gpt2.c:862-872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L862-L872)）与 `gpt2_backward`（[train_gpt2.c:992-1002](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L992-L1002)），体会一个 Transformer block 的前向/反向如何严格镜像。
- 进阶可关注 `dev/cuda/attention_forward.cu`（多版本内核）与 `llmc/attention.cuh`（手写 CUDA attention）/ cuDNN Flash Attention，它们都是为了缓解本讲综合实践里提到的 `(B,NH,T,T)` 显存压力——这条线索会在 Unit 5 继续。
