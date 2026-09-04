# 内嵌 AdamW 后备轨道：与标准 AdamW 对比

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `Muon.step` 中 AdamW 后备分支（AdamW backup）的每一行代码：状态惰性初始化、`lerp_` 动量更新、偏差校正与 `scale`、解耦权重衰减。
2. 说明 `lerp_` 这种写法与常见的 `buf.mul_(β).add_(g, alpha=1-β)` 在数学上为什么等价。
3. 把手写 AdamW 与 `torch.optim.AdamW` 逐项对照，指出两者唯一的数学差异（ε 在偏差校正中的位置）以及若干工程差异。
4. 解释为什么 Moonlight 版 Muon 让内嵌 AdamW 与 Muon 分支**共用同一个 lr 和 wd**，这与上一讲的「更新 RMS 一致化」如何互相成就。

## 2. 前置知识

### 2.1 Adam / AdamW 三十分钟回顾

Adam 优化器为每个参数维护两个「动量」：

- 一阶动量 \( m_t \)：梯度的指数滑动平均（EMA），相当于「最近梯度加权和的方向」；
- 二阶动量 \( v_t \)：梯度平方的指数滑动平均，相当于「最近梯度幅度的大小」。

更新公式为「方向除以幅度」：

\[ \theta_t = \theta_{t-1} - \eta \cdot \frac{\hat{m}_t}{\sqrt{\hat{v}_t} + \varepsilon} \]

由于 \( m_t \)、\( v_t \) 都从零初始化，训练初期它们的数值系统性地偏小（比如第一步 \( m_1 = (1-\beta_1) g_1 \)，只是梯度的零头），因此要做**偏差校正**（bias correction）：

\[ \hat{m}_t = \frac{m_t}{1-\beta_1^t}, \qquad \hat{v}_t = \frac{v_t}{1-\beta_2^t} \]

AdamW 与 Adam 的区别在于**解耦权重衰减**：不是把衰减项混进梯度，而是直接把参数乘以 \( (1-\eta\lambda) \) 收缩一遍：

\[ \theta_t \leftarrow \theta_{t-1} \cdot (1-\eta\lambda) - \eta \cdot \frac{\hat{m}_t}{\sqrt{\hat{v}_t} + \varepsilon} \]

这样衰减强度只由 \( \eta\lambda \) 决定，不会被自适应学习率扭曲。

### 2.2 `lerp_` 是什么

`Tensor.lerp_(target, weight)` 是 PyTorch 的原地线性插值：

```python
# buf.lerp_(g, w) 等价于：buf = buf + w * (g - buf)
```

展开就是 \( \text{buf} \leftarrow (1-w)\cdot\text{buf} + w\cdot g \)。令 \( w = 1-\beta \)，就得到 EMA 的标准形式 \( \text{buf} \leftarrow \beta\cdot\text{buf} + (1-\beta)\cdot g \)。一次函数调用完成「乘旧项、加新项」，比 `mul_` + `add_` 两步少一次内核开销。新版 PyTorch 自己的 AdamW 单张量实现路径也用 `exp_avg.lerp_(grad, 1 - beta1)` 维护一阶动量（可用第 4.4 节的实践验证，随版本可能变化）。

### 2.3 承接前面几讲

- **u2-l1** 划定了参数分组：玩具 Qwen2 的 110 个参数中，84 个矩阵参数走 Muon 分支，其余 26 个（1 个二维嵌入矩阵 + 25 个一维 norm 向量）打上 `use_muon = False` 标记，走本讲的 AdamW 分支。一维向量没有「正交化」的定义，嵌入矩阵的梯度按行稀疏、Newton-Schulz 又开销大，所以留给 AdamW。
- **u2-l4** 讲了 Muon 分支的更新 RMS 一致化：以 \( 0.2\sqrt{\max(A,B)} \) 放大学习率，把矩阵参数的更新 RMS 统一到 \( 0.2\eta \)。README 里把这个技术表述为 "keep a consistent update RMS across different **matrix and non-matrix parameters**"（[README.md:L27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L27)）——注意「非矩阵参数」的更新 RMS 正是由**本讲的 AdamW 分支**决定的。换句话说，AdamW 分支不是配角，它是整个「单一学习率」设计要对齐的参照系。本讲就来把这个参照系本身看清楚。

## 3. 本讲源码地图

本讲涉及的代码全部集中在唯一源码文件 `examples/toy_train.py` 中：

| 代码位置 | 作用 |
| --- | --- |
| [examples/toy_train.py:L92-L104](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L92-L104) | Muon 类的文档字符串，列出了（部分并不存在的）内嵌 AdamW 参数 |
| [examples/toy_train.py:L106-L140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L106-L140) | `Muon.__init__`：默认超参、参数合并、`use_muon` 标记 |
| [examples/toy_train.py:L205-L237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L205-L237) | **本讲主角**：`step()` 内的 AdamW backup 分支 |
| [examples/toy_train.py:L287-L291](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L291) | `get_optimizer` 中作为基线的 `torch.optim.AdamW`，对照的「标准答案」 |

