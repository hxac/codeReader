# 采样与解码策略

## 1. 本讲目标

在前几讲里，我们已经看到 `prefill` 用 argmax 选出「第一个 token」，而 `generate` 的自回归主循环把「选出下一个 token」的工作交给了 `llm_select_next_token`。本讲就把这个「选 token」的黑盒彻底打开。

学完本讲你应该能够：

- 读懂 `sampling.cpp` 四个自由函数（`softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs`）的实现细节与它们之间的契约；
- 理解带温度 softmax 的数值稳定写法、top_k / top_p「截断但不重归一化」的设计、以及 `std::discrete_distribution` 为何能吃未归一化的概率；
- 看懂 `llm_select_next_token` 如何先做 **repetition penalty**、再串起上面的四个函数；
- 区分本项目里**两套采样实现**：基类 `ncnn_llm_base::sample_logits`（服务 NLLB 自跑解码循环）与共享运行时 `llm_select_next_token`（服务 LLM/VLM/OCR/ASR），并理解它们的随机源差异。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l4（generate 自回归解码主循环）**：知道每一步的数据流是 embed → decoder(KV cache) → lm_head(logits) → 采样，以及 `GenerateConfig` 的存在。
- **u2-l1 / u2-l2**：知道基类 `ncnn_llm_base` 与共享文本运行时 `llm_select_next_token` 各自的位置。

下面用通俗语言补几个采样领域的基础概念。

**logits 与概率。** 语言模型最后一层输出的是一个长度为词表大小 \(V\) 的实数向量，叫 logits。它本身不是概率。要变成概率，最常见的方式是 softmax：

\[
p_i = \frac{\exp(z_i)}{\sum_j \exp(z_j)}
\]

**贪心（greedy）解码。** 直接取 logits 最大的那个 token：\( \arg\max_i z_i \)。它确定性强、可复现，但容易陷入重复、缺乏多样性。

**温度（temperature）。** 在 softmax 里给 logits 除一个 \(T>0\)：

\[
p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)}
\]

- \(T\to 0\)：分布变「尖」，趋向贪心；
- \(T=1\)：标准 softmax；
- \(T>1\)：分布变「平」，更随机、更多样。

**top_k。** 只在概率最大的 \(k\) 个 token 里采样，其余全部置 0。这是对「候选集大小」的硬截断。

**top_p（nucleus sampling，核采样）。** 把 token 按概率从大到小排序，累加直到累计概率达到 \(p\)，只在这个最小的「核」集合里采样。这是对「累计概率」的软截断，候选个数会随分布形态动态变化。

**重复惩罚（repetition penalty）。** 对「已经出现在历史里的 token」的 logits 做缩放，降低它们再次被选中的概率，缓解「我爱你我爱你我爱你」式的复读。

