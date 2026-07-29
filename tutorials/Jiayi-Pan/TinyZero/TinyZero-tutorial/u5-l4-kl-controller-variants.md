# KL 控制器与 KL 估计变体

## 1. 本讲目标

上一讲（u5-l1）我们已经看过 `apply_kl_penalty` 如何把任务分翻译成强化学习奖励：

\[ \text{reward} = \text{score} - \beta\cdot\mathrm{KL} \]

但当时我们把两件事当成了「黑盒」：那个系数 \(\beta\)（代码里叫 `kl_coef`）究竟从哪来、会不会变化？以及那个 \(\mathrm{KL}\) 到底用哪个公式算？本讲就专门拆开这两个黑盒。

学完本讲你应当能够：

1. 说出 `FixedKLController` 与 `AdaptiveKLController` 的区别，并写出 Adaptive 控制器的「比例误差」更新公式。
2. 解释 `horizon`、`target_kl`、`n_steps` 三个参数如何共同决定 \(\beta\) 的变化速度，以及为什么 `n_steps / horizon` 不能太大。
3. 区分 `kl_penalty` 支持的 `kl` / `abs` / `mse` / `low_var_kl` 四种估计公式，并说明 `low_var_kl` 相对朴素 `kl` 的低方差优势。
4. 看清 KL 在 verl 里有**两条互斥路径**：reward 端（用控制器、默认 `kl` 估计）与 loss 端（GRPO 用、固定系数、默认 `low_var_kl`），并能根据配置判断当前走的是哪一条。

## 2. 前置知识

- **KL 散度**：衡量两个概率分布差异的非负量 \(\mathrm{KL}(p\|q)=\mathbb{E}_{x\sim p}[\log p(x)-\log q(x)]\)。在 RLHF/RL 训练里，它被用来约束「正在训练的策略」不要离「冻结的参考策略」太远，防止模型为了刷奖励而胡言乱语。
- **单样本 KL 估计**：我们无法遍历整个词表，只能用模型实际采到的 token 来估计 KL。不同估计公式在「是否无偏」「方差大小」「是否恒非负」上有取舍，这是本讲核心之一。
- **比例控制器（P 控制器）**：一种反馈控制思想——测量当前值与目标值的相对误差，按比例调整控制量。`AdaptiveKLController` 就是把 \(\beta\) 当成控制量，把 `current_kl` 拉向 `target_kl`。
- **上一讲结论**：`use_kl_loss=True` 时跳过 reward 端 KL 惩罚，把 KL 直接放进 loss（GRPO 路线）；`use_kl_loss=False` 时才走 reward 端惩罚（GAE/PPO 路线）。本讲会把这两条路径的细节补全。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py) | 定义两个 KL 控制器类、`kl_penalty` 四种估计公式，以及若干（未被主流程调用的）辅助函数 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `apply_kl_penalty`（reward 端惩罚）与 `RayPPOTrainer.__init__` 中控制器的实例化、`fit()` 中何时调用 |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | loss 端 KL：`use_kl_loss=True` 时在 `update_policy` 里把 KL 加进策略损失 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | 默认配置：`algorithm.kl_penalty=kl`、`algorithm.kl_ctrl.type=fixed`、`actor_rollout_ref.actor.kl_loss_type=low_var_kl` |

## 4. 核心概念与源码讲解

### 4.1 KL 控制器：FixedKLController 与 AdaptiveKLController

#### 4.1.1 概念说明

`kl_coef`（即公式里的 \(\beta\)）是 KL 惩罚的「缰绳力度」：

- \(\beta\) 太小 → 模型可以随便偏离参考策略，奖励黑客（reward hacking）风险上升；
- \(\beta\) 太大 → 模型被勒得太紧，几乎不敢探索，学不动。

verl 提供两种调度 \(\beta\) 的策略：

