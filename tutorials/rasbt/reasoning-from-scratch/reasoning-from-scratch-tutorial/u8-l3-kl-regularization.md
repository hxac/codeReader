# GRPO 改进二：KL 散度正则

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出逐 token KL 散度的数学形式，并解释代码中 `kl_loss = kl_coeff * torch.mean(new_logps - ref_logps)` 为什么是它的序列级蒙特卡洛估计。
2. 读懂 `7_5_plus_kl.py` 相对 `7_4_plus_clip_ratio.py` 的全部新增代码：参考模型（ref_model）的创建与冻结、rollout 阶段的 `ref_logp` 收集、损失组装与 `kl_coeff=0` 的退火路径。
3. 解释 KL 系数（代码中的 `kl_coeff`，论文里常记作 \(\beta\)）在「锚定基座能力」与「允许探索」之间的权衡。
4. 从作者发布的两份真实训练日志（7_5 与 7_4）中读出一个反直觉的事实：在 0.6B 数学训练这个特定设定下，β=0.001 的 KL 项不仅没能稳住训练，反而参与了把模型推向近均匀分布的崩溃过程，并理解背后的两条机理。

## 2. 前置知识

- **KL 散度（Kullback–Leibler divergence）**：衡量同一个样本空间上两个概率分布的差异。对离散分布 \(p\) 与 \(q\)：

  \[ D_{\mathrm{KL}}(p \,\|\, q) = \sum_{v} p(v) \log\frac{p(v)}{q(v)} \ge 0 \]

  它度量「用 \(q\) 去编码按 \(p\) 采样的事件，平均要多付出的编码长度」。它非负、非对称，且当且仅当 \(p=q\) 时为 0。
- **参考策略（reference policy）**：RLHF/GRPO 中的常见角色——一份被冻结的「训练前原始模型」。KL 正则惩罚当前策略偏离它的程度，防止模型为了刷奖励而丢掉基座原有的语言能力。
- **正则化（regularization）**：在损失里额外加一项「惩罚项」，约束优化不要走向极端。这里惩罚的是策略漂移。
- **承接 u7-l3 / u8-l1**：`sequence_logprob` 对答案区逐 token 对数概率**求和**，得到序列级 logprob（不是平均）；熵 \(H=-\sum_v p(v)\log p(v)\) 在 Qwen3 全词表 151,936 下的上限是 \(\ln(151936)\approx 11.93\)，熵逼近这个值意味着分布接近均匀、模型接近「随机吐 token」。
- **承接 u8-l2**：clip ratio 约束的是**单步更新幅度**（新旧策略概率比的信任区间）；本讲的 KL 项约束的是**长期累积漂移**（相对训练起点的总偏移）。二者互补但角色不同。
- **蒙特卡洛估计**：期望 \(\mathbb{E}_{a\sim p}[f(a)]\) 可以用按 \(p\) 采到的样本的 \(f(a)\) 的平均值来近似；样本有限时估计值可能为负，即使期望本身非负。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py) | 本讲主角：在 7_4 的裁剪版 GRPO 上加入 KL 损失项的完整训练脚本 |
| [ch07/02_logs/7_5_plus_kl_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv) | 作者发布的 7_5 版 500 步真实训练日志（含 `kl_loss` 列） |
| [ch07/02_logs/7_4_plus_clip_ratio_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv) | 上一讲无 KL 版的 500 步日志，用作对照组 |
| [ch07/01_main-chapter-code/ch07_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb) | 第 7 章主 notebook，7.5 节给出 KL 项的图解与对崩溃现象的官方解释 |

阅读提示：两份 CSV 均为「第 1 行表头 + 第 2 行起每步一行」，即第 \(N\) 步对应文件第 \(N+1\) 行。

## 4. 核心概念与源码讲解

### 4.1 KL 散度正则：概念与数学形式

#### 4.1.1 概念说明

强化学习训练有一个经典风险：策略为了最大化奖励可以走向任何方向，包括「语无伦感但碰巧得分」的方向。KL 正则的思路是给损失加一个软约束——

\[ \mathcal{L} = \mathcal{L}_{\text{PG}} + \beta \cdot \widehat{D}_{\mathrm{KL}}(\pi_{\theta} \,\|\, \pi_{\text{ref}}) \]

