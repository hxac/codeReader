# GRPO 损失函数

## 1. 本讲目标

本讲是强化学习单元的核心一讲：把前两讲备好的零件（`reward_rlvr` 奖励、`sample_response` 采样）装配成一个可反传、可优化的**损失函数** `compute_grpo_loss`。

学完后你应该能够：

1. **手算组内标准化 advantage**：给定一组 rollout 奖励（如 `[1, 1, 0]`），能手推均值、样本标准差与每个 rollout 的 advantage，并知道 `torch.std` 默认除以 \(N-1\) 而不是 \(N\)。
2. **读懂 `sequence_logprob`**：理解它如何用一次 teacher-forcing 前向 + `gather` 抽出**答案区间**的逐 token 对数概率并求和，以及它与第 5 章 `avg_logprob_answer`「求和 vs 平均、带梯度 vs 推理模式」的两处关键差异。
3. **解释 policy gradient 损失** \( \mathcal{L}_{\mathrm{PG}} = -(\hat{A} \cdot \log p).mean() \)：为什么乘 advantage、为什么取负号、为什么对 logp 而非概率求梯度、为什么要 `detach()`。
4. **说明 eval/train 模式切换的影响**：rollout 前切 `eval()`、结束后按 `was_training` 恢复，以及「eval 模式 ≠ 无梯度」这个容易混淆的点。

## 2. 前置知识

本讲默认你已读完 u7-l1（RLVR 奖励）、u7-l2（rollout 采样）与 u6-l1（log-prob 打分）。用三句话复习：

- **策略（policy）**：在 RL 语境里，被训练的语言模型本身就是策略——给定 prompt \(x\)，它输出一个完整回答 \(y\)。GRPO（Group Relative Policy Optimization）源自 DeepSeekMath，相比 PPO 省去了额外的价值模型（critic），学习信号来自**一组 rollout 之间的相对比较**。
- **rollout 与奖励**：策略对一条 prompt 生成的完整回答叫一个 rollout；`reward_rlvr` 对它做「强制 `\boxed{}` 提取 + SymPy 判分」，返回 1.0 或 0.0。一条 prompt 采样多个 rollout 构成一个**组（group）**。
- **对数概率**：第 5 章的 `avg_logprob_answer` 用「答案区 token 的 logprob **平均**」给回答打分（长度归一化，适合打分）；本讲的 `sequence_logprob` 改成 **求和**（序列级联合概率的对数），因为 GRPO 里每个 rollout 拿到的是整条序列的奖励，梯度必须按整条序列的似然来缩放。

再补两个本讲要用的小概念：

- **advantage（优势）**：衡量「这条 rollout 比组内平均水平好多少」。把奖励减去组均值再除以组标准差，好于均值的为正、差于均值为负。GRPO 的「GR」就是指这个组内相对化。
- **teacher forcing（教师强制）**：把完整已知序列一次喂给因果模型，每个位置 \(t\) 的输出分布天然只看 \(t\) 之前的内容，因此一次前向就能拿到「预测每个真实 next token」的概率——u6-l1 已用过，本讲在训练侧再用一次，这次**要保留梯度**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) | 本讲主角：`sequence_logprob`（L111–L117）与 `compute_grpo_loss`（L120–L187）；`sample_response`（L53–L98）与 `reward_rlvr`（L101–L108）是上两讲精读过的依赖 |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 依赖：`render_prompt`（L413）、`extract_final_candidate`（L186）、`grade_answer`（L360），被 `compute_grpo_loss` 经 `reward_rlvr` 复用 |
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | 对照件：`avg_logprob_answer`（L100），与 `sequence_logprob` 逐行对照 |
| [tests/test_ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py) | 回归测试：`test_sequence_logprob_matches_manual_sum`（L129–L140）、`test_compute_grpo_loss_restores_training_and_returns_stats`（L143–L202），后者完整演示了本讲综合实践的打桩技巧 |
| [ch06/01_main-chapter-code/ch06_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb) | 教科书讲解：6.7 节（advantage）、6.8 节（sequence logprob）、6.9 节（pg_loss）、6.10 节（组装成 compute_grpo_loss） |

## 4. 核心概念与源码讲解

本讲拆四个最小模块：**4.1 五阶段总览与 eval/train 切换**、**4.2 advantage 计算**、**4.3 sequence_logprob**、**4.4 pg_loss**。前三个模块（4.2–4.4）是规格指定的核心，4.1 是把它们串起来的编排层。

### 4.1 compute_grpo_loss 总览：五个阶段与 eval/train 切换

#### 4.1.1 概念说明

`compute_grpo_loss` 是「一步 GRPO」的完整封装：输入一条训练样本（一道数学题），输出一个**带梯度的损失张量**和一份统计字典。它解决的的问题是：把「采样回答 → 判对错 → 变成相对分数 → 变成可反传的损失」这条链压缩进一个函数，让训练循环（下一讲 `train_rlvr_grpo`）只需 `loss.backward()` + `optimizer.step()`。

