# 编码层 encoder：token 与 position embedding

## 1. 本讲目标

本讲是「前向各层」单元（Unit 2）的第一篇。在上一篇（u1-l3）里我们已经搭好了 `train_gpt2.c` 的骨架：知道了 `GPT2Config`、16 个参数张量、23 个激活张量，以及 `main` 里的「前向 → 清零梯度 → 反向 → 更新」四步循环。从本篇开始，我们逐层放大，进入模型真正的第一道工序——**编码层 encoder**。

读完本讲，你应该能够：

- 说清楚 **token embedding（wte）** 和 **position embedding（wpe）** 各自是什么、为什么要把它们**相加**。
- 看懂 `encoder_forward` 如何用「查表（gather）」式的指针算术，把一段 token id 序列变成 `(B,T,C)` 的浮点张量。
- 解释 `encoder_backward` 为什么对 `dwte`/`dwpe` 用 `+=`（累加）而不是 `=`（覆盖）。
- 手写一个最小的 C 程序，验证 `out[b,t,:] = wte[inp[b,t],:] + wpe[t,:]`。

---

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

### 2.1 神经网络不认整数，只认浮点向量

经过分词（参见 u1-l4），一段文本被切成了一串 **token id**，比如 `[2, 0, 4]`。这些 id 只是词表里的整数下标。但 Transformer 的每一层都是浮点矩阵运算，整数没法直接参与。所以第一步必须把每个整数 id 变成一个 **C 维的浮点向量**（GPT-2 124M 中 `C = 768`）。这个向量就叫做这个 token 的 **embedding（嵌入）**。

最自然的做法是**查表**：准备一张 `(V, C)` 的大表 `wte`（V 是词表大小），第 `ix` 行就是 id 为 `ix` 的 token 的向量。给定 id，只要取对应那一行即可。这种「按下标取一行」的操作叫 **gather（聚合/查表）**。

> `wte` = **w**eight **t**oken **e**mbedding，即词嵌入表；`wpe` = **w**eight **p**ositional **e**mbedding，即位置嵌入表。命名里的 `w` 提示它们是模型**参数**（要被训练更新的权重），而不是中间激活。

### 2.2 光有词义不够，还得告诉模型「这是第几个词」

自注意力（attention）本身对位置是不敏感的：它把序列当成「一组向量」来看，打乱顺序结果几乎一样。为了让模型区分「我爱你」和「你爱我」，必须给每个位置额外注入一个**位置信号**。

GPT-2 的做法是再来一张表 `wpe`，形状 `(maxT, C)`（maxT 是最大序列长度，GPT-2 为 1024）。第 `t` 行就是「第 t 个位置」的位置向量。最终某个位置上某个 token 的表示，就是把它的**词向量**和**位置向量**逐元素相加：

\[ \text{out}[b,t,i] = \underbrace{\text{wte}[\text{inp}[b,t],\, i]}_{\text{词义}} + \underbrace{\text{wpe}[t,\, i]}_{\text{位置}}, \quad i \in [0, C) \]

两份信息相加而不是拼接，是为了不增加通道数，让残差流的维度从头到尾保持 `C`。

### 2.3 行主序（row-major）内存布局回顾

承接 u1-l3：所有张量都是**一维连续内存 + 指针算术**模拟出来的多维数组，采用**行主序**（C 语言默认）。对一个形状 `(B, T, C)` 的张量 `out`，元素 `out[b][t][i]` 在一维数组里的下标是：

\[ \text{offset}(b,t,i) = b \cdot (T \cdot C) + t \cdot C + i \]

