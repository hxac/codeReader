# 采样与自回归生成

## 1. 本讲目标

训练完成（或加载预训练权重）之后，GPT-2 怎么「写出文字」？本讲只回答这个问题，聚焦三个最小模块：

1. 一个极简伪随机数发生器 `random_f32`（xorshift* 算法），负责抛出落在 \([0,1)\) 的「硬币」。
2. 一个多项分布采样器 `sample_mult`，用这枚硬币在词表的概率分布上「落点」选出下一个 token。
3. `main` 里的自回归（autoregressive）生成循环：每生成一个 token，就把整段序列重新喂进 `gpt2_forward`，再采下一个。

学完后你应该能：

- 说清「抛硬币落到累计概率区间」这种逆 CDF 采样为什么能还原原始概率分布；
- 看懂 xorshift* 那几行位运算和「右移 8 位除以 \(2^{24}\)」如何把一个 64 位状态变成 \([0,1)\) 的 float；
- 解释生成循环里为何 `t` 从 1 开始、为何只看 `probs[0, t-1, :]`、为何每个 token 都要把整段重算一遍前向。

本讲只讲「采样与生成」，不涉及反向传播与优化（那是 u3-l1、u3-l2 的内容），也不再展开前向各层算法（u2 已讲透）。

## 2. 前置知识

- **下一个 token 预测**：GPT-2 的训练目标就是「给定前 \(t\) 个 token，预测第 \(t+1\) 个 token」（u1-l4、u2-l6）。因此 `probs[b, t, :]` 这一行概率，建模的就是「在第 \(b\) 条序列、位置 \(t\) 之后，下一个 token 的分布」。生成时我们就从这个分布里抽一个 token 当作「模型的续写」。
- **probs 的内存布局**：`acts.probs` 是形状 \((B, T, V_p)\) 的行主序一维数组（u2-l6、u2-l7）。其中 \(V=50257\) 是真实词表大小，\(V_p=50304\) 是为对齐而填充后的词表大小，填充区 \([V, V_p)\) 被 `softmax_forward` 显式清零。
- **targets==NULL 的前向**：`gpt2_forward` 既服务训练（传入 `targets` 算交叉熵 loss）也服务生成（传 `NULL`，loss 用 \(-1.0\) 哨兵占位，但 `probs` 照常算出）。本讲用到的是后者。
- **概率积分变换（probability integral transform）**：若 \(u\sim\text{Uniform}[0,1)\)，则用累计分布函数（CDF）「反查」能还原任意离散分布——这是 `sample_mult` 的数学基础，下面会展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.c` | CPU 参考实现。采样三件套（`random_u32`/`random_f32`/`sample_mult`）直接内联定义，`main` 里的生成循环也在其中。本讲主角。 |
| `llmc/sampler.h` | CUDA 主线用的采样器头文件。定义了**同样的** `random_u32`/`random_f32`（xorshift*），以及一个从 logits 直接采样的 `sample_softmax`（与 CPU 版的 `sample_mult` 对照）。 |
| `llmc/rand.h` | **Mersenne Twister（mt19937）** 伪随机数发生器，刻意与 PyTorch 数值完全一致。注意：它**不是**生成时用的 RNG，而是给 `dataloader.h` 做 shuffle、给测试做可复现随机数用的。本讲会把它和 `random_f32` 区分清楚，避免混淆。 |

> 小提示：CPU 参考实现 `train_gpt2.c` 是单文件、自包含的，它**没有** `#include "llmc/sampler.h"`，而是把 `random_u32`/`random_f32`/`sample_mult` 又抄了一遍。两处的 `random_u32`/`random_f32` 逐字节相同，作者这样做是为了让 CPU 单文件能独立编译、便于教学。

## 4. 核心概念与源码讲解

### 4.1 随机数生成 random_f32：xorshift\* 伪随机

#### 4.1.1 概念说明

采样需要一个「够均匀、够随机」的 \([0,1)\) 浮点数作为输入。llm.c 没有用标准库的 `rand()`，而是自带了一个极小、确定性的伪随机数发生器（PRNG）：**xorshift\***。

