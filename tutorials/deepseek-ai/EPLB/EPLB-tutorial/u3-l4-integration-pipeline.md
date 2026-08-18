# 工程集成：从负载统计到专家权重重排

## 1. 本讲目标

前几讲我们一直在"算法内部"看 EPLB：怎么复制、怎么装箱、怎么合成映射。本讲把镜头拉远，看 EPLB 在一个真实训练/推理系统里的完整位置。学完本讲，你应该能够：

1. 画出「框架统计 → 负载估计 → `rebalance_experts` → 应用映射」的四段闭环流水线，并说清 EPLB 只覆盖其中哪一段、为什么（README 有明确的边界声明）。
2. 用滑动平均（moving average）实现一个最小的负载估计器，理解系数 \(\alpha\) 在"响应速度"与"估计平稳"之间的权衡。
3. 以**消费者视角**使用三张输出表：按 `phy2log` 用 gather 重排专家权重、按 `log2phy + logcnt` 构建路由查找，并正确处理 `-1` padding（包括"第 0 槽位永远有效"这一安全默认）。
4. 量化一次重排的参数迁移开销，讨论重排触发节奏与均衡收益之间的取舍。
5. 跑通（或精读）一个闭环模拟脚本：负载随时间漂移 → 滑动平均 → 每轮重排 → 打印重排前后每 GPU 负载变化。

本讲对应的代码实践产出是一个可以直接运行的模拟集成脚本，它是后续 u3-l5 做变体实验的载体。

## 2. 前置知识

本讲是 advanced 层的"集成课"，不再展开算法内部细节，但默认你已从前面几讲带走以下结论。先快速对齐口径：

| 前置结论 | 来自 | 一句话回顾 |
| --- | --- | --- |
| 三张输出表 | u1-l3、u2-l6 | `phy2log[l, p]`＝槽位 p 放的是哪个逻辑专家（正向表，驱动**权重重排**）；`logcnt[l, e]`＝逻辑专家 e 的副本数；`log2phy[l, e, r]`＝专家 e 第 r 个副本在哪个槽位（反向表，驱动**路由**），无效槽位为 -1 |
| 槽位编码 | u1-l3、u2-l5 | 物理槽位按「节点 → GPU → 槽内位置」混合进制连续编码，因此 `view(L, P, M//P)` 一步就能按 GPU 分组求和 |
| 入口三段式 | u2-l6 | `rebalance_experts` = 输入规范化（`weight.float().cpu()`）→ 按 `num_groups % num_nodes` 分派层级/全局策略 → scatter 组装 `log2phy` |
| 均分假设 | u2-l2、u2-l5 | 源码用 `weight / logcnt` 作为单副本负载，隐含"逻辑专家的流量在它的副本间均分"——这个假设要靠下游路由来兑现 |
| IB 指标 | u3-l2 | 不均衡度 IB = 每 GPU 负载的 max/mean，恒 ≥ 1，本讲沿用它衡量"重排前后"的均衡质量 |
| 不变量测试 | u3-l1 | 结构正确性用断言（守恒、互逆、覆盖等），本讲的脚本会顺手复用其中一两条 |
| gather 方向 | u2-l3 | gather 是"读侧重排"，index 取「目的地 → 来源」方向：`new = old.gather(-1, dst2src)` |

环境仍是 u1-l3 搭好的 CPU 版 PyTorch：`eplb` 是纯 Python 模块，唯一第三方依赖是 `torch`，本讲所有脚本都不需要 GPU。

一个需要提前建立的观念转变：前面几讲我们是 `rebalance_experts` 的**读者**（看它怎么算），本讲我们是它的**调用者**（看怎么把它嵌进系统）。读者的问题是"它为什么对"，调用者的问题是"它的输入从哪来、输出怎么用、多久调一次"。

## 3. 本讲源码地图

本仓库极小（核心实现只有 `eplb.py` 约 165 行），本讲涉及的文件与位置如下：

| 文件 | 关键位置 | 在本讲中的角色 |
| --- | --- | --- |
| `README.md` | 第 3-8 行 | 问题陈述：负载随 workload 变化，所以需要"持续地"再均衡——闭环的动机 |
| `README.md` | 第 10-13 行 | **边界声明**：负载预测方法不在本仓库范围，建议用历史统计的滑动平均——本讲 4.2 的依据 |
| `README.md` | 第 19-31 行 | 层级策略用于 prefill（小 EP）、全局策略用于 decode（大 EP）——实际系统要维护两套布局的依据 |
| `README.md` | 第 39-57 行 | 官方示例：一次性调用 `rebalance_experts` 的最小用法 |
| `eplb.py` | 第 131-162 行 | `rebalance_experts` 入口：本讲流水线的中段，重点看输入输出契约与 `log2phy` 的 -1 构造 |
| `eplb.py` | 第 44-71 行 | `replicate_experts`：`logcnt` 初始化为全 1（第 64 行）是"-1 padding 安全默认"的证据；第 67 行的 `weight / logcnt` 是均分假设的证据 |
| `eplb.py` | 第 103-108 行 | 层级策略 Step 1：贪心装箱只看当前权重、与历史布局无关——"库无惯性，节奏靠调用方"的证据 |

下面按四个最小模块展开：流水线全景、上游估计、下游应用、触发节奏。四个模块合起来正好覆盖本讲规格指定的最小模块 `rebalance_experts` 的集成面。

## 4. 核心概念与源码讲解

### 4.1 流水线全景：EPLB 只是闭环中的一环

#### 4.1.1 概念说明

`rebalance_experts` 是一个**无状态的纯函数**：喂进去一份负载估计，吐出来一套放置方案。它不知道上一轮放了什么（没有任何内部状态），也不负责把方案落地（不搬任何参数）。在一个真实的专家并行系统里，它只覆盖下述闭环的中段一环：