其中 \(\pi_{\text{ref}}\) 是训练开始前冻结的基座模型，\(\beta\)（代码中叫 `kl_coeff`）控制惩罚强度。直觉上：奖励项告诉模型「往哪走」，KL 项告诉模型「别离家太远」。这正是 DeepSeek 原始 GRPO 论文损失中的 β 项，本书第 6 章为控制篇幅跳过了它，第 7.5 节把它补齐。

#### 4.1.2 核心流程

逐 token 形式。设生成到第 \(t\) 步时，当前策略与参考策略在词表上的分布分别为 \(\pi_\theta(\cdot|s_t)\) 与 \(\pi_{\text{ref}}(\cdot|s_t)\)，则该步的 KL 为：

\[ D_{\mathrm{KL},t} = \sum_{v} \pi_\theta(v|s_t) \log\frac{\pi_\theta(v|s_t)}{\pi_{\text{ref}}(v|s_t)} \]

直接算它需要对整个词表求和（151,936 项），可行但昂贵；GRPO 采用的是**采样型估计**。注意到恒等式：

\[ \mathbb{E}_{a_t \sim \pi_\theta}\big[\log \pi_\theta(a_t|s_t) - \log \pi_{\text{ref}}(a_t|s_t)\big] = D_{\mathrm{KL},t} \]

即「对实际采到的 token 计算`当前策略对数概率 − 参考策略对数概率`，再取期望」就等于 KL。把一条 rollout 答案区的所有步加总、再对多条 rollout 取平均，就得到序列级估计器：

\[ \widehat{D}_{\mathrm{KL}} = \frac{1}{N}\sum_{i=1}^{N}\sum_{t \in \text{answer}_i}\Big[\log\pi_\theta(a_{i,t}) - \log\pi_{\text{ref}}(a_{i,t})\Big] \]

三个值得记住的性质：

1. **无偏但可为负**：有限样本下单个估计可以小于 0（只要当前策略碰巧给采样 token 的概率低于参考策略），期望才是非负的 KL。
2. **随长度增长**：内层是对 token 的**求和**，答案越长估计越大——这是后文崩溃机理之一的根源。
3. **梯度只走当前策略**：\(\log\pi_{\text{ref}}\) 一项与参数 \(\theta\) 无关，反向传播时被 `detach` 掉。

#### 4.1.3 源码精读

KL 项的核心实现只有一行，位于 [7_5_plus_kl.py:L224-L229](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L224-L229)：

```python
if kl_coeff:
    ref_logps = torch.stack(roll_ref_logps).detach()
    kl_loss = kl_coeff * torch.mean(new_logps - ref_logps)
else:
    kl_loss = torch.tensor(0.0, device=new_logps.device)
loss = pg_loss + kl_loss
```

这段代码做的事：把 rollout 阶段收集的各条参考对数概率堆叠并 `detach`（参考模型不训练）；按上节公式计算 `kl_coeff × mean(new − ref)`；`kl_coeff=0` 时直接退化为 0 张量（功能开关）；最后与 u8-l2 的裁剪版策略梯度损失相加。`new_logps` 来自 [7_5_plus_kl.py:L208-L212](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L208-L212)，对当前模型带梯度重算，是公式中 \(\log\pi_\theta\) 的载体；而「求和而非平均」的答案区切片逻辑在 [7_5_plus_kl.py:L89-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L89-L91)（`selected[prompt_len - 1:]` 后 `torch.sum`），决定了估计器随序列长度线性增长。

主 notebook 对该公式的图解与说明见 [ch07_main.ipynb:L1546](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L1546)：对每条 rollout 用**同一份生成 token** 分别在当前策略与参考模型下计算对数概率，逐项求和度量策略偏移。

#### 4.1.4 代码实践：手算 KL 并与代码公式对齐

**实践目标**：用一个两词表的小例子，验证「逐项求和公式」与「采样估计公式」在期望意义下相等。

**操作步骤**（示例代码，纯 CPU 张量运算，不需要模型与 GPU）：

1. 手算 \(p=[0.7, 0.3]\)、\(q=[0.5, 0.5]\) 的 KL：

   \[ D_{\mathrm{KL}}(p\,\|\,q) = 0.7\ln\frac{0.7}{0.5} + 0.3\ln\frac{0.3}{0.5} = 0.7\times 0.33647 + 0.3\times(-0.51083) \approx 0.0823 \text{ nat} \]