即「越靠左的维度，跨一步要跳过的元素越多」。本讲的指针寻址全部基于这一条。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.c` | CPU 参考实现。本讲的主角：`encoder_forward`（第 35–58 行）、`encoder_backward`（第 60–76 行），以及它们在 `gpt2_forward`/`gpt2_backward` 里的调用点和参数结构体定义。 |
| `doc/layernorm/layernorm.c` | 仓库自带的「单层独立教学程序」样板（内容是 LayerNorm，不是 encoder）。本讲只把它当作**编程范式**参考：它示范了如何用一个独立的小 C 程序、配合 `gcc ... -lm` 验证某一层的正确性，本讲的代码实践会沿用这个风格。 |

> 说明：`doc/layernorm/layernorm.c` 讲的是 LayerNorm 层，**不含 encoder 代码**；本讲引用它只为说明「单层独立验证程序」的写法。encoder 的全部真实代码都在 `train_gpt2.c` 里。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **encoder_forward 的查表与相加**——前向怎么做。
2. **encoder_backward 的梯度累加**——反向为什么用 `+=`。
3. **指针算术与 `(B,T,C)` 布局**——地址是怎么算出来的。

### 4.1 encoder_forward 的查表与相加

#### 4.1.1 概念说明

前向编码层的任务是：输入一个 `(B,T)` 的整数 token id 数组 `inp`，输出一个 `(B,T,C)` 的浮点张量 `out`。对每个位置 `(b,t)`：

1. 取出 token id `ix = inp[b,t]`；
2. 在词嵌入表 `wte` 里查第 `ix` 行，得到这个词的 C 维词向量；
3. 在位置嵌入表 `wpe` 里查第 `t` 行，得到这个位置的 C 维位置向量；
4. 把两者**逐元素相加**，写入 `out[b,t,:]`。

这一步**不混合** batch 之间、也不混合时间步之间的信息——每个 `(b,t)` 独立计算。它是整个 Transformer 的「入口」，产出的 `out` 就是残差流的第 0 层 `residual[0]`（后续所有层都在这条残差流上做加法）。

#### 4.1.2 核心流程

用伪代码描述：

```
for 每个样本 b in [0, B):
    for 每个时间步 t in [0, T):
        ix      = inp[b, t]          # 这个位置的 token id（整数）
        wte_ix  = wte 的第 ix 行      # 词向量，长度 C
        wpe_t   = wpe 的第 t 行       # 位置向量，长度 C
        for 每个通道 i in [0, C):
            out[b, t, i] = wte_ix[i] + wpe_t[i]
```

整个过程就是一个**双层循环 + 一个内层通道循环**，没有任何复杂的控制流。

#### 4.1.3 源码精读

函数签名和注释把张量形状讲得很清楚：

- [train_gpt2.c:35-41](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L35-L41) —— `encoder_forward` 的签名与形状注释：`out` 是 `(B,T,C)`，`inp` 是 `(B,T)` 的整数，`wte` 是 `(V,C)`，`wpe` 是 `(maxT,C)`。

核心循环只有十几行：

- [train_gpt2.c:42-57](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L42-L57) —— 先定位 `out[b,t,:]` 的指针 `out_bt`，取出 id `ix`，再分别查到 `wte_ix`、`wpe_t` 两个指针，最后用 `out_bt[i] = wte_ix[i] + wpe_t[i]` 完成相加。注意第 47 行 `int ix = inp[b * T + t];` 正是「用 id 当行号查表」的关键。

`encoder_forward` 在前向主函数里被最先调用：

- [train_gpt2.c:825](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L825) —— `encoder_forward(acts.encoded, inputs, params.wte, params.wpe, B, T, C);`，编码结果写入 `acts.encoded`，注释里写明「encoding goes into residual[0]」。

#### 4.1.4 代码实践

> 这是本讲的主实践任务，对应大纲里的 `practice_task`。

**目标**：手写一个最小 C 程序，给定 `wte(V,C)`、`wpe(maxT,C)` 和一段 token id，输出对应位置的嵌入向量，并验证它等于 `wte[ix] + wpe[t]`。

**操作步骤**：把下面这段**示例代码**（非项目原有代码，为方便验证而写）保存为 `mini_encoder.c`，然后用 `gcc mini_encoder.c -o mini_encoder -lm && ./mini_encoder` 编译运行：

```c
// 示例代码：最小 encoder_forward，验证 out[b,t,:] == wte[ix,:] + wpe[t,:]
#include <stdio.h>

