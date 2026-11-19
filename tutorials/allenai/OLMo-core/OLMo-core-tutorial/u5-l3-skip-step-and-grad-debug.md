# SkipStepOptimizer:loss spike 检测与梯度裁剪

## 1. 本讲目标

学完本讲,你应该能够:

1. 解释 **loss spike**(损失尖峰)为什么危险,以及 OLMo-core 用「整步跳过」而非「更强裁剪」来应对的理由。
2. 读懂 [SkipStepOptimizer](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L12) 基类的滚动窗口统计与 \(k\sigma\) 判据,理解 `step_factor`「乘 0 而不是提前 return」的设计。
3. 掌握梯度裁剪在 OLMo-core 中的三个实现位置(基类 TrainModule、TransformerTrainModule、OLMoDDPOptimizer),以及 `latest_loss` / `latest_grad_norm` 必须在 `step()` 之前设置好的时序契约。
4. 会用 `grad_debug.py` 的 `debug_nan_inf_grad_norm()` 定位产生 NaN/Inf 梯度的具体参数。

本讲是优化器单元的第三讲,聚焦「训练稳定性防线」:上一讲的调度器决定学习率*应该多大*,本讲决定*异常步要不要执行*。

## 2. 前置知识

### 2.1 loss spike 是什么

大模型预训练中,个别 batch(坏数据、极端长序列、数值事故)会产生远超正常水平的 loss 和梯度。单步巨梯度注入 Adam 的动量 \(m_t\) 与二阶矩 \(v_t\) 后,会连续污染后续几十步的更新方向,轻则 loss 曲线出现鼓包,重则梯度爆炸成 NaN 导致训练崩溃。这种现象叫 **loss spike**(损失尖峰)。

### 2.2 梯度裁剪(gradient clipping)回顾

最常用的 **按范数裁剪** 是:先算出所有参数梯度的全局 \(L_2\) 范数 \(\lVert g \rVert_2\),若超过阈值则整体缩放:

\[
c = \min\left(1,\ \frac{\text{max\_grad\_norm}}{\lVert g \rVert_2 + 10^{-6}}\right),\qquad g \leftarrow c \cdot g
\]

裁剪把单步梯度*压小*,但被压过的更新仍然会执行;跳步(skip step)则把可疑的整步*完全放弃*。两者互补:裁剪是常态防线,跳步是极端情况的安全气囊。

### 2.3 host-device 同步

GPU 上的张量如果被 `.item()`、`float()` 或 Python `if` 读取,程序必须等 GPU 把值算完传回 CPU,这叫 **host-device sync**,会让流水线停顿。本讲会反复看到一个设计动机:*把「是否跳步」的判断结果保留为 GPU 上的 0/1 张量,直接乘进更新公式,从而避免同步*。

### 2.4 滚动窗口与 \(k\sigma\) 判据

「多大算异常?」一种相对判据:维护最近 \(W\) 步 loss(和梯度范数)的滚动窗口,当前值若超过窗口均值加 \(k\) 倍标准差,即判为异常。默认 \(W=128\)、\(k=6\),即经典的「6σ 事件」。

### 2.5 前置讲义