对照对象 `torch.optim.AdamW` 不在本仓库内，属于 PyTorch 标准库，本讲会以它为镜子逐项对照。

## 4. 核心概念与源码讲解

### 4.1 后备轨道的入口：谁在走这条轨道

#### 4.1.1 概念说明

`Muon.step` 对 `param_groups` 里的每个 group 做两轮遍历：第一轮挑出 `use_muon=True` 的参数做 Newton-Schulz 正交化更新（u2-l2、u2-l3 的内容）；第二轮挑出 `use_muon=False` 的参数，用**手写的 AdamW** 更新。这就是源码里注释 banner 写的 "AdamW backup"——「后备轨道」。

这条轨道存在的原因在 u2-l1 已详细讨论过：不是所有参数都能（或应该）被正交化。这里再从「训练整体」的角度补一句：一个完整的优化器必须覆盖模型的**每一个**参数，否则嵌入和 norm 向量就成了「冻住的死角」。Moonlight 的选择是不把这些问题参数排除出优化器，而是让 Muon 类内部长出一个标准 AdamW 来接管它们——两个分支共用同一个 `param_group`、同一个 lr 和 wd，由同一次 `optimizer.step()` 驱动。

#### 4.1.2 核心流程

`step()` 中 AdamW 分支的执行顺序：

```text
遍历每个 param_group：
  1. 筛出 use_muon == False 的参数子集
  2. 读取超参：lr（与 Muon 分支共用）、adamw_betas、adamw_eps、wd（与 Muon 分支共用）
  3. 对每个参数 p：
     a. p.grad 为 None → 跳过
     b. 首次遇到 → 惰性初始化 state：step=0, moment1=0, moment2=0
     c. step 加一
     d. lerp_ 更新一阶、二阶动量
     e. 用动量比值重写 g
     f. 计算偏差校正 scale
     g. 解耦权重衰减 + 应用更新
```

#### 4.1.3 源码精读

先看入口处的参数筛选与超参读取：

[examples/toy_train.py:L205-L213](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L205-L213) 中，第 209 行用 `not self.state[p]["use_muon"]` 从**同一个** `group["params"]` 里筛出后备参数——与第 168 行 Muon 分支的筛选条件恰好互补（u2-l1 讲过这种「取反保证互斥完备」的写法）。接着四行读取超参：

```python
params = [p for p in group["params"] if not self.state[p]["use_muon"]]
lr = group['lr']
beta1, beta2 = group["adamw_betas"]
eps = group["adamw_eps"]
weight_decay = group["wd"]
```

注意三个超参的来源分两类：

- `lr` 与 `weight_decay` 读的是 group 的公共字段——**与 Muon 分支第 170-171 行读的是同一个值**。训练循环里 `lr_scheduler.step()` 修改的就是 `group["lr"]`（u1-l3），所以调度器一抬手，两条分支的学习率同时变化。
- `beta1, beta2, eps` 读的是 AdamW 专属字段，来自构造时的默认值。

再看默认超参从哪来：

[examples/toy_train.py:L106-L117](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L106-L117) 的构造签名里，`adamw_betas=(0.9, 0.95)`、`adamw_eps=1e-8`。而 [examples/toy_train.py:L287-L291](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L291) 中基线 `torch.optim.AdamW` 也用的是 `betas=(0.9, 0.95)`（eps 默认同为 1e-8）。这不是巧合——git 历史里有一条提交专门把 `adamw_betas` 默认值从 `(0.95, 0.95)` 改成 `(0.9, 0.95)`，提交说明就是 "update chained adamw default betas to avoid confusiong"（提交 `5afcb69`，2025-03-28）：让内嵌 AdamW 与对照基线的超参严格对齐，避免「对比实验里两边 β 不一样」这种混淆。

还有一个值得留意的细节：[examples/toy_train.py:L92-L104](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L92-L104) 的文档字符串列出了 `adamw_lr`、`adamw_wd` 等参数说明，但真正的签名（L106-L117）里**并不存在**这两个参数——文档字符串与实现不同步。实现的真实选择是：内嵌 AdamW 没有自己独立的 lr 和 wd，一律用 group 的公共值。为什么敢于这样合并，见 4.4.3 节。

`use_muon = False` 标记的设置在构造函数里完成：

[examples/toy_train.py:L138-L140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L138-L140) 遍历 `adamw_params`，为每个参数写 `state[p]["use_muon"] = False`。配合 [examples/toy_train.py:L129-L132](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L129-L132) 把两组参数合并进**唯一一个** param_group，这就是「一个优化器、两条分支、一次 step」的全部机制。

#### 4.1.4 代码实践

用解释器直接构造一个只含后备参数的 Muon，确认筛选逻辑（不启动训练）：