- **Fixed（固定）**：\(\beta\) 永远等于一个你给的常数，全程不变。简单、可预测，但无法根据训练中实际的 KL 大小自适应。
- **Adaptive（自适应）**：你给一个目标 KL（`target_kl`），控制器每个训练步测量当前 KL，按「比例误差」上调或下调 \(\beta\)，把实际 KL 拉回目标附近。出自论文 [Fine-Tuning Language Models from Human Preferences](https://arxiv.org/pdf/1909.08593.pdf)（代码注释里贴了链接）。

直觉：当模型跑得太远（`current_kl > target`）就勒紧缰绳（\(\beta\) 增大）；当模型太保守（`current_kl < target`）就放松（\(\beta\) 减小）。

#### 4.1.2 核心流程

Adaptive 控制器每个训练步执行一次「测误差 → 算乘子 → 调 \(\beta\)」：

1. 计算比例误差（并裁剪到 \([-0.2, 0.2]\) 防止单步跳变过猛）：

\[ \text{proportional\_error} = \mathrm{clip}\!\left(\frac{\text{current\_kl}}{\text{target}} - 1,\,-0.2,\,0.2\right) \]

2. 把误差摊到 `horizon` 步上，得到本步的乘子：

\[ \text{mult} = 1 + \text{proportional\_error}\cdot\frac{n_{\text{steps}}}{\text{horizon}} \]

3. 更新系数：

\[ \beta_{t+1} = \beta_t \cdot \text{mult} \]

其中 `n_steps` 是本步的 batch size（见 `apply_kl_penalty` 里的 `kl_ctrl.update(current_kl=current_kl, n_steps=batch_size)`）。`horizon` 越大，摊得越细、调整越温和。

> **重要约束**：当 `current_kl` 偏离 `target` 较多时，`proportional_error` 会被裁剪到 \(\pm 0.2\)，此时 `mult = 1 \pm 0.2\cdot n_{\text{steps}}/\text{horizon}`。若 \(0.2\cdot n_{\text{steps}}/\text{horizon} > 1\)，`mult` 会变成负数，导致 \(\beta\) 变号（变成负惩罚，反而鼓励偏离）——这是配 `horizon` 时必须避免的。一个安全经验是 \(n_{\text{steps}}/\text{horizon}\) 不要超过 5。

#### 4.1.3 源码精读

两个控制器类都极短。先看 Adaptive：

[verl/trainer/ppo/core_algos.py:28-43](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L28-L43) 定义 `AdaptiveKLController`，`__init__` 存 `value/target/horizon`，`update` 一行 `np.clip` 算比例误差、一行算乘子、一行自乘 `self.value *= mult`：

```python
def update(self, current_kl, n_steps):
    target = self.target
    proportional_error = np.clip(current_kl / target - 1, -0.2, 0.2)
    mult = 1 + proportional_error * n_steps / self.horizon
    self.value *= mult
```

[verl/trainer/ppo/core_algos.py:46-53](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L46-L53) 定义 `FixedKLController`，它的 `update` 是个空函数（`pass`），所以 \(\beta\) 永远等于构造时传入的 `kl_coef`：

```python
class FixedKLController:
    def __init__(self, kl_coef):
        self.value = kl_coef
    def update(self, current_kl, n_steps):
        pass
```

两者对外接口完全一致（都有 `.value` 和 `.update(current_kl, n_steps)`），因此下游 `apply_kl_penalty` 可以无差别地用 `kl_ctrl.value` 取系数、用 `kl_ctrl.update(...)` 触发更新——多态的典型用法。

**控制器在哪里被实例化？** 在 `RayPPOTrainer.__init__` 里（reward 端专用）。[verl/trainer/ppo/ray_trainer.py:326-338](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L326-L338) 读 `config.algorithm.kl_ctrl.type` 选择 fixed 或 adaptive；当**没有参考策略**（`use_reference_policy=False`）时，直接给一个 `FixedKLController(kl_coef=0.)`，相当于完全不惩罚：

```python
if self.use_reference_policy:
    if config.algorithm.kl_ctrl.type == 'fixed':
        self.kl_ctrl = core_algos.FixedKLController(kl_coef=config.algorithm.kl_ctrl.kl_coef)
    elif config.algorithm.kl_ctrl.type == 'adaptive':
        assert config.algorithm.kl_ctrl.horizon > 0, ...
        self.kl_ctrl = core_algos.AdaptiveKLController(
            init_kl_coef=config.algorithm.kl_ctrl.kl_coef,
            target_kl=config.algorithm.kl_ctrl.target_kl,
            horizon=config.algorithm.kl_ctrl.horizon)
else:
    self.kl_ctrl = core_algos.FixedKLController(kl_coef=0.)
```

> ⚠️ **源码阅读提示（陈旧/死代码）**：`core_algos.py` 里还有一个 [get_kl_controller(config)](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L56-L67) 工厂函数，看起来像是「构造控制器」的入口。但它**从未被任何代码调用**（grep 全仓只有定义处一处命中），而且它读的是 `config.critic.kl_ctrl`（与实际 yaml 里的 `algorithm.kl_ctrl` 路径不符）。真正的构造逻辑是 trainer 上面这段内联代码。同理 [compute_rewards](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L158-L160) 也是定义了却没人调用的辅助函数。读这段源码时要相信 `ray_trainer.py` 的实际接线，而不是 `core_algos.py` 里那些「看起来像入口」的函数。

#### 4.1.4 代码实践

**实践目标**：用脚本复刻 `AdaptiveKLController`，直观感受 `target`、`horizon`、`n_steps` 如何决定 \(\beta\) 的变化，并验证「低于目标 → \(\beta\) 衰减；高于目标 → \(\beta\) 增长；恰等于目标 → \(\beta\) 稳定」。

**操作步骤**：把下面这段「示例代码」存成 `adaptive_kl_demo.py` 并运行（需要 `numpy` 和 `matplotlib`）。它忠实复刻了源码的 `update` 逻辑：

```python
# 示例代码：复刻 core_algos.AdaptiveKLController 的行为
import numpy as np
import matplotlib.pyplot as plt

class AdaptiveKLController:                       # 与源码 core_algos.py:28-43 一致
    def __init__(self, init_kl_coef, target_kl, horizon):
        self.value = init_kl_coef
        self.target = target_kl
        self.horizon = horizon
    def update(self, current_kl, n_steps):
        proportional_error = np.clip(current_kl / self.target - 1, -0.2, 0.2)
        mult = 1 + proportional_error * n_steps / self.horizon
        self.value *= mult

target, horizon = 0.1, 100           # 任务指定
init_kl_coef = 0.001                 # yaml 默认 kl_coef
n_steps = 100                        # 取 n_steps == horizon，使单步乘子在 [0.8, 1.2]

# Part A：响应曲线——单步乘子 mult 关于 current_kl
kl_grid = np.linspace(0, 2, 400)
prop = np.clip(kl_grid / target - 1, -0.2, 0.2)
mult_grid = 1 + prop * n_steps / horizon

plt.figure(figsize=(10, 4))
plt.subplot(1, 2, 1)
plt.plot(kl_grid, mult_grid)
plt.axvline(target, ls='--', c='gray', label=f'target={target}')
plt.axhline(1.0, ls=':', c='gray')
plt.xlabel('current_kl'); plt.ylabel('单步乘子 mult')
plt.title('Adaptive 响应曲线（裁剪后的分段线性）'); plt.legend()

# Part B：轨迹——固定不同 current_kl，模拟 beta 随步数演化
plt.subplot(1, 2, 2)
for cur in [0.05, 0.1, 0.3, 1.0]:
    ctrl = AdaptiveKLController(init_kl_coef, target, horizon)
    traj = [ctrl.value]
    for _ in range(500):
        ctrl.update(cur, n_steps)
        traj.append(ctrl.value)
    plt.plot(traj, label=f'current_kl={cur}')
plt.xlabel('update step'); plt.ylabel('beta (=kl_coef)')
plt.title('beta 轨迹（对数轴）'); plt.yscale('log'); plt.legend()

plt.tight_layout(); plt.savefig('adaptive_kl_curve.png', dpi=120)
print('saved adaptive_kl_curve.png')
```

**需要观察的现象 / 预期结果**（待本地验证具体图像，但曲线形状可由公式推出）：

- **Part A 响应曲线**是一条「被裁平的斜坡」：`current_kl` 在 \([0.08, 0.12]\) 之外时 `proportional_error` 饱和，`mult` 恒为 \(0.8\)（左段）或 \(1.2\)（右段）；在 \([0.08, 0.12]\) 内线性从 \(0.8\) 升到 \(1.2\)，恰好在 `current_kl=target=0.1` 处穿过 `mult=1.0`。
- **Part B 轨迹**：`current_kl=0.1`（等于目标）时 \(\beta\) 全程水平；`0.05`（低于目标）时 \(\beta\) 以 \(\times 0.8\) 每步指数衰减趋近 0（对数坐标下是直线下降）；`0.3` 与 `1.0`（都远高于目标、误差饱和）时 \(\beta\) 以相同速率 \(\times 1.2\) 指数增长——**两条曲线重合**，这正是裁剪的副作用：控制器分不清「偏高一点」和「偏高很多」。
- 把脚本里 `n_steps` 改成 `600`（使 \(0.2\cdot 600/100=1.2>1\)），重跑会发现 `mult` 在饱和段变成负数（\(-0.2\)），\(\beta\) 变号——这就是 4.1.2 提到的「`n_steps/horizon` 不能太大」的直观验证。

#### 4.1.5 小练习与答案

**练习 1**：若把 `target_kl` 从 0.1 调到 0.5（其余不变），`current_kl=0.1` 时 `proportional_error` 是多少？\(\beta\) 会升还是降？
**答**：`0.1/0.5 - 1 = -0.2`（裁剪边界），`mult = 1 - 0.2*n_steps/horizon < 1`，\(\beta\) 下降。因为新目标 0.5 比当前 0.1 更宽松，控制器主动放松缰绳，鼓励模型多探索。

**练习 2**：为什么 `FixedKLController` 仍然需要 `update` 方法（哪怕是空的）？
**答**：为了与 `AdaptiveKLController` 保持接口一致。`apply_kl_penalty` 不关心具体类型，统一调用 `kl_ctrl.update(current_kl, n_steps)`；固定控制器的空 `update` 让这段通用代码无需 `if` 分支即可工作。

---

### 4.2 kl_penalty 函数：四种 KL 估计公式

#### 4.2.1 概念说明

无论走 reward 端还是 loss 端，KL 本身需要用采样到的 token 来估计。设当前（rollout）策略为 \(\pi_\theta\)、参考策略为 \(\pi_{\text{ref}}\)，对一个采样 token \(x\)，记 \(L=\log\pi_\theta(x)-\log\pi_{\text{ref}}(x)=\log\frac{\pi_\theta(x)}{\pi_{\text{ref}}(x)}\)。`kl_penalty` 函数提供四种「把 \(L\) 变成一个 KL 估计」的公式，各有取舍：

| 估计 | 公式 | 无偏？ | 恒非负？ | 方差 |
| --- | --- | --- | --- | --- |
| `kl` | \(L\) | 是（对 \(\mathrm{KL}(\pi_\theta\|\pi_{\text{ref}})\)） | 否（单样本可为负） | 高 |
| `abs` | \(|L|\) | 否 | 是 | 中 |
| `mse` | \(\tfrac12 L^2\) | 否 | 是 | 中 |
| `low_var_kl` | \(r-1-\log r\)，\(r=\pi_{\text{ref}}/\pi_\theta\) | 是 | 是 | **低** |

关键术语：

- **无偏（unbiased）**：估计的期望正好等于真实 KL。朴素 `kl` 和 `low_var_kl` 都无偏，但前者单样本可正可负，后者恒非负。
- **低方差估计（low-variance estimator）**：来自 Schulman 的博客 [Approximating KL divergence](http://joschu.net/blog/kl-approx.html)（源码注释里贴了链接），是本节重点。

#### 4.2.2 核心流程

朴素估计直接用对数比 \(L\)，方差大、可为负。Schulman 的低方差技巧改写为关于比值 \(r\) 的形式：

令 \(r=\dfrac{\pi_{\text{ref}}(x)}{\pi_\theta(x)}=\exp(\log\pi_{\text{ref}}-\log\pi_\theta)\)（注意是「参考/当前」，与朴素 \(L\) 的「当前−参考」方向相反），则：

\[ \widehat{\mathrm{KL}}_{\text{low}} = r - 1 - \log r \]

为什么它更好？两点数学事实：

1. **恒非负**：由凸性不等式 \(r-1 \geq \log r\)（对一切 \(r>0\) 成立），所以 \(r-1-\log r \geq 0\)，每个 token 的估计都不会是负数。
2. **无偏且方差更低**：在 \(x\sim\pi_\theta\) 下可证 \(\mathbb{E}[r-1-\log r]=\mathrm{KL}(\pi_\theta\|\pi_{\text{ref}})\)（仍无偏），但因为它在 \(r\) 接近 1 时近似为二次（\(\tfrac12(r-1)^2\)），尾部增长被 \(\log r\) 缓和，方差显著低于朴素的 \(L\)。

> 直觉：朴素估计 \(L\) 对「极端 token」（两策略概率相差悬殊）非常敏感，一个离群 token 就把 KL 估计炸飞；\(r-1-\log r\) 在极端处增长温和，离群 token 的影响被压住，因此更稳定。这一点对「KL 直接进入 loss、要被求导」的 GRPO 尤其重要。

#### 4.2.3 源码精读

[verl/trainer/ppo/core_algos.py:242-274](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L242-L274) 是完整的 `kl_penalty` 函数，用一连串 `if` 分发四种公式：

```python
def kl_penalty(logprob, ref_logprob, kl_penalty):
    if kl_penalty == "kl":
        return logprob - ref_logprob                       # 朴素：log(π_θ/π_ref)
    if kl_penalty == "abs":
        return (logprob - ref_logprob).abs()
    if kl_penalty == "mse":
        return 0.5 * (logprob - ref_logprob).square()
    if kl_penalty == 'low_var_kl':                          # Schulman 低方差
        kl = ref_logprob - logprob                          # log(π_ref/π_θ)
        ratio = torch.exp(kl)                               # r = π_ref/π_θ
        kld = (ratio - kl - 1).contiguous()                 # r - log r - 1 = r-1-log r
        return torch.clamp(kld, min=-10, max=10)            # 数值保护
    ...
```

逐行对照 4.2.2 的推导：`kl = ref_logprob - logprob` 即 \(\log(\pi_{\text{ref}}/\pi_\theta)\)，`ratio = exp(kl)` 即 \(r\)，`ratio - kl - 1` 即 \(r - 1 - \log r\)。`clamp(min=-10, max=10)` 是数值安全网——当两策略概率相差极大、\(r\) 爆炸时，把单 token 估计限幅，防止梯度爆炸。

注意调用方传入的参数名虽叫 `kl_penalty=`，但它接的值是「估计类型字符串」（`'kl'`/`'abs'`/`'mse'`/`'low_var_kl'`），不是系数。系数 \(\beta\) 是另一回事（由控制器或 `kl_loss_coef` 提供），别混淆。

#### 4.2.4 代码实践

**实践目标**：用数值实验亲自验证「`low_var_kl` 的方差比朴素 `kl` 低，且二者期望都接近真实 KL」。

**操作步骤**：运行下面这段「示例代码」。它构造两个简单的分类分布，按当前策略采样大量 token，分别用四种公式估计 KL，与「真值」（直接按分布解析算出的 KL）对比均值与标准差：

```python
# 示例代码：比较四种 KL 估计的均值与方差
import numpy as np

p = np.array([0.7, 0.2, 0.1])                 # 当前策略 π_θ
q = np.array([0.3, 0.4, 0.3])                 # 参考策略 π_ref
true_kl = np.sum(p * (np.log(p) - np.log(q)))  # 真值（解析）
N = 200000
x = np.random.choice(3, size=N, p=p)           # 从 π_θ 采样

Lp, Lq = np.log(p[x]), np.log(q[x])
L = Lp - Lq
r = np.exp(Lq - Lp)                            # = q/p = π_ref/π_θ

ests = {
    'kl':        L,
    'abs':       np.abs(L),
    'mse':       0.5 * L**2,
    'low_var_kl': np.clip(r - 1 - np.log(r), -10, 10),
}
print(f'true KL = {true_kl:.4f}\n')
for name, e in ests.items():
    print(f'{name:12s} mean={e.mean(): .4f}  std={e.std():.4f}')
```

**需要观察的现象 / 预期结果**：

- `kl` 与 `low_var_kl` 的 `mean` 都应非常接近 `true KL`（无偏）；`abs`、`mse` 的 `mean` 会明显偏大（有偏）。
- `low_var_kl` 的 `std` 显著小于 `kl` 的 `std`——这就是「低方差」的直接证据。
- `kl` 会有相当比例的负样本（打印 `(L<0).mean()` 可看到），而 `low_var_kl` 没有负样本。

（具体数值待本地验证，但上述大小关系由公式决定，是稳定可复现的。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mse`（\(\tfrac12 L^2\)）不是 KL 的无偏估计？
**答**：\(\mathbb{E}[\tfrac12 L^2]\) 衡量的是对数比 \(L\) 的二阶矩（接近「两倍 Pearson \(\chi^2\) 散度」），只有当两分布非常接近时才近似等于 KL；一般情况下它系统性偏大，是有偏的。

**练习 2**：把 `low_var_kl` 里的 `torch.clamp(kld, min=-10, max=10)` 去掉会怎样？
**答**：理论上结果不变（因为 \(r-1-\log r\geq 0\)，`min=-10` 几乎用不上；`max=10` 只在 \(r\) 极端大时触发）。但当两策略在某个 token 上概率悬殊时 \(r\) 会数值爆炸，导致 `kld` 变成 inf/nan 进而梯度爆炸；`max=10` 是工程上的数值保险，不改变正常区间的数学性质。

---

### 4.3 两条 KL 路径与控制器接线（reward 端 vs loss 端）

#### 4.3.1 概念说明

verl 里 KL 作用在**两个完全不同的位置**，而且**互斥**——由 `actor_rollout_ref.actor.use_kl_loss` 这一个开关决定走哪条：

- **reward 端（`apply_kl_penalty`）**：KL 作为惩罚项从奖励里扣掉，`reward = score - \beta\cdot\mathrm{KL}`，然后再算优势。对应 GAE/PPO 路线（`use_kl_loss=False`）。这里的 \(\beta\) **由控制器调度**，KL 公式默认用 `kl`（朴素无偏）。
- **loss 端（`dp_actor.py` 的 `update_policy`）**：KL 作为额外损失项直接加进策略 loss，`policy_loss = pg_loss - entropy\cdot\text{coeff} - \mathrm{KL}\cdot\text{kl\_loss\_coef}`，KL 系数是**固定**的 `kl_loss_coef`。对应 GRPO 路线（`use_kl_loss=True`），KL 公式默认用 `low_var_kl`。

理解这两条路径，才能解释一个常见困惑：「我配了 `kl_coef`，为什么训练指标里有时看到 `critic/kl_coeff`、有时看到 `actor/kl_loss`？」——因为它们分属两条路径。

#### 4.3.2 核心流程

两条路径的对照表：

| 维度 | reward 端（`apply_kl_penalty`） | loss 端（`dp_actor.update_policy`） |
| --- | --- | --- |
| 触发条件 | `use_kl_loss == False` | `use_kl_loss == True` |
| 代码位置 | `ray_trainer.py` 的 `apply_kl_penalty` | `dp_actor.py` 的 `update_policy` |
| KL 公式配置项 | `algorithm.kl_penalty`（默认 `kl`） | `actor.kl_loss_type`（默认 `low_var_kl`） |
| KL 系数来源 | `kl_ctrl.value`（fixed 或 adaptive 可调度） | `kl_loss_coef`（固定常数，默认 0.001） |
| 作用方式 | `reward = score - beta*KL`（影响后续优势） | `loss = pg - ent - kl_loss_coef*KL`（直接反传） |
| 控制器是否更新 | 是，每步 `kl_ctrl.update` | 否（系数固定，控制器不参与） |
| 典型路线 | GAE / PPO | GRPO |

reward 端的执行顺序（见 4.1.3 与上一讲 u5-l1）：在 `fit()` 里先打 `token_level_scores`（任务分），若 `not use_kl_loss` 则进入 `apply_kl_penalty`：用 `kl_penalty(...)` 算每个 token 的 kld、乘 `response_mask`、以 `kl_ctrl.value` 为系数扣分得到 `token_level_rewards`，再用 batch 平均 KL 调一次 `kl_ctrl.update`。loss 端则在 actor 反传时才发生，且根本不碰控制器。

一个微妙的设计选择：**为什么 reward 端默认用 `kl`、loss 端默认用 `low_var_kl`？** 合理解释是——reward 端的 KL 只是一个标量惩罚，之后还要经过 GAE/`masked_whiten` 归一化，方差会被吸收，无偏性更重要；而 loss 端的 KL 要直接被求导进入梯度，低方差、恒非负对梯度稳定性更关键，所以选了 `low_var_kl`。

#### 4.3.3 源码精读

**reward 端**：[verl/trainer/ppo/ray_trainer.py:84-113](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L84-L113) 是 `apply_kl_penalty` 全貌。关键几行：

```python
if 'ref_log_prob' in data.batch.keys():
    kld = core_algos.kl_penalty(data.batch['old_log_probs'],
                                data.batch['ref_log_prob'],
                                kl_penalty=kl_penalty)   # ← 默认 'kl'
    kld = kld * response_mask
    beta = kl_ctrl.value                                  # ← 控制器当前系数
else:
    beta = 0
    kld = torch.zeros_like(response_mask, dtype=torch.float32)

token_level_rewards = token_level_scores - beta * kld     # reward = score - beta*KL
...
kl_ctrl.update(current_kl=current_kl, n_steps=batch_size) # ← 控制器在这里被更新
```

它在 [fit() 主循环里的调用点](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L630-L634) 被 `if not use_kl_loss` 守卫，因此与 loss 端互斥：

```python
if not self.config.actor_rollout_ref.actor.use_kl_loss:
    batch, kl_metrics = apply_kl_penalty(batch,
                                         kl_ctrl=self.kl_ctrl,
                                         kl_penalty=self.config.algorithm.kl_penalty)
```

**loss 端**：[verl/workers/actor/dp_actor.py:259-269](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L259-L269) 在 `update_policy` 的 micro-batch 循环里：

```python
if self.config.use_kl_loss:
    ref_log_prob = data['ref_log_prob']
    kld = core_algos.kl_penalty(logprob=log_prob,
                                ref_logprob=ref_log_prob,
                                kl_penalty=self.config.kl_loss_type)   # ← 默认 'low_var_kl'
    kl_loss = masked_mean(kld, response_mask)
    policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef      # ← 固定系数
    metrics['actor/kl_loss'] = kl_loss.detach().item()
```

注意两处都调用的是**同一个** `core_algos.kl_penalty` 函数，只是传的字符串不同（`algorithm.kl_penalty` vs `kl_loss_type`），且后续对结果的处理不同（reward 端乘 `beta` 扣分并更新控制器；loss 端乘固定 `kl_loss_coef` 加进 loss）。

**配置默认值**：[ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L142-L145) 里 `algorithm.kl_penalty: kl`、`algorithm.kl_ctrl: {type: fixed, kl_coef: 0.001}`；[actor 段](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L30-L32) 里 `use_kl_loss: False`、`kl_loss_coef: 0.001`、`kl_loss_type: low_var_kl`。可见 TinyZero 默认走 reward 端 fixed 路线（因为 `use_kl_loss=False`），训练脚本 `scripts/train_tiny_zero.sh` 里也只覆盖了 `algorithm.kl_ctrl.kl_coef=0.001`。

#### 4.3.4 代码实践

**实践目标**：跟踪配置→代码，判断一个给定的运行走的是哪条 KL 路径、用的是哪种估计、系数会不会变。

**操作步骤**（源码阅读型实践，不运行训练）：

1. 打开 `scripts/train_tiny_zero.sh`，找到所有 `use_kl_loss`、`kl_penalty`、`kl_ctrl`、`kl_loss_type`、`kl_loss_coef` 相关的覆盖项，记录它们的值。
2. 结合 `ppo_trainer.yaml` 的默认值，填一张「本次运行 KL 配置表」：
   - `use_kl_loss` 最终值 → 决定走 reward 端还是 loss 端；
   - 若走 reward 端：`algorithm.kl_penalty` 用哪种估计？`kl_ctrl.type` 是 fixed 还是 adaptive？系数是多少、会不会变？
   - 若走 loss 端：`kl_loss_type` 用哪种估计？`kl_loss_coef` 是多少（固定不变）？
3. 对照 4.3.2 的表格，写出本次运行「KL 如何影响训练」的一句话总结。

**需要观察的现象 / 预期结果**：

- 默认 `train_tiny_zero.sh`（countdown，`adv_estimator=gae`）应判定为：`use_kl_loss=False` → **reward 端** → 估计 `kl` → `kl_ctrl.type=fixed` → \(\beta=0.001\) 恒定（因为 fixed 的 `update` 是空操作，`critic/kl_coeff` 指标全程为 0.001 不变）。
- 若改用 `examples/grpo_trainer` 的配置（`adv_estimator=grpo`、`use_kl_loss=True`），应判定为：**loss 端** → 估计 `low_var_kl` → 系数固定 `0.001`，此时 `apply_kl_penalty` 根本不被调用，控制器形同虚设。

（具体覆盖项以你本地脚本为准；若脚本未覆盖某项，则用 yaml 默认值。）

#### 4.3.5 小练习与答案

**练习 1**：假如同时设置 `use_kl_loss=True` 且 `kl_ctrl.type=adaptive`，KL 会被惩罚两次吗？
**答**：不会。`use_kl_loss=True` 时 `apply_kl_penalty` 被 `if not use_kl_loss` 挡掉，reward 端不执行，控制器虽然被构造出来却永远不会被 `update`（\(\beta\) 一直停在初始值）。KL 只在 loss 端以固定 `kl_loss_coef` 出现一次。这也是上一讲强调的「两条路径互斥」。

**练习 2**：reward 端 `apply_kl_penalty` 里，`current_kl` 是怎么算出来的？为什么用 `masked_mean`？
**答**：先用 `kl_penalty` 得到每个 token 的 kld 并乘 `response_mask`（只看回答段的非 pad token），再 `masked_mean(..., axis=-1)` 在序列上求平均得到每条样本的 KL，最后对整个 batch 取均值得到标量 `current_kl`（见 [ray_trainer.py:104-105](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L104-L105)）。用 mask 是为了排除右填充的 pad token，避免它们（kld=0）把 KL 估计稀释。

---

## 5. 综合实践

**任务**：为 TinyZero 设计一个「自适应 KL」实验，并预测训练指标曲线。

1. **配置改造**：复制一份 `scripts/train_tiny_zero.sh`，把 reward 端 KL 从 fixed 改成 adaptive，新增两个 yaml 里没有的键（用 `+` 前缀）：
   - `algorithm.kl_ctrl.type=adaptive`
   - `+algorithm.kl_ctrl.target_kl=0.05`
   - `+algorithm.kl_ctrl.horizon=10000`
   - 保持 `algorithm.kl_penalty=kl`、`use_kl_loss=False`。
2. **参数自检**：假设 `train_batch_size=256`。计算最坏情况下的单步乘子：`mult = 1 ± 0.2 * 256/10000 = 1 ± 0.00512`，确认它为正（满足 4.1.2 的约束），并解释为什么这里要把 `horizon` 设得比 `n_steps` 大很多（答：让每步调整温和，避免 \(\beta\) 震荡）。
3. **指标预测**：训练时观察 `critic/kl` 和 `critic/kl_coeff` 两条曲线。对照 4.1 的理论，预测：
   - 若初期模型偏离参考策略较远（`critic/kl > target_kl=0.05`），`critic/kl_coeff` 应该逐步**上升**；
   - 随着 \(\beta\) 上升、惩罚加重，`critic/kl` 应被**拉回** 0.05 附近；
   - 最终两条曲线在目标 KL 附近达成动态平衡（`critic/kl_coeff` 不再单调变化）。
4. **对比实验**：再跑一组 fixed（`kl_ctrl.type=fixed, kl_coef=0.001`），对比两组的 `critic/kl` 稳态值——adaptive 组应更贴近 `target_kl`，而 fixed 组的稳态 KL 取决于固定 \(\beta=0.001\) 是否「恰好」合适，缺乏自纠正能力。

> 说明：本任务需要在多卡 GPU 上实际跑训练才能得到真实曲线，属「待本地验证」内容；但步骤 2 的数值校核和步骤 3 的曲线趋势可由本讲公式直接推出，无需运行即可完成推理部分。

## 6. 本讲小结

- verl 用 `FixedKLController`（\(\beta\) 恒定）和 `AdaptiveKLController`（按比例误差把 `current_kl` 拉向 `target_kl`）两种控制器调度 KL 系数，二者接口一致、可无缝替换。
- Adaptive 的更新是 \(\beta \mathrel{*}= 1 + \mathrm{clip}(\text{current\_kl}/\text{target}-1,\pm0.2)\cdot n_{\text{steps}}/\text{horizon}\)；`horizon` 越大越温和，且必须保证 \(0.2\cdot n_{\text{steps}}/\text{horizon}<1\) 以免乘子变负。
- `kl_penalty` 提供 `kl`（朴素无偏、可为负、高方差）、`abs`、`mse`（后两者有偏）、`low_var_kl`（\(r-1-\log r\)，无偏、恒非负、低方差）四种估计。
- KL 有两条互斥路径：reward 端 `apply_kl_penalty`（`use_kl_loss=False`，用控制器、默认 `kl`）与 loss 端 `dp_actor`（`use_kl_loss=True`/GRPO，固定 `kl_loss_coef`、默认 `low_var_kl`）。
- TinyZero 默认走 reward 端 fixed 路线（`kl_coef=0.001`）；`core_algos.py` 里的 `get_kl_controller` / `compute_rewards` 是定义了却未被调用的死代码，真实接线在 `ray_trainer.py`。

## 7. 下一步学习建议

- 本讲把 KL 的「系数调度」和「估计公式」讲透了，但 GRPO 如何在 loss 端用 `low_var_kl` 与组内归一化优势配合，是下一讲 **u5-l5 GRPO 算法实现** 的主题，建议接着读 `compute_grpo_outcome_advantage` 与 `dp_actor.update_policy` 全貌。
- 若想看 KL 控制器在更大代码栈里的位置，可回看 u4-l3 的 `fit()` 时序图，把 `apply_kl_penalty` 这一阶段重新嵌入主循环理解。
- 对 KL 估计的数学原理感兴趣，强烈推荐读源码注释引用的 Schulman 博客 [http://joschu.net/blog/kl-approx.html](http://joschu.net/blog/kl-approx.html)，它是 `low_var_kl` 公式的直接出处。
