# 源码中的 PyTorch 张量技巧：gather、scatter 与逆置换

## 1. 本讲目标

本讲是通往 `rebalance_experts_hierarchical` 主流程之前的"工具箱补课"。学完本讲，你应该能够：

- 把 `eplb.py` 里出现的每一张 `xx2yy` 表读成一张"编号翻译表"，并说出一行 `gather` 代码到底把数据重排成了什么顺序。
- 用 `scatter_` 从零写出 `inverse(perm)`，并解释 `gather` 与 `scatter_` 互为对偶的原因。
- 区分 `expand` 与 `repeat`、看懂 `unflatten`/`flatten`/`view` 的形状演变，以及带步长 `arange` 生成的节点偏移。
- 手算 `(pack_index * groups_per_node + rank_in_pack) * group_size + arange(group_size)` 这类复合索引编码，明白它就是多维下标的进位展平。

本讲不引入新的负载均衡算法，只把 u1-l4 代码地图里"一闪而过"的那些张量操作逐一拆开。掌握它们之后，u2-l4 与 u2-l5 精读层级策略时，你看到的将不再是"一长串看不懂的链式调用"，而是四五次朴素的"查表翻译"。

## 2. 前置知识

本讲默认你已学完 u1-l4（知道四个函数的分工、`A2B` 映射表命名约定、`mlog`/`pphy` 等中间编号空间的含义）。在此之外，只需要下面几块基础知识：

- **张量与形状记法**：`[X, n]` 表示第一维长度为 X、最后一维长度为 n 的二维张量。EPLB 中 X 通常是层数（或层数×节点数），n 是专家数或槽位数。
- **维度编号 `dim`**：PyTorch 的维度从 0 开始编号，`-1` 恒指最后一维。`eplb.py` 的 gather/scatter 几乎全部作用在 `-1` 上。
- **整型索引**：`gather`/`scatter_` 的 index 张量必须是整数类型（源码统一用 `torch.int64`），否则会报错。
- **置换（permutation）**：集合 \(\{0, 1, \dots, n-1\}\) 到自身的一一对应。直观地说：n 个格子重排，每个格子恰好放一个编号、每个编号恰好出现一次。一张 `int64` 张量 `perm` 只要每行都是 0..n-1 的重排，它就"是"一个置换。
- **视图（view）与拷贝**：`view`/`unflatten`/`flatten`/`expand` 只改变"怎么看这块内存"，不复制数据；`repeat`/`clone` 会分配新内存。这个区别决定了源码里哪些地方必须用 `repeat`。

一个贯穿全讲的记号：对一张映射表 `A2B`，我们读作

\[
\text{B编号} = \text{A2B}[\text{A编号}]
\]

例如 `log2mlog[i]` 表示"逻辑专家 i 被安排到的 mlog 槽位编号"；`mlog2log` 与它互逆，`mlog2log[j]` 表示"mlog 槽位 j 上放的是哪个原始逻辑专家"。

## 3. 本讲源码地图

本仓库的全部实现只有 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) 一个文件（约 165 行）。本讲从"张量操作"的视角重新切一遍这个文件：

| 行段 | 所在函数 | 本讲视角 |
|---|---|---|
| [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) | `balanced_packing` | `arange(...).expand(...)`：恒等置换的零拷贝广播 |
| [eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) | `balanced_packing` | `sort().indices`：argsort 也是一种置换 |
| [eplb.py:L62-L64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L64) | `replicate_experts` | `repeat` vs `zeros`/`ones`：初始张量怎么造 |
| [eplb.py:L98-L101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101) | `rebalance_experts_hierarchical` 内嵌 | **`inverse` 函数**：用 `scatter_` 求逆置换 |
| [eplb.py:L103-L108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L108) | Step 1 | `unflatten+sum`、复合索引编码、`flatten(-2)` |
| [eplb.py:L110-L113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113) | Step 2 | `gather` 重排 + `view` 按节点切行 |
| [eplb.py:L115-L129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L129) | Step 3 | `gather` 映射链复合、槽位编码、带步长 `arange` 偏移 |
| [eplb.py:L157-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161) | `rebalance_experts` | `scatter_` 组装 `log2phy`（含 `-1` padding） |

建议把 `eplb.py` 在编辑器中打开对着看，本讲所有代码片段都出自以上行段。

## 4. 核心概念与源码讲解

### 4.1 编号翻译表与 gather：一行代码重排整个布局

#### 4.1.1 概念说明

EPLB 的世界观可以概括成一句话：**布局就是编号方案，改布局就是换一张翻译表**。

层级策略的每一步都在"重新编号"：Step 1 把专家从 `log` 空间搬到 `mlog` 空间（按节点分块），Step 3 再搬到 `pphy` 空间（按 GPU 槽位）。每换一次编号空间，就需要做两件事：

1. **重排数据**：负载统计 `weight` 要按新编号顺序重新排列；
2. **翻译表本身也要跟着搬家**：上一张映射表要转换到新编号空间下使用。

这两件事在源码里全部由 `torch.gather` 完成。`gather` 的语义（二维、`dim=-1`、逐行独立）是：

\[
\text{out}[r, c] = \text{src}[r,\ \text{index}[r, c]]
\]

也就是说：**输出第 c 个位置的内容，从 index 表第 c 项指定的来源处取**。index 回答的问题是"目的地的每一格从哪里读"。

先看一个最小例子（示例代码，可在交互环境运行）：

```python
src   = torch.tensor([[10, 20, 30],
                      [40, 50, 60]])
index = torch.tensor([[2, 0],
                      [1, 1]])
src.gather(-1, index)   # tensor([[30, 10],
                        #         [50, 50]])
```