按书中 GRPO 流程图，函数分五个阶段：

1. **Stage 1 多次 rollout**：对同一条 prompt 采样 `num_rollouts` 个回答；
2. **Stage 2 收集奖励**：每个 rollout 过 `reward_rlvr` 得 1/0；
3. **Stage 3 组内标准化 advantage**：奖励变成相对分数；
4. **Stage 4 重算对数概率**：`sequence_logprob` 带梯度地重算每条 rollout 的序列 logprob；
5. **Stage 5 策略梯度损失**：\( -(\hat{A} \cdot \log p) \) 求均值。

打开源码你会注意到循环内的注释编号是「Stage 1、2、4」而非「1、2、3」——这是作者刻意标注的：为了把采样、奖励、logprob 压进**同一个 for 循环**（避免写多个循环遍历同一组 rollout），循环内先完成每条 rollout 的 1/2/4 步，循环后再统一做 2（汇总）、3（advantage）、4（stack）、5（损失）。注释编号对齐的是流程图的阶段，不是执行顺序。

#### 4.1.2 核心流程

```
compute_grpo_loss(model, tokenizer, example, device, num_rollouts, ...)
├── 断言 num_rollouts >= 2                 # 组内比较至少要两条
├── prompt = render_prompt(example["problem"])
├── was_training = model.training           # 记住调用方模式
├── model.eval()                            # rollout 用推理模式
├── for _ in range(num_rollouts):           # ── 每条 rollout ──
│     ├── Stage 1  token_ids, prompt_len, text = sample_response(...)
│     ├── Stage 2  reward = reward_rlvr(text, example["answer"])
│     ├── Stage 4  logp   = sequence_logprob(model, token_ids, prompt_len)   # 带梯度！
│     └── 收集 roll_logps / roll_rewards / samples
├── if was_training: model.train()          # 恢复调用方模式
├── Stage 2  rewards    = tensor(roll_rewards)
├── Stage 3  advantages = (rewards - mean) / (std + 1e-4)
├── Stage 4  logps      = stack(roll_logps)
├── Stage 5  pg_loss    = -(advantages.detach() * logps).mean()
└── return {"loss", "pg_loss", "rewards", "advantages", "samples", "loss_tensor"}
```

#### 4.1.3 源码精读

**入口与断言**。[reasoning_from_scratch/ch06.py:L120-L132](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L120-L132)：函数签名声明了采样超参数（默认 `temperature=0.8, top_p=0.9`，正是 u5-l2 讲过的组内多样性来源）；`assert num_rollouts >= 2` 保证组内至少两条 rollout——单条 rollout 无从「组内相对」，且样本标准差除以 \(N-1\) 会除零；三个列表 `roll_logps / roll_rewards / samples` 分别积攒带梯度的 logprob 张量、Python 浮点奖励和诊断样本。

**eval/train 模式切换**。[reasoning_from_scratch/ch06.py:L134-L135](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L134-L135) 先用 `was_training = model.training` 记住调用方状态再 `model.eval()`；[reasoning_from_scratch/ch06.py:L164-L165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L164-L165) 在循环结束后仅在 `was_training` 为真时恢复 `model.train()`。两个必须分清的事实：

- **eval() 切换的是模块行为，不是梯度**。`train()/eval()` 只影响 Dropout、BatchNorm 这类模式敏感层；梯度是否追踪由 `torch.no_grad()` / `requires_grad` 决定。所以 Stage 4 的 `sequence_logprob` 虽然在 `eval()` 之后、`train()` 之前执行，它照样构建计算图（综合实践会验证这一点）。采样那边则相反：`sample_response` 用 `@torch.no_grad()` 显式关梯度（u7-l2 讲过为何不用 `inference_mode`——inference 张量不能保存供 backward）。
- **对本书的 Qwen3Model，eval/train 切换不改变任何数值**：`qwen3.py` 里没有 Dropout 和 BatchNorm，RMSNorm/Linear/Embedding 都是模式无关的。这两行体现的是通用工程习惯（rollout 属于推理行为），也保证将来换含 Dropout 的模型时语义正确。`was_training` 的存在则是为了不污染调用方：如果某评测脚本在 eval 模式下调 `compute_grpo_loss` 做诊断，函数返回后模型仍应保持 eval。

**循环体**。[reasoning_from_scratch/ch06.py:L137-L162](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L137-L162)：每条 rollout 依次拿采样三件套（完整 `token_ids`、`prompt_len`、解码文本）、算奖励、算带梯度的 logp，并把 `gen_len = token_ids.numel() - prompt_len` 存进 `samples` 供训练循环统计响应长度。

**返回字典**。[reasoning_from_scratch/ch06.py:L180-L187](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L180-L187)：注意同时返回了 `loss`（Python 浮点，用于日志）和 `loss_tensor`（张量，供 `backward()`），以及 `advantages` 的纯数值列表——训练循环只消费这几项。

