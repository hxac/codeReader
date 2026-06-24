# 数据放置算法与链表生成

## 1. 本讲目标

3FS 用「链（Chain）」做数据复制、用「链表（ChainTable）」做文件条带化。一个集群在第一次部署、或者扩容时，都要回答同一个问题：**到底该把哪些存储目标（target）编进同一条链、又该把哪些链编进同一张链表？**

这个答案不是随便排的。一条草率的链表在节点故障时会让恢复流量集中砸在少数节点上，直接拖垮整个集群的读吞吐。3FS 把它抽象成一个数学问题——**平衡不完全区组设计（BIBD）**，并用整数规划求解器算出最优解，最后导出成 `admin_cli` 命令灌进 mgmtd。

学完本讲，你应当能够：

- 理解为什么链表设计必须追求「恢复期流量均衡」，以及它如何映射成 BIBD；
- 读懂 `deploy/data_placement` 下的 Pyomo 模型（变量、约束、目标）与 `gen_chain_table.py` 的链生成流程；
- 独立用这两个脚本为一组节点生成 `create-target` / `upload-chains` / `upload-chain-table` 命令，并导入集群验证。

## 2. 前置知识

阅读本讲前，请先确认你理解下面几个概念（它们在前置讲义里已建立）：

- **target / chain / chain table**：target 是「一个 SSD 上一块独立的存储区」；chain 是一条 CRAQ 复制链，由若干 target 串成；chain table 是一组 chain 的有序清单，供 meta 服务为文件做条带化。详见 [u3-l4 ChainTable / Chain / Target 数据模型](u3-l4-chain-target-model.md)。
- **CRAQ（写全读任何）**：写请求从链头沿链传播，读请求可打链上任意 target。这意味着节点故障时，该节点上 target 的读流量会被重定向到**同链其它副本所在节点**。详见 [docs/design_notes.md:149-151](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L149-L151)。
- **admin_cli**：无状态瘦客户端，可单条、`;` 批量、`< file` 逐行执行命令。本讲要用到 `create-target`、`upload-chains`、`upload-chain-table`。详见 [u1-l3 部署一个测试集群与 admin_cli](u1-l3-deploy-and-admin-cli.md)。
- **整数规划（Integer Programming）**：在一组线性约束下，求满足条件的 0/1 整数解。本讲不需要你写过求解器，只需理解「变量 + 约束 + 目标」三件套即可。
- **区组设计（Block Design）**：组合数学概念，研究「把若干个点（point）分到若干个区组（block）里」的方案。本讲会把节点当「点」、把链当「区组」。

> 提示：本讲的 Python 脚本只依赖 Pyomo + HiGHS 求解器，**不需要**真实集群就能跑通「生成链表」这一步；只有最后「导入集群」一步需要 mgmtd 在线。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [deploy/data_placement/src/model/data_placement.py](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py) | 把数据放置建模成 BIBD / 整数规划，调用 HiGHS 求解，产出「关联矩阵（incidence matrix）」 |
| [deploy/data_placement/src/setup/gen_chain_table.py](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py) | 读关联矩阵，生成 `generated_chains.csv`、`generated_chain_table.csv`、`create_target_cmd.txt` 等 |
| [deploy/data_placement/README.md](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/README.md) | 用法说明与示例输出 |
| [deploy/README.md](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md) | 六节点部署手册，其中 Step 7 串联了本讲的全部命令 |
| [src/client/cli/admin/UploadChains.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChains.cc) | `upload-chains` 命令实现：解析 CSV、调用 `setChains` |
| [src/client/cli/admin/UploadChainTable.cc](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChainTable.cc) | `upload-chain-table` 命令实现：解析 CSV、调用 `setChainTable` |
| [src/client/cli/admin/CreateTarget.cc](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/CreateTarget.cc) | `create-target` 命令实现：在指定节点的指定盘上建 target |

数据流向（一次完整的链表生成与导入）：

```
节点拓扑(v,k,r) ──► data_placement.py ──► incidence_matrix.pickle
                                              │ (关联矩阵：每个 target 属于哪条链)
                                              ▼
                                     gen_chain_table.py
                                              │
                 ┌────────────────────────────┼─────────────────────────────┐
                 ▼                            ▼                             ▼
       generated_chains.csv        generated_chain_table.csv       create_target_cmd.txt
       (ChainId,TargetId,...)        (ChainId 列)              (create-target --... 一行一个)
                 │                            │                             │
                 ▼ (upload-chains)            ▼ (upload-chain-table)        ▼ (< file 批量执行)
              mgmtd setChains            mgmtd setChainTable            storage createTarget
```

## 4. 核心概念与源码讲解

### 4.1 平衡区组设计：为什么链表要这么排

#### 4.1.1 概念说明

先看一个**反例**。假设 6 个节点 A–F，每节点 1 块 SSD，每块 SSD 建 5 个 target，三副本。设计_notes 给出了一张「朴素」链表（前 10 条链）：

```
链1: A1 → B1 → C1      链2: D1 → E1 → F1
链3: A2 → B2 → C2      链4: D2 → E2 → F2
...
```