1. **实践目标**：验证 `muon_params=[]` 时 Muon 退化成纯 AdamW，`step()` 只走后备分支。
2. **操作步骤**（示例代码，在仓库根目录运行；依赖已按 u1-l2 安装）：

   ```python
   import sys, torch
   sys.path.insert(0, "examples")
   from toy_train import Muon  # 模块级 import 不会触发训练（有 __main__ 守护）

   p = torch.nn.Parameter(torch.randn(8, 8))
   opt = Muon(lr=1e-3, wd=0.1, muon_params=[], adamw_params=[p])
   print(opt.state[p]["use_muon"])          # 预期 False
   print(len(opt.param_groups[0]["params"]))  # 预期 1
   ```

   注意 `muon_params=[]` 必须显式传空列表——传 `None` 会在 `list(muon_params)` 处抛 `TypeError`。
3. **需要观察的现象**：`use_muon` 为 `False`；两组参数确实合进了同一个 `param_groups[0]`。
4. **预期结果**：输出 `False` 和 `1`。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：玩具 Qwen2（`hidden_size=1024`）里走 AdamW 后备分支的参数有多少个？分别是什么？

答案：26 个——1 个二维嵌入矩阵（`model.embed_tokens.weight`，形状 151936×1024）加 25 个一维 norm 向量（12 层各 1 个 input_layernorm + 1 个 post_attention_layernorm，再加 1 个最终的 model.norm）。依据见 u2-l1 的分组统计。

**练习 2**：为什么嵌入矩阵明明是二维的，却必须走 AdamW 分支？

答案：两个原因（u2-l1 详述）：其一，语言建模每个 step 只有少量 token 参与输出，嵌入的梯度按行稀疏，正交化会把稀疏结构「抹平」成稠密的满秩更新，语义不合适；其二，151936×1024 的矩阵做 Newton-Schulz 每步要多次大矩阵乘法，开销远超普通投影层。AdamW 的逐元素自适应缩放对稀疏梯度天然友好。

**练习 3**：文档字符串里的 `adamw_lr`、`adamw_wd` 在签名里找不到，这说明什么？

答案：文档字符串是上游用法的残留，实现已改为共享 group 的 `lr` 与 `wd`。这是 Moonlight 版 Muon 的一个有意的工程选择（动机见 4.4.3），也提醒我们：读源码时以签名为准，文档字符串可能滞后。

### 4.2 状态初始化与 `lerp_` 动量更新

#### 4.2.1 概念说明

AdamW 需要为每个参数维护三个状态：步数计数器 `step`、一阶动量 `moment1`（梯度的 EMA）、二阶动量 `moment2`（梯度平方的 EMA）。本实现采用**惰性初始化**：第一次对该参数调用 `step()` 时才创建状态，零初始化。这与 PyTorch 官方优化器的做法一致——优化器构造时（u1-l3 提到，甚至在模型搬上 GPU 之前）状态尚不存在，首次更新时才按当时梯度的形状与设备创建，避免了「状态与参数设备不一致」的坑。

动量更新用 `lerp_` 完成。回忆 2.2 节：

\[ \text{buf} \leftarrow \beta\,\text{buf} + (1-\beta)\,g \]

一阶动量用 \( \beta_1 \)，二阶动量的插值目标是 \( g^2 \)、用 \( \beta_2 \)。零初始化 + 指数加权意味着**初期动量显著偏小**：第 1 步的 \( m_1 = (1-\beta_1)g_1 = 0.1\,g_1 \)，只有真实梯度的十分之一——这正是下一节偏差校正要修复的问题。

#### 4.2.2 核心流程

单个参数的一次状态更新（数学形式）：

\[ m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t \]
\[ v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2 \]

用伪代码描述源码行为：

```text
state 里没有 "step" 键（首次）:
    step = 0, moment1 = zeros_like(g), moment2 = zeros_like(g)
step += 1
moment1.lerp_(g,        1 - beta1)   # 即 m ← β1·m + (1-β1)·g
moment2.lerp_(g.square(), 1 - beta2)  # 即 v ← β2·v + (1-β2)·g²
```

#### 4.2.3 源码精读

[examples/toy_train.py:L215-L229](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L215-L229) 完成了「跳过无梯度参数 + 惰性建状态 + 双动量更新」：

```python
for p in params:
    g = p.grad
    if g is None:
        continue
    state = self.state[p]
    if "step" not in state:
        state["step"] = 0
        state["moment1"] = torch.zeros_like(g)
        state["moment2"] = torch.zeros_like(g)
    state["step"] += 1
    step = state["step"]
    buf1 = state["moment1"]
    buf2 = state["moment2"]
    buf1.lerp_(g, 1 - beta1)
    buf2.lerp_(g.square(), 1 - beta2)
```

几个值得注意的细节：