#### 4.1.4 代码实践

**实践目标**：不写一行新代码，用仓库自带的回归测试确认「模式恢复 + 损失公式」行为，并对照源码标注五个 Stage 的行号。

**操作步骤**：

1. 在仓库根目录运行（纯 CPU、哑模型、不下载任何权重，秒级完成）：

   ```bash
   pytest tests/test_ch06.py::test_compute_grpo_loss_restores_training_and_returns_stats -v
   ```

2. 打开 [tests/test_ch06.py:L143-L202](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L143-L202)，观察测试如何用 `monkeypatch.setattr` 把 `sample_response`、`reward_rlvr`、`sequence_logprob` 三个依赖替换成迭代器（喂入固定的样本、奖励 `[1.0, 0.0]`、logp `[0.4, 0.2]`），再在 [tests/test_ch06.py:L183-L202](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L183-L202) 独立复算 advantage 与 `pg_loss` 做全等断言，最后断言 `model.training is True`。
3. 对照 `ch06.py` L137–L178，把 Stage 1/2/3/4/5 各自的行号写成一列，验证循环内确实只出现 Stage 1、2、4。

**需要观察的现象**：测试通过；`result["samples"][0]["gen_len"] == 1`（token 序列长 3、prompt 长 2）；`result["loss"] == result["pg_loss"]`（本章还没有 KL 项）。

**预期结果**：1 passed。测试文件里的 monkeypatch 打桩正是本讲综合实践要手工复现的技巧。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `assert num_rollouts >= 2` 是必须的？
**答案**：GRPO 的学习信号来自组内相对比较，单条 rollout 无「组」可言；且优势公式的分母是样本标准差（除以 \(N-1\)），\(N=1\) 时未定义。

**练习 2**：如果删掉 `was_training`、在函数末尾无条件 `model.train()`，什么场景会出 bug？
**答案**：任何在 eval 模式下调用 `compute_grpo_loss` 的场景（如评测或诊断脚本）都会被函数偷偷切回 train 模式，改变调用方后续行为。`was_training` 让函数对调用方模式「无副作用」。

**练习 3**：循环内注释为什么是 Stage 1、2、4 而不是 1、2、3？
**答案**：注释编号对齐书中 GRPO 流程图的五个阶段；为了把每条 rollout 的采样、奖励、logprob 放进同一个 for 循环（避免多个循环），执行顺序与阶段编号不再一一对应，Stage 3（advantage）必须在收集完全部奖励之后才能做。

### 4.2 组内标准化 advantage：把 1/0 奖励变成相对分数

#### 4.2.1 概念说明

奖励本身是「绝对」的 1/0，但 GRPO 想要的是「这条回答在**这一组同伴**里算好还是差」。advantage 做的就是把奖励平移缩放成组内相对分：

\[ \hat{A}_i = \frac{r_i - \mu_r}{\sigma_r + \epsilon} \]

其中 \(\mu_r\) 是组内奖励均值，\(\sigma_r\) 是标准差，\(\epsilon = 10^{-4}\) 防止除零。直觉：

- 好于均值的 rollout 得**正**优势 → 损失会**抬高**它的生成概率；
- 差于均值的得**负**优势 → 损盛会**压低**它的概率；
- 全对或全错时 \(\hat{A} \equiv 0\)，这一步**不更新参数**——GRPO 只从「组内有分歧」的题里学习。这个性质还有一个隐含好处：太简单（全对）和太难（全错）的题自动被跳过，训练信号集中在模型「有时对有时错」的能力边界上。

这就是「GR」（group relative）的含义，也是 GRPO 不需要 PPO 那个价值模型（critic）的原因：**组均值就是 baseline**。

#### 4.2.2 核心流程

以 notebook 6.7 节的四条 rollout（答案分别为 `\boxed{83}`、`The correct answer is \boxed{83}`、`The final answer is 83`、`We get \boxed{38}`，标准答案 83）为例，奖励为 \([1, 1, 0, 0]\)：

1. \(\mu_r = 0.5\)，偏差 \(r_i - \mu_r = [+0.5, +0.5, -0.5, -0.5]\)；
2. 样本标准差（除以 \(N-1=3\)）：\(\sigma_r = \sqrt{(0.25 \times 4)/3} = \sqrt{1/3} \approx 0.5774\)；
3. \(\hat{A}_i = \pm 0.5 / (0.5774 + 10^{-4}) \approx \pm 0.8659\)，与 notebook 输出 `tensor([0.8659, 0.8659, -0.8659, -0.8659])` 一致。

⚠️ **高频考点**：`torch.std` 默认 `correction=1`（除以 \(N-1\)，样本标准差），不是除以 \(N\) 的总体标准差。手算时用错分母，结果就对不上代码。

#### 4.2.3 源码精读