本讲要讲的就是上面这些策略在源码里的真实实现。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/sampling.h` | 声明四个采样自由函数（一行一个），是本讲的主角。 |
| `src/sampling.cpp` | 四个函数的实现，外加一个**全局随机数源** `static std::mt19937 rng`。 |
| `src/ncnn_text_runtime.h` | 定义 `LlmTokenSampleConfig`（采样配置，含 repetition_penalty）与 `llm_select_next_token` 声明。 |
| `src/ncnn_text_runtime.cpp` | `llm_select_next_token` 的实现：repetition penalty + 串起四个采样函数。 |
| `src/ncnn_llm_base.h` | 基类内的**第二套**采样实现：`SampleConfig`、`sample_logits`、以及与 sampling.cpp 逻辑相同的私有成员函数。 |
| `src/ncnn_llm_gpt.h` | `GenerateConfig` 默认值（temperature/top_p/top_k/repetition_penalty/do_sample）。 |
| `src/ncnn_llm_gpt.cpp` | `generate` 里如何构造 `history` 集合并把它喂给 `llm_select_next_token`。 |
| `src/nllb_600m.cpp` | 基类 `sample_logits` 的唯一调用方（NLLB 翻译）。 |

一句话定位：`sampling.cpp` 是「采样原语库」，`llm_select_next_token` 是「加了重复惩罚的完整采样器」，基类 `sample_logits` 是「NLLB 专用的同款采样器」。

## 4. 核心概念与源码讲解

### 4.1 softmax_vec：带温度的数值稳定 softmax

#### 4.1.1 概念说明

`softmax_vec` 把一段原始 logits 转成概率分布，并顺带实现温度调节。它的关键不是公式本身，而是**数值稳定写法**：直接算 \(\exp(z_i)\) 会因为 \(z_i\) 很大而溢出。标准做法是先减去最大值 \(z_{\max}\)：

\[
p_i = \frac{\exp(z_i - z_{\max})}{\sum_j \exp(z_j - z_{\max})}
\]

减去同一个常数不改变 softmax 结果（分子分母同乘 \(\exp(-z_{\max})\)），但让最大的指数项变成 \(\exp(0)=1\)，杜绝溢出。

#### 4.1.2 核心流程

```text
输入: logits[], temperature
1. max_logit = max(logits)
2. 对每个 x: x = exp((x - max_logit) / temperature)   # 注意：先除温度，再 exp
3. sum = 累加所有 x
4. 对每个 x: x = x / sum                                # 原地归一化为概率
返回: logits[]（已被改写为概率，和为 1）
```

注意第 2 步：温度是作用在「减去 max 之后、exp 之前」的，即 \(\exp((z_i-z_{\max})/T)\)，与上面的数学公式一致。

#### 4.1.3 源码精读

声明见 [src/sampling.h:5](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.h#L5)：

```cpp
void softmax_vec(std::vector<float>& logits, float temperature);
```

实现见 [src/sampling.cpp:7-15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L7-L15)，这段代码做了：减最大值 → 除温度 → exp → 归一化：

```cpp
void softmax_vec(std::vector<float>& logits, float temperature) {
    float max_logit = *std::max_element(logits.begin(), logits.end());
    float sum = 0.f;
    for (float& x : logits) {
        x = std::exp((x - max_logit) / temperature);
        sum += x;
    }
    for (float& x : logits) x /= sum;
}
```

要点：

- 函数是**原地修改** `logits`（传引用），调用后里面已是概率。
- 参数 `temperature` 直接做除数，所以若传 0 会除零——这正是 `llm_select_next_token` 在调用前要单独判断 `temperature <= 0` 的原因（见 4.4）。
- 这是**自由函数**版本，逻辑与基类 [src/ncnn_llm_base.h:172-180](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L172-L180) 里的私有成员方法 `softmax_vec` **逐字符相同**（两套实现的差异只在随机源，见 4.5）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「减最大值」对数值稳定的作用。

**操作步骤**（这是一段不依赖 ncnn 的示例代码，因为它只用到 `std::vector<float>`，可直接用 g++ 编译运行）：

```cpp
// 示例代码：softmax_demo.cpp —— 复现 sampling.cpp 的 softmax_vec
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>

void softmax_vec(std::vector<float>& logits, float temperature) {
    float max_logit = *std::max_element(logits.begin(), logits.end());
    float sum = 0.f;
    for (float& x : logits) { x = std::exp((x - max_logit) / temperature); sum += x; }
    for (float& x : logits) x /= sum;
}