- u5-l1:OptimConfig 家族、`build()` 流程、参数组(param_groups)概念。
- u5-l2:调度器如何在 `optim_step` 中被调用(本讲的时序图会用到)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/optim/skip_step_optimizer.py` | `SkipStepOptimizer` 基类:滚动窗口、`get_step_factor()` 判定 |
| `src/olmo_core/optim/adamw.py` | `adamw_step()` 把 `step_factor` 织进 AdamW 公式;`SkipStepAdamW` 与 `SkipStepAdamWConfig` |
| `src/olmo_core/optim/lion.py` | `SkipStepLion`:另一个跳步实现(基类文档点名参考它) |
| `src/olmo_core/optim/moe_optimizer.py` | `OLMoDDPOptimizer`:MoE 场景下裁剪+跳步一体化的分布式融合优化器,也是 `grad_debug` 的唯一调用方 |
| `src/olmo_core/optim/grad_debug.py` | `debug_nan_inf_grad_norm()`:非有限梯度范数的诊断工具 |
| `src/olmo_core/train/train_module/transformer/train_module.py` | `TransformerTrainModule.optim_step()`:裁剪→记录→step 的时序契约 |
| `src/olmo_core/train/train_module/train_module.py` | 基类 `TrainModule.optim_step()`:最朴素的裁剪位置 |
| `src/test/optim/skip_step_optimizer_test.py` | 跳步行为单元测试(CPU 可跑) |
| `src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py` | 官方脚本中 `SkipStepAdamWConfig` 的真实用法 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块:**SkipStepOptimizer 基类** → **SkipStepAdamW 的公式织入** → **梯度裁剪的位置与时序契约** → **grad_debug 诊断工具**。

### 4.1 SkipStepOptimizer 基类:滚动统计与跳步判定

#### 4.1.1 概念说明

`SkipStepOptimizer` 是一个**优化器包装基类**(直接继承 `torch.optim.optimizer.Optimizer`):任何优化器只要在更新公式里乘上它提供的 `step_factor`(1=执行,0=跳过),就获得了「检测到异常步则整步放弃」的能力。

它解决两个问题:

1. **判定**:当前 loss / 梯度范数相对近期历史是否异常(均值 + \(k\sigma\) 上界,单边检测——只有*过大*才跳,过小不管)。
2. **无同步执行**:判定结果是一个留在设备上的 0/1 浮点张量,子类把它乘进每一步更新,数学上等价于「这步什么都没做」,却不用把布尔值搬回 CPU、也不用 `if` 分支。

为什么不直接 `if is_bad: return`?因为 `is_bad` 要从 GPU 张量读出真值就必须 host-device sync;而且在多卡训练中,各 rank 若因时序差异拿到不同分支,参数会立刻失同步。让*每个 rank 基于相同的全局统计*算出相同的 `step_factor`(0 或 1),再以乘法施加,是确定且无同步的。

#### 4.1.2 核心流程

```
每个训练步(子类 step() 内部):
1. step_factor = self.get_step_factor()
     ├─ len(_losses) < max(2, W//2)?  → 返回 1.0(统计未热身,不检测)
     ├─ 用 _losses[:-1](不含当前值)算 loss 的 mean / std
     ├─ 若有 _grad_norms:同样算 grad_norm 的 mean / std,
     │    step_factor = (loss ≤ μℓ + kσℓ) AND (gnorm ≤ μg + kσg)
     └─ 否则只看 loss 一条
2. self._step_skipped = 1 - step_factor(供监控读取)
3. 把 step_factor 乘进全部更新公式(见 4.2)
```

判定公式(双变量版):

\[
\text{step\_factor} = \mathbb{1}\!\left[\ell_t \le \mu^\ell_{t} + k\,\sigma^\ell_{t}\right] \;\wedge\; \mathbb{1}\!\left[g_t \le \mu^g_{t} + k\,\sigma^g_{t}\right]
\]

其中 \(\mu_t, \sigma_t\) 取自**剔除当前值后**的滚动窗口(`_losses[:-1]`),窗口至多 \(W\) 条;任一变量越界即跳步。注意热身门槛:至少积累 \(\max(2, W/2)\) 个 loss 才开始检测,默认 \(W=128\) 意味着前 64 步无条件执行。

#### 4.1.3 源码精读

**构造函数只存参数和两个历史列表**。[skip_step_optimizer.py:L33-L45](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L33-L45):`rolling_interval_length`(默认 128)和 `sigma_factor`(默认 6)是仅有的两个旋钮;`_losses` / `_grad_norms` 存的是 **GPU 张量**(不是 float),这是无同步设计的第一步。

**`latest_loss` 是带副作用的 property**。[skip_step_optimizer.py:L59-L70](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L59-L70):对 `optim.latest_loss = loss` 赋值实际是把张量 append 进 `_losses`,并把列表裁到 `W + 1` 条。多保留 1 条是刻意的——`get_step_factor()` 里用 `[:-1]` 剔除当前值后,历史恰好是 \(W\) 条。`latest_grad_norm` 同构([L72-L83](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L72-L83));不设梯度范数则退化为纯 loss 判定。

**核心判定 `get_step_factor()`**。[skip_step_optimizer.py:L85-L109](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L85-L109) 值得逐行读:

- [L94-L95](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L94-L95):热身门槛,统计不足直接给 `1.0` 张量。
- [L97](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L97):`torch.std_mean(torch.stack(self._losses[:-1]))` 一次算出窗口均值与标准差,全程不下 GPU。
- [L102-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L102-L105):`torch.logical_and` 组合 loss 与梯度范数两个单边不等式,转 `.float()` 得 0/1。
- 整个方法被 [`@torch._dynamo.disable()`](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L85) 装饰:当优化器 step 被 `torch.compile` 编译(见 u5-l1 的 `OptimConfig.compile`)时,这个函数仍按 eager 执行——因为窗口长度会随训练变化,把它编进图会导致反复重编译。

**`step_skipped` 是反向读数**。[skip_step_optimizer.py:L111-L116](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L111-L116) 返回 `1 - get_step_factor()`,即「1 = 这步被跳过」,供训练循环记录成监控指标。

**使用契约写在类文档里**。[skip_step_optimizer.py:L18-L25](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L18-L25):调用方必须在 `step()` **之前**设置 `latest_loss` 和 `latest_grad_norm`;`TransformerTrainModule` 会在优化器是 `SkipStepOptimizer` 子类时自动完成(时序见 4.3)。

> 细节提醒:当窗口历史恰好只有 1 个样本时(如把 `rolling_interval_length` 设得很小做实验),`torch.std_mean` 的无偏标准差是 NaN,而与 NaN 的比较恒为 False,该步会被判为跳过。默认参数下热身门槛(\(\max(2, 64)\))保证了实际训练不会落入这种退化情形,但自己调小窗口做实验时要意识到这一点。

#### 4.1.4 代码实践

**实践目标**:直观感受两个旋钮 `rolling_interval_length` 与 `sigma_factor` 对判定的影响。

1. 打开 [src/test/optim/skip_step_optimizer_test.py:L23-L51](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/skip_step_optimizer_test.py#L23-L51),注意它用 `rolling_interval_length=2, sigma_factor=1` 把检测调到最灵敏。
2. 运行:`pytest -v src/test/optim/skip_step_optimizer_test.py`(纯 CPU,无 GPU 依赖)。
3. 把测试里的 `sigma_factor=1` 改成 `6`(在脑中或本地副本中推演),预期第 40 行注入的 `1e9` 离群 loss 仍会被跳过——因为它离群太远,\(k\) 从 1 放宽到 6 不影响结论;但如果把离群值从 `1e9` 改成 `10 * normal_loss`,`sigma_factor=6` 时就不再跳过。

**观察现象**:测试三段断言分别是「正常步不跳 → 离群步跳 → 再正常步不跳」。**预期结果**:3 项断言全部通过。若你做了第 3 步的本地实验,请记录跳与不跳的边界离群倍数。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `latest_loss` 的 setter 要保留 `rolling_interval_length + 1` 条而不是恰好 `rolling_interval_length` 条?

**答案**:`get_step_factor()` 用 `self._losses[:-1]` 把*当前*刚 append 的值从统计里剔除,再与窗口比较。保留 \(W+1\) 条,`[:-1]` 之后恰好剩 \(W\) 条历史,与「滚动窗口长度」的语义精确一致;若只保留 \(W\) 条,实际参与统计的只有 \(W-1\) 条。

**练习 2**:该检测器会不会因为某个 batch 的 loss 特别*小*而跳步?给出代码依据。

**答案**:不会。判定不等式是 `(latest - mean) <= k * std`,即只设了**上界**(单边检测)。loss 远低于均值时左式是非常大的负数,必然满足 `<=`,`step_factor` 仍为 1。依据见 [skip_step_optimizer.py:L102-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L102-L107)。

**练习 3**:如果不设置 `latest_grad_norm`,检测行为有何变化?

**答案**:`self._grad_norms` 为空列表时,`get_step_factor()` 走 [L106-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L106-L107) 的单变量分支,只按 loss 判定;梯度范数通道关闭。反过来说,想让「梯度爆炸但 loss 还没显出来」的步也被拦下,就必须喂梯度范数——这正是 4.3 时序契约的动机之一。

### 4.2 SkipStepAdamW:把 step_factor 织进更新公式

#### 4.2.1 概念说明

基类只负责*判定*;真正实现「跳过」的是子类。`SkipStepAdamW` 把标准 AdamW 更新公式的每一项都乘上 `step_factor`:

- `step_factor = 0` 时,权重衰减系数变成 1、动量混合权重变成 0、更新量变成 0、内部 step 计数不前进——整步在数学上是**严格的 no-op**;
- `step_factor = 1` 时,公式还原为标准 AdamW。

这种「软跳过」的代价只是几次逐元素乘法,换来零 host-device 同步与全 rank 行为一致。

#### 4.2.2 核心流程

单个参数的更新(记 \(f\) 为 step_factor,\(\beta_1,\beta_2\) 为 betas):

\[
\begin{aligned}
p &\leftarrow p \big(1 - f \cdot \lambda \cdot \text{wd}\big) &&\text{解耦权重衰减}\\
m &\leftarrow m + f(1-\beta_1)(g - m) &&\text{一阶矩}\\
v &\leftarrow \big(1 - f(1-\beta_2)\big)v + f(1-\beta_2)\,g^2 &&\text{二阶矩}\\
p &\leftarrow p - f \cdot \frac{\lambda}{1-\beta_1^{s+1}} \cdot \frac{m}{\sqrt{v/(1-\beta_2^{s+1})} + \epsilon} &&\text{参数更新}\\
s &\leftarrow s + f &&\text{内部步计数}
\end{aligned}
\]

注意 \(s\) 也乘 \(f\):被跳过的步不推进 Adam 的偏差校正时钟,保证后续 bias correction 语义连续。

#### 4.2.3 源码精读

**单参数版 `adamw_step()`**。[adamw.py:L12-L47](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L12-L47)。关键三处:

- [L29](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L29):`p.mul_(1 - step_factor * (lr * weight_decay))` —— 衰减量乘 \(f\)。
- [L32-L34](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L32-L34):一阶矩用 `lerp_`,插值权重是 \(f(1-\beta_1)\)(\(f=0\) 时 m 原样不动);二阶矩的衰减与新息同样乘 \(f\)。
- [L43-L47](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L43-L47):最终更新 `update.mul_(step_factor)` 后加回参数,并且 `step.add_(step_factor)` 只在 `step_increment_bugfix=True` 时推进内部计数。

**多张量版 `foreach_adamw_step()`**。[adamw.py:L50-L102](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L50-L102) 用 `torch._foreach_*` 系列把整组参数的一次更新合并成少量 kernel,公式完全同上([L99](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L99) 的 `(-step_factor * step_sizes)` 同样织入 \(f\))。另注意 [L76-L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L76-L80) 的注释:`foreach_lerp_` 在 DTensor 下有已知问题,所以用 `_foreach_mul_`/`_foreach_add_` 手工展开 lerp。

**`SkipStepAdamW.step()`**。[adamw.py:L146-L183](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L146-L183) 的流程是:取 `step_factor = self.get_step_factor()` → 缓存 `self._step_skipped = 1 - step_factor` → 逐参数初始化优化器状态(`step` 是设备上的 float32 标量张量、`exp_avg`/`exp_avg_sq` 按 `dtype` 字段开缓冲,[L165-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L165-L169))→ 调 `adamw_step()`。传参前用 `get_local_tensor()`([L172-L173](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L172-L173))把 DTensor 退化为本地分片,因此天然兼容张量/流水线并行的分片参数。`foreach=True` 时走 [`_step_foreach()`](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L185-L230),逻辑相同只是按参数组收集列表后批量更新。

**`step_increment_bugfix` 的来历**。[adamw.py:L272-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L272-L279) 的文档字符串写明:早期实现里内部 step 计数从不递增,导致有效学习率约是标称值的 2.2 倍且偏差校正失效;该标志默认 `True` 修复此 bug,设 `False` 可复现旧行为(用于对照旧 checkpoint)。

**配置类 `SkipStepAdamWConfig`**。[adamw.py:L254-L295](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L254-L295) 以注册名 `skip_step_adamw` 挂进 `OptimConfig` 体系(承接 u2-l2 的 Registrable):字段除标准 AdamW 超参外,还有 `rolling_interval_length`(默认 128,见 [L281-L284](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L281-L284) 的文档)与 `sigma_factor`(默认 6,[L286-L289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L286-L289));`build()` 会把这些字段自动透传给 `SkipStepAdamW` 构造函数(u5-l1 讲过的机制)。

**官方脚本的真实用法**。[OLMo-2-0325-32B-train.py:L69-L78](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L69-L78):OLMo-2 32B 主训练用 `SkipStepAdamWConfig(lr=6e-4, weight_decay=0.1, betas=(0.9, 0.95), compile=True, foreach=False, ...)`,窗口与 sigma 用默认值。用 `grep -rn "SkipStepAdamWConfig" src/scripts/official` 可以看到 OLMo-2/OLMo-3/OLMo-hybrid 的全部官方配方都选它——跳步是大规模预训练的默认保险,而非可选项。

**旁证:SkipStepLion**。[lion.py:L82-L138](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/lion.py#L82-L138) 是同一基类的另一个实现(Lion 的符号更新同样乘 `step_factor`),基类文档([skip_step_optimizer.py:L27-L30](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L27-L30))专门以它为例说明如何在 `step()` 内无同步地使用 `get_step_factor()`——实现自己的跳步优化器时照此模板即可。

#### 4.2.4 代码实践

**实践目标**:确认「跳步 = 内部状态严格不动」。

1. 阅读并运行 `pytest -v src/test/optim/skip_step_optimizer_test.py`(与 4.1.4 同一文件,这里关注第 40 行的离群注入方式:`optim.latest_loss = torch.tensor(1e9)`——直接替换赋值,梯度本身正常)。
2. 在脑中推演:第 2 步被跳过时,`exp_avg`、`exp_avg_sq`、`state["step"]` 各自是否变化?
3. (可选,待本地验证)在测试第 42 行 `optim.step()` 之后插入一行打印:`print({k: v for k, v in next(iter(optim.state.values())).items() if k == "step"})`,对比正常步与跳过步的输出。

**观察现象**:跳过步之后 `state["step"]` 的值与跳过前相同(因为 [L47](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0c3d44e/src/olmo_core/optim/adamw.py#L47) 的 `step.add_(step_factor)` 加了 0)。**预期结果**:正常步计数 +1,跳过步计数不变;`exp_avg` 数值逐位相同(可用 `torch.equal` 断言)。

#### 4.2.5 小练习与答案

**练习 1**:把 `step_factor` 只乘在最终更新 `update.mul_(step_factor)`([L44](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L44))上、其余地方不乘,跳步还能成立吗?

**答案**:不能。若动量更新不乘 \(f\)([L32-L34](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L32-L34)),坏梯度会照样写进 `exp_avg`/`exp_avg_sq`,污染后续所有步——这正是 loss spike 危害的根源;权重衰减若不乘 \(f\),被跳过的步参数仍会缩小。软跳步必须覆盖**公式的每一项**,包括内部 step 计数。

**练习 2**:`SkipStepAdamWConfig.foreach` 默认为 `True`,而 OLMo-2 32B 官方脚本显式写了 `foreach=False`。哪个版本更快?为什么还要关?

**答案**:`foreach=True` 用 `torch._foreach_*` 多张量 kernel,通常更快(配置文档 [adamw.py:L266-L270](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/adamw.py#L266-L270) 即如此说明)。但该脚本同时开了 `compile=True`(用 `torch.compile` 编译优化器 step):编译路径与 foreach 多张量路径的组合在当时的官方配置下不是最优/最稳的选择,故显式关闭。两者是「编译展开」与「手工多张量」两种批处理策略的取舍。

**练习 3**:MoE 用的 `OLMoDDPOptimizer` 并不继承 `SkipStepOptimizer`(见 [moe_optimizer.py:L519](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L519)),它如何获得跳步能力?

**答案**:它把同一套逻辑复制进了自己的类:滚动窗口 property([moe_optimizer.py:L1021-L1044](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1021-L1044))与逐行等价的 `get_step_factor()`([moe_optimizer.py:L2092-L2107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L2092-L2107))。作为自带 fp32 主权重、按 DP/EP 分片的融合优化器,它走的是独立实现路线,类文档([L519-L528](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L519-L528))明确写了「fused per-group AdamW step with skip-step loss-spike detection」。

### 4.3 梯度裁剪在哪里:optim_step 的时序契约

#### 4.3.1 概念说明

OLMo-core 的梯度裁剪**不是优化器的构造参数**,而是发生在 `TrainModule.optim_step()` 里、`optim.step()` 之前的独立一步。对跳步优化器而言,裁剪还顺带生产了检测器需要的第二路信号:`latest_grad_norm`。

于是形成一条严格的时序契约:

```
train_batch():
  前向 → loss.backward()
  若 optim 是 SkipStepOptimizer:
      先把 loss 跨 rank 归约成全局均值 → optim.latest_loss   ← 信号①
optim_step():
  ① 若设了 max_grad_norm:计算总梯度范数并裁剪
  ② 记录 "total grad norm" 指标
  ③ 若 optim 是 SkipStepOptimizer:optim.latest_grad_norm = 裁剪前范数  ← 信号②
  ④ 调度器为本步设定 lr(u5-l2)
  ⑤ optim.step()(内部用信号①②算 step_factor)
  ⑥ 记录 "step skipped" 指标
zero_grads():optim.zero_grad()
```

要点:两个信号都必须在 `step()` 之前就位(基类文档的硬性要求);且 loss 必须是**全局**的——各 rank 拿本地 loss 判定会得出不同 `step_factor`,参数立即失同步。

#### 4.3.2 核心流程

裁剪本身的算法(与 2.2 公式一致):

```
total_norm = ‖所有参数梯度‖₂(跨并行维度正确归约)
clip_coef = max_grad_norm / (total_norm + 1e-6)
所有梯度 ×= clamp(clip_coef, max=1.0)     ← 无条件乘,用 clamp 避免 if 同步
```

分布式难点在「total_norm 怎么算才对」:分片(DTensor/FSDP)、复制、流水线各持有一部分梯度,必须先按分片平方和的局部和、再跨进程组归约,最后开方——TransformerTrainModule 与 OLMoDDPOptimizer 对此有各自实现。

#### 4.3.3 源码精读

**信号①:train_batch 里提前归约 loss**。[train_module.py:L502-L510](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L502-L510):`isinstance(self.optim, SkipStepOptimizer)` 分支中,先把 `ce_batch_loss` 除以各 rank 的 token 数因子、`dist.all_reduce` 求和再平均(注释写明「Need to reduce the loss right away for the SkipStepOptimizer」),然后才 `self.optim.latest_loss = ce_batch_loss`。普通优化器则走 [L512](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L512) 的延迟归约路径(`ReduceType.mean`,日志时才归约)——跳步优化器等不到那时。

**时序契约的主战场 `optim_step()`**。[train_module.py:L583-L606](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L583-L606) 完整对应上面流程图的 ①-⑥:[L586-L591](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L586-L591) 裁剪并记录(注释强调范数已跨 rank 归约,指标 `reduce_type=None` 防止二次归约);[L592-L593](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L592-L593) 写入信号②;[L602-L604](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L602-L604) step 之后把 `optim.step_skipped` 记录为命名空间 `optim` 下的 **"step skipped" 指标**——训练日志里看到它变成 1,就知道检测器拦下了一步。

**裁剪实现 `_clip_grad_norm()`**。[train_module.py:L683-L710](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L683-L710) 分两路:FSDP 模型直接委托 [`model.clip_grad_norm_`](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L686-L687)(FSDP 框架自己懂分片);否则用改编自 torchtitan 的两段式——`nn.utils.get_total_norm` 算范数([L694-L696](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L694-L696)),若是 DTensor 则 `full_tensor()` 归约([L704-L707](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L704-L707),注释说明这也是为了让 PP 场景下 `.item()` 拿到正确值),最后 `clip_grads_with_norm_` 施加裁剪([L709](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L709))。**返回的是裁剪前的总范数**——正好就是检测器要的梯度范数信号。

**最朴素的对照组:基类 TrainModule**。[train_module.py:L390-L400](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L390-L400) 只有一行 `nn.utils.clip_grad_norm_(self.model.parameters(), self.max_grad_norm)` 然后直接 `optim.step()`——没有信号②、没有跳步指标,这就是「裁剪」与「裁剪+跳步」两种配置的差异全景。

**优化器内裁剪:OLMoDDPOptimizer._clip_grad()**。[moe_optimizer.py:L1072-L1140](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1072-L1140) 把裁剪搬进了优化器内部(它的 `step()` 在 [L1277-L1285](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1277-L1285) 里依次:校验 loss 有限 → `_clip_grad()` → `self.latest_grad_norm = total_grad_norm` → `_step_foreach`)。函数开头的大段注释([L1073-L1087](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1073-L1087))解释了为什么:优化器看到的模型已经被 PP/EP/DP 多维切过,范数必须按「分片量分桶归约、复制量直接取」的组合方式算([L1114-L1119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1114-L1119))。[L1131-L1138](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1131-L1138) 的裁剪施加与 4.2 的软跳步同一手法:`clamp(clip_coef, max=1.0)` 后**无条件** `foreach_mul_`,注释明说是为了躲开 `if clip_coef < 1:` 带来的 CPU↔设备同步。

#### 4.3.4 代码实践

**实践目标**:亲手梳理一次 `optim_step` 的完整时序(源码阅读型实践)。

1. 打开 [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L583-L606),从 `train_batch()`(L502 起)读到 `optim_step()`(L583 起)。
2. 列出一个 step 内下列事件的先后顺序:loss 归约、`latest_loss` 赋值、梯度裁剪、`total grad norm` 指标、`latest_grad_norm` 赋值、调度器 `set_lr`、`optim.step()`、`step skipped` 指标、`zero_grads`。
3. 回答:如果工程上把 `optim.latest_grad_norm = grad_norm`(L593)挪到 `optim.step()` 之后,会发生什么?

**观察现象**:时序表应与 4.3.1 的流程图一致;`zero_grads` 由 Trainer 主循环在 `optim_step` 之后调用(u1-l4 讲过的 `train_batch → optim_step → zero_grads` 节奏)。**预期结果**:第 3 问——`step()` 内 `get_step_factor()` 读到的 `_grad_norms` 比当前步落后一步(或首轮为空),梯度范数检测通道要么失真要么失效;这违反了 [skip_step_optimizer.py:L18-L21](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/skip_step_optimizer.py#L18-L21) 的文档契约。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `TransformerTrainModule` 记录 `total grad norm` 指标时显式传 `reduce_type=None`?

**答案**:因为 `_clip_grad_norm()` 返回的范数已经跨所有 rank 归约过(DTensor `full_tensor()` 或 FSDP 内部归约),每个 rank 手里的值相同;若再按指标系统做一次 mean 归约,等于把同一个数平均回自己,虽不出错但多余,而其他 reduce 语义则会算错。见 [train_module.py:L588-L591](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L588-L591) 的注释。

**练习 2**:`max_grad_norm` 设为 `None`(不裁剪)时,跳步检测还能工作吗?

**答案**:只能工作一半。`optim_step` 的裁剪分支整体跳过([L586](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L586) 的 `if self.max_grad_norm is not None`),`latest_grad_norm` 永远不会被设置,`get_step_factor()` 退化为纯 loss 判定(4.1 练习 3)。想要完整检测,必须开启裁剪——检测信号是裁剪的副产品。

**练习 3**:OLMoDDPOptimizer 把裁剪放进优化器、TransformerTrainModule 把裁剪放在训练模块,两种放法各适合什么场景?

**答案**:TransformerTrainModule 的放法适合「优化器是黑盒(torch 原生或 SkipStepAdamW)」的常规路径,裁剪用模型侧的通用工具完成;OLMoDDPOptimizer 自持 fp32 主权重与 DP/EP 分片梯度,只有它自己清楚每个分片的布局,范数计算与裁剪必须与梯度搬运(`_copy_model_grads_to_main_grads`)在同一个对象内完成,顺便也把 `latest_grad_norm` 的赋值([moe_optimizer.py:L1283](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1283))内化成 `step()` 的一部分,契约不可能被调用方违反。

### 4.4 grad_debug:定位非有限梯度的元凶参数

#### 4.4.1 概念说明

裁剪和跳步处理的是「太大」的梯度;最恶性的故障是 **NaN/Inf 梯度**——范数一旦非有限,`clip_coef` 变 NaN 会污染所有参数,训练基本报废。此时第一现场的问题是:*几百个参数里,是哪一个先坏的?*

`grad_debug.py` 的 `debug_nan_inf_grad_norm()` 就是这个「事故调查员」:给定(节点本地的)总梯度范数,若它是有限的,函数**立即返回、零开销**;若非有限,则逐参数计算本地 \(L_2\) 范数与 \(L_\infty\) 范数(最大绝对值)、有限性标志,把非有限的「嫌犯」和范数最大的 Top-K「背景」一起打日志,可选地 `torch.save` 落盘供事后分析。

它体现了一个性能模式:**延迟求值**——`component_norms` 和 `iter_local_grads` 两个参数是回调函数而非现成数据,只有确认要报警时才被调用,正常训练步不为诊断付任何代价。

#### 4.4.2 核心流程

```
debug_nan_inf_grad_norm(total_grad_norm_local, ...):
  1. rank 不在 ranks_filter 内 → 返回
  2. total_grad_norm_local 全有限 → 返回(常态,零成本)
  3. component_norms() → 各分桶(dp 复制/分片、ep 复制/分片)的本地范数
  4. 遍历 iter_local_grads() 的每个 (name, param_group, placements, grad):
       local_norm = ‖grad‖₂, max_abs = ‖grad‖∞
       任一非有限 → bad_entries(嫌犯列表)
       全部进 top_entries
  5. top_entries 按 local_norm 降序(非有限排最前)
  6. 可选:torch.save 完整报告到 dump_dir/rankXXX_stepYYYYYY_....pt
  7. log.error 输出:总数、components、BAD 行、Top 行
```

#### 4.4.3 源码精读

**零成本早退**。[grad_debug.py:L53-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L53-L58):rank 过滤(`rank_matches_filter`,`"all"`/`"*"` 或逗号分隔的编号表,见 [utils.py:L74-L83](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L74-L83))之后,一次 `torch.isfinite(...).all().item()` 判断:有限就直接返回——这正是文档字符串([L40-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L40-L43))所说「component_norms/iter_local_grads 只在决定报警后才被调用」的实现位置。

**逐参数体检**。[grad_debug.py:L65-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L65-L92):对每个梯度算 `vector_norm(ord=2)`(本地范数)与 `ord=inf`(max_abs),用 `math.isfinite` 标注有限性([L71-L72](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L71-L72));非有限者进 `bad_entries`,所有条目进 `top_entries`,排序时非有限值按 \(+\infty\) 置顶([L88-L90](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L88-L90))。每条记录还带 param_group、DTensor placements、dtype、shape——这些都是定位「哪种并行布局下的哪类参数」的关键线索。

**落盘与告警**。[grad_debug.py:L94-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L94-L107) 把报告存成 `.pt` 文件(文件名含 rank 与 step);[L109-L125](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L109-L125) 用 `log.error` 输出 `BAD` 嫌犯行与 Top 范数行。[L109-L110](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L109-L110) 的注释坦承一个已知取舍:`max_log_entries` 同时截断嫌犯列表,非有限参数特别多时日志会截断(完整数据仍在 dump 文件里)。

**谁来调用它:优化器侧适配器**。[moe_optimizer.py:L1189-L1232](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1189-L1232) 的 `_maybe_debug_nan_inf_grad_norm()` 是唯一调用方,它读四个**环境变量**当开关(代码注释也自我批评这偏离了库的 Config 中心约定,TODO 迁移):

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `OLMO_DDP_DEBUG_NONFINITE_GRAD` | 总开关(`env_bool`) | 未设置(关) |
| `OLMO_DDP_DEBUG_NONFINITE_GRAD_TOPK` | Top 列表长度 | `20` |
| `OLMO_DDP_DEBUG_NONFINITE_GRAD_RANKS` | 哪些 rank 报告 | `all` |
| `OLMO_DEBUG_DUMP_DIR` | 报告落盘目录 | 未设置(只打日志) |

调用点在 `_clip_grad()` 内部([moe_optimizer.py:L1121-L1127](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1121-L1127)),紧挨着 `check_nan_inf_grad` 的异步有限性断言([L1128-L1129](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1128-L1129)):断言负责「立刻炸出来」,诊断负责「留下遗书」。适配器传入的 `step` 用的是 trainer 的 `global_step` 而非优化器内部 step——因为跳步会让后者落后于前者,注释见 [L1209-L1211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1209-L1211),这也回扣了 4.2 的 `step_increment_bugfix`。

#### 4.4.4 代码实践

**实践目标**:亲手触发一次诊断,看懂它的输出格式。

以下为**示例代码**(待本地验证;可在仓库根目录存成临时脚本运行,不要写入 `src/`):

```python
import logging
import torch
from torch import nn
from olmo_core.optim.grad_debug import debug_nan_inf_grad_norm

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s %(message)s")

model = nn.Linear(4, 4)
loss = model(torch.randn(8, 4)).sum()
loss.backward()
model.weight.grad[0, 0] = float("inf")   # 注入一个 Inf 梯度

debug_nan_inf_grad_norm(
    torch.tensor(float("inf")),            # 总范数非有限 → 触发报告
    step=123,
    component_norms=lambda: {"dp_replicated_local": torch.tensor(float("inf"))},
    iter_local_grads=lambda: [
        ("weight", "dp", "R", model.weight.grad),
        ("bias", "dp", "R", model.bias.grad),
    ],
    max_log_entries=5,
    dump_dir="/tmp/grad-debug-demo",       # 会生成 rank000_step000123_....pt
)
```

1. 运行脚本(需要已按 u1-l2 安装 `olmo_core`)。
2. 观察日志中 `BAD` 行的 `norm_finite=False` 与 `name=weight`。
3. 用 `torch.load("/tmp/grad-debug-demo/rank000_step000123_optim_nonfinite_grad_norm.pt")` 查看落盘报告的结构(注意:报告是一个 dict,含 `bad_entries` / `top_entries` 等键)。

**观察现象**:终端输出一条 `ERROR` 级日志,`bad_entries=1/2`,`bias` 因范数正常出现在 Top 列表但不在 BAD 列表。**预期结果**:`BAD` 行精准指到 `weight`;去掉注入行后重跑,无任何输出(早退路径)。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `debug_nan_inf_grad_norm` 的 `component_norms` / `iter_local_grads` 参数设计成回调而不是普通字典/列表?

**答案**:为了常态零开销。提取逐参数梯度范数需要遍历并规约全部梯度,开销不小;设计成回调后,只有总范数确认为非有限([grad_debug.py:L57-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L57-L58) 的早退失败)时才会执行提取,正常步一次都不调用。文档([L40-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/grad_debug.py#L40-L43))明确承诺了这一点。

**练习 2**:生产训练中如何零改码启用这套诊断?

**答案**:给 MoE/OLMoDDP 路径的训练进程设置环境变量后重启:`OLMO_DDP_DEBUG_NONFINITE_GRAD=1`,按需加 `OLMO_DDP_DEBUG_NONFINITE_GRAD_TOPK`(如 50)、`OLMO_DDP_DEBUG_NONFINITE_GRAD_RANKS=0,3`(只让个别 rank 报告,避免 N 卡刷屏)、`OLMO_DEBUG_DUMP_DIR=/path`(留全量证据)。开关解析见 [moe_optimizer.py:L1213-L1224](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1213-L1224)。

**练习 3**:跳步检测(4.1)能拦截 NaN 梯度吗?两者如何分工?

**答案**:大概率拦不住,也不该指望它。NaN 与任何数的比较都是 False,`get_step_factor()` 会给出 0(跳过),看似拦住了;但 OLMoDDPOptimizer 路径在 `check_nan_inf_grad=True` 时会用 `_assert_finite_async` 在裁剪处直接断言失败([moe_optimizer.py:L1128-L1129](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/moe_optimizer.py#L1128-L1129)),把 NaN 当*事故*而非*尖峰*处理。分工是:6σ 跳步对付「有限但异常大」的尖峰;有限性断言 + grad_debug 对付「已经非有限」的硬故障,后者需要人介入查根因(数据、lr、数值精度)。

## 5. 综合实践

**任务**:写一个 ~40 行的脚本,完整复现「正常训练 → 注入 loss spike → 跳步 → 恢复」的全过程,并把检测器的内部状态打印出来。这是本讲规格里代码实践任务的落地。

以下为**示例代码**(待本地验证;CPU 即可运行,依赖 `olmo_core` 已按 u1-l2 安装):

```python
"""skip_step_demo.py — 人为注入 loss spike,观察 SkipStepAdamW 的跳步行为。"""
import torch
from torch import nn
from olmo_core.optim import SkipStepAdamWConfig

torch.manual_seed(0)

class MyModel(nn.Module):                     # 与官方单测同款小模型
    def __init__(self):
        super().__init__()
        self.wte = nn.Embedding(1024, 16)
        self.fc1 = nn.Linear(16, 32)
        self.fc2 = nn.Linear(32, 16)
    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(self.wte(x))))