2. 用 `torch` 的逐项求和公式复算；
3. 模拟代码的采样估计：按 \(p\) 采样单个 token，计算 \(\log p(a) - \log q(a)\)，重复多次取平均，观察收敛到 0.0823。

```python
import torch

p = torch.tensor([0.7, 0.3])
q = torch.tensor([0.5, 0.5])

kl_exact = (p * (p.log() - q.log())).sum()
print(f"KL(逐项求和公式) = {kl_exact:.4f}")   # 预期 0.0823

torch.manual_seed(42)
n = 100_000
samples = torch.multinomial(p, num_samples=n, replacement=True)
estimates = p.log()[samples] - q.log()[samples]   # 每个样本: log p(a) - log q(a)
print(f"KL(采样估计, n={n}) = {estimates.mean():.4f}")
print(f"单样本取值: token0 -> {(p.log()-q.log())[0]:.4f}, "
      f"token1 -> {(p.log()-q.log())[1]:.4f}")
```

**需要观察的现象**：逐项求和公式输出 0.0823；采样估计随 \(n\) 增大收敛到同一数值；单个样本的取值只有两种——\(\ln(1.4)\approx 0.3365\) 或 \(\ln(0.6)\approx -0.5108\)，后者为负。

**预期结果**：`kl_exact ≈ 0.0823`；`estimates.mean()` 随种子与样本数在 0.08 附近波动（n=100,000 时通常精确到小数点后两到三位）。采样法在 n 很小时可能给出负值，这正是 7_5 日志第 1 步 `kl_loss=-0.0967`、第 57 步 `kl_loss=-0.690` 为负的原因——样本级估计可为负，期望才非负。蒙特卡洛部分的具体数值依赖随机种子，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `kl_loss` 公式里的 `torch.mean` 换成对「逐 token 平均后的 logprob」之差，估计器的哪个性质会改变？哪类作弊行为会被消除？

**答案**：换成长度归一化（token 级）后，估计不再随答案长度线性增长，「把答案写长来稀释/放大 KL 项」的激励消失。这正是 notebook 建议的缓解方案之一（见 4.3 节），也是 DAPO/Dr. GRPO 采用 token 级损失的部分动机。

**练习 2**：`D_KL(p‖q)` 与 `D_KL(q‖p)` 相等吗？代码估计的是哪个方向？

**答案**：不相等，KL 非对称。代码计算 \(\mathbb{E}_{a\sim\pi_\theta}[\log\pi_\theta(a)-\log\pi_{\text{ref}}(a)]\)，样本来自**当前策略**，因此是 \(D_{\mathrm{KL}}(\pi_\theta \,\|\, \pi_{\text{ref}})\)——「从当前策略出发看它偏离参考多远」，梯度把当前策略拉回参考。

**练习 3**：为什么 `ref_logps` 要 `detach()`，而 `new_logps` 不用？

**答案**：参考模型参数已冻结、`requires_grad=False`，且在 `torch.no_grad()` 下计算，本来就无梯度；`detach()` 是把「不训练参考模型」的语义显式写进损失组装处。`new_logps` 是唯一携带 \(\theta\) 梯度的项，KL 项正是靠它把「拉回参考」的梯度传回当前模型。

### 4.2 compute_grpo_loss_plus_kl：参考模型与损失组装

#### 4.2.1 概念说明

`compute_grpo_loss_plus_kl` 是 7_4 版 `compute_grpo_loss` 的超集：在「旧模型采样 + 裁剪比率」的骨架上多了一个常驻角色——参考模型。现在训练时内存里同时存在**三份模型**：

| 角色 | 生命周期 | 是否更新 | 用途 |
|---|---|---|---|
| `model`（当前策略） | 全程 | ✅ 唯一被优化 | 计算 `new_logps`，产生梯度 |
| `old_model` | 每个外层 step 开头 `deepcopy` 一次 | ❌ 冻结 | 采样 rollout、计算 `old_logps`（供比率用） |
| `ref_model` | 训练前创建一次，全程不变 | ❌ 冻结 | 计算 `ref_logps`（供 KL 项用） |