第 0 行取 `src[0,2]` 和 `src[0,0]`，第 1 行取 `src[1,1]` 两次——每一行独立查表，互不干扰。这正对应 u2-l2 讲过的"所有层按行独立、一次向量化并行"。

还有一个容易忽视的事实：**argsort 本身就是置换**。`tensor.sort(dim)` 返回 `(values, indices)` 的命名元组，其中 `indices[j]` 是"排在第 j 位的元素原来的下标"。它回答"第 j 名是谁"，这与"谁排第几"（逆置换）互为反向——这个"一张表两种读法"的视角马上会在 4.2 派上用场。

#### 4.1.2 核心流程

EPLB 中一次标准的数据重排只有三步：

```text
1. 拿到一张"新编号 → 旧编号"的翻译表 idx
   （通常是某张 A2B 表的逆，例如 mlog2log）
2. data_new = data_old.gather(-1, idx)
   —— 新顺序第 j 格 = 旧顺序第 idx[j] 格
3. 此后按新编号空间继续计算；
   需要旧编号时再用 gather 把别的表翻译回来
```

关键口诀：**gather 的 index 必须是"目的地 → 来源"方向的表**。要把数据从 `log` 顺序排成 `mlog` 顺序，index 的第 j 项必须告诉我们"mlog 第 j 格放的是哪个 log"，也就是 `mlog2log` 而不是 `log2mlog`。初读源码最常见的困惑（"为什么 gather 用的表和刚构造的表是反的？"）就出在这里。

#### 4.1.3 源码精读

**（1）argsort 置换**。[eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)：

```python
indices = weight.float().sort(-1, descending=True).indices.cpu()
```

这行在 u2-l1 已从算法角度讲过（取负载降序的处理顺序）。从张量角度看，`indices` 是一个 `[X, n]` 的置换：每行都是 0..n-1 的重排，`indices[i][j]` 是第 i 层第 j 个被处理的物品编号。`.indices` 直接从排序结果里取下标表，等价于 `argsort`；`.cpu()` 是为了后面 `for group in indices[i]` 的 Python 逐元素遍历在 CPU 上进行。

**（2）Step 2 的入口重排**。[eplb.py:L112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112)：

```python
tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
```

这行做两件事（`view` 部分留到 4.3）：先用刚构造的逆置换 `mlog2log` 把每层负载从"原始 log 顺序"重排为"按节点分块的 mlog 顺序"，使得**同一个节点的专家占据连续的一段**。

**（3）Step 3 的负载重排**。[eplb.py:L117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)：

```python
tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
```

`phy2mlog[phy]` 是"物理槽位 phy 上放的 mlog 专家"，正好是 gather 需要的"目的地→来源"表：重排后第 phy 格就是该副本的均分负载（除以 `mlogcnt` 摊薄，见 u2-l2）。

**（4）映射链的复合翻译**。[eplb.py:L126-L128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L126-L128)：

```python
pphy2log = mlog2log.gather(-1, pphy2mlog)
pphyrank = phyrank.gather(-1, pphy2phy).view(num_layers, -1)
logcnt = mlogcnt.view(num_layers, -1).gather(-1, log2mlog)
```

注意这里 gather 的对象不再是负载数据，而是**另外一张映射表**——翻译表自己也搬到了新编号空间下。三行分别是：把 `mlog2log` 表从 mlog 顺序排到 pphy 顺序（得到 `pphy → log` 的总表）；把副本序号 `phyrank` 随槽位重排；把副本计数 `mlogcnt` 翻译回原始 log 顺序。这正是 u2-l5 要追的"映射链"的全部机关。

#### 4.1.4 代码实践

**实践目标**：建立 `out[j] = src[index[j]]` 的肌肉记忆，并亲眼确认"index 方向反了结果就乱了"。

**操作步骤**（示例代码，交互环境运行）：

```python
import torch

weight = torch.tensor([[90, 132, 40, 61, 104, 165]])
idx = torch.tensor([[3, 1, 0, 5, 4, 2]])          # 一张"目的地→来源"表

print(weight.gather(-1, idx))                      # 按表重排
print([weight[0, j].item() for j in idx[0].tolist()])  # 等价的 Python 循环
```

**需要观察的现象**：两个 print 输出完全一致（`gather` 就是向量化的下标取数）；再故意换一张不是置换的表（比如某行有重复下标 `[0,0,1,1,2,2]`），`gather` 依然能跑，输出中出现重复值——`gather` 并不要求 index 是置换，但**只有是置换时它才可逆**。

**预期结果**：第一组输出 `tensor([[ 61, 132,  90, 165, 104,  40]])`（由公式手推，请本地运行核对）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`src = torch.tensor([[10,20,30],[40,50,60]])`，`index = torch.tensor([[2,0],[1,1]])`，求 `src.gather(-1, index)`。

答案：`tensor([[30, 10], [50, 50]])`。逐格代入 `out[r,c] = src[r, index[r,c]]` 即得。

**练习 2**：`eplb.py:L112` 为什么用 `mlog2log`（新→旧）而不是刚构造出来的 `log2mlog`（旧→新）作为 gather 的 index？

答案：gather 的语义是 `out[j] = src[index[j]]`，即 index 必须回答"新顺序的第 j 格从哪个旧位置读"。`mlog2log[j]` 恰好是"mlog 槽位 j 上放的原始专家"；若误用 `log2mlog`，得到的是把 weight 按 `log2mlog` 的值乱序取一遍，节点分块关系会被破坏。