- 第 217 行 `if g is None: continue` 与 Muon 分支（L178-179）同款：没有梯度的参数（比如被冻结的层）整个跳过，状态都不会创建。PyTorch 官方优化器同样跳过 `grad is None` 的参数。
- `state["step"]` 是 Python int，先加一再使用——所以第一次更新时 `step` 已是 1，与 PyTorch 官方实现一致（PyTorch 为了支持 `foreach`/`capturable` 把 step 存成 tensor，这里玩具实现用 int 即可）。
- `moment2` 的插值目标是 `g.square()`：先平方再插值，等价于「对梯度平方做 EMA」，这是 Adam 的二阶矩定义，不是「对梯度做 EMA 再平方」。

#### 4.2.4 代码实践

验证 `lerp_` 与教科书 EMA 写法数值一致（示例代码）：

1. **实践目标**：确认 `buf.lerp_(g, 1-beta)` 与 `buf.mul_(beta).add_(g, alpha=1-beta)` 逐元素相同。
2. **操作步骤**：

   ```python
   import torch
   torch.manual_seed(0)
   beta = 0.9
   g  = torch.randn(1000, dtype=torch.float64)
   b1 = torch.zeros(1000, dtype=torch.float64)
   b2 = b1.clone()
   b1.lerp_(g, 1 - beta)
   b2.mul_(beta).add_(g, alpha=1 - beta)
   print((b1 - b2).abs().max())   # 预期为 0
   ```

3. **需要观察的现象**：最大差为 0（两种写法仅调用方式不同）。
4. **预期结果**：打印 `0.0`（float64 下精确相等；待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：代数证明 `buf.lerp_(g, 1-beta)` 等于 `beta*buf + (1-beta)*g`。

答案：`lerp` 的定义是 `buf + w*(target - buf)`，代入 `w = 1-beta` 得 `buf + (1-beta)g - (1-beta)buf = beta*buf + (1-beta)*g`。∎

**练习 2**：取 `beta1=0.9`，第 1 步和第 2 步的 `moment1` 分别是多少（设梯度依次为 \( g_1, g_2 \)）？哪个更「接近」真实梯度？

答案：\( m_1 = 0.1 g_1 \)；\( m_2 = 0.9 \times 0.1 g_1 + 0.1 g_2 = 0.09 g_1 + 0.1 g_2 \)。两者都只有真实梯度的约 10% 量级——零初始化的 EMA 在 warmup 期系统性偏小，且 \( m_2/m_1 \) 的「有效窗口」远未达到稳态，必须靠偏差校正补偿（下一节）。

**练习 3**：如果把 `moment2` 的更新误写成 `buf2.lerp_(g, 1-beta2).square()`（先插值再平方），会发生什么？

答案：语义完全变了——变成「梯度 EMA 的平方」而非「梯度平方的 EMA」。由 Jensen 不等式 \( (E[g])^2 \le E[g^2] \)，分母会系统性偏小，更新量偏大，且符号信息泄漏进幅度（正负梯度会互相抵消后才平方）。这正是手写优化器最容易犯的一类错误。

### 4.3 偏差校正与 `scale`：一次合并的技巧

#### 4.3.1 概念说明

2.1 节说过，零初始化的动量在早期偏小，需要除以 \( 1-\beta^t \) 校正。标准 AdamW 的写法是把校正分别施加到分子（\( \hat{m} \)）和分母（\( \sqrt{\hat{v}} \)）上。本实现没有分别校正，而是把两个校正系数**合并成一个标量 `scale`**：

\[ \text{scale} = \frac{1-\beta_1^t}{\sqrt{1-\beta_2^t}} \]

然后更新时用 `lr / scale` 作为有效步长。这样做的好处是省去对张量做两次逐元素除法——`scale` 是一个 Python 标量，校正被「折叠」进标量步长里，更新主路径只需一次张量除法（L231）加一次 `add_`（L237）。

代价是一个极小的数学近似：ε 没有跟着偏差校正一起缩放。下一节会精确推导这个差异，这里先记住结论：**在 ε 可忽略的意义下，手写式与标准 AdamW 严格等价**。

#### 4.3.2 核心流程

先看更新方向的重写（L231）：

\[ u = \frac{m_t}{\varepsilon + \sqrt{v_t}} \]

注意 ε 加在 \( \sqrt{v_t} \) 上（未校正的二阶动量开方）。再看校正与应用（L233-L237）：

\[ \theta_t = \theta_{t-1}(1 - \eta\lambda) - \frac{\eta}{\text{scale}} \cdot u \]

把它与标准 AdamW 逐项对齐（记 \( b_1 = 1-\beta_1^t \)，\( b_2 = 1-\beta_2^t \)）：

\[ \frac{\eta}{\text{scale}}\cdot\frac{m}{\varepsilon+\sqrt{v}} = \eta\,\frac{\sqrt{b_2}}{b_1}\cdot\frac{m}{\varepsilon+\sqrt{v}} = \eta\,\frac{\hat{m}_t}{\sqrt{v}/\sqrt{b_2} + \varepsilon/\sqrt{b_2}} \]