**收集奖励**。[reasoning_from_scratch/ch06.py:L167-L168](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L167-L168)：把循环里攒下的 Python 浮点列表转成张量。注意 `reward_rlvr` 返回的是普通浮点（`float(correct)`），所以这个张量天然不带梯度——优势在数学上就是被当作常量处理的。

**标准化**。[reasoning_from_scratch/ch06.py:L170-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L170-L171)：

```python
advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-4)
```

这一行就是公式本体：`rewards.std()` 走 PyTorch 默认的样本标准差；`+ 1e-4` 兜住全对/全错时 \(\sigma_r = 0\) 的除零。得到的张量形状 `(num_rollouts,)`，均值恰为 0（偏差之和恒为零，再除同一分母不变）、标准差接近 1——「标准化」名副其实。

#### 4.2.4 代码实践

**实践目标**：亲手复现 notebook 6.7 节的 ±0.8659，并量化「样本 vs 总体标准差」两种口径的差异。

**操作步骤**（示例代码，可直接粘进 Python 交互环境）：

```python
import torch

# 1) 复现 notebook：奖励 [1,1,0,0]
rewards = torch.tensor([1.0, 1.0, 0.0, 0.0])
adv = (rewards - rewards.mean()) / (rewards.std() + 1e-4)
print(adv)                       # 期望 [ 0.8659,  0.8659, -0.8659, -0.8659]

# 2) 全对：优势全 0，本步不更新
r_all = torch.tensor([1.0, 1.0, 1.0])
print((r_all - r_all.mean()) / (r_all.std() + 1e-4))   # [0., 0., 0.]

# 3) 两种标准差口径
for c in (1, 0):
    s = rewards.std(correction=c)
    print(f"correction={c}: std={s:.4f}, "
          f"adv={(rewards - rewards.mean()) / (s + 1e-4)}")
```

**需要观察的现象**：第 1 段输出与 notebook 完全一致；第 3 段 `correction=1` 给 ±0.8659，`correction=0` 给 ±1.0000 左右（总体 std 为 0.5）。

**预期结果**：优势符号与相对比例在两种口径下不变，只有整体幅度缩放——这等价于给学习率乘了个常数系数，但会直接改变与论文/日志对比时的数值。

#### 4.2.5 小练习与答案

**练习 1**：一个 batch 里某道题 8 个 rollout 全部答对，这一步对参数的更新量是多少？为什么这反而是件好事？
**答案**：advantage 全 0，`pg_loss` 对权重的梯度为 0，不更新。好处是把训练算力自动集中到「有时对有时错」的题目上，太容易和太难的题都跳过。

**练习 2**：奖励 `[1, 1, 0]`（\(N=3\)），手算 advantage。
**答案**：\(\mu_r = 2/3\)，偏差 \([+1/3, +1/3, -2/3]\)，样本方差 \(= (1/9+1/9+4/9)/2 = 1/3\)，\(\sigma_r = \sqrt{1/3} \approx 0.5774\)，分母 \(0.5774+10^{-4} \approx 0.5775\)，故 \(\hat{A} \approx [0.5773,\ 0.5773,\ -1.1545]\)。（这正是综合实践的验证目标。）

**练习 3**：把 `1e-4` 改成 `1e-2`，对全对/全错的组没影响，对 ` [1,1,0]` 的组有什么影响？
**答案**：分母变大、优势整体变小（约缩小 1%），损失梯度的有效步长随之变小；`\epsilon` 越大越保守，但正常奖励下 \(10^{-4}\) 的量级几乎不影响数值。

### 4.3 sequence_logprob：带梯度的序列级对数概率

#### 4.3.1 概念说明

`sequence_logprob` 回答的问题是：**当前模型**给这条 rollout（prompt + 生成答案拼成的整段 token 序列）的对数概率是多少？它要满足两个 GRPO 特有的要求：

1. **序列级（求和，不平均）**：每条 rollout 只有一个奖励、一个优势，梯度必须按整条序列的联合似然缩放。若像第 5 章那样对 token 平均，就等于把学习信号隐式地按序列长度重新缩放，长 rollout 的每个 token 会被打折，扭曲策略更新。求和还有一个副作用：序列 logprob 随长度 \(T\) 线性变负，同等奖励下模型更偏好短答案——**鼓励尽早说完**。
2. **带梯度**：第 5 章的打分函数套了 `@torch.inference_mode()` 且 `.item()` 返回浮点；这里必须去掉推理模式、保留张量，让 `\log p` 进入计算图，否则没有东西可以 backward。

#### 4.3.2 核心流程

序列对数概率的数学定义（\(y_1..y_T\) 为生成 token，\(x\) 为 prompt）：

\[ \log p_W(y \mid x) = \sum_{t=1}^{T} \log p_W\!\left(y_t \mid y_{<t}, x\right) \]

实现上是 u6-l1 错位配对思想的 gather 版：