void encoder_forward(float* out, int* inp, float* wte, float* wpe,
                     int B, int T, int C) {
    for (int b = 0; b < B; b++) {
        for (int t = 0; t < T; t++) {
            float* out_bt = out + b * T * C + t * C;
            int ix = inp[b * T + t];
            float* wte_ix = wte + ix * C;
            float* wpe_t  = wpe + t * C;
            for (int i = 0; i < C; i++) {
                out_bt[i] = wte_ix[i] + wpe_t[i];
            }
        }
    }
}

int main(void) {
    const int V = 5, maxT = 4, C = 3;          // 小尺寸，方便肉眼验证
    float wte[V*C], wpe[maxT*C];
    // wte[ix][i] = ix*10 + i；wpe[t][i] = t*100 + i，取整好认
    for (int ix = 0; ix < V; ix++)
        for (int i = 0; i < C; i++) wte[ix*C + i] = ix*10 + i;
    for (int t = 0; t < maxT; t++)
        for (int i = 0; i < C; i++) wpe[t*C + i] = t*100 + i;

    int B = 1, T = 3;
    int inp[3] = {2, 0, 4};                    // 一句话的 token id 序列
    float out[B*T*C];
    encoder_forward(out, inp, wte, wpe, B, T, C);

    for (int b = 0; b < B; b++)
        for (int t = 0; t < T; t++)
            for (int i = 0; i < C; i++) {
                int ix = inp[b*T + t];
                float expect = wte[ix*C + i] + wpe[t*C + i];
                float got    = out[b*T*C + t*C + i];
                printf("out[%d,%d,%d]=%.0f expect=%.0f %s\n",
                       b, t, i, got, expect, got == expect ? "OK" : "FAIL");
            }
    return 0;
}
```

**需要观察的现象 / 预期结果**：所有行都打印 `OK`。例如位置 `t=0` 的 token id 是 `2`，则

- `out[0,0,0] = wte[2,0] + wpe[0,0] = 20 + 0 = 20`
- `out[0,2,0] = wte[4,0] + wpe[2,0] = 40 + 200 = 240`

你可以把 `inp` 改成含重复 id 的序列（例如 `{2, 2, 4}`），观察同一个词在不同位置（位置向量不同）会得到**不同**的 `out`——这正是位置嵌入起作用的证据。如果你没有本地 C 编译器，可标注「待本地验证」并手工代算上述两个数值核对。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `wpe`（位置嵌入）整张表清零，模型还能区分「我爱你」和「你爱我」吗？为什么？

**答案**：基本不能。清零 `wpe` 后，`out[b,t,:]` 只依赖 token id 而与位置 `t` 无关；同一组词无论顺序如何，在每一层进入 attention 时都是同一组无序向量，模型失去了顺序信息（attention 本身对位置不敏感）。

**练习 2**：为什么用「相加」而不是「拼接」来融合词向量和位置向量？

**答案**：拼接会让通道数从 `C` 变成 `2C`，破坏了残差流维度恒定为 `C` 的设计，后续所有层的形状都要跟着改；相加则保持维度不变，最省事。

---

### 4.2 encoder_backward 的梯度累加

#### 4.2.1 概念说明

反向传播的任务是：已知损失对前向输出 `out` 的梯度 `dout`（形状 `(B,T,C)`），求损失对两个**参数** `wte`、`wpe` 的梯度 `dwte`、`dwpe`。

关键观察有两点：

1. **没有 `dinp`**。`inp` 是整数 token id，整数不可导，梯度不会回传到输入下标。所以 `encoder_backward` 的参数里根本没有 `dinp`，它只更新 `dwte`/`dwpe`。
2. **必须用 `+=` 累加**。前向是「**一对多**」的查表：同一个 token id `ix` 可能在 batch 的多个位置出现；同一个位置 `t` 会出现在每个 batch 样本里。这些位置的前向输出都「读」了同一行 `wte[ix]` / `wpe[t]`。因此反向时，每个位置都要把自己那份梯度**贡献**回这一行，全部加起来才是总梯度。这是 gather（查表）的逆运算——**scatter-add（散播累加）**。

用数学语言，设 \(S_{ix}=\{(b,t)\mid \text{inp}_{b,t}=ix\}\) 为所有「id 等于 ix」的位置集合，则：

\[ \frac{\partial L}{\partial \text{wte}_{ix, i}} = \sum_{(b,t)\in S_{ix}} \frac{\partial L}{\partial \text{out}_{b,t,i}}, \qquad \frac{\partial L}{\partial \text{wpe}_{t, i}} = \sum_{b} \frac{\partial L}{\partial \text{out}_{b,t,i}} \]

代码里直接用 `dwte_ix[i] += d` 实现这个求和。

> 还有一个让 `+=` 必不可少的理由：`wte` 是参数，它的梯度要在整个反向过程中跨层累加。事实上 `wte` 还兼职做了最后的「输出投影」（权重绑定 weight tying），`gpt2_backward` 里对 `grads.wte` 既写过一次（matmul 反向）又会被 `encoder_backward` 再 `+=` 一次（见 4.2.3）。两处都用 `+=`，加上每步开头 `gpt2_zero_grad` 把梯度清零，累加才正确。

#### 4.2.2 核心流程

```
for 每个样本 b in [0, B):
    for 每个时间步 t in [0, T):
        dout_bt  = dout[b, t, :]
        ix       = inp[b, t]
        dwte_ix  = dwte 的第 ix 行     # 注意：和前向查的是同一行！
        dwpe_t   = dwpe 的第 t 行
        for 每个通道 i in [0, C):
            d = dout_bt[i]
            dwte_ix[i] += d           # 散播累加到词嵌入
            dwpe_t[i]  += d           # 散播累加到位置嵌入
