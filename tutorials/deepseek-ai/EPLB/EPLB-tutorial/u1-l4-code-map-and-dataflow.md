# 代码地图：四个函数的分工与数据流

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `eplb.py` 中四个函数的调用关系图，说出每个函数的职责。
2. 列出每个函数的输入、输出张量形状，以及参数必须满足的整除约束。
3. 描述 hierarchical 策略「组打包到节点 → 节点内复制 → 物理专家打包到 GPU」三步流程中，各中间张量形状如何一步步演变。
4. 拿着一张自己整理的「函数与形状速查表」，为后续 u2 系列的逐函数精读做好准备。

本讲是**地图级**的纵览：只建立结构与数据流认知，不深入算法细节（贪心为什么有效、gather/scatter 怎么用，留给 u2 各讲展开）。

## 2. 前置知识

### 2.1 从上一讲带来的认知

上一讲（u1-l3）你已经跑通了 README 示例，并知道 `rebalance_experts` 返回的三个张量是同一份放置方案的三种读法：

- `phy2log`：正向表，物理槽位 → 逻辑专家编号；
- `logcnt`：每个逻辑专家的副本数；
- `log2phy`：反向表，逻辑专家 → 它的各个副本所在物理槽位（第三维长度为 `maxlogcnt`，没有副本的槽位用 `-1` 补齐）。

本讲我们把镜头拉远，看这份方案是**由哪些函数、经过怎样的数据流**算出来的。

### 2.2 命名约定：`A2B` 读作「A to B」

这份源码大量使用 `xx x2y` 形式的变量名，其中的 `2` 读作 **to**，表示一张映射表：

- `phy2log[i] = j` 表示「第 `i` 个物理专家对应逻辑专家 `j`」；
- `log2phy[l, e, r] = p` 表示「第 `l` 层逻辑专家 `e` 的第 `r` 个副本放在物理槽位 `p`」。

同一对方向还会出现**互逆的两张表**（如 `log2mlog` 与 `mlog2log`），它们互为逆置换。把「名字里的方向」和「下标含义」对上，是读这份代码最重要的基本功。

### 2.3 置换表与 gather 的直觉

一个长度为 \( n \) 的**置换**（permutation）就是一张重排表：`perm[i]` 告诉你位置 `i` 应该放原来的哪个元素。`torch.gather(input, -1, index)` 则是「按下标表取值」：结果的第 `i` 个元素是 `input[index[i]]`。本讲你只需要这两个直觉；手写逆置换、复合索引编码等技巧在 u2-l3 专门练习。

### 2.4 符号约定

本讲统一使用以下符号（与 README 示例对应）：

| 符号 | 含义 | README 示例取值 |
|---|---|---|
| \( L \) | MoE 层数 `num_layers` | 2 |
| \( E \) | 逻辑专家数 `num_logical_experts` | 12 |
| \( M \) | 物理专家总数 `num_replicas` | 16 |
| \( G \) | 专家组数 `num_groups` | 4 |
| \( N \) | 节点数 `num_nodes` | 2 |
| \( P \) | GPU 总数 `num_gpus` | 8 |

由此派生：`group_size` \( = E/G \)、`groups_per_node` \( = G/N \)、`phy_experts_per_gpu` \( = M/P \)。

## 3. 本讲源码地图

整个仓库的核心算法只有一个文件 `eplb.py`（共 165 行），结构如下：

| 行范围 | 内容 | 作用 |
|---|---|---|
| [eplb.py:L1-L3](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L1-L3) | 导入 | 仅依赖 `torch` 与标准库 `typing` |
| [eplb.py:L5-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L41) | `balanced_packing` | 把 \( n \) 个带权物品装箱到 \( m \) 个包，每包恰好 \( n/m \) 个 |
| [eplb.py:L44-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71) | `replicate_experts` | 把 \( num\_log \) 个逻辑专家复制成 \( num\_phy \) 个物理专家 |
| [eplb.py:L74-L129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L129) | `rebalance_experts_hierarchical` | 层级策略主流程：三步生成放置方案 |
| [eplb.py:L131-L162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162) | `rebalance_experts` | 唯一公开入口：分派策略并组装 `log2phy` |
| [eplb.py:L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164) | `__all__` | 只导出 `rebalance_experts`，其余三个函数是内部实现 |

四个函数的调用关系（箭头表示「调用」）：