```
┌────────────────────────────────────────────────────────────────────┐
│  ① 框架统计    每层每个逻辑专家收到的 token 数 x_t（随 workload 漂移） │
│       ↓                                                             │
│  ② 负载估计    滑动平均等历史统计 → \bar{w}_t                         │
│       ↓  （README 明确：这一步 out of this repo's scope）             │
│  ③ 放置计算    eplb.rebalance_experts(\bar{w}_t, M, G, N, P)         │
│       ↓        → phy2log / log2phy / logcnt                         │
│  ④ 应用映射    框架按表迁移专家权重、切换路由，新布局生效              │
│       ↓                                                             │
│    回到 ①，继续收集统计 —— 闭环，每 T 步转一圈                        │
└────────────────────────────────────────────────────────────────────┘
```

为什么这样切分？因为 ② 的统计口径（数什么、在哪数、多久数一次）和 ④ 的落地方式（参数怎么搬、路由怎么切）都和具体框架强耦合，而 ③ 是纯算法：输入一份 `float` 权重、输出三张 `int64` 表，不依赖任何框架概念。把纯算法单独抽出来，才有了这个可以被我们在这套讲义里独立测试（u3-l1）、评估（u3-l2）、基准化（u3-l3）的仓库。

#### 4.1.2 核心流程

把闭环写成伪代码：

```text
初始化: 布局 plan₀（例如均匀放置），est₀ = 首轮统计
每个训练/推理步 t:
    x_t  = 统计本步每层每逻辑专家的 token 数          # ① 框架侧
    est  = ema_update(x_t, est)                       # ② 估计器（调用方实现）
    if t % T == 0:                                    # 每 T 步触发一次
        phy2log, log2phy, logcnt = rebalance_experts(est, M, G, N, P)   # ③ EPLB
        migrate_weights(old_layout, phy2log)          # ④ 应用（调用方实现）
        switch_routing_table(log2phy, logcnt)         # ④ 应用（调用方实现）
    用当前布局执行前向/反向                            # 路由按副本均分
```

本讲要补齐的就是 ②④ 两段的最小实现，以及 T 怎么选（4.4）。

#### 4.1.3 源码精读

先看 README 里最重要的一句"边界声明"：

> [README.md:10-13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)

```python
To facilitate reproduction and deployment, we open-source our deployed EP load balancing algorithm in `eplb.py`. 
The algorithm computes a balanced expert replication and placement plan based on the estimated expert loads. Note 
that the exact method to predict the loads of experts is out of this repo's scope. A common method is to use 
moving average of historical statistics.
```

这三句话就是本讲的存在理由：仓库只负责"基于**估计的**负载计算放置方案"；估计方法（建议滑动平均）和落地应用都留给集成方。

再看入口的输入输出契约，全部写在 docstring 里：

> [eplb.py:131-146](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L146)

```python
def rebalance_experts(weight: torch.Tensor, num_replicas: int, num_groups: int,
                      num_nodes: int, num_gpus: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    ...
        weight: [layers, num_logical_experts], the load statistics for all logical experts
        ...
    Returns: 
        physical_to_logical_map: [layers, num_replicas], the expert index of each replica
        logical_to_physical_map: [layers, num_logical_experts, X], the replica indices for each expert
        expert_count: [layers, num_logical_experts], number of physical replicas for each expert
    """
```

注意两个用词：参数 `weight` 的说明是 **"the load statistics"**——它期待的就是 ① 产出的统计（经 ② 平滑）；返回值名字是 `..._map`——它们是"表"，不是动作。库把"怎么做"留给自己，把"从哪来、到哪去"留给调用方。

形状从哪来？看函数体第一段：

> [eplb.py:148-149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L149)

```python
    num_layers, num_logical_experts = weight.shape
    weight = weight.float().cpu()
```

层数和逻辑专家数都从 `weight` 的形状推断——这意味着 ① 侧的统计张量必须精确按 `[层数, 逻辑专家数]` 组织。`.float().cpu()` 则告诉我们：统计哪怕是在 GPU 上算出来的半精度/整数计数器，入口也会统一搬成 CPU 上的 float32（动机在 u2-l6 与 u3-l3 分析过：数值统一与避免跨设备同步）。

最后，模块的公共面收得极窄：

> [eplb.py:164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164)

```python
__all__ = ['rebalance_experts']
```

只导出一个入口函数——库对自己的定位就是"闭环中段的那一个纯函数"。

#### 4.1.4 代码实践

**实践目标**：把"契约"从源码里抄成一张集成速查表，并用一个最小脚本验证形状契约。

**操作步骤**：

1. 运行下面的最小验证脚本（示例代码）：

```python
# contract_check.py（示例代码）
import torch, eplb

L, E, M, G, N, P = 2, 12, 16, 4, 2, 8
weight = torch.rand(L, E) * 100          # 模拟一份负载统计
phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, G, N, P)
assert phy2log.shape == (L, M)
assert logcnt.shape == (L, E)
assert log2phy.shape[:2] == (L, E)
assert log2phy.size(-1) == logcnt.max().item()   # 第三维 = 全局最大副本数
print("契约验证通过；maxlogcnt =", log2phy.size(-1))
```

2. 对照 u1-l4 的"函数与形状速查表"，手填一张集成视角的契约表：每个输入的**生产者**是谁（框架的哪个统计）、每个输出的**消费者**是谁（权重迁移器 / 路由器）。

**需要观察的现象**：`log2phy` 的第三维不是常量，它等于 `logcnt.max()`——同一组参数下换一份负载，第三维可能变。

**预期结果**：断言全部通过。打印出的 maxlogcnt 值取决于随机权重，通常为 2 或 3，具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 EPLB 把负载预测排除在仓库之外，而不是内置一个滑动平均？