```
logits  = model(token_ids)             # (T, V)  一次 teacher-forcing 前向
logprobs = log_softmax(logits, -1)     # 位置 t 的分布预测 token t+1
selected = logprobs[:-1].gather(1, token_ids[1:])   # 错位：位置 t 抽出真实 token t+1 的 logp
answer   = selected[prompt_len - 1 :]  # 只留生成部分的 logp（起点 = prompt_len - 1）
return   sum(answer)                   # 求和，返回带 grad_fn 的标量张量
```

为什么切片起点是 `prompt_len - 1`：第一个生成 token 位于序列索引 `prompt_len`，而它的 logprob 存放在「预测它」的位置 `prompt_len - 1` 上——错位一位，所以减一。这和 u6-l1 里 `start = len(prompt_ids) - 1` 完全同源。

为什么这里**不传 KV cache**：采样阶段的 cache 是在 `no_grad` 下算出的，不携带计算图；而本函数需要对整段序列做一次全新的、可微的前向（teacher forcing），一次拿到全部位置的 logits 最直接。

#### 4.3.3 源码精读

[reasoning_from_scratch/ch06.py:L111-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L111-L117)（全文仅 7 行）：

```python
def sequence_logprob(model, token_ids, prompt_len):
    logits = model(token_ids.unsqueeze(0)).squeeze(0).float()
    logprobs = torch.log_softmax(logits, dim=-1)
    selected = logprobs[:-1].gather(
        1, token_ids[1:].unsqueeze(-1)
    ).squeeze(-1)
    return torch.sum(selected[prompt_len - 1:])
```

逐行要点：

- **`.float()`**：logits 升到 fp32 再 log_softmax，bfloat16 下的 log/exp 数值更稳（u2-l2 讲过 RMSNorm 的同类处理）。
- **`logprobs[:-1]`**：丢掉最后一个位置——它预测的是「序列结束后的下一个 token」，没有真实标签。
- **`gather(1, token_ids[1:]...)`**：对每个位置 \(t\) 从第 \(t+1\) 个真实 token 的列里抽出 logprob，等价于 fancy-indexing 写法 `logprobs[torch.arange(T-1), token_ids[1:]]`，gather 版对 GPU 更友好（notebook 6.8 节先给了 index 版草稿再换成 gather 版，两者数学等价）。
- **`torch.sum` 且不 `.item()`**：返回带 `grad_fn=<SumBackward0>` 的张量——这一行是与第 5 章 [reasoning_from_scratch/ch05.py:L100](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L100) `avg_logprob_answer` 的分水岭：那边 `torch.mean(...).item()` 返回浮点做打分，这边 `torch.sum(...)` 返回张量做损失原料。

#### 4.3.4 代码实践

**实践目标**：用仓库测试同款哑模型，验证 gather 结果与手工索引逐位一致。

**操作步骤**（示例代码，改写自 [tests/test_ch06.py:L49-L56 与 L129-L140](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L129-L140)）：

```python
import torch
from reasoning_from_scratch.ch06 import sequence_logprob

class ConstLogitModel:
    """每个位置输出同一组 logits（vocab=3）"""
    def __init__(self, base):
        self.base = torch.tensor(base, dtype=torch.float32)
    def __call__(self, token_ids):
        T = token_ids.size(1)
        return self.base.repeat(T, 1).unsqueeze(0)

model = ConstLogitModel([0.0, 0.2, 0.4])
token_ids = torch.tensor([1, 2, 0, 2])   # 前 2 个是 prompt
prompt_len = 2

out = sequence_logprob(model, token_ids, prompt_len)

# 手工复算：位置 t 的分布预测 token t+1；只汇总答案区（t >= prompt_len-1）
lp = torch.log_softmax(model.base, dim=-1)
manual = lp[token_ids[1:]][prompt_len - 1:].sum()
print(out, manual, torch.allclose(out, manual))
```

**需要观察的现象**：`out` 与 `manual` 完全相等；`out` 是张量且 `out.requires_grad` 可由外部控制（本例模型无参数所以无梯度，综合实践里换 `nn.Module` 即可看到 `grad_fn`）。

**预期结果**：`True`。请自行数一数错位关系：T=4 的序列只有前 3 个位置（0、1、2）产出分布，位置 0 预测 token_ids[1]=2、位置 1 预测 token_ids[2]=0、位置 2 预测 token_ids[3]=2，于是 `token_ids[1:] = [2, 0, 2]` 对应的三个 logprob 中，切片 `[prompt_len-1:] = [1:]` 只留下后两个——即 prompt（前 2 个 token）之后「生成部分」的 token 0 和 token 2 参与求和。若你的理解与此一致，说明错位切片你已经吃透。

#### 4.3.5 小练习与答案

**练习 1**：切片为什么是 `prompt_len - 1` 而不是 `prompt_len`？
**答案**：位置 \(t\) 的输出分布预测 token \(t+1\)；第一个生成 token 的索引是 `prompt_len`，它的 logprob 存放在位置 `prompt_len - 1` 上，切片从那里开始恰好覆盖全部生成 token。

