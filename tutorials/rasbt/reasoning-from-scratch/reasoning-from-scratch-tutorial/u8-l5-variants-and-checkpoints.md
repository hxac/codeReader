# 训练变体与 GRPO checkpoint 使用

## 1. 本讲目标

本讲是「GRPO 改进」单元的收官之作，前四讲逐一叠加了跟踪、裁剪、KL、格式奖励四种改进，本讲把视野拉远：

1. **纵览社区改进全景**：GRPO 自 2024 年 4 月（DeepSeekMath）问世以来，学术界提出了至少 15 条改进。本讲对照三个进阶训练脚本，看它们各自动了损失函数的哪个零件。
2. **精读三个 7_7 变体**：`olmo3_style.py`（改进 1–7）、`deepseek_v32_style.py`（改进 8–12）、`gdpo.py`（改进 13），理解「Olmo 3 风格」「DeepSeek-V3.2 风格」「GDPO」分别代表哪一类思路。
3. **打通 checkpoint 复用链路**：用 `download_qwen3_grpo_checkpoints` 下载作者发布的训练检查点，按 `usage_example.py` 的方式加载进 `Qwen3Model` 并流式生成文本。
4. **学会评估与质疑**：用仓库公开的真实训练日志评估各改进的实际收益（基线终点 33.0% vs 裁剪版终点 41.6%），并认识到三个 7_7 变体**没有**发布配套日志这一局限。

学完本讲，你应当能把「读论文提出的改进」落到「读仓库里对应的代码行」，并能把任何一个 GRPO checkpoint 变成一个能对话的模型。

## 2. 前置知识

本讲假设你已完成单元八前四讲（u8-l1 到 u8-l4），以下是关键结论的快速回顾，外加两个新术语。

**已建立的认知（不再重复推导）：**

- **策略比与裁剪**（u8-l2）：`ratio = exp(new_logp − old_logp)`，`obj = min(r·A, clamp(r, 1−ε, 1+ε)·A)` 取悲观下界；每步 deepcopy 一份 `old_model`，`inner_epochs` 内层循环让数据变为 off-policy。
- **KL 正则**（u8-l3）：`kl_coeff · mean(new_logps − ref_logps)` 是逐 token KL 的序列级蒙特卡洛估计，**求和式**会随答案变长而增长；β=0.001 在数学训练中反而 60–75 步崩溃。
- **格式奖励**（u8-l4）：奖励从单一正确性扩展为 `正确性 + w × 格式`，权重过大会触发「答错但格式好」的奖励黑客；7_6 版本改为训练已会写 `<think>` 标签的 reasoning 变体模型。
- **训练骨架**（u7-l4）：`train_rlvr_grpo` 八阶段循环 + `save_checkpoint` 存 `state_dict` + 追加式 CSV 日志；`skip_zero_adv` 在优势全零时跳过更新。
- **下载基础设施**（u1-l3）：`download_file` 主 URL（Hugging Face）失败自动切备用 URL（Backblaze B2），本地大小与 `Content-Length` 严格相等才算缓存命中；上层代码只经 `qwen3.py` 门面取权重。

**本讲新术语：**