**答案**：统计口径（数 token 还是数 FLOPs、按层还是按桶聚合、在 dispatch 前还是 combine 后数）与具体框架强耦合，内置反而限制复用；而放置算法只依赖"一份可信的估计"，把估计外置后算法可以独立测试、独立复现（u3-l1 的测试不需要任何真实框架）。纯函数边界也让"换估计器"成为零成本实验——4.2 会利用这一点。

**练习 2**：`rebalance_experts` 两次用**完全相同**的输入调用，输出一定相同吗？这对集成有什么意义？

**答案**：库内没有随机数、没有内部状态；贪心链路（排序取 indices、`min(range, key=...)` 取并列最小包号）在同一平台上是确定性的，因此相同输入得到相同输出（注：`sort` 对并列权重的次序跨设备不保证稳定，但同平台可复现）。集成意义：重放同一份统计日志即可复现布局——这正是本讲综合实践能"模拟"闭环的前提；也意味着布局抖动只能来自**输入（估计）的抖动**，而非库本身，这是 4.4 讨论迟滞的出发点。

**练习 3**：如果框架侧统计的是 `int64` 的 token 计数，直接喂给入口会有问题吗？

**答案**：入口第 149 行 `weight.float().cpu()` 会把它转成 float32。float32 的有效位数约 24 bit（可精确表示到约 \(1.7\times 10^7\)），一般 token 计数远小于此，精度足够；但如果统计周期很长、计数极大，需要先在框架侧归一化或分桶（如按比例缩放到 0-1000）再输入——数值细节在 u3-l3 讨论过。

### 4.2 上游：从历史统计到负载估计（滑动平均）

#### 4.2.1 概念说明

为什么要"估计"而不是直接用当前一步的统计？两个原因：

1. **负载是时变的**。README 开篇就说 [README.md:3-4](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L3-L4)："the load of different experts may vary depending on the current workload"。今天的热点专家和明天不同，所以闭环必须周期性重估。
2. **瞬时统计有噪声，而布局对噪声极其敏感**。`balanced_packing` 按权重降序贪心（[eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)），两个专家权重的一点扰动就可能改变装箱次序、进而改变整个布局；`replicate_experts` 的 `argmax` 同理（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)）。直接把带噪统计喂进去，布局会"颠簸"，而每次颠簸都是一次真实的参数迁移（4.4）。

README 建议的默认估计器是滑动平均。最常用的是指数滑动平均（EMA）：

\[ \bar{w}_t = \alpha\, x_t + (1-\alpha)\,\bar{w}_{t-1}, \qquad \alpha \in (0, 1] \]

递推展开等价于对历史做指数衰减加权：

\[ \bar{w}_t = \alpha \sum_{k \ge 0} (1-\alpha)^k\, x_{t-k} \]

- \(\alpha = 1\)：退化成"直接用最新统计"，零滞后但对噪声全敏感。
- \(\alpha\) 小：有效记忆窗口约 \(1/\alpha\) 步，平滑但滞后。

如果统计噪声近似独立同分布、方差为 \(\sigma_x^2\)，标准 EMA 理论给出估计量的稳态方差：

\[ \sigma_{\bar{w}}^2 = \frac{\alpha}{2-\alpha}\,\sigma_x^2 \]

即 \(\alpha\) 减半（在小区间内）近似把噪声方差减半——这是"平滑"的定量收益；代价是当负载真的漂移时，估计要花约 \(1/\alpha\) 步才能跟上——"滞后"的定量代价。选 \(\alpha\) 就是在这两者之间找平衡。

#### 4.2.2 核心流程

上游估计器的最小实现只有一行递推，流程上它插在"统计"与"重排"之间：

```text
x_t（本步原始统计, [L, E]）
   │
   ├─ α=1 ──────────────→ est = x_t            # 无平滑，直接进 ③
   └─ 0<α<1 ─→ est = α·x_t + (1-α)·est_prev    # EMA 递推
                      │
                      ↓
        eplb.rebalance_experts(est, M, G, N, P)
```

有一个容易被忽略的集成细节：**输出的形状本身依赖输入**。看第 157 行：

> [eplb.py:157](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157)

```python
    maxlogcnt = logcnt.max().item()
```

`log2phy` 第三维 = `maxlogcnt`，它由本轮负载决定（本轮谁被复制、复制几次）。负载漂移 → 估计变化 → `maxlogcnt` 可能逐轮变化。所以下游不能把 `log2phy` 当固定形状的表缓存，必须每轮动态处理——这是 4.3 里"以 logcnt 为准做掩码"的又一个理由。

#### 4.2.3 源码精读

上游的三处源码证据：

1. **闭环动机**——[README.md:3-4](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L3-L4)：负载因 workload 而异，所以均衡不是一次性的，而是持续的过程。
2. **估计器建议**——[README.md:11-13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L11-L13)：

   ```python
   Note that the exact method to predict the loads of experts is out of this repo's scope. A common method is to use 
   moving average of historical statistics.
   ```

   "moving average of historical statistics"——注意它甚至没有规定是 EMA 还是窗口平均，只给了默认思路。本讲选 EMA 是因为一行递推、无需缓存历史窗口，适合分布式场景各 rank 各自维护。
