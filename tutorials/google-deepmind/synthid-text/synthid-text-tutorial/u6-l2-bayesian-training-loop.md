# 训练循环：损失、优化与 TPR@FPR 验证

## 1. 本讲目标

上一讲（u6-l1）我们已经把贝叶斯检测器的**推理链路**理清：给定 g 值与掩码，`BayesianDetectorModule` 用两个似然模型算出后验 \(P(w\mid g)\)。但那时我们假设可学习参数（`beta`、`delta`、`prior`）是「已知」的——它们从哪里来？答案就是本讲的主题：**训练（training）**。

本讲聚焦 `detector_bayesian.py` 中的训练子系统，学完后你应当能够：

1. 说清「交叉熵 + L2」复合损失每一项的来源与作用，并解释为什么 L2 只惩罚 `delta` 而放过 `beta`。
2. 顺着 `train` 函数的闭包嵌套，画出一次 jit 化 minibatch 参数更新的完整流程（`optax.adam` + `jax.value_and_grad`）。
3. 理解为什么默认用 **TPR@FPR**（而非交叉熵）作为验证指标，以及「负 TPR」这个小技巧如何让 `argmin` 统一处理两种指标。
4. 解释 `best_val_epoch` 是怎么选出来的、为什么最终写回检测器的是「最优 epoch」的参数而不是最后一个 epoch 的参数（即早停 / early stopping）。

## 2. 前置知识

- **g 值与后验**：回顾 u6-l1，g 值是形状 `[batch, seq, depth]` 的 0/1 张量；检测器输出后验 \(P(w\mid g)\in(0,1)\)，越接近 1 越像水印文本。
- **二分类监督信号**：训练时每条样本带一个标签 \(y\in\{0,1\}\)，1 表示水印（watermarked）、0 表示非水印（unwatermarked）。检测器本质上是一个二分类器。
- **交叉熵（cross entropy）**：衡量预测概率分布与真实标签的差异，是二分类最常用的损失。预测越靠近真实标签，交叉熵越小。
- **L2 正则（权重衰减）**：在损失里加上参数平方和，迫使参数趋向小值，缓解过拟合。
- **JAX / Flax / optax 三件套**：Flax 的 `nn.Module` 用 `apply(params, ...)` 跑前向；`jax.value_and_grad` 对参数求梯度；`optax.adam` 是 Adam 优化器，`optax.apply_updates` 把梯度叠加到参数上；`@jax.jit` 把纯函数编译成加速版本。
- **TPR 与 FPR**：在「水印 vs 非水印」二分类里：
  - **TPR（True Positive Rate，召回率）**= 被正确判为水印的水印样本占比，越高越好。
  - **FPR（False Positive Rate，假阳率）**= 被误判为水印的非水印样本占比，越低越好。
  - 二者互相牵制，`TPR@FPR=1%` 表示「在只允许误伤 1% 干净文本的前提下，能抓到多少水印文本」，是部署时最关心的指标。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 本讲关注的部分 | 作用 |
| --- | --- | --- |
| `src/synthid_text/detector_bayesian.py` | `xentropy_loss`、`loss_fn`、`l2_loss` | 损失函数与 L2 正则项 |
| 同上 | `tpr_at_fpr`、`ValidationMetric` | 验证指标 |
| 同上 | `train`（含其内部一系列闭包） | 训练主循环、minibatch、jit、最优 epoch 选择 |

提示：上一讲讲过的 `LikelihoodModelWatermarked._compute_latents`、`_compute_posterior`、`BayesianDetectorModule.__call__` 是被训练的对象，本讲把它们当作「前向黑盒」调用，不再重复其内部推导。

## 4. 核心概念与源码讲解

### 4.1 损失函数与 L2 正则

#### 4.1.1 概念说明

训练一个二分类器，需要一个「打分函数」告诉优化器当前参数好不好。SynthID 的选择是经典的**二元交叉熵**作为数据拟合项，再加一个**仅作用于 `delta` 的 L2 正则项**作为防过拟合项。