注意 `old_model` 与 `ref_model` 的区别：前者是「上一刻的我」，用于限制单步更新幅度（u8-l2）；后者是「出发时的我」，用于限制累计漂移（本讲）。据主 notebook [ch07_main.ipynb:L1546](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L1546)，DeepSeek-R1 的参考模型每 400 步会同步为最新模型，本脚本为简化全程不更新。代价是显存：多一份 0.6B 模型常驻。

#### 4.2.2 核心流程

`compute_grpo_loss_plus_kl` 的执行过程（五阶段，KL 相关步骤加粗）：

1. **前置校验**：`kl_coeff` 非零却没给 `ref_model` → 立刻 `ValueError`；`old_model` 缺省时退化为当前模型自身。
2. **rollout 采样**：对同一 prompt 用 `old_model` 采样 `num_rollouts` 条回答（温度 0.8 + top-p 0.9）。
3. **统计收集（无梯度）**：对每条 rollout 计算 `old_logp` 与熵；**若 `kl_coeff` 非零，再用 `ref_model` 对同一份 token 计算 `ref_logp`**；随后用 `reward_rlvr` 判分。
4. **优势与零优势短路**：组内标准化 advantage；`skip_zero_adv` 且全零时直接返回（`kl_loss=0.0`）。
5. **损失计算（有梯度）**：当前模型重算 `new_logps` → 概率比与裁剪得 `pg_loss` → **`kl_loss = kl_coeff·mean(new − ref)`** → `loss = pg_loss + kl_loss`。

#### 4.2.3 源码精读

**入口校验**，[7_5_plus_kl.py:L119-L137](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L119-L137)：函数签名新增 `ref_model` 与 `kl_coeff=0.001` 两个参数；`kl_coeff` 非零而 `ref_model is None` 时就地报错，把配置错误拦在训练开始之前。

**rollout 循环中的参考统计**，[7_5_plus_kl.py:L146-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L146-L163)：

```python
token_ids, prompt_len, text = sample_response(model=old_model, ...)
with torch.no_grad():
    old_logp, entropy = sequence_logprob_and_entropy(
        old_model, token_ids, prompt_len, return_entropy=True
    )
    if kl_coeff:
        ref_logp = sequence_logprob_and_entropy(ref_model, token_ids, prompt_len)
    else:
        ref_logp = None
```

采样自 `old_model`；旧策略与参考策略的统计都在 `no_grad` 下计算，且**复用同一份 `token_ids`**——KL 比较的是两个模型对同一条序列的置信度之差。收集侧 `if kl_coeff: roll_ref_logps.append(ref_logp)` 位于 [L166-L172](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L166-L172)。

**损失组装**已在 4.1.3 精读（[L224-L229](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L224-L229)）；返回字典新增 `kl_loss` 字段（[L231-L242](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L231-L242)），零优势短路分支同样带 `kl_loss: 0.0`（[L188-L206](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L188-L206)）。

**参考模型的创建**在脚本入口，[7_5_plus_kl.py:L597-L603](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L597-L603)：`copy.deepcopy(model)` 后 `eval()` 并逐参数关闭 `requires_grad`；`--kl_coeff 0` 时干脆不创建，省下一份显存。命令行默认值 `0.001` 定义在 [L539-L544](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L539-L544)，训练函数签名同样默认 `kl_coeff=0.001`（[L322-L343](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L322-L343)）。对比之下，`old_model` 是每个外层 step 开头才 `deepcopy` 一次（[L356-L359](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L356-L359)）。

#### 4.2.4 代码实践：用张量复算损失组装与梯度流向

**实践目标**：脱离模型，只用张量验证 4.2.3 中损失组装的三个行为：公式正确、梯度只流经 `new_logps`、`kl_coeff=0` 正确退火。

**操作步骤**（示例代码，纯 CPU）：

```python
import torch

kl_coeff = 0.001
new_logps = torch.tensor([-8.0, -20.2, -16.6, -23.4], requires_grad=True)
ref_logps = torch.tensor([-7.9, -19.9, -17.0, -22.9])          # 假设已 detach
adv = torch.tensor([0.866, 0.866, -0.866, -0.866])

pg_loss = -(adv * new_logps).mean()
kl_loss = kl_coeff * torch.mean(new_logps - ref_logps)
loss = pg_loss + kl_loss
loss.backward()

print(f"pg_loss={pg_loss:.4f}, kl_loss={kl_loss:.6f}, loss={loss:.4f}")
print(f"d(loss)/d(new_logps) = {new_logps.grad}")
```