3. **统计的设备与精度入口**——[eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 的 `weight.float().cpu()`：框架的统计计数器通常在 GPU 上，集成时可以直接把 GPU 张量传进来（入口会搬），也可以像 u3-l3 讨论的那样先在框架侧聚合，避免每步同步。

另外注意一个上游必须兜住的边界：**统计恒为 0 的专家**。`replicate_experts` 初始化 `logcnt` 为全 1（[eplb.py:64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L64)），副本在循环里只增不减，所以零负载专家仍会占据恰好一个槽位。估计器不需要对零负载做特殊处理，但集成方要知道：**冗余只能摊薄重载，省不掉轻载专家的显存**。

#### 4.2.4 代码实践

**实践目标**：直观感受 \(\alpha\) 对"噪声平滑"与"漂移滞后"的影响。

**操作步骤**：运行以下脚本（示例代码）：

```python
# ema_experiment.py（示例代码）
import torch

torch.manual_seed(0)
T, L, E = 60, 1, 12
base = torch.rand(L, E) * 80 + 40                       # 基础负载
steps = torch.arange(T)
hot = (steps // 10 * 3) % E                              # 热点每 10 步移动 3 个位置

def observe(t):                                          # 真实负载 = 基础 + 热点 + 噪声
    x = base.clone()
    x[0, hot[t]] += 200.0
    return x + torch.randn(L, E) * 20                    # σ = 20 的观测噪声

for alpha in (1.0, 0.3, 0.05):
    est, noise_err, drift_err = observe(0), [], []
    for t in range(1, T):
        pred = est                                       # 用"上一时刻的估计"预测当前
        truth_clean = base.clone(); truth_clean[0, hot[t]] += 200.0
        noise_err.append((pred - truth_clean - (observe(t) - truth_clean)).abs().mean().item())
        drift_err.append((pred - truth_clean).abs().mean().item())
        est = alpha * observe(t) + (1 - alpha) * est     # EMA 递推
    print(f"alpha={alpha:<5} 预测总误差≈{sum(drift_err)/T:6.2f}  其中噪声贡献≈{sum(noise_err)/T:6.2f}")
```

**需要观察的现象**：三行输出中总误差的构成——\(\alpha=1.0\) 噪声贡献大；\(\alpha=0.05\) 噪声小但热点跳变后误差持续很久（滞后）；中间值总误差最小。

**预期结果**：定性规律如上；具体数值待本地验证。可把 `hot` 的移动周期从 10 改成 3，观察"漂移更快时最优 \(\alpha\) 变大"。

#### 4.2.5 小练习与答案

**练习 1**：把 \(\alpha\) 设成 0.01 会发生什么？什么场景下反而是合理的？

**答案**：有效窗口约 100 步，估计几乎不动，重排会长期沿用旧布局，热点漂移期间持续失衡。合理场景：负载分布基本稳定、只想滤掉短窗噪声的低频调优，或者重排代价极高（4.4）只能低频触发的系统。

**练习 2**：估计器输出的 `est` 里某个专家长期为 0，最终布局里它占几个槽位？

**答案**：恰好 1 个。`replicate_experts` 初始 `logcnt = ones`（[eplb.py:64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L64)），贪心只复制 `weight/logcnt` 最大的专家，零负载专家永远不会被选中，副本数保持 1。所以物理专家预算 \(M\) 中至少有 \(E\) 个槽位是"保底显存"。

**练习 3**：为什么不把原始统计直接喂给 `rebalance_experts`，让库自己去平滑？

**答案**：库是无状态纯函数（4.1），没有"上一次的估计"可记，想平滑也只能在调用方记。更重要的是平滑参数 \(\alpha\) 与重排周期 T、迁移预算强耦合（4.4），属于部署侧策略而非算法本体——README 把两者一起划在仓库之外是自洽的。

### 4.3 下游：按三张表重排权重与路由表

#### 4.3.1 概念说明

拿到 `(phy2log, log2phy, logcnt)` 之后，框架要做两件事，分别由两张表驱动：

**（a）权重重排（参数显存布局）—— `phy2log` 驱动**。新布局里槽位 p 的参数 = 逻辑专家 `phy2log[l, p]` 的参数。这就是一次标准的 gather 重排（u2-l3 的"读侧重排"）：

```text
new_params[l, p] = old_params[l, phy2log[l, p]]        # index 方向：目的地 → 来源
```

被复制的专家，其参数会写到多个槽位（复制即拷贝权重）。

**（b）路由表 —— `log2phy + logcnt` 驱动**。前向时逻辑专家 e 被激活，token 要发到它的某个**副本**所在的 GPU：

```text
r    = token 在 [0, logcnt[l, e]) 内取一个值    # 例如按 token 序号取模
slot = log2phy[l, e, r]                          # 该副本的物理槽位
gpu  = slot // (M // P)                          # 槽位连续编码 → 整除即得 GPU 号
```

第 2 步必须以 `logcnt` 为界取 r——这就引出本讲的核心细节：**`-1` padding 的处理**。

回顾 `log2phy` 的构造（u2-l6 精读过，这里从消费侧再看一遍）：

> [eplb.py:157-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161)

```python
    maxlogcnt = logcnt.max().item()
    log2phy: torch.Tensor = torch.full((num_layers, num_logical_experts, maxlogcnt), 
                                       -1, dtype=torch.int64, device=logcnt.device)
    log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank, 
            torch.arange(num_replicas, dtype=torch.int64, device=log2phy.device).expand(num_layers, -1))
```

先用 -1 铺满整个 `[L, E, maxlogcnt]`，再 scatter 写入每个副本的槽位号。于是对每个逻辑专家 e：第 0 到 `logcnt[l,e]-1` 个位置是有效槽位号，之后直到 `maxlogcnt-1` 全是 -1。消费侧的三条纪律：

1. **绝不能把未经掩码的 `log2phy` 当索引用**。-1 在 PyTorch 高级索引里是"倒数第一个"——静默回绕到错误元素；在 `gather`/`scatter_` 里则会直接报 index 越界。无论哪种，都不是你要的语义。正确做法是先 `log2phy[l, e, :logcnt[l, e]]` 截断或用 `r < logcnt` 做布尔掩码。
2. **第 0 槽位永远有效，可作安全默认**。`replicate_experts` 初始化 `logcnt = ones`（[eplb.py:64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L64)），每个逻辑专家至少 1 个副本，且其 rank 恒为 0（初始副本 rank 为 0，后续副本 rank 递增，见 [eplb.py:62-69](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L69)），所以 `log2phy[:, :, 0]` 永不为 -1。
3. **路由必须把流量均分到各副本**，才兑现均衡前提。源码反复用 `weight / logcnt` 当单副本负载——[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)（复制谁）与 [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)（装箱时的单副本负载）：

   ```python
   tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
   ```

   如果路由把某专家的 token 都发给同一副本，真实负载就不再是 `weight/logcnt`，EPLB 算出的方案会失真。均分最简单的实现就是按 token 序号对 `logcnt` 取模选 r。

#### 4.3.2 核心流程

下游应用的完整伪代码（标注每步用到的表）：

```text
输入: 旧布局 (phy2log_old, log2phy_old, logcnt_old)，新布局三表，专家参数 W_old[l, e, ...]

1. 参数迁移计划（槽位 → 槽位）:
   对每个新槽位 p:  需要逻辑专家 e = phy2log_new[l, p] 的参数
   它在旧布局的存放处: src = log2phy_old[l, e, 0]        # rank-0 槽位，永为有效值
   → 迁移表 transfer[l, p] = src；仅 src != p 的槽位需要真正搬运

2. 权重重排:
   W_new = W_old.gather(1, phy2log_new)                   # 若 W 圚含 hidden 维，先 expand index

3. 路由切换:
   对被激活的 (l, e, token): r = token % logcnt_new[l, e]
                             dst_gpu = log2phy_new[l, e, r] // (M // P)

4. 之后每步统计真实负载，回到 4.2 的估计器 —— 闭环继续
```

其中第 1 步把两张表串了起来：`transfer = log2phy_old[:, :, 0].gather(1, phy2log_new)` 一行即可算出。迁移量（4.4 的核心指标）就是 `transfer` 中 `src != p` 的元素个数乘以每专家参数字节数。

#### 4.3.3 源码精读

返回值契约在 docstring 里写得很清楚：

> [eplb.py:144-146](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L144-L146)

```python
        physical_to_logical_map: [layers, num_replicas], the expert index of each replica
        logical_to_physical_map: [layers, num_logical_experts, X], the replica indices for each expert
        expert_count: [layers, num_logical_experts], number of physical replicas for each expert
```

注意 `logical_to_physical_map` 的第三维写的是 `X`——文档没有承诺具体长度，运行时由 `maxlogcnt` 决定（4.2 已强调）。`physical_to_logical_map` 的说明"the expert index of each replica"正是权重重排的语义：**每个副本（槽位）持有哪个专家的参数**。

`-1` 只出现在第 158-159 行的 `torch.full(..., -1, ...)`，而 scatter（第 160-161 行）只写 `num_replicas` 个有效地址——所以 -1 的分布**恰好**就是无效副本槽位（u2-l6 从构造侧证明过；下面 4.3.4 的实践从数据侧再验证一次）。有效/无效的分界线由 `phyrank` 的取值性质决定：每个专家的 rank 恰为 \(0..logcnt-1\)，这可以追溯到 `replicate_experts` 的初始化与递增逻辑：

> [eplb.py:62-69](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L69)

```python
    phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
    rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
    logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
    arangen = torch.arange(n, dtype=torch.int64, device=device)
    for i in range(num_log, num_phy):
        redundant_indices = (weight / logcnt).max(dim=-1).indices
        phy2log[:, i] = redundant_indices
        rank[:, i] = logcnt[arangen, redundant_indices]
```

初始每个专家占槽位 i、rank 0、count 1；新副本的 rank 取当时的 count（写入前），因此 rank 集合恰为 \(\{0,1,\dots,logcnt-1\}\)，没有空洞——这就是"`log2phy[:, :, 0]` 安全默认"与"`-1` 恰在尾部"两个消费侧性质共同的根。

最后，均分假设的证据链在层级策略 Step 3 再次出现：

> [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)

```python
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
```

装箱给 GPU 时用的就是"均分后的单副本负载"。**路由侧若不均分，这一行的假设就被打破**——这是集成时最容易踩的隐性契约。

#### 4.3.4 代码实践

**实践目标**：验证 -1 padding 的三条消费侧性质，并完成一次最小的"参数重排 + 路由找回"往返。

**操作步骤**：运行以下脚本（示例代码）：

```python
# downstream_check.py（示例代码）
import torch, eplb

torch.manual_seed(0)
L, E, M, G, N, P = 2, 12, 16, 4, 2, 8
weight = torch.rand(L, E) * 100
phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, G, N, P)

# 性质 1：rank-0 槽位永远有效（安全默认）
assert (log2phy[:, :, 0] != -1).all()

# 性质 2：-1 恰好落在 logcnt 之外的尾部槽位
r = torch.arange(log2phy.size(-1))
valid = r.unsqueeze(0).unsqueeze(0) < logcnt            # [L, E, maxlogcnt] 布尔掩码
assert (log2phy[valid] >= 0).all()
assert (log2phy[~valid] == -1).all()

# 性质 3：权重重排与路由表互为一致的读法（往返验证）
params = torch.arange(L * E).view(L, E)                  # 模拟参数：每专家一个标量 id
new_params = params.gather(1, phy2log)                   # 重排：槽位 p ← 专家 phy2log[l,p]
back = new_params.gather(1, log2phy[:, :, 0])            # 路由：专家 e ← 其 rank-0 副本槽位
assert torch.equal(back, params)

# 危险演示：把未掩码的 log2phy 当索引用（取消注释观察错误）
# wrong = new_params.gather(1, log2phy.flatten(1)[:, :E])  # 若其中含 -1 会越界报错
print("三条性质 + 往返一致性全部通过")
```

**需要观察的现象**：断言全部通过；把 `valid` 掩码打印出来，能看到每行 1 的个数恰等于 `logcnt`；取消最后注释行（或手工 `params[:, -1]`）观察 -1 索引的行为差异。

**预期结果**：性质 1-3 是从源码推出的不变量（u3-l1 的 INV 清单也覆盖了它们），预期全部通过；"危险演示"的具体报错信息随 PyTorch 版本可能不同，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `log2phy` 用定长张量 + `-1` 填充，而不是每专家一个变长列表？

**答案**：定长张量可以整体放在 GPU 上被 gather/掩码向量化访问，与路由热点路径兼容；变长列表只能逐专家循环，且无法参与批量张量运算。代价是无效槽位需要哨兵值与掩码纪律——这是"变长信息的定长张量化"的通用取舍（u2-l6 从构造侧讨论过同一问题）。

**练习 2**：哨兵为什么选 -1 而不是 0？

**答案**：0 是合法槽位号（第一个槽位），用作哨兵会与真实数据混淆；-1 不是任何槽位号，逻辑上无歧义。但要注意 PyTorch 的负索引语义使 -1 在高级索引下"静默合法"（取最后一个元素），所以 -1 的安全性**完全依赖消费方先掩码**——选哨兵时要同时想到它在下游所有索引路径上的行为。

**练习 3**：冗余专家的多个副本在后续训练中参数会各自更新吗？需要同步吗？

**答案**：这超出本仓库范围——EPLB 只输出映射表，不管理参数生命周期。工程上副本是同一逻辑专家的拷贝，训练时各副本收到不同 token、产生不同梯度，需要框架侧做梯度同步/规约才能保持参数一致（DeepSeek-V3 论文的部署描述属于框架侧实现）。对纯推理部署则只需在重排时一次性拷贝权重。集成时的要点是：**副本参数一致性是框架的职责，EPLB 的表只保证"谁从谁拷贝"这个来源关系**。

### 4.4 触发节奏与迁移开销的权衡

#### 4.4.1 概念说明

重排不是免费的。一次布局切换至少包含三类成本：

1. **参数搬运**：迁移表（4.3.2 第 1 步）中 `src != p` 的每个槽位都要把一份完整专家权重从旧槽位搬到新槽位，通常还要跨 GPU（甚至跨节点）走集合通信。一个专家的参数量在 DeepSeek-V3 量级的模型上是数百 MB 级，搬运会与训练/推理流量争抢互连带宽。
2. **切换停顿**：路由表切换需要所有 rank 到齐（谁还在按旧表发 token 就会路由到错误专家），通常意味着一个同步屏障。
3. **布局颠簸**：统计噪声引起的无意义迁移（4.2 的动机）。

对应地，不重排的成本是**失衡损失**：布局滞后于负载漂移，IB 从重排刚结束的接近 1 逐渐爬升。于是节奏问题就是一个在线决策：

\[ \text{此刻重排} \iff \text{预期的失衡改善收益} > \text{迁移开销} \]

工程上常用的第一道防线是 4.2 的滑动平均（压噪声），第二道是**迟滞触发**（hysteresis）：只有当"用当前估计试算的新方案"比现行方案 IB 改善超过阈值 \(\delta\)，且预估迁移量低于预算时才真正切换。注意这两道防线都不在 EPLB 里——这是本讲最重要的集成结论：

> [eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)

```python
    if num_groups % num_nodes == 0:
        # use hierarchical load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 
                                                                  num_groups, num_nodes, num_gpus)
    else:
        # use global load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

入口每次调用都**从头完整重算**：没有增量更新、没有迟滞、没有"尽量少动"的约束。库对节奏完全无感，节奏控制是调用方的责任。这一点在层级策略 Step 1 也看得到——贪心装箱只看当前权重：

> [eplb.py:104-105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L105)

```python
    tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
    group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes)
