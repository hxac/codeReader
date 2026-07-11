# 学习率调度、MFU 估算与日志

> 本讲属于「训练工程」单元（u6）。在学完混合精度、重计算/融合算子/global norm（u6-l1、u6-l2）之后，本讲关注训练过程中三个「辅助但不可或缺」的子系统：**学习率随步数如何变化**、**怎么衡量 GPU 跑得够不够快**、**怎么把训练状态安全地写进日志**。它们都不改变模型数学，但决定了「训练能否收敛、跑得是否高效、出了问题能否回溯」。

---

## 1. 本讲目标

读完本讲后，你应当能够：

1. 说清楚 `LearningRateScheduler` 支持 `cosine` / `linear` / `wsd` / `constant` 四种调度的公式与各自形状，并解释 **warmup** 与 `final_learning_rate_frac` 两个参数的作用。
2. 理解 **MFU（Model Flops Utilization，模型算力利用率）** 的定义，能复述 `flops_per_token = 6*N + 6*L*C*T` 这一估算式的来源，并说清 `get_flops_promised` 如何按 GPU 型号 + 精度查表折算峰值算力。
3. 看懂 `Logger` 如何用「只让 rank 0 写文件 + 追加模式」保证多卡训练下的日志安全，并能读懂 `main.log` 里每一行字段的含义。

---

## 2. 前置知识

本讲默认你已经掌握（见依赖讲义 u5-l1 及更早内容）：

- **训练四步循环**：前向 → 清零梯度 → 反向 → AdamW 更新（u3-l2）。本讲不碰这四步，只讲「在这一步用什么学习率」「这一步算得快不快」「这一步的数字怎么记下来」。
- **`step` 与 `train_num_batches`**：训练是一个 `for (step = 0; step <= train_num_batches; step++)` 的主循环，学习率调度以 `step` 为自变量。
- **多 GPU 中的 `process_rank` / `num_processes`**：多卡训练时，每张卡是一个进程，`process_rank == 0` 的那张卡（rank 0）负责打印与写日志，其余卡静默。
- **梯度累积 `grad_accum_steps`**：当期望的全局 batch 大于单卡一次能装的 batch 时，把一个大 batch 拆成若干 micro-batch 累加梯度，算「一步更新」。它会影响 MFU 估算里的 token 计数。
- **`PRECISION_MODE` / `floatX`**（u5-l1、u6-l1）：编译期由 `PRECISION`（默认 BF16）决定的精度档位，本讲里它决定 `get_flops_promised` 查的是 BF16 还是 FP32 还是 FP16 的峰值。

几个本讲会反复用到的概念：

