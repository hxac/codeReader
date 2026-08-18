# u3-l3 性能、数值与设备一致性：结合两次真实修复演进

## 1. 本讲目标

前三单元我们已经把 EPLB 的算法读透、测对了（u3-l1）、也评了好坏（u3-l2）。本讲换一个视角：**把 eplb.py 当作一个在真实集群里运行的工程组件来审视**。学完本讲，你应该能够：

1. 分析 `balanced_packing` 与 `replicate_experts` 中 Python 循环的复杂度，判断哪个才是真正的耗时瓶颈，并能用 `timeit` 实测验证。
2. 解释入口处 `weight.float().cpu()` 这一行在数值稳定性和设备一致性上的双重作用。
3. 复盘两次真实提交：`e1100fe`（全局策略补上 GPU 级均衡）与 `d52c72d`（修复 `pphy2mlog` 设备缺失），理解"退化参数复用"这一重构带来的收益与代价。
4. 对 `rebalance_experts_hierarchical` 做一次完整的设备传播审计，说清楚"哪些张量在哪个设备上诞生"。

一句话定位：u3-l2 问"方案好不好"，本讲问"**算得快不快、稳不稳、在哪种硬件上能跑**"——这是二次开发前必须建立的工程判断力。

## 2. 前置知识

### 2.1 Python 循环 vs 张量化

PyTorch 的每次 `tensor[i, j]` 读写、每次算子调用都要经过 Python 解释器和张量调度层，单次开销在微秒量级；而一个形状为 `[100000]` 的向量化除法整体只要几十微秒。经验法则：

- **Python 层循环次数**往往比"循环体内处理了多少元素"更能决定墙钟时间。
- 把循环从"逐行"改成"行维批量"（循环次数从 \(X \cdot n\) 降到 \(n\)），是把 Python 开销摊薄到张量里的标准手法——`replicate_experts` 正是这么写的，`balanced_packing` 则不是。

### 2.2 复杂度速记

本讲用大 O 描述**Python 级操作次数**（区别于张量内部逐元素的 FLOPs）。一次 Python 级操作指一次解释器字节码意义上有感知的操作：一次 `min()` 调用、一次张量索引赋值、一次 0 维张量与 float 的运算。

### 2.3 PyTorch 的设备模型

- PyTorch **不会**自动搬运张量：CUDA 张量与 CPU 张量直接参与同一个运算会抛出 `RuntimeError`（设备不匹配）。
- 因此库代码里创建新张量时要么显式 `device='cpu'`，要么从已有张量**继承设备**：`torch.arange(..., device=x.device)`。
- 逐元素地用 Python 迭代 CUDA 张量（`for v in cuda_tensor`）每次迭代都伴随一次主机-设备同步，慢得离谱——所以"要在 Python 里逐元素处理"的代码通常先把张量 `.cpu()`。

### 2.4 浮点精度速查

| dtype | 尾数位数 | 十进制有效位 | 备注 |
|---|---|---|---|
| `bfloat16` | 8 | ~2–3 | 动态范围与 fp32 相同，精度很差 |
| `float16` | 10 | ~3–4 | |
| `float32` | 23 | ~7 | 整数可精确表示到 \(2^{24} \approx 1.68\times 10^7\) |
| `int64` | — | — | 计数精确，但做除法会被提升为浮点 |

EPLB 的输入 `weight` 是负载统计，可能是 token 计数（`int64`）或 GPU 上汇总来的半精度统计量；下游要做除法（`weight / logcnt`）和排序，这就引出入口的 `.float()` 规范化。

### 2.5 git 考古

用只读命令回看历史：`git log --oneline`、`git show <commit>` 看 diff、`git show <commit>:<file>` 取历史版本文件。本讲的两个案例：

| commit | 日期 | 内容 |
|---|---|---|
| `e1100fe` | 2025-03-21 | 全局策略从 `replicate_experts(weight, num_replicas)` 改为 `rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)`（close #14） |
| `d52c72d` | 2025-03-24（当前 HEAD） | 给 Step 3 中的 `torch.arange` 补上 `device=group_pack_index.device` |

### 2.6 承接前讲

你需要 u2 系列建立的映射链术语（log/mlog/phy/pphy、互逆置换、映射复合）和 u3-l2 的不均衡度指标：

\[
\mathrm{IB} \;=\; \frac{\max_g \mathrm{load}_g}{\mathrm{mean}_g \mathrm{load}_g} \;\ge\; 1
\]

## 3. 本讲源码地图

整个仓库只有一个源码文件 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L1-L165)（165 行），外加 [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L1-L67)。

| 位置 | 函数/片段 | 本讲关注点 |
|---|---|---|
| [eplb.py:L5-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L41) | `balanced_packing` | 双重 Python 循环、`.cpu()` 与 `device='cpu'`、平凡分支与循环分支的设备不一致 |
| [eplb.py:L44-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71) | `replicate_experts` | 已行维批量化的循环、`weight / logcnt` 的数值含义、向量化变体 |
| [eplb.py:L74-L129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L129) | `rebalance_experts_hierarchical` | 设备传播链、`d52c72d` 修复处（L123-L125）、`e1100fe` 退化复用 |
| [eplb.py:L131-L162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162) | `rebalance_experts`（入口） | L149 `weight.float().cpu()`、策略分派 |
| [README.md:L27-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L27-L31) | Global 策略说明 | `e1100fe` 修复的行为背景 |

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**balanced_packing**（性能热点）、**replicate_experts**（向量化样板与优化空间）、**rebalance_experts_hierarchical**（设备一致性与两次 commit 的演化现场）。