```

`balanced_packing` 不接收上一轮布局作为输入（签名见 [eplb.py:5](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5)），对"最小扰动"没有任何偏好。两道防线之外，还有一个结构性事实值得集成方注意：**prefill 与 decode 的 EP 规模不同**（[README.md:19-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L31)：层级策略用于较小 EP 的 prefill，全局策略用于较大 EP 的 decode），所以一个混合部署的系统实际上同时维护**两套布局、两套统计、两套节奏**——恰好对应入口按 `num_groups % num_nodes` 的自动分派：两套统计的 `num_groups` 不同，自然走进不同分支。

#### 4.4.2 核心流程

量化迁移开销：设每专家参数字节数为 \(B_{\text{expert}}\)，一轮重排的迁移量为

\[ \text{moved}(t) = \sum_{l,\,p} \mathbb{1}\left[\text{phy2log}^{(t)}_{l,p} \neq \text{phy2log}^{(t-1)}_{l,p}\right] \times B_{\text{expert}} \]

（用"新表与旧表在槽位 p 放的专家不同"计数；严格的传输量应经 4.3.2 的 `transfer` 表换算，槽位值相同的专家也可能换了来源副本，但作为代理指标，直接比较两张 `phy2log` 已经够用且实现只需一行。）

迟滞触发的决策伪代码（**示例设计**，不是仓库代码）：

```text
每个重排候选时刻:
    est = 当前滑动平均估计
    新方案 = rebalance_experts(est, ...)
    ib_old = 按旧布局复算的 IB（用 est 当负载）
    ib_new = 按新方案复算的 IB
    moved  = (新phy2log != 旧phy2log).sum() * B_expert
    if (ib_old - ib_new) / ib_old > δ  and  moved < 预算:
        执行迁移与切换
    else:
        保持旧布局（省下这次迁移）