```text
rebalance_experts  (唯一公开入口)
│  ├─ weight.float().cpu()                      # 一律搬到 CPU 计算
│  ├─ num_groups % num_nodes == 0 ?
│  │    ├─ 是 → rebalance_experts_hierarchical(weight, M, G, N, P)   # 层级策略
│  │    └─ 否 → rebalance_experts_hierarchical(weight, M, 1, 1, P)   # 全局策略（退化参数复用）
│  │              │
│  │              ├─ Step 1: balanced_packing(tokens_per_group, num_nodes)        # 组 → 节点
│  │              ├─ Step 2: replicate_experts(tokens_per_mlog, M // num_nodes)   # 节点内复制
│  │              └─ Step 3: balanced_packing(tokens_per_phy, P // num_nodes)     # 物理专家 → GPU
│  │              └─ (内部还定义并两次使用局部函数 inverse，构造逆置换)
│  └─ 用 scatter_ 组装 log2phy（含 -1 padding）
```

两个值得先记住的「地图级」观察：

1. **`balanced_packing` 被复用在两个层级**：Step 1 装的是「组 → 节点」，Step 3 装的是「物理专家 → GPU」。同一个装箱函数，换一组参数就换了一个粒度。
2. **全局策略没有独立实现**：入口在不可整除时直接以 `num_groups=1, num_nodes=1` 调用层级实现（[eplb.py:L154-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L154-L156)），此时「组打包到节点」退化成平凡步骤，只剩全局复制 + GPU 级装箱。

## 4. 核心概念与源码讲解

### 4.1 模块一：`balanced_packing` —— 带容量约束的贪心装箱

#### 4.1.1 概念说明

把 \( n \) 个带权重的物品装进 \( m \) 个包，要求：

- **硬约束**：每个包恰好装 \( n/m \) 个物品（数量均衡）；
- **软目标**：各包权重之和尽量接近（负载均衡）。

在 EPLB 中它是纯粹的「工具函数」，不关心物品是什么——Step 1 里物品是专家组、包是节点；Step 3 里物品是物理专家、包是 GPU。

#### 4.1.2 核心流程

1. 断言 \( n \bmod m = 0 \)，算出每包容量 `groups_per_pack`。
2. 若每包只装 1 个物品（\( m = n \)），直接返回恒等放置，不进循环。
3. 否则按权重**降序**排序，逐个物品放入「仍有容量且当前最轻」的包；记录包号 `pack_index` 和包内序号 `rank_in_pack`。

伪代码：

```text
for 每一行（每一层）:
    pack_weights ← 全 0, pack_items ← 全 0
    for 物品 in 按权重降序:
        pack ← 仍在 { 未满的包 } 中权重最小者
        pack_index[物品] ← pack
        rank_in_pack[物品] ← pack_items[pack]
        pack_weights[pack] += weight[物品];  pack_items[pack] += 1
```

直觉：先放大物品，再用小物品填缝——这是 LPT（最长处理时间优先）式贪心的变体，为何有效在 u2-l1 详述。

#### 4.1.3 源码精读

函数签名与文档字符串在 [eplb.py:L5-L17](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L17)：输入 `weight: [X, n]`（`X` 行是相互独立的装箱问题），返回 `pack_index: [X, n]` 与 `rank_in_pack: [X, n]`——**输出形状与输入完全相同**。

整除断言与每包容量的计算在 [eplb.py:L18-L20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L18-L20)：`X` 被解包为 `num_layers`，`n` 被解包为 `num_groups`（这两个名字透露了它的主要调用场景）。

平凡分支在 [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)：`groups_per_pack == 1` 时每个包恰好一个物品，包号就是物品自身的序号（`arange` 广播），包内序号全 0。

主循环在 [eplb.py:L30-L40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L40)，其中选包的一行是全函数最密集的表达式：

```python
pack = min((i for i in range(num_packs) if pack_items[i] < groups_per_pack), 
           key=pack_weights.__getitem__)
```