**练习 2**：与 `avg_logprob_answer` 相比有哪两处本质差异，各自为什么？
**答案**：① sum vs mean——GRPO 按整条序列的联合似然缩放梯度，平均会按长度隐式打折；打分场景才需要长度归一。② 不套 `inference_mode`、不 `.item()`——训练需要 `grad_fn` 张量参与 backward，推理模式会切断计算图。

**练习 3**：两条奖励相同的 rollout，一条 10 个 token、一条 100 个 token，谁的序列 logprob 更可能更小？这会引导模型往哪个方向走？
**答案**：更长的 logprob 是 100 项之和，通常更负；同等优势下，压低长答案概率、抬高短答案概率的梯度收益更大，于是模型被引导「尽早作答」——notebook 6.8 节明确指出这是序列级 logprob 的固有偏好。

### 4.4 pg_loss：优势加权的策略梯度损失

#### 4.4.1 概念说明

最后一步把 advantage 和 logprob 相乘。策略梯度的目标是**最大化期望优势**：

\[ J(W) = \mathbb{E}_{y \sim p_W}\!\left[\hat{A}(y)\right], \qquad \nabla_W J = \mathbb{E}\!\left[\hat{A}\, \nabla_W \log p_W(y \mid x)\right] \]

这就是 REINFORCE（score function 估计器）：梯度方向是「优势 × 对数似然的梯度」。落实到 \(N\) 条 rollout 的 Monte Carlo 估计并取负号（因为优化器做的是**最小化**）：

\[ \mathcal{L}_{\mathrm{PG}} = -\frac{1}{N}\sum_{i=1}^{N} \hat{A}_i \sum_{t=1}^{T_i} \log p_W\!\left(y_t^{(i)} \,\middle|\, y_{<t}^{(i)}, x^{(i)}\right) \]

内层求和是 `sequence_logprob`，外层乘优势再平均就是 `pg_loss`。直觉读法：**给好回答的整条序列加权增概率，给坏回答的整条序列加权减概率，权重就是组内相对分数**。

两个必答的「为什么」（综合实践要求你用自己的话写 3–5 句）：

- **为什么取负号**：PyTorch 优化器沿负梯度方向下降（最小化）；我们要上升（最大化期望优势），最小化 \(-J\) 与最大化 \(J\) 等价。
- **为什么对 logp 而非概率 p 求梯度**：策略梯度定理给出的就是 \(\nabla \log \pi\) 形式；log 把序列概率的连乘 \(\prod_t p(y_t)\) 化为逐 token 之和，数值稳定（连乘几百个小于 1 的数会下溢），且 \(\nabla_W \log p_W(y_t)\) 恰好就是 softmax 交叉熵的梯度形式，实现上就是一次前向 + `log_softmax` + gather。

#### 4.4.2 核心流程

用 notebook 6.9 节的真实数字走一遍（四条 rollout 的优势 ±0.8659，序列 logprob 分别为 -7.9243、-20.1546、-16.6130、-23.3677）：

| rollout | \(\hat{A}_i\) | \(\log p_i\) | \(\hat{A}_i \cdot \log p_i\) |
| --- | --- | --- | --- |
| `\boxed{83}`（对，最短） | +0.8659 | -7.9243 | -6.862 |
| `The correct answer is \boxed{83}`（对，长） | +0.8659 | -20.1546 | -17.452 |
| `The final answer is 83`（错格式） | -0.8659 | -16.6130 | +14.387 |
| `We get \boxed{38}`（错值） | -0.8659 | -23.3677 | +20.234 |

均值 ≈ +2.577，取负得 \(\mathcal{L}_{\mathrm{PG}} \approx -2.58\)（notebook 全精度输出 -2.5764；用表中四位小数复算得 -2.5767，差异只是输入截断）。注意**负的损失值不代表模型变差**——它只说明当前优势加权的对数似然为正；真正驱动更新的是梯度方向。

#### 4.4.3 源码精读

[reasoning_from_scratch/ch06.py:L173-L178](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L173-L178)：

```python
logps = torch.stack(roll_logps)              # Stage 4: (num_rollouts,)

pg_loss = -(advantages.detach() * logps).mean()   # Stage 5
loss = pg_loss  # In the next chapter we add a KL term here
```

- **`torch.stack`**：把循环里攒的单元素张量堆成 `(num_rollouts,)`，堆叠保留每条 rollout 的计算图（`grad_fn=<StackBackward0>`），梯度能沿各自的前向路径回传到共享的模型权重。
- **`advantages.detach()`**：把优势切成常量——学习信号本身不参与梯度。本实现里优势源自 `grade_answer` 的 Python 浮点，本就没有图，`detach` 是把「优势只当权重、只对 logp 反传」这一语义显式写进代码（notebook 6.9 节原话：treat the advantages as fixed learning signals）。
- **`loss = pg_loss` 与那行注释**：这是第 7 章的伏笔——KL 正则、clip ratio 等改进都会挂在 `loss` 上，`pg_loss` 保持纯策略梯度项，便于消融对比。