```

节奏的三个旋钮及其方向：

| 旋钮 | 调大 | 调小 |
| --- | --- | --- |
| EMA 系数 \(\alpha\) | 响应快、噪声大、迁移频繁 | 平滑、滞后、可能错过热点 |
| 重排周期 T | 迁移少、失衡累积 | 均衡好、开销大 |
| 迟滞阈值 \(\delta\) | 只在大失衡时动，稳但迟钝 | 接近每周期必动，接近无迟滞 |

#### 4.4.3 源码精读

本模块的"源码精读"读的是**库没做什么**：

- [eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)：入口没有时间、频率、历史概念——每次调用都是独立的完整计算。调用方想加迟滞，唯一途径是**在调用方先试算、再决定是否应用**（把"算方案"和"用方案"解耦，方案算错了不迁移就没有成本，只有一次 CPU 上的计算开销——u3-l3 量级下这是廉价的）。
- [eplb.py:5-20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L20)：`balanced_packing` 的签名只有 `(weight, num_packs)`——没有 `previous_pack_index` 之类的惯性输入。
- [README.md:19-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L31)：两策略分别绑定 prefill/decode 场景——集成侧应把它们当作两条独立的闭环，而不是一个集群的一套布局。

#### 4.4.4 代码实践

**实践目标**：在固定负载轨迹下，扫重排周期 T，观察"累计失衡"与"累计迁移"的此消彼长。

**操作步骤**：使用第 5 节综合实践脚本，把 `REBALANCE_EVERY` 分别改为 1、2、4、8 各跑一遍（EMA 的 \(\alpha\) 固定 0.3），记录每次输出的累计 `重排前IB` 均值与累计 `迁移槽位` 总数，整理成表。

**需要观察的现象**：T 越大，"重排前 IB"逐步抬升（布局越来越滞后于热点），但迁移总量近似反比下降；T=1 时迁移槽位数最大，且多数迁移发生在热点跳变后的首次重排。

**预期结果**：存在一个中间 T 使"累计失衡 × 时间 + 迁移惩罚"的组合指标最优；具体数值待本地验证。进一步可以把 `moved > 0 但 IB 改善 < 1%` 的"无效重排"次数也统计出来——它就是迟滞阈值 \(\delta\) 应该拦掉的部分。

#### 4.4.5 小练习与答案

**练习 1**：既然每次调用 `rebalance_experts` 只花 CPU 上的毫秒级时间（u3-l3），为什么不能每个训练步都调用并应用？

**答案**：调用本身廉价，**应用**不廉价——每个专家的参数要以数百 MB 计跨设备搬运，还有路由切换的同步屏障。把"算方案"（可每步做，用于监控与迟滞判断）与"迁移应用"（须节流）分开，正是迟滞设计的结构基础。

**练习 2**：迟滞触发需要"试算"，试算结果与最终应用之间隔了一个判断分支。这会引入什么新问题？

**答案**：一是**一致性**：试算用 `est`，若真正应用时统计又变了，方案与动机可能脱节，通常以同一份 `est` 快照为准；二是**多重触发**：多个 rank/进程各自判断可能得出不同结论，判断必须集中在一处再广播；三是**抖动边界**：\(\delta\) 太小时在阈值附近反复切换，工程上还会再加最小间隔时间（冷却期）。

**练习 3**：为什么 prefill 与 decode 可以（且需要）用不同的重排节奏？

**答案**：两个阶段的负载稳定性与迁移代价不同。prefill 计算密集、批内负载相对稳定，且阶段间有天然屏障（适合趁机重排）；decode 访存密集、EP 更大、用户在环，重排停顿直接伤延迟，通常更低频、更保守（\(\alpha\) 与 \(\delta\) 更大）。两套节奏对应入口的两条分派分支，互不干扰。

## 5. 综合实践

现在把四个模块串成一个闭环模拟器，对应本讲的实践任务：**生成随时间漂移的专家负载统计（滑动平均），每轮调用 `rebalance_experts` 并按映射"重排"模拟的专家权重张量，打印每次重排前后每 GPU 负载的变化曲线**。

设计要点逐条对应前文：

- 漂移负载：基础负载 + 每 `DRIFT_EVERY` 步移动一次的热点（4.2 的时变假设）；
- 估计器：一行 EMA 递推（4.2）；
- 每轮重排 + 按 `phy2log` gather 重排"参数"（4.3 的读侧重排；用标量 id 模拟参数，便于断言重排正确性）；
- "重排前 IB"用**旧布局**面对当前真实负载复算，"重排后 IB"用**新布局**面对同一负载复算（承接 u3-l2 的口径：单副本负载 = 真实负载 / logcnt，槽位按 GPU 连续编码后 `view + sum`）；
- 迁移量用新旧 `phy2log` 的差异槽位数作代理（4.4）。

```python
# simulate_eplb_pipeline.py（示例代码）
import torch
import eplb