即「在未满的包里，挑权重最小的那个」。排序下标由 [eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 一次性算好。

#### 4.1.4 代码实践

1. **实践目标**：用一个小例子验证你读懂了输出语义。
2. **操作步骤**（示例代码，非项目原有）：

   ```python
   import torch, eplb

   weight = torch.tensor([[10, 30, 20, 40]])
   pack_index, rank_in_pack = eplb.balanced_packing(weight, num_packs=2)
   print(pack_index, rank_in_pack)
   ```

3. **需要观察的现象**：手工模拟排序序 `[3, 1, 2, 0]`（按 40, 30, 20, 10 降序）——物品 3 进包 0；物品 1 进当时更轻的包 1；物品 2 进仍是 30 < 40 的包 1；此时包 1 已满，物品 0 只能进包 0。
4. **预期结果**：`pack_index = [0, 1, 1, 0]`，`rank_in_pack = [1, 0, 1, 0]`，两包负载 50/50 完全均衡。再试 `eplb.balanced_packing(torch.rand(1, 10), num_packs=4)`，应触发 [eplb.py:L19](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19) 的 `AssertionError`（10 不能被 4 整除）。

#### 4.1.5 小练习与答案

**练习 1**：输入 `weight` 形状为 `[3, 12]`、`num_packs=3`，输出形状是什么？每包几个物品？
答案：两个输出都是 `[3, 12]`；每包 \( 12/3 = 4 \) 个。

**练习 2**：`num_groups=10, num_packs=4` 会发生什么？
答案：\( 10 \bmod 4 \ne 0 \)，[eplb.py:L19](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19) 断言失败，抛出 `AssertionError`。

**练习 3**：`num_packs` 等于物品总数时返回什么？
答案：走 [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 的平凡分支：`pack_index` 是 `arange` 广播（物品 `i` 进包 `i`），`rank_in_pack` 全 0。

### 4.2 模块二：`replicate_experts` —— 冗余副本的贪心分配

#### 4.2.1 概念说明

给定 \( num\_log \) 个逻辑专家的负载，复制出 \( num\_phy \) 个物理专家（\( num\_phy \ge num\_log \)），目标是最小化**所有副本中最大的单副本负载**。复制一个专家不会减少总计算量，只是把它的负载摊薄到多个副本上——这正是 u1-l1 建立的「冗余专家」策略的核心一步。

#### 4.2.2 核心流程

1. 初始化：物理槽位 \( 0 \ldots num\_log-1 \) 与逻辑专家一一对应（恒等映射），每个逻辑专家副本数为 1。
2. 循环 \( num\_phy - num\_log \) 次，每次复制一个冗余专家：
   - 计算 \( weight / logcnt \)（每个副本的期望负载）；
   - 选其最大者复制一份：写入 `phy2log`、记录该副本的序号 `rank`、`logcnt` 加一。

为什么选 \( weight/logcnt \) 最大者？直觉是「哪個专家摊到每个副本上的负载最重，就再给它加一个副本」，等价于不断拉平所有副本的期望负载；证明与反例讨论留到 u2-l2。

#### 4.2.3 源码精读

签名与文档字符串在 [eplb.py:L44-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L57)：输入 `weight: [X, num_log]`，输出三张表 `phy2log: [X, num_phy]`、`rank: [X, num_phy]`、`logcnt: [X, num_log]`。注意 `X` 在层级策略的调用里是 `num_layers * num_nodes`（见 4.3），即「每层的每个节点」独立做一次复制。

初始化在 [eplb.py:L58-L65](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L58-L65)：`phy2log` 用 `arange(num_phy)` 初始化——前 `num_log` 列正好是恒等映射，后面的列是占位符，会被循环覆盖。

循环体在 [eplb.py:L66-L70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L70)：

```python
for i in range(num_log, num_phy):
    redundant_indices = (weight / logcnt).max(dim=-1).indices
    phy2log[:, i] = redundant_indices
    rank[:, i] = logcnt[arangen, redundant_indices]
    logcnt[arangen, redundant_indices] += 1
```

三行分别完成「选出要复制的专家、记录它是第几个副本、副本数加一」。

#### 4.2.4 代码实践

1. **实践目标**：验证 \( weight/logcnt \) 贪心的行为。
2. **操作步骤**（示例代码）：

   ```python
   import torch, eplb

   weight = torch.tensor([[3, 9]])
   phy2log, rank, logcnt = eplb.replicate_experts(weight, num_phy=4)
   print(phy2log, rank, logcnt)
   ```

3. **需要观察的现象**：手算两轮循环——第 1 轮 \( [3/1, 9/1] \) 最大者在专家 1；第 2 轮 \( [3/1, 9/2] = [3, 4.5] \) 最大者仍是专家 1。
4. **预期结果**：`phy2log = [0, 1, 1, 1]`，`rank = [0, 0, 1, 2]`，`logcnt = [1, 3]`。此时每个副本的期望负载恰好都是 3（\( 9/3 \) 与 \( 3/1 \)），两专家负载完全拉平。若无法本地运行，此结果可按上述手算推导，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`weight` 形状 `[5, 20]`、`num_phy=26`，三个输出形状与冗余副本数？
答案：`[5, 26]`、`[5, 26]`、`[5, 20]`；冗余副本数 \( 26 - 20 = 6 \)。

**练习 2**：复制完成后，某个物理专家的期望负载怎么算？
答案：它对应逻辑专家的总负载除以副本数，即 \( weight[log] / logcnt[log] \)。

**练习 3**：循环体一共执行多少次？
答案：\( num\_phy - num\_log \) 次（[eplb.py:L66](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66) 的 `range(num_log, num_phy)`），每轮恰好新增一个副本。

### 4.3 模块三：`rebalance_experts_hierarchical` —— 层级策略主流程

#### 4.3.1 概念说明

这是把前两个工具函数串成完整放置方案的地方。它要同时满足两类约束：

- **负载约束**：各 GPU 的期望负载尽量均衡（用复制 + 装箱实现）；
- **拓扑约束**：同组专家（及其副本）落在同一节点，以配合 DeepSeek-V3 的组受限路由、减少跨节点流量（u1-l2）。

为此它采用三步层级流程：先把组分配到节点，再在节点内部复制，最后把物理专家装到 GPU。

#### 4.3.2 核心流程

以符号 \( (L, E, M, G, N, P) \) 表示，三步中张量形状的演变如下（右侧以 README 示例 \( L{=}2, E{=}12, M{=}16, G{=}4, N{=}2, P{=}8 \) 标注具体值）：

```text
weight [L, E]                                    (=[2,12])
  │ unflatten(-1,(G, group_size)).sum(-1)        每组内求和
  ▼
tokens_per_group [L, G]                          (=[2,4])
  │ balanced_packing(…, num_nodes)               ← Step 1：组打包到节点
  ▼
log2mlog / mlog2log [L, E]（互逆置换，节点分块编号）
  │ gather + view(-1, E/N)
  ▼
tokens_per_mlog [L·N, E/N]                       (=[4,6])
  │ replicate_experts(…, M/N)                    ← Step 2：节点内复制
  ▼
phy2mlog / phyrank [L·N, M/N]，mlogcnt [L·N, E/N] (=[4,8] / [4,6])
  │ (tokens_per_mlog / mlogcnt).gather(…)        每副本期望负载
  ▼
tokens_per_phy [L·N, M/N]                        (=[4,8])
  │ balanced_packing(…, P/N)                     ← Step 3：物理专家打包到 GPU
  ▼
phy2pphy / pphy2phy [L·N, M/N]（互逆置换）
  │ gather 链 + 节点偏移 + 逆置换
  ▼
pphy2log [L, M]，pphyrank [L, M]，logcnt [L, E]   (=[2,16] / [2,12])
```

三个关键的中转编号（从代码行为理解命名）：

- **`mlog`**：Step 1 之后按「节点分块」重新编号的逻辑专家。`log2mlog[log] = mlog`，其逆为 `mlog2log`。编号方式保证前 \( E/N \) 个 `mlog` 恰好属于节点 0，接下来 \( E/N \) 个属于节点 1……因此 `view(-1, E/N)` 后每一行正好是「某层的某个节点」（[eplb.py:L112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112) 的注释也写明了这一点）。
- **`phy`**：Step 2 产出的、节点内编号的物理专家。
- **`pphy`**：Step 3 之后「节点内 GPU 槽位」编号的物理专家：\( pphy = gpu_{node内序号} \times (M/P) + 槽内序号 \)；最终输出的全局槽位再加上节点偏移，即「节点 → GPU → 槽内位置」的编码（与 u1-l3 你还原放置图用的公式一致）。

#### 4.3.3 源码精读

参数断言与派生量在 [eplb.py:L89-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96)：四条整除约束 \( E \bmod G = 0 \)、\( G \bmod N = 0 \)、\( P \bmod N = 0 \)、\( M \bmod P = 0 \)（u1-l2 已解释过它们的来源）。

局部函数 `inverse` 在 [eplb.py:L98-L101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101)：用 `scatter_` 构造置换的逆，在 L108 与 L120 两处使用。

**Step 1（组打包到节点）**在 [eplb.py:L103-L108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L108)：先 `unflatten + sum` 把专家负载聚合成组负载，`balanced_packing` 把组装到节点，随后一行复合索引把「节点号 + 节点内组序号 + 组内偏移」编码成 `log2mlog`（这行展开讲在 u2-l4）。

**Step 2（节点内复制）**在 [eplb.py:L110-L113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113)：`gather` 按 `mlog2log` 重排后 `view` 成 `[L·N, E/N]`，让**所有层的所有节点在一次调用里并行完成复制**——这就是 4.2 中 `X = num_layers * num_nodes` 的由来。

**Step 3（GPU 打包与映射链合成）**在 [eplb.py:L115-L129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L129)：先算每个副本的期望负载 \( tokens\_per\_mlog / mlogcnt \) 再 `gather` 成 `phy` 顺序，装箱到节点内各 GPU；随后通过 `phy2pphy → pphy2phy → pphy2mlog → pphy2log` 的映射复合，把所有中间编号一路翻译回**原始逻辑专家编号**，最后把 `logcnt` 也 `gather` 回原顺序。返回值依次是 `pphy2log, pphyrank, logcnt`，形状 \( [L, M], [L, M], [L, E] \)。这条映射链的逐步追踪是 u2-l5 的主题。

#### 4.3.4 代码实践

1. **实践目标**：跑通层级策略，验证返回形状与「副本数守恒」。
2. **操作步骤**（示例代码）：

   ```python
   import torch, eplb

   weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                          [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
   phy2log, phyrank, logcnt = eplb.rebalance_experts_hierarchical(
       weight.float(), 16, 4, 2, 8)
   print(phy2log.shape, phyrank.shape, logcnt.shape)
   print(logcnt.sum(-1))
   ```

3. **需要观察的现象**：三个输出的形状；以及每行 `logcnt` 的总和。
4. **预期结果**：形状依次为 `torch.Size([2, 16])`、`torch.Size([2, 16])`、`torch.Size([2, 12])`；`logcnt.sum(-1)` 每层都等于 16（每个物理专家恰好对应一个逻辑专家，副本数之和恒等于物理专家总数）。`phy2log` 的第一行应与 [README.md:L55](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55) 给出的输出一致。若暂时无法运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：README 示例参数下 `tokens_per_mlog` 的形状是什么？
答案：\( [L \cdot N,\ E/N] = [2 \times 2,\ 12/2] = [4, 6] \)。

**练习 2**：层级实现内部 `balanced_packing` 被调用了几次？`num_packs` 分别是多少？
答案：两次（[eplb.py:L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L105) 与 [eplb.py:L118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118)）：Step 1 是 `num_nodes`，Step 3 是 `num_gpus // num_nodes`（节点内 GPU 数）。

**练习 3**：为什么 Step 2 之后要把每副本负载写成 \( tokens\_per\_mlog / mlogcnt \)？
答案：一个逻辑专家的多个副本均摊它的总负载，装箱时应以「单副本期望负载」为权重，而不是逻辑专家的总负载。

### 4.4 模块四：`rebalance_experts` —— 入口分派与逆映射组装

#### 4.4.1 概念说明

这是 `__all__` 中唯一的公开函数（[eplb.py:L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164)），对外统一接口。它做三件事：规范化输入、按整除性分派策略、把层级实现返回的中间结果组装成用户需要的 `log2phy` 逆映射。

#### 4.4.2 核心流程

```text
rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
  1. weight → float → cpu                          # 数值稳定 + 设备统一
  2. 分派：
       num_groups % num_nodes == 0 → hierarchical(weight, M, G, N, P)
       否则                         → hierarchical(weight, M, 1, 1, P)
  3. maxlogcnt ← logcnt 的全局最大值
  4. 构造 log2phy [L, E, maxlogcnt]：先填 -1，再用 scatter 一次写入
  5. 返回 (phy2log, log2phy, logcnt)
```

`log2phy[l, e, r] = p` 的含义：第 `l` 层逻辑专家 `e` 的第 `r` 个副本在物理槽位 `p`。由于不同专家的副本数不同而第三维长度统一取 `maxlogcnt`，副本数不足的位置保持 `-1`（padding）。

#### 4.4.3 源码精读

输入规范化在 [eplb.py:L148-L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L149)：`weight.float().cpu()` 把负载统一转成 CPU 上的浮点张量——这就是 u1-l3 说「CPU 版 PyTorch 即可运行」的原因，也避免了整型除法截断（数值细节在 u3-l3 讨论）。

策略分派在 [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)：整除走层级策略；不整除（分组无法对齐节点）则放弃分组约束，以 `num_groups=1, num_nodes=1` 退化为「全局复制 + GPU 级装箱」的全局策略——同一份实现服务两种策略，这一设计模式的利弊在 u3-l5 讨论。

`log2phy` 的组装在 [eplb.py:L157-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161)：

```python
log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank, 
        torch.arange(num_replicas, ...).expand(num_layers, -1))
```

核心是**扁平化技巧**：把三维下标 \( (e, r) \) 编码成一维 \( e \cdot maxlogcnt + r \)，就能用一次 `scatter_` 把每个物理槽位号 \( p \) 写进它该在的位置。注意层级实现返回的 `phyrank` 在这里被消耗掉、不再返回给用户——它的信息被吸收进了 `log2phy`。

#### 4.4.4 代码实践

1. **实践目标**：分别触发两条策略分支，观察 `log2phy` 形状差异。
2. **操作步骤**（示例代码）：

   ```python
   import torch, eplb

   weight = torch.randint(1, 200, (2, 12))
   # 分支一：4 % 2 == 0，层级策略
   p1, l1, c1 = eplb.rebalance_experts(weight, 16, 4, 2, 8)
   # 分支二：4 % 3 != 0，全局策略（注意 num_gpus 须是 num_nodes 的倍数）
   p2, l2, c2 = eplb.rebalance_experts(weight, 18, 4, 3, 6)
   print(p1.shape, l1.shape, c1.shape)
   print(p2.shape, l2.shape, c2.shape)
   print((l1 == -1).sum(), (l2 == -1).sum())
   ```

3. **需要观察的现象**：两分支三个输出的形状；`log2phy` 中 `-1` 的个数。
4. **预期结果**：分支一形状为 `[2,16]`、`[2,12,maxlogcnt₁]`、`[2,12]`（用 README 的固定权重时 `maxlogcnt₁ = 2`，因为其 `phy2log` 中每个专家至多出现 2 次）；分支二形状为 `[2,18]`、`[2,12,maxlogcnt₂]`、`[2,12]`（`18 % 6 == 0` 满足约束）。两分支的 `log2phy` 中都应存在若干 `-1`，且总数等于 \( L \times (E \cdot maxlogcnt - M) \)。随机权重下 `maxlogcnt` 的具体值待本地验证。另可尝试 `num_gpus=8, num_nodes=3`，应触发 [eplb.py:L94](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L94) 的断言。

#### 4.4.5 小练习与答案

**练习 1**：`num_groups=4, num_nodes=3, num_gpus=6, num_replicas=18, E=12` 走哪条分支？实际传给层级实现的参数是什么？
答案：\( 4 \bmod 3 \ne 0 \)，走全局分支，实际调用 `rebalance_experts_hierarchical(weight, 18, 1, 1, 6)`（[eplb.py:L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L156)）。

**练习 2**：`log2phy` 的第三维由什么决定？
答案：`maxlogcnt = logcnt.max().item()`（[eplb.py:L157](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157)），即所有层、所有专家中最大的副本数。

**练习 3**：`scatter_` 之前为什么先 `view(num_layers, -1)`？
答案：把 `[L, E, maxlogcnt]` 压平成 `[L, E·maxlogcnt]`，使复合下标 `phy2log * maxlogcnt + phyrank`（即 \( e \cdot maxlogcnt + r \)）可以直接作为一维目标下标，一次 `scatter_` 完成全部写入。

## 5. 综合实践：制作你的「函数与形状速查表」

把四个模块的实践拼成一个脚本，为每个函数固定一组随机输入（用固定种子保证可复现），断言全部输出形状，并打印汇总表（示例代码，非项目原有）：

```python
import torch
import eplb

torch.manual_seed(42)
L, E, M, G, N, P = 2, 12, 16, 4, 2, 8          # README 示例参数
weight = torch.randint(1, 200, (L, E))

# 1) balanced_packing: [L, G] -> 两个 [L, G]
tokens_per_group = weight.unflatten(-1, (G, E // G)).sum(-1)
pack_index, rank_in_pack = eplb.balanced_packing(tokens_per_group, N)
assert pack_index.shape == (L, G) and rank_in_pack.shape == (L, G)

# 2) replicate_experts: [L, E] -> [L, M], [L, M], [L, E]
r_phy2log, r_rank, r_logcnt = eplb.replicate_experts(weight.float(), M)
assert r_phy2log.shape == (L, M) and r_rank.shape == (L, M) and r_logcnt.shape == (L, E)

# 3) rebalance_experts_hierarchical: [L, M], [L, M], [L, E]
h_phy2log, h_phyrank, h_logcnt = eplb.rebalance_experts_hierarchical(
    weight.float(), M, G, N, P)
assert h_phy2log.shape == (L, M) and h_phyrank.shape == (L, M) and h_logcnt.shape == (L, E)

# 4) rebalance_experts: [L, M], [L, E, maxlogcnt], [L, E]
phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, G, N, P)
maxlogcnt = logcnt.max().item()
assert phy2log.shape == (L, M) and log2phy.shape == (L, E, maxlogcnt) and logcnt.shape == (L, E)

# 附加守恒检查（通往 u3-l1 的桥梁）
assert torch.all(logcnt.sum(-1) == M)

rows = [
    ("balanced_packing",              f"[{L},{G}] -> [{L},{G}], [{L},{G}]"),
    ("replicate_experts",             f"[{L},{E}] -> [{L},{M}], [{L},{M}], [{L},{E}]"),
    ("rebalance_experts_hierarchical", f"[{L},{E}] -> [{L},{M}], [{L},{M}], [{L},{E}]"),
    ("rebalance_experts",             f"[{L},{E}] -> [{L},{M}], [{L},{E},{maxlogcnt}], [{L},{E}]"),
]
for name, sig in rows:
    print(f"{name:32s} {sig}")
print("全部形状断言通过")
```

**预期结果**：所有断言通过，打印出四行的速查表；`maxlogcnt` 通常为 2（固定种子下具体值待本地验证）。建议把这张表抄进自己的笔记，并补一列「参数约束」（\( n \bmod m = 0 \)、\( M \ge E \)、四条整除断言等），u2 精读时随时对照。

进阶观察（可选）：对同一 `weight` 分别调用 3) 和 4)，比较 `h_phy2log` 与 `phy2log` 是否完全一致——入口对层级分支只是做了类型/设备规范化与 `log2phy` 组装，放置方案本身应相同。