为什么只正则 `delta`、放过 `beta`？回顾 u6-l1：`beta` 是每层的偏置（一维），`delta` 是层与层之间的权重矩阵（二维，形状 `[depth, depth]`），参数量远大于 `beta`，且被下三角掩码做成自回归结构——它才是真正「记忆训练数据细节」、最容易过拟合的部分。把 L2 集中在 `delta` 上，是「把正则花在刀刃上」。

#### 4.1.2 核心流程

复合损失定义为：

\[
\mathcal{L}(\theta;\, \text{batch}) = \mathcal{L}_{\text{xent}}(y,\hat{y}) \;+\; \frac{\lambda}{n_{\text{mb}}}\sum_{i,j,k,l}\delta_{ijkl}^{2}
\]

其中：

- \(\hat{y}=P(w\mid g)\) 是检测器在当前参数下输出的后验。
- 二元交叉熵：

\[
\mathcal{L}_{\text{xent}} = -\frac{1}{N}\sum_{i}\bigl[\,y_i\log\hat{y}_i + (1-y_i)\log(1-\hat{y}_i)\,\bigr]
\]

- L2 项里 \(\lambda\) 是用户传入的 `l2_weight`，\(\sum\delta^2\) 是 `delta` 全部元素的平方和；除以 \(n_{\text{mb}}\)（minibatch 数）是为了把「按 epoch 计」的正则强度均摊到每个 minibatch 上（详见 4.1.3 与 4.2）。
- 数值稳定：交叉熵里会出现 \(\log\hat{y}\) 与 \(\log(1-\hat{y})\)，当 \(\hat{y}\) 极接近 0 或 1 时会爆炸，故先把 \(\hat{y}\) 裁剪到 \([10^{-5},\,1-10^{-5}]\)。

#### 4.1.3 源码精读

先看交叉熵。`xentropy_loss` 先裁剪预测、再做标准的二元交叉熵均值：

[src/synthid_text/detector_bayesian.py:L453-L456](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L453-L456) —— 把预测裁剪到 \([10^{-5},1-10^{-5}]\) 后计算二元交叉熵均值，裁剪是为了避免 \(\log 0\)。

```python
def xentropy_loss(y, y_pred):
  y_pred = jnp.clip(y_pred, 1e-5, 1 - 1e-5)
  return -jnp.mean((y * jnp.log(y_pred) + (1 - y) * jnp.log(1 - y_pred)))
```

再看 L2 项的「原料」。`LikelihoodModelWatermarked.l2_loss` 用 `einsum` 把 `delta` 的全部平方求和，**完全没有出现 `beta`**：

[src/synthid_text/detector_bayesian.py:L261-L262](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L261-L262) —— `einsum("ijkl->", delta**2)` 对 `delta` 四维张量求全部元素平方和，等价于 `jnp.sum(delta**2)`；注意它只覆盖 `delta`，`beta` 不受正则。

```python
  def l2_loss(self):
    return jnp.einsum("ijkl->", self.delta**2)
```

`BayesianDetectorModule.l2_loss` 只是转发给水印似然模型：

[src/synthid_text/detector_bayesian.py:L404-L405](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L404-L405) —— 检测器模块的 L2 就是其内部水印似然模型的 L2，对外统一接口。

最后，`loss_fn` 把三件事串起来：前向算后验、算未加权 L2、加权后与交叉熵相加。

[src/synthid_text/detector_bayesian.py:L459-L472](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L459-L472) —— 给定参数与一个 batch，先用 `apply` 算后验 `w_pred`，再把 `l2_batch_weight * sum(delta^2)` 加到交叉熵上。

```python
def loss_fn(params, detector_inputs, w_true, l2_batch_weight, detector_module):
  w_pred = detector_module.apply(
      params, *detector_inputs, method=detector_module.__call__)
  unweighted_l2 = detector_module.apply(params, method=detector_module.l2_loss)
  l2_loss = l2_batch_weight * unweighted_l2
  return xentropy_loss(w_true, w_pred) + l2_loss
```