int main() {
    std::vector<float> z = {0.1f, 2.0f, 1.8f, 0.5f, 3.0f};
    for (float T : {0.5f, 1.0f, 2.0f}) {
        std::vector<float> p = z;          // 复制，避免原地改写影响下一轮
        softmax_vec(p, T);
        std::printf("T=%.1f  p=%.3f %.3f %.3f %.3f %.3f\n",
                    T, p[0], p[1], p[2], p[3], p[4]);
    }
    return 0;
}
```

编译运行：`g++ -std=c++17 softmax_demo.cpp -o softmax_demo && ./softmax_demo`

**需要观察的现象**：温度越低，最大项（3.0 对应下标 4）的概率越接近 1，分布越「尖」；温度越高分布越平。

**预期结果**：`T=0.5` 时下标 4 的概率明显高于其余项；`T=2.0` 时各项概率更接近。（具体数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果去掉「减 max_logit」这一步，输入 `[1000, 1001]` 会发生什么？

**答案**：`exp(1000)` 远超 `float` 表示范围，结果为 `inf`，最后归一化得到 `nan`。减去 max 后最大指数项为 `exp(0)=1`，杜绝溢出——这正是数值稳定写法存在的理由。

**练习 2**：温度 \(T=1\) 时，`softmax_vec` 等价于什么？

**答案**：等价于标准 softmax，对 logits 不做额外缩放。

---

### 4.2 apply_top_k / apply_top_p：截断但不重归一化

#### 4.2.1 概念说明

top_k 与 top_p 都是对「候选 token 集合」做截断：把不该被采样的 token 概率直接置 0，只留下一个小集合。两者的截断准则不同：

- **top_k**：留下概率最大的 \(k\) 个（按「个数」截断）；
- **top_p（核采样）**：留下累计概率刚达到 \(p\) 的最小集合（按「累计概率」截断）。

本讲要特别注意一个实现细节：**截断后，`sampling.cpp` 并不会重新归一化**。剩下的概率之和 \(<1\)，但这没问题——后面的 `sample_from_probs` 用的 `std::discrete_distribution` 会内部归一化（见 4.3）。这是四个函数之间的一条隐式契约。

#### 4.2.2 核心流程

**top_k**：

```text
输入: probs[]（已 softmax）, k
1. 若 k<=0 或 k>=size：直接返回（不截断）。
2. 用 nth_element 找到第 k 大的值 threshold（O(n)，不全排序）。
3. 把所有 probs[i] < threshold 的项置 0。
（不重归一化；等于 threshold 的项保留，所以有并列时可能保留略多于 k 个。）
```

**top_p**：

```text
输入: probs[], p
1. 若 p>=1.0：直接返回（不截断）。
2. 构造 (prob, 原下标) 列表，按 prob 降序全排序。
3. 从大到小累加 cum，一旦 cum>=p，记 cutoff=i+1 并停止。
4. 只保留前 cutoff 项，其余置 0。
（不重归一化。）
```

#### 4.2.3 源码精读

top_k 实现见 [src/sampling.cpp:17-23](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L17-L23)：

```cpp
void apply_top_k(std::vector<float>& probs, int k) {
    if (k <= 0 || k >= (int)probs.size()) return;          // 不截断的守卫
    std::vector<float> tmp = probs;
    std::nth_element(tmp.begin(), tmp.end() - k, tmp.end());
    float threshold = tmp[tmp.size() - k];                 // 第 k 大的值
    for (float& p : probs) if (p < threshold) p = 0.f;     // 严格小于则置 0
}
```

两个细节值得记：

1. `std::nth_element` 是「部分排序」，只保证第 `size-k` 个位置上的值就是排序后该位置的值（即第 k 大），它左右两侧不一定有序，复杂度 \(O(n)\)，比全排序更快。
2. 判定是 `p < threshold`（严格小于），所以**等于阈值的项会被保留**——若分布里存在并列，保留个数可能略多于 \(k\)。

top_p 实现见 [src/sampling.cpp:25-50](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L25-L50)：

```cpp
void apply_top_p(std::vector<float>& probs, float p) {
    if (p >= 1.0f) return;
    std::vector<std::pair<float,int>> v;
    v.reserve(probs.size());
    for (int i = 0; i < (int)probs.size(); ++i) v.emplace_back(probs[i], i);
    std::sort(v.begin(), v.end(), std::greater<>());       // 按 prob 降序

    float cum = 0.f;
    size_t cutoff = v.size();
    for (size_t i = 0; i < v.size(); ++i) {
        cum += v[i].first;
        if (cum >= p) { cutoff = i + 1; break; }           // 凑够 p 就停
    }
    std::vector<char> keep(probs.size(), 0);
    for (size_t i = 0; i < cutoff; ++i) keep[v[i].second] = 1;  // 用原下标回标
    for (int i = 0; i < (int)probs.size(); ++i) if (!keep[i]) probs[i] = 0.f;
}
```

这里用 `(prob, 原下标)` 配对再排序，是为了在置 0 时能通过 `keep[原下标]` 精确找回每个概率对应的位置——因为排序后顺序已经打乱。`cutoff = i + 1` 表示「累计达到 \(p\) 的那一刻，连同当前项一起保留」，即保留**最小可凑够 \(p\) 的核**。

> 注意：top_p 用了 `std::sort`（\(O(n\log n)\) 全排序），而 top_k 用了 `nth_element`（\(O(n)\)）。在大词表（几万到十几万）下，top_p 是这一步里最贵的操作。

#### 4.2.4 代码实践

**实践目标**：观察 top_k / top_p「置 0 但不重归一化」的效果。

**操作步骤**：在 4.1.4 的 `softmax_demo.cpp` 基础上，先对 logits 做一次 `softmax_vec(p, 1.0f)`，再分别调用 `apply_top_k` / `apply_top_p`，打印剩余非零项及其下标。两个函数照抄 [src/sampling.cpp:17-50](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L17-L50)。

**需要观察的现象**：

- top_k=2 后只剩 2 个非零项（或并列时略多），其余为 0；
- top_p=0.9 后保留的项数取决于分布形态，是个**动态个数**；
- 两种情况下，剩余项的概率之和都 **小于 1**（因为没重归一化）。

**预期结果**：以 `z=[0.1,2.0,1.8,0.5,3.0]` 为例，softmax 后最大项（下标 4）概率最高；top_k=2 保留它和次大项；top_p=0.9 至少保留最大的 1~2 项。（精确数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_top_k` 用 `nth_element` 而不是 `sort`？