**需要观察的现象**：`kl_loss` 等于 `0.001 × mean([-0.1, -0.3, 0.4, -0.5]) = -1.25e-4`；梯度每个分量为 \(-\text{adv}_i/4 + 0.001/4\)，即策略梯度主导、KL 项只贡献一个恒定的微小偏移 `kl_coeff/N`。

**预期结果**：手算与程序输出一致（`kl_loss = -0.000125`）。这一恒定偏移解释了 KL 项的「拉回」方式：它不区分哪条 rollout 该升该降，只统一压低采样序列的相对对数概率差，靠期望语义把分布拉回参考。梯度数值可手算验证；如遇版本差异，待本地确认。

#### 4.2.5 小练习与答案

**练习 1**：`--kl_coeff 0` 时脚本与 7_4 版还有行为差异吗？

**答案**：损失计算上没有——`ref_model=None`、不创建副本、`kl_loss` 恒为 0 张量，损失退化为 `pg_loss`。但 CSV 里仍会写 `kl_loss=0` 列（7_6 脚本默认就是 `--kl_coeff 0.0`，其官方说明见 [ch07_main.ipynb:L2403-L2406](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L2403-L2406)）。

**练习 2**：rollout 由 `old_model` 采样而非当前策略，这对 KL 估计器意味着什么？

**答案**：严格说估计器变成了用 `old_model` 样本近似 \(\mathbb{E}_{\pi_\theta}\)，是轻度 off-policy 的（与 u8-l2 中比率的漂移修正同源）。`inner_epochs=2` 的第二次更新时偏差更大。缓解办法是重要性加权重加权 KL（见 4.3.3），即 DeepSeek V3.2 的做法之一。

**练习 3**：为什么零优势短路分支返回的 `loss` 是 `0.0` 而不是 `kl_loss`？

**答案**：`skip_zero_adv` 的语义是「这一组 rollout 没有学习信号，跳过更新」，返回 `loss_tensor=None` 后训练循环不执行 `backward()`（[L378-L383](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L378-L383) 只在 `loss_tensor is not None` 时更新），因此 KL 项也被一并跳过。默认 `skip_zero_advantage_updates=False` 时则会照常计算——这一点在 4.3 节的崩溃分析里很关键。

### 4.3 beta 权衡：真实日志中的 KL 正则效果

#### 4.3.1 概念说明

\(\beta\)（`kl_coeff`）是「锚」与「帆」的调节旋钮：

- **β 大**：策略被紧紧拴在参考模型上，漂移小、基座通用能力保留好，但探索受限——模型难以跳出基座已有的解题风格，奖励上涨慢。
- **β 小**：策略自由追逐奖励，学习快，但漂移不受控，可能丢掉语言能力甚至崩坏。
- **β = 0**：完全没有锚，退回 7_4 版（纯裁剪约束）。

理论权衡如此，但**工程结论必须来自测量**。本书最可贵的一点是：作者把两次 500 步真实运行的完整日志发布在仓库里，让读者亲眼看到 β=0.001 在 0.6B 数学训练上的实际表现——结果是反直觉的。

#### 4.3.2 核心流程：这次运行是怎么崩的

两次运行配置相同（steps=500、max_new_tokens=1024、num_rollouts=8、temperature=0.8、top_p=0.9、clip_eps=10、inner_epochs=2、seed=42），唯一差别是 7_5 打开了 `kl_coeff=0.001`（7.5 节的运行设置在 [ch07_main.ipynb:L2406-L2412](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L2406-L2412) 的官方说明中得到印证：7.6 节复现 7.5 设置的命令正是 `--kl_coeff 0.001 --inner_epochs 2`）。

7_5 日志的关键节点（各列含义见 CSV 第 1 行表头）：