要点：`loss_fn` 是一个**纯函数**——同样的 `params` 与同样的 batch 永远得到同一个标量，这是后续 `jax.value_and_grad` 能对它求导、`@jax.jit` 能编译它的前提。`l2_batch_weight` 由调用方（`train`）注入，训练时非零、验证时为 0（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：确认「L2 只作用于 `delta`、交叉熵依赖后验裁剪」，并理解 `loss_fn` 的纯函数性质。

**操作步骤**（源码阅读型）：

1. 打开 `src/synthid_text/detector_bayesian.py`，定位 `xentropy_loss`（L453）、`loss_fn`（L459）、`l2_loss`（L261）。
2. 在 `loss_fn` 里数一数：`detector_module.apply(...)` 一共被调用了几次？分别用的是哪个 `method`？
3. 思考：如果把 `l2_loss` 改成 `jnp.sum(self.beta**2) + jnp.sum(self.delta**2)`，正则行为会怎样变化？

**需要观察的现象 / 预期结果**：

- `loss_fn` 里 `apply` 出现 **2 次**：一次 `method=__call__`（算后验 `w_pred`，依赖数据），一次 `method=l2_loss`（只依赖 `delta` 参数，与数据无关）。
- 若把 `beta` 也纳入 L2，偏置会被一并压缩，可能削弱模型对「该层是否真有两个不同 token」的表达力；这正解释了源码为何刻意只罚 `delta`。

> 待本地验证：如果你已装好 jax/flax，可用如下**示例代码**（非项目原有代码）构造一个微型模块，验证 `l2_loss` 对 `beta` 改动无反应、对 `delta` 改动有反应：
>
> ```python
> # 示例代码：仅用于理解，不是项目原有代码
> import jax, jax.numpy as jnp
> from synthid_text import detector_bayesian as db
> m = db.LikelihoodModelWatermarked(watermarking_depth=4)
> rng = jax.random.PRNGKey(0)
> params = m.init(rng, jnp.zeros((1, 5, 4)))
> base = m.apply(params, method=m.l2_loss)
> # 改大 beta，l2_loss 不变；改大 delta，l2_loss 增大
> ```

#### 4.1.5 小练习与答案

**练习 1**：`xentropy_loss` 为什么要 `jnp.clip(y_pred, 1e-5, 1-1e-5)`？去掉会怎样？
**答案**：后验 \(\hat{y}\) 可能极接近 0 或 1，此时 \(\log\hat{y}\) 或 \(\log(1-\hat{y})\) 趋向 \(-\infty\)，使损失爆炸、梯度发散。裁剪到 \([10^{-5},1-10^{-5}]\) 给两端留出「安全垫」，保证数值稳定，代价是损失有微小偏差。

**练习 2**：`loss_fn` 的参数列表里 `detector_module` 排在最后，且 `train` 里用 `functools.partial` 把它绑死。这样做的好处是什么？
**答案**：`jax.value_and_grad` 默认对**第一个参数**求导。把待求导的 `params` 放第一位、把 `detector_module` 这种「配置/模板」用 `partial` 绑成闭包，既能满足 `value_and_grad` 的约定，又避免在每个 minibatch 调用处重复传 `detector_module`。

---

### 4.2 train 训练循环：minibatch、optax.adam 与 jit

#### 4.2.1 概念说明

`train` 是检测器的训练入口。它不返回一个新模型，而是**原地**更新传入的 `detector_module.params`（训练完后把最优参数写回去）。它的职责可以拆成三块：

1. **切分 minibatch + 打乱**：把训练集切成固定大小的 minibatch，可选地在 epoch 开始前打乱。
2. **参数更新**：对每个 minibatch，用 `jax.value_and_grad` 算 `loss_fn` 对 `params` 的梯度，用 `optax.adam` 更新参数；整个过程用 `@jax.jit` 编译加速。
3. **记录历史**：每个 epoch 记录训练损失、验证损失、以及当时的参数快照，供最后挑选最优 epoch。

#### 4.2.2 核心流程

一次参数更新的数学本质是 Adam 步：

\[
g_t = \nabla_{\theta}\,\mathcal{L}(\theta_t;\,\text{minibatch}),\qquad
\theta_{t+1} = \mathrm{Adam}(\theta_t,\, g_t)
\]