在这张表里，**A 的所有 target 只和 B、C 共链**。当 A 故障，A 的全部读流量只能重定向到 B、C 两个节点。在重负载下 B、C 的读带宽瞬间被打满，成为整个系统的瓶颈；而替换 SSD + 同步数据往往要数小时，这期间吞吐严重受损。详见 [docs/design_notes.md:128-132](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L128-L132)。

解决思路很直觉：**让 A 和其余每个节点都共过链**。这样 A 故障时，它的读流量会被均匀分摊到其余 5 个节点上，每个节点只多扛 1/5。这张「均衡」链表见 [docs/design_notes.md:134-145](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L134-L145)，其中 A1 同时出现在和 B、C、D、E、F 共链的多条链里。

把这件事说精确，就是组合数学里的 **平衡不完全区组设计（Balanced Incomplete Block Design, BIBD）**：

- 把 **v 个节点** 看成「点（point）」；
- 把 **b 条链** 看成「区组（block）」，每个区组含 **k 个点**（k = 副本数）；
- 要求**任意一对点恰好同时出现在 λ 个区组里**。

这里的 λ（读作 lambda）正是「恢复期每对节点之间分担的流量」——它越小且越均匀，故障时的恢复流量就越分散。design_notes 明确点出这一点：

> To achieve maximum read throughput during recovery, the load balance problem can be formulated as a balanced incomplete block design. The optimal solution is obtained by using integer programming solver.
> —— [docs/design_notes.md:147](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L147)

#### 4.1.2 核心流程

BIBD 有 5 个标准参数，记作 \((v, b, r, k, \lambda)\)。3FS 给它们一一对应到了集群概念，见 [data_placement.py:54-72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L54-L72)：

| BIBD 参数 | 代码属性 | 含义 |
| --- | --- | --- |
| \(v\) | `num_nodes` | 存储节点数 |
| \(b\) | `num_groups` | 链（区组）数 |
| \(r\) | `num_targets_per_disk` | 每个节点（逻辑盘）上的 target 数 |
| \(k\) | `group_size` | 每条链的 target 数 = 副本数（CR）或 EC 组大小 |
| \(\lambda\) | `max_recovery_traffic_on_peer` | 故障时单节点对外分担的最大恢复流量 |

BIBD 有三条经典恒等式，3FS 的代码与可行性与它们一致：

\[ b = \frac{v \cdot r}{k}, \qquad r(k-1) = \lambda_{\text{BIBD}}(v-1), \qquad bk = vr \]

其中 \(\lambda_{\text{BIBD}}\) 是「每对点共区的精确次数」。**当 \(vr\) 不能被 \(k\) 整除，或 \(\lambda_{\text{BIBD}}\) 不是整数时，严格的 BIBD 不存在**，3FS 就退而求其次——允许流量在 \([\lambda - \text{lb}, \lambda + \text{ub}]\) 区间内浮动，这就是后面会看到的 `-lb` / `-ub` 松弛参数。

恢复流量的计算分两种复制类型，见 [data_placement.py:91-100](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L91-L100)：

\[ \text{sum\_recovery\_traffic\_per\_failure} = r \cdot \text{factor}, \quad \text{factor} = \begin{cases} 1 & \text{CR（链复制）} \\ k-1 & \text{EC（纠删码）} \end{cases} \]

\[ \lambda = \left\lceil \frac{\text{sum\_recovery\_traffic\_per\_failure}}{v-1} \right\rceil \]

整个建模—求解流程是：

1. 用 `find_params` 在给定 \((v,k)\) 下找一个合法的 \(r\)（保证 \(b\) 整数、满足 Fisher 不等式等）；
2. `build_model` 建立整数规划：变量是「节点 d 是否在链 g 里」、约束是「每链恰好 k 节点 / 每节点恰 r 个 target / 节点对流量在窗口内」；
3. `solve` 调 HiGHS 求解；若不可行或超时，`run` 的循环会自动放宽 lb/ub 重试；
4. 解出后保存关联矩阵 `incidence_matrix.pickle` 供下一步用。

#### 4.1.3 源码精读

**（a）找参数 `find_params`。** 给定节点数 \(v\)、副本数 \(k\)，从最小 target 数开始递增寻找满足整除与下界条件的 \(r\)，见 [data_placement.py:106-114](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L106-L114)：

```python
@staticmethod
def find_params(v, k, min_r=1, max_r=100, bibd_only=False):
  if bibd_only: min_r = max(min_r, k)
  for r in range(min_r, max_r):
    if v * r % k == 0 and r * (k - 1) >= v - 1:      # b 为整数 & Fisher 下界
      b = v * r // k
      if not bibd_only or r * (k - 1) % (v - 1) == 0:  # BIBD 需 λ 整数
        return v, b, r, k
  raise ValueError(f"cannot find valid params: {v=}, {k=}")
```

两个判据对应：`v*r % k == 0` 保证链数 \(b\) 为整数；`r*(k-1) >= v-1` 是 Fisher 不等式的推论（保证每对节点至少能碰一次）。`bibd_only` 模式额外要求 `r*(k-1) % (v-1) == 0`，即 \(\lambda_{\text{BIBD}}\) 必须整数。