- **on-policy / off-policy（同策略 / 离策略）**：rollout 由当前策略采样、立刻用来更新当前策略，是 on-policy；`inner_epochs > 1` 时，第二轮更新用的 rollout 来自「一步之前的旧模型」，数据已经 off-policy。如何安全地吃 off-policy 数据，是 DeepSeek-V3.2 风格变体的主题。
- **token 级 vs 序列级损失**：把整条 rollout 的 logprob 求和成一个数再算损失，是序列级；保留逐 token 的 logprob、对全体 token 求平均，是 token 级。长答案在序列级下天然权重更大（每个 token 都往和里加一份），token 级则显式消解了这种长度偏差。
- **`state_dict`**：PyTorch 把模型所有可学习参数（含名称）序列化成的有序字典。GRPO checkpoint 就是一个纯 `state_dict` 文件——它**不是** Hugging Face Transformers 格式，模型结构必须由本地代码（`Qwen3Model` + `QWEN_CONFIG_06_B`）重建，再把权重灌进去。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch07/03_rlvr_grpo_scripts_advanced/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/README.md) | 15 条 GRPO 改进清单与三个 7_7 变体脚本的分工说明 |
| [ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py) | 变体一：实现改进 1–7（DAPO / Dr. GRPO 风格） |
| [ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py) | 变体二：实现改进 8–12（DeepSeek-V3.2 风格） |
| [ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py) | 变体三：实现改进 13（GDPO 分奖励归一化），基于 7_6 格式奖励版 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | `download_qwen3_grpo_checkpoints`：GRPO checkpoint 下载门面 |
| [ch07/04_download_trainining_checkpoints/usage_example.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/usage_example.py) | 下载 checkpoint → 选分词器 → 加载模型 → 流式生成 的完整示例 |
| [ch07/04_download_trainining_checkpoints/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/README.md) | checkpoint 家族说明、分词器选择规则、可用步数列表 |
| [ch07/02_logs/](https://github.com/rasbt/reasoning-from-scratch/tree/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs) | 作者发布的真实训练日志（7_3–7_6 与 ch06 基线，**无 7_7 变体日志**） |

三个变体脚本都是自包含的训练程序（各约 700 行），公共骨架（`sample_response`、训练循环、日志、checkpoint）彼此相同，差异集中在**损失函数与优势计算**上——这正是本讲要逐行对比的部分。

## 4. 核心概念与源码讲解

先看总纲。仓库 README 把 15 条改进编号列出，三个脚本各认领一段（编号与代码注释中的 `# (n)` 一一对应）：

- **olmo3_style.py → 改进 1–7**：零梯度过滤、主动采样、token 级损失、去 KL、非对称裁剪、截断重要性采样、优势不除以标准差。
- **deepseek_v32_style.py → 改进 8–12**：KL 领域化（数学域取零）、重加权 KL、off-policy 序列掩码、采样掩码贯穿训练、保留原版优势归一化。
- **gdpo.py → 改进 13**：多奖励分量先各自组内归一化，再聚合。

编号清单见 [ch07/03_rlvr_grpo_scripts_advanced/README.md:L22-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/README.md#L22-L50)，其中 L24–L38 是完整的 15 条列表（附论文链接），L44–L50 说明三个脚本各自实现的编号区间。

### 4.1 变体一：olmo3_style.py——「去掉偏差与浪费」（改进 1–7）

#### 4.1.1 概念说明

DAPO（2025）与 Dr. GRPO（2025）这批工作的共同观察是：**原始 GRPO 里藏着几处系统性偏差和算力浪费**。Olmo 3（2025）在真实训练中采用了这批修正，本脚本把它们叠加在 7_5 版骨架上。七条改进可归为三组：

- **省算力**：(1) 零梯度信号过滤——全对或全错时优势全零，更新毫无意义，不如直接换题；(2) 主动采样——发现零信号就换下一道题重采样，最多试 8 次。
- **消偏差**：(3) token 级损失——不再把整条序列的 logprob 求和，长答案不再天然获得更大的梯度；(7) 优势不除以标准差——Dr. GRPO 指出除以 std 会引入与奖励分布形状相关的隐式长度偏差。
- **改更新规则**：(4) 默认关掉 KL；(5) clip-higher——上界放宽到 `1+10`、下界仍收在 `1−0.2`，鼓励「变好」的大步、限制「变坏」的大步；(6) 截断重要性采样——ratio 先单独截一次上界，防止单个 token 的概率比爆炸。

#### 4.1.2 核心流程

```text
每个训练步：
  deepcopy 当前模型 → old_model（冻结）
  尝试最多 8 次（主动采样）：
      取第 (step + attempt) 道题
      用 old_model 采样 num_rollouts 条 rollout，收集奖励
      若奖励全相同（零梯度信号）→ 换下一道题重试
      否则跳出尝试循环
  优势 = 奖励 − 组均值                      # 改进 7：不除以 std
  对每条 rollout：
      逐 token 重算 new_logp（带梯度）       # 改进 3：token 级
      ratio = exp(new_logp − old_logp)       # 逐 token 的比值
      ratio = clamp(ratio, max=1+10)         # 改进 6：截断重要性采样
      clip_ratio = clamp(ratio, 1−0.2, 1+10) # 改进 5：非对称裁剪
      token_obj = min(ratio·A, clip_ratio·A)
  obj = 全部 rollout 的所有 token_obj 拼接后取均值
  loss = −obj + kl_coeff × KL               # 改进 4：kl_coeff 默认 0
```

与 7_4 版（u8-l2）最大的结构差异：那里 `ratio` 是**每条序列一个数**（logp 先求和再相减），这里 `ratio` 是**每个 token 一个数**，最后跨序列拼接取均值。

#### 4.1.3 源码精读

**改进 3——函数改名即立场。** 序列级函数被重命名为 token 级版本，返回值从「求和后的标量」变成「逐 token 张量」：

[olmo3_style.py:L82-L94](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py#L82-L94)——注释明确写着「改名以体现 token 级 logprob（DAPO）」；当 `return_entropy=False` 时直接返回 `selected_answer_logprobs`（答案区逐 token 的 logprob 向量），**不再调用 `torch.sum`**。求和被推迟到损失聚合处，且是对「目标项」求均值而非对「logprob」求和。

**改进 7 + 改进 1——优势计算与零信号检测：**

[olmo3_style.py:L185-L188](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py#L185-L188)——`advantages = rewards - rewards.mean()`，除以 `std` 的操作被删除（注释标明 Dr. GRPO）；紧接着 `zero_grad = rewards.max() == rewards.min()` 用「奖励全同」作为零梯度信号的判据。注意它比 `skip_zero_adv` 的 `torch.allclose` 判据更宽：只要最大最小相等（哪怕是全 1），就视为无信号。

**改进 5 + 6——token 级的非对称裁剪与截断：**

[olmo3_style.py:L211-L236](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py#L211-L236)——对每条 rollout：`new_logp` 是 token 级向量，`log_ratio = new_logp - old_logp` 逐元素相减得到逐 token 的 ratio；L220 先 `clamp(ratio, max=1.0+clip_eps_high)` 截掉上界（改进 6）；L221–L223 再构造非对称裁剪区间 `[1−clip_eps_low, 1+clip_eps_high]`（改进 5，默认 0.2 与 10.0）；L228 取 `min(ratio·A, clipped·A)` 的悲观下界；L236 把所有 rollout 的 `token_obj` 用 `torch.cat` 拼成一个大向量再 `.mean()`——这就是 token 级损失：**每个生成的 token 权重相等，与答案长短无关**。

**改进 2 + 4——训练循环里的主动采样与默认无 KL：**

[olmo3_style.py:L357-L366](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py#L357-L366)——函数签名里 `kl_coeff=0.0`（改进 4，注释注明 DAPO 与 Dr. GRPO 都建议不用 KL），并新增 `active_sampling=True` 与 `max_active_sampling_tries=8` 两个参数。

[olmo3_style.py:L387-L409](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py#L387-L409)——主动采样的实现：内层 `for attempt in range(max_active_sampling_tries)` 每次取 `math_data[(step + attempt) % len(math_data)]`（换题），算完损失后若 `active_sampling and stats["zero_grad"]` 为真就继续循环换下一题，直到拿到有梯度信号的一组 rollout；8 次尝试都失败则 `continue` 跳过本步。这就是「把算力花在有学习信号的题目上」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「除不除以 std」如何改变优势的尺度，并观察 token 级与序列级损失在长答案上的权重差异。

**操作步骤**（示例代码，可在已装好环境的任意 Python 会话中运行，仅需 torch）：

```python
# 示例代码：对比 Dr. GRPO 与原始 GRPO 的优势计算
import torch

rewards = torch.tensor([1., 1., 0., 0.])

# 原始 GRPO（除以样本标准差，u7-l3 的做法）
adv_grpo = (rewards - rewards.mean()) / (rewards.std() + 1e-4)

# Dr. GRPO / olmo3_style（改进 7：不除以 std）
adv_dr = rewards - rewards.mean()

print(adv_grpo)   # tensor([0.8659, 0.8659, -0.8659, -0.8659])
print(adv_dr)     # tensor([0.5, 0.5, -0.5, -0.5])

# 再看一个奖励尺度不同的组，观察两种算法对「尺度」的敏感性
rewards2 = torch.tensor([3., 3., 0., 0.])
print((rewards2 - rewards2.mean()) / (rewards2.std() + 1e-4))  # 仍是 ±0.866
print(rewards2 - rewards2.mean())                              # 变成 ±1.5
```

**需要观察的现象**：第一组里两种算法只差一个固定倍数（0.866 vs 0.5），排序不变；第二组里不除 std 的优势随奖励尺度**线性放大**（±1.5），而除 std 的版本保持 ±0.866 不变。

**预期结果**：除 std 相当于把每步的优势自动归一到单位方差（等价于自适应调学习率），Dr. GRPO 认为这恰恰引入了与奖励分布相关的偏差，宁可让优势保持「奖励的原生尺度」、把尺度控制交给学习率。生成文本类实验需下载模型，本实践为纯张量计算，结果可直接复现。

#### 4.1.5 小练习与答案

**练习 1**：`olmo3_style.py` 里 `zero_grad`（L187）和 `skip_zero_adv`（L196）都检测「无信号」，它们有什么区别？

**答案**：`zero_grad` 用 `rewards.max() == rewards.min()` 判据，供**主动采样**使用——为真时换一道题重新采样；`skip_zero_adv` 用 `torch.allclose(advantages, 0)` 判据，供**跳过更新**使用——为真时直接返回 `loss_tensor=None`，本步不做反向传播。前者是「换食材」，后者是「这顿不吃了」；主动采样开启时通常轮不到后者兜底。

**练习 2**：为什么 clip-higher 要把上界放宽到 `1+10` 而下界仍收在 `1−0.2`？

**答案**：非对称裁剪的直觉是：ratio > 1 表示新策略比旧策略**更看重**这条轨迹。若这条轨迹优势为正（好答案），大胆加码通常无害且有利于熵的维持（低概率 token 被选中也应该能大幅提升概率）；若优势为负（坏答案），下界收紧到 `1−0.2` 防止模型「一步否决」、把某 token 概率砸穿。对称的 ±0.2 会同时封死正方向的探索空间，DAPO 认为这会导致熵过快坍缩。

**练习 3**：token 级损失（改进 3）如何消解长度偏差？

**答案**：序列级损失对每条 rollout 的 logprob 求和，答案越长、|logp| 越大、该 rollout 在 `.mean()` 里的梯度贡献越大——模型隐式地「重视长答案」。token 级损失把所有 rollout 的所有答案 token 拼接后取均值，每个 token 权重相等，长答案不再因为长而获得额外话语权（u8-l3 提到的「求和式 KL 激励长输出」是同一种偏差在 KL 上的体现）。

### 4.2 变体二：deepseek_v32_style.py——「off-policy 纪律」（改进 8–12）

#### 4.2.1 概念说明

DeepSeek-V3.2 风格变体的主题是：**当 `inner_epochs > 1` 让数据 off-policy 之后，如何既复用 rollout 省算力、又不让更新失控**。五条改进围绕这条主线：

- **(8) KL 领域化**：KL 系数按任务域设定，数学域直接取 0（社区共识，见 u8-l3 的崩溃实验）；且 KL 关闭时**连 `ref_model` 都不构建**，省下整整一份模型显存。
- **(9) 重加权 KL**：若仍要开 KL，把旧策略重要性比 `exp(new−old)` 乘进 KL 项，修正 off-policy 数据上的估计偏差（u8-l3 末尾提过的缓解方案 3，这里有了实现）。
- **(10) off-policy 序列掩码**：负优势且已显著偏离旧策略的序列，优势直接清零——不再惩罚「模型已经走远的旧轨迹」。
- **(11) 采样掩码贯穿训练**：rollout 时 top-p 把核外 token 概率清零了，但训练重算 logprob 用的是**完整 softmax**——核外 token 凭空分到了采样时不存在概率质量。该改进把采样时的核掩码记录下来，训练前向时把核外 token 的 logits 填 `-inf`，保证「训练分布在采样分布上」。
- **(12) 保留原版优势归一化**：与 olmo3 的改进 7 正好相反，V3.2 选择保留 `(r−μ)/(σ+ε)`。

一句话对比：olmo3 变体改的是「偏差」，V3.2 变体改的是「off-policy 安全性」。两者在优势归一化上**观点相反**（除 vs 不除 std），这本身就是当前社区未有定论的信号。

#### 4.2.2 核心流程

```text
rollout 阶段（sample_response）：
  每步采样后记录 keep_mask = (top-p 过滤后的 probas > 0)   # 改进 11
  返回值多出第四件套：keep_masks 列表

训练前向阶段（sequence_logprob_and_entropy）：
  先用 apply_keep_sampling_mask 把核外 token 的 logits 填 -inf  # 改进 11
  再 log_softmax + gather 取答案区 logprob（序列求和版）

优势阶段：
  advantages = (r − μ) / (σ + 1e-4)                        # 改进 12：保留除 std

损失阶段：
  ratio = exp(new − old)（序列级）
  seq_kl = (old − new) / gen_len                           # 改进 10：逐 token 平均
  优势掩码：adv<0 且 seq_kl>δ 的序列 → 优势置 0
  裁剪：obj = min(r·A, clamp(r, 1−ε, 1+ε)·A)
  KL（若开启）：kl = β · mean( exp(new−old) · (new − ref) )  # 改进 9：重加权
```

#### 4.2.3 源码精读

**改进 11（采样端）——记录核掩码：**

[deepseek_v32_style.py:L62-L82](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L62-L82)——采样流水线在 `top_p_filter`（L63，复用 ch04）之后，L72 取 `mask = probas > 0` 把「核内 token」标记为布尔掩码；随后重归一化、`multinomial` 抽 token，L82 把每个生成步的掩码 `keep_masks.append` 进列表。函数最终返回四件套（L96）：`full_token_ids, prompt_len, 解码文本, keep_masks`——比 u7-l2 的三件套多出掩码。

**改进 11（训练端）——把掩码盖回 logits：**

[deepseek_v32_style.py:L99-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L99-L113)——`apply_keep_sampling_mask`：定位答案区起点 `start = prompt_len - 1`（错位配对：第 `start+i` 行 logits 预测第 `i` 个生成 token，见 u6-l1），对每个生成步把 `~step_mask` 的位置 `masked_fill_(float("-inf"))`。`-inf` 过 `log_softmax` 后概率恰为 0——训练时「看不见」核外 token。

[deepseek_v32_style.py:L116-L120](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L116-L120)——`sequence_logprob_and_entropy` 在 `log_softmax` **之前**调用掩码函数，保证整个分布（包括熵计算）都基于截断后的支撑集。

**改进 12——保留原版优势归一化：**

[deepseek_v32_style.py:L223-L226](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L223-L226)——`advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-4)`，注释标明「Keep GRPO advantage normalization (DeepSeek V3.2)」。对照 4.1 的 L186：同一仓库、同一单元，两种流派并陈。

**改进 10——off-policy 序列掩码：**

[deepseek_v32_style.py:L256-L266](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L256-L266)——先算序列级 `ratio = exp(new − old)`（L257–L258）；L262 用 `(old_logps - new_logps) / gen_lens` 算**逐 token 平均**的对数概率漂移 `seq_kl`（除以生成长度正是 u8-l3 说的长度归一化缓解）；L265 把「负优势 **且** 漂移超过 `off_policy_delta`（默认 0.1）」的序列优势置 0。直觉：一条已经不被旧策略看好的轨迹，如果当前模型对它的概率又已大幅变化，再按旧优势去惩罚它就是在「追着旧账打」，不如放过。

**改进 8——数学域默认关 KL，且省掉 ref_model：**

[deepseek_v32_style.py:L603-L608](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L603-L608)——命令行 `--kl_coeff` 默认 `0.0`，help 文本直言「数学任务设 0 禁用」。

[deepseek_v32_style.py:L667-L675](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L667-L675)——只有 `kl_coeff` 非零时才 `deepcopy` 出 `ref_model`，否则 `ref_model = None`。对照 u8-l3 的「三份模型驻留」，这里默认只需两份（当前策略 + 每步冻结的 old_model），0.6B 模型省约 1.2 GB 显存。

**改进 9——重加权 KL：**

[deepseek_v32_style.py:L275-L283](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py#L275-L283)——KL 项从 u8-l3 的 `kl_coeff * mean(new − ref)` 变为 `kl_coeff * mean(exp(new − old) * (new − ref))`：重要性比 `exp(new_logps − old_logps)` 把「旧策略采的样本」重新加权到当前策略视角下，是对 off-policy KL 估计的标准修正。

#### 4.2.4 代码实践

**实践目标**：用一个极小例子验证改进 11 的掩码确实把「采样时不可能出现的 token」从训练分布中剔除。

**操作步骤**（示例代码，纯张量计算）：

```python
# 示例代码：验证采样掩码对 log_softmax 分布的影响
import torch

logits = torch.tensor([[2.0, 1.0, 0.5, -1.0]])   # 假设词表只有 4 个 token
probas = torch.softmax(logits, dim=-1)

# 模拟 top_p_filter：假设核只保留前 2 个 token
mask = torch.tensor([True, True, False, False])
filtered = probas * mask
filtered = filtered / filtered.sum()             # 重归一化，与 ch04 相同

# 训练时若不做掩码：完整 softmax 下 token 2/3 仍占有概率质量
print("无掩码 logprobs:", torch.log_softmax(logits, dim=-1))

# 训练时按改进 11 做掩码：核外 logits 填 -inf
masked_logits = logits.clone()
masked_logits[~mask] = float("-inf")
print("掩码后 logprobs:", torch.log_softmax(masked_logits, dim=-1))
```

**需要观察的现象**：无掩码版本中 token 2、3 的 logprob 是有限负数（概率非零）；掩码版本中它们是 `-inf`（概率恰为 0）。

**预期结果**：两条轨迹对同一个「真实采到的 token」（必在核内）的 logprob 也略有不同——因为分母（归一化常数）变了。这正是改进 11 的意义：训练优化的分布与实际采样的分布保持同一个支撑集。生成文本类的端到端验证需下载模型与训练，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：改进 10 的掩码为什么只处理 `adv < 0` 的序列，不管 `adv > 0` 的？

**答案**：off-policy 漂移的危害是不对称的。对负优势序列继续施压，是在「惩罚一条当前模型概率分布已经大幅改变的旧轨迹」——梯度信号已失真，且 V3.2 的立场是不追旧账；而正优势序列虽然同样 off-policy，但「奖励好轨迹」的更新方向与策略改进方向一致，风险低得多。这裁掉的是**有害的**那半边更新，而不是全部。

**练习 2**：改进 8「KL 关闭时不构建 ref_model」除了省显存，还有什么工程好处？

**答案**：`ref_model` 是训练全程冻结的初始模型副本，必须随主模型一起驻留设备、一起被 `deepcopy` 管理。按需构建把「功能开关」落实为「资源开关」：不开 KL 就完全不付这份内存与拷贝成本；同时也避免了对 `ref_model is None` 时误用 KL 的路径（L168–L169 会在 `kl_coeff` 非零且 `ref_model` 缺失时显式 `ValueError`）。

**练习 3**：`seq_kl = (old_logps − new_logps) / gen_lens` 中除以 `gen_lens` 解决了什么问题？

**答案**：u8-l3 分析过「求和式 KL 随答案长度增长，激励模型写长答案」的缺陷。除以生成长度把序列级的对数概率差折算成**每 token 平均**漂移，长短答案的 KL 度量回到同一尺度，`off_policy_delta=0.1` 这样的绝对阈值才有意义。

### 4.3 变体三：gdpo.py——「分奖励归一化」（改进 13）

#### 4.3.1 概念说明

GDPO（Liu et al., 2026）针对的是 u8-l4 引入的多奖励场景：当总奖励是「正确性 + λ×格式」的线性组合时，直接对**混合后的标量**做组内归一化有个隐患——哪个分量方差大、取值范围大，哪个分量就在优势里话语权大；调 λ 并不能真正控制两个信号的相对强度，因为归一化发生在混合**之后**。

GDPO 的主张：**先归一化，后聚合**。每个奖励分量先在自己的组内标准化（均值 0、方差 1），再加权求和，最后对和再做一次组内标准化以恢复统一尺度：

\[
a_c = \frac{r_c - \mu_c}{\sigma_c + \epsilon}, \qquad
a_f = \frac{r_f - \mu_f}{\sigma_f + \epsilon}, \qquad
s = a_c + \lambda \, a_f
\]

\[
A = \frac{s - \mu_s}{\sigma_s + \epsilon}
\]

其中 \(\mu_c, \sigma_c\) 是正确性奖励在本组 rollout 内的均值与标准差，\(\lambda\) 即代码里的 `format_coeff`（默认 0.1，沿用了 u8-l4「降权防奖励黑客」的结论）。这样 λ 的含义变成纯粹的「两个**已标准化**信号的混合比」，与各分量的原始量纲脱钩。

#### 4.3.2 核心流程

```text
每个 rollout：
  正确性奖励：可走 number_then_full 回退（宽松）
  格式奖励：必须 boxed 才得分（严格）
两个分量分别收集 → 各自组内标准化 → 加权求和 → 再整体标准化
其余（ratio、裁剪、KL）与 7_6 版相同
训练对象：reasoning 变体模型（沿用 u8-l4 的结论）
```

#### 4.3.3 源码精读

**奖励拆分——格式严、正确性宽：**

[gdpo.py:L125-L138](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py#L125-L138)——`reward_with_format_bonus` 返回三元组 `(总奖励, 格式分, 正确性分)`：格式分 `format_reward = 1.0 if boxed else 0.0`（用 `fallback=None` 强制要求 `\boxed{}`）；而正确性分用的 `candidate = boxed or extract_final_candidate(..., fallback="number_then_full")`——没写 boxed 时仍可从全文提数字来判对错。也就是说**两个分量的口径刻意不同**：格式分是「行为 shaping」，正确性分是「结果验证」，各管各的。

**GDPO 核心——三段式归一化：**

[gdpo.py:L210-L219](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py#L210-L219)——L211–L212 把两个分量分别转为张量；L215–L216 各自做 `(r − mean) / (std + 1e-4)`；L217 用 `adv_correct + format_coeff * adv_format` 聚合；L218 对聚合结果**再**做一次组内标准化。对照被注释掉的 L210（直接用混合奖励 `roll_rewards` 归一化的旧方案），改动一目了然。注意 `format_coeff` 直接参与两次运算：先作为聚合权重，最终尺度又被第二次归一化统一，λ 只影响相对混合比、不影响优势的绝对幅度。

**超参与训练对象：**

[gdpo.py:L587-L592](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py#L587-L592)——命令行 `--format_coeff` 默认 `0.1`，即 u8-l4 实测后推荐的低权重。

[gdpo.py:L634-L643](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/gdpo.py#L634-L643)——无论从零训练还是从 checkpoint 恢复，加载的都是 `which_model="reasoning"` 的变体与配套分词器，与 7_6 一致（base 模型不认识 `<think>` token，见 u8-l4）。

#### 4.3.4 代码实践

**实践目标**：手算同一组 rollout 在「简单混合后归一化」（7_6 风格）与「GDPO 分量归一化」两种算法下的优势，体会差异。

**操作步骤**（示例代码）：

```python
# 示例代码：对比简单混合与 GDPO 的优势计算
import torch

r_correct = torch.tensor([1., 1., 0., 0.])   # rollout 1-4 的正确性分
r_format  = torch.tensor([1., 1., 1., 0.])   # 格式分（rollout 3 答错但写了 boxed）
lam = 0.1

# 方案 A：先混合后归一化（7_6 风格）
total = r_correct + lam * r_format
adv_a = (total - total.mean()) / (total.std() + 1e-4)

# 方案 B：GDPO——先归一化后聚合再归一化
a_c = (r_correct - r_correct.mean()) / (r_correct.std() + 1e-4)
a_f = (r_format - r_format.mean()) / (r_format.std() + 1e-4)
s = a_c + lam * a_f
adv_b = (s - s.mean()) / (s.std() + 1e-4)

for i in range(4):
    print(f"rollout {i+1}: r_c={r_correct[i]:.0f} r_f={r_format[i]:.0f} "
          f"混合法={adv_a[i]:+.3f}  GDPO={adv_b[i]:+.3f}")
```

**需要观察的现象**（预期结果，可直接手算复核）：

| rollout | 正确性 | 格式 | 混合法优势 | GDPO 优势 |
| --- | --- | --- | --- | --- |
| 1 | 1 | 1 | +0.864 | +0.863 |
| 2 | 1 | 1 | +0.864 | +0.863 |
| 3（答错但合规） | 0 | 1 | −0.782 | −0.769 |
| 4（答错且不合规） | 0 | 0 | −0.946 | −0.958 |

两种算法的**排序完全一致**，差异体现在幅度：rollout 3 在两种算法下都因格式分少挨了一点惩罚，但程度不同。

**预期结果与解释**：在**二值奖励、两个分量方差相近**的例子里，两种算法天然接近——这正是要诚实指出的：GDPO 的收益在分量方差悬殊（例如一个 0/1 奖励配一个 0~10 连续奖励）或多分量场景下才显著；本仓库的正确性与格式奖励恰好都是二值，所以此例主要用于验证「机制理解」而非「效果差异」。端到端训练对比需 GPU 数小时至数天，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GDPO 聚合后还要再做一次整体归一化（L218）？

**答案**：分量归一化后 \(a_c, a_f\) 的方差各约为 1，加权和 \(s\) 的方差取决于两者的相关性，可能大于 1（正相关）也可能小于 1（负相关）。再除以 \(\sigma_s\) 把最终优势拉回单位方差，保证优势的绝对尺度与单奖励 GRPO 一致——学习率、梯度裁剪阈值等下游超参不需要因引入多奖励而重调。

**练习 2**：`reward_with_format_bonus` 里两个分量的提取口径为什么不同（L126–L131）？

**答案**：格式分用 `fallback=None`，没写 `\boxed{}` 就是 0——它是行为奖励，奖励的就是「写 boxed」这个动作本身；正确性分允许 `number_then_full` 回退，即使没写 boxed 也从全文提取数字判对错——它是结果奖励，关心「答案对不对」而非「格式好不好」。若两者口径相同（比如正确性也强制 boxed），「答对但没写 boxed」的 rollout 将拿不到任何正确性信号，与格式分无法区分。

**练习 3**：gdpo.py 建立在 7_6 而非 7_5 之上，为什么？

**答案**：GDPO 改的是**多奖励聚合**方式，先得有多奖励可聚合。7_5 只有单一正确性奖励，7_6（格式奖励版）才提供了「正确性 + 格式」两个分量；README 也明确「GDPO 是针对多奖励的微调」。这体现了脚本间的搭积木关系：变体不是并列的三选一，而是站在不同基础上的增量。

### 4.4 download_qwen3_grpo_checkpoints：checkpoint 家族与下载

#### 4.4.1 概念说明

训练一个 GRPO 变体动辄数小时到数天，多数读者需要的是**直接复用作者训练好的权重**来推理、评测或继续训练。为此作者把第 6、7 章各版本的关键步数 checkpoint 发布到 Hugging Face 仓库 `rasbt/qwen3-from-scratch-grpo-checkpoints`，并用 `download_qwen3_grpo_checkpoints` 做统一入口。

设计要点（承接 u1-l3 的门面模式）：

- **两级寻址**：`grpo_type`（五个家族）× `step`（训练步数）唯一定位一个文件；
- **文件名协议**：`qwen3-0.6B-rlvr-grpo-step{step:05d}.pth`，与三个变体脚本里 `save_checkpoint` 的命名（`qwen3-0.6B-rlvr-grpo-step{step:05d}{suffix}.pth`）完全一致——训练端存什么名，下载端就拼什么名；
- **本地校验**：非法 `grpo_type` 在本地直接 `ValueError`，不会发出一个 404 的网络请求；
- **重要限制**：mapper 里**没有** 7_7 变体的家族——三个变体（olmo3/deepseek_v32/gdpo）的 checkpoint 未发布，想用须自己训练。

#### 4.4.2 核心流程

```text
download_qwen3_grpo_checkpoints(grpo_type, step, out_dir)
  1. grpo_type 在 mapper 五个键中查表 → 找不到则 ValueError
  2. step 转字符串并 zfill(5)（"50" → "00050"）
  3. 拼 URL：https://huggingface.co/rasbt/qwen3-from-scratch-grpo-checkpoints/resolve/main/{家族目录}/{文件名}
  4. （仅 no_kl + 00050）附 Backblaze B2 备用 URL
  5. 交给 download_file：流式下载、进度显示、大小校验缓存
  6. 返回本地文件路径
```

#### 4.4.3 源码精读

[reasoning_from_scratch/qwen3.py:L460-L473](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L460-L473)——mapper 把五个短名映射到 HF 仓库里的目录：`no_kl` 对应第 6 章基线，`tracking` / `clip_ratio` / `kl` / `format_reward` 分别对应 7.3–7.6 节；不在键里就地 `ValueError`。**注意没有任何 7_7 变体的条目**——这是评估其效果时必须知道的限制。

[reasoning_from_scratch/qwen3.py:L475-L480](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L475-L480)——`step.isdigit()` 时补零到 5 位（与训练端 `f"{step:05d}"` 对齐），拼出主 URL。所以 `step=50`、`"50"`、`"00050"` 三种写法等价。

[reasoning_from_scratch/qwen3.py:L482-L493](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L482-L493)——备用 URL 仅对 `no_kl + 00050` 一个组合提供，其余家族只有 HF 单通道；最后把主备 URL 交给 u1-l3 讲过的 `download_file` 并返回落盘路径。

各家族可用的步数清单见 [ch07/04_download_trainining_checkpoints/README.md:L143-L159](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/README.md#L143-L159)：`no_kl` 有 00050–09000 共 8 档，四个 7.x 家族各有 00050–00500 每 50 步一档共 10 档。

#### 4.4.4 代码实践

**实践目标**：验证本地校验与补零逻辑，再实际下载一个 checkpoint。

**操作步骤**（示例代码）：

```python
# 示例代码
from reasoning_from_scratch.qwen3 import download_qwen3_grpo_checkpoints

# 1) 故意传非法家族，观察本地 ValueError（不产生网络请求）
try:
    download_qwen3_grpo_checkpoints(grpo_type="gdpo")
except ValueError as e:
    print("按预期报错:", e)   # 报错信息会列出五个合法键

# 2) 下载第 6 章基线 step 50（约 1.5 GB，含主备双通道）
path = download_qwen3_grpo_checkpoints(grpo_type="no_kl", step="00050", out_dir="qwen3")
print(path)   # qwen3/qwen3-0.6B-rlvr-grpo-step00050.pth

# 3) 再次调用同一参数，观察缓存命中（'already up-to-date'）
path = download_qwen3_grpo_checkpoints(grpo_type="no_kl", step="00050", out_dir="qwen3")
```

**需要观察的现象**：第 1 步立刻抛 `ValueError` 且信息中列出合法键；第 2 步终端显示分块下载进度；第 3 步显示缓存跳过。

**预期结果**：非法参数被本地拦截；合法参数返回本地路径。下载体积较大（约 1.5 GB/个），网络与磁盘占用**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `no_kl + 00050` 有备用 URL，其他组合没有？

**答案**：备用 URL 的维护成本在高（Backblaze B2 上要为每个文件留一份镜像）。`no_kl + 00050` 是第 6 章教学的最小验证点、被最多读者使用，作者为它单独上传了镜像；其余家族与步数只走 Hugging Face 单通道。这是「按使用频率决定冗余度」的工程取舍。

**练习 2**：`step` 参数为什么做 `zfill(5)` 而不是要求调用者传规范格式？

**答案**：训练端 `save_checkpoint` 用 `f"step{step:05d}"` 生成文件名（5 位零填充），下载端必须逐字符对齐才能拼出正确 URL。做宽容处理（接受 `50` / `"50"` / `"00050"`）把协议细节封装在函数内，调用者不容易踩坑；这也与 u1-l3 总结的「上层只经门面取权重」原则一致。

**练习 3**：三个 7_7 变体脚本自己训练出的 checkpoint 能否用这个函数下载？

**答案**：不能。mapper 只收录五个已发布家族，7_7 变体的权重未上传 HF 仓库。自己训练的 checkpoint 会存在脚本同目录的 `checkpoints/<脚本名>/` 下，命名与协议一致（如 `qwen3-0.6B-rlvr-grpo-step00050.pth`），可直接用 `torch.load` 本地加载——4.5 节的加载流程同样适用。

### 4.5 usage_example.py：加载 checkpoint 并流式生成

#### 4.5.1 概念说明

`ch07/04_download_trainining_checkpoints/usage_example.py` 是「checkpoint 变成对话模型」的完整示范，五步流水线：

1. **下载 checkpoint**：按 `--grpo_type` 与 `--step` 取权重文件；
2. **按家族选分词器**：`format_reward` 家族训练自 reasoning 变体 → 用 reasoning 分词器 + 聊天模板 + thinking；其余家族训练自 base 模型 → 用 base 分词器裸文本编码；
3. **重建模型灌权重**：`Qwen3Model(QWEN_CONFIG_06_B)` 在本地搭好 28 层架构，`torch.load` + `load_state_dict` 把权重灌入；
4. **渲染提示**：`render_prompt` 套上第 3 章的 `\boxed{}` 模板，`tokenizer.encode` 转 token id；
5. **流式生成**：`generate_text_basic_stream_cache`（u3-l2 的贪心 + KV 缓存版）逐 token 吐出，`eos_token_id` 控制停止。

分词器选择规则是最容易踩的坑，[ch07/04_download_trainining_checkpoints/README.md:L41-L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/README.md#L41-L53) 明确列出：`7_3`/`7_4`/`7_5` 用 base 分词器，`7_6` 用 reasoning 分词器——**checkpoint 的权重决定它期待什么样的输入格式**，用错分词器等于让模型读一门没学过的语言。

#### 4.5.2 核心流程

```text
main():
  解析参数（grpo_type ∈ 5 家族, step, prompt, max_new_tokens, local_dir, device）
  checkpoint_path = download_qwen3_grpo_checkpoints(...)
  tokenizer = build_tokenizer(grpo_type, local_dir)   # 家族 → 分词器分支
  model = Qwen3Model(QWEN_CONFIG_06_B)
  model.load_state_dict(torch.load(checkpoint_path, map_location=device))
  model.to(device); model.eval()
  prompt = render_prompt(args.prompt); input_ids = encode(prompt)
  for token in generate_text_basic_stream_cache(...):  # 贪心 + KV 缓存
      print(decode(token), end="")
```

#### 4.5.3 源码精读

**分词器分支：**

[usage_example.py:L24-L35](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/usage_example.py#L24-L35)——`build_tokenizer`：`grpo_type == "format_reward"` 时走 reasoning 分支（`tokenizer-reasoning.json` + `apply_chat_template=True` + `add_generation_prompt=True` + `add_thinking=True`，即 u2-l6 讲过的「生成提示 + 模型自写 think」形态）；否则走 base 分支（`tokenizer-base.json` 裸文本）。两个分支都先经 `download_qwen3_small(tokenizer_only=True)` 拿分词器文件（u1-l3）。

**参数与命令行接口：**

[usage_example.py:L43-L55](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/usage_example.py#L43-L55)——`--grpo_type` 的 `choices` 恰好是 mapper 的五个键（默认 `clip_ratio`），`--step` 默认 `00050`；非法家族在 argparse 层就被拦下。

**下载 + 建模 + 灌权重的标准次序：**

[usage_example.py:L82-L94](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/usage_example.py#L82-L94)——先下载再选分词器再建模：`torch.load(checkpoint_path, map_location=device)` 直接把张量载到目标设备，`load_state_dict` 逐参数名校验灌入（键名不匹配会报错——这正是 checkpoint 与本地代码版本必须配套的原因），最后 `.to(device)` + `.eval()`（关闭 dropout 等训练期行为）。

**渲染与流式生成：**

[usage_example.py:L96-L115](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/04_download_trainining_checkpoints/usage_example.py#L96-L115)——`render_prompt` 保证推理时的提示格式与训练时完全一致（`\boxed{}` 契约）；生成用 u3-l2 的 `generate_text_basic_stream_cache`，逐 token `decode` 并 `flush` 实时打印。注意这是**贪心解码**：同一 checkpoint 上同一提示的输出完全确定，方便复现与对比。

#### 4.5.4 代码实践

**实践目标**：把作者训练的 no-KL 基线第 50 步 checkpoint 变成一个能解题的模型。

**操作步骤**：

```bash
# 1) 直接跑官方示例（默认 clip_ratio 家族，可先跳过）
uv run ch07/04_download_trainining_checkpoints/usage_example.py

# 2) 换成本讲要求的 no_kl 基线 + step 50（约 1.5 GB 下载）
uv run ch07/04_download_trainining_checkpoints/usage_example.py \
    --grpo_type no_kl --step 00050 --max_new_tokens 256
```

或等价的 API 写法（示例代码）：

```python
# 示例代码：usage_example.py 的核心流程改写
import torch
from reasoning_from_scratch.ch02 import generate_text_basic_stream_cache, get_device
from reasoning_from_scratch.ch03 import render_prompt
from reasoning_from_scratch.qwen3 import (
    Qwen3Model, Qwen3Tokenizer, QWEN_CONFIG_06_B,
    download_qwen3_grpo_checkpoints, download_qwen3_small,
)

device = get_device()
ckpt = download_qwen3_grpo_checkpoints(grpo_type="no_kl", step="00050", out_dir="qwen3")
download_qwen3_small(kind="base", tokenizer_only=True, out_dir="qwen3")

tokenizer = Qwen3Tokenizer(tokenizer_file_path="qwen3/tokenizer-base.json")
model = Qwen3Model(QWEN_CONFIG_06_B)
model.load_state_dict(torch.load(ckpt, map_location=device))
model.to(device).eval()

ids = torch.tensor(
    tokenizer.encode(render_prompt("Solve: If x + 7 = 19, what is x?")),
    device=device,
).unsqueeze(0)

for token in generate_text_basic_stream_cache(
    model=model, token_ids=ids, max_new_tokens=256,
    eos_token_id=tokenizer.eos_token_id,
):
    print(tokenizer.decode([token.squeeze(0).item()]), end="", flush=True)
```

**需要观察的现象**：终端先打印环境信息横幅（torch 版本、设备、家族、步数），随后模型流式输出解题过程，最终答案应出现在 `\boxed{}` 中（x = 12）。

**预期结果**：第 50 步的 no-KL checkpoint 在 MATH-500 上的评测准确率是 46.2%（见下节日志数据），解这类一元一次方程应当稳定正确。CPU 亦可运行但每 token 较慢；具体生成文本**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `--grpo_type format_reward` 传给 usage_example.py，但手动改成 base 分词器，会发生什么？

**答案**：`format_reward` 家族的权重来自 reasoning 变体，训练时输入都带 `<|im_start|>user\n…<|im_end|>` 聊天包装与 `<think>` 起始语境。改用 base 分词器裸文本编码后，模型收到的 token 序列与训练分布严重错位，输出大概率退化为乱码或提前 EOS。这正是 `build_tokenizer` 按家族分支的原因。

**练习 2**：为什么加载用 `map_location=device` 而不是先载 CPU 再 `.to(device)`？

**答案**：`map_location` 让 `torch.load` 直接把张量落到目标设备，省去「CPU 占一份完整副本再搬运」的峰值内存与时间。对 0.6B 模型差异尚可感知，对更大的模型（附录 D 的 1.7B–32B）这几乎是从「能跑」到「跑不动」的区别。两种写法结果等价，工程上首选前者。

**练习 3**：这个示例能用 `torch.compile` 进一步加速吗？

**答案**：能。按 u3-l3 的次序「加载权重 → 搬设备 → 编译」即可，例如 `model = torch.compile(model)` 放在 `.eval()` 之后。示例脚本为普适性默认不编译；带 KV 缓存的 decode 阶段形状稳定，是编译友好的场景（u3-l3 的结论）。

## 5. 综合实践

本讲综合实践分三部分：**读脚本做对照表 → 下载加载 checkpoint → 用真实日志评估收益**。

### 5.1 变体-改动点-动机对照表

通读三个脚本（重点看各自的 `compute_grpo_loss_plus_kl` 与训练循环），制作一张每格不超过 15 字的对照表。参考答案（读者应自己先做，再核对）：

| 变体 | 改动点 | 动机 |
| --- | --- | --- |
| olmo3_style | 优势不除以 std | 消隐式长度偏差 |
| olmo3_style | 零信号就换题重采样 | 算力花在有信号题上 |
| olmo3_style | token 级损失均值 | 长答案不抢梯度 |
| olmo3_style | 裁剪下紧上松 0.2/10 | 护住探索防熵坍缩 |
| olmo3_style | ratio 先截上界 | 防 token 概率比爆炸 |
| olmo3_style | 默认 kl_coeff=0 | 数学域无需 KL |
| deepseek_v32 | 关 KL 则不建 ref_model | 省一份模型显存 |
| deepseek_v32 | KL 乘重要性比 exp(new−old) | 修 off-policy 偏差 |
| deepseek_v32 | 负优势且漂移大者清零 | 不追着旧账打 |
| deepseek_v32 | 采样核掩码盖回训练 | 训练=采样支撑集 |
| deepseek_v32 | 保留除 std 归一化 | 与 Dr. GRPO 相反 |
| gdpo | 分量先归一化再聚合 | λ 只管混合比 |
| gdpo | 正确性与格式口径分离 | 结果奖励行为奖励各司其职 |
| gdpo | 训练 reasoning 变体 | base 不识 think 标签 |

### 5.2 下载并运行 checkpoint

按 4.5.4 的命令下载 `no_kl + 00050` 并生成文本；再换成 `clip_ratio + 00500`（同一道题）对比两个家族的输出风格（响应长度、是否分步）。

### 5.3 用真实日志评估收益与局限

仓库公开的训练日志是评估「这些改进值不值」的唯一客观依据。用 [ch07/02_logs/ch06_rlvr_grpo_original_no_kl_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/ch06_rlvr_grpo_original_no_kl_metrics.csv) 与 [ch07/02_logs/7_4_plus_clip_ratio_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv) 的 `eval_acc` 列（每 50 步在 MATH-500 上的准确率）填表：

| 家族 | step 50 | step 200 | step 350 | step 500 | 走势 |
| --- | --- | --- | --- | --- | --- |
| no_kl（第 6 章基线） | 46.2 | 44.2 | 38.0 | **33.0** | 前高后衰 |
| clip_ratio（7.4） | 45.2 | 42.4 | 41.0 | **41.6** | 全程平稳 |

**评估结论**：

- **峰值相近**（46.2 vs 45.6），**终点差距显著**（33.0 vs 41.6）：裁剪版终点高出 8.6 个百分点，相对提升约 26%——学习目标里说的「约 20% 提升」量级成立，但要注意它主要来自「基线自己衰退」而非「裁剪版冲得更高」，比较点不同数字也不同；
- 裁剪的真正收益是**稳定性**（notebook 原话：不再有 step 400 附近的下跌），这与 u8-l2 的信任区间分析一致；
- **局限一**：7_5（KL 版）日志显示约 60 步后崩溃、7_6（格式奖励版）先升后回落（u8-l3、u8-l4 已详析），改进并非多多益善；
- **局限二**：**三个 7_7 变体没有任何公开日志**（`ch07/02_logs/` 只有 7_3–7_6 与 ch06 五个文件），HF checkpoint 仓库也未收录它们的家族——其效果只能自行训练验证，或等待作者补充（README 末尾作者自注「计划日后写更详细的说明」）。

## 6. 本讲小结

- 社区对 GRPO 的 15 条改进被编排进三个脚本：**olmo3_style**（1–7，消偏差省算力）、**deepseek_v32_style**（8–12，off-policy 纪律）、**gdpo**（13，分奖励归一化）；代码注释里的 `# (n)` 与 README 清单一一对应。
- olmo3 与 V3.2 在「优势是否除以 std」上**观点相反**（L186 vs L225），同一仓库并陈两种流派，说明该问题尚无定论。
- V3.2 的采样掩码（改进 11）把 top-p 截断从采样端**贯穿**到训练端：`mask = probas > 0` 记录核，`apply_keep_sampling_mask` 用 `-inf` 填核外 logits，保证训练分布与采样分布同支撑集。
- GDPO 的公式是「分量各自组内标准化 → 加权求和 → 再整体标准化」，让 λ 只表达已标准化信号间的混合比；在二值奖励场景下与简单混合法差异有限。
- `download_qwen3_grpo_checkpoints` 以「家族 × 步数」两级寻址拼 URL，文件名协议与训练端 `save_state` 命名完全对齐；**三个 7_7 变体不在已发布家族之列**。
- `usage_example.py` 五步流水线（下载 → 选分词器 → 重建模型灌权重 → 渲染 → 流式贪心生成）把任何 GRPO checkpoint 变成可对话模型；**分词器必须跟着 checkpoint 的训练来源走**（format_reward → reasoning，其余 → base）。
- 真实日志评估：裁剪版与基线峰值相当（≈46%），但终点 41.6% vs 33.0%（相对 +26%），收益在稳定性；改进不叠加——KL 版崩溃、格式版回落均有日志为证。

## 7. 下一步学习建议

本讲收束了第 6–7 章的强化学习主线（单元七、八全部完成）。接下来有两条路：

- **推荐主线——进入单元九（第 8 章知识蒸馏）**：从 [u9-l1 蒸馏数据生成](u9-l1-distillation-data-generation.md) 开始。GRPO 训出的模型上限受基座能力约束，蒸馏则直接把更强教师（DeepSeek-R1、Qwen3-235B）的推理模式写进 0.6B 学生模型；其中 [u9-l5 与 Hugging Face 生态互通](u9-l5-huggingface-interop.md) 会把本讲的「加载 state_dict」扩展为「与 transformers 生态双向转换」，与本讲 4.5 节衔接紧密。
- **延伸阅读源码**：若想继续深挖 GRPO 变体，可读 README 清单中尚未实现的改进 14（GSPO 序列级重要性采样）与 15（CISPO 裁剪重要性权重），并思考如何在 `deepseek_v32_style.py` 基础上各加十几行实现它们——这是检验本讲学习效果的试金石。也可以回头跑 [ch07/02_logs/](https://github.com/rasbt/reasoning-from-scratch/tree/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs) 中 7_5、7_6 的 CSV，用 u8-l1 的 `plot_grpo_metrics` 复现本讲 5.3 的表格。