`train` 的整体控制流（伪代码）：

```
准备 minibatch 下标；按需 shuffle
若 module 已有 params 则沿用，否则用 init 生成
optimizer = optax.adam(lr);  opt_state = optimizer.init(params)
for epoch in 1..epochs:
    for 每个 minibatch (g, mask, label):        # update_with_minibatches
        loss, grads = value_and_grad(loss_fn)(params, ...)   # @jax.jit update
        updates, opt_state = optimizer.update(grads, opt_state)
        params = optax.apply_updates(params, updates)
    val_loss = 选定的验证指标(params)             # TPR@FPR 或 cross_entropy
    history[epoch] = {loss, val_loss, params快照}
# 训练结束后：按 val_loss 挑最优 epoch，把其参数写回 module（见 4.3）
```

#### 4.2.3 源码精读

**L2 权重的 minibatch 均摊**。`train` 先算出「每个 minibatch 应用的 L2 权重」：

[src/synthid_text/detector_bayesian.py:L591-L593](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L591-L593) —— 训练时把 `l2_weight` 除以 minibatch 数 `n_minibatches`，使一个 epoch 内累计的正则强度约等于 `l2_weight`；验证时一律取 0，因为 L2 只是训练正则、不是评估目标。

```python
  n_minibatches = len(g_values) / minibatch_size
  l2_batch_weight_train = l2_weight / n_minibatches
  l2_batch_weight_val = 0.0
```

注意 `len(g_values) / minibatch_size` 用的是 Python 真除法，结果是浮点数；当样本数不能被 `minibatch_size` 整除时，它与实际的 minibatch 个数（`len(minibatch_inds)`，向上取整）略有出入，是一种近似。

接着用 `functools.partial` 把 `loss_fn` 绑成两个专用版本：训练版带 L2、验证版不带 L2 且预先 `jit`：

[src/synthid_text/detector_bayesian.py:L594-L605](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L594-L605) —— 训练损失与验证损失共享同一个 `loss_fn`，仅 `l2_batch_weight` 不同；验证版额外 `jax.jit`。

```python
  loss_fn_train = functools.partial(
      loss_fn, l2_batch_weight=l2_batch_weight_train, detector_module=detector_module)
  loss_fn_jitted_val = jax.jit(
      functools.partial(
          loss_fn, l2_batch_weight=l2_batch_weight_val, detector_module=detector_module))
```

**核心更新步**。`update` 被 `@jax.jit` 装饰，对当前 minibatch 算梯度并应用 Adam 更新：

[src/synthid_text/detector_bayesian.py:L607-L617](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L607-L617) —— 这是整个训练的「心脏」：`jax.value_and_grad` 对 `params` 求损失与梯度，`optimizer.update` 生成更新量，`optax.apply_updates` 落到新参数。

```python
  @jax.jit
  def update(gvalues, masks, labels, params, opt_state):
    loss_fn_partialed = functools.partial(
        loss_fn_train, detector_inputs=(gvalues, masks), w_true=labels)
    loss, grads = jax.value_and_grad(loss_fn_partialed)(params)
    updates, opt_state = optimizer.update(grads, opt_state)
    params = optax.apply_updates(params, updates)
    return loss, params, opt_state
```

注意一个容易困惑的细节：`update` 的函数体里引用了 `optimizer`，但 `optimizer` 在源码中是**在这之后**才创建的（见下文 L672）。这在 Python 里没问题，因为 `update` 只在训练循环里被**调用**时才查找 `optimizer` 这个名字，而那时它早已存在；`@jax.jit` 也是在首次调用时才触发编译。

外层 `update_with_minibatches` 把一个 epoch 内所有 minibatch 串起来，并返回平均损失：

[src/synthid_text/detector_bayesian.py:L619-L633](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L619-L633) —— 逐 minibatch 调 `update`，把返回的 loss 求平均，作为该 epoch 的训练损失。

`update_fn` 把「训练一遍 + 算验证损失」组合成一个 epoch 的动作：

[src/synthid_text/detector_bayesian.py:L648-L666](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L648-L666) —— 先跑一轮 minibatch 更新，再按 `validation_metric` 选择 TPR@FPR 或 cross_entropy 计算验证损失；没有验证集时 `val_loss=None`。