```

对比前向你会发现：**反向的循环结构与指针寻址和前向几乎一模一样**，只是把「读 wte/wpe 写 out」换成了「读 dout 累加写 dwte/dwpe」。这是手工反传的一个常见且舒服的规律。

#### 4.2.3 源码精读

- [train_gpt2.c:60-76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L60-L76) —— `encoder_backward` 全文。注意第 67 行 `float* dwte_ix = dwte + ix * C;` 与前向第 49 行 `wte_ix = wte + ix * C;` 寻址方式完全一致；第 71–72 行用 `+=` 把同一份 `d` 分别累加进 `dwte_ix[i]` 和 `dwpe_t[i]`。

它在反向主函数里被**最后**调用（与前向最先调用形成镜像）：

- [train_gpt2.c:1004](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1004) —— `encoder_backward(grads.wte, grads.wpe, grads_acts.encoded, model->inputs, B, T, C);`。注意第三个参数 `grads_acts.encoded` 正是前向 `out`（即 `acts.encoded`）对应的梯度 `dout`，第四个参数 `model->inputs` 是前向时缓存下来的 token id（见 4.3.3）。

权重绑定的「另一处」对 `grads.wte` 的写入：

- [train_gpt2.c:935](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L935) —— 最终 logits 的 `matmul_backward(..., grads.wte, ...)` 也往 `grads.wte` 里 `+=`。它与第 1004 行的 `encoder_backward` 共同累加，体现了 `wte` 同时是「输入嵌入」和「输出分类器」的双重身份。

#### 4.2.4 代码实践

**目标**：构造一个 token id 在两个位置重复出现的 batch，手算 `dwte`，直观感受「累加」。

**操作步骤**（源码阅读 + 纸笔计算型实践，无需编译）：

1. 设 `B=1, T=3, C=2`，`inp = {5, 5, 9}`（id `5` 在位置 0 和 1 各出现一次）。
2. 给定反向输入梯度 `dout = [[1,1],[10,10],[100,100]]`（即 `dout[0]=1,1`，`dout[1]=10,10`，`dout[2]=100,100`）。
3. 假设 `dwte` 初始全 0，按 `encoder_backward` 的规则累加。

**需要观察的现象 / 预期结果**：

- 位置 0：`ix=5`，`dwte[5] += (1,1)` → `dwte[5] = (1,1)`
- 位置 1：`ix=5`，`dwte[5] += (10,10)` → `dwte[5] = (11,11)`  ← 同一行被**累加**了两次！
- 位置 2：`ix=9`，`dwte[9] += (100,100)` → `dwte[9] = (100,100)`

`dwpe` 同理：位置 0/1/2 各只出现一次，故 `dwpe[0]=(1,1)`、`dwpe[1]=(10,10)`、`dwpe[2]=(100,100)`。

**结论**：若误把 `+=` 写成 `=`，`dwte[5]` 会只剩 `(10,10)`，位置 0 的梯度被覆盖丢失——这正是必须累加的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `encoder_backward` 没有 `dinp` 这个输出参数？

**答案**：`inp` 是整数 token id，不可导，链式法则不适用于「下标选择」这一步，因此没有梯度流向输入下标；反向只需更新两个浮点参数表 `dwte`/`dwpe`。

**练习 2**：如果整个 batch 里某个 token id 一次都没出现，它对应那一行 `dwte[ix]` 的值会是多少？

**答案**：全 0。因为没有位置「读」过这一行，自然没有梯度贡献；又因为每步 `gpt2_zero_grad` 先把 `dwte` 清零，未触及的行保持 0，该行参数本轮不更新。

---

### 4.3 指针算术与 `(B,T,C)` 布局

#### 4.3.1 概念说明

`train_gpt2.c` 不用任何张量库，所有多维数组都是「**一维 `float` 数组 + 指针算术**」模拟出来的。理解本层（以及后续每一层）的关键，就是看懂这四行地址计算：

```c
float* out_bt  = out + b * T * C + t * C;   // 跳到 out[b,t,:]
int    ix      = inp[b * T + t];            // 读出 inp[b,t] 这个整数
float* wte_ix  = wte + ix * C;              // 跳到 wte 的第 ix 行
float* wpe_t   = wpe + t * C;               // 跳到 wpe 的第 t 行
```

四个指针分别来自四张布局不同的表，但都遵循行主序规则：「想取第 `k` 行，就把基地址加上 `k * 每行元素数`」。理解了这一条，就理解了整段代码。

#### 4.3.2 核心流程

各张量的形状与「每行元素数」对照：

| 张量 | 形状 | 含义 | 「第 k 行」的起始地址 | 每行元素数 |
| --- | --- | --- | --- | --- |
| `out` | `(B,T,C)` | 编码输出 | `out + b*T*C + t*C` | `C` |
| `inp` | `(B,T)` | token id（整数） | `inp + b*T + t` | `1` |
| `wte` | `(V,C)` | 词嵌入表 | `wte + ix*C` | `C` |
| `wpe` | `(maxT,C)` | 位置嵌入表 | `wpe + t*C` | `C` |

注意 `inp` 是 `int*` 而不是 `float*`，且它的「行」只有一个元素（一个 id），所以寻址是 `inp + b*T + t`，不带 `*C`。

#### 4.3.3 源码精读

**前向的四处寻址**（本层的核心）：

- [train_gpt2.c:45-54](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L45-L54) —— 连续四行指针计算 + 一行相加，正好对应上表四行。第 45 行算 `out_bt`，第 47 行算 `ix`，第 49 行算 `wte_ix`，第 51 行算 `wpe_t`。

**这两张表是怎么「钉」进内存的**（承接 u1-l3 的 malloc-and-point 技巧）：

- [train_gpt2.c:538-539](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L538-L539) —— `ParameterTensors` 里 `wte`、`wpe` 是最前面的两个参数指针。
- [train_gpt2.c:561-562](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L561-L562) —— `wte` 大小为 `Vp*C`（`Vp` 是**填充后**词表 50304，而非 50257，目的是 128 对齐提速；多出的行不会被查到，因为 id 都在 `[0,V)` 内）；`wpe` 大小为 `maxT*C`。
- [train_gpt2.c:588-592](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L588-L592) —— `wte`、`wpe` 排在指针表最前，所以它们占据那一整块 `params_memory` 的**最前面**两段。
- [train_gpt2.c:748-749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L748-L749) —— `malloc_and_point_parameters` 分配好整块内存后，一次 `fread` 就把 16 个张量（含 `wte`/`wpe`）全部从 checkpoint 读进来。

**反向为什么能拿到正确的 id**：前向时 `gpt2_forward` 把输入 token 缓存到了 `model->inputs`，反向才能重新查回同一张表。

- [train_gpt2.c:816](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L816) —— `memcpy(model->inputs, inputs, B * T * sizeof(int));`，正是这一步让第 1004 行的 `encoder_backward(..., model->inputs, ...)` 能复现前向用过的 id。

#### 4.3.4 代码实践

**目标**：用具体数字熟悉「行主序」地址计算，为后续读懂更复杂的层打基础。

**操作步骤**（纸笔计算型）：设 `B=2, T=3, C=4`，即 `out` 形状 `(2,3,4)`，共 24 个浮点。

1. 计算 `out[1,2,:]`（第 2 个样本、第 3 个位置）的起始下标。
2. 设 `inp` 形状 `(2,3)`，计算 `inp[1,2]` 的下标。
3. 若 `wte` 形状 `(V,4)`、`inp[1,2]=7`，计算 `wte[7,:]` 的起始下标。

**需要观察的现象 / 预期结果**：

1. `out[1,2,:]` 下标 = `1*T*C + 2*C = 1*12 + 2*4 = 20`，即从第 20 号浮点开始的 4 个元素。
2. `inp[1,2]` 下标 = `1*T + 2 = 1*3 + 2 = 5`（注意是 `int` 数组，且不带 `*C`）。
3. `wte[7,:]` 下标 = `7*C = 7*4 = 28`。

**结论**：同一套「基地址 + 行号 × 每行元素数」的公式适用于所有张量，差别只在「每行元素数」和「元素类型（int/float）」。后续 matmul、attention 等层的寻址只是把这一套路用到更复杂的形状上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inp` 的寻址是 `inp + b*T + t`，而 `out` 是 `out + b*T*C + t*C`？