它的状态只有一个 64 位整数 `state`（在 `main` 里初值为 `1337`）。每次调用都用三次异或移位 + 一次常数乘法把状态「搅乱」，再从中取出 32 位作为输出。它的优点是：状态小（8 字节）、速度快（几条位运算）、可复现（同一种子产生同一序列）。对于一个「只是做 sanity check 文本生成」的循环，这种强度完全够用。

> ⚠️ 别和 `llmc/rand.h` 搞混。`llmc/rand.h` 实现的是 **Mersenne Twister（mt19937）**，注释明确写着「numerically identical to torch」——它存在的目的是让 `dev/` 测试和数据加载（`llmc/dataloader.h` 在 [llmc/dataloader.h:18](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L18) 处 `#include "rand.h"`）和 PyTorch 的随机序列逐位一致，便于正确性比对。**生成时的采样用的是 xorshift\* 的 `random_f32`，不是 Mersenne Twister。**

#### 4.1.2 核心流程

xorshift\* 的递推写成公式是：

\[
s \leftarrow s \oplus (s \gg 12)
\]
\[
s \leftarrow s \oplus (s \ll 25)
\]
\[
s \leftarrow s \oplus (s \gg 27)
\]
\[
\text{out} = (s \cdot 0\text{x}2545\text{F}4914\text{F}6\text{CDD}1\text{D}) \gg 32
\]

前三行是三次「异或移位」（xor + shift，故名 xorshift），把状态充分打散；第四行再乘一个固定的奇常数（xorshift**\*** 里的那次乘法，能显著改善低位的统计质量），取 64 位乘积的高 32 位作为输出 `out`。

再把 `out` 变成 \([0,1)\) 的 float：

\[
\text{random\_f32} = \frac{\text{out} \gg 8}{2^{24}}
\]

- `out >> 8` 取的是 32 位里的**高 24 位**（丢掉低 8 位）。
- 除以 \(2^{24} = 16777216\)，把整数区间 \([0, 2^{24}-1]\) 线性映射到 \([0,1)\)。
- 选 24 位恰好对应 float32 的 24 位尾数精度，不多不少；取高位是因为低位通常随机性更弱。

#### 4.1.3 源码精读

CPU 参考实现的 RNG 定义在 [train_gpt2.c:1051-1059](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1051-L1059)：

```c
unsigned int random_u32(uint64_t *state) {
    // xorshift rng: https://en.wikipedia.org/wiki/Xorshift#xorshift.2A
    *state ^= *state >> 12;
    *state ^= *state << 25;
    *state ^= *state >> 27;
    return (*state * 0x2545F4914F6CDD1Dull) >> 32;
}
float random_f32(uint64_t *state) { // random float32 in [0,1)
    return (random_u32(state) >> 8) / 16777216.0f;
}
```

逐行对应上面的公式：三次异或移位、乘常数取高 32 位、再右移 8 位除以 \(2^{24}\)。注意 `state` 通过指针传入并在函数内被原地修改，所以每次调用都会推进序列。