```python
  def update_fn(opt_state, params):
    loss, params, opt_state = update_with_minibatches(
        g_values, mask, watermarked, minibatch_inds, params, opt_state)
    val_loss = None
    if g_values_val is not None:
      if validation_metric == ValidationMetric.TPR_AT_FPR:
        val_loss = update_fn_if_fpr_tpr(params)
      else:
        val_loss = validate_with_minibatches(...)
    return opt_state, params, loss, val_loss
```

**初始化与主循环**。参数要么复用传入的，要么用 `init` 生成；优化器用 `optax.adam`；主循环按 epoch 推进，并把每轮的 loss / val_loss / 参数快照写入 `history`：

[src/synthid_text/detector_bayesian.py:L668-L692](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L668-L692) —— 参数初始化、Adam 优化器构造，以及 `while epochs_completed < epochs` 的主循环；每个 epoch 都把 `params["params"]` 快照存进 `history`。

```python
  params = detector_module.params
  if params is None:
    params = detector_module.init(param_rng, g_values[:1], mask[:1])

  optimizer = optax.adam(learning_rate=learning_rate)
  opt_state = optimizer.init(params)

  history = {}
  epochs_completed = 0
  while epochs_completed < epochs:
    opt_state, params, loss, val_loss = update_fn(opt_state, params)
    epochs_completed += 1
    history[epochs_completed] = {"loss": loss, "val_loss": val_loss,
                                 "params": params["params"]}
```

注意 `history` 存的是 `params["params"]`（剥掉了外层 `"params"` 键的内层子树），4.3 会看到写回时再重新包一层。

#### 4.2.4 代码实践

**实践目标**：理清 `train` 内部闭包的调用层次，理解 `@jax.jit update` 为何能引用「后定义」的 `optimizer`。

**操作步骤**（源码阅读型 / 跟踪调用链）：

1. 从 `train` 的主循环（L678）出发，按顺序列出一次 `update_fn` 调用会经过的函数：`update_fn` → `update_with_minibatches` → `update` →（`jax.value_and_grad` → `loss_fn` → `xentropy_loss` + `l2_loss`）→ `optimizer.update` → `optax.apply_updates`。
2. 在源码里标注每个闭包定义的行号，以及它「被谁调用」。
3. 解释：`optimizer = optax.adam(...)`（L672）定义在 `update`（L607）之后，为什么不会报 `NameError`？

**需要观察的现象 / 预期结果**：

- 你会发现 `train` 用了「先定义一堆嵌套闭包、再在主循环里调用」的结构，所有可变状态（`params`、`opt_state`、`history`）都以参数形式在线程间传递，没有用外部可变变量——这正是 JAX 函数式编程的典型写法。
- `update` 是闭包，Python 在**调用时**才解析 `optimizer`；而它的首次调用发生在主循环（L678），那时 `optimizer`（L672）已存在，故不报错。

#### 4.2.5 小练习与答案

**练习 1**：为什么训练损失用 `loss_fn_train`（带 L2），而验证损失用 `l2_batch_weight_val=0.0` 的版本？
**答案**：L2 的作用是「在训练时压制 `delta` 防止过拟合」，它本身不是「检测器好不好」的衡量。验证的目的是评估泛化能力，应当只看数据拟合项（交叉熵）或真正的检测质量（TPR@FPR），所以验证阶段把 L2 权重置 0。

**练习 2**：把 `update` 上的 `@jax.jit` 去掉，程序结果会变吗？会变慢吗？
**答案**：数值结果不变（`jit` 不改变语义），但每个 minibatch 都会解释执行而非编译执行，速度显著下降；这正是项目用 `@jax.jit` 的理由。

---

### 4.3 TPR@FPR 验证与最优 epoch 选择

#### 4.3.1 概念说明

训练 N 个 epoch 后，用哪个 epoch 的参数？最直觉的答案是「最后一个」，但这恰恰会过拟合——后期 epoch 训练损失最低，但验证集表现可能已经下滑。正确做法是**早停（early stopping）**：按验证集表现挑最优 epoch。