**练习 3**：`.sort(-1, descending=True).indices` 与 `.argsort(-1, descending=True)` 是什么关系？

答案：完全等价，都返回"第 j 名是谁"的下标表。源码用前者是因为 `sort` 同时可以拿到 values（虽然这里只用了 indices）。

### 4.2 scatter_ 与逆置换：inverse 函数与 log2phy 的组装

#### 4.2.1 概念说明

`gather` 是"读侧重排"：先固定输出的位置，再按表去取。`scatter_` 是"写侧重排"：先固定数据的顺序，再按表去放。对 `dim=-1` 的二维情形：

\[
\text{self}[r,\ \text{index}[r, c]] = \text{src}[r, c]
\]

index 回答的问题是"**来源的第 c 个数据写到目的地哪一格**"。

对比一下就能看到漂亮的对偶——**同一张 idx 表，在 gather 和 scatter 里方向恰好相反**：

| 用法 | 语义 | idx 的读法 |
|---|---|---|
| `out.gather(-1, idx)` 模式 | `out[j] = src[idx[j]]` | 目的地 j → 来源 idx[j] |
| `dst.scatter_(-1, idx, src)` 模式 | `dst[idx[j]] = src[j]` | 来源 j → 目的地 idx[j] |

这个对偶正是求逆置换的钥匙。设 `perm[j] = σ(j)` 是一个置换，我们想要 `inv` 满足逆置换定义：

\[
\sigma^{-1}(\sigma(j)) = j \quad\Longleftrightarrow\quad \text{inv}[\text{perm}[j]] = j
\]

把左边看成"目的地为 `perm[j]`"、右边看成"来源为 j"（j 就是 `arange(n)` 的第 j 项），这正是一句 `scatter_`：

```python
inv.scatter_(1, perm, torch.arange(n).expand(perm.shape))
```

`eplb.py` 里的 `inverse` 函数就是它。此外 `scatter_` 还有一个 `gather` 没有的特性：**没被 index 覆盖到的格子保持原值**。`rebalance_experts` 组装 `log2phy` 时的 `-1` padding 就靠这个特性白送。

#### 4.2.2 核心流程

**求逆置换（`inverse`）**：

```text
1. inv = empty_like(perm)          # 未初始化的容器
2. inv.scatter_(1, perm, arange)   # 对每行 r：inv[r, perm[r, c]] = c
3. 返回 inv                        # inv 与 perm 互逆
```

**组装 `log2phy`（二维键合成的 scatter）**：

```text
1. maxlogcnt = logcnt.max()              # 第三维长度 = 最大副本数
2. log2phy = full(-1)                    # 先铺满 -1
3. view(num_layers, -1)                  # 三维 [L, E, maxlogcnt] 压平成二维
4. 目标列 = phy2log * maxlogcnt + phyrank  # (逻辑专家, 副本序号) 二维键 → 一维列号
5. scatter 写入物理专家编号              # 未覆盖的列保持 -1
```

#### 4.2.3 源码精读

**（1）`inverse` 函数**。[eplb.py:L98-L101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101)：

```python
def inverse(perm: torch.Tensor) -> torch.Tensor:
    inv = torch.empty_like(perm)
    inv.scatter_(1, perm, torch.arange(perm.size(1), dtype=torch.int64, device=perm.device).expand(perm.shape))
    return inv
```

这四行是全文件最值得背下来的代码。逐行解读：

- `empty_like`：只分配不初始化。因为置换是满射，scatter 会覆盖每一格，初始化纯属浪费（`zeros_like` 结果相同但多做一次清零）。
- `scatter_(1, perm, ...)`：对第 r 行执行 `inv[r, perm[r, c]] = c`。`perm` 是"来源 c → 目的地 perm[c]"方向的表。
- `arange(n).expand(perm.shape)`：要写入的值就是来源下标 c 本身，每行都一样，所以零拷贝的 `expand` 足够（scatter 只读 src）。
- `device=perm.device`：显式跟随输入设备，避免在 GPU 张量上混入 CPU 索引（同类问题曾在 commit `d52c72d` 中被修复，详见 u3-l3）。

手算一个例子（示例代码）：

```python
perm = torch.tensor([[2, 0, 1]])
# scatter 语义: inv[0, 2]=0, inv[0, 0]=1, inv[0, 1]=2
# 得 inv = tensor([[1, 2, 0]])
```

验证互逆：`perm.gather(-1, inv)` 即 `perm[[1,2,0]] = [0,1,2]`；`inv.gather(-1, perm)` 即 `inv[[2,0,1]] = [0,1,2]`——两边都恢复成有序序列。

**（2）`inverse` 的两个调用点**。[eplb.py:L108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L108) 与 [eplb.py:L120](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L120)：

```python
mlog2log = inverse(log2mlog)     # Step 1 产出：mlog→log 逆表
pphy2phy = inverse(phy2pphy)     # Step 3 产出：pphy→phy 逆表
```

两次求逆服务的正是 4.1 的口诀：gather 重排需要"目的地→来源"方向的表，而贪心算法（装箱、复制）天然按"来源"（物品、专家）组织输出，所以每次都要转一下方向。

**（3）`log2phy` 的组装**。[eplb.py:L157-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161)：

```python
maxlogcnt = logcnt.max().item()
log2phy: torch.Tensor = torch.full((num_layers, num_logical_experts, maxlogcnt), 
                                   -1, dtype=torch.int64, device=logcnt.device)
log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank, 
        torch.arange(num_replicas, dtype=torch.int64, device=log2phy.device).expand(num_layers, -1))
```