而标准 AdamW（PyTorch 实现）为：

\[ \eta\,\frac{\hat{m}_t}{\sqrt{v}/\sqrt{b_2} + \varepsilon} \]

两者分子、主分母完全相同，**唯一差别是 ε 项**：手写版等效于 \( \varepsilon/\sqrt{b_2} \)，PyTorch 是 \( \varepsilon \)。训练初期 \( \sqrt{b_2} < 1 \)（如 \( t=1, \beta_2=0.95 \) 时 \( \sqrt{b_2}=\sqrt{0.05}\approx 0.224 \)），手写版的有效 ε 约为 PyTorch 的 4.5 倍；随着 \( t \) 增大 \( b_2 \to 1 \)，差异消失。由于默认 \( \varepsilon = 10^{-8} \) 远小于典型 \( \sqrt{v} \)，这个差异在默认超参下数值上不可察觉——第 5 节的综合实践会先证明「几乎相等」，再故意调大 ε 让差异显形。

再感受一下 `scale` 的量级：\( t=1 \) 时 \( \text{scale} = 0.1/\sqrt{0.05} \approx 0.447 \)，即有效步长 \( \eta/\text{scale} \approx 2.24\eta \)——早期更新被放大两倍多，补偿零初始化动量的偏小。若忘了做校正（相当于 `scale=1`），前几百步的有效学习率会偏小，warmup 变相拖长。

#### 4.3.3 源码精读

[examples/toy_train.py:L231-L237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L231-L237) 是后备分支的最后七行，完成「求方向、算校正、衰减、更新」：

```python
g = buf1 / (eps + buf2.sqrt())

bias_correction1 = 1 - beta1**step
bias_correction2 = 1 - beta2**step
scale = bias_correction1 / bias_correction2**0.5
p.data.mul_(1 - lr * weight_decay)
p.data.add_(g, alpha=-lr / scale)
```

逐行说明：

- L231：新的 `g` 是一个**新分配的张量**（不是原地改 buf1），方向为「一阶动量 / (ε + 二阶动量开方)」，这就是 Adam 著名的逐元素自适应缩放。
- L233-L235：两个偏差校正系数合并为标量 `scale`。
- L236：解耦权重衰减——直接 `p.data.mul_(1 - lr*weight_decay)`，与 Muon 分支第 200 行的写法一字不差（u2-l3 讲过：正交化会抹掉幅度信息，所以衰减必须解耦；对 AdamW 分支而言这是 AdamW 的标准定义）。
- L237：以 `alpha=-lr/scale` 应用负更新。

注意 L236-L237 与 Muon 分支 L200-L203 的平行结构：两边都是「先乘衰减、再加更新」，衰减都用**原始 lr**、步长各自定（Muon 分支用 `adjusted_lr`，本分支用 `lr/scale`）。这份对称性正是 u2-l4「更新用调整后学习率、衰减用原始学习率」设计在两条分支上的统一体现。

#### 4.3.4 代码实践

手动复算一步，验证对 `scale` 的理解（示例代码）：

1. **实践目标**：用纯张量运算手工复现 L228-L237 的一次更新，与直接调用 `Muon.step` 的结果比对。
2. **操作步骤**：

   ```python
   import sys, torch
   sys.path.insert(0, "examples")
   from toy_train import Muon

   torch.manual_seed(0)
   beta1, beta2, eps, lr, wd = 0.9, 0.95, 1e-8, 1e-3, 0.1

   p = torch.nn.Parameter(torch.randn(64, dtype=torch.float64))
   p0 = p.data.clone()
   opt = Muon(lr=lr, wd=wd, muon_params=[], adamw_params=[p],
              adamw_betas=(beta1, beta2), adamw_eps=eps)
   g = torch.randn(64, dtype=torch.float64)
   p.grad = g.clone()
   opt.step()

   # 手工复现（首步 step=1）
   m, v = (1-beta1)*g, (1-beta2)*g**2
   u = m / (eps + v.sqrt())
   scale = (1-beta1**1) / (1-beta2**1)**0.5
   expect = p0*(1 - lr*wd) - lr/scale * u
   print((p.data - expect).abs().max())
   ```

3. **需要观察的现象**：最大差为 0——说明 `lerp_`、`scale`、衰减顺序都被正确复现。
4. **预期结果**：打印 `0.0`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：计算 `beta1=0.9, beta2=0.95` 时第 1 步与第 100 步的 `scale`，并解释变化。