## 6. 本讲小结

- `eplb.py` 共 165 行、四个函数：`balanced_packing`（带容量约束的贪心装箱）与 `replicate_experts`（冗余副本贪心分配）是两个独立工具；`rebalance_experts_hierarchical` 把它们串成三步主流程；`rebalance_experts` 是唯一公开入口。
- 层级策略三步：**组打包到节点**（`balanced_packing(…, num_nodes)`）→ **节点内复制**（`replicate_experts`，行维度 `L·N` 并行）→ **物理专家打包到 GPU**（`balanced_packing(…, P/N)`），最后经 `phy2pphy → pphy2phy → pphy2mlog → pphy2log` 映射链把中间编号翻译回原始逻辑编号。
- 形状演变主线：`[L,E] → [L,G] → [L,E]（置换）→ [L·N, E/N] → [L·N, M/N] → [L,M]`；入口再组装出 `[L, E, maxlogcnt]` 的 `log2phy`。
- 全局策略没有独立实现：不可整除时以 `(1, 1, num_gpus)` 的退化参数复用层级实现。
- 变量名 `A2B` 表示「A 到 B 的映射表」；`mlog` 是节点分块重编号的逻辑专家，`pphy` 是节点内 GPU 槽位编号的物理专家。
- 关键不变量：每层 `logcnt` 之和恒等于 \( M \)；所有计算在 CPU 浮点上完成。

## 7. 下一步学习建议

下一讲进入 u2 精读系列，建议顺序：

1. [u2-l1] `balanced_packing` 逐行精读：为什么降序排序能改善贪心质量、`min(range, key=...)` 惯用法。
2. [u2-l2] `replicate_experts` 精读：\( weight/logcnt \) 贪心的原理、`phy2log/rank/logcnt` 三表如何随循环逐步构造。
3. [u2-l3] 张量工具箱：`gather`、`scatter_`、逆置换 `inverse`、复合索引编码——读懂主流程的前置课。
4. [u2-l4] ~ [u2-l6] 层级策略三步与入口的完整精读，本讲的形状速查表将是你最好的随身地图。