这是 `scatter_` 的进阶用法：**把二维键压进一维列号再做 scatter**。每个物理专家 phy 有两个属性——它属于哪个逻辑专家（`phy2log`）、它是该专家的第几个副本（`phyrank`）。目标数组是三维的 `[L, E, maxlogcnt]`，直接按二维坐标散写并不方便，于是先把目标 `view` 成 `[L, E*maxlogcnt]`，再用进位公式合成列号：

\[
c = \text{log} \cdot \text{maxlogcnt} + r
\]

这正是 4.4 要展开的 row-major 编码。写入的值是物理专家自己的编号（`arange(num_replicas)` 展开）。由于每层内 `(log, rank)` 二元组唯一（一个专家的第 r 个副本至多一个），scatter 不会发生写冲突；而**没有被任何物理专家占用的 `(log, r)` 组合保持初始的 -1**——这就是 u1-l3 见过的 padding 语义：复制数不足 `maxlogcnt` 的专家，靠后的槽位是 -1。

示意（示例代码，E=3、maxlogcnt=3、副本数分别为 2/1/3 的一层）：

```text
log2phy[l] =
  log=0: [ phy_a, phy_b,   -1  ]
  log=1: [ phy_c,   -1,    -1  ]
  log=2: [ phy_d, phy_e,  phy_f ]
```

#### 4.2.4 代码实践

**实践目标**：亲手实现 `inverse` 并双向验证互逆性，同时确认它与 `argsort` 的等价关系。

**操作步骤**（示例代码）：

```python
import torch

def inverse(perm):                     # 1D 版本，思想与 eplb.py:L98-L101 相同
    inv = torch.empty_like(perm)
    inv.scatter_(0, perm, torch.arange(perm.numel(), dtype=perm.dtype))
    return inv

torch.manual_seed(0)
perm = torch.randperm(6)
inv = inverse(perm)

print(perm, inv)
print(perm.gather(0, inv))             # 断言 1：应恢复有序
print(inv.gather(0, perm))             # 断言 2：应恢复有序
print(torch.equal(inv, perm.argsort()))  # 断言 3：逆置换 == argsort
```

**需要观察的现象**：两个 gather 的输出都是 `tensor([0, 1, 2, 3, 4, 5])`；断言 3 为 `True`。

**预期结果**：三条全部成立。原因：`inv[perm[j]] = j` 保证 `perm[inv[i]] = i` 与 `inv[perm[i]] = i` 同时成立；而 argsort 把"第 k 小的值"定位到来源下标，对 0..n-1 的置换而言第 k 小的值就是 k，故 `argsort(perm)[k]` = `j 使得 perm[j]=k` = `inv[k]`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`perm = torch.tensor([3, 1, 0, 2])`，手算 `inverse(perm)`。

答案：`[2, 1, 3, 0]`。由 `inv[3]=0, inv[1]=1, inv[0]=2, inv[2]=3` 按位置重排得到。

**练习 2**：把 `inverse` 里的 `torch.empty_like` 换成 `torch.zeros_like`，结果会变吗？为什么源码选 `empty_like`？

答案：结果不变——置换是满射，每个格子都会被 scatter 覆盖一次。`empty_like` 只是省掉一次无意义的清零，属于微优化兼意图声明（"每格必被写入"）。

**练习 3**：如果 `scatter_` 的 index 中同一行出现重复值（例如两次都写到列 5），会发生什么？`log2phy` 为什么不用担心？

答案：官方语义不保证哪个值胜出（结果不确定）。`log2phy` 的写入键是 `(phy2log, phyrank)` 二元组，每层内每个逻辑专家的第 r 个副本至多一个，键唯一故无冲突；反过来，`phy2log` 单独作键就可能有重复（复制的专家），这正是要乘 `maxlogcnt` 再加 `phyrank` 合成复合键的原因。

### 4.3 形状与广播工具箱：unflatten/flatten/view、expand/repeat、带步长 arange

#### 4.3.1 概念说明

层级策略主流程的形状主线是（u1-l4 已给出，这里标注每步用的工具）：

```text
[L, E]                                        原始负载
  -- unflatten + sum  -->  [L, G]             每组 token 数      (Step 1)
  -- 复合编码 + flatten(-2) --> [L, E]        log2mlog 置换      (Step 1)
  -- gather + view     -->  [L·N, E/N]        按节点切行         (Step 2)
  -- replicate_experts -->  [L·N, M/N]        节点内复制         (Step 2)
  -- gather + 编码 + 偏移 + flatten --> [L, M] 最终放置方案      (Step 3)
```

工具箱里每个成员的分工：

- **`unflatten(dim, sizes)`**：把某一维拆成多维视图，零拷贝。`x.unflatten(-1, (G, gs))` 要求 `G*gs` 等于原长度，效果等同 `x.view(..., G, gs)`。
- **`flatten(dim0, dim1)` / `view`**：反向合并或任意合法的重排视图，同样零拷贝。`flatten(-2)` 只压最后两维。
- **`expand`**：广播到更大形状，但**不复制内存**（被广播的维度 stride 为 0）。只能用于"每行内容相同"且只读的场景。
- **`repeat`**：真实地平铺复制，分配新内存。需要逐行独立写入时必须用它。
- **带步长 `arange(start, end, step)`**：一次生成等差偏移序列，是"给每个节点/GPU 算基地址"的标准写法。

#### 4.3.2 核心流程

`expand` 与 `repeat` 的选型可以总结成一棵小决策树：

```text
需要一块 [n, m] 的初始张量，每行都相同？
├─ 之后只读（作为 gather/scatter 的 src 等）
│    └─ expand：零拷贝，最省
└─ 之后要按行独立覆写（如循环中 phy2log[:, i] = ...）
     └─ repeat / 显式构造：必须每人一份实体内存
```

