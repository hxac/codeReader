# AdamW 优化器：gpt2_update

## 1. 本讲目标

在前一讲（u3-l1）里，我们沿着 `gpt2_backward` 把损失对**每一个参数**的梯度 `grads_memory[i]` 全部算了出来。但「算出梯度」并不等于「训练」——训练还差最后、也是最关键的一步：**用这些梯度去更新参数**。完成这件事的函数就是本讲的主角 `gpt2_update`，它实现了 **AdamW 优化器**。

学完本讲你应该能够：

- 说清楚 AdamW 一步更新里，对**每一个参数**到底做了哪些运算；
- 解释一阶动量 `m`、二阶动量 `v`、以及**偏差修正（bias correction）**的作用；
- 说明 AdamW 与经典 Adam 的本质区别——**解耦权重衰减（decoupled weight decay）**，并理解为什么代码里把 `weight_decay * param` 写在它所在的位置；
- 对照 [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) 的 CPU 参考实现，手算第 1 步某个参数的更新量。

本讲只讲「优化器这一步」，不涉及前向/反向如何算梯度（已在 u2、u3-l1 讲过），也不涉及 CUDA 版的混合精度细节（留待 u6-l1）。

## 2. 前置知识

在继续之前，请确保你已经理解以下概念（来自前面的讲义）：

- **参数与梯度是一一对应的两个一维数组**。llm.c 把 16 个参数张量（`NUM_PARAMETER_TENSORS = 16`）一次性 `malloc` 到一整块连续内存 `params_memory`，再用指针把它们「钉」到不同偏移；梯度 `grads_memory` 用同样的布局（见 [train_gpt2.c:L678-L697](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L678-L697)）。所以优化器面对的不是一堆矩阵，而是一个长度为 `num_parameters`（约 1.24 亿）的「扁平」浮点数组，循环 `for (i = 0; i < num_parameters; i++)` 逐个更新即可。
- **梯度累加 + 每步清零**的约定（u3-l1）。反向传播里所有 `*_backward` 都用 `+=` 把梯度累加进 `grads_memory`，因此每步反向前必须调用 `gpt2_zero_grad` 把它清零，否则梯度会跨步累加导致训练发散。
- **学习率（learning rate, lr）**：控制每步走多大的一步。本工程训练时取 `1e-4`。
- **小批量梯度下降**的直觉：每步用当前 batch 算出的梯度，沿「负梯度方向」走一小步来降低损失。

> 如果你还不熟悉「动量（momentum）」「指数移动平均」这两个词，没关系，下面会从零讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | CPU 参考实现。本讲聚焦其中的 `gpt2_update`（优化器主体）、`GPT2` 结构体里的 `m_memory`/`v_memory`（动量缓冲）、`gpt2_zero_grad`（配套清零），以及 `main` 里调用优化器的那一行。 |
| [llmc/adamw.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh) | CUDA 主线版的 AdamW kernel。公式与 CPU 版完全一致，但多了并行化、梯度缩放 `grad_scale`、master weights、随机舍入等工程细节，本讲用作「同一算法的高性能对照」。 |

> 注意命名差异：CPU 版函数叫 `gpt2_update`，CUDA 版叫 `adamw_update`。别在 `llmc/adamw.cuh` 里搜 `gpt2_update`——搜不到。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先给出完整更新公式（4.1），再拆开其中的「动量 + 偏差修正」部分（4.2），最后单独讲「解耦权重衰减」这个 AdamW 的灵魂（4.3）。

### 4.1 AdamW 更新公式

#### 4.1.1 概念说明

最朴素的优化器是 **SGD（随机梯度下降）**，对每个参数 \(\theta\) 一步更新就是：

\[
\theta \leftarrow \theta - \mathrm{lr} \cdot g
\]

其中 \(g\) 是当前 batch 算出的梯度。SGD 的问题：每个参数都用同一个学习率，且对梯度的噪声很敏感。Adam 在此基础上做了两件事——用**梯度的指数移动平均**（动量）来平滑方向，用**梯度平方的指数移动平均**来给每个参数**自适应地**缩放步长。