# ---- 配置 ----
L, E = 2, 12                    # 层数、逻辑专家数
M, G, N, P = 16, 4, 2, 8        # 物理专家数、组数、节点数、GPU 数
PHY_PER_GPU = M // P
T_STEPS = 24                    # 模拟步数
DRIFT_EVERY = 4                 # 热点每 4 步移动一次
ALPHA = 0.3                     # EMA 系数（4.2 的旋钮）
REBALANCE_EVERY = 1             # 重排周期（4.4 的旋钮，改为 2/4/8 做实验）

torch.manual_seed(0)
base = torch.rand(L, E) * 80 + 40

def true_load(step):
    """模拟随时间漂移的真实负载：基础负载 + 移动热点"""
    x = base.clone()
    hot_layer = (step // DRIFT_EVERY) % L
    hot_left = ((step // DRIFT_EVERY) * 5) % E
    x[hot_layer, hot_left:hot_left + 2] += 160.0       # 热点作用于相邻两专家
    return x

def gpu_load(x, phy2log, logcnt):
    """按给定布局复算每 GPU 负载（u3-l2 的口径）"""
    per_phy = (x / logcnt).gather(-1, phy2log)          # 均分假设下的单副本负载 → 槽位负载
    return per_phy.view(L, P, PHY_PER_GPU).sum(-1)      # 槽位按 GPU 连续编码

def ib(load):
    return (load.max() / load.mean()).item()

# ---- 初始布局：朴素的循环放置（每专家至少 1 槽，前 M-E 个专家多 1 槽）----
phy2log = (torch.arange(M) % E).repeat(L, 1)
logcnt = torch.stack([torch.bincount(phy2log[l], minlength=E) for l in range(L)])

est = None
print(f"{'step':>4} | {'重排前IB':>8} | {'重排后IB':>8} | {'迁移槽位':>8} | 曲线(每格0.05)")
for step in range(T_STEPS):
    x = true_load(step)
    est = x.clone() if est is None else ALPHA * x + (1 - ALPHA) * est   # ② EMA

    ib_before = ib(gpu_load(x, phy2log, logcnt))        # 旧布局 × 当前真实负载

    moved = 0
    if step % REBALANCE_EVERY == 0:                     # ③ 试算 + 应用
        new_phy2log, _, new_logcnt = eplb.rebalance_experts(est, M, G, N, P)
        moved = (new_phy2log != phy2log).sum().item()   # 迁移量代理（4.4）
        # 权重重排（此处用 id 模拟参数；真实场景是 gather hidden 维权重）
        # params = params.gather(1, new_phy2log)
        phy2log, logcnt = new_phy2log, new_logcnt

    ib_after = ib(gpu_load(x, phy2log, logcnt))         # 新布局 × 同一真实负载
    bar = ' ' * int((ib_before - 1) / 0.05) + '|' + '#' * int((ib_after - 1) / 0.05)
    print(f"{step:>4} | {ib_before:>8.3f} | {ib_after:>8.3f} | {moved:>8} | {bar}")
```

**运行与观察**（结果待本地验证，以下为预期的定性现象）：

1. **重排立竿见影**：`重排后IB` 普遍显著低于 `重排前IB`，尤其在热点刚移动、旧布局还把冗余留给"上一个热点"的步骤。
2. **估计滞后可见**：热点跳变后第一轮，`重排后IB` 也会偏高——EMA 还带着旧热点的记忆（\(\alpha=0.3\) 时约需 3-4 步收敛），这正是 4.2 练习 1 的现象在布局层面的放大。
3. **迁移集中在漂移时刻**：`迁移槽位` 在热点跳变后的首次重排出现尖峰；若把 `DRIFT_EVERY` 调大到超过 T_STEPS（负载恒定），估计收敛后 `迁移槽位` 归零——贪心的确定性使布局到达不动点（4.1 练习 2 的性质）。
4. **周期实验**：把 `REBALANCE_EVERY` 改为 4，会看到非重排步的 `重排前IB = 重排后IB`（同一布局），且失衡在四个步内持续爬升——4.4 的权衡直接可读。
5. **稳健性检查**：可随时加上 u3-l1 的不变量断言（如 `logcnt.sum(-1) == M`）作为每轮的护栏。

一个值得思考的偏差：模拟中"重排后 IB"用的是**真实**负载 x，而 EPLB 拿到的是**估计** est——两者的差就是"估计误差传导为失衡"的部分。想单独看它，可把 `gpu_load` 的输入换成 `est` 再跑一遍对比。

## 6. 本讲小结

- **EPLB 是闭环中的一环**：`rebalance_experts` 是无状态纯函数，只覆盖「估计负载 → 放置方案」；负载预测（README 建议滑动平均）与权重迁移、路由切换都在集成方（[README.md:10-13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)）。
- **上游用 EMA 压噪声**：\(\bar{w}_t = \alpha x_t + (1-\alpha)\bar{w}_{t-1}\)，\(\alpha\) 平衡响应与滞后；库对输入噪声敏感（贪心比序、argmax），估计器是防布局颠簸的第一道防线；`log2phy` 第三维 = `maxlogcnt` 逐轮可变，下游不能缓存固定形状。
- **下游两表两用**：`phy2log` 驱动权重 gather 重排（`new = old.gather(1, phy2log)`），`log2phy + logcnt` 驱动路由；`-1` padding 必须按 `logcnt` 掩码，`log2phy[:, :, 0]` 是永远有效的安全默认；跨轮迁移表可由 `log2phy_old[:, :, 0].gather(1, phy2log_new)` 一行求得。
- **路由必须副本均分**：源码以 `weight / logcnt` 为单副本负载（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)、[eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)），不均分的路由会让整套均衡计算失真。
- **节奏与迁移开销的权衡在调用方**：库无增量、无迟滞、无最小扰动约束；迁移量可用新旧 `phy2log` 差异槽位数 × 每专家字节数估算；"算方案"与"应用方案"解耦后即可实现迟滞触发。
- **prefill/decode 是两条闭环**：小 EP 层级策略、大 EP 全局策略（[README.md:19-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L31)），两套统计、两套布局、两套节奏，由入口的整除性分派自然承载。

## 7. 下一步学习建议

- **下一讲 u3-l5（二次开发实战）**：把本讲的模拟器当实验床——实现"最大副本数受限"的 `replicate_experts` 变体、给集成层加迟滞触发，用 u3-l2 的 IB 指标与 u3-l1 的不变量测试分别评估其收益与正确性。
- **延伸阅读（超出本仓库范围）**：DeepSeek-V3 论文中 redundant experts 与组受限路由的部署描述，可以对照理解本仓库两个策略的场景绑定；各开源框架的 expert placement / weighted routing 实现则能对照 4.3 的"消费者视角"。
- **回看源码**：带着集成视角重读 [eplb.py:131-162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162)，这次重点不是算法，而是问自己：如果我要把它接进一个真实框架，输入从哪个统计钩子来、输出交给哪个迁移器、节奏由谁决定——能回答这三个问题，本讲的目标就达成了。