**答案**：`nth_element` 只需 \(O(n)\) 即可定位第 k 大的阈值，而 `sort` 要 \(O(n\log n)\)。这里只关心阈值、不关心完整顺序，所以用更快的部分排序。

**练习 2**：假设 top_p 之后剩余概率之和是 0.6（小于 1），这会让后面的抽样出错吗？

**答案**：不会。后续 `sample_from_probs` 用 `std::discrete_distribution`，它把权重当比例处理、内部自动归一化，所以未归一化的概率可以直接喂进去。这就是「截断但不重归一化」能成立的依据。

---

### 4.3 sample_from_probs：discrete_distribution 负责抽样

#### 4.3.1 概念说明

经过 softmax + top_k + top_p 之后，我们手里是一段「带若干 0、且和可能小于 1」的概率向量。最后一步是**按这个分布抽一个下标**作为输出 token id。`sample_from_probs` 用 C++ 标准库的 `std::discrete_distribution` 完成这件事：你给它一组权重（不需要归一化），它构造一个按权重比例产生下标的随机分布，调用一次返回一个下标。

#### 4.3.2 核心流程

```text
输入: probs[]（带权，允许有 0、允许和≠1）
1. 用 probs 构造 std::discrete_distribution<int>（内部会归一化权重，
   并按累积权重建查找表）。
2. 用全局随机源 rng 抽一次，返回下标。
```

#### 4.3.3 源码精读

实现见 [src/sampling.cpp:52-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L52-L55)：

```cpp
int sample_from_probs(const std::vector<float>& probs) {
    std::discrete_distribution<int> dist(probs.begin(), probs.end());
    return dist(rng);
}
```

这段极简，但有两个要点：

1. **随机源是文件顶部的全局变量** [src/sampling.cpp:5](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L5)：

   ```cpp
   static std::mt19937 rng(std::random_device{}());
   ```

   这是一个**进程级单例**随机数发生器，用 `random_device` 播种。它与基类成员 `rng_`（[src/ncnn_llm_base.h:111](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L111)）是**两个独立的随机流**——这是区分两套采样实现的线索之一（见 4.5）。

2. **`std::discrete_distribution` 要求至少有一个正权重**，否则行为未定义。如果 top_k / top_p 因数值问题把所有概率都置成了 0（或出现 NaN），抽样会出问题。`llm_select_next_token` 因此在调用前专门做了一次 `sum` 安全检查（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：感受「带温度采样」是随机的、可复现的。

**操作步骤**：在示例程序里固定一段 logits，`softmax_vec(p, 1.0f)` 后连续调用 10 次 `sample_from_probs(p)`，打印这 10 个下标。再把 logits 换成更平的分布（如 `[1,1,1,1,1]`）重复实验。

**需要观察的现象**：分布越尖，重复抽样越集中在最大项；分布越平，抽样越发散。多次运行程序，结果会变（因为 `random_device` 每次播种不同）。

**预期结果**：尖分布下 10 次抽样大多落在 argmax 下标；平分布下五个下标都会出现。（精确序列待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 `probs` 全部传 0，`discrete_distribution` 会怎样？

**答案**：全部权重为 0 时行为未定义（C++ 标准要求至少一个正权重）。这就是为什么 `llm_select_next_token` 要先校验 `sum > 0` 再抽样，否则回退到 argmax。

**练习 2**：`sample_from_probs` 与「贪心 argmax」最本质的区别是什么？

**答案**：argmax 永远选最大项（确定性、可复现）；`sample_from_probs` 按概率**随机**抽取，概率大的更可能被选但不是必然，从而带来多样性。

---

### 4.4 llm_select_next_token：repetition penalty + 采样全链路

#### 4.4.1 概念说明

前面三个模块是「原语」，`llm_select_next_token` 才是 `generate` 真正调用的**完整采样器**。它做了三件事：

1. **重复惩罚（repetition penalty）**：对历史里出现过的 token 的 logits 做缩放，抑制复读；
2. **决定贪心还是采样**：根据 `do_sample` 和 `temperature` 二选一；
3. **采样**：把 `softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs` 串成一条链，并带一个兜底安全检查。

重复惩罚用的是 HuggingFace 的约定，**按 logits 原始值的正负号分别处理**（不是按概率）：

\[
\text{score}_t =
\begin{cases}
z_t \cdot \rho, & z_t < 0 \\
z_t / \rho, & z_t \ge 0
\end{cases}
\]