| 步 | kl_loss | reward_avg | entropy_avg | avg_response_len | eval_acc | 阶段 |
|---|---|---|---|---|---|---|
| 1 | −0.0967 | 0.125 | 1.27 | 38.4 | 15.6 | 起点（基座水平） |
| 50 | 0.129 | 0.000 | 1.22 | 369.5 | **40.8** | 短暂健康，评测大涨 |
| 55 | 0.088 | 0.500 | 0.44 | 295.5 | — | 最后的好时光 |
| 60 | −0.637 | 0.000 | 7.13 | 739.5 | — | 熵开始起飞 |
| 66 | 8.51 | 0.000 | 10.49 | 975.1 | — | 加速恶化 |
| 71 | 17.38 | 0.000 | 11.10 | 1024 | — | 响应顶满 |
| 75 | 24.25 | 0.000 | 11.59 | 1024 | — | 基本崩盘 |
| 100 | 25.35 | 0.000 | 11.89 | 1024 | **0.0** | 评测归零 |
| 500 | 26.99 | 0.000 | 11.93 | 1024 | **0.0** | 冻结在崩溃态 |

数据出处：[7_5_plus_kl_metrics.csv:L2](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv#L2)（step 1）、[L51](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv#L51)（step 50）、[L56-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv#L56-L81)（step 55–80 的崩塌过程）、[L101](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv#L101)（step 100，eval=0.0）、[L501](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_5_plus_kl_metrics.csv#L501)（step 500）。

对照 7_4（无 KL）同 500 步：reward_avg 在 0–1 间正常波动（step 50 为 1.0、step 300 为 1.0、step 500 为 0.875），entropy_avg 长期低于 2.6，响应长度在 171–922 间震荡而非钉死 1024，eval_acc 稳定在 39–46%（[7_4_plus_clip_ratio_metrics.csv:L51](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv#L51)、[L301](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv#L301)、[L501](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv#L501)）。

注意 7_5 崩溃态的两个指纹：`entropy_avg=11.928` 几乎贴着 \(\ln(151936)\approx 11.931\) 的均匀分布上限；`loss ≈ kl_loss`（如 step 500 两者都是 26.99）——因为 reward 全零导致 `adv_std=0`、`pg_loss=0`，损失完全由 KL 项构成。

作者在 notebook 中给出崩溃的两条机理（[ch07_main.ipynb:L1798-L1803](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L1798-L1803)）：

1. **求和式 KL 激励长输出**：KL 项建立在答案区对数概率的**求和**上，序列越长数值越大，训练因此有把回答拉长的倾向——日志中响应长度恰好单调冲向 1024 上限；长序列反过来放大估计量与梯度，失稳自我加强。
2. **奖励坍缩后 KL 独占梯度**：全零奖励 → 优势全零 → 策略梯度无信号；而 KL 项仍然有梯度，成为唯一更新方向，可以把模型推向高熵/近均匀分布——这解释了随机 token 输出（作者实测样本形如 `"framework.raises Self profess(import"`）与 0.0% 的评测准确率。

#### 4.3.3 源码精读：缓解方案与社区共识

主 notebook 随后列出四条替代稳定方案与一条社区共识（[ch07_main.ipynb:L1811-L1836](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb#L1811-L1836)）：

1. **token 级归一化**：对比率与 KL 计算前的 logprob 除以生成长度，消除拉长序列的激励；
2. **调低 β**（例如降到 0.001——作者注明「在我的运行里同样没能救回来」；本次日志用的正是 0.001）；
3. **重加权 KL（重要性采样）**：`kl_loss = kl_coeff * torch.mean(torch.exp(new_logps - old_logps) * (new_logps - ref_logps))`，用 \(\exp(\text{new}-\text{old})\) 修正 4.2.5 练习 2 指出的 off-policy 偏差；
4. **加一个小额格式奖励**让优势不全为零（正是下一讲 7_6 的入口）。

而**共识是：数学类 RLVR 训练普遍建议干脆不加 KL 项**（Dr. GRPO、Olmo 3、DeepSeek V3.2），既更稳，又省掉一份参考模型的显存与代码复杂度。KL 项作为 GRPO 论文的标准组件，本书仍完整实现并实测——这正是「从零构建」的教学价值：读者看到的不是成功学的堆叠，而是一次带完整日志的受控失败。

#### 4.3.4 代码实践：对比两份日志并写出结论

**实践目标**：亲手从两份 CSV 中提取 `kl_loss` 与 `reward_avg` 的走势，验证 4.3.2 的叙述，形成自己的判断。

**操作步骤**（示例代码，CSV 已随仓库提供，无需 GPU）：

```python
import csv
from pathlib import Path

def load(path):
    with Path(path).open(newline="") as f:
        return list(csv.DictReader(f))

kl  = load("ch07/02_logs/7_5_plus_kl_metrics.csv")   # 有 KL
ref = load("ch07/02_logs/7_4_plus_clip_ratio_metrics.csv")  # 无 KL 对照

for rows, tag in [(kl, "7_5(+KL)"), (ref, "7_4(noKL)")]:
    for r in rows:
        if r["step"] in {"1", "50", "60", "75", "100", "200", "300", "500"}:
            print(tag, r["step"],
                  "kl=", r.get("kl_loss", "N/A"),
                  "reward=", r["reward_avg"],
                  "entropy=", r["entropy_avg"],
                  "resp_len=", r["avg_response_len"],
                  "eval=", r["eval_acc"] or "-")
```

**需要观察的现象**：7_5 的 `kl_loss` 从 ±0.1 量级在第 60–75 步间爆到 ~25 并恒定；同期 `reward_avg` 跌到 0 后 425 步再未回升；`eval_acc` 从 40.8% 直落 0.0%。7_4 的 `reward_avg` 持续在 0–1 波动、`eval_acc` 全程 39–46%。若想画图，可直接复用 u8-l1 介绍过的 `plot_grpo_metrics`（`reasoning_from_scratch/ch07.py`）。

**预期结果**：程序输出与 4.3.2 的表格逐行一致。基于数据可写出如下三句结论（参考答案）：

1. 加入 KL 后 `kl_loss` 并未稳定在小值，反而在约 60–75 步从 ±0.1 爆炸到 25–28 并恒定，说明 β=0.001 未能约束住策略漂移，漂移本身在奖励坍缩前已失控。
2. `reward_avg` 的走势是分水岭：7_4 全程保持 0–1 的健康波动并维持 ~41–45% 评测准确率，7_5 在同一窗口内跌到 0 并永久卡死——在这个设定里 KL 项与「更稳训练」的预期相反。
3. 7_5 的崩溃伴随 `entropy_avg → 11.93`（均匀分布上限）与响应长度钉死 1024，且 `loss ≈ kl_loss`（pg_loss=0），印证求和式 KL 先激励长输出、再在奖励归零后独占梯度把模型推向近均匀分布的两条机理。

**练习延伸**：把对比脚本的分段步数换成 `{"120", "140", "160", "180"}`，确认 100 步之后 `kl_loss`、`reward_avg`、`entropy_avg` 三列几乎不再变化（崩溃态被冻结）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：7_5 崩溃后 `loss` 与 `kl_loss` 几乎相等（step 500 均为 26.99），这传达了什么信息？

**答案**：`loss = pg_loss + kl_loss` 而 `pg_loss = -mean(ratio·adv)`；reward 全零 → 组内标准化优势全零 → `pg_loss=0`，损失完全由 KL 项构成。结合默认不开启 `skip_zero_advantage_updates`，此时唯一的更新信号就是 KL 项本身——正是机理二的直接证据。

**练习 2**：把 β 从 0.001 调大到 0.1，按 4.1 的公式预期 `kl_loss` 列的数值会怎么变？这能让崩溃消失吗？

**答案**：同样漂移下 `kl_loss` 数值放大 100 倍（β 乘在估计器外面）。更强的锚定在漂移尚小时确有抑制力，但机理一（求和式长度激励）与机理二（KL 独占梯度）都来自估计器的结构而非 β 的大小；且 β 过大会压制学习本身。作者的实测态度是「降 β 也救不回来」，方向性缓解要靠 token 级归一化或干脆去掉 KL。

**练习 3**：既然共识是数学训练不用 KL，为什么还要完整学这一讲？

**答案**：三个理由——(1) KL 项是 GRPO 论文的标准组件，读懂它才能读懂 DeepSeek-R1 的原始损失与后续变体（7_7 的 deepseek_v32_style.py 就实现了「按领域调 KL、数学为零」）；(2) 崩溃日志是难得的负样本教材：熵贴均匀上限、`loss≈kl_loss`、响应顶满上限三个指纹，是今后诊断自己 RL 训练的通用体征；(3) 重加权 KL、token 级归一化等修复手段在其它场景（如需要保护基座能力的对齐训练）依然有用。

## 5. 综合实践

**任务：给 KL 项做一次「体检报告」。** 综合运用本讲三个模块的知识：

1. **数学层**（4.1）：手算 \(p=[0.7,0.3]\)、\(q=[0.5,0.5]\) 的 KL（≈0.0823 nat），再用 `torch.multinomial` 采样估计复现，绘制估计值随样本数 10、100、1000、10000 的收敛过程，标出小样本时出现的负值。
2. **代码层**（4.2）：对照阅读 [7_5_plus_kl.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py) 与 7_4 版（可用 `git diff` 或并排打开），列出一份「KL 新增代码清单」——至少应包含：入口校验（L134-L135）、rollout 内 `ref_logp` 收集（L160-L163）、损失组装（L224-L229）、`__main__` 中 ref_model 创建（L597-L603）、argparse `--kl_coeff`（L539-L544），并为每处写一句中文注释。
3. **数据层**（4.3）：运行 4.3.4 的对比脚本，从两份 CSV 各取 step 1/50/60/75/100/500 六个断面，制作一张双列对照表，最后用三句话给出结论（可对照 4.3.4 的参考答案自查）。

产出物：一个可复跑的 Python 脚本 + 一份 Markdown 报告。全程 CPU 可完成（模型训练部分完全用作者日志替代，这正是本项目「日志公开」设计的价值）。

## 6. 本讲小结

- KL 正则在策略梯度损失外加上 \(\beta\cdot\widehat{D}_{\mathrm{KL}}(\pi_\theta\|\pi_{\text{ref}})\)，用一份训练前冻结的参考模型惩罚策略漂移；代码实现是对采样 token 计算 `kl_coeff * mean(new_logps - ref_logps)`，是逐 token KL 的序列级蒙特卡洛估计——无偏但单样本可为负，且随答案长度求和而增长。
- 训练时内存里同时存在三份模型：被优化的当前策略、每步冻结的 `old_model`（供裁剪比率）、全程不变的 `ref_model`（供 KL）；`kl_coeff=0` 是完整的功能开关，连带省掉参考模型的显存。
- β 权衡锚定与探索：大 β 保基座能力但学得慢，小 β 学得快但漂移失控；但工程结论必须来自测量。
- 作者发布的真实日志给出反直觉结果：β=0.001 的 7_5 运行在约 60–75 步崩溃——熵冲向 11.93 的均匀上限、响应钉死 1024、奖励归零、评测从 40.8% 落到 0.0%；无 KL 的 7_4 同期全程健康（约 41–45%）。
- 崩溃两条机理：求和式 KL 激励长输出；奖励坍缩后优势全零、KL 独占梯度把模型推向近均匀分布。缓解方案有 token 级归一化、调 β、重要性加权重加权 KL、格式奖励救活优势，但社区共识是数学类训练干脆不用 KL（Dr. GRPO、Olmo 3、DeepSeek V3.2）。
- 诊断指纹三件套：`entropy_avg` 贴近 \(\ln V\)、`loss ≈ kl_loss`、`avg_response_len` 顶满上限——在自己跑 RL 训练时看到任意两个同时出现，就该停下来查日志了。

## 7. 下一步学习建议

- **下一讲 u8-l4（GRPO 改进三：格式奖励与 think 标签）**：它直接回应本讲机理二——用一个不依赖正确性的 `reward_format` 让优势不再全零，从源头避免「KL 独占梯度」的死局；同时把训练对象从 base 模型切换为 reasoning 变体。
- **延伸阅读 7_7 变体**：[7_7_improvements/deepseek_v32_style.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/deepseek_v32_style.py) 实现了「按领域调 KL 强度（数学为零）」与「重加权 KL」，可视为本讲 4.3.3 两条方案的工程落地；[olmo3_style.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_7_improvements/olmo3_style.py) 则示范了 token 级损失与去 KL 的组合。
- **复习锚点**：若对「求和 vs 平均」的长度偏差还不敏感，回到 u6-l1（`avg_logprob_answer`）对照——打分场景用平均消除长度偏差，本讲损失却用求和，两相对照能加深理解。