带步长 arange 的直觉：

\[
\text{torch.arange}(0,\ E,\ E/N) = [\,0,\ E/N,\ 2E/N,\ \dots\,]
\]

第 n 项恰是"节点 n 的全局编号基地址"——每个节点占有连续的 \(E/N\) 个编号，基地址之间正好相差步长 \(E/N\)。

#### 4.3.3 源码精读

**（1）`unflatten + sum` 求组负载**。[eplb.py:L104](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104)：

```python
tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
```

`[L, E]` 先被看成 `[L, G, gs]`（连续 `gs` 个专家一组，零拷贝），再对最后一维求和得到 `[L, G]`。一行完成"按组聚合"，没有一次显式循环。

**（2）`unsqueeze + 广播 + flatten(-2)` 构造置换**。[eplb.py:L106-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107)：

```python
log2mlog = (((group_pack_index * groups_per_node + group_rank_in_pack) * group_size).unsqueeze(-1) + 
            torch.arange(group_size, dtype=torch.int64, device=group_pack_index.device)).flatten(-2)
```

每组的"基址"形状 `[L, G]` 先 `unsqueeze(-1)` 变 `[L, G, 1]`，与 `arange(group_size)` 形状 `[gs]` 相加时自动广播成 `[L, G, gs]`（每组的 gs 个槽位拿到的偏移恰好是 0..gs-1），最后 `flatten(-2)` 压回 `[L, E]`。这段编码的数学含义留到 4.4 展开。

**（3）`view` 按节点切行**。[eplb.py:L112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112)：

```python
tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
```

gather 之后 mlog 编号已按节点分块（节点 n 占连续的 `E/N` 个槽位），`view(-1, E/N)` 把 `[L, E]` 切成 `[L·N, E/N]`：第 l 层的第 n 段正好成为第 `l·N + n` 行。于是**"节点"从编号前缀变成了独立的一行**，Step 2 的 `replicate_experts` 就能让所有层的所有节点共用一次调用并行处理（u2-l2 讲过的按行独立）。

**（4）`view + 带步长 arange + flatten` 还原全局编号**。[eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)：

```python
pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) + 
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                          device=group_pack_index.device).view(1, -1, 1)).flatten(-2)
```

此时 `pphy2mlog` 的值还是"节点内编号"（值域 `[0, E/N)`）。`view(L, N, -1)` 把节点维显式拎出来，`arange(0, E, E/N).view(1, -1, 1)` 生成各节点基地址 `[0, E/N, 2E/N, ...]`，广播相加即"节点内编号 + 节点基地址 = 全局 mlog 编号"，最后压回 `[L, M]`。这是（2）的逆操作：一个编码用乘法进位，一个还原用加法偏移。`device=group_pack_index.device` 参数正是 commit `d52c72d` 补上的修复。

**（5）`expand` 与 `repeat` 的三个出场**。[eplb.py:L23](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L23)、[eplb.py:L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62)、[eplb.py:L100](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L100)：

```python
# balanced_packing 平凡分支：物品 i 放包 i、序号 0 —— 只读，用 expand
pack_index = torch.arange(weight.size(-1), dtype=torch.int64, device=weight.device).expand(weight.shape)
```

```python
# replicate_experts 初始 phy2log：每行都是 [0..num_phy)，但循环里要逐列覆写 —— 必须 repeat
phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)     # 各行相同的初值另有造法
logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
```

```python
# inverse 的 scatter src：只读，用 expand
inv.scatter_(1, perm, torch.arange(...).expand(perm.shape))
```

三者合起来正好覆盖决策树的所有分支：`L62` 的 `phy2log` 在 [eplb.py:L68](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L68) 被 `phy2log[:, i] = redundant_indices` 逐列改写，各行最终内容不同，所以必须用 `repeat` 造出实体内存；而 `rank`/`logcnt` 初值本来就是常数 0/1，直接用 `zeros`/`ones`。

#### 4.3.4 代码实践

**实践目标**：用肉眼+代码确认"视图不搬内存、expand 零拷贝、repeat 分配新内存"，并复现 Step 1 的形状链。

**操作步骤**（示例代码）：

```python
import torch

w = torch.arange(12.)                          # 模拟一层的负载
g = w.unflatten(-1, (4, 3))                    # [12] -> [4, 3]
print(g.shape, g.data_ptr() == w.data_ptr())   # 视图：同一块内存

a = torch.arange(3)
b = a.expand(2, 3)
c = a.repeat(2, 1)
print(b.stride(), c.stride())                  # (0, 1) vs (3, 1)
print(b.data_ptr() == a.data_ptr(), c.data_ptr() == a.data_ptr())

print(torch.arange(0, 12, 3))                  # 节点基地址：[0, 3, 6, 9]
print(g.sum(-1))                               # 每组负载（4 组）
```

**需要观察的现象**：`unflatten` 后 `data_ptr` 不变；`expand` 出来的 `b` stride 第 0 维是 0 且与 `a` 共享存储，`repeat` 出来的 `c` stride 正常且存储不同；`arange(0,12,3)` 就是四个节点基地址。

**预期结果**：输出 `torch.Size([4, 3]) True`、`(0, 1) (3, 1)`、`True False`、`tensor([0, 3, 6, 9])`、`tensor([ 3., 12., 21., 30.])`（由定义手推，请本地运行核对）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`x = torch.arange(12)`，`x.unflatten(-1, (3, 4))` 与 `x.unflatten(-1, (4, 3))` 有何区别？