SynthID 给了两种验证指标（`ValidationMetric` 枚举）：

- `TPR_AT_FPR`（默认）：直接衡量检测质量。
- `CROSS_ENTROPY`：衡量概率校准质量。

为什么默认选 `TPR_AT_FPR`？因为检测器的终极目标是「在控制假阳率的前提下尽量多抓水印」，`TPR@FPR=1%` 正是这个目标的原生度量；交叉熵只是代理，一个交叉熵低的模型不一定在 1% FPR 处召回率高。

#### 4.3.2 核心流程

`TPR@FPR=target_fpr` 的计算分三步：

1. 用当前参数给所有验证样本打分，分成正例分数集与负例分数集。
2. 在负例分数集上取第 \((100 - \text{target\_fpr}\times 100)\) 百分位作为阈值 \(\tau\)。例如 `target_fpr=0.01` 时取第 99 百分位——只有 1% 的干净文本分数会高于 \(\tau\)。

\[
\tau = \mathrm{Percentile}_{100(1-\text{target\_fpr})}\bigl(\{s_i : y_i=0\}\bigr)
\]

3. 统计正例中分数不低于 \(\tau\) 的比例，即为 TPR：

\[
\text{TPR} = \frac{1}{|P|}\sum_{i:\,y_i=1}\mathbf{1}[s_i \geq \tau]
\]

为了让「最优 = 最小」的 `argmin` 逻辑同时适用两种指标，`train` 用了一个巧妙的小技巧：**把 TPR 取负**当作 `val_loss`。于是：

- `TPR_AT_FPR` 模式：`val_loss = -TPR`，越小 → TPR 越大 → 越好。
- `CROSS_ENTROPY` 模式：`val_loss = cross_entropy`，越小 → 越好。

两种模式都满足「`val_loss` 越小越好」，所以最后统一用 `np.argmin(val_loss)` 选最优 epoch。

#### 4.3.3 源码精读

`tpr_at_fpr` 严格按上面三步实现：

[src/synthid_text/detector_bayesian.py:L475-L502](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L475-L502) —— 分正负索引、分 minibatch 打分、用负例分数的 `jnp.percentile` 求阈值、统计正例中不低于阈值的比例。

```python
  positive_idxs = w_true == 1
  negative_idxs = w_true == 0
  ...
  fpr_threshold = jnp.percentile(negative_scores, 100 - target_fpr * 100)
  return jnp.mean(positive_scores >= fpr_threshold)
```

注意 `target_fpr=0.01` 是默认参数（1% 假阳率）。

「负 TPR」技巧出现在 `update_fn_if_fpr_tpr`：

[src/synthid_text/detector_bayesian.py:L580-L589](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L580-L589) —— 把 TPR 取负作为验证损失，从而让「越大越好」的 TPR 转成「越小越好」的 `val_loss`，与交叉熵模式统一。

```python
  def update_fn_if_fpr_tpr(params):
    tpr_ = tpr_at_fpr(params=params, detector_inputs=(g_values_val, mask_val),
                      w_true=watermarked_val, minibatch_size=minibatch_size,
                      detector_module=detector_module)
    return -tpr_
```

`ValidationMetric` 枚举给出两种选择：

[src/synthid_text/detector_bayesian.py:L505-L510](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L505-L510) —— 训练时可选 `TPR_AT_FPR` 或 `CROSS_ENTROPY` 两种验证指标；`train` 的默认值是前者（见 L529）。

最后看**最优 epoch 的挑选与写回**，这是本讲实践任务的核心：

[src/synthid_text/detector_bayesian.py:L693-L700](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L693-L700) —— 训练结束后，把所有 epoch 的 `val_loss` 收集成数组，用 `np.argmin` 选最优 epoch，再把该 epoch 的参数快照重新包成 `{"params": ...}` 写回 `detector_module.params`。