---

### 4.1 balanced_packing：全仓库的性能热点

#### 4.1.1 概念说明

`balanced_packing` 是 EPLB 里唯一一段**双层纯 Python 循环**。它回答"放在哪"，在层级策略里被调用两次（组→节点、物理专家→GPU）。理解它的复杂度，就理解了整个 `rebalance_experts` 的耗时上限；反过来，如果将来你要把 EPLB 嵌进参数搜索或仿真扫描（比如 u3-l2 那种成百上千次调用的评测网格），这里就是第一个要优化的地方。

#### 4.1.2 核心流程与复杂度

记输入 `weight` 形状为 \([X, n]\)（\(X\) 行、\(n\) 个物品）、包数为 \(m\)。循环结构：

```
for 每一行 i (X 次):
    初始化 pack_weights / pack_items（Python 列表）
    for 该行每个物品 group（按权重降序，n 次）:
        pack = 未满的包里最轻的那个（min 扫描，O(m) 次比较）
        pack_index[i, group] = pack      # 张量逐元素赋值
        rank_in_pack[i, group] = ...
        pack_weights[pack] += weight[i, group]
```

Python 级操作总量：

\[
T_{\text{pack}} \;=\; \Theta(X \cdot n \cdot m) \quad \text{次比较} \;+\; 2Xn \text{ 次张量逐元素赋值}
\]

代入层级策略两次调用的实际规模（\(L\) 层、\(E\) 个逻辑专家、\(G\) 组、\(N\) 节点、\(P\) GPU、\(M\) 个物理专家）：

| 调用点 | 行数 \(X\) | 物品 \(n\) | 包 \(m\) | Python 物品迭代次数 \(X \cdot n\) |
|---|---|---|---|---|
| Step 1 组→节点 | \(L\) | \(G\) | \(N\) | \(L \cdot G\) |
| Step 3 专家→GPU | \(L \cdot N\) | \(M/N\) | \(P/N\) | \(L \cdot M\) |
| Step 2 `replicate_experts` | \(L \cdot N\) | \(E/N\) | — | \((M-E)/N\) **轮**（见 4.2） |

以 DeepSeek-V3 量级（\(L=60,\ E=256,\ G=8,\ N=4,\ P=64,\ M=320\)）为例：

- Step 1：\(60 \times 8 = 480\) 次物品迭代；
- Step 3：\(60 \times 320 = 19\,200\) 次物品迭代，每次还要在 16 个包里做 `min` 扫描；
- Step 2：仅 \(16\) 轮 Python 循环。

**结论（待本地验证量级）：Step 3 的装箱循环比复制循环多约三个数量级的 Python 迭代，是整条链路当之无愧的瓶颈。** 假设每次物品迭代耗费 2–10 μs（生成器 + `min` + 两次张量赋值 + 0 维张量隐式提升），Step 3 约需 0.05–0.2 s。

还有一个工程判断：EPLB 的重排多久跑一次？README（[README.md:L11-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L11-L13)）说负载预测不在本仓库范围、常用历史统计滑动平均——这意味着重排是分钟级甚至更低频的决策，0.1 s 的耗时相对权重迁移开销可以忽略。**先量再优化，并且问一句"调用频率是多少"**，这是本讲想传递的第一条工程直觉。

#### 4.1.3 源码精读

排序与强制搬 CPU 的那一行：

```python
indices = weight.float().sort(-1, descending=True).indices.cpu()
```

[eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)：排序在输入设备上完成，但索引立刻 `.cpu()`——因为接下来的双层循环要在 Python 里逐元素读取这个张量（`for group in indices[i]`），逐元素迭代 CUDA 张量会带来灾难性的同步开销。

双重循环本体：

```python
for i in range(num_layers):
    pack_weights = [0] * num_packs
    pack_items = [0] * num_packs
    for group in indices[i]:
        pack = min((i for i in range(num_packs) if pack_items[i] < groups_per_pack),
                   key=pack_weights.__getitem__)
        assert pack_items[pack] < groups_per_pack
        pack_index[i, group] = pack
        rank_in_pack[i, group] = pack_items[pack]
        pack_weights[pack] += weight[i, group]
        pack_items[pack] += 1
```

[eplb.py:L30-L40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L40)。逐行看开销来源：

- `for group in indices[i]`：每次迭代产出一个 **0 维张量**（不是 Python int），后面拿它做索引（`pack_index[i, group]`）要走张量索引协议。
- `min(生成器, key=...)`：每次物品迭代都重建生成器并对至多 \(m\) 个包调用 `pack_weights.__getitem__`。
- `pack_weights[pack] += weight[i, group]`：右值是 0 维张量，`float + 0维张量` 的结果还是 0 维张量——**`pack_weights` 的元素在第一次累加后就从 Python float 变成了张量**，此后的比较都在张量层面进行，进一步抬高常数。
- `pack_index[i, group] = pack`：每次都是一个独立的张量 setitem 调用。

以及输出缓冲的构造：