**（b）「是否真的均衡」的判定。** 见 [data_placement.py:86-104](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L86-L104)：

```python
@property
def balanced_peer_traffic(self):
  return self.all_targets_used and self.sum_recovery_traffic_per_failure % (self.num_nodes-1) == 0

@property
def balanced_incomplete_block_design(self):
  return self.bibd_only and self.balanced_peer_traffic and self.relax_ub == 0
```

`balanced_peer_traffic` 要求「总 target 全用上」且「恢复流量能被其余节点整除」（即 \(\lambda\) 恰为整数、可严格均衡）。只有同时满足 `bibd_only`、严格均衡、`ub==0`，才算「真 BIBD」，此时约束会写成等式（见下面约束函数）。

**（c）整数规划模型 `build_model`。** 决策变量是 0/1 矩阵 `disk_used_by_group[disk, group]`，表示「节点 disk 是否参与链 group」，见 [data_placement.py:219](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L219)：

```python
model.disk_used_by_group = po.Var(model.disks, model.groups, domain=po.Binary)
```

三条核心约束：

1. **每节点 target 数受限**（[data_placement.py:242-247](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L242-L247)）：全用上时取等、否则取 ≤。
2. **每条链恰好 k 个节点**（[data_placement.py:249-251](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L249-L251)）：`sum(disk_used_by_group[disk,group] for disk) == group_size`。
3. **节点对恢复流量在窗口内**（[data_placement.py:253-275](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L253-L275)）。这是建模的灵魂，节选关键两段：

```python
def peer_recovery_traffic_upper_bound(model, disk, peer):
  if self.balanced_incomplete_block_design:
    return calc_peer_recovery_traffic(model, disk, peer) == self.max_recovery_traffic_on_peer   # 严格 BIBD：等式
  else:
    return calc_peer_recovery_traffic(model, disk, peer) <= self.max_recovery_traffic_on_peer + self.relax_ub  # 否则：上界 λ+ub

def peer_recovery_traffic_lower_bound(model, disk, peer):
  return calc_peer_recovery_traffic(model, disk, peer) >= max(0, self.max_recovery_traffic_on_peer - self.relax_lb)  # 下界 λ-lb
```

其中 `calc_peer_recovery_traffic` 是「节点 disk 与 peer 共同出现在多少条链里」：

```python
def calc_peer_recovery_traffic(model, disk, peer):
  if self.qlinearize:
    return po.quicksum(model.disk_in_same_group[disk,peer,group] for group in model.groups)
  else:
    return po.quicksum(calc_disk_in_same_group(model, disk, peer, group) for group in model.groups)
```

**为什么需要 `qlinearize`？** 「两节点共链」本质是两个 0/1 变量的乘积 \(x_d \cdot x_p\)，是**二次**项，HiGHS 这类线性整数规划求解器处理不了。`-ql` 开关引入辅助 0/1 变量 `disk_in_same_group` 并用三条线性约束把它等价替换（标准的线性化技巧），见 [data_placement.py:225-240](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L225-L240)：

```python
def define_disk_in_same_group_lower_bound(model, disk, peer, group):
  return model.disk_used_by_group[disk,group] + model.disk_used_by_group[peer,group] <= model.disk_in_same_group[disk,peer,group] + 1
def define_disk_in_same_group_upper_bound1(model, disk, peer, group):
  return model.disk_in_same_group[disk,peer,group] <= model.disk_used_by_group[disk,group]
def define_disk_in_same_group_upper_bound2(model, disk, peer, group):
  return model.disk_in_same_group[disk,peer,group] <= model.disk_used_by_group[peer,group]
```

这三条约束合起来恰好 forcing \(y = x_d \wedge x_p\)（两者都为 1 时 \(y\) 被迫为 1，否则为 0）。

> 注意：本模型其实是个**可行性问题**，目标是 `expr=1` 的占位目标（[data_placement.py:281](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L281)）。真正带优化目标的是扩容用的 `RebalanceTrafficModel`——它最小化「需要搬动的 target 数」，见 [data_placement.py:436-442](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L436-L442)。

**（d）求解与自动松弛 `run`。** 见 [data_placement.py:116-149](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L116-L149)。循环最多跑 `λ*2` 轮，每轮逐步加大求解时限；一旦「不可行」或「超时」，就在 `auto_relax` 下放宽 lb/ub 后重试（[data_placement.py:134-137](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L134-L137)）：

```python
except (InfeasibleModel, SolverTimeout) as ex:
  if auto_relax:
    self.relax_lb = init_relax_lb + (loop+1) // 2
    self.relax_ub = init_relax_ub + (loop+2) // 2
    continue
```

解出后 `save_solution` 把关联矩阵 pickle 落盘（[data_placement.py:350-353](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L350-L353)），输出目录名形如 `DataPlacementModel-v_5-b_10-r_6-k_3-λ_2-lb_1-ub_1`——名字里的 `λ`、`lb`、`ub` 就是最终采用的参数，方便复现。