```python
  detector_module.params = params                     # 先写最后一个 epoch（临时）
  val_loss = np.squeeze(
      np.array([history[epoch]["val_loss"] for epoch in range(1, epochs + 1)]))
  best_val_epoch = np.argmin(val_loss) + 1            # epoch 从 1 开始计数
  min_val_loss = val_loss[best_val_epoch - 1]
  print(f"Best val Epoch: {best_val_epoch}, min_val_loss: {min_val_loss}")
  detector_module.params = {"params": history[best_val_epoch]["params"]}  # 真正生效的写回
```

几个关键细节：

- `range(1, epochs + 1)` 是因为 `history` 的键是 1 开始的 epoch 号；`np.argmin(...) + 1` 把 0 基下标还原成 epoch 号。
- 第 693 行先把 `params`（最后一个 epoch）写回，但第 700 行又用最优 epoch 的参数**覆盖**了它。所以真正决定返回检测器行为的是**第 700 行**（最优 epoch），第 693 行只是临时赋值、最终被覆盖。
- 写回时用了 `history[best_val_epoch]["params"]`（4.2 里存的「剥掉外层键」的内层子树），再重新包成 `{"params": ...}`，恢复 `BayesianDetectorModule.score` 所期望的完整 PyTree 结构（`score` 会检查 `self.params is not None` 再 `apply`）。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：阅读 `train` 函数，说明 `best_val_epoch` 是如何选出的，以及为什么最终写回 `detector_module` 的是 `history[best_val_epoch]` 的参数。

**操作步骤**（源码阅读型）：

1. 打开 `src/synthid_text/detector_bayesian.py`，定位 `train` 的收尾段（L693–L700）。
2. 回答以下问题（写在你的学习笔记里）：
   - `val_loss` 数组每一项分别来自哪里？当 `validation_metric=TPR_AT_FPR` 时，这些值是正数还是负数？为什么 `argmin` 仍能选出「最好」的 epoch？
   - `best_val_epoch = np.argmin(val_loss) + 1` 里的 `+1` 是为了解决什么「差一」问题？
   - 第 693 行与第 700 行都给 `detector_module.params` 赋值，哪一行最终生效？为什么作者要先写一个「会被覆盖」的赋值？
3. 跟踪上层调用：看 `train_best_detector_given_g_values`（L941）如何读取 `best_detector.params`，确认它拿到的是最优 epoch 的参数。

**需要观察的现象 / 预期结果**：

- `val_loss` 来自 `history[epoch]["val_loss"]`，即每轮结束时 `update_fn` 返回的验证损失；`TPR_AT_FPR` 模式下它是 `-TPR`（负数），`argmin` 选最小（最负）→ TPR 最大 → 检测质量最好。
- `+1` 是因为 Python 的下标从 0 开始，而 `history` 的键从 1 开始。
- 第 700 行最终生效（用最优 epoch 参数写回），这就是**早停**：即便后续 epoch 继续降低训练损失，只要验证损失没有更小，就不会被采用。第 693 行可视为一种防御性赋值，保证 `detector_module.params` 非 `None`，但功能上被第 700 行覆盖。
- `train_best_detector_given_g_values` 里 `if min_val_loss < lowest_loss: best_detector = detector_module`，它拿到的 `detector_module.params` 正是第 700 行写入的最优 epoch 参数——所以整条链路最终输出的检测器，是「最优 l2_weight × 最优 epoch」双重选择的结果。

> 待本地验证：若在 GPU 上完整跑一遍 notebook 的训练单元（cell 24，默认 `validation_metric=TPR_AT_FPR`、`l2_weights=np.zeros((1,))`），可观察打印出的 `Best val Epoch: ...` 往往明显小于 `n_epochs`（如 100），直观印证早停在起作用。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `update_fn_if_fpr_tpr` 里的 `return -tpr_` 改成 `return tpr_`（不取负），会发生什么？
**答案**：`val_loss` 变成正值且「越大越好」，但最后的 `np.argmin` 仍按「越小越好」挑选，于是会选出 **TPR 最小**（最差）的 epoch，模型质量崩坏。这正说明「取负」不是为了语义，而是为了与 `CROSS_ENTROPY` 模式共用同一套 `argmin` 逻辑。