答案：\( t=1 \)：\( (1-0.9)/(1-0.95)^{1/2} = 0.1/\sqrt{0.05} \approx 0.447 \)，有效步长 \( \eta/0.447 \approx 2.24\eta \)。\( t=100 \)：\( (1-0.9^{100})/\sqrt{1-0.95^{100}} \approx 1/\sqrt{0.994} \approx 1.003 \)，有效步长几乎等于 \( \eta \)。解释：动量从零启动，早期估计偏小需要放大补偿；\( \beta^t \) 指数衰减到 0 后校正自动退场。

**练习 2**：`scale` 为什么不是 \( b_1 \cdot \sqrt{b_2} \) 或别的组合？从量纲推导。

答案：更新量纲为 \( \eta \cdot \frac{m/b_1}{\sqrt{v/b_2}} = \eta \cdot \frac{\sqrt{b_2}}{b_1} \cdot \frac{m}{\sqrt{v}} \)，要把未校正比值 \( m/\sqrt{v} \) 换算成校正值，需乘 \( \sqrt{b_2}/b_1 \)，即除以 \( b_1/\sqrt{b_2} \)——正是源码的 `scale`。

**练习 3**：把 L231 的括号去掉写成 `g = buf1 / eps + buf2.sqrt()` 会怎样？

答案：运算优先级完全改变，变成「\( m/\varepsilon + \sqrt{v} \)」——一个天文数字加一个小量，更新方向退化为符号函数 scaled，训练立即崩溃。这也是为什么读优化器源码时要特别留意 ε 的位置（下一节的主题）。

### 4.4 与 `torch.optim.AdamW` 逐项对照

#### 4.4.1 概念说明：一致之处

把两个实现并排放，默认配置下它们**几乎就是同一个算法**。一致点清单：

| 维度 | 手写分支（toy_train.py） | `torch.optim.AdamW` 基线（L289-291） | 是否一致 |
| --- | --- | --- | --- |
| betas 默认 | (0.9, 0.95)（L115） | (0.9, 0.95)（L290，显式传入） | 一致 |
| eps 默认 | 1e-8（L116） | 1e-8（未传，取默认） | 一致 |
| 一阶动量 | `lerp_` EMA（L228） | EMA（新版同样用 `lerp_`，待本地验证） | 一致 |
| 二阶动量 | 对 \( g^2 \) 做 EMA（L229） | 对 \( g^2 \) 做 EMA | 一致 |
| 偏差校正 | 折叠进标量 `scale`（L233-L235） | 分别校正 \( \hat{m}, \hat{v} \) | 数学等价（见差异 1） |
| 权重衰减 | 解耦 `p.mul_(1-lr*wd)`，更新前执行（L236） | 解耦，同样先衰减后更新 | 一致 |
| lr 来源 | `group['lr']`，随调度器变化（L210） | `group['lr']`，随调度器变化 | 一致 |
| 无梯度参数 | 跳过（L217-218） | 跳过 | 一致 |
| step 计数 | 首次 0→1（L224） | 首次 0→1 | 一致 |

其中 betas 的一致还有一条 git 考古证据：提交 `5afcb69`（2025-03-28）专门把默认 `adamw_betas` 从 `(0.95, 0.95)` 改为 `(0.9, 0.95)`，提交说明即为避免与基线混淆。

#### 4.4.2 概念说明：差异清单

1. **ε 相对偏差校正的位置（唯一数学差异）**：手写版等效于 \( \varepsilon/\sqrt{b_2} \)，PyTorch 是 \( \varepsilon \)（推导见 4.3.2）。默认 `eps=1e-8` 下数值不可察觉；ε 调大后差异显形，且随步数增大自行消失（\( b_2 \to 1 \)）。
2. **没有独立超参**：手写分支与 Muon 分支共享 `lr`、`wd`，不支持给 AdamW 单独设学习率（文档字符串里的 `adamw_lr`/`adamw_wd` 并不存在于签名）。标准 `torch.optim.AdamW` 的 lr/wd 就属于自己的 param_group。
3. **功能裁剪**：手写版没有 `amsgrad`、`maximize`、`capturable`、`foreach`/`fused` 等选项，是固定形态的最小实现；`state["step"]` 是 Python int 而非 tensor。
4. **单 group 双分支**：手写分支活在 Muon 的 `param_groups[0]` 里，与 Muon 分支同生共死——一次 `step()` 同时更新两组参数；而 `torch.optim.AdamW` 是独立优化器。

#### 4.4.3 为什么敢共用一个 lr：承接 u2-l4 的闭环

这是本讲最想传递的设计观点。Muon 分支的更新 RMS 是形状相关的 \( 1/\sqrt{\max(A,B)} \)（u2-l4），而 AdamW 的逐元素归一让更新 RMS 天然形状无关、量级约为 \( \eta \)。如果不做处理，一个 lr 无法同时伺候两条分支——要么矩阵参数更新太小，要么嵌入更新太大。

Moonlight 的解法分两半，各在一条分支上：