```python
pack_index = torch.full_like(weight, fill_value=-1, dtype=torch.int64, device='cpu')
```

[eplb.py:L28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28)：注意 `full_like` 显式传了 `device='cpu'`，**覆盖**了 `weight` 的设备。这一点在 4.3 的设备审计里会反过来咬人。

#### 4.1.4 代码实践：装箱微基准

1. **实践目标**：用实测确认 \(T_{\text{pack}}\) 随规模的增长，验证"Step 3 是瓶颈"的推演。
2. **操作步骤**（示例代码，保存为 `bench_packing.py`）：

   ```python
   import timeit, torch
   from eplb import balanced_packing

   torch.manual_seed(0)
   # Step 1 规模：X=60 行, n=8 物品, m=4 包
   w1 = torch.rand(60, 8)
   # Step 3 规模：X=240 行, n=80 物品, m=16 包
   w3 = torch.rand(240, 80)

   t1 = min(timeit.repeat(lambda: balanced_packing(w1, 4), number=5, repeat=5))
   t3 = min(timeit.repeat(lambda: balanced_packing(w3, 16), number=5, repeat=5))
   print(f"step1-like: {t1/5:.4f}s   step3-like: {t3/5:.4f}s   ratio: {t3/t1:.1f}x")
   ```

3. **需要观察的现象**：两种规模耗时的比值。
4. **预期结果**：物品迭代数之比为 \(19200/480 = 40\times\)，`min` 扫描宽度之比为 \(16/4 = 4\times\)，因此总比值应落在 \(40\times\) 到 \(160\times\) 之间；Step 3 规模的绝对耗时在几十到几百毫秒量级。**待本地验证**（具体数字依赖机器与 PyTorch 版本）。
5. 顺带确认 `assert num_groups % num_packs == 0`（[eplb.py:L19](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19)）对非法参数的报错行为。

#### 4.1.5 小练习与答案

**练习 1**：把 Step 3 的循环改成"行维批量化"（只循环 \(n\) 步、每步处理所有行）后，Python 迭代次数变成多少？瓶颈移到哪里？

答案：循环次数从 \(X \cdot n\) 降为 \(n\)（本例 \(19200 \to 80\)），每步是一次 `[X, m]` 的 masked `min`。瓶颈从"Python 迭代"转移到 \(n\) 次张量小算子的调度开销（每次约几十微秒），总量约 \(n \times\) 几十 μs，通常仍快 1–2 个数量级。参考实现见 5. 综合实践的进阶部分。

**练习 2**：`balanced_packing` 里为什么 `indices` 要 `.cpu()`，而 `pack_index` 直接 `full_like(..., device='cpu')`？

答案：两者是同一个决策的两面——循环体在 Python 层逐元素读写，读写对象必须在 CPU。`indices` 是排序的产物（在输入设备上算完再搬），`pack_index` 是新建的输出缓冲，干脆直接建在 CPU 上。

**练习 3**：行维批量化版本用 `torch.min(dim=-1)` 选包，与原版 `min(..., key=...)` 在并列（两个包负载相同）时的选择是否一定一致？

答案：不一定。Python `min` 对并列取**先遍历到**的（编号较小的）包；张量 `min(dim)` 的并列索引行为需要查阅当前 PyTorch 文档确认（`argmax` 文档明确取首个最大值，`min(dim)` 的保证需验证）。因此批量化版本在浮点随机负载（几乎不并列）下可与原版逐位一致；在整数负载（并列常见）下应退而要求"方案级等价"——输出都是合法装箱且目标值相同。

---

### 4.2 replicate_experts：已经向量化的循环还能再快吗

#### 4.2.1 概念说明

`replicate_experts` 是本仓库**向量化写得最标准**的函数：Python 循环只跑"要复制多少个"（\(R = num\_phy - num\_log\)）轮，所有行（层×节点）在每轮里用一次批量化算子并行处理。把它与 `balanced_packing` 对照，正好构成"该怎么写"与"还能怎么改"的两个样本。

#### 4.2.2 核心流程与复杂度

```
q = weight / logcnt                 # [X, num_log] 商矩阵：每副本期望负载
for 第 i 个冗余位 (共 R 轮):
    idx = argmax(q)                 # 每行各自复制商最大的专家
    phy2log[:, i] = idx
    rank[:, i]   = logcnt[idx]      # 该专家的第几个副本（读旧值）
    logcnt[idx] += 1                # 计数 +1，下一轮商自动变小
```

每轮做一次 \([X, num\_log]\) 的除法 + `max`，共：

\[
T_{\text{rep}} \;=\; \Theta(R \cdot X \cdot num\_log) \quad \text{次张量元素操作，但只有 } R \text{ 次 Python 迭代}
\]

层级策略里 \(X = L \cdot N\)、\(num\_log = E/N\)、\(R = (M-E)/N\)，代入 60/256/320/4 的例子：**16 轮 Python 循环，每轮一个 \([240, 64]\) 的小算子**——这与 Step 3 装箱的 19 200 次迭代完全不在一个量级。

为什么不能把 \(R\) 轮也消掉？因为存在**数据依赖**：第 \(i+1\) 轮的 `argmax` 依赖第 \(i\) 轮对 `logcnt` 的更新（复制会摊薄该专家的商）。这是"贪心序列内在的时序依赖"，只能减少每轮成本，不能直接折叠成一次算子。