其中 \(\rho\) 是 `repetition_penalty`，\(z_t\) 是 token \(t\) 的原始 logit。当 \(\rho>1\) 时：负 logit 乘以 \(>1\) 变得更负，正 logit 除以 \(>1\) 变得更小——两个方向都被**压低**，从而抑制重复。当 \(\rho=1\) 时无任何影响；当 \(\rho<1\) 时反而会**鼓励**重复。

#### 4.4.2 核心流程

```text
输入: logits(ncnn::Mat), history(已出现 token 集合), cfg
1. 把 logits 拷贝到 std::vector<float> scores（长度 = cfg.vocab_size 或 logits.w）。
2. 【重复惩罚】对每个 t in history：
       若 scores[t] < 0：scores[t] *= cfg.repetition_penalty
       否则            ：scores[t] /= cfg.repetition_penalty
3. 【贪心分支】若 do_sample != 1 或 temperature <= 0：返回 argmax(scores)。
4. 【采样分支】softmax_vec(scores, temperature)
              若 top_k>0：apply_top_k(scores, top_k)
              若 top_p<1：apply_top_p(scores, top_p)
5. 【安全检查】sum = Σscores；若 sum 非有限或 <=0：返回 argmax。
6. 【抽样】返回 sample_from_probs(scores)。
```

注意第 2 步里的 `history` 是一个 `std::unordered_set<int>`，**只记录「出现过哪些 token」、不记录出现次数**——所以这是一种「出现即惩罚」的存在性惩罚（presence penalty 风格），而不是按次数累加的频率惩罚。

#### 4.4.3 源码精读