**答案**：`inp` 每个位置只存 1 个整数，所以「每行元素数」是 1，行跨步为 `T`；`out` 每个位置存 `C` 个浮点，行跨步为 `T*C`。差别完全来自每行元素数不同。

**练习 2**：`wte` 分配了 `Vp*C`（50304 行），但真实词表只有 `V=50257`。多出的 47 行会被查到吗？会有什么影响？

**答案**：不会被查到。`gpt2_forward` 在 [train_gpt2.c:782-783](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L782-L783) 断言所有 id 都在 `[0,V)` 内，所以多出的填充行从不被 gather；它们存在的唯一目的是让 `Vp` 对齐到 128 的倍数，使后续矩阵乘法更高效。反向时这些行也收不到梯度，保持不变。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端的小任务**（纸笔 + 选做编译）：

**场景**：设 `B=2, T=2, C=3`。

- 输入 `inp = [[3, 3], [3, 1]]`（注意 id `3` 在 batch 里出现了 **3 次**：样本 0 的位置 0、位置 1，以及样本 1 的位置 0）。
- 设词嵌入（只写出用到的行）：`wte[1] = (0,0,0)`，`wte[3] = (1,2,3)`。
- 设位置嵌入：`wpe[0] = (10,20,30)`，`wpe[1] = (40,50,60)`。
- 设反向梯度 `dout` 与 `out` 同形，令 `dout[0,0]=(1,0,0)`，`dout[0,1]=(0,1,0)`，`dout[1,0]=(0,0,1)`，`dout[1,1]=(0,0,0)`。