AdamW 则是 Adam 的一个修正版（[Loshchilov & Hutter, 2019](https://arxiv.org/abs/1711.05101)），它指出 Adam 原版里把权重衰减混进梯度（L2 正则）的做法在自适应缩放下会「变味」，于是改成**解耦权重衰减**。llm.c 的注释也直接指向 PyTorch 的 AdamW 文档作为参考。

对第 \(t\) 步、第 \(i\) 个参数，设当前梯度为 \(g_t\)，AdamW 的完整更新规则是：

\[
\begin{aligned}
m_t &= \beta_1 m_{t-1} + (1-\beta_1)\, g_t \\
v_t &= \beta_2 v_{t-1} + (1-\beta_2)\, g_t^2 \\
\hat{m}_t &= \frac{m_t}{1-\beta_1^t}, \qquad \hat{v}_t = \frac{v_t}{1-\beta_2^t} \\
\theta_t &= \theta_{t-1} - \mathrm{lr}\left( \frac{\hat{m}_t}{\sqrt{\hat{v}_t}+\epsilon} + w_d \cdot \theta_{t-1} \right)
\end{aligned}
\]

其中：

- \(m_t\)：一阶动量（梯度的指数移动平均），代表「最近梯度的平滑方向」；
- \(v_t\)：二阶动量（梯度平方的指数移动平均），代表「最近梯度的波动大小」；
- \(\hat{m}_t, \hat{v}_t\)：偏差修正后的动量；
- \(\epsilon\)：防除零的小常数（本工程取 `1e-8`）；
- \(w_d\)：权重衰减系数（本工程训练时取 `0.0f`，即关闭）；
- \(\beta_1, \beta_2\)：两个平滑系数（本工程取 `0.9`、`0.999`）。

直觉地说：\(\frac{\hat{m}_t}{\sqrt{\hat{v}_t}+\epsilon}\) 这个比值很像一个「**信噪比**」——分子是平均方向，分母是波动幅度。若某参数的梯度一直稳定地朝一个方向，分子大、分母小，步长就大；若梯度来回抖动，分子小、分母大，步长就被自动压小。这就是 Adam「**每个参数自适应步长**」的核心。

#### 4.1.2 核心流程

`gpt2_update` 的执行流程非常直白（伪代码）：

```
输入：model, learning_rate, beta1, beta2, eps, weight_decay, 当前步数 t
1. 如果 m_memory/v_memory 还没分配（第一次调用），用 calloc 分配并清零
2. for 每个参数下标 i in [0, num_parameters):
     a. 取出当前参数 param = params_memory[i]，梯度 grad = grads_memory[i]
     b. 用旧动量 + 当前梯度更新一阶动量 m
     c. 用旧二阶动量 + 当前梯度平方更新二阶动量 v
     d. 对 m、v 做偏差修正得到 m_hat、v_hat
     e. 把新的 m、v 写回（供下一步使用）
     f. 更新参数：params_memory[i] -= lr * (m_hat/(sqrt(v_hat)+eps) + wd*param)
```

关键点：**每个参数的更新彼此完全独立**，只依赖「该参数自己的旧值、自己的梯度、自己的旧 m/v」。这正是为什么这个循环可以轻松并行化成 CUDA kernel（见 4.1.3 对照）。

#### 4.1.3 源码精读

CPU 参考实现的主体就在这一段（[train_gpt2.c:L1007-L1033](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1007-L1033)），公式与上一节的数学表达逐行对应：

```c
void gpt2_update(GPT2 *model, float learning_rate, float beta1, float beta2,
                 float eps, float weight_decay, int t) {
    // reference: https://pytorch.org/docs/stable/generated/torch.optim.AdamW.html

    // lazily allocate the memory for m_memory and v_memory
    if (model->m_memory == NULL) {
        model->m_memory = (float*)calloc(model->num_parameters, sizeof(float));
        model->v_memory = (float*)calloc(model->num_parameters, sizeof(float));
    }

    for (size_t i = 0; i < model->num_parameters; i++) {
        float param = model->params_memory[i];
        float grad  = model->grads_memory[i];

        float m = beta1 * model->m_memory[i] + (1.0f - beta1) * grad;   // 一阶动量
        float v = beta2 * model->v_memory[i] + (1.0f - beta2) * grad*grad; // 二阶动量
        float m_hat = m / (1.0f - powf(beta1, t));                       // 偏差修正
        float v_hat = v / (1.0f - powf(beta2, t));

        model->m_memory[i] = m;
        model->v_memory[i] = v;
        model->params_memory[i] -= learning_rate * (m_hat/(sqrtf(v_hat)+eps) + weight_decay*param);
    }
}
```

几个要留意的细节：

- **懒分配**：`m_memory`/`v_memory` 在第一次调用 `gpt2_update` 时才 `calloc` 出来（且 `calloc` 保证初值为 0，对应 \(m_0 = v_0 = 0\)）。这和 u1-l3 讲过的「激活/梯度/优化器状态都依赖运行时 B、T，故推迟分配」是同一套思路。
- **`t` 是从 1 开始的**：`main` 里传的是 `step+1`（见 4.2.3），所以第一步 `t=1`，偏差修正的分母 \(1-\beta^1\) 不为零。
- **`weight_decay * param` 写在括号内、与自适应项相加**：这正是「解耦」的关键，4.3 会专门讲。

对照 CUDA 版（[llmc/adamw.cuh:L18-L47](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L18-L47)），公式一模一样，只是换成了「每个线程处理一个参数」的写法，并用了一个两 FLOP 的 `lerp` 来算指数移动平均：

```c
m = lerp(grad, m, beta1);          // 等价于 beta1*m + (1-beta1)*grad
v = lerp(grad * grad, v, beta2);
m /= beta1_correction;             // m_hat
v /= beta2_correction;             // v_hat
float param = old_param - (learning_rate * (m/(sqrtf(v)+eps) + weight_decay*old_param));
```

可以看到「动量更新 → 偏差修正 → 减去自适应步 + 权重衰减」的三段式在 GPU 上完全没变。

#### 4.1.4 代码实践

**实践目标**：用 `printf` 把每个参数的「梯度方向」和「实际更新量」打印出来，亲眼看到「第一步、wd=0 时，更新量近似等于 \(-\mathrm{lr}\cdot\mathrm{sign}(g)\)」这个 4.2 会推导的结论。

> 注意：这是**修改源码做观察**的实践。请先备份，或在阅读后还原。不要把带调试打印的版本当作正式训练用。

**操作步骤**：

1. 在 [train_gpt2.c:L1016](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1016) 的 `for` 循环里，临时加一行：当 `t == 1 && i < 5` 时打印 `grad`、`m_hat`、`v_hat` 和 `learning_rate * (m_hat/(sqrtf(v_hat)+eps) + weight_decay*param)`。
2. `make train_gpt2` 重新编译。
3. 用 `OMP_NUM_THREADS=4 ./train_gpt2` 跑几步。

**需要观察的现象**：打印出来的「实际更新量」的**绝对值**应该非常接近 `1e-4`（即 `lr`），且符号与 `grad` 相反。

**预期结果**：因为 `wd=0`、`t=1`、`eps=1e-8` 相对 `sqrt(v_hat)` 可忽略，第一步更新量 ≈ \(\mathrm{lr}\cdot\frac{m_{\hat{}}}{\sqrt{v_{\hat{}}}}\) ≈ \(\mathrm{lr}\cdot\frac{g}{|g|}=\mathrm{lr}\cdot\mathrm{sign}(g)\)，绝对值就是 `lr=1e-4`。若你观察到偏差，多半是 `eps` 在 `g` 很小时不可忽略——这正是 4.2 要讨论的。若本地无法编译运行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：AdamW 一步更新里，更新某个参数需要哪些「历史信息」？为什么这些历史信息可以用一个一维数组存下？

**参考答案**：需要该参数的旧值 `param`、旧一阶动量 `m`、旧二阶动量 `v`，加上当前步的梯度 `grad` 和步数 `t`。因为每个参数只依赖自己这三个历史量、参数之间互不影响，所以 `m` 和 `v` 各自是一个长度等于 `num_parameters` 的一维数组（`m_memory`/`v_memory`），与 `params_memory`/`grads_memory` 同布局，下标一一对应。

**练习 2**：如果把循环里的 `model->m_memory[i] = m;` 和 `model->v_memory[i] = v;` 这两行删掉会怎样？

**参考答案**：动量不再跨步累积，\(m\)、\(v\) 每步都从 0 重新算（退化为只依赖当前梯度的单步量）。实际上动量机制失效，优化器行为严重偏离 AdamW，训练几乎肯定发散或学不动。这两行是把「本次算出的新动量」写回，供下一步的 `beta1 * m_memory[i]` 使用。

---

### 4.2 动量与偏差修正

#### 4.2.1 概念说明

**动量（momentum）** 的想法来自物理学：把历史梯度按指数衰减地累加起来，相当于给优化方向加上「惯性」。一阶动量 \(m\) 是梯度的指数移动平均（EMA）：

\[
m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t
\]

展开后会发现 \(m_t\) 其实是过去梯度的加权和，越久远的梯度权重越低（权重近似为 \((1-\beta_1)\beta_1^{k}\)）。本工程 \(\beta_1=0.9\)，意味着 \(m\) 大约是最近 ~10 步梯度的平滑平均。它能**抹掉梯度的随机抖动**，让更新方向更稳。

**二阶动量** \(v\) 是梯度**平方**的指数移动平均（这就是 RMSProp 的核心）：

\[
v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2
\]

\(\beta_2=0.999\)，平均窗口更长（~1000 步）。\(v\) 估计的是「梯度波动有多大」，用来把步长按 \(\sqrt{v}\) 缩放——波动大的参数走小步、波动小的参数走大步。

**偏差修正（bias correction）** 要解决的是**冷启动问题**。因为 \(m\)、\(v\) 的初值是 0，在最初几步里它们会被严重低估（真实平均还没「攒」起来）。除以 \(1-\beta^t\) 这个随 \(t\) 增大而趋近 1 的因子，正好抵消了这种系统性低估。随着 \(t\) 增大，\(\beta^t \to 0\)，修正因子 \(\to 1\)，偏差修正自然失效。

#### 4.2.2 核心流程

第 \(t\) 步对单个参数：

```
m  = beta1 * m_old + (1 - beta1) * grad      # 平滑方向
v  = beta2 * v_old + (1 - beta2) * grad*grad # 平滑波动
m_hat = m / (1 - beta1^t)                     # 抵消冷启动低估
v_hat = v / (1 - beta2^t)
```

一个值得记住的特例（也是综合实践要手算的）：**第 1 步** \(t=1\) 时，\(m_0=v_0=0\)，于是

\[
m_1 = (1-\beta_1)g,\quad v_1 = (1-\beta_2)g^2
\]

\[
\hat{m}_1 = \frac{(1-\beta_1)g}{1-\beta_1^1} = \frac{(1-\beta_1)g}{1-\beta_1} = g
\]

\[
\hat{v}_1 = \frac{(1-\beta_2)g^2}{1-\beta_2^1} = g^2
\]

偏差修正**完美地**把第一步的动量「还原」回了原始梯度 \(g\) 和 \(g^2\)！代入更新式（\(w_d=0\)）：

\[
\Delta\theta = -\mathrm{lr}\cdot\frac{g}{|g|+\epsilon}\approx -\mathrm{lr}\cdot\mathrm{sign}(g)
\]

也就是说，**第一步每个参数都朝各自梯度相反方向走一个 `lr` 大小的步**——这是偏差修正最直观的好处：没有它，第一步的步长会被压成 \((1-\beta)\mathrm{lr}\) 那么小。

#### 4.2.3 源码精读

`main` 训练循环里调用优化器的那一行（[train_gpt2.c:L1162-L1171](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1162-L1171)），传入了完整的超参数，注意最后一个参数是 `step+1`：

```c
// do a training step
clock_gettime(CLOCK_MONOTONIC, &start);
dataloader_next_batch(&train_loader);
gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);
gpt2_zero_grad(&model);
gpt2_backward(&model);
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, step+1);  // <- 本讲主角
```

对照 `gpt2_update` 的签名逐个对应：

| 实参 | 形参 | 含义 |
|------|------|------|
| `1e-4f` | `learning_rate` | 学习率 lr |
| `0.9f` | `beta1` | 一阶动量平滑系数 |
| `0.999f` | `beta2` | 二阶动量平滑系数 |
| `1e-8f` | `eps` | 分母防除零 |
| `0.0f` | `weight_decay` | 权重衰减（训练时关闭） |
| `step+1` | `t` | 从 1 开始的步数，用于偏差修正 |

再看 `gpt2_update` 内部的动量与偏差修正（[train_gpt2.c:L1020-L1026](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1020-L1026)）：

```c
float m = beta1 * model->m_memory[i] + (1.0f - beta1) * grad;
float v = beta2 * model->v_memory[i] + (1.0f - beta2) * grad * grad;
float m_hat = m / (1.0f - powf(beta1, t));
float v_hat = v / (1.0f - powf(beta2, t));
```

这两段就是把 4.2.2 的公式逐行翻译。CUDA 版用 `lerp(grad, m, beta1)` 替代手写 `beta1*m + (1-beta1)*grad`（[llmc/adamw.cuh:L14-L16](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L14-L16)），数学等价但只用 2 次浮点运算（`fma`），是个小优化。

#### 4.2.4 代码实践

**实践目标**：手算第 1 步某个参数的更新量，验证 4.2.2 推出的「≈ \(-\mathrm{lr}\cdot\mathrm{sign}(g)\)」结论。这是本讲的指定实践任务。

**操作步骤**：设第 1 步某参数梯度 \(g=2.0\)（任意非零值都可），把 `main` 里的超参数代入。逐行计算：

1. \(t=1\)，\(m_0=v_0=0\)。
2. \(m = 0.9 \times 0 + (1-0.9)\times 2.0 = 0.1 \times 2.0 = 0.2\)
3. \(v = 0.999 \times 0 + (1-0.999)\times 2.0^2 = 0.001 \times 4.0 = 0.004\)
4. 偏差修正：\(\hat{m} = 0.2 / (1-0.9^1) = 0.2/0.1 = 2.0\)
5. \(\hat{v} = 0.004 / (1-0.999^1) = 0.004/0.001 = 4.0\)
6. 更新量（\(w_d=0\)）：\(\mathrm{lr}\cdot\hat{m}/(\sqrt{\hat{v}}+\epsilon) = 1\mathrm{e}{-4}\times 2.0/(2.0+1\mathrm{e}{-8}) \approx 1\mathrm{e}{-4}\times 1.0 = 1\mathrm{e}{-4}\)
7. 新参数：\(\theta_1 = \theta_0 - 1\mathrm{e}{-4}\)。

**需要观察的现象**：尽管动量被 \(\beta\) 压得很小（\(m=0.2\)），但偏差修正把它还原回 \(g=2.0\)；更新量正好是 \(-\mathrm{lr}\)。

**预期结果**：第一步更新量的绝对值 ≈ `1e-4`，与 `lr` 相等，符号为负（梯度为正时参数减小）。可把 \(g\) 换成 \(-0.5\) 再算一遍，会得到更新量 \(+1\mathrm{e}{-4}\)，即 \(-\mathrm{lr}\cdot\mathrm{sign}(g)\)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `t` 必须从 1 开始（`step+1`）而不是从 0？

**参考答案**：偏差修正分母是 \(1-\beta^t\)。若 \(t=0\)，分母 \(=1-\beta^0=1-1=0\)，会触发除零。\(t\) 表示「这是第几步更新」，第一步自然记为 1。

**练习 2**：随着训练进行（比如 \(t=1000\)），偏差修正因子 \(1/(1-\beta_2^t)\)（\(\beta_2=0.999\)）大约是多少？

**参考答案**：\(0.999^{1000}\approx e^{-1}\approx 0.368\)，故修正因子 \(=1/(1-0.368)\approx 1.58\)。等 \(t\) 大到几千步，\(\beta_2^t\to 0\)，因子 \(\to 1\)，偏差修正基本失效——这也说明它只在最初阶段重要。

---

### 4.3 解耦权重衰减

#### 4.3.1 概念说明

**权重衰减（weight decay）** 是一种正则化手段：让参数本身不要长得太大，防止过拟合。最朴素的形式是每步把参数往 0 的方向拉一点：\(\theta \leftarrow \theta(1-\mathrm{lr}\cdot w_d)\)。

关键问题是**怎么把权重衰减和 Adam 的自适应更新结合起来**。有两种做法：

- **经典 Adam 的做法（L2 正则）**：把衰减混进梯度，即令 \(g' = g + w_d\cdot\theta\)，然后把 \(g'\) 喂给动量更新（\(m,v\) 都基于 \(g'\) 算）。问题在于：\(g'\) 也会被 \(\sqrt{\hat{v}}\) 缩放，于是「大梯度参数的衰减」和「小梯度参数的衰减」步长不一致，权重衰减被自适应机制扭曲了，效果偏离原始的「等比例缩小参数」。

- **AdamW 的做法（解耦权重衰减）**：动量 \(m,v\) **只用原始梯度 \(g\) 算**（不含衰减），衰减项 \(w_d\cdot\theta\) 在最后**单独**加到更新里、且只被 `lr` 缩放。这样衰减就回到了「等比例往零拉」的纯粹语义，不受自适应缩放干扰。

这就是「**解耦（decoupled）**」一词的含义：自适应步长与权重衰减各走各的路。llm.c 严格采用 AdamW 的解耦形式（注释里链接的 PyTorch 文档也是 AdamW）。

> 本工程 `main` 里传的是 `weight_decay = 0.0f`，即**默认不开启权重衰减**（在 finetune GPT-2 124M 这个小规模上不需要）。但代码完整实现了它，4.3.3 会指出它在公式里的确切位置。

#### 4.3.2 核心流程

对比两种写法（伪代码，省略偏差修正）：

```
# 经典 Adam（L2 正则，耦合）—— llm.c 没有用
g_tilde = g + wd * param
m = beta1*m + (1-beta1)*g_tilde     # 动量里混进了衰减
v = beta2*v + (1-beta2)*g_tilde^2
param -= lr * m_hat / (sqrt(v_hat)+eps)   # 衰减被 sqrt(v_hat) 扭曲

# AdamW（解耦）—— llm.c 实际用的
m = beta1*m + (1-beta1)*g           # 动量只用纯梯度
v = beta2*v + (1-beta2)*g^2
param -= lr * (m_hat/(sqrt(v_hat)+eps) + wd*param)  # 衰减单独加，只被 lr 缩放
```

一句话总结：AdamW 把 `wd*param` 从「动量的输入」挪到了「最终更新的加法项」。

#### 4.3.3 源码精读

关键就是这一行（[train_gpt2.c:L1031](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1031)），注意 `weight_decay * param` 出现在**括号内**、与自适应项 `m_hat/(sqrtf(v_hat)+eps)` **相加**，整体再乘 `learning_rate`：

```c
model->params_memory[i] -= learning_rate * (m_hat / (sqrtf(v_hat) + eps) + weight_decay * param);
```

可以把它拆成两项来理解：

\[
\theta \leftarrow \theta - \mathrm{lr}\cdot\frac{\hat{m}}{\sqrt{\hat{v}}+\epsilon} \;-\; \mathrm{lr}\cdot w_d\cdot\theta
\]

第一项是 Adam 的自适应更新（用纯梯度算的动量），第二项就是纯粹的权重衰减（参数按比例 \(\mathrm{lr}\cdot w_d\) 朝零收缩）。两者各算各的，互不污染——这就是 AdamW 的解耦精髓。CUDA 版的写法完全一致（[llmc/adamw.cuh:L40](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L40)）：

```c
float param = old_param - (learning_rate * (m / (sqrtf(v) + eps) + weight_decay * old_param));
```

当 `weight_decay = 0.0f`（如 `main` 传的值），第二项消失，更新退化为纯自适应 Adam。

#### 4.3.4 代码实践

**实践目标**：通过「改一个参数观察行为」，体会权重衰减的「等比例收缩」效果。

**操作步骤**：

1. 在 [train_gpt2.c:L1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1168)，把 `gpt2_update` 的第 5 个参数从 `0.0f` 改成一个明显值，例如 `0.1f`（这对应较大的权重衰减，仅为观察）。
2. 重新 `make train_gpt2` 并跑几步。

**需要观察的现象**：相比 `wd=0`，开启权重衰减后参数绝对值整体偏小，loss 下降曲线会有所不同（可能略慢但更稳）。**注意**：这只是为了理解而做的实验，`0.1f` 在本工程的训练设置下未必是最优值，不要当作推荐配置。

**预期结果**：你能从公式 \(\theta \leftarrow \theta(1-\mathrm{lr}\cdot w_d)\) 预测，每步参数会被额外乘以 \((1-1\mathrm{e}{-4}\times 0.1)\approx 0.99999\)，单步几乎看不出，但累积很多步会让权重整体偏小。若本地无 GPU/CPU 资源长时间运行，**待本地验证**，你也可以只读公式做纸面推导。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `weight_decay * param` 从括号内移到括号外（写成 `learning_rate * m_hat/(sqrtf(v_hat)+eps) + weight_decay*param`），更新行为有何不同？

**参考答案**：那样衰减项就不再被 `learning_rate` 缩放，相当于直接 `param -= wd*param`，即按比例 \(w_d\) 而非 \(\mathrm{lr}\cdot w_d\) 衰减——通常 \(w_d\gg\mathrm{lr}\)，衰减会强得多，参数可能被快速压向 0。正确做法是让衰减也受 `lr` 控制，这正是代码把它放在括号内、与自适应项一起乘 `lr` 的原因。

**练习 2**：为什么说 AdamW 的权重衰减比经典 Adam 的 L2 正则「更纯粹」？

**参考答案**：因为 AdamW 里衰减项不经过动量、也不被 \(\sqrt{\hat{v}}\) 自适应缩放，每个参数都按同一个 \(\mathrm{lr}\cdot w_d\) 比例朝零收缩，等价于最朴素的权重衰减语义；而经典 Adam 把衰减塞进梯度，再被自适应机制按各参数的波动重新缩放，导致衰减强度因参数而异，偏离了「等比例收缩」的初衷。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「**纸面追踪一个参数走完前两步**」的综合练习。这也是把 u2（前向）、u3-l1（反向）、u3-l2（优化器）打通的好机会。

**任务设定**：选定某个参数下标 \(i\)，假设它在第 1、2 步的梯度分别是 \(g_1=2.0\)、\(g_2=-1.0\)，初始值 \(\theta_0=0.5\)，初始动量 \(m_0=v_0=0\)。用 `main` 的超参数（`lr=1e-4, beta1=0.9, beta2=0.999, eps=1e-8, wd=0`）手算。

**步骤**：

1. **第 1 步（\(t=1\)）**：按 4.2.4 的过程算出 \(m_1=0.2, v_1=0.004, \hat{m}_1=2.0, \hat{v}_1=4.0\)，更新量 \(\approx +1\mathrm{e}{-4}\)（朝 \(-g_1\) 方向），\(\theta_1 \approx 0.5 - 1\mathrm{e}{-4} = 0.4999\)。
2. **第 2 步（\(t=2\)）**：
   - \(m_2 = 0.9\times 0.2 + 0.1\times(-1.0) = 0.18 - 0.1 = 0.08\)
   - \(v_2 = 0.999\times 0.004 + 0.001\times(-1.0)^2 = 0.003996 + 0.001 = 0.004996\)
   - 偏差修正：\(\hat{m}_2 = 0.08/(1-0.9^2)=0.08/0.19\approx 0.421\)；\(\hat{v}_2=0.004996/(1-0.999^2)=0.004996/0.001999\approx 2.499\)
   - 更新量 \(=1\mathrm{e}{-4}\times 0.421/(\sqrt{2.499}+1\mathrm{e}{-8})\approx 1\mathrm{e}{-4}\times 0.421/1.581\approx 2.66\mathrm{e}{-5}\)
   - 注意此时 \(g_2<0\)，但 \(\hat{m}_2>0\)（动量记住了上一步的正方向），所以更新量仍是正的、参数继续减小。\(\theta_2\approx 0.4999 - 2.66\mathrm{e}{-5}\approx 0.49987\)。
3. **观察**：第 2 步虽然梯度反号，但动量让方向「刹不住车」仍朝原方向走——这就是动量「惯性」的体现；同时更新量从第 1 步的 `1e-4` 变小到 `2.66e-5`，反映 \(\sqrt{v}\) 变大后自适应步长被压缩。

**延伸**（可选）：在 [train_gpt2.c:L1016](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1016) 的循环里，对固定下标 `i` 打印每步的 `grad`、`m`、`v`、更新量，对比你的手算结果，确认理解无误。

---

## 6. 本讲小结

- `gpt2_update` 把训练的「最后一步」——用梯度更新参数——实现了 **AdamW 优化器**；它对 `num_parameters` 个参数各自独立循环一遍。
- **一阶动量** `m`（梯度 EMA）平滑方向，**二阶动量** `v`（梯度平方 EMA）估计波动；两者合起来让每个参数拥有**自适应步长** \(\hat{m}/(\sqrt{\hat{v}}+\epsilon)\)。
- **偏差修正** \(1/(1-\beta^t)\) 抵消 `m`、`v` 冷启动时被 0 初值造成的低估；它让**第 1 步**的更新量恰好近似为 \(-\mathrm{lr}\cdot\mathrm{sign}(g)\)。
- **AdamW 与 Adam 的本质区别是解耦权重衰减**：衰减项 `weight_decay*param` 不混进梯度，而是作为独立加法项与自适应步一起乘 `lr`，保证「等比例收缩」的纯粹语义。
- `m_memory`/`v_memory` 采用懒分配（首次调用 `calloc` 清零），`main` 里 `step+1` 保证 `t` 从 1 开始避免除零；CUDA 版 [llmc/adamw.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh) 公式完全一致，只是并行化并多了 master weights/`grad_scale` 等工程细节。

## 7. 下一步学习建议

至此你已经走完了 CPU 参考实现里「前向 → 清零梯度 → 反向 → 更新」的完整训练四步。接下来可以：

- **u3-l3 采样与自回归生成**：看看训练出来的模型如何用 `sample_mult` 逐 token 生成文本，理解为什么生成时每个 token 都要重算前向。
- **u3-l4 数值正确性测试**：阅读 `test_gpt2.c`，理解它如何用「10 步训练 loss 比对」来保证整个前向/反向/优化器链路的数值正确性——你刚手算过的更新公式，正是那个测试要守护的对象之一。
- 若对 GPU 版优化器感兴趣，可先翻一眼 [llmc/adamw.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh)，留意 `master_params_memory` 和 `grad_scale` 两个 CPU 版没有的参数，它们分别对应 u6-l1（混合精度/master weights）和 u6-l4（多卡梯度累积）的话题，届时会展开讲。