**练习 2**：`tpr_at_fpr` 内部用 Python `for` 循环逐 minibatch 打分，且没有加 `@jax.jit`。这会带来什么影响？
**答案**：相比 `validate_with_minibatches`（用了 `loss_fn_jitted_val`），`tpr_at_fpr` 每个 epoch 的验证开销更大、更慢。这是用「更贴合部署目标的指标」换「更多验证耗时」的取舍；这也是项目把 `epochs` 默认设成较大值（250）但实际调用处（如 `train_best_detector` 默认 `n_epochs=50`）往往调小的原因之一。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画出一次 `BayesianDetector.train_best_detector(...)` 调用的完整生命周期，并解释「双重最优选择」。

**步骤**：

1. 阅读 `train_best_detector`（L986）→ `process_raw_model_outputs`（L761，数据处理，u6-l3 会详讲）→ `train_best_detector_given_g_values`（L941）→ `train`（L513）这条调用链。
2. 在一张图上标出两层「最优选择」：
   - **内层（每个 l2_weight 之内）**：`train` 用 `np.argmin(val_loss)` 选 `best_val_epoch`，把该 epoch 参数写回 `detector_module`（L700）。
   - **外层（跨 l2_weight）**：`train_best_detector_given_g_values` 比较 每个 l2_weight 的 `min_val_loss`，取最小者作为 `best_detector`（L979–L981）。
3. 解释：notebook cell 24 传入 `l2_weights=np.zeros((1,))`（只含一个 0），这意味着外层搜索退化成「只跑一次、不比较」，此时返回的检测器等价于「`l2_weight=0` 的单次训练里的最优 epoch」。
4. 思考：如果想在水印检测任务上做正则强度调参，应该怎么改 `l2_weights`？默认值 `np.logspace(-3, -2, num=4)`（见 L954 / L1000）代表哪几个候选值？

**预期产出**：一段文字 + 一张两层嵌套的选择示意图，能清楚说明「最终拿到的检测器参数 = argmin_{l2_weight} ( argmin_{epoch} val_loss )」。

## 6. 本讲小结

- **复合损失**：`loss_fn = xentropy_loss + l2_batch_weight * sum(delta^2)`；交叉熵负责拟合数据，L2 负责防过拟合，且 L2 **只惩罚 `delta`**、不罚 `beta`。
- **纯函数 + jit**：`loss_fn` 是纯函数，`jax.value_and_grad` 对 `params` 求导，`@jax.jit update` 编译加速，`optax.adam` 完成一步 Adam 更新。
- **L2 均摊**：训练时 `l2_batch_weight = l2_weight / n_minibatches`，使一个 epoch 内累计的正则强度约等于 `l2_weight`；验证时 L2 权重置 0。
- **验证指标**：默认 `TPR_AT_FPR`（在 1% 假阳率下的召回率），用「负 TPR」技巧把「越大越好」转成「越小越好」，从而与 `CROSS_ENTROPY` 共用 `argmin` 选择逻辑。
- **早停**：训练结束后用 `np.argmin(val_loss)` 选 `best_val_epoch`，把该 epoch 的参数（而非最后一个 epoch）写回 `detector_module.params`，避免过拟合。
- **双重最优**：上层 `train_best_detector_given_g_values` 还会在 `l2_weights` 网格里取最优，最终检测器是「最优 l2_weight × 最优 epoch」的产物。

## 7. 下一步学习建议

- 下一讲 **u6-l3（数据处理与端到端检测 API）** 会补上本讲有意跳过的 `process_outputs_for_training` / `process_raw_model_outputs`（正负样本的截断、填充、mask 对齐），以及 `BayesianDetector.score` 的端到端用法，把「训练 → 打分」整条链路接通。
- 想加深对验证指标的理解，可对比 u5-l2 的 `mean_score`：那是**免训练**的打分，本讲的贝叶斯检测器是**需训练**的打分，两者输出的都是 \([0,1]\) 分数但来源完全不同。
- 若对 JAX 训练范式感兴趣，建议精读本讲 `train` 的闭包结构，并对照 `optax` 文档理解 `optimizer.init` / `optimizer.update` / `optax.apply_updates` 三件套的状态流转。