答案：前者是 `view(3, 4)`，得到 `[[0,1,2,3],[4,5,6,7],[8,9,10,11]]`；后者是 `view(4, 3)`，得到 `[[0,1,2],[3,4,5],...]`。unflatten 按 row-major 顺序拆分，外层尺寸是"每组多少个连续元素"的计数——EPLB 中 `(num_groups, group_size)` 的顺序保证"连续 group_size 个专家是一组"。

**练习 2**：[eplb.py:L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62) 的 `phy2log` 改成 `torch.arange(num_phy).expand(n, 1)` 会怎样？（注意 `expand` 的参数应是形状 `phy2log` 的形状。）

答案：`expand` 得到的是各行共享同一块内存的 stride-0 视图，且 `expand(n, 1)` 形状也不对（应为 `expand(n, num_phy)`）。即使形状改对，后续 `phy2log[:, i] = redundant_indices`（[eplb.py:L68](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L68)）对共享内存的写入要么直接报错、要么让所有层被同时改坏——层与层之间会互相污染。所以必须 `repeat` 出 n 份独立内存。

**练习 3**：`torch.arange(0, 12, 3)` 的输出是什么？它在 [eplb.py:L124](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L124) 中扮演什么角色？

答案：`tensor([0, 3, 6, 9])`。当 12 个逻辑专家分给 4 个节点（每节点 3 个连续编号）时，它给出各节点的全局编号基地址，广播相加后把"节点内编号"还原为"全局 mlog 编号"。

### 4.4 复合索引编码：多重编号的进位展平与还原

#### 4.4.1 概念说明

现在回答本讲的最后一个问题：`(pack_index * groups_per_node + rank_in_pack) * group_size + arange(group_size)` 到底在算什么？

答案是：**多维下标的 row-major 进位展平**。把三个整数编号

- `pack_index`：节点号（0..N-1）
- `rank_in_pack`：节点内的组序号（0..groups_per_node-1）
- `arange(group_size)`：组内的专家序号（0..gs-1）

按"高位在前"的混合进制

\[
\text{mlog} = \big((\text{node} \cdot \text{groups\_per\_node}) + \text{group\_rank}\big) \cdot \text{group\_size} + \text{offset}
\]

压成一个一维编号。展开后更直观：

\[
\text{mlog} = \text{node} \cdot \underbrace{(\text{groups\_per\_node} \cdot \text{group\_size})}_{\text{每节点槽位数 } E/N} + \text{group\_rank} \cdot \text{group\_size} + \text{offset}
\]

这和把三位数 `abc` 写成 \(100a + 10b + c\) 是同一件事，只是"每一位的进制"各不相同（分别是 groups_per_node 和 group_size）。它保证了三条性质，而这三条正是层级策略需要的空间布局：

1. **节点连续**：节点 n 独占 `[n·E/N, (n+1)·E/N)` 这一段 mlog 编号；
2. **组不拆散**：同组的 gs 个专家拿到的 mlog 编号连续（Step 3 里整组一起搬）；
3. **无冲突**：不同 `(node, rank, offset)` 三元组必得不同编号，所以结果是 0..E-1 的置换（可逆、可 scatter）。

同一模式在源码里出现三次，只是"位数"不同：

| 位置 | 公式 | 各位含义 |
|---|---|---|
| [eplb.py:L106-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107) | `((pack * groups_per_node + rank) * group_size) + arange(group_size)` | 节点 / 组序号 / 组内偏移 → mlog |
| [eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) | `pack_index * phy_experts_per_gpu + rank_in_pack` | GPU 号 / GPU 内序号 → pphy |
| [eplb.py:L160](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L160) | `phy2log * maxlogcnt + phyrank` | 逻辑专家 / 副本序号 → log2phy 的列号 |

#### 4.4.2 核心流程

编码与还原是一对：

```text
编码（乘法进位）：
  flat = ((高位 * 进制1) + 次高位) * 进制2 + 低位
还原（加法偏移）：
  全局编号 = 节点内编号 + node * (E/N)        # eplb.py:L123-L125
  （除法取模也能还原：node = flat // (E/N)，offset = flat % (E/N)）
```

源码选择"加偏移"而不是"除法取模"，是因为还原时手里已经有现成的节点维（`view(L, N, -1)` 拎出来的），加法广播一次完成，无需再算整除。

#### 4.4.3 源码精读

**（1）log2mlog 的编码**。[eplb.py:L106-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107)（4.3 已拆过形状，这里看数值）：对每个组 g，`group_pack_index[g]` 是它被装箱到的节点，`group_rank_in_pack[g]` 是它在节点内的装箱顺序，两者与 `arange(group_size)` 组合，把组 g 的专家们安排到"该节点段内的连续 gs 个槽位"。

**（2）phy2pphy 的编码**。[eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)：

```python
phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
```

Step 3 里 `balanced_packing` 把节点内的物理专家装到节点内的 GPU 上，输出"GPU 号 + GPU 内序号"。两位编号进位展平成 pphy 槽位：每个 GPU 恰好占连续 `phy_experts_per_gpu` 个槽位——这就是 u1-l3 讲过的"物理槽位按节点→GPU→槽内位置编码"的出处。

**（3）偏移还原**。[eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)：4.3 已精读。它与（1）互为逆操作：（1）把"节点号"乘成高位，（3）把"节点基地址"加回来。

#### 4.4.4 代码实践

**实践目标**：拿 README 示例的第一层数据，手算一遍 log2mlog 编码，再用代码复现验证。

**操作步骤**：

第一步，手算。取 README 示例第一层（12 专家、4 组、每组 3 个、2 节点）：