model = MyModel()
# 窗口调小便于演示;sigma_factor=2 比默认 6 灵敏
optim = SkipStepAdamWConfig(rolling_interval_length=4, sigma_factor=2).build(model)

def train_step(loss_spike=False, grad_spike=False):
    optim.zero_grad(set_to_none=True)
    loss = model(torch.randint(0, 128, (4, 8))).sum()
    loss.backward()
    if grad_spike:                            # 模拟梯度爆炸:把梯度放大 1e4 倍
        for p in model.parameters():
            if p.grad is not None:
                p.grad.mul_(1e4)
    # 模拟 TransformerTrainModule 的时序契约(4.3):先裁剪拿范数,再喂两个信号,最后 step
    grad_norm = nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    optim.latest_grad_norm = grad_norm.detach()
    optim.latest_loss = torch.tensor(1e9) if loss_spike else loss.detach()
    before = model.fc1.weight.detach().clone()
    optim.step()
    inner_step = next(iter(optim.state.values()))["step"].item()
    moved = (model.fc1.weight - before).abs().max().item()
    print(f"loss={optim.latest_loss.item():>12.4f}  gnorm={optim.latest_grad_norm.item():8.4f}  "
          f"skipped={optim.step_skipped.item():.0f}  window={len(optim._losses)}  "
          f"inner_step={inner_step:.0f}  |Δw|={moved:.2e}")