配置结构 `LlmTokenSampleConfig` 见 [src/ncnn_text_runtime.h:11-18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h#L11-L18)，相比基类的 `SampleConfig` 多了 `vocab_size` 与 `repetition_penalty`，且 `do_sample` 是 `int` 而非 `bool`：

```cpp
struct LlmTokenSampleConfig {
    int vocab_size = 0;
    float temperature = 1.0f;
    float top_p = 1.0f;
    int top_k = 0;
    float repetition_penalty = 1.0f;   // 注意默认 1.0（无影响）
    int do_sample = 0;
};
```

主实现见 [src/ncnn_text_runtime.cpp:85-115](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L85-L115)。逐段看：

**拷贝 logits + 重复惩罚**（[src/ncnn_text_runtime.cpp:88-99](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L88-L99)）：

```cpp
const int vocab_size = cfg.vocab_size > 0 ? cfg.vocab_size : logits.w;
std::vector<float> scores(vocab_size);
std::memcpy(scores.data(), logits.data, sizeof(float) * vocab_size);

for (int t : history) {
    if (t < 0 || t >= vocab_size) continue;
    if (scores[t] < 0) scores[t] *= cfg.repetition_penalty;   // 负：乘
    else                scores[t] /= cfg.repetition_penalty;   // 非负：除
}
```

`vocab_size` 优先用配置值，否则取 `logits.w`。在 `generate` 里它来自分词器的词表大小（[src/ncnn_llm_gpt.cpp:844](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L844) `const int vocab_size = bpe->vocab_size();`），传给采样配置在 [src/ncnn_llm_gpt.cpp:973](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L973)。惩罚里的越界保护 `t < 0 || t >= vocab_size` 是必要的防御。

**贪心 vs 采样分支**（[src/ncnn_text_runtime.cpp:101-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L101-L103)）：

```cpp
if (cfg.do_sample != 1 || cfg.temperature <= 0.0f) {
    return (int)(std::max_element(scores.begin(), scores.end()) - scores.begin());
}
```

这里把 `temperature <= 0` 也视作贪心——既表达了「温度趋零即贪心」的直觉，也保护了下面 `softmax_vec` 不会除零。这是它比基类 `sample_logits` 更严谨的一点。

**采样链 + 安全检查**（[src/ncnn_text_runtime.cpp:105-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L105-L114)）：

```cpp
softmax_vec(scores, cfg.temperature);
if (cfg.top_k > 0) apply_top_k(scores, cfg.top_k);
if (cfg.top_p < 1.0f) apply_top_p(scores, cfg.top_p);

const float sum = std::accumulate(scores.begin(), scores.end(), 0.0f);
if (!std::isfinite(sum) || sum <= 0.0f) {        // 兜底：全 0 或 NaN 时回退 argmax
    return (int)(std::max_element(scores.begin(), scores.end()) - scores.begin());
}
return sample_from_probs(scores);
```

这条链正是 4.1~4.3 三个模块的原语在「真实顺序」里的串联：softmax → top_k → top_p → 抽样。`sum` 校验专门为 `discrete_distribution` 的「至少一个正权重」要求兜底。

**`history` 在 `generate` 里如何构造**（[src/ncnn_llm_gpt.cpp:868-882](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L868-L882)）：

```cpp
std::unordered_set<int> history;
history.insert(ctx->cur_token);          // 预填好的「当前 token」入集合
...
int next_id = llm_select_next_token(logits_mat, history, sample_cfg);
ctx->cur_token = next_id;
history.insert(next_id);                 // 每步把新 token 累计进集合
```

可见 `history` 跨解码步持续累积——已经生成过的 token 在后续每一步都会被惩罚。一个细节：当一次工具调用结束时，`history` 会被清空并重置为当前 token（[src/ncnn_llm_gpt.cpp:883-884](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L883-L884)），因为工具响应已被 `prefill` 回填、上下文重启。

**默认配置**：`generate` 的实际默认值在 `GenerateConfig`（[src/ncnn_llm_gpt.h:32-43](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L32-L43)）：`temperature=0.3`、`top_p=0.8`、`top_k=50`、`repetition_penalty=1.1`、`do_sample=1`。也就是说默认是**采样模式**，带轻微温度、核采样 0.8、top_k 50、轻度重复惩罚 1.1。

#### 4.4.4 代码实践

**实践目标**：复现 repetition penalty「翻转 argmax」的效果。

**操作步骤**：写一段不依赖 ncnn 的示例代码，**手写复现** `llm_select_next_token` 里 [src/ncnn_text_runtime.cpp:88-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L88-L103) 的「拷贝 + 重复惩罚 + 贪心 argmax」三段逻辑（因为完整版依赖 `ncnn::Mat`，这里用 `std::vector<float>` 等价复现）：

```cpp
// 示例代码：repetition_penalty_demo.cpp —— 复现 llm_select_next_token 的惩罚块
#include <algorithm>
#include <cstdio>
#include <unordered_set>
#include <vector>

int argmax(const std::vector<float>& s) {
    return (int)(std::max_element(s.begin(), s.end()) - s.begin());
}

// 只复现「重复惩罚 + 贪心」分支（do_sample=0），用来观察惩罚对 argmax 的影响
int greedy_with_penalty(std::vector<float> scores,
                        const std::unordered_set<int>& history,
                        float penalty) {
    for (int t : history) {
        if (t < 0 || t >= (int)scores.size()) continue;
        if (scores[t] < 0) scores[t] *= penalty;
        else               scores[t] /= penalty;
    }
    return argmax(scores);
}

int main() {
    std::vector<float> logits = {2.9f, 3.0f};   // 词表只有 2 个 token
    std::unordered_set<int> history = {1};       // 假设 token 1 刚出现过

    std::printf("无惩罚 (penalty=1.0): argmax=%d\n",
                greedy_with_penalty(logits, history, 1.0f));
    std::printf("有惩罚 (penalty=5.0): argmax=%d\n",
                greedy_with_penalty(logits, history, 5.0f));
    return 0;
}
```

编译运行：`g++ -std=c++17 repetition_penalty_demo.cpp -o rpd && ./rpd`

**需要观察的现象**：

- `penalty=1.0`：token 1 的 logit 3.0 不变，argmax=1（3.0 > 2.9）。
- `penalty=5.0`：token 1 的 logit 变成 3.0/5.0=0.6，token 0 仍是 2.9，argmax 翻转为 0。

**预期结果**：输出依次为 `argmax=1`、`argmax=0`——重复惩罚把一个原本会被反复选中的 token 压了下去，让另一个 token 有机会胜出。

> 想做完整随机采样链的复现，可把 4.1~4.3 的三个函数照抄进来，按 [src/ncnn_text_runtime.cpp:105-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L105-L114) 的顺序串联即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么重复惩罚按「原始 logit 的正负号」分别用乘和除，而不是统一除以 \(\rho\)？

**答案**：这是为了在 \(\rho>1\) 时让正负两种 logit **都被压低**。若统一除以 \(\rho\)：正 logit 确实变小（被压低），但负 logit 除以 \(>1\) 反而**变大**（更接近 0，被抬高了），起不到「抑制」作用。正用除、负用乘，才能保证两个方向都被抑制，与 HuggingFace 的实现一致。

**练习 2**：`history` 是 `unordered_set`，如果一个 token 在历史里出现了 10 次，它会被惩罚几次？

**答案**：只惩罚 1 次（按存在性）。`unordered_set` 去重，循环里对该 token 只缩放一次。所以这是「出现即惩罚」的存在性惩罚，而非按次数累加的频率惩罚。

**练习 3**：`llm_select_next_token` 在调用 `sample_from_probs` 之前那次 `sum` 校验，是为了防什么？

**答案**：防止 `scores` 全为 0 或含 NaN/Inf 时 `std::discrete_distribution` 行为未定义。一旦 `sum` 非有限或 ≤0，就回退到确定性的 argmax，保证采样器永不崩溃。

---

### 4.5 两套采样实现的对比与调用方

#### 4.5.1 概念说明

`ncnn_llm` 里其实有**两份几乎逐字符相同的采样代码**：一份是 `sampling.cpp` 的自由函数（供 `llm_select_next_token` 用），另一份是基类 `ncnn_llm_base` 的私有成员方法（供 `sample_logits` 用）。理解它们为何「重复」、差异在哪、分别服务谁，是本讲的收尾目标。

#### 4.5.2 核心流程

两套实现的结构完全平行：

| 维度 | 自由函数版（sampling.cpp） | 基类版（ncnn_llm_base.h） |
| --- | --- | --- |
| 配置结构 | `LlmTokenSampleConfig`（多 `vocab_size`/`repetition_penalty`，`do_sample` 为 `int`） | `SampleConfig`（无 `repetition_penalty`，`do_sample` 为 `bool`） |
| 入口函数 | `llm_select_next_token`（含重复惩罚 + 安全检查） | `ncnn_llm_base::sample_logits`（无重复惩罚） |
| 随机源 | 全局 `static std::mt19937 rng`（进程级） | 成员 `rng_`（每个对象一个） |
| 贪心条件 | `do_sample != 1 \|\| temperature <= 0` | `!do_sample` |
| 调用方 | LLM / VLM / OCR / ASR 的 `generate`（经共享运行时） | 仅 NLLB 翻译 |

#### 4.5.3 源码精读

**基类入口** `sample_logits` 见 [src/ncnn_llm_base.h:147-169](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L147-L169)：

```cpp
int sample_logits(const ncnn::Mat& logits, const SampleConfig& cfg) {
    if (!cfg.do_sample) return argmax1d(logits);      // 贪心
    std::vector<float> probs(logits.w);
    const float* p = logits;
    for (int i = 0; i < logits.w; ++i) probs[i] = p[i];
    softmax_vec(probs, cfg.temperature);
    if (cfg.top_k > 0)  apply_top_k(probs, cfg.top_k);
    if (cfg.top_p < 1.0f) apply_top_p(probs, cfg.top_p);
    return sample_from_probs(probs);
}
```

注意它**没有** repetition penalty，**没有** `sum` 安全检查，贪心判定也**不**包含 `temperature <= 0` 的保护。它调用的 `softmax_vec`/`apply_top_k`/`apply_top_p`/`sample_from_probs` 是基类的**私有成员方法**（[src/ncnn_llm_base.h:172-220](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L172-L220)），与 `sampling.cpp` 的自由函数逻辑一致，唯一差别是用成员 `rng_` 而非全局 `rng`。

**唯一调用方**是 NLLB 翻译，见 [src/nllb_600m.cpp:128-141](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L128-L141)：

```cpp
SampleConfig sample_cfg;
sample_cfg.temperature = config.temperature;
sample_cfg.top_k = config.top_k;
sample_cfg.top_p = config.top_p;
sample_cfg.do_sample = config.do_sample;
...
last_index = sample_logits(logits, sample_cfg);
```

而 LLM/VLM/OCR/ASR 的 `generate` 都走另一条路——构造 `LlmTokenSampleConfig` 并调用 `llm_select_next_token`（典型处见 [src/ncnn_llm_gpt.cpp:972-979](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L972-L979)）。OCR 与 ASR 也同样如此（如 [src/ncnn_llm_ocr.cpp:533](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L533)、[src/ncnn_llm_asr.cpp:308](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L308) 都在组装 `sample_cfg.do_sample`）。

> 为什么会有两份重复代码？基类 `sample_logits` 是更早的、自包含的实现（NLLB 这类**自带完整 encoder-decoder 解码循环**的运行时直接继承基类、自己管 KV cache，所以顺手用基类成员方法与成员随机源）。后来抽出的「共享文本运行时」要服务多个模型家族、且需要 repetition penalty，于是另写了一份自由函数版放在 `sampling.cpp`。两者并存是历史演进的产物——读源码时知道「它们逻辑相同、只是配置与随机源不同」即可。

#### 4.5.4 代码实践

**实践目标**：用静态阅读确认「两套实现的调用边界」。

**操作步骤**：

1. 在仓库里全文搜索 `sample_logits(` 的调用点（确认只在 `nllb_600m.cpp`）；
2. 全文搜索 `llm_select_next_token(` 的调用点（确认在 gpt/ocr/asr 的 generate 链路）。

**需要观察的现象**：`sample_logits` 仅 1 处调用（NLLB），`llm_select_next_token` 多处调用且都集中在主推理运行时。

**预期结果**：印证「NLLB 走基类版、其余模态走共享版」的分工。

#### 4.5.5 小练习与答案

**练习 1**：如果你在 NLLB 翻译里发现模型输出总是重复，能用 `repetition_penalty` 解决吗？

**答案**：不能直接解决。因为 NLLB 走的是基类 `sample_logits`，它**不读取** `repetition_penalty`（`SampleConfig` 里压根没这个字段）。要给 NLLB 加重复惩罚，需要改基类 `sample_logits` 或让它改用 `llm_select_next_token`。

**练习 2**：两套实现的随机源不同，会带来什么实际影响？

**答案**：基类版每个对象用各自的 `rng_`，对象之间独立；自由函数版用进程级全局 `rng`，所有走共享运行时的调用共享一个随机流。在多线程或同时跑多个模型时，两者的可复现性与隔离性表现不同。

## 5. 综合实践

把本讲四个模块串起来，写一个**不依赖 ncnn 的最小采样器**（示例代码），完整复现 `llm_select_next_token` 的全链路：

1. 照抄 [src/sampling.cpp:7-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L7-L55) 的 `softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs` 四个函数；
2. 写一个 `select_next_token(const std::vector<float>& logits, const std::unordered_set<int>& history, const LlmTokenSampleConfig& cfg)`，按 [src/ncnn_text_runtime.cpp:88-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L88-L114) 的顺序实现：拷贝 → 重复惩罚 → 贪心分支判断 → softmax+top_k+top_p → sum 安全检查 → 抽样；
3. 用一段固定 logits（如 `[2.9, 3.0, 0.5, 0.1]`）和 `history={1}`，分别用四组配置各取一次 token：
   - greedy：`do_sample=0`
   - top_k：`do_sample=1, temperature=1.0, top_k=2`
   - top_p：`do_sample=1, temperature=1.0, top_p=0.9`
   - 带温度采样：`do_sample=1, temperature=2.0`
4. 再把 `repetition_penalty` 从 1.0 调到 5.0，对比贪心分支下 argmax 是否翻转。

把每次的输出 token id 与你对源码的预测对照，确认完全一致。最后回答：这四组配置里，哪几组的结果是确定性的？哪几组受随机源影响？（答：greedy、top_k+贪心、top_p+贪心都确定；只有 `do_sample=1` 且进入 `sample_from_probs` 的那组才是随机的。）

## 6. 本讲小结

- `softmax_vec` 用「减最大值」保证数值稳定，温度作用在 `exp` 之前；它是**原地**把 logits 改写成概率。
- `apply_top_k`（`nth_element` 找阈值）与 `apply_top_p`（降序累加凑够 \(p\)）都是**置 0 但不重归一化**，靠下游 `discrete_distribution` 内部归一化兜底。
- `sample_from_probs` 用 `std::discrete_distribution` 按权重抽样，随机源是 `sampling.cpp` 顶部的全局 `static std::mt19937`。
- `llm_select_next_token` 先按「负乘正除」做 repetition penalty（`history` 是去重的存在性集合），再决定贪心或采样，并在抽样前用 `sum` 校验防全 0/NaN。
- 项目有**两套采样实现**：基类 `sample_logits`（无重复惩罚、用成员 `rng_`、仅 NLLB 用）与共享运行时 `llm_select_next_token`（有重复惩罚、用全局 `rng`、服务 LLM/VLM/OCR/ASR）。

## 7. 下一步学习建议

- **u3-l1 / u3-l2**：如果你想理解 `generate` 里 `history` 集合的 token 是怎么从字符串切出来的，回到分词器讲义，看 `bpe->encode` 与特殊令牌如何变成 id。
- **u7-l2（工具调用机制）**：本讲提到工具调用结束时 `history` 会被清空重置——如果你想看清这条分支的来龙去脉，接着读工具调用讲义。
- **u8-l2（benchmark）**：采样参数（temperature/top_p/top_k/repetition_penalty）会显著影响生成质量与速度，benchmark 讲义会展示如何在 `benchllm` 里调参测速。
- 直接读源码的话，建议重读一遍 [src/ncnn_text_runtime.cpp:85-115](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L85-L115)，把本讲的伪代码与真实代码逐行对齐。