#### 4.2.3 源码精读

```python
device = weight.device
phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
arangen = torch.arange(n, dtype=torch.int64, device=device)
for i in range(num_log, num_phy):
    redundant_indices = (weight / logcnt).max(dim=-1).indices
    phy2log[:, i] = redundant_indices
    rank[:, i] = logcnt[arangen, redundant_indices]
    logcnt[arangen, redundant_indices] += 1
```

[eplb.py:L61-L70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L61-L70)。三个观察：

1. **设备继承的样板**：L61 先记下 `device = weight.device`，L62-L65 的四个张量全部显式带上——这个函数对任意设备都是自洽的（与 `balanced_packing` 形成对比，见 4.3）。
2. **`weight / logcnt` 每轮整表重算**：整除运算只对"被复制的那一列"发生了变化，其余列的商没变却也被重算了一遍。这就是可优化点。
3. **`rank` 的读取顺序**：先读 `logcnt`（旧值）再 `+=1`，所以 `rank` 是 0 基的副本序号——改写时必须保持这个顺序，否则 `log2phy` 的槽位地址会错位。

#### 4.2.4 代码实践：增量商变体与等价性验证

1. **实践目标**：写一个只更新"被选中列"的 `replicate_experts` 变体，用 u3-l1 的测试证明它与原版等价，再比较速度。
2. **操作步骤**（示例代码）：

   ```python
   # replicate_fast.py（示例代码）
   import torch
   from typing import Tuple

   def replicate_experts_fast(weight: torch.Tensor, num_phy: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
       n, num_log = weight.shape
       num_redundant = num_phy - num_log
       assert num_redundant >= 0
       device = weight.device
       phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
       rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
       logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
       arangen = torch.arange(n, dtype=torch.int64, device=device)
       q = weight / logcnt                       # 商矩阵只构建一次
       for i in range(num_log, num_phy):
           idx = q.max(dim=-1).indices           # 每轮只做 max，不再整表相除
           phy2log[:, i] = idx
           rank[:, i] = logcnt[arangen, idx]
           logcnt[arangen, idx] += 1
           q[arangen, idx] = weight[arangen, idx] / logcnt[arangen, idx]  # 只刷新被选中列
       return phy2log, rank, logcnt
   ```

   等价性验证分两层：

   ```python
   # 第一层：直接逐位对比（随机浮点负载，并列概率极低）
   from eplb import replicate_experts
   for seed in range(50):
       w = torch.rand(13, 64)                 # 奇数行数更易暴露边界
       a = replicate_experts(w, 96)
       b = replicate_experts_fast(w, 96)
       assert all(torch.equal(x, y) for x, y in zip(a, b)), seed

   # 第二层：接入完整管线跑 u3-l1 的 test_eplb.py（check_invariants 全量参数矩阵）
   ```

3. **需要观察的现象**：50 组随机种子下 `torch.equal` 是否全部通过；u3-l1 的七条不变量是否仍然全绿。
4. **预期结果**：理论上应**逐位一致**——商 \(q_{r,c} = w_{r,c}/\text{logcnt}_{r,c}\) 只在 logcnt 变化时改变，而变体恰在更新时用相同操作数重算该列，浮点除法对相同操作数结果确定；`max` 看到的矩阵逐元素相同，索引自然相同。速度方面：每轮省掉一次 \([X, num\_log]\) 除法，但 `max` 仍在，**预计只有小幅提升**（也许 1.2–2×，待本地验证）——这恰好印证 4.2.2 的判断：这个函数本来就快，优化它的学习价值大于收益价值。
5. **注意事项**：若额外构造整数负载用例，`max` 的并列选择可能使两版本"逐位不同但方案等价"，此时应对比 `(logcnt, 排序后的 phy2log)` 而非逐位相等。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `replicate_experts` 的循环次数与层数无关（在层级策略中是 \((M-E)/N\) 而不是 \(L(M-E)/N\)）？

答案：因为行维（层×节点）被压进了张量的第一维，每轮 `max(dim=-1)` 对所有行各自独立地选出要复制的专家。这正是 `balanced_packing` 缺失的"行维批量化"。

**练习 2**：能否把每轮的 `max` 也省掉（例如缓存上一轮的最大值，只比较变化的那一列）？

答案：思路可行但实现精巧。某列被复制后其商下降，其余列不变，因此新最大值 = max(其余列的旧最大值, 该列新商)。但"其余列的旧最大值"需要在剔除一列后仍然有效，通常要维护每行的前两小/前两大值，更新逻辑复杂且仍是 \(R\) 轮 Python 循环；在当前规模（\(R\) 只有十几）下得不偿失。可作为思路练习，不建议落地。

**练习 3**：如果 `weight` 是 `bfloat16` 直接喂给本函数（不经过入口的 `.float()`），可能出现什么问题？

答案：`weight / logcnt` 会在 bf16 精度下进行，~3 位有效数字使得不同的专家商频繁并列或失真，`argmax` 的选择变得不稳定；更糟的是 `weight / logcnt` 中 int64 的 logcnt 与 bf16 相除会提升到 bf16，损失进一步放大。这就是入口 L149 `weight.float()` 存在的理由之一。

---

### 4.3 rebalance_experts_hierarchical：设备一致性与数值规范化

#### 4.3.1 概念说明