**要求**：

1. **前向**：手算 `out` 的全部 12 个元素，验证每个都等于 `wte[id] + wpe[t]`。例如 `out[0,0] = wte[3] + wpe[0] = (1+10, 2+20, 3+30) = (11,22,33)`。
2. **布局**：写出 `out[1,0,:]` 在一维数组里的起始下标（答案：`1*T*C + 0*C = 6`）。
3. **反向**：从全 0 的 `dwte`、`dwpe` 出发，按 `encoder_backward` 的 `+=` 规则累加，求出 `dwte[1]`、`dwte[3]`、`dwpe[0]`、`dwpe[1]`。

**参考答案**：

- 前向 `out`：
  - `out[0,0] = wte[3]+wpe[0] = (11,22,33)`
  - `out[0,1] = wte[3]+wpe[1] = (41,52,63)`
  - `out[1,0] = wte[3]+wpe[0] = (11,22,33)`
  - `out[1,1] = wte[1]+wpe[1] = (40,50,60)`
- 反向累加（把每个位置贡献的 `dout` 加到对应行）：
  - `dwte[3]` 收到来自 `(0,0)`、`(0,1)`、`(1,0)` 三处 = `(1,0,0)+(0,1,0)+(0,0,1) = (1,1,1)`
  - `dwte[1]` 只收到 `(1,1)` = `(0,0,0)`
  - `dwpe[0]` 收到 `(0,0)`、`(1,0)` 两处（位置 0 出现两次）= `(1,0,0)+(0,0,1) = (1,0,1)`
  - `dwpe[1]` 收到 `(0,1)`、`(1,1)` 两处 = `(0,1,0)+(0,0,0) = (0,1,0)`