- Muon 分支：`adjust_lr_for_muon` 乘 \( 0.2\sqrt{\max(A,B)} \)，把矩阵更新 RMS 抬到 \( 0.2\eta \)（0.2 是对齐 AdamW 分支实际水平的经验常数，u2-l4）；
- AdamW 分支：保持标准形态，作为「基准音」。

于是两条分支的更新 RMS 同为 \( 0.2\eta \) 量级，一个 `group['lr']`（加上一个调度器）就能驱动整个优化器。README 把这表述为 "keep a consistent update RMS across different matrix and non-matrix parameters through parameter-wise update scale adjustments"（[README.md:L27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L27)），并称其「显著增强训练稳定性」。文档字符串中消失的 `adamw_lr`/`adamw_wd` 正是这个设计的注脚：**当更新尺度已经一致化，就不再需要第二组学习率**。

#### 4.4.4 代码实践

本讲的主实践放在第 5 节综合实践（逐步对拍两个实现）。这里先做一个一分钟的「源码考古」小实践，验证 4.4.1 表格中关于 PyTorch 内部实现的表述：

1. **实践目标**：确认本地 PyTorch 的 AdamW 单张量路径如何写一阶动量更新与 ε。
2. **操作步骤**（示例代码）：

   ```python
   import inspect
   import torch.optim.adamw as m

   src = inspect.getsource(m._single_tensor_adamw)
   for line in src.splitlines():
       if "lerp_" in line or "addcmul_" in line or "eps" in line:
           print(line.strip())
   ```

3. **需要观察的现象**：是否出现 `exp_avg.lerp_(grad, 1 - beta1)`；ε 是加在「已做偏差校正的分母」上（形如 `denom = (exp_avg_sq.sqrt() / bias_correction2_sqrt).add_(eps)`）还是别处。
4. **预期结果**：新版 PyTorch 单张量路径用 `lerp_` 维护一阶动量，ε 加在校正后的分母上——与 4.3.2 的推导呼应。不同 torch 版本实现路径可能不同，以本地输出为准。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：两个实现对同一参数、同一梯度序列产生的更新，在什么条件下**严格**相等？

答案：当 \( \varepsilon \to 0 \)（或 \( \varepsilon \ll \sqrt{v_t} \) 使 ε 项可忽略）时，`scale` 折叠式与分别校正式严格相等；再配上相同 betas、相同 lr/wd、相同衰减顺序，两者逐步更新完全一致。ε 有限时手写版有效 ε 为 \( \varepsilon/\sqrt{b_2} \)，仅此一项不同。

**练习 2**：为什么手写版可以不做 `amsgrad`、`capturable` 这些功能，而 PyTorch 必须做？

答案：定位不同。toy_train.py 是论文思想的可运行演示，首要目标是**可读**——约 20 行讲清 AdamW 主干即可；PyTorch 要服务所有用户（分布式、CUDA graph 捕获、混合精度、性能优化），必须参数化这些行为。Moonlight 的工业级分布式版本在 Megatron-LM PR #1428 中（u3-l4 会展开），玩具实现无需背这些包袱。

**练习 3**：如果想让手写分支与 PyTorch **完全**逐位一致，改哪一行？

答案：把 L231 的 `g = buf1 / (eps + buf2.sqrt())` 改为除以「先校正再 ε」的分母，如 `g = buf1 / (buf2.sqrt() / bias_correction2**0.5 + eps)`，同时 L237 的步长改为 `alpha=-lr / bias_correction1`（去掉 `sqrt(b2)` 因子）——即放弃标量 `scale` 折叠，回到分别校正。默认 ε 下没有必要。

## 5. 综合实践：逐步对拍手写 AdamW 与 `torch.optim.AdamW`

这是本讲的主实践，把 4.2-4.4 的知识串成一条线：**构造完全相同的一串随机梯度，分别喂给 `Muon` 的后备分支与 `torch.optim.AdamW`，逐步对比参数，回答「两者是否数学等价、差异来自哪里」**。

### 5.1 实践目标

1. 亲手复现「两实现几乎相等」这一事实，并量化差距。
2. 通过放大 ε 让隐藏的差异显形，定位到「ε 相对偏差校正的位置」。
3. 观察差异随步数收敛（\( b_2 \to 1 \)），验证 4.3.2 的推导。

### 5.2 操作步骤

在仓库根目录创建 `compare_adamw.py`（示例代码，注意是新建文件，不改动源码）：