`sampler.h` 里有一份**逐字节相同**的拷贝，见 [llmc/sampler.h:10-20](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/sampler.h#L10-L20)，供 CUDA 主线 `train_gpt2.cu` 复用。

对照看一下 Mersenne Twister 的入口（不是生成用，仅供对比），见 [llmc/rand.h:106-146](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/rand.h#L106-L146)：它有 624 个 32 位状态的庞大内部数组（`MERSENNE_STATE_N`）、`next_state()` 的 twist 步骤、以及 tempering 的四次位运算——复杂度远高于 xorshift\*，正是为了达到与 PyTorch 逐位一致的质量。

#### 4.1.4 代码实践

**实践目标**：亲手验证「同一种子产生同一序列」，并确认 `random_f32` 输出确实落在 \([0,1)\)。

**操作步骤**（示例代码，不依赖模型权重，可单独编译运行）：

```c
// 示例代码：验证 xorshift* 的可复现性与值域
#include <stdio.h>
#include <stdint.h>

unsigned int random_u32(uint64_t *state) {
    *state ^= *state >> 12;
    *state ^= *state << 25;
    *state ^= *state >> 27;
    return (*state * 0x2545F4914F6CDD1Dull) >> 32;
}
float random_f32(uint64_t *state) {
    return (random_u32(state) >> 8) / 16777216.0f;
}

int main() {
    uint64_t s = 1337;            // 与 train_gpt2.c main 里同样的种子
    float lo = 1.0f, hi = 0.0f;
    for (int i = 0; i < 100000; i++) {
        float r = random_f32(&s);
        if (r < lo) lo = r;
        if (r > hi) hi = r;
    }
    printf("min=%f max=%f\n", lo, hi);
    return 0;
}
```

编译运行：`gcc demo.c -o demo && ./demo`。

**需要观察的现象**：`min` 始终 \(\ge 0\)，`max` 始终 \(< 1\)，印证值域是 \([0,1)\)。

**预期结果**：`min` 接近 0（如 `0.0000xx`）、`max` 接近但小于 1（如 `0.9999xx`）。若你把种子改回 `1337` 重跑，输出应完全一致（可复现）。待本地验证具体数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `random_f32` 要 `>> 8` 取高 24 位，而不是直接用 `random_u32` 除以 \(2^{32}\)？

**参考答案**：取高位是为了避开低位通常较弱的随机性；选 24 位则正好匹配 float32 的尾数精度，再多也存不下。

**练习 2**：如果把 `main` 里的 `uint64_t rng_state = 1337;` 改成别的值，生成的文本会不会变？为什么？

**参考答案**：会变。种子决定 xorshift\* 的整条输出序列，进而决定每次采样抛出的「硬币」，所以同一模型、不同种子会生成不同的续写（但统计分布不变）。

---

### 4.2 多项采样 sample_mult：用一枚硬币选 token

#### 4.2.1 概念说明

`gpt2_forward` 给出当前位置的 `probs`——一个长度为 \(V\) 的概率向量，和为 1。我们需要「按这个分布抽一个 token 出来」。这个操作叫**多项分布采样（multinomial sampling）**，即从一个有限离散分布里抽一个样本。

`sample_mult` 用的方法是**逆累积分布函数法（inverse CDF method）**，也叫「抛硬币落区间」。直觉是：把 \([0,1)\) 区间按各 token 的概率切成首尾相接的小段，第 \(i\) 段长度等于 \(p_i\)；然后抛一枚均匀硬币 `coin` 落在 \([0,1)\)，它落在哪一段，就选中哪个 token。因为每段长度恰好是 \(p_i\)，硬币又是均匀的，所以选中 \(i\) 的概率就是 \(p_i\)——完美还原原始分布。

#### 4.2.2 核心流程

设分布为 \(p_0, p_1, \dots, p_{n-1}\)，累计分布函数（CDF）定义为：

\[
\text{CDF}_k = \sum_{i=0}^{k} p_i, \qquad \text{CDF}_{-1}=0
\]

给定硬币 \(u\sim\text{Uniform}[0,1)\)，返回最小的 \(k\) 满足：

\[
u < \text{CDF}_k
\]

那么：

\[
P(\text{返回 } k) = P(\text{CDF}_{k-1} \le u < \text{CDF}_k) = \text{CDF}_k - \text{CDF}_{k-1} = p_k
\]

这正是概率积分变换。用伪代码描述：

```
cdf = 0
for i in 0..n-1:
    cdf += p[i]
    if coin < cdf:
        return i
return n-1   # 浮点累加误差兜底
```

举个迷你例子：词表 `["hello", "world", "!"]`，`probs = [0.5, 0.3, 0.2]`，则 CDF 序列为 `[0.5, 0.8, 1.0]`。
- `coin` 落在 \([0, 0.5)\) → 选 `"hello"`；
- 落在 \([0.5, 0.8)\) → 选 `"world"`；
- 落在 \([0.8, 1.0)\) → 选 `"!"`。

> 对照 CUDA 主线：`llmc/sampler.h` 里的 [sample_softmax](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/sampler.h#L22-L39) 思路相同，但它**直接吃 logits**：先算 \(Z=\sum_i e^{\text{logits}_i}\)，再把 `coin *= Z`，于是「累加 \(e^{\text{logits}_i}\) 直到超过 `coin`」就等价于在 softmax 后的概率上做逆 CDF——省掉了显式归一化那一步。两者统计上等价，CPU 版复用前向已算好的 `probs`，CUDA 版从 logits 重算一次（因为 CUDA 主线前向并不会无条件算出完整 `probs`）。

#### 4.2.3 源码精读

`sample_mult` 在 [train_gpt2.c:1062-1073](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1062-L1073)：

```c
int sample_mult(float* probabilities, int n, float coin) {
    // sample index from probabilities (they must sum to 1!)
    // coin is a random number in [0, 1), usually from random_f32()
    float cdf = 0.0f;
    for (int i = 0; i < n; i++) {
        cdf += probabilities[i];
        if (coin < cdf) {
            return i;
        }
    }
    return n - 1; // in case of rounding errors
}
```

注意三个要点：

1. 注释明确要求传入的 `probabilities` 必须「和为 1」——这是逆 CDF 法的前提，由 `softmax_forward` 保证。
2. 用的是严格小于 `coin < cdf`，配合 `coin ∈ [0,1)` 与最后一个 `cdf ≈ 1.0`，理论上总会命中某一段。
3. 末尾 `return n - 1` 是**浮点累加误差兜底**：万一 `cdf` 因多次相加比 1.0 略小、而 `coin` 又恰好非常接近 1，循环可能一次都不命中，这时退回最后一个 token。

`n` 在调用处传的是 `model.config.vocab_size`（即真实词表 \(V\)，不是填充后的 \(V_p\)），见 [train_gpt2.c:1147](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1147)。这保证绝不会采样到填充区里的「假 token」——反正那里 `softmax_forward` 已经清零了。

#### 4.2.4 代码实践

**实践目标**：验证 `sample_mult` 在大量重复下确实还原原始概率分布。

**操作步骤**（示例代码，接 4.1.4 的 RNG，可独立编译运行）：

```c
// 示例代码：验证 sample_mult 还原多项分布
#include <stdio.h>
#include <stdint.h>

unsigned int random_u32(uint64_t *state) {
    *state ^= *state >> 12; *state ^= *state << 25; *state ^= *state >> 27;
    return (*state * 0x2545F4914F6CDD1Dull) >> 32;
}
float random_f32(uint64_t *state) { return (random_u32(state) >> 8) / 16777216.0f; }
int sample_mult(float* p, int n, float coin) {
    float cdf = 0.0f;
    for (int i = 0; i < n; i++) { cdf += p[i]; if (coin < cdf) return i; }
    return n - 1;
}

int main() {
    float probs[] = {0.5f, 0.3f, 0.2f};          // 必须和为 1
    const char* vocab[] = {"hello", "world", "!"};
    int n = 3, counts[3] = {0,0,0};
    uint64_t state = 1337;
    for (int i = 0; i < 100000; i++) {
        int idx = sample_mult(probs, n, random_f32(&state));
        counts[idx]++;
    }
    for (int i = 0; i < n; i++)
        printf("%s: %d (%.3f)\n", vocab[i], counts[i], counts[i]/100000.0f);
    return 0;
}
```

**需要观察的现象**：三个 token 的频率应分别接近 `0.5 / 0.3 / 0.2`。

**预期结果**：例如 `hello` 约 0.500、`world` 约 0.300、`!` 约 0.200（误差随采样次数增大而减小）。待本地验证具体数值。

#### 4.2.5 小练习与答案

**练习 1**：如果 `probs` 不小心没归一化（比如和为 1.5），`sample_mult` 会怎样？

**参考答案**：由于 `cdf` 会更早超过 1.0，而 `coin<1`，所以排在后面的 token 几乎永远抽不到，采样分布被严重偏向前面几个 token。这就是注释强调「they must sum to 1」的原因。

**练习 2**：为什么兜底返回 `n-1` 而不是报错？

**参考答案**：这只在浮点累加导致 `cdf` 比 1.0 略小、`coin` 又极接近 1 的极端情形触发，概率极低；返回最后一个 token 是一种安全、不中断生成的退化策略。

---

### 4.3 自回归生成循环：逐 token 重算前向

#### 4.3.1 概念说明

「自回归（autoregressive）」生成指：模型每一步只产出一个 token，再把这个新 token 拼回序列末尾，作为下一步的输入，如此循环。GPT-2 这类 decoder-only 模型天生适合这样做——它本来就是按「看前文、预测下一个」训练的。

llm.c 的生成循环非常朴素，朴素到作者自己都标注了「很浪费」：**每生成一个 token，就把整段 \(B\times T\) 序列从头重新跑一遍前向**，而没有任何「KV cache」之类的增量优化。这样做的好处是代码极简、与训练前向完全共用同一个 `gpt2_forward`；代价是计算量随生成长度平方增长。对一个「仅用于 sanity check、看看模型有没有学到东西」的循环来说，这点浪费完全可以接受。

#### 4.3.2 核心流程

生成的整体流程：

1. **准备种子**：把长度为 \(B\times T\) 的 `gen_tokens` 全部填成 `<|endoftext|>`（EOT）token。EOT 是 GPT-2 里表示「一段文本结束」的特殊标记，用它作起点相当于告诉模型「请无条件开始一段新文本」（u1-l4、u3-l3 都提到过这个约定）。
2. **逐 token 循环**：`for (t = 1; t < genT; t++)`。从 \(t=1\) 开始，因为 `gen_tokens[0]` 是 EOT 种子，要采的是位置 1 之后的 token；`genT=64` 即最多生成 63 个 token。
3. **每步重算前向**：调用 `gpt2_forward(&model, gen_tokens, NULL, B, T)`，`targets` 传 `NULL` 表示「只算 `probs`、不算 loss」。
4. **取分布、抛硬币、采样**：取位置 \(t-1\) 处的概率向量 `probs[0, t-1, :]`，抛一枚 `coin`，用 `sample_mult` 选出 `next_token`。
5. **写回并打印**：`gen_tokens[t] = next_token`，再用 `tokenizer_decode` 把 token id 解码成文字打印。

用伪代码概括：

```
fill gen_tokens with EOT
for t in 1..genT-1:
    gpt2_forward(model, gen_tokens, NULL)      # 重算整段前向
    probs = acts.probs[0, t-1, :]              # 预测下一个 token 的分布
    coin   = random_f32(rng_state)
    next   = sample_mult(probs, V, coin)
    gen_tokens[t] = next
    print(tokenizer_decode(next))
```

#### 4.3.3 源码精读

**准备种子与 RNG** 在 [train_gpt2.c:1103-1106](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1103-L1106)：

```c
uint64_t rng_state = 1337;
int* gen_tokens = (int*)mallocCheck(B * T * sizeof(int));
const int genT = 64; // number of steps of inference we will do
```

`rng_state` 初值 `1337` 正是 4.1 里 xorshift\* 的种子；`genT=64` 是生成长度上限。

**生成块整体** 在 [train_gpt2.c:1126-1160](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1126-L1160)，由 `step % 20 == 0` 触发。先填 EOT（[train_gpt2.c:1127-1130](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1127-L1130)）：

```c
// fill up gen_tokens with the GPT2_EOT, which kicks off the generation
for(int i = 0; i < B * T; ++i) {
    gen_tokens[i] = tokenizer.eot_token;
}
```

**自回归循环的核心几行** 在 [train_gpt2.c:1138-1148](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1138-L1148)：

```c
gpt2_forward(&model, gen_tokens, NULL, B, T);
...
float* probs = model.acts.probs + (t-1) * model.config.padded_vocab_size;
float coin = random_f32(&rng_state);
int next_token = sample_mult(probs, model.config.vocab_size, coin);
gen_tokens[t] = next_token;
```

逐行解释三个关键点：

- **为何重算前向**：作者在 [train_gpt2.c:1133-1137](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1133-L1137) 的注释里写得很直白——「inference is very wasteful here because for each token we re-calculate the forward pass for all of (B,T) positions from scratch」，并说这只是 sanity check、以后或许优化。原因是没有 KV cache：要拿到位置 \(t-1\) 处的预测，就必须把 \(0..t-1\) 这些 token 全部重新喂进网络。
- **为何只取 `probs[0, t-1, :]`**：`probs` 布局是 \((B,T,V_p)\) 行主序，元素 \((b,t',v)\) 的偏移是 `b*T*Vp + t'*Vp + v`。这里取 \(b=0\)、\(t'=t-1\)，偏移就是 `(t-1)*Vp`，所以代码写 `probs + (t-1)*padded_vocab_size`。两层含义：
  - **\(b=0\)**：\(B=4\) 意味着 4 条序列并行生成（全用 EOT 起头），但打印只跟第 0 条（注释 [train_gpt2.c:1139-1141](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1139-L1141) 说明「we're only using b=0」）。
  - **\(t'=t-1\)**：GPT-2 是「下一个 token 预测」——位置 \(t-1\) 的输出分布预测的正是位置 \(t\) 的 token。所以要填位置 \(t\)，就读位置 \(t-1\) 的 `probs`。
- **为何传 `vocab_size` 而非 `padded_vocab_size`**：注释 [train_gpt2.c:1145-1146](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1145-L1146) 解释——只在真实词表 \(V\) 上采样、忽略填充；填充区本就被 `softmax_forward` 清零（见 [train_gpt2.c:479-481](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L479-L481)）。

**解码打印** 在 [train_gpt2.c:1149-1157](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1149-L1157)：用 `tokenizer_decode` + `safe_printf` 把 token id 转成文字；若 tokenizer 加载失败则退化打印数字 id。`safe_printf` 会过滤 BPE 词表里的控制字节，避免乱码破坏终端（u1-l4 已介绍）。

> **与 CUDA 主线对照**：`train_gpt2.cu` 的生成循环（[train_gpt2.cu:1762-1795](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1762-L1795)）逻辑相同，但有两点差异：其一，它把 `probs`（实为 logits）从 GPU 拷回 CPU 再采样（[train_gpt2.cu:1776-1783](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1776-L1783)），并用 `sample_softmax` 直接从 logits 采样；其二，它做了个小优化——不总是算满 \(T\)，而是 `CEIL_DIV(t, min(T,256)) * min(T,256)`（[train_gpt2.cu:1772](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1772)）以减少浪费，但注释也坦言仍无 KV cache、依旧浪费。

#### 4.3.4 代码实践

**实践目标**：亲手跑通 CPU 版生成、看到真实输出文本；并理解 `probs[0, t-1, :]` 的取址含义。

**操作步骤**（属于「源码阅读 + 运行」型实践，需要 starter pack）：

1. 按 u1-l2 的说明，先运行 `./dev/download_starter_pack.sh` 下载 `gpt2_124M.bin`、`gpt2_tokenizer.bin` 与数据。
2. `make train_gpt2` 编译 CPU 版。
3. `OMP_NUM_THREADS=4 ./train_gpt2` 运行，关注每 20 步打印一次的 `generating:` 块。
4. 对照 [train_gpt2.c:1143](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1143) 那行 `probs + (t-1) * padded_vocab_size`，在纸上写出 \(b=0, t'=t-1\) 的偏移推导：`offset = 0*T*Vp + (t-1)*Vp + v = (t-1)*Vp + v`，确认它确实定位到 `probs[0, t-1, :]`。

**需要观察的现象**：`generating:` 块会打印一段由 EOT 起头的英文文本；随着训练步数增加（loss 下降），文本会从「乱码」逐渐变得有点像英文/莎士比亚风格。运行会比较慢——因为每个 token 都重算一遍 \(4\times64\) 的前向，这正是 4.3.1 说的「浪费」。

**预期结果**：能看到若干行生成文本；初期可能不通顺，训练若干步后可读性提升。**待本地验证**具体文本（取决于数据集是 tiny_shakespeare 还是 tiny_stories）。

> 若本地无法下载 starter pack，可退化为纯阅读型实践：把 [train_gpt2.c:1138-1148](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1138-L1148) 逐行注释，向自己讲清楚「为何 `t` 从 1 开始、为何看 `t-1`、为何只取 `b=0`」。

#### 4.3.5 小练习与答案

**练习 1**：循环为何是 `for (t = 1; t < genT; t++)` 而不是从 `t = 0` 开始？

**参考答案**：`gen_tokens[0]` 已经被填成 EOT 作为种子。要采的第一个新 token 是位置 1，所以循环从 \(t=1\) 起；同时位置 \(t\) 的预测依赖位置 \(t-1\) 的 `probs`，\(t=0\) 时 \(t-1=-1\) 越界，故也不能从 0 起。

**练习 2**：如果要把「每步重算前向」改成「只算必要位置」以省时间，最大的障碍是什么？

**参考答案**：注意力层会把当前位置与**所有历史位置**的信息混合（u2-l4），所以位置 \(t\) 的输出依赖 \(0..t\) 的全部 K/V。要增量计算就必须缓存历史 K/V（即业界常说的 KV cache），而当前实现没有这个缓存，只能每次从头重算。这正是注释提到的未来优化方向。

## 5. 综合实践

把本讲三个模块串起来：**亲手组装一个最小「分布 → 采样 → 拼接」的生成器**，模拟 GPT-2 的自回归流程（用一个写死的概率表代替真实模型）。

任务：写一段 C 程序，维护一个长度 `T=16` 的 `gen_tokens` 数组（初值全设为一个表示 EOT 的 id，比如 `0`）；用一个固定的「假概率分布」函数 `fake_probs(prev_token, probs, V)`（你可以简单实现为「对每个候选 token 给个固定概率，或让 `probs[prev_token]=0.6`、其余均分」）替代 `gpt2_forward`；然后用本讲学到的 `random_f32` + `sample_mult` 逐 token 采样、写回 `gen_tokens[t]`，最后打印整个序列。

要求：

1. 复用 4.1.4 的 xorshift\* `random_f32` 与 4.2.4 的 `sample_mult`，不要重新实现。
2. 循环结构与 [train_gpt2.c:1133-1158](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1133-L1158) 保持一致：先填 EOT、`for t in 1..T-1`、每步「取分布 → 抛硬币 → 采样 → 写回」。
3. 把 `V` 设为一个小词表（如 8），把 id 映射到可读字符（如 `'a'+id`）打印，直观看出生成结果。

完成后再回答：你的实现里，`probs` 是按 `t-1` 取的（对应「预测位置 \(t\)」）吗？如果是，说明你已经理解了「下一个 token 预测」如何驱动自回归生成。

> 本任务为「示例代码型」实践，可在不下载任何模型权重的情况下编译运行，用来巩固对采样与生成循环的理解。

## 6. 本讲小结

- 生成用的 RNG 是极简的 **xorshift\***（`random_u32`/`random_f32`），单个 64 位状态、几条位运算、可复现；`random_f32` 用 `(u>>8)/2^24` 把它压到 \([0,1)\) 的 float。
- `sample_mult` 用**逆 CDF 法**：累加概率直到超过一枚 \([0,1)\) 的「硬币」，选中 token；数学上 `P(返回 i) = p_i`，所以能还原原始分布，兜底返回 `n-1` 防浮点误差。
- `llmc/rand.h` 的 **Mersenne Twister** 是另一套 RNG，为与 PyTorch 逐位一致而存在，服务于数据 shuffle 和测试，**不参与**生成采样——两者不要混淆。
- 自回归生成循环：先填 EOT 作种子，再 `for t=1..genT-1`，每步对整段 \(B\times T\) **重算前向**（无 KV cache，故浪费，但够 sanity check）。
- 关键取址 `probs[0, t-1, :]` 同时编码了三件事：只跟第 0 条序列（\(b=0\)）、用位置 \(t-1\) 的分布预测位置 \(t\)（下一个 token 预测）、只在真实词表 \(V\) 上采样、忽略填充区。
- CPU 版（`sample_mult` 吃 `probs`）与 CUDA 主线（`sample_softmax` 吃 logits）统计等价，是同一思路在两套实现里的不同切法。

## 7. 下一步学习建议

- **u3-l4 数值正确性测试**：`test_gpt2.c` 会加载 `gpt2_124M_debug_state.bin` 跑前向、训练 10 步比对 loss，正好接着本讲的 `gpt2_forward(..., NULL, ...)` 生成路径，去看 `gpt2_forward(..., targets, ...)` 的训练路径如何被严格校验。
- **u4-l1 PyTorch 参考 train_gpt2.py**：去看 nanoGPT 风格的采样与 `torch.multinomial` / `F.softmax` 如何等价于本讲的 `sample_mult`，对照「框架自动做了什么」。
- **延伸阅读**：若想理解「为何重算前向很浪费、KV cache 如何省」，可在掌握 u5 的 CUDA attention kernel 后，自行思考 `attention_forward` 中 `(B,NH,T,T)` 的 `preatt`/`att` 矩阵如何被复用——这是日后实现高效推理的入口。