- 组负载：组 0 = 90+132+40 = 262，组 1 = 61+104+165 = 330，组 2 = 39+4+73 = 116，组 3 = 56+183+86 = 325。
- 按 u2-l1 的装箱算法（降序处理、放入未满最轻包）：组 1(330)→节点 0 第 0 个；组 3(325)→节点 1 第 0 个；组 0(262)→节点 1 第 1 个；组 2(116)→节点 0 第 1 个。
- 按组编号组织：`pack_index = [1, 0, 0, 1]`，`rank_in_pack = [1, 0, 1, 0]`。

代入编码公式 \(((\text{pack} \cdot 2 + \text{rank}) \cdot 3 + \text{offset})\)：

| 组 g | pack | rank | 基址 \((\text{pack}\cdot2+\text{rank})\cdot3\) | 组内专家 | mlog 编号 |
|---|---|---|---|---|---|
| 0 | 1 | 1 | 9 | 0, 1, 2 | 9, 10, 11 |
| 1 | 0 | 0 | 0 | 3, 4, 5 | 0, 1, 2 |
| 2 | 0 | 1 | 3 | 6, 7, 8 | 3, 4, 5 |
| 3 | 1 | 0 | 6 | 9, 10, 11 | 6, 7, 8 |

即 `log2mlog = [9,10,11, 0,1,2, 3,4,5, 6,7,8]`（索引 = 原专家编号，值 = mlog 槽位）。注意节点 0 拿到 mlog 0..5（组 1、组 2），节点 1 拿到 mlog 6..11（组 3、组 0）——节点连续性成立。

第二步，代码复现（示例代码）：

```python
import torch
import eplb

weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86]])
num_groups, group_size, num_nodes = 4, 3, 2
groups_per_node = num_groups // num_nodes

tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
pack_index, rank_in_pack = eplb.balanced_packing(tokens_per_group, num_nodes)

log2mlog = (((pack_index * groups_per_node + rank_in_pack) * group_size).unsqueeze(-1)
            + torch.arange(group_size)).flatten(-2)

print(tokens_per_group)   # 预期 tensor([[262, 330, 116, 325]])
print(pack_index)         # 预期 tensor([[1, 0, 0, 1]])
print(rank_in_pack)       # 预期 tensor([[1, 0, 1, 0]])
print(log2mlog)           # 预期 tensor([[ 9, 10, 11,  0,  1,  2,  3,  4,  5,  6,  7,  8]])
```

第三步，顺带验证 4.2 的求逆与 4.1 的重排（示例代码）：

```python
inv = torch.empty_like(log2mlog)
inv.scatter_(1, log2mlog, torch.arange(weight.size(-1)).expand(log2mlog.shape))
print(inv)                              # 预期 tensor([[ 3,  4,  5,  6,  7,  8,  9, 10, 11,  0,  1,  2]])

reordered = weight.gather(-1, inv)
print(reordered)                        # 预期按 mlog 顺序排列的负载
print(reordered.view(-1, 6))            # 行 0 = 节点 0 的 6 个专家，行 1 = 节点 1
print(reordered.view(-1, 6).sum(-1))    # 预期 tensor([446., 587.])
```

**需要观察的现象**：`inv` 与手算一致；`reordered.view(-1, 6)` 的两行分别恰好是节点 0（专家 3..8）与节点 1（专家 9,10,11,0,1,2）的负载——`gather` + `view` 两步就把"按节点分块"落实了。

**预期结果**：`reordered` 为 `tensor([[ 61, 104, 165,  39,   4,  73,  56, 183,  86,  90, 132,  40]])`，两行负载和为 446 与 587（由手算推导，请本地运行核对）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：三重下标 `(a, b, c)`，各维尺寸为 `(A, B, C)`，写出 row-major 展平公式。

答案：\(\text{flat} = (a \cdot B + b) \cdot C + c\)。取 `A=num_nodes`、`B=groups_per_node`、`C=group_size` 即得 log2mlog 的编码公式。

**练习 2**：沿用 4.4.4 的手算结果：专家 7 的 mlog 编号是多少？

答案：专家 7 属于组 2（专家 6..8），该组 `pack=0, rank=1`，组内偏移 `7-6=1`，故 `mlog = ((0·2+1)·3)+1 = 4`。对照表中"组 2 → mlog 3,4,5"一致。