for i in range(6):
    train_step()                              # 热身 + 正常步
print("---- 注入 loss spike ----");  train_step(loss_spike=True)
print("---- 注入 grad spike ----");  train_step(grad_spike=True)
for i in range(3):
    train_step()                              # 恢复正常
```

**操作步骤**:

1. 把脚本存到仓库外的任意目录,`python skip_step_demo.py` 运行。
2. 先做对照实验:把 `sigma_factor=2` 改回默认 `6`,再跑一次。
3. 再运行官方单测做交叉验证:`pytest -v src/test/optim/skip_step_optimizer_test.py`。

**需要观察的现象**:

- 热身阶段(窗口样本数 < `max(2, 4//2)=2`)`skipped` 恒为 0;
- 注入 loss spike 的那一步 `skipped=1`,且 `|Δw|≈0`、`inner_step` 不前进——整步是严格 no-op;
- 注入 grad spike 的那一步同样 `skipped=1`(梯度范数通道触发);
- spike 之后的正常步 `skipped=0`(检测是单边的,远低于均值不跳);
- `sigma_factor=6` 时,若你的正常 loss 波动很小,spike 仍被跳过(离群 1e9 太远)。

**预期结果**:跳步发生当且仅当注入步;`optim._losses` 长度最终不超过 `rolling_interval_length + 1 = 5`;被跳步的 `inner_step` 与上一步相同。若现象与预期不符,优先检查你是否在 `optim.step()` **之前**设置了 `latest_loss`/`latest_grad_norm`(契约见 4.1.3)。全部输出数值随环境不同,以「跳/不跳的布尔模式」为准。

## 6. 本讲小结

- **SkipStepOptimizer** 用滚动窗口(默认 128 步)的均值 + \(k\sigma\)(默认 6σ)单边判据检测 loss 与梯度范数异常,判定结果 `step_factor` 是留在设备上的 0/1 张量,全程无 host-device 同步,保证各 rank 决策一致。
- **SkipStepAdamW** 把 `step_factor` 织进更新公式的每一项(权重衰减、两个动量、更新量、内部 step 计数),跳步是数学上严格的 no-op;`step_increment_bugfix` 记录了内部计数不递增导致有效 lr 偏高 2.2 倍的历史 bug。OLMo-2/OLMo-3 全部官方配方默认使用它。
- **时序契约**:裁剪发生在 `TrainModule.optim_step()` 内、`optim.step()` 之前;`latest_loss` 在 `train_batch` 里提前跨 rank 归约,`latest_grad_norm` 复用裁剪算出的(裁剪前)总范数;两者必须先于 `step()` 就位。`OLMoDDPOptimizer` 则把裁剪、喂信号、跳步全部内化进 `step()`。
- **梯度范数的正确性**是多维并行下的难点:分片量按平方和归约、复制量直取、PP/EP 各自归并,`clamp+无条件乘` 的施加方式同样是为了避免同步。
- **grad_debug** 的 `debug_nan_inf_grad_norm()` 以「有限即零开销早退、非有限才逐参数体检」的模式定位 NaN/Inf 元凶,由 `OLMO_DDP_DEBUG_NONFINITE_GRAD` 等环境变量在 OLMoDDPOptimizer 路径触发;跳步对付有限尖峰,有限性断言 + 诊断对付硬故障。

## 7. 下一步学习建议

- **u6-l1(Trainer 训练循环剖析)**:`optim_step` / `zero_grads` 如何被主循环调度、`optim` 命名空间的指标(`total grad norm`、`step skipped`)如何被收集与跨 rank 归约——本讲的时序图将被放进完整训练循环的上下文。
- **u7-l1(数据并行)**:`_clip_grad_norm` 里 FSDP 分支与 DTensor `full_tensor()` 归约的深层原因,将在 FSDP/HSDP 的分片机制中展开。
- **延伸阅读**:`src/olmo_core/optim/moe_optimizer.py` 中 `OLMoDDPOptimizer.step()`(L1262 起)是「梯度搬运 + 裁剪 + 有限性检查 + 跳步 + 融合更新」一体化的完整样本,配合 u3-l5(MoE)阅读收获最大;`_assert_finite_async` 的异步断言机制也值得顺带一看。