#### 4.1.4 代码实践

**实践目标**：在不依赖任何集群的情况下，亲手跑通求解器，观察「节点对恢复流量」是否被压平。

**操作步骤**：

1. 安装依赖（仅需 Python 环境）：

   ```bash
   cd deploy/data_placement
   pip install -r requirements.txt   # 关键是 pyomo + highspy==1.8.0
   ```

2. 跑 5 节点、三副本、每盘至少 6 target 的链复制模型（与 [deploy/data_placement/README.md:24](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/README.md#L24) 一致）：

   ```bash
   python src/model/data_placement.py -ql -relax -type CR \
     --num_nodes 5 --replication_factor 3 --min_targets_per_disk 6 --init_timelimit 600
   ```

3. 观察日志里的 `min_peer_traffic` / `max_peer_traffic` 与每对节点的流量。

**需要观察的现象**：

- 日志应出现 `optimal solution` 与形如 `saved solution to: output/DataPlacementModel-v_5-b_10-r_6-k_3-λ_2-lb_1-ub_1` 的成功行（对照 [deploy/data_placement/README.md:28-55](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/README.md#L28-L55)）。
- 每对节点的 `peer traffic` 都落在 `[λ-lb, λ+ub]` 内。本例 \(\lambda=2\)、最终松弛到 `lb=1, ub=1`，所以每对流量应在 \([1, 3]\)；README 示例里所有节点对都是 `1.5`，说明被压到了窗口下沿附近、非常均衡。
- `output/.../incidence_matrix.pickle` 与 `peer_traffic_map.pickle` 被生成。

**预期结果**：5 节点时 \(v=5,b=10,r=6,k=3,\lambda=2\)；总恢复流量 `total_traffic=30.0`，`max_total_traffic=30`（见 [data_placement.py:329-332](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L329-L332)）。若求解器版本不同，lb/ub 可能略有差异，但 λ、b 不变。**若本地未装 highspy，此步会因找不到求解器而跳过（测试用 `@pytest.mark.skipif` 守护，见 [test_model.py:53](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/test/test_model.py#L53)），属正常。**

#### 4.1.5 小练习与答案

**练习 1**：6 节点、三副本，若要严格 BIBD（`bibd_only`），至少每盘几个 target？

**答案**：`find_params` 在 `bibd_only` 时令 `min_r = max(min_r, k) = 3`，并要求 `r*(k-1) % (v-1) == 0`，即 `r*2 % 5 == 0`，最小的 \(r\) 是 5。所以每盘至少 5 个 target（这正是 design_notes 的 6 节点示例：每 SSD 5 target，共 30 target，10 条链）。

**练习 2**：把 `-type` 从 `CR` 换成 `EC`、`group_size` 设 12，\(\lambda\) 会怎样变化？

**答案**：EC 的 `recovery_traffic_factor = k-1 = 11`（[data_placement.py:91-92](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L91-L92)），`sum_recovery_traffic_per_failure = r*11`，分摊到 \(v-1\) 个节点，\(\lambda = \lceil r\cdot 11/(v-1)\rceil\)，比 CR 大得多——这正是 EC 故障恢复更重、更需要精心放置的原因。

---

### 4.2 链表生成：从关联矩阵到 CSV

#### 4.2.1 概念说明

4.1 产出的是一张抽象的「关联矩阵」——它只回答「逻辑上第几个 target 属于第几条链」，**不含真实节点号、盘号、target/chain ID**。要把这张矩阵变成 mgmtd 能吃的链表，还差三步：

1. **把逻辑 target 映射到真实物理位置**：第几个节点、第几块盘、盘上第几个 target；
2. **给每个 target 和每条链分配全局唯一的 ID**（按可读的位段编码）；
3. **输出三件套**：链定义 CSV、链表 CSV、建 target 命令脚本。

`gen_chain_table.py` 就是干这三件事的。它额外引入一个模型里没有的维度——**每节点的物理盘数 `num_disks_per_node`**：同一份均衡好的关联矩阵会被「复制」到每块物理盘上，每块盘各得一套独立的 chain ID。这样节点级的恢复均衡被保留，同时把负载铺到多块 SSD。

#### 4.2.2 核心流程

`generate_chains` 的主循环结构（[gen_chain_table.py:37-54](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L37-L54)）：

```
for disk_index in range(num_disks_per_node):       # 外层：逐块物理盘
    for node_id in range(begin, end+1):            # 中层：逐节点
        for target_index in range(num_targets_per_disk):   # 内层：逐 target
            target_id = calc_target_id(...)         # 算 target ID
            查关联矩阵得到 group(=chain_index)        # 决定它属于哪条链
            chain_id  = (按 disk_index 编码)          # 算 chain ID
            把 target 塞进 chain_target_list[chain_id]
```

**ID 编码规则**是理解输出的关键。target ID 按固定宽度位段拼成（[gen_chain_table.py:12-13](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L12-L13)）：

\[ \text{target\_id} = (((\text{prefix}\cdot 10^6 + \text{node})\cdot 10^3 + (\text{disk}+1))\cdot 10^2 + (\text{target}+1)) \]

即 `[prefix 2位][node 6位][disk 3位][target 2位]`，共 12 位数字，人眼可直接读出归属。chain ID 类似：`[chain_prefix 3位含盘号][chain_index 5位]`（[gen_chain_table.py:51](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L51)）。

**CR 与 EC 的链结构不同**（[gen_chain_table.py:44-50](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L44-L50)）：

- **CR**：`chain_index = group`，同一条链装下整组 \(k\) 个 target（每个链含 `group_size` 个 target）。
- **EC**：把一个组拆成 \(k\) 条「单 target 链」，`chain_index = (group-1)*group_size + slot`，于是每条链只有 1 个 target，\(k\) 条单 target 链合起来才是一个 EC 组。

输出三个文件（[gen_chain_table.py:100-120](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L100-L120)）：

| 文件 | 内容 | 给谁用 |
| --- | --- | --- |
| `generated_chains.csv` | `ChainId,TargetId,TargetId,...` 每行一条链 | `upload-chains` |
| `generated_chain_table.csv` | 单列 `ChainId`，链的有序清单 | `upload-chain-table` |
| `create_target_cmd.txt` | 每行一条 `create-target ...` 命令 | `< file` 批量喂给 admin_cli |

#### 4.2.3 源码精读

**（a）ID 编码函数。** 见 [gen_chain_table.py:12-13](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L12-L13)：

```python
def calc_target_id(target_id_prefix: int, node_id: int, disk_index: int, target_index: int):
  return ((target_id_prefix * 1_000_000 + node_id) * 1_000 + (disk_index+1)) * 100 + (target_index+1)
```

以部署示例 `target_id_prefix=1`、节点 `10001`、`disk_index=0`、`target_index=0` 为例：算得 `101000100101`，拆位即「1 | 010001 | 001 | 01」= 前缀 1、节点 10001、盘 1、target 1。

**（b）CR/EC 的链归属。** 见 [gen_chain_table.py:44-50](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L44-L50)：

```python
if chain_table_type == "EC":
  group_slot_idx[groups[target_pos]] += 1
  chain_index = (groups[target_pos]-1) * group_sizes[0] + group_slot_idx[groups[target_pos]]
else:
  chain_index = groups[target_pos]
```

其中 `groups[target_pos]` 来自关联矩阵——`target_pos = (node_id - begin) * num_targets_per_disk + target_index`，是 target 在「无盘维度」扁平矩阵里的位置（[gen_chain_table.py:42](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L42)）。注意外层 `disk_index` 循环只是把同一矩阵复用到每块盘，不参与 `target_pos`，所以每块盘的放置模式一致、仅 chain_id 因盘号不同而不同。

**（c）产出 CSV 与命令。** 链定义与链表见 [gen_chain_table.py:100-108](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L100-L108)：

```python
with open(.../generated_chains.csv, "w") as fout:
  print(f"ChainId,{','.join(['TargetId']*len(chain_list[0].target_list))}", file=fout)
  for chain in chain_list:
    print(f"{chain.chain_id},{','.join(str(t.target_id) for t in chain.target_list)}", file=fout)

with open(.../generated_chain_table.csv, "w") as fout:
  print("ChainId", file=fout)
  for chain in chain_list:
    print(f"{chain.chain_id}", file=fout)
```

建/删 target 命令见 [gen_chain_table.py:110-120](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L110-L120)，每行形如：

```python
print(f"create-target --node-id {t.node_id} --disk-index {t.disk_index} "
      f"--target-id {t.target_id} --chain-id {chain.chain_id} {chunk_size_opt} --use-new-chunk-engine", file=fout)
```

注意它默认带 `--use-new-chunk-engine`（走 Rust chunk engine，见 [u6-l1 Chunk Engine 总览与 C++/Rust FFI](u6-l1-chunk-engine-overview.md)），并可选 `--chunk-size` 列表。

#### 4.2.4 代码实践

**实践目标**：把 4.1 产出的关联矩阵变成可直接检视的 CSV，并验证 CR 模式下「每条链恰好 3 个 target」。

**操作步骤**：

1. 在 4.1 已生成 `output/DataPlacementModel-v_5-b_10-r_6-k_3-λ_2-lb_1-ub_1/incidence_matrix.pickle` 之后，跑（参数取自 [deploy/README.md:277-282](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L277-L282)）：

   ```bash
   python src/setup/gen_chain_table.py --chain_table_type CR \
     --node_id_begin 10001 --node_id_end 10005 \
     --num_disks_per_node 16 --num_targets_per_disk 6 \
     --target_id_prefix 1 --chain_id_prefix 9 \
     --incidence_matrix_path output/DataPlacementModel-v_5-b_10-r_6-k_3-λ_2-lb_1-ub_1/incidence_matrix.pickle
   ```

2. 检视输出：

   ```bash
   ls -1 output/                      # 应见 create_target_cmd.txt 等 3 个新文件
   head -n 3 output/generated_chains.csv
   head -n 3 output/generated_chain_table.csv
   head -n 2 output/create_target_cmd.txt
   ```

**需要观察的现象**：

- `generated_chains.csv` 表头是 `ChainId,TargetId,TargetId,TargetId`（CR 三副本 → 3 个 TargetId 列）；每行的 3 个 TargetId 分属不同节点（因为同链三副本必须跨节点）。
- `generated_chain_table.csv` 只有一列 `ChainId`，行数 = 链数。
- `create_target_cmd.txt` 每行一个 `create-target`，node_id 在 10001–10005 间循环、disk_index 在 0–15 间循环。
- target_id 如 `101000100101`，按位段可读出归属。

**预期结果**：5 节点 × 16 盘 × 6 target = 480 个 target；CR 三副本 → 160 条链。`generated_chains.csv` 应有 160 行数据、每行 3 个 TargetId。**这些数字可用 `wc -l output/generated_chains.csv` 自行核对。若你未跑 4.1，可先从 [test_setup.py:10-31](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/test/test_setup.py#L10-L31) 的测试用例复现一份关联矩阵再走本步。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generated_chains.csv` 每行的 3 个 TargetId 一定分属 3 个不同节点？

**答案**：因为关联矩阵的 BIBD 约束「每条链恰好 k=3 个**不同节点**」（`enough_disks_assigned_to_each_group`，[data_placement.py:249-251](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L249-L251)），且每个节点在一条链里至多出现一次。三副本同链必跨三节点，单节点故障至多丢一个副本，这正是 CRAQ 强一致的前提。

**练习 2**：EC 模式下 `generated_chains.csv` 每行有几个 TargetId？链数是多少？

**答案**：1 个（[gen_chain_table.py:62-64](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/setup/gen_chain_table.py#L62-L64) 断言 `len(target_ids) == 1`）；链数 = target 总数 = `num_nodes * num_disks_per_node * num_targets_per_disk`。EC 把「组」拆成多条单 target 链，组的概念在 EC 数据恢复时才体现。

---

### 4.3 建链命令：把链表灌进 mgmtd

#### 4.3.1 概念说明

4.2 产出的还只是磁盘上的文件。要让集群真正「认识」这些链，必须通过 admin_cli 把它们写进 mgmtd 维护的 `RoutingInfo`（最终落 FoundationDB）。这一步严格遵循 u3 单元建立的次序（见 [u3-l4 ChainTable / Chain / Target 数据模型](u3-l4-chain-target-model.md)）：**先建 target，再 upload chains，最后 upload chain table**。

- **`create-target`**：在某个节点的某块盘上实际创建一个 target（数据面落盘），并把它登记到指定 chain。这是唯一会触发 storage 服务在 SSD 上分配空间的命令。
- **`upload-chains`**：把「链 → target 列表」的拓扑批量写入 mgmtd（`setChains`）。
- **`upload-chain-table`**：把若干 chain 组织成一张有序链表写入 mgmtd（`setChainTable`），meta 服务后续就从这张表里为文件选链条带化。

为什么次序不能乱？因为 chain table 引用的是 chain id，而 upload-chains 又要求 target 已存在——前一步是后一步的前置事实。

#### 4.3.2 核心流程

部署手册 [deploy/README.md:284-300](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L284-L300) 给出的标准三连（已省略重复的 cfg 前缀）：

```
# 1) 批量建 target（< file 逐行喂命令）
admin_cli ... < output/create_target_cmd.txt

# 2) 上传链
admin_cli ... "upload-chains output/generated_chains.csv"

# 3) 上传链表（1 是 chain table id，须与 init-cluster 时的表 id 一致）
admin_cli ... "upload-chain-table --desc stage 1 output/generated_chain_table.csv"

# 4) 验证
admin_cli ... "list-chains"
admin_cli ... "list-chain-tables"
```

注意第 3 步的 `1` 是 chain table ID——它必须等于 `init-cluster` 时写入根目录 Layout 的 `chainTableId`（见 [deploy/README.md:146-152](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L146-L152)，参数依次为 chain table id=1、chunk size、stripe size）。

#### 4.3.3 源码精读

**（a）`create-target` 的参数。** 见 [CreateTarget.cc:14-23](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/CreateTarget.cc#L14-L23)，必填 `--node-id / --disk-index / --target-id / --chain-id`，可选 `--chunk-size` 与 `--use-new-chunk-engine`。命令执行时会先刷新路由信息，并**禁止重建处于 `LASTSRV` 状态的 target**（[CreateTarget.cc:56-65](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/CreateTarget.cc#L56-L65)）——这是 u3-l5 状态机里的「最后服务者」保护，避免误操作丢数据。

**（b）`upload-chains` 解析 CSV 并调用 `setChains`。** 见 [UploadChains.cc:68-92](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChains.cc#L68-L92)，下面是这段逻辑的伪代码（示例代码，非逐字源码）：

```
for (每行 row) {
  ChainSetting chain;
  chain.chainId            = row[0];           // 第一列 ChainId
  chain.setPreferredTargetOrder = <命令行开关>;
  for (j = 1; j < row.size(); ++j) {
    ChainTargetSetting target;
    target.targetId        = row[j];           // 其余列都是 TargetId
    chain.targets.push_back(target);
  }
  chains.push_back(chain);
}
co_await mgmtdClient->setChains(userInfo, chains);
```

它严格校验：首列必须叫 `ChainId`、其余列必须都叫 `TargetId`（[UploadChains.cc:48-62](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChains.cc#L48-L62)）——这正是 `gen_chain_table.py` 表头 `ChainId,TargetId,TargetId,...` 的来源，两边格式是咬合的。`-d/--dump-template` 可导出模板 CSV（[UploadChains.cc:29-37](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChains.cc#L29-L37)）。

**（c）`upload-chain-table` 解析单列 CSV 并调用 `setChainTable`。** 见 [UploadChainTable.cc:59-84](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChainTable.cc#L59-L84)，伪代码如下（示例代码）：

```
auto tableId = parser.get<uint32_t>("tableId");        // 位置参数：表 id
for (每行) chainIds.emplace_back(row[0]);               // 单列 ChainId
auto rsp = co_await mgmtdClient->setChainTable(userInfo, tableId, chainIds, desc);
// 回包含 chainTableVersion，打印 "Upload {tableId} of {version} succeeded"
```

它要求 CSV **只有一列**且名为 `ChainId`（[UploadChainTable.cc:46-53](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/UploadChainTable.cc#L46-L53)），并校验 ChainId 为正且不溢出。回包里的 `chainTableVersion` 就是 u3-l4 讲过的「链表版本号」——每次 `setChainTable` 都会让它单调递增。

> 三条命令都通过 admin_cli 的 `Dispatcher` 注册（`registerAdminCommands.cc` 里统一挂载），最终落到 mgmtd 的 `setChains` / `setChainTable` / storage 的 `createTarget`，与 u3-l6 讲的路由信息分发链路衔接。

#### 4.3.4 代码实践

**实践目标**：把 4.2 产出的链表导入一个测试集群，并用 `list-*` 命令验证。

**操作步骤**（前置：已完成 u1-l3 的部署到 Step 6，集群里有 5 个 storage 节点 10001–10005、每节点 16 盘已挂载，并已 `user-add` 拿到 admin token 存于 `/opt/3fs/etc/token.txt`）：

1. 批量建 target（`<` 重定向逐行执行，命令取自 [deploy/README.md:286](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L286)）：

   ```bash
   /opt/3fs/bin/admin_cli --cfg /opt/3fs/etc/admin_cli.toml \
     --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' \
     --config.user_info.token $(<"/opt/3fs/etc/token.txt") \
     < output/create_target_cmd.txt
   ```

2. 上传链与链表（[deploy/README.md:290-295](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L290-L295)）：

   ```bash
   /opt/3fs/bin/admin_cli --cfg /opt/3fs/etc/admin_cli.toml \
     --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' \
     --config.user_info.token $(<"/opt/3fs/etc/token.txt") \
     "upload-chains output/generated_chains.csv"

   /opt/3fs/bin/admin_cli --cfg /opt/3fs/etc/admin_cli.toml \
     --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' \
     --config.user_info.token $(<"/opt/3fs/etc/token.txt") \
     "upload-chain-table --desc stage 1 output/generated_chain_table.csv"
   ```

3. 验证（[deploy/README.md:298-299](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L298-L299)）：

   ```bash
   /opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml \
     --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' "list-chains"
   /opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml \
     --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' "list-chain-tables"
   ```

**需要观察的现象**：

- `upload-chains` 回显 `Upload N chains succeeded`（N=160）。
- `upload-chain-table` 回显 `Upload 1 of <version> succeeded`，`<version>` ≥ 1。
- `list-chains` 打印出全部链及其 target；`list-chain-tables` 打印出 id=1 的表及其包含的 chain id 列表，与 CSV 内容一致。

**预期结果**：链表 id=1 与 `init-cluster` 写入根 Layout 的 chainTableId 对齐；之后在该目录下建文件，meta 就会从这张表 round-robin 选链条带化（见 [u4-l4 文件数据布局与链分配](u4-l4-layout-and-chain-alloc.md)）。**本步依赖真实集群与 RDMA 网络，若本地无集群，建议在容器/虚拟环境中按 u1-l3 搭建最小集群后再验证；否则标注「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：如果误把 `upload-chain-table` 的表 id 写成 2，会发生什么？

**答案**：mgmtd 会**新建**一张 id=2 的链表（u3-l4 讲过多张 ChainTable 按 id 共存）。它不会覆盖 id=1。但根目录 Layout 仍指向 id=1，所以新建文件依旧用旧表——id=2 这张表成了「孤儿」，除非你显式用 `SetDirLayout` 把某目录指向它。这就是为什么表 id 必须与 `init-cluster` 的参数对齐。

**练习 2**：`upload-chains` 与 `upload-chain-table` 谁先谁后？为什么？

**答案**：先 chains 后 table。chain table 只存 ChainId 清单，本身不包含 target 信息；若先传表，表里引用的 ChainId 在 mgmtd 里尚不存在，虽然写入不报错，但客户端解析路由时会拿不到链的 target 列表。标准次序是 create-target → upload-chains → upload-chain-table，每一步为下一步提供事实依据。

---

## 5. 综合实践

把三个模块串起来，完成一次「为一个小集群设计并导入均衡链表」的完整任务。

**场景**：你有 4 个 storage 节点（id 10001–10004），每节点 4 块 SSD，希望三副本、每盘 3 个 target，且故障时恢复流量尽量均衡。

**任务**：

1. **建模**：用 `find_params` 的逻辑手算 \((v,b,r,k,\lambda)\)。\(v=4, k=3\)，`find_params` 从 `min_r=3` 起：\(4r \% 3 == 0\) 且 \(r\cdot2 \ge 3\) → 最小 \(r=3\)（\(4\cdot3/3=4\) 条链）。算 \(\lambda = \lceil r/(v-1)\rceil = \lceil 3/3\rceil = 1\)，且 \(3 \% 3 == 0\) → 这是**严格可均衡**的情形（`balanced_peer_traffic=True`）。
2. **求解**：跑 `data_placement.py -ql -type CR --num_nodes 4 --replication_factor 3 --num_targets_per_disk 3`，确认输出目录名里 `λ_1` 且尽量 `ub_0`（若求解器报不可行再加 `-relax`）。
3. **生成**：跑 `gen_chain_table.py --chain_table_type CR --node_id_begin 10001 --node_id_end 10004 --num_disks_per_node 4 --num_targets_per_disk 3 --target_id_prefix 1 --chain_id_prefix 9 --incidence_matrix_path output/.../incidence_matrix.pickle`。
4. **自检**：用 `awk -F, 'NR>1{print NF-1}' output/generated_chains.csv | sort -u` 确认每行都是 3 个 target；用 `wc -l` 确认链数 = \(4\cdot4\cdot3/3 = 16\) 条。
5. **导入**（有集群时）：按 4.3.4 的三连命令把链表灌进 mgmtd，`list-chains` 验证每条链的 3 个 target 分属 3 个不同节点。

**验收标准**：

- 关联矩阵满足「每对节点共链次数相等」（严格 BIBD，因为 \(\lambda\) 整数）；
- 生成的链表里任意一条链的三副本跨三节点；
- 集群 `list-chain-tables` 能看到 id=1 的表且 chain 数正确。

> 进阶：把节点数加到 5（即 deploy README 的标准示例），观察 \(\lambda\) 从 1 变成 2、且因 \(6\%4\neq0\) 不再严格均衡、需要 `-relax` 松弛——这正是 README 示例目录名带 `lb_1-ub_1` 的原因。

## 6. 本讲小结

- 链表设计的核心目标不是「能存」，而是**故障时恢复流量均衡**：朴素链表会让重定向流量砸在少数节点上拖垮吞吐，BIBD 让每对节点均匀共链、把恢复负载摊平。
- 3FS 把放置问题建模成整数规划：变量是「节点是否在链里」、约束是「每链 k 节点 / 每节点 r target / 节点对流量在 \([\lambda-\text{lb}, \lambda+\text{ub}]\)」、严格 BIBD 时退化为等式约束；二次项靠 `qlinearize` 线性化以适配 HiGHS。
- `data_placement.py` 求解后产出关联矩阵 `incidence_matrix.pickle`，目录名编码 \((v,b,r,k,\lambda,\text{lb},\text{ub})\) 便于复现；不可行/超时时 `-relax` 自动放宽 lb/ub 重试。
- `gen_chain_table.py` 把关联矩阵映射到真实节点/盘/target，按位段编码 ID，区分 CR（每链 k 个 target）与 EC（每链 1 个 target），产出三件套 CSV/命令。
- 导入集群严格三步走：`create-target`（建 target 落盘）→ `upload-chains`（`setChains`）→ `upload-chain-table`（`setChainTable`，表 id 须与 `init-cluster` 对齐），三者 CSV 格式与 admin_cli 解析逻辑严格咬合。

## 7. 下一步学习建议

- **链表被消费的方式**：链表灌进去之后，meta 如何为文件选链、如何条带化？继续读 [u4-l4 文件数据布局与链分配](u4-l4-layout-and-chain-alloc.md)，看 `ChainAllocator` 的 round-robin 与 shuffle。
- **链的状态会变**：target 上线/故障后链版本号如何推进、public 状态如何翻转？读 [u3-l5 Target 状态机与故障检测](u3-l5-target-state-machine.md)，理解你建的 target 后续的生命周期。
- **扩容时的再均衡**：本讲的 `RebalanceTrafficModel`（[data_placement.py:391-443](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/data_placement/src/model/data_placement.py#L391-L443)）在加节点时最小化「需要搬动的 target 数」，可结合 `data_placement_job.py` 的批量参数搜索进一步研究生产扩容流程。
- **自己跑测试**：`deploy/data_placement/test/` 下的 `test_model.py`、`test_setup.py` 用小规模参数验证整套流程，是把本讲知识固化下来的最佳练习起点。