这个任务一次性验证了「查表相加」「行主序寻址」「`+=` 累加」三件事：`dwte[3]` 之所以是 `(1,1,1)` 而不是某个单份梯度，正是因为同一个 id `3` 在多处出现、梯度被**累加**。

> 进阶（可选）：把 4.1.4 的示例程序扩展成「先跑前向，再给定 `dout` 跑反向，打印 `dwte`/`dwpe`」，并与上面的手算结果对照。

---

## 6. 本讲小结

- encoder 是 Transformer 的入口：把 `(B,T)` 的整数 token id 变成 `(B,T,C)` 的浮点嵌入，产出 `acts.encoded` 即残差流的第 0 层。
- 前向 = **查表 + 相加**：`out[b,t,:] = wte[inp[b,t],:] + wpe[t,:]`；词嵌入给词义，位置嵌入给顺序，两者逐元素相加。
- 反向 = **scatter-add（散播累加）**：因为同一个 id/位置会被多处读取，梯度必须用 `+=` 累加回 `dwte`/`dwpe`；`inp` 是整数不可导，所以没有 `dinp`。
- 全部多维数组都是「一维数组 + 行主序指针算术」，核心公式是「基地址 + 行号 × 每行元素数」。
- `wte` 还兼职最终输出投影（权重绑定），所以它的梯度会被 `matmul_backward` 和 `encoder_backward` 两处共同 `+=`，依赖每步开头的 `gpt2_zero_grad` 清零来保证累加正确。
- `wte` 按 `Vp=50304`（128 对齐）分配，真实词表 `V=50257`，多出的填充行永远不会被查到。

---

## 7. 下一步学习建议

本讲只看了 encoder 这一层，且它的输入是「干净的整数 id」。接下来推荐：

- **u2-l2 LayerNorm**：encoder 产出的残差流进入第一个 Transformer block 时，第一道工序就是 LayerNorm。它会引入「均值/方差/rstd 缓存」和更复杂的反向推导，是理解后续每层反向的好跳板。
- **u2-l3 MatMul**：`wte` 还在最终 logits 处以矩阵乘法的形式出现（权重绑定），读懂 matmul 能让你彻底理解 `wte` 的双重身份。
- **顺手阅读**：`doc/layernorm/layernorm.c` 的 `main()` 写法（读 `.bin` 参考数据 + `check_tensor` 比对），可以借鉴到你为 encoder 写的独立验证程序里。

阅读建议：先把本讲「`+=` 为什么不能写成 `=`」和「四个指针怎么算」这两点彻底吃透，再进入下一层——它们会在后面每一层的反向里反复出现。