| 术语 | 含义 |
|------|------|
| **学习率（learning rate, lr）** | 每步参数更新的步长，过大发散、过小收敛慢 |
| **warmup（预热）** | 训练最开头把 lr 从 0 线性升到最大值，避免初期梯度混乱把权重带飞 |
| **衰减（decay）** | warmup 之后逐步降低 lr，帮助训练在后期稳定收敛到更优解 |
| **TFlops** | 每秒万亿次（\(10^{12}\)）浮点运算，衡量 GPU 算力 |
| **MFU** | 实际达到的算力 ÷ GPU 该精度的峰值算力，越接近 1（即 100%）越高效 |
| **NVML** | NVIDIA Management Library，运行时查询 GPU 频率/温度/利用率等状态的库 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`llmc/schedulers.h`](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h) | 定义 `LearningRateScheduler` 结构体与四种调度函数，是本讲主角之一 |
| [`llmc/mfu.h`](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h) | GPU 峰值算力数据库 `gpu_db`、`get_flops_promised` 查表函数，以及 NVML 运行时状态查询 `get_gpu_utilization_info` |
| [`llmc/logger.h`](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h) | 极简的文件日志器 `Logger`，三种 `logger_log_*` 写日志函数 |
| [`train_gpt2.cu`](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | `gpt2_estimate_mfu`（MFU 估算主函数）、命令行默认值、`main` 中三件套的装配与每步调用 |
| [`llmc/zero.cuh`](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh) | `printf0` 宏——只在 rank 0 打印，是终端输出的多进程安全保证 |

---

## 4. 核心概念与源码讲解

### 4.1 学习率调度（LearningRateScheduler）

#### 4.1.1 概念说明

为什么要「调度」学习率，而不是从头到尾用一个固定值？两个直觉：

1. **训练刚开始时权重是随机的，梯度方向噪声极大。** 如果一上来就用大 lr，参数会被带得乱跑甚至 loss 爆炸。所以前面要用一段 **warmup**，让 lr 从 0 慢慢爬到最大值，给优化器一个「找方向」的缓冲期。
2. **训练到后期，loss 曲面逐渐变平，需要更小的步长才能精修到更好的极小值。** 所以 warmup 结束后要 **衰减**，让 lr 逐渐下降。

`LearningRateScheduler` 把这两段拼成一个 `lr(step)` 函数：给定当前是第几步，返回这一步该用的学习率。它支持四种衰减形状：

- **cosine**（默认，最常用）：warmup 后按余弦曲线平滑下降。
- **linear**：warmup 后按直线下降。
- **wsd**（Warmup-Stable-Decay）：warmup 后保持常数，只在最后 20% 急剧下降。
- **constant**：完全不衰减，永远用最大 lr。

其中最关键的三个可调参数是：

- `learning_rate`：最大学习率（warmup 顶点 / 衰减起点）。
- `warmup_iterations`：warmup 持续多少步，`0` 表示不要 warmup。
- `final_learning_rate_frac`：衰减**终点**的 lr 是「最大 lr 的多少倍」。例如 `0.1` 表示衰减到峰值的 10%，`0.0` 表示衰减到 0。**注意默认值是 `1.0`，也就是终点 = 起点，意味着不衰减**（详见下面的实践）。

#### 4.1.2 核心流程

以 `cosine` 为例（`linear` 只是把余弦换成直线），分两段：

**Warmup 段**（`step < warmup_iterations`）：线性上升

\[
\text{lr}(\text{step}) = \text{lr}_{\max} \cdot \frac{\text{step}+1}{\text{warmup}}
\]

注意分子是 `step+1` 而不是 `step`，所以第 0 步就有非零（虽小）的 lr，避免完全不动。

**衰减段**（`step >= warmup_iterations`）：先算「衰减进度」`decay_ratio`（从 0 到 1），再用余弦系数 `coeff`（从 1 到 0）在「最大 lr」与「最小 lr」之间插值

\[
\text{decay\_ratio} = \frac{\text{step}-\text{warmup}}{\text{train\_num\_batches}-\text{warmup}}, \qquad
\text{coeff} = \frac{1+\cos(\pi\cdot \text{decay\_ratio})}{2}
\]

\[
\text{min\_lr} = \text{lr}_{\max} \cdot \text{final\_frac}, \qquad
\text{lr} = \text{min\_lr} + \text{coeff}\cdot(\text{lr}_{\max}-\text{min\_lr})
\]

`coeff` 从 1（`decay_ratio=0`，刚结束 warmup）平滑降到 0（`decay_ratio=1`，训练结束）。`linear` 版本把 `coeff` 换成 `1 - decay_ratio`，其余完全一样。

`wsd` 则是三段：`step < warmup` 线性升；`warmup ≤ step < 0.8*total` 保持常数；最后 20% 用 `1 - sqrt(decay_ratio)` 急降（论文 [arXiv:2405.18392](https://arxiv.org/abs/2405.18392)）。

#### 4.1.3 源码精读

**结构体与初始化**——`LearningRateScheduler` 把五个配置塞进一个结构体，`lr_scheduler_init` 只是逐字段赋值：

[llmc/schedulers.h:11-17](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L11-L17) 定义结构体；[llmc/schedulers.h:19-25](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L19-L25) 初始化。`type` 是一个字符串（`"cosine"` / `"linear"` / …），运行时用 `strcmp` 分发。

**cosine 调度**——严格对应上面的公式：

[llmc/schedulers.h:28-41](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L28-L41)。注意第 35 行 `coeff = 0.5f * (1.0f + cosf(M_PI * decay_ratio))`，注释 `// coeff starts at 1 and goes to 0` 一语道破；第 37-38 行算出 `min_lr` 并在 `min_lr` 与 `learning_rate` 之间用 `coeff` 插值。

**linear 调度**——与 cosine 共享 warmup 段，仅衰减段公式不同：

[llmc/schedulers.h:44-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L44-L55)。第 52 行 `lr = scheduler->learning_rate - decay_ratio * (scheduler->learning_rate - min_lr)`，即 `lr = lr_max - decay_ratio*(lr_max - min_lr)`，是一条从 `lr_max` 到 `min_lr` 的直线。

**wsd 与 constant**：

[llmc/schedulers.h:58-80](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L58-L80)（含 `constant` 第 58-60 行与 `wsd` 第 64-80 行）。`wsd` 第 65 行 `decay_point = (int)(0.8f * train_num_batches)` 定下「最后 20%」的起点，第 77 行用 `1.0f - sqrtf(decay_ratio)` 做平方根式急降。

**分发器**——用字符串比较选函数，未知类型直接 `exit`：

[llmc/schedulers.h:83-98](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L83-L98)。

**在 `main` 里的装配**——命令行参数读到默认值后，构造一个调度器：

[train_gpt2.cu:1665-1668](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1665-L1668) 调 `lr_scheduler_init`。对应的命令行默认值见 [train_gpt2.cu:1424-1436](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1424-L1436)：调度类型默认 `"cosine"`、`warmup_iterations` 默认 `0`、`final_learning_rate_frac` 默认 `1.0f`。命令行开关是 `-k`（类型）、`-u`（warmup）、`-q`（final frac），见 [train_gpt2.cu:1475-1476](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1475-L1476) 与 [train_gpt2.cu:1490](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1490)。

**每步取学习率**——训练循环里，算完梯度、调用 `gpt2_update` 之前，问调度器要这一步的 lr：

[train_gpt2.cu:1838-1839](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1838-L1839)，`float step_learning_rate = get_learning_rate(&lr_scheduler, step);`，随后 [train_gpt2.cu:1852](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1852) 把它作为第一个实参传给 `gpt2_update`。

#### 4.1.4 代码实践

**实践目标**：亲手算出 cosine 与 linear 两条曲线在几个关键步数的取值，画成趋势图，并解释 `final_learning_rate_frac`。

**操作步骤**（纯纸笔/计算器，无需 GPU）：

1. 取一个真实配置作为样例，来自复现脚本 [scripts/run_gpt2_124M.sh:35-36](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh#L35-L36)：`-q 0.0`（`final_learning_rate_frac=0.0`）、`-u 700`（`warmup_iterations=700`），最大 lr 取 `-l 0.0006`（`learning_rate=6e-4`），训练步数取脚本注释里的 `train_num_batches=18865`。
2. 用本节公式分别算 `step = 0, 700, 9782（约中点）, 18865` 这四个点的 cosine 与 linear 学习率。
   - `step=0`：warmup 段，lr = `6e-4 * (0+1)/700 ≈ 8.57e-7`（两种调度相同）。
   - `step=700`：刚进衰减段，`decay_ratio=0`，`coeff=1`，lr = `6e-4`（峰值，两种调度相同）。
   - `step≈9782`：`decay_ratio≈0.5`。cosine 的 `coeff=0.5*(1+cos(π/2))=0.5`，lr≈`3e-4`；linear 的 `lr=6e-4 - 0.5*6e-4=3e-4`。**两曲线在中点恰好重合**，这是两种调度的关键共性。
   - `step=18865`：`decay_ratio=1`，cosine `coeff=0`、linear 衰减到底，两者 lr 都 = `min_lr = 6e-4 * 0.0 = 0`。
3. 画出趋势：两曲线在 `[0,700]` 重合线性上升、在 `700` 到达峰值 `6e-4`、之后 cosine 是「上凸的余弦弧」、linear 是「直线下斜」、在终点都落到 0。

**需要观察的现象**：cosine 前期下降慢、后期下降也慢（在峰值和谷底都「平缓」），linear 全程匀速。这就是 cosine 通常更受欢迎的原因——它在峰值附近多停留一会儿、在谷底也温和收尾。

**`final_learning_rate_frac` 控制什么**：它决定衰减终点 `min_lr = learning_rate × final_learning_rate_frac`。
- `=0.0`：衰减到 0（`run_gpt2_124M.sh` 的选择）。
- `=0.1`：衰减到峰值的 10%（`run_gpt3_125M.sh` 与 `run_gpt2_1558M.sh` 的选择，留一点 lr 不归零）。
- `=1.0`（**默认**）：`min_lr = learning_rate`，于是 cosine/linear 的衰减段变成一条平的直线——**等价于不要衰减**。这是初学者最容易踩的坑：用默认参数跑 cosine，实际看到的是「warmup 后恒定」，并非余弦曲线。

**预期结果**：你能口头复述「默认 `final_frac=1.0` 让 cosine 失去衰减效果，必须显式传 `-q` 才会真的衰减」。

#### 4.1.5 小练习与答案

**练习 1**：若把 `warmup_iterations` 设为 0，`step=0` 时调用 `get_learning_rate_cosine` 会发生什么？
> **答案**：会进 `else` 分支（`0 < 0` 为假），`decay_ratio = 0`，`coeff = 1`，返回 `min_lr + 1*(lr - min_lr) = lr`，即第 0 步直接用最大 lr。warmup=0 等价于「无预热」。

**练习 2**：`wsd` 调度相对 `cosine` 的形状特点是什么？为什么说它适合「不知道该训多少步」的场景？
> **答案**：`wsd` 中间保持常数 lr，只在最后 20% 用 `1-sqrt` 急降。因为它的大部分训练是恒定 lr，你可以随时决定「现在进入收尾」，把训练总步数往后再延；而 cosine 的形状一开始就被 `train_num_batches` 锁死，改总步数会让整条曲线变形。

---

### 4.2 MFU 估算

#### 4.2.1 概念说明

训练一个大模型，你总会问：**「我的 GPU 跑满了吗？」** MFU（Model Flops Utilization，模型算力利用率）就是回答这个问题的指标，来自 PaLM 论文（[arXiv:2001.08361](https://arxiv.org/pdf/2001.08361) Section 2.1）。它的定义很朴素：

\[
\text{MFU} = \frac{\text{这一步模型理论上需要的浮点运算数 / 实际耗时}}{\text{GPU 在该精度下的峰值算力}}
\]

分子是「模型算力」（按理论公式估算的 FLOP/s），分母是「硬件算力」（GPU 规格表给的峰值 TFlops）。MFU 越接近 100%，说明 GPU 越是被「喂饱」了。

这里有两个关键设计选择，都体现在 `gpt2_estimate_mfu` 的注释里：

1. **用理论 FLOP 估算，而不是实测每个 kernel 的 FLOP。** 这样 MFU 反映的是「按理想数学公式，这个模型本该消耗多少算力」，与实现细节（用了什么 kernel、是否融合）解耦，方便跨实现/跨硬件对比。
2. **`flops_per_token = 6*N + 6*L*C*T`。** 其中：
   - 第一项 `6*N` 是所有权重 matmul 的贡献，`N` 是参数总量。著名的「**每个参数每个 token 消耗 6 FLOP**」经验法则：前向约 `2N`（每个参数一次乘加）、反向约 `4N`（对权重的梯度 `2N` + 对输入的梯度 `2N`，用于继续往回传），合计 `6N`。
   - 第二项 `6*L*C*T` 是注意力 matmul（QK^T 和 att@V 随序列长 T 增长）的贡献，注释里说它「通常是小头」。

注释也坦承 `N` 里其实把 LayerNorm/bias/位置嵌入这些「不参与 matmul」的参数也算进去了，会**略微高估**，但它们占比小，可忽略。

#### 4.2.2 核心流程

`gpt2_estimate_mfu(model, num_tokens, dt)` 的计算链：

1. 取 `N = model->num_parameters`、`L/C/T` 从配置里拿。
2. `flops_per_token = 6*N + 6*L*C*T`。
3. `flops_per_step = flops_per_token * num_tokens`（这一步处理的总 token 数）。
4. `flops_achieved = flops_per_step / dt`（除以耗时，得到这一步的 FLOP/s）。
5. `flops_promised = get_flops_promised(deviceProp.name, PRECISION_MODE) * 1e12`（查表得峰值，单位转成 FLOP/s）。
6. 若查不到（返回负数），返回 `-1.f` 表示「不知道」；否则 `mfu = flops_achieved / flops_promised`。

`get_flops_promised` 的查表逻辑：

1. 在 `gpu_db[]` 里线性搜 GPU 名字（`deviceProp.name`，如 `"NVIDIA A100-SXM4-80GB"`）。
2. 找到后，按 `PRECISION_MODE` 取该精度档位的基准 TFlops（BF16 取 `BF_16_32`、FP32 取 `TF_32`、FP16 取 `FP_16_32`）。
3. 因为同一代架构的不同型号「用的是同款 tensor core、只是数量和频率不同」，所以用 `value * (new_cores / CORES) * (new_mhz / CLOCK)` 按实际核心数和频率**线性缩放**得到该型号的峰值。
4. 找不到型号或该精度无数据，返回 `-1.0f`。

#### 4.2.3 源码精读

**MFU 主函数**——上面的流程一目了然：

[train_gpt2.cu:1126-1153](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1126-L1153)。第 1143 行就是招牌公式 `flops_per_token = 6 * N + (size_t)6 * L * C * T;`，注释（第 1136-1137 行）解释了「`6*N` 是权重 matmul、第二项是注意力 matmul」。第 1147 行调 `get_flops_promised`，第 1148-1150 行处理「查不到」的兜底。

**`PerfData` 与 GPU 规格常量**——每种架构一张规格表：

[llmc/mfu.h:30-39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L30-L39) 定义 `PerfData`（各精度的 TFlops + 基准核心数 `CORES` 与频率 `CLOCK`）；[llmc/mfu.h:42-46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L42-L46) 给出 VOLTA / AMPERE_DATACENTER / AMPERE_CONSUMER / HOPPER / ADA 五代架构的基准数据（来自 NVIDIA 白皮书）。注意有些档位是 `-1.f`（如 VOLTA 不支持 BF16），查到负数会报错。

**`gpu_db` 型号数据库**——每个具体型号对应一行 `{名字, 架构表指针, 实际核心数, 实际频率}`：

[llmc/mfu.h:56-95](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L56-L95)，覆盖 V100、A100、各档 RTX 30/40 系、H100 等约 40 种型号。

**`get_flops_promised`**——线性搜表 + 精度选档 + 线性缩放：

[llmc/mfu.h:97-152](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L97-L152)。第 133-135 行按精度选 `value`；第 146 行 `adjusted = value * (new_cores / CORES) * (new_mhz / CLOCK)` 做线性缩放，注释（第 112 行）举了 4080 的例子验证线性缩放成立。

**峰值 TFlops 也用于启动时的诊断打印**：

[train_gpt2.cu:1555](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1555) 在参数表里打印 `peak TFlops`，让你一眼看到这张卡的理论上限。

**运行时 GPU 状态（NVML）**——MFU 只告诉你「算力利用率」，而 `get_gpu_utilization_info` 还能告诉你「频率有没有被降、有没有过热降频」，帮你诊断为什么 MFU 上不去：

[llmc/mfu.h:154-166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L154-L166) 定义 `GPUUtilInfo`；[llmc/mfu.h:196-237](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L196-L237) 用 NVML 查询时钟、功率、温度、风扇、降频原因（`get_throttle_reason` 把位域转成人话，第 183-193 行），并对采样缓冲区求平均得到 `gpu_utilization` / `mem_utilization`。仅当编译时能找到 `<nvml.h>` 才启用（[llmc/mfu.h:7-12](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L7-L12) 的 `__has_include` 探测），否则 `get_gpu_utilization_info` 直接报错退出（[llmc/mfu.h:239-242](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L239-L242)）。

**每步打印 MFU**——训练循环里，算完耗时后估 MFU 并打印：

[train_gpt2.cu:1871-1874](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1871-L1874)。注意 [train_gpt2.cu:1871](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1871) 传入的 `num_tokens` 是 `B * T * grad_accum_steps`（**单卡**这一步处理的 token 数），所以 MFU 是**单卡指标**；而同一循环里 [train_gpt2.cu:1862](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1862) 的 `tokens_processed` 则乘了 `num_processes`，是全局吞吐——两个口径不要混淆。打印行里 `%.1f%% bf16 MFU`（[train_gpt2.cu:1872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1872)）就是这里的百分比，注意它写死了 "bf16" 字样，即使你用 fp32 训练标签也不会变（这是已知的小瑕疵）。

#### 4.2.4 代码实践

**实践目标**：手算 GPT-2 124M 模型的 `flops_per_token`，验证 `6*N` 占主导。

**操作步骤**：

1. 取 GPT-2 124M 的配置：`N ≈ 1.24e8`（参数量）、`L=12`、`C=768`、`T=1024`（来自 [u1-l3](u1-l3-cpu-reference-overview.md) 讲过的 `GPT2Config`）。
2. 代入 `flops_per_token = 6*N + 6*L*C*T`：
   - 权重项 `6*N = 6 * 1.24e8 = 7.44e8`。
   - 注意力项 `6*L*C*T = 6 * 12 * 768 * 1024 = 56,623,104 ≈ 5.66e7`。
   - 合计 `≈ 8.0e8` FLOP/token。
3. 对照 [scripts/run_gpt2_124M.sh:3](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh#L3) 的注释 `6 * 124e6 * 10e9 = 7.44e18`——这正是「`6*N` × 全训练 10B token」的总算力估计，验证了 `6*N` 是主项、注意力项在生产规模下确实可忽略。

**需要观察的现象**：注意力项 `5.66e7` 只有权重项 `7.44e8` 的约 7%，印证了注释里「注意力通常是小头」的说法。这也是 MFU 公式敢于忽略很多细节仍能近似成立的原因。

**预期结果**：你能说出「124M 模型每个 token 大约消耗 \(8 \times 10^8\) 次浮点运算，其中约 93% 来自权重 matmul（`6N`）」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gpt2_estimate_mfu` 里 `N` 用「参数总数」而非「参与 matmul 的参数数」会让 MFU **偏高**？
> **答案**：LayerNorm 的 scale/bias、各层 bias、位置嵌入 `wpe` 这些参数几乎不产生 matmul FLOP，但被算进了 `N`。于是分子（理论 FLOP）被略微夸大，导致 MFU 比真实值偏高一点点。注释指出它们占比小，影响可忽略。

**练习 2**：如果你的 GPU 型号不在 `gpu_db` 里，`get_flops_promised` 返回什么？`gpt2_estimate_mfu` 又会怎样？
> **答案**：`get_flops_promised` 走完整个循环没匹配，返回 `-1.0f`（[llmc/mfu.h:151](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/mfu.h#L151)）。`gpt2_estimate_mfu` 检测到 `flops_promised < 0` 就返回 `-1.f`（[train_gpt2.cu:1148-1150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1148-L1150)），打印时 MFU 就是 `-100%`，表示「不知道」。

---

### 4.3 日志输出（Logger）

#### 4.3.1 概念说明

训练一个模型要跑成千上万步，你必须能**事后回溯**每一步的 loss、学习率、梯度范数等指标。llm.c 用「双管齐下」：

1. **终端实时打印**：每步把关键数字打到 stdout（用 `printf0` 宏），让你盯着看。
2. **文件日志**：把同样的关键数字追加写进 `<output_log_dir>/main.log`，供事后用脚本画曲线、对比实验。

在**多卡训练**里，日志安全是个真问题：8 张卡同时跑、同时往同一个文件 `fprintf`，会互相覆盖、内容交错。llm.c 的解法简单粗暴但有效：**只有 rank 0 写文件**，其余卡连 `Logger` 都不激活。这和 `printf0` 宏（只在 rank 0 `printf`）是同一套思路。

`Logger` 被刻意设计成**无状态**：每次写日志都重新 `fopen(..., "a")`（追加模式）然后 `fclose`。这样即使程序中途崩溃，已写的内容也都落盘了，不会因为缓冲区没 flush 丢数据——代价是每条日志一次系统调用，但训练日志频率很低（每步一条），完全可接受。

#### 4.3.2 核心流程

- `logger_init(logger, log_dir, process_rank, resume)`：
  - 默认 `active = 0`。
  - 只有 `log_dir != NULL && process_rank == 0` 时才 `active = 1`，并把日志路径设成 `<log_dir>/main.log`。
  - 若 `resume == 0`（全新训练），以 `"w"` 模式打开一次再关掉，**清空旧日志**；若 `resume == 1`（断点续训），不清空，保留历史。
- 三种写日志函数（都先判 `active`，再追加写一行）：
  - `logger_log_val`：验证损失，格式 `s:<step> tel:<val_loss>`。
  - `logger_log_eval`：评测准确率（如 HellaSwag），格式 `s:<step> eval:<acc>`。
  - `logger_log_train`：训练损失 + 学习率 + 梯度范数，格式 `s:<step> trl:<train_loss> lr:<lr> norm:<grad_norm>`。

#### 4.3.3 源码精读

**`Logger` 结构体与初始化**——只有「是否激活」和「日志文件路径」两个字段：

[llmc/logger.h:14-17](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h#L14-L17) 结构体；[llmc/logger.h:19-32](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h#L19-L32) 初始化。第 22 行 `if (log_dir != NULL && process_rank == 0)` 就是「只 rank 0 激活」的判定；第 26-30 行的 `resume == 0` 分支用 `"w"` 打开再关闭来清空文件。

**三个写日志函数**——结构完全一致，只是 `fprintf` 的格式串不同：

[llmc/logger.h:34-40](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h#L34-L40)（`log_eval`）、[llmc/logger.h:42-48](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h#L42-L48)（`log_val`）、[llmc/logger.h:50-56](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/logger.h#L50-L56)（`log_train`）。每个都：判 `active` → `fopen("a")` → `fprintf` → `fclose`。

**`printf0` 宏**——终端打印的多进程安全保证，与 logger 同源：

[llmc/zero.cuh:555-556](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L555-L556)，`#define printf0(...) if (::multi_gpu_config.process_rank == 0) { printf(__VA_ARGS__); }`。注释 `convenience macro that only prints if the rank of process is zero` 说明意图。

**在 `main` 里的装配与调用**：

[train_gpt2.cu:1656-1659](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1656-L1659) 先由 rank 0 建目录、再 `logger_init`。循环里三处调用：验证损失 [train_gpt2.cu:1729](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1729)、HellaSwag 评测 [train_gpt2.cu:1748](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1748)、每步训练状态 [train_gpt2.cu:1881](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1881)。注意训练那行把上一节算出的 `step_learning_rate` 与 `grad_norm` 一起记下，于是事后你能画出「lr 调度曲线」与「loss 曲线」叠在一起对比。

#### 4.3.4 代码实践

**实践目标**：跑一段短训练，观察 `main.log` 的真实内容，并解释 `resume` 对日志的影响。

**操作步骤**：

1. 先准备好数据与权重（参考 [u1-l2](u1-l2-build-and-run.md) 与 [u1-l4](u1-l4-data-and-tokenizer.md)），编译 CUDA 主线 `make train_gpt2cu`。
2. 跑一个**很短**的训练并指定输出目录与较短步数（示例命令，参数含义见 [u1-l2](u1-l2-build-and-run.md)）：
   ```bash
   ./train_gpt2cu -i "dev/data/tinyshakespeare/tiny_shakespeare_train.bin" \
                  -j "dev/data/tinyshakespeare/tiny_shakespeare_val.bin" \
                  -e "gpt2_124M_bf16.bin" \
                  -o log_short \
                  -x 20 -u 5 -q 0.1 -l 0.0006 -b 4 -t 128 -d 512 -v 10 -s 10
   ```
   （`-o log_short` 指定日志目录、`-x 20` 只跑 20 步、`-u 5` 5 步 warmup、`-q 0.1` 衰减到 10%。）若没有 GPU 或数据未就绪，**待本地验证**。
3. 训练结束后查看日志文件：`cat log_short/main.log`。

**需要观察的现象**：`main.log` 里会看到三类行，例如：
   ```
   s:0 tel:...
   s:0 trl:... lr:0.000120 norm:...
   s:10 tel:...
   s:10 eval:...
   ```
   - `tel:` 行来自 `logger_log_val`、`eval:` 行来自 `logger_log_eval`、`trl: lr: norm:` 行来自 `logger_log_train`。
   - 观察 `lr:` 字段：它应当从 warmup 的小值升到 `0.0006`，再随 cosine 缓降（因为 `-q 0.1`）——这就是 4.1 节调度曲线的**真实落盘**。
4. 把同样的命令再跑一次但加 `-y 1`（`resume=1`）续训，对比 `main.log`：新内容会**追加**在旧内容之后，而不是清空。

**预期结果**：你能指着 `main.log` 的每一行说出它对应哪个 `logger_log_*` 函数、每个字段是什么含义，并解释 `resume=1` 时为何旧日志还在。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Logger` 每次写日志都重新 `fopen`/`fclose`，而不是开一个持久的 `FILE*`？
> **答案**：为了让已写内容**立即落盘**。训练可能跑很久、也可能中途崩溃；若用持久 `FILE*` 配带缓冲，崩溃时缓冲区里未 flush 的日志会丢失。每次 `fclose` 都强制 flush，保证日志不丢。代价是每条日志一次系统调用，但日志频率低（每步一条），开销可忽略。

**练习 2**：多卡训练时，rank 1～7 的进程调用 `logger_log_train` 会发生什么？
> **答案**：它们的 `logger->active == 0`（因为 `logger_init` 只在 `process_rank == 0` 时置 1），所以 `logger_log_train` 第一个 `if (logger->active == 1)` 判断为假，直接返回，什么也不写。这就是多卡下日志不冲突的根本原因。

---

## 5. 综合实践

把三件套串起来做一个小对比实验（**需要 GPU 与数据，待本地验证**）：

1. 用 [scripts/run_gpt2_124M.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh) 作为模板，复制成两个脚本，**只改调度相关参数**，其余（数据、B/T、模型、步数）保持一致：
   - 实验 A：`-k cosine -q 0.1 -u 700`（cosine 衰减到 10%）。
   - 实验 B：`-k linear -q 0.1 -u 700`（linear 衰减到 10%）。
   - 两个实验分别用 `-o log_cosine` 与 `-o log_linear` 输出到不同目录。
2. 各跑相同的较短步数（如 `-x 2000`，减小 `-d` 让单步更快），结束后读两份 `main.log`。
3. 用任意画图工具（Python matplotlib / gnuplot）把两份日志里的 `s:` 与 `lr:` 字段解析出来，画 lr-step 曲线，验证它与你在 4.1.4 算出的趋势一致；再把 `trl:`（train loss）画出来，对比两种调度下 loss 的下降差异。
4. 同时把日志里训练步的打印行（终端的 `... | lr %.2e | ... | %.1f%% bf16 MFU | %.0f tok/s`）与 `main.log` 对应，确认终端打印（`printf0`）与文件日志（`logger_log_train`）记录的是同一组数。

**预期结果**：你得到一张图，上面有 cosine 与 linear 两条 lr 曲线（在 warmup 段重合、在中点相交、在终点都落到 `0.1 * 6e-4`），以及对应的 loss 曲线；并能解释 `main.log` 每一行的来源。如果暂时没有 GPU，可退化为「纸笔算 lr 曲线 + 阅读 `logger.h`/`schedulers.h` 源码画流程图」的源码阅读型实践。

---

## 6. 本讲小结

- `LearningRateScheduler` 用 `step` 作自变量返回当步 lr，支持 `cosine`/`linear`/`wsd`/`constant` 四种衰减；都先经过一段线性 **warmup**，再进入衰减，衰减终点由 `final_learning_rate_frac` 控制（`min_lr = lr × frac`）。
- 默认 `final_learning_rate_frac = 1.0` 意味着「不衰减」，要让 cosine/linear 真正衰减必须显式传 `-q`（如复现脚本里的 `-q 0.0` 或 `-q 0.1`）。
- **MFU** 用理论公式 `flops_per_token = 6*N + 6*L*C*T` 估算分子，用 `get_flops_promised` 按 GPU 型号 + 精度查表（再按核心数/频率线性缩放）得到分母峰值，二者之比即为利用率；`6*N` 是主项，注意力项通常是小头。
- `gpt2_estimate_mfu` 传入的是**单卡** token 数，MFU 是单卡指标；而打印的 `tok/s` 是乘了 `num_processes` 的全局吞吐，二者口径不同。
- `Logger` 是无状态追加写日志器，**只 rank 0 激活**，三种 `logger_log_*` 分别写验证损失、评测准确率、训练（loss/lr/norm）；`resume=0` 时清空旧文件，`resume=1` 时追加。
- 终端打印的 `printf0` 宏与日志器的「只 rank 0」是同一套多进程安全思路，保证多卡下输出不冲突。

---

## 7. 下一步学习建议

- **向分布式走**：本讲的「只 rank 0 写日志」是多 GPU 的入门，下一讲 [u6-l4 多 GPU 训练：ZeRO 分片与 NCCL](u6-l4-multi-gpu-zero.md) 会讲 `num_processes`/`process_rank` 如何驱动数据分片与梯度 all-reduce，与本讲的 rank 0 概念直接衔接。
- **回到采样器**：本讲的 `lr_scheduler` 与 [u3-l3 采样与自回归生成](u3-l3-sampling-generation.md) 的 `sampler` 是训练入口里最常被命令行调的两类「策略对象」，可对照阅读它们同样朴素的 C 风格封装。
- **深读源码**：若想动手改调度，可直接在 [llmc/schedulers.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h) 里仿照 `get_learning_rate_wsd` 加一个新函数并在 [schedulers.h:83-98](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/schedulers.h#L83-L98) 的分发器里注册一个新字符串，这是 llm.c 里最低成本的二次开发练习之一。