#### 4.4.4 代码实践

**实践目标**：用上表数字手工复现 `pg_loss`，确认与 notebook 一致，并验证「乘 advantage 改变的是梯度方向」。

**操作步骤**（示例代码）：

```python
import torch

advantages = torch.tensor([0.8659, 0.8659, -0.8659, -0.8659])
logps = torch.tensor([-7.9243, -20.1546, -16.6130, -23.3677],
                     requires_grad=True)

pg_loss = -(advantages.detach() * logps).mean()
pg_loss.backward()
print("pg_loss =", pg_loss.item())    # 期望 ≈ -2.5767
print("d/dlogp =", logps.grad)        # 期望 [-0.2165, -0.2165, +0.2165, +0.2165]
```

**需要观察的现象**：损失 ≈ -2.58；梯度符号与优势同号——优势为正的 rollout，其 logp 的梯度为负（最小化损失 = 增大该 logp）；优势为负的恰好相反。

**预期结果**：`logps.grad = -advantages / 4`。梯度大小相同（4 条优势绝对值相等时），方向由优势符号决定——这就是「好回答增概率、坏回答减概率」在微积分层面的样子。

#### 4.4.5 小练习与答案

**练习 1**：删掉负号、改成 `+(advantages * logps).mean()` 会发生什么？
**答案**：优化器最小化该目标等于**最小化**优势加权似然——好回答概率被压低、坏回答被抬高，模型朝反方向退化。负号是把「梯度上升」翻译成 PyTorch「梯度下降」的桥梁。

**练习 2**：如果去掉 `.detach()`，本实现里结果会变吗？为什么仍然建议保留？
**答案**：不会变——优势张量由无梯度的奖励浮点构造，本来就不在计算图里。保留它是显式声明「优势是固定学习信号」的防御性写法，防止将来奖励改成可微（如神经网络奖励模型）时梯度意外流入优势项。

**练习 3**：为什么 loss 里外层用 `.mean()` 而不是 `.sum()`？
**答案**：`.mean()` 把损失对 rollout 数量归一，使梯度尺度与 `num_rollouts` 无关，学习率不必随组大小调参；`.sum()` 会让 8 条组的梯度天然是 2 条组的 4 倍。

## 5. 综合实践

**任务**：手算 `num_rollouts=3`、`rewards=[1,1,0]` 的 advantage，再用微型模型跑通 `compute_grpo_loss` 验证手算，最后用自己的话解释 pg_loss 的负号与「对 logp 求梯度」。

### 第一步：手算

- 均值 \(\mu_r = 2/3\)，偏差 \([+1/3,\ +1/3,\ -2/3]\)；
- 样本标准差（`torch.std` 默认除以 \(N-1=2\)）：\(\sigma_r = \sqrt{(1/9+1/9+4/9)/2} = \sqrt{1/3} \approx 0.5774\)；
- advantage \(\approx 0.5773,\ 0.5773,\ -1.1545\)（分母 \(0.5774 + 10^{-4}\)）。

### 第二步：微型模型验证

设计思路：不打桩奖励和 logprob——奖励走**真实的** `reward_rlvr`（用三条精心构造的回答文本产生 `[1,1,0]`），logprob 走**真实的** `sequence_logprob`（微型 `nn.Module` 提供可微 logits）；只打桩 `sample_response`（跳过真实采样）。这正是 `tests/test_ch06.py` 中 monkeypatch 的手工版。

保存为 `grpo_loss_mini.py`（示例代码，仓库根目录运行；纯 CPU、无需下载权重）：

```python
import torch
import torch.nn as nn
import reasoning_from_scratch.ch06 as ch06

torch.manual_seed(123)

class MicroModel(nn.Module):
    """微型策略：emb(10,8) + linear(8,10)，输出 (b, T, 10) logits"""
    def __init__(self):
        super().__init__()
        self.emb = nn.Embedding(10, 8)
        self.head = nn.Linear(8, 10)

    def forward(self, token_ids):
        return self.head(self.emb(token_ids))

model = MicroModel()
model.train()  # 模拟训练循环中的调用状态

# 打桩 sample_response：前两条答对（\boxed 且正确），第三条答错
responses = [
    (torch.tensor([1, 2, 3]), 2, r"The answer is \boxed{42}"),
    (torch.tensor([1, 2, 4]), 2, r"So we get \boxed{42}"),
    (torch.tensor([1, 2, 5]), 2, "I cannot solve it"),
]
queue = iter(responses)

def fake_sample_response(**kwargs):
    print("  rollout 发生时 model.training =", kwargs["model"].training)
    return next(queue)

real_sample = ch06.sample_response     # 打桩前保存真函数
ch06.sample_response = fake_sample_response          # 打桩

stats = ch06.compute_grpo_loss(
    model=model,
    tokenizer=None,                    # 打桩后真实分词器不再被使用
    example={"problem": "What is 6*7?", "answer": "42"},
    device="cpu",
    num_rollouts=3,
)

ch06.sample_response = real_sample     # 恢复，避免污染同进程后续调用

print("rewards    =", stats["rewards"])       # 期望 [1.0, 1.0, 0.0]
print("advantages =", stats["advantages"])    # 期望 ≈ [0.5773, 0.5773, -1.1545]
print("loss       =", stats["loss"])
print("调用后 model.training =", model.training)   # 期望 True（was_training 恢复）

# eval 模式 ≠ 无梯度：backward 一次，确认梯度存在
stats["loss_tensor"].backward()
print("emb.grad is None =", model.emb.weight.grad is None)   # 期望 False
```