**练习 3**：若把 [eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 中的 `rank_in_pack` 项去掉（`phy2pphy = pack_index * phy_experts_per_gpu`），会出什么问题？

答案：同一 GPU 上的所有专家会得到相同的 pphy 编号，`phy2pphy` 不再是 0..M/N-1 的置换：`inverse(phy2pphy)` 里的 scatter 会在同一目的地反复写（结果不确定，等于丢失专家），后续整条映射链全部失效。低位（GPU 内序号）正是保证"无冲突"的那一位。

## 5. 综合实践

综合实践把本讲四个模块串成一条线：**随机置换上验证 gather/scatter 对偶 → 真实数据上复现编码与求逆 → 用复合键 scatter 组装一张小型 log2phy**。

**实践目标**：不借助任何封装，只用 `arange`/`gather`/`scatter_`/`unflatten`/`view` 复现 `eplb.py` 的核心张量机关，并全部用断言验证。

**操作步骤**（示例代码，存为独立脚本或逐步在交互环境运行）：

```python
import torch

# ---------- Part A：随机置换上验证 gather / scatter 对偶 ----------
def inverse1d(perm):
    inv = torch.empty_like(perm)
    inv.scatter_(0, perm, torch.arange(perm.numel(), dtype=perm.dtype))
    return inv

torch.manual_seed(42)
for _ in range(100):
    perm = torch.randperm(10)
    inv = inverse1d(perm)
    n = torch.arange(10)
    assert torch.equal(perm.gather(0, inv), n)      # 断言 1
    assert torch.equal(inv.gather(0, perm), n)      # 断言 2
    assert torch.equal(inv, perm.argsort())          # 断言 3
print("Part A passed")

# ---------- Part B：真实数据上复现编码与求逆（4.4.4 的第二、三步） ----------
import eplb
weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86]])
G, gs, N = 4, 3, 2
tpg = weight.unflatten(-1, (G, gs)).sum(-1)
pack, rank = eplb.balanced_packing(tpg, N)
log2mlog = (((pack * (G // N) + rank) * gs).unsqueeze(-1)
            + torch.arange(gs)).flatten(-2)
mlog2log = inverse1d(log2mlog[0]).unsqueeze(0)
assert torch.equal(log2mlog.gather(-1, mlog2log), torch.arange(12).unsqueeze(0))  # 互逆
assert torch.equal(mlog2log.gather(-1, log2mlog), torch.arange(12).unsqueeze(0))
print("Part B passed:", log2mlog)

# ---------- Part C：复合键 scatter 组装一张小型 log2phy ----------
# 假设某层 4 个逻辑专家，共 6 个物理专家（M=6），副本序号如下：
phy2log  = torch.tensor([[0, 1, 1, 2, 3, 3]])       # 逻辑专家 1、3 各有 2 个副本
phyrank  = torch.tensor([[0, 0, 1, 0, 0, 1]])       # 每个副本的序号
logcnt   = torch.tensor([[1, 2, 1, 2]])
maxlogcnt = logcnt.max().item()

log2phy = torch.full((1, 4, maxlogcnt), -1, dtype=torch.int64)
log2phy.view(1, -1).scatter_(-1, phy2log * maxlogcnt + phyrank,
                             torch.arange(6).expand(1, 6))
print(log2phy)
# 预期:
# tensor([[[0, -1],
#          [1,  2],
#          [3, -1],
#          [4,  5]]])
assert log2phy[0, 1].tolist() == [1, 2]              # 专家 1 的两个副本
assert (log2phy == -1).sum() == 2                     # 未填满的槽位恰是 -1
print("Part C passed")
```

**需要观察的现象**：Part A 的三条断言在 100 个随机置换上全部通过；Part B 输出与 4.4.4 手算的 `log2mlog = [9,10,11,0,1,2,3,4,5,6,7,8]` 一致；Part C 中 `log2phy` 的 `-1` 恰好出现在复制数不足 `maxlogcnt` 的专家槽位上，且每行非 -1 值合起来恰是 0..5 的一个排列。

**预期结果**：三段全部打印 passed；Part C 的 `log2phy` 与注释中的预期一致（由公式手推，请本地运行核对）。待本地验证。

**思考题**（选做）：Part C 中若把 `phy2log * maxlogcnt + phyrank` 误写成 `phy2log * 2 + phyrank`，在 `maxlogcnt == 2` 时结果恰好不变——为什么？什么时候会出错？（提示：只有当 `maxlogcnt` 恰为 2 时两个编码才重合；它必须来自 `logcnt.max()` 才能对任意输入成立。）

## 6. 本讲小结

- **布局即编号**：EPLB 的每一步重排都是"换一张编号翻译表"；`A2B[i]` 读作"A 编号 i 对应的 B 编号"，`gather` 需要"目的地→来源"方向的表（如 `mlog2log`），`scatter_` 需要"来源→目的地"方向的表——同一张表在两种操作里方向相反。
- **`gather` 与 `scatter_` 互为对偶**：`out[j] = src[index[j]]`（读侧重排）对 `dst[index[j]] = src[j]`（写侧重排）；`inverse` 函数四行（`empty_like` + `scatter_(1, perm, arange)`）由此直接写出，且对一维置换有 `inverse(perm) == perm.argsort()`。
- **视图工具零拷贝**：`unflatten/flatten/view` 只改变形状视角，`expand` 是 stride-0 的零拷贝广播（只读场景用），需要逐行独立写入时必须 `repeat`（`eplb.py:L62` 的 `phy2log` 就是典型）。
- **复合索引编码 = 混合进制进位**：`((pack * groups_per_node + rank) * group_size + offset)` 把（节点, 组序号, 组内偏移）压成一维编号，保证节点连续、组不拆散、结果可逆；还原用带步长 `arange` 生成基地址再相加。
- **`scatter_` 的"未覆盖保持原值"特性**是 `log2phy` 中 `-1` padding 的来源；复合键 `phy2log * maxlogcnt + phyrank` 把二维坐标压进一维列号，同时保证键唯一、无写冲突。

## 7. 下一步学习建议

本讲补齐了全部张量工具，下一讲 **u2-l4（层级策略前两步）** 将把它们投入实战：精读 `rebalance_experts_hierarchical` 的 Step 1（`unflatten+sum`、`balanced_packing`、`log2mlog` 编码与求逆）与 Step 2（`gather+view` 切行、`replicate_experts` 节点内复制），你会发现那两段代码就是本讲 4.4.4 实践的完整版。之后 **u2-l5** 追 Step 3 的映射链复合（`phy2pphy → pphy2phy → pphy2mlog → pphy2log`），**u2-l6** 看入口函数如何用 `scatter_` 组装最终的 `log2phy`。建议阅读源码时随身带着本讲的口诀："gather 查表取，scatter 查表放；高位乘进制，低位加偏移"。