```python
import sys
import torch

sys.path.insert(0, "examples")
from toy_train import Muon  # 只导入类，不触发训练

LR, WD, STEPS = 1e-3, 0.1, 200

def duel(eps, steps=STEPS):
    """同一串梯度下对拍：返回每步后两参数的最大绝对差。"""
    torch.manual_seed(0)
    gs = [torch.randn(1024, dtype=torch.float64) for _ in range(steps)]

    p_hand = torch.nn.Parameter(torch.randn(1024, dtype=torch.float64))
    torch.manual_seed(0)                      # 保证两边初始值相同
    p_ref = torch.nn.Parameter(torch.randn(1024, dtype=torch.float64))

    opt_hand = Muon(lr=LR, wd=WD, muon_params=[], adamw_params=[p_hand],
                    adamw_betas=(0.9, 0.95), adamw_eps=eps)
    opt_ref = torch.optim.AdamW([p_ref], lr=LR, weight_decay=WD,
                                betas=(0.9, 0.95), eps=eps)

    gaps = []
    for g in gs:
        p_hand.grad, p_ref.grad = g.clone(), g.clone()
        opt_hand.step()
        opt_ref.step()
        gaps.append((p_hand.data - p_ref.data).abs().max().item())
    return gaps

g8 = duel(1e-8)
g2 = duel(1e-2)
for t in (1, 2, 10, 100, 200):
    print(f"step {t:>3}: eps=1e-8 gap={g8[t-1]:.3e}   eps=1e-2 gap={g2[t-1]:.3e}")
```

运行：`python compare_adamw.py`（依赖已按 u1-l2 安装；用 float64 是为了让小于 float32 精度的差异也能显示出来）。

### 5.3 需要观察的现象

1. `eps=1e-8` 一列：gap 极小（预期在 1e-15 ~ 1e-10 量级，非零但不增长成有意义的差别）——两实现在默认超参下 practically 等价。
2. `eps=1e-2` 一列：gap 在最初几步就显著大于 1e-8 列；且**相邻步的 gap 增量**随步数增大而缩小（因为 \( \sqrt{b_2} \to 1 \)，两边的有效 ε 趋同；累计 gap 不会自己变小，看增量才清楚）。
3. 若把 `duel` 中手写一侧换成 4.3.4 的手工复现，gap 恒为 0——证明差异只来自 ε 位置，而非 `lerp_` 或 `scale` 的写法。

### 5.4 预期结果与结论

- 结论一：除「ε 相对偏差校正的位置」外，手写分支与 `torch.optim.AdamW` 逐步数学等价（`lerp_`、`scale`、解耦衰减、step 计数全部对得上）。
- 结论二：默认 `eps=1e-8` 下该差异比 float32 舍入误差还小，训练层面完全无感——所以把它称作「后备轨道的 AdamW」名副其实。
- 具体数值待本地验证；若你观察到的量级与上述预期不符，优先检查两侧初始参数是否真的相同（脚本里两次 `manual_seed(0)` 就是为此）。

## 6. 本讲小结

- `Muon.step` 的后半段是一条完整的**手写 AdamW 后备轨道**（[L205-L237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L205-L237)）：`step/moment1/moment2` 惰性初始化，`lerp_` 维护双动量，标量 `scale = b1/√b2` 把两次偏差校正折叠进有效步长 `lr/scale`，最后以解耦方式先衰减后更新。
- `lerp_` 写法与 `mul_ + add_` 数学等价，且新版 PyTorch 官方 AdamW 内部同款。
- 与 `torch.optim.AdamW` 逐项对照：默认 betas (0.9, 0.95)、eps 1e-8、解耦衰减、step 计数全部对齐（betas 对齐甚至有专门提交 `5afcb69` 佐证）；唯一数学差异是 ε 位置——手写版有效 ε 为 \( \varepsilon/\sqrt{b_2} \)，默认超参下不可察觉。
- 手写分支**没有独立 lr/wd**（文档字符串里的 `adamw_lr`/`adamw_wd` 是残留），与 Muon 分支共用 group 超参——这之所以可行，是因为 u2-l4 的 \( 0.2\sqrt{\max(A,B)} \) 缩放把两条分支的更新 RMS 对齐到了同一量级（README L27 的 matrix/non-matrix 一致 RMS）。
- 本讲补完了 Muon 优化器的最后一块拼图：至此 u2 单元从参数分组、正交化、动量与衰减、RMS 缩放到后备 AdamW，`toy_train.py` 的优化器已全部读完。

## 7. 下一步学习建议

- **进入 u3 单元做实验**：u3-l1 将基于本讲与 u2-l4 的「更新 RMS 一致」认知，设计 Muon vs AdamW 的公平对比实验（学习率扫描、loss 曲线）——你会同时用到本讲的对拍方法论和 u1-l3 搭建的观测仪表盘。
- **读 PyTorch 源码巩固**：对照 `torch/optim/adamw.py` 的 `_single_tensor_adamw` 与本讲手写版，体会「教学实现」与「工业实现」在等价数学下的工程差异（foreach、fused、capturable）。
- **预习分布式方向**：u3-l4 会讨论 ZeRO-1 式的分布式 Muon——届时回想本讲的一个细节：`moment1`/`moment2` 与 Muon 的 `momentum_buffer` 都是按参数形状创建的状态张量，ZeRO-1 分片分的正是它们；理解了状态在哪里，才能理解分布式优化器在通信什么。