（这种「保存-替换-恢复」的手工打桩正是 pytest `monkeypatch.setattr` 自动帮你完成的事；忘记恢复会让同进程里后续所有调用 `sample_response` 的代码都拿到哑版本。）

**需要观察的现象**：

1. 三次 rollout 打印里 `model.training = False`（函数内切了 eval）；
2. `rewards = [1.0, 1.0, 0.0]`——真实 `reward_rlvr` 只认 `\boxed{}` 且判分正确的前两条；
3. `advantages` 与手算一致（±0.5773 / -1.1545，受浮点影响末位可能有微差）；
4. 调用后 `model.training = True`；
5. `backward()` 成功、`emb.weight.grad` 非空——证明 `sequence_logprob` 在 eval 模式下照样建图。

**预期结果**：以上五点全部命中。具体打印格式与末位小数**待本地验证**（本讲义未代跑；数值由源码与手算推导）。

### 第三步：写 3–5 句解释

用你自己的话回答「为什么 pg_loss 取负号、为何对 logp 而非概率求梯度」。参考要点（来自源码与 notebook 6.9 节，请改写成自己的句子）：

> PyTorch 优化器做的是最小化，而策略梯度要最大化优势加权的对数似然，取负号把「上升」翻译成「下降」。对 logp 求梯度是因为策略梯度定理给出的估计量就是 \(\hat{A}\,\nabla\log\pi\)：log 把整条序列概率的连乘化成逐 token logprob 之和，避免几百个概率连乘下溢，也让梯度形式退化为一次 softmax 交叉熵的反传。若直接对概率 p 求梯度，得到的既不是 REINFORCE 估计量，数值上也不可行。

## 6. 本讲小结

- `compute_grpo_loss` 把一步 GRPO 编排成五个阶段：**采样 rollout → 收集奖励 → 组内标准化 advantage → sequence_logprob 重算带梯度 logprob → pg_loss**；循环内只做 Stage 1/2/4 是为了单循环实现。
- **advantage** \( \hat{A}_i = (r_i - \mu_r)/(\sigma_r + 10^{-4}) \)：组均值当 baseline（GRPO 免 critic 的原因），`torch.std` 是样本标准差（÷\(N-1\)），全对/全错时优势全零、该步不更新。
- **sequence_logprob**：一次 teacher-forcing 前向 + `gather` 错位抽取 + 从 `prompt_len-1` 起切片 + **求和**（不平均）——序列级、带 `grad_fn`，与第 5 章打分函数在「sum/mean」与「梯度/推理模式」两个维度上分道扬镳。
- **pg_loss** \( =-(\hat{A}.detach() \cdot \log p).mean() \)：负号把最大化翻译成最小化；logp 源自策略梯度定理且数值稳定；`detach` 声明优势是固定学习信号。
- **eval/train 切换**只改变模块行为（Dropout/BatchNorm），不控制梯度；rollout 前 `eval()`、按 `was_training` 恢复，对无 Dropout 的 Qwen3 数值无影响，但保证了函数对调用方模式无副作用。
- `loss = pg_loss` 处的注释是第 7 章伏笔：KL 正则、clip ratio 都将挂在 `loss` 上。

## 7. 下一步学习建议

- **下一讲 u7-l4（GRPO 训练循环）**：看 `train_rlvr_grpo`（[reasoning_from_scratch/ch06.py:L222-L338](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L222-L338)）如何把本讲的 `loss_tensor` 接进 `zero_grad → backward → clip_grad_norm_ → step` 的标准八阶段循环，以及 CSV 日志、checkpoint 与批量化脚本。
- **第 7 章改进预告**：`compute_grpo_loss` 里那行「In the next chapter we add a KL term here」将在单元八逐一兑现——先读 `ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py`（新旧策略比值裁剪），它直接改写本讲的 Stage 5。
- **源码延伸**：对照 DeepSeekMath/DeepSeek-R1 论文中的 GRPO 目标函数，找找本实现省略了哪些项（如重要性采样比、KL 项的符号约定），为单元八做概念铺垫。