这个函数是两次真实演化的现场：`e1100fe` 让全局策略复用它（见 4.4），`d52c72d` 修掉了它 Step 3 里的一个设备缺失。本节做两件事：给全文件做一次**设备传播审计**，并解释入口的**数值规范化**。核心结论先摆出来：

- 入口 [eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 的 `weight = weight.float().cpu()` 是全库设备与数值策略的**总开关**：只要走公开 API，一切都在 CPU 上的 float32 进行。
- 但 `rebalance_experts_hierarchical` 是一个可以直接调用的库内函数；**绕过入口直接喂 CUDA 张量时，它的设备一致性并不完备**——这正是 `d52c72d` 所修 bug 的根源，而且修复后仍有遗留缺口（下面逐步推演）。

#### 4.3.2 核心流程：设备传播链

设备传播的规则很朴素：**新张量的设备 = 创建时显式指定的设备**，运算结果的设备 = 操作数的设备（不一致则报错）。全文件的设备决策点如下：

| 创建语句 | 位置 | 设备来源 |
|---|---|---|
| `torch.arange(..., device=weight.device)`（平凡分支） | [eplb.py:L23](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L23) | 跟随输入 |
| `torch.zeros_like(weight, ...)`（平凡分支） | [eplb.py:L24](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L24) | 跟随输入 |
| `sort(...).indices.cpu()` | [eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) | 强制 CPU |
| `torch.full_like(weight, ..., device='cpu')`（循环分支） | [eplb.py:L28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28) | **强制 CPU** |
| `device = weight.device` + 四个构造 | [eplb.py:L61-L65](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L61-L65) | 跟随输入 |
| `inverse` 里的 `device=perm.device` | [eplb.py:L100](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L100) | 跟随 perm |
| Step 1 的 `device=group_pack_index.device` | [eplb.py:L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L107) | 跟随 pack_index |
| Step 3 的 `device=group_pack_index.device`（**d52c72d 修复处**） | [eplb.py:L124](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L124) | 跟随 pack_index |
| 入口 `log2phy`/`arange` 的 `device=logcnt.device` / `device=log2phy.device` | [eplb.py:L158-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L161) | 跟随 logcnt |

关键发现：**`balanced_packing` 的输出设备依赖于走了哪个分支**——平凡分支（`groups_per_pack == 1`，[eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)）跟随输入设备，循环分支强制 CPU。而层级函数后续的 `log2mlog`、`pphy2mlog` 偏移等全部从 `group_pack_index.device` 继承设备。于是"直接用 CUDA 张量调用层级函数"会遇到三种情形（推演，**待本地验证**，需要 CUDA 环境）：

1. **Step 1 走循环分支**（`groups_per_node > 1`，即 \(G > N\)）：`mlog2log` 落在 CPU → [eplb.py:L112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112) 的 `weight.gather(-1, mlog2log)` 是 CUDA 权重 + CPU 索引 → RuntimeError。**修复前后都失败**。
2. **Step 1 平凡但 Step 3 走循环分支**（\(G = N\) 且 `phy_experts_per_gpu > 1`，含退化全局调用）：Step 3 返回 CPU 索引 → [eplb.py:L122](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L122) 的 `phy2mlog.gather(-1, pphy2phy)` 设备不匹配 → RuntimeError。**修复前后都失败**。
3. **两步都走平凡分支**（`phy_experts_per_gpu == 1`，即 \(M = P\)）：全链路保持 CUDA，唯一断点是 [eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125) 的节点基地址偏移——**修复前**这里创建的 `torch.arange` 没带 device（落在 CPU），CUDA + CPU 相加直接报错；**`d52c72d` 修复后**才能全程 CUDA 跑通。

这正是 `d52c72d` 的 bug 全貌：它藏在"所有装箱都退化为恒等置换"的最窄路径里，只有直接调用层级函数且每 GPU 恰好一个物理专家时才触发；走公开入口的用户永远被 L149 的 `.cpu()` 保护着。

数值规范化方面，L149 的 `weight.float()` 解决三件事：

- **半精度输入**（bf16/fp16 的统计量）：升到 float32 后 `weight / logcnt`（L67）、`tokens_per_mlog / mlogcnt`（L117）和 L27 的排序才有足够的有效位做区分；
- **整数计数**（int64 token 数）：统一成 float32，避免不同调用方 dtype 混入导致的行为分叉。注意 float32 只能精确表示 \(2^{24}\) 以内的整数，若统计窗口累计 token 数超过约 \(1.68\times10^7\)，个别计数会有舍入——对"只需相对大小"的负载排序通常无碍，但值得知情（待本地验证对具体分布的影响）；
- **确定性**：统一在 CPU 上计算使结果与运行环境无关；另外 `torch.sort` 默认 `stable=False`，权重完全并列的物品顺序未严格指定——不影响装箱质量，但做"逐位回归对比"时要意识到这一点。

#### 4.3.3 源码精读：d52c72d 的 diff

修复前（`e1100fe` 版本）：

```python
pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) +
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes).view(1, -1, 1)).flatten(-2)
```

修复后（当前 HEAD）：

```python
pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) +
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                          device=group_pack_index.device).view(1, -1, 1)).flatten(-2)
```

[eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)：这一段在做"节点内 mlog 编号 + 节点基地址 → 全局 mlog 编号"的加法（u2-l5 讲过的带步长 `arange`）。加号的左边跟随装箱结果的设备，右边修复前是默认 CPU。**修复方式是"从上游已有张量继承设备"**（`group_pack_index.device`），与 L100、L107 的既有写法保持一致——这是库代码里创建伴生张量的标准姿势：不要假设 CPU，也不要新造设备参数，从数据流上游继承。

#### 4.3.4 代码实践：设备审计与 bug 复现

1. **实践目标**：亲自复现 `d52c72d` 修复的 bug，并产出一张设备传播审计表。
2. **操作步骤**：
   - 取历史版本到独立文件（不动源码）：`git show e1100fe:eplb.py > eplb_prefix.py`；
   - 在有 CUDA 的机器上运行（示例代码）：

     ```python
     import torch, eplb_prefix          # 修复前版本
     w = torch.rand(60, 64, device="cuda")
     # E=64, M=P=128, 退化全局参数：Step1/Step3 均平凡, phy_experts_per_gpu=1
     eplb_prefix.rebalance_experts_hierarchical(w, 128, 1, 1, 128)
     ```

   - 换成当前版本再跑一次：`import eplb`，同样调用；
   - 按本节三情形构造另外两个用例（如 `rebalance_experts_hierarchical(w_cuda, 320, 8, 4, 64)`），记录各自在哪一行报错。
3. **需要观察的现象**：修复前版本抛出设备不匹配的 RuntimeError；当前版本情形 3 跑通且输出张量在 CUDA 上；情形 1、2 在两个版本都在 gather 处报错。
4. **预期结果**：与 4.3.2 的推演一致——即当前 HEAD 的层级函数**仍不是**任意设备安全的，只有入口路径保证 CPU 一致性。**待本地验证**（本讲义写作环境无 GPU，以上为基于设备规则的推演）。
5. 无 GPU 时改为**阅读型审计**：对照 4.3.2 的表格，逐行标注 `rebalance_experts_hierarchical` 中每个中间张量（`tokens_per_group`/`log2mlog`/`mlog2log`/`tokens_per_mlog`/`phy2mlog`/`tokens_per_phy`/`phy2pphy`/`pphy2phy`/`pphy2mlog`/`pphy2log`/`logcnt`）在"CPU 输入"与"CUDA 输入"两种情景下的设备，写出你自己的传播表。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `d52c72d` 的修复选择 `device=group_pack_index.device`，而不是 `device=pphy2mlog.device` 或干脆 `device=weight.device`？

答案：三者在此处等价（全链路一致时都指向同一设备），但语义上这个 `arange` 是给"按节点分块重编号"加基地址的伴生张量，其自然主人是决定分块布局的 `group_pack_index`；且 `weight` 在本函数签名里未必存在同设备保证（中间张量都经过装箱函数），从"最近的上游布局张量"继承最稳。工程上更重要的是**选定一种"从上游继承"的约定并全文件贯彻**——eplb.py 在 L100/L107/L124 用的是同一招。

**练习 2**：把入口的 `weight.float().cpu()` 改成 `weight.cpu().float()`，行为有区别吗？

答案：功能上几乎无区别（最终都是 CPU 上的 float32）。细微差别在于执行顺序：前者在原设备上先做一次 dtype 转换（若输入是 CUDA 半精度，会在 GPU 上产生一份 float32 临时张量再搬运），后者先搬运再转换。对本仓库的规模两者都可忽略；这类"顺序无关紧要"的判断本身也是性能审读的一部分。

**练习 3**：如果要让 `rebalance_experts_hierarchical` 真正支持 CUDA 直调，最小改动集是什么？

答案：统一 `balanced_packing` 的设备策略——循环分支不再强制 CPU（L27/L28 改为跟随 `weight.device`，同时把双层 Python 循环改为 4.1.5 练习 1 的行维批量化版本，否则逐元素迭代 CUDA 张量慢到不可用），并保证 `indices`/`pack_index`/`rank_in_pack` 与输入同设备；其余位点已经全部"从上游继承"，会自动跟上。改动后用 4.3.4 的三个用例回归。

---

### 4.4 工程演化案例：e1100fe 全局策略补上 GPU 级均衡

#### 4.4.1 概念说明

`e1100fe`（close #14）只有一行实质改动，却是一次教科书式的重构：**用退化参数复用既有实现，替代功能残缺的专用实现**。它也是"算法输出质量"层面的性能问题——不是算得慢，而是**算出来的方案让 GPU 负载不均**。

#### 4.4.2 核心流程：修复前后对比

修复前，全局策略直接调用 `replicate_experts(weight, num_replicas)`：

```python
phy2log, phyrank, logcnt = replicate_experts(weight, num_replicas)
```

问题在于 `replicate_experts` 只回答"**复制谁**"（[eplb.py:L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62)：`phy2log` 的前 \(E\) 列就是 \(0..E-1\)，冗余副本按贪心顺序追加在尾部槽位），完全不回答"**放在哪**"。消费方按"槽位连续切块"把物理专家分给 GPU：GPU \(g\) 拿走第 \(g\cdot M/P\) 到 \((g+1)\cdot M/P - 1\) 号槽位。于是 GPU 负载等于"逻辑编号恰好相邻的专家负载之和"——与均衡毫无关系。

以 README 的第一层数据（[README.md:L43-L44](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L43-L44)）作对照：若 GPU0 分到逻辑专家 {0,1}（负载 \(90+132=222\)），而某个 GPU 分到 {6,7}（负载 \(39+4=43\)），单层内 GPU 负载差超过 5 倍。复制了冗余专家却不做装箱，均衡收益无从谈起。

修复后：

```python
phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

[eplb.py:L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L156)：传入 `num_groups=1, num_nodes=1` 的退化参数后，层级函数的三步自动退化为：

- **Step 1 退化为恒等置换**：单组单节点，`balanced_packing` 走 `groups_per_pack == 1` 平凡分支，`log2mlog` 就是单位矩阵；
- **Step 2 变为全局复制**：不再按节点切分，`replicate_experts` 在全部逻辑专家上贪心；
- **Step 3 变为 GPU 级装箱**：`balanced_packing(tokens_per_phy, num_gpus)` 真正按单副本负载把物理专家装到 GPU——这正是修复标题"add gpu-level load balance for global policy"的含义，也让代码与 README 的描述（[README.md:L27-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L27-L31)：全局策略"复制后打包到 GPU"）重新一致。

这次演化的第二层教训藏在时间线里：`e1100fe`（3 月 21 日）让全局流量路过层级函数的 Step 3，三天后 `d52c72d`（3 月 24 日）就修掉了那条路径上的设备缺失。**复用会同时传播特性与缺陷**——新路径触发了旧代码里潜伏的 bug，这是重构后需要回归测试的直接论据（u3-l1 的不变量测试正是这类回归的护城河）。

顺带一提，该 diff 还删除了 `balanced_packing` docstring 里的一个空行——读 commit 时把这种化妆品改动与实质改动区分开，也是源码考古的基本功。

#### 4.4.3 源码精读

修复生效的机制在 Step 3 的槽位编码：

```python
tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
```

[eplb.py:L117-L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117-L119)：退化参数下 `num_gpus // num_nodes = num_gpus`，装箱以单副本负载 `tokens_per_phy` 为权重、以 `phy_experts_per_gpu = M/P` 为基数约束，直接输出"物理专家 → GPU 槽位"的置换。对比修复前——这条链路根本不存在，`replicate_experts` 的输出被原样当作槽位表消费。

#### 4.4.4 代码实践：量化 e1100fe 的均衡收益

1. **实践目标**：用 u3-l2 的 IB 指标，实测"复制但不装箱"与"复制且装箱"的 GPU 负载差。
2. **操作步骤**（示例代码，纯 CPU 可跑）：

   ```python
   # bench_e1100fe.py（示例代码）
   import torch
   from eplb import replicate_experts, rebalance_experts

   L, E, M, P = 60, 256, 320, 64
   torch.manual_seed(0)
   w = torch.distributions.Pareto(1.0, 1.3).sample((L, E))   # 长尾负载

   def gpu_ib(phy2log, logcnt, w, P):
       per_copy = w.gather(-1, phy2log) / logcnt.gather(-1, phy2log)  # 每副本负载
       load = per_copy.view(L, P, -1).sum(-1)                        # [L, P] 每 GPU 负载
       return (load.max(-1).values / load.mean(-1).values)

   # 修复前：只复制（隐式连续切块放置）
   p2l_old, _, cnt_old = replicate_experts(w, M)
   # 修复后：全局策略（G=3 不被 N=2 整除即可触发）
   p2l_new, _, cnt_new = rebalance_experts(w, M, 3, 2, P)

   print("before e1100fe  IB:", gpu_ib(p2l_old, cnt_old, w, P).mean().item())
   print("after  e1100fe  IB:", gpu_ib(p2l_new, cnt_new, w, P).mean().item())
   ```

   （`before` 分支严格对应历史行为；`after` 分支用当前入口以 `num_groups=3, num_nodes=2` 触发全局策略。）
3. **需要观察的现象**：两版的平均 IB。
4. **预期结果**：`after` 的 IB 明显更接近 1（受基数约束 `phy_experts_per_gpu` 的残余不均衡限制，不会等于 1，见 u3-l2 的分析）；`before` 的 IB 在长尾负载下可能显著偏离 1。**具体数值待本地验证**。
5. 思考题式的观察点：固定 `M` 增大 `P`（每 GPU 槽位变少），两版 IB 各如何移动？

#### 4.4.5 小练习与答案

**练习 1**：退化调用 `(weight, M, 1, 1, P)` 下，Step 1 产生的 `log2mlog` 是什么？开销多大？

答案：单位置换。`tokens_per_group = weight.unflatten(-1, (1, E)).sum(-1)` 形状 `[L,1]`，`balanced_packing` 走平凡分支输出全 0 的 `group_pack_index` 与全 0 的 `rank_in_pack`，于是 `log2mlog = ((0*1+0)*E + arange(E)).flatten` = 每行 `arange(E)`。开销是几次小张量运算，可忽略。

**练习 2**：如果不做这次重构，而是给 `replicate_experts` 的输出"事后补一次装箱"，等价吗？

答案：数学上可以等价（把 `phy2log` 当作物品列表再调 `balanced_packing`），但要自己把 `rank`、`logcnt` 与新槽位对齐、重建逆映射，等于把 Step 3 的合成逻辑再写一遍。退化复用把这些对齐全部交给已经测试过的层级实现——**少一条代码路径就是少一类 bug**；代价是全局策略多了几次恒等置换的开销（可忽略）和"必须理解层级函数才能读懂全局策略"的心智成本。

**练习 3**：`e1100fe` 之后，全局策略的输出还满足 u3-l1 的哪些不变量？哪条不再适用？

答案：形状、值域、守恒（每层副本总数 = M）、覆盖计数、互逆一致、布局契约都满足；不再适用的是"组-节点对齐"——全局策略本来就无视分组（\(G=1\) 时约束自动平凡），这也是 u3-l1 测试矩阵里层级与全局分支要分别构造参数的原因。

---

## 5. 综合实践

**任务：给 EPLB 做一次完整的性能体检，并交付一个经过验证的加速变体。**

把 4.1.4、4.2.4、4.4.4 的零件组装成一个脚本 `perf_audit.py`（示例代码框架）：

```python
import timeit, torch, eplb
from eplb import balanced_packing, replicate_experts
from replicate_fast import replicate_experts_fast          # 4.2.4 的变体

L, E, G, N, P, M = 60, 256, 8, 4, 64, 320                 # DeepSeek-V3 量级
torch.manual_seed(0)
w = torch.distributions.Pareto(1.0, 1.3).sample((L, E))

# ① 整体耗时（两种策略各测）
for tag, g, n in [("hierarchical", G, N), ("global(3,2)", 3, 2)]:
    t = min(timeit.repeat(lambda: eplb.rebalance_experts(w, M, g, n, P), number=3, repeat=5))
    print(f"{tag:15s} {t/3:.3f}s")

# ② 分步耗时：把 rebalance_experts_hierarchical 的三步拷贝进脚本并插桩
#    （对照 eplb.py L103-L128 逐行拷贝，在 Step1/Step2/Step3 前后取 time.perf_counter）

# ③ 变体等价性 + 速度（4.2.4 的两层验证），再统计：
#    - 原 vs 变体的 replicate_experts 耗时比
#    - 变体在全管线中的占比变化
```

交付物与检查清单：

1. **分步耗时表**：Step 1 / Step 2 / Step 3 各占多少，验证"Step 3 装箱主导"的预判（4.1.2）。
2. **等价性报告**：50 组随机种子的 `torch.equal` 断言 + u3-l1 `test_eplb.py` 全量参数矩阵通过。
3. **速度对比**：变体相对原版的加速比，以及它对整条管线总耗时的改善（预期很小——说明瓶颈不在这里，把优化预算留给装箱）。
4. **进阶（可选）**：实现 4.1.5 练习 1 的行维批量化 `balanced_packing`，先在随机浮点负载上与原版做逐位对比，再用"方案级等价"（装箱目标值相同 + u3-l1 不变量全过）兜底，报告它对 Step 3 的加速。
5. **结论段**：用一段话回答——"若把 EPLB 嵌入每分钟一次的在线重排，值得优化吗？嵌入千次级参数扫描呢？"（提示：对照 4.1.2 关于调用频率的讨论。）

所有计时数字均以本地实测为准，本讲义给出的量级只是预判。

## 6. 本讲小结

- **瓶颈定位**：`balanced_packing` 的 \(\Theta(X \cdot n \cdot m)\) 双重 Python 循环（尤其 Step 3 的 \(L \cdot M\) 次物品迭代）主导耗时；`replicate_experts` 已行维批量化，只有 \(R\) 轮循环，不是瓶颈。
- **数值规范化**：入口 L149 `weight.float().cpu()` 一行同时解决半精度精度、整数计数统一与跨设备确定性三件事，是全库的数值/设备总开关。
- **设备一致性**：eplb.py 的约定是"伴生张量从上游继承设备"（L100/L107/L124）；但 `balanced_packing` 循环分支强制 CPU、平凡分支跟随输入，导致层级函数在 CUDA 直调时仍有一段不完备（情形 1/2 在 gather 处报错）。
- **d52c72d**：修复了 Step 3 节点基地址 `arange` 缺 device 的 bug，触发路径是"CUDA 直调 + 每 GPU 一个物理专家"的最窄分支。
- **e1100fe**：全局策略从"只复制不装箱"改为退化参数 `(M, 1, 1, P)` 复用层级实现，补上 GPU 级均衡；复用传播特性也传播缺陷，三天后的 d52c72d 即是证据。
- **工程方法论**：先测调用频率再谈优化；等价性验证分"逐位一致"与"方案级等价"两档；重构后必须跑不变量回归。

## 7. 下一步学习建议

- 下一讲 u3-l4 会把视角从库内移到库外：如何统计负载（滑动平均）、如何按 `phy2log`/`log2phy` 重排真实权重与路由表、以及重排频率与参数迁移开销的权衡——本讲的耗时测量将直接喂给那边的成本模型。
- 建议精读的源码：把 [eplb.py:L27-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L41) 的循环改写为行维批量化并跑通 u3-l1 测试，是通往 u3-l5（变体实战）的最佳热身。
- 延伸阅读：PyTorch 官方文档中 `torch.sort` 的 `stable` 参数、`argmax`/`min(dim)` 的并列值索引约定，以及 `full_like`/`empty_like` 的 `device` 覆盖语义——本讲多处结论依赖这些细节，值得逐一核实。
