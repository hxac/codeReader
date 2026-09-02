# P2P bucket 分配算法：带宽最大化贪心

## 1. 本讲目标

本讲是高级单元第五单元的收官之作，精读 P2P 更新路径中最「纯粹」的一段算法代码：`_assign_receiver_ranks`。学完本讲，你应该能够：

1. 说清楚「按 owner 的 RDMA 设备分组 → 列优先展平 → 每轮为每个接收端配一个不重复网卡的桶」这条贪心链路的每一步为什么存在。
2. 理解 `occupied_devices` 轮内去重如何保证任意时刻的活跃拉取流两两占用不同的源网卡，从而使收发双方带宽都被打满。
3. 对一个小规模拓扑（几个 rank、几张网卡、十几个桶）**手工推演**出与 `tests/test_assign_receiver_ranks.py` 期望完全一致的分配结果。

前置依赖：u3-l5（bucket 切分与 `H2DBucket`/`BucketRange`）、u5-l5（P2PStore、RDMA 网卡发现与拓扑键）。

## 2. 前置知识

本讲只涉及一个函数级的算法，但需要先复习三个来自前置讲义的认知。

**第一，桶（bucket）是流水线的调度单位（u3-l5）。** `_gen_h2d_buckets` 把全局参数元数据按 owner 分组、按 `bucket_size`（软上限）切成若干 `H2DBucket`；`BucketRange(idx, offset, size)` 描述一个桶的某段字节来自第 `idx` 块锁页 buffer 的哪个偏移。本讲的输入就是这张桶清单。

**第二，P2P 更新的数据面是 RDMA 单边读（u5-l5）。** owner（权重持有者）在注册 checkpoint 时把自己的锁页内存池登记进 mooncake transfer engine；receiver（接收端）用 `batch_transfer_sync_read` 直接从远端内存把字节搬进自己注册过的 GPU buffer。整个拉取过程 owner 侧 CPU 零参与——相应地，**带宽的物理瓶颈是网卡**：同一张网卡同一时刻挤进多个流，只会互相瓜分带宽，不会凭空变快。

**第三，拓扑表来自 gather_metas（u3-l3）。** 所有 rank 用 `all_gather_object` 交换元数据后，每台 rank 都持有两张「`网卡名@主机IP` → rank 集合」的拓扑表：

- `local_topo`（接收侧）：变量名 `_local_rdma_devices`，描述**当前作业**里哪些 rank 共享哪张网卡；
- `remote_topo`（持有侧）：变量名 `_remote_rdma_devices`，描述**权重实际所在的那批进程**里哪些 rank 共享哪张网卡。默认两者相同；join 复用模式下由 `load_metas` 用旧实例导出的 metas 重建。

两个新术语：

- **owner / receiver**：owner 是桶的字节当前所在的 rank（pinned buffer 的持有者）；receiver 是被指派去「拉这个桶并把它广播给目标组」的 rank。P2P 更新的目标 ranks 里，只有被指派为 receiver 的 rank 才发起 RDMA 读。
- **轮（round）与列主序（column-major）**：分配算法把桶排成一个序列，然后按「轮」消费——每一轮里每个 receiver 至多领一个桶。列主序指展平桶矩阵时先取所有行的第 0 个桶、再取所有行的第 1 个桶……与之相对的是行主序（先取完第 0 行再取第 1 行）。

为什么分配问题只存在于 P2P 路径？因为 colocate/broadcast 路径（`ranks` 为空）下 owner 就是 receiver，装填是**本地**锁页内存的 H2D 拷贝，根本不经过网卡。只有当字节必须跨网卡流动时，「谁去拉哪个桶」才成为一个值得优化的问题。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `checkpoint_engine/ps.py` | `_gen_h2d_buckets`（L68-L105） | 生成桶清单，并在 P2P 分岔处移交给本讲主角 |
| `checkpoint_engine/ps.py` | `_assign_receiver_ranks`（L108-L163） | **本讲核心**：贪心分配 (owner, bucket) → (receiver, owner, bucket) |
| `checkpoint_engine/ps.py` | `gather_metas`（L462-L525，重点 L511-L522） | 构建 `local_topo`，默认令 `remote_topo` 与之相同 |
| `checkpoint_engine/ps.py` | `load_metas`（L295-L303） | join 模式下用外部 metas 重建 `remote_topo` |
| `checkpoint_engine/ps.py` | `_update_per_bucket`（L751-L940，重点 L804-L840、L855-L905） | 消费分配结果：拉取清单 + 广播编排 |
| `checkpoint_engine/ps.py` | `_copy_to_buffer`（L684-L714）、`_get_addr_ptrs`（L716-L719） | 把「拉取」落实为 mooncake 批量同步读 |
| `checkpoint_engine/data_types.py` | L78-L87、L103-L111、L19 | `BucketRange`/`H2DBucket`、带 `rdma_device` 字段的元数据模型、泛型参数 `T` |
| `tests/test_assign_receiver_ranks.py` | L6-L59（6 组参数化用例）、L61-L68 | 纯 CPU 单元测试，是本讲实践的验收标准 |

注意：`tests/test_assign_receiver_ranks.py` **没有** `gpu` marker，可以在纯 CPU 环境直接运行，这将是本讲实践的主要工具。

## 4. 核心概念与源码讲解

### 4.1 local_topo 与 remote_topo：两张「网卡 → rank」拓扑表

#### 4.1.1 概念说明

分配算法的一切决策都建立在两张拓扑表上。它们回答同一个问题的两个方向：

- `local_topo`：**接收侧**——目标 ranks 里，哪些 rank 挂在同一张网卡后面？挂在一起的 rank 共享这张网卡的带宽，让它们同时拉数据只会互相争抢。
- `remote_topo`：**持有侧**——权重所在的那批进程里，哪些 rank 共享同一张网卡？从同一张源网卡并行走两个流，同样是在瓜分同一份带宽。

理想情况下，任意时刻的并行拉取流数是

\[ P = \min(|\text{接收侧网卡组数}|,\ |\text{持有侧网卡组数}|) \]

总传输时间的下界约为

\[ T \approx \frac{D}{P \cdot B_{\text{nic}}} \]

其中 \( D \) 是总字节数、\( B_{\text{nic}} \) 是单网卡带宽。分配算法的全部意义，就是让实际的并行度贴近这个 \( P \)。

两张表在普通 P2P 更新与 join 复用模式下来源不同：

| 场景 | local_topo 来源 | remote_topo 来源 | 两侧 rank 是否同批进程 |
| --- | --- | --- | --- |
| 同一作业内 P2P（`ranks` 指定） | 本作业 `gather_metas` | `local_topo` 的浅拷贝（默认同拓扑） | 是 |
| join 复用（新实例拉旧实例权重） | 新实例自己的 `gather_metas` | `load_metas` 从旧实例 metas 重建 | **否**（rank 编号是两套命名空间） |

#### 4.1.2 核心流程

1. `gather_metas` 中 `all_gather_object` 收齐全部 rank 的 `DataToGather` 后，逐 rank 归类：以 `rdma_device@p2p_store_addr的主机IP`（无 p2p store 时退化为 `host_ip`）为键，把 rank 编号累进集合。
2. gather 结尾默认认定「发送方与接收方拓扑相同」，直接 `remote = local.copy()`。
3. join 模式下，`load_metas` 用导入的 metas **整体重建** `remote_topo`——只有 metas 里出现过的 owner rank 才会进表（空注册 rank 不在其中）。

#### 4.1.3 源码精读

拓扑键的构造在 gather 循环里，每个 rank（不管有没有权重）都会被归入 local_topo：

[checkpoint_engine/ps.py:511-515](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L511-L515)

这段代码把 rank `i` 加入 `rdma_device@主机IP` 键下的集合。`rdma_device` 与 `p2p_store_addr` 都来自元数据模型：

[checkpoint_engine/data_types.py:103-111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L103-L111)

`MemoryBufferMetaList` 携带 `p2p_store_addr`（mooncake 引擎地址）和 `rdma_device`（网卡名），`DataToGather` 再补上 `host_ip` 与 `device_uuid`（u3-l3 已讲）。

默认「同拓扑」假设与 join 模式的改写入口：

[checkpoint_engine/ps.py:520-522](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L520-L522)

[checkpoint_engine/ps.py:295-303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L295-L303)

`load_metas` 用外部 metas 替换全局参数表后，从零重建 `_remote_rdma_devices`：键仍是 `rdma_device@主机IP`，但集合里只含旧实例的 owner rank，并断言 `rdma_device`、`p2p_store_addr` 非空（join 场景必须有 p2p 通道）。

#### 4.1.4 代码实践

1. **实践目标**：在不运行任何分布式代码的前提下，为一个小集群手写出两张拓扑表。
2. **操作步骤**：假设 2 台主机各 8 卡（rank 0-7 在主机 A、rank 8-15 在主机 B），每机 2 张 RDMA 网卡，`_get_my_rdma_device`（u5-l5）按「连续 rank 共享一块」均分：主机 A 的 rank 0-3 用 `mlx5_0`、rank 4-7 用 `mlx5_1`，主机 B 同理。在纸上写出 `local_topo`。
3. **需要观察的现象**：拓扑键必须包含主机维度；rank 分组按「网卡 → rank 集合」组织而不是按主机。
4. **预期结果**：

   ```python
   # 示例代码：手推结果的自检（可在任何 Python 环境运行，无需安装本仓库）
   local_topo = {
       "mlx5_0@10.0.0.1": {0, 1, 2, 3, 8, 9, 10, 11},
       "mlx5_1@10.0.0.1": {4, 5, 6, 7, 12, 13, 14, 15},
   }
   # 注意：主机 B 的 mlx5_0 与主机 A 的 mlx5_0 是两张不同的物理网卡，
   # 但从 RDMA 拓扑角度它们分属两个键——因为键里的 IP 不同。
   ```

   按本仓库的键构造规则（`网卡名@p2p_store_addr主机IP`），两台主机的同名网卡会落在**不同**的键里；上例把它们合并进同一个集合是**故意画错**的，请你找出并改正（这正是下面的练习 1）。
5. 预期分配结果「待本地验证」的部分：无——本实践是纯手推。

#### 4.1.5 小练习与答案

**练习 1**：拓扑键为什么是「网卡名@主机IP」而不是裸网卡名？

**答案**：带宽瓶颈是**物理网卡**。同一主机上挂同一张网卡的 rank 共享带宽，必须归为一组；而不同主机的两张同名网卡（两台机各有一张 `mlx5_0`）是互不争抢的独立瓶颈点，必须分开。裸网卡名会把它们错误合并，导致 receiver 选择与去重判断失真。此外键里直接复用了 `p2p_store_addr` 的 host 部分，天然带主机维度。

**练习 2**：空注册 rank（分不到权重的 rank，u3-l3 讲过它会「空注册」）会出现在 local_topo 吗？会出现在 `load_metas` 重建的 remote_topo 吗？

**答案**：会出现在 local_topo——gather_metas 的归类循环（L511-L515）对 world 内**每个** rank 执行，与是否持有权重无关，这正是「空注册 rank 不能当 owner 但可以当 receiver」的基础。不会出现在 join 模式的 remote_topo——`load_metas` 只遍历 metas 中实际存在的 owner。至于普通 P2P 场景，`remote = local.copy()` 包含全部 rank，但反查表多几个条目无害（桶清单里根本不会有空注册 rank 的桶）。

**练习 3**：join 模式下 remote_topo 里的 rank 3 和 local_topo 里的 rank 3 是同一个进程吗？

**答案**：不是。remote_topo 的 rank 编号来自**旧实例**导出的 metas，local_topo 的编号是**新实例**自己的 gather 结果，两套编号各自独立。算法对此毫不在意——它只把 rank 当字典键用：owner 侧的键查 remote_topo，receiver 侧的键查 local_topo，两侧通过桶清单这一份全局数据耦合，从不跨表比较 rank。

### 4.2 `_gen_h2d_buckets` 的 P2P 分岔：过滤接收侧、放行 owner

#### 4.2.1 概念说明

`_gen_h2d_buckets` 在 u3-l5 已详细讲过（按 owner 分组、`bucket_size` 是软上限、以对齐槽位为原子、ranges 可跨锁页 buffer）。本讲只关注它结尾的**分岔逻辑**：桶清单生成完毕后，谁来当 receiver？

- `ranks` 为空（colocate/broadcast）：owner 即 receiver，直接返回 `(owner, owner, bucket)`——本地 H2D，不经网卡。
- `ranks` 非空（P2P）：先把 local_topo **按目标 ranks 过滤**，再把桶清单连同两张拓扑表交给 `_assign_receiver_ranks`。

过滤的含义：P2P 更新只服务 `ranks` 圈定的目标组。local_topo 里不含任何目标 rank 的网卡组整组剔除；含目标 rank 的组，组内只保留目标 rank。而 remote_topo **不做**任何过滤——owner 由「权重在哪」客观决定，不随目标组变化。

#### 4.2.2 核心流程

```text
切桶（u3-l5，按 owner 分组、软上限）          # 得到 [(owner_rank, H2DBucket), ...]
  ↓
ranks 为空？
  ├─ 是 → 返回 [(owner, owner, bucket)]        # colocate：本地 H2D，无分配问题
  └─ 否 → actual_local_topo = local_topo 按 ranks 求交集并剔除空组
          → 返回 _assign_receiver_ranks(buckets, actual_local_topo, remote_topo)
```

#### 4.2.3 源码精读

分岔处的完整代码：

[checkpoint_engine/ps.py:97-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L97-L105)

三处要点：`ranks_set` 把列表转集合以便求交；字典推导式 `{k: v & ranks_set ... if v & ranks_set}` 同时完成「组内过滤」与「空组剔除」；注释明确写了 colocate 短路的语义。整个函数签名与调用点：

[checkpoint_engine/ps.py:68-74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L68-L74)

[checkpoint_engine/ps.py:805-811](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L805-L811)

调用点显示桶大小探测（u3-l5 的 `_detect_bucket_size`）先于切桶，两张拓扑表以成员变量身份传入。

#### 4.2.4 代码实践

1. **实践目标**：验证 ranks 过滤的精确语义（组内交集 + 空组剔除）。
2. **操作步骤**：在任意 Python 环境（无需本仓库依赖）运行下面的片段，对照源码 L98-L100 逐行翻译：

   ```python
   # 示例代码：复刻 _gen_h2d_buckets 的 local_topo 过滤
   ranks = [4, 5, 6, 7]
   local_topo = {"mlx5_0@A": {0, 1, 2, 3}, "mlx5_1@A": {4, 5, 6, 7}, "mlx5_0@B": {8, 9}}
   ranks_set = set(ranks)
   actual = {k: v & ranks_set for k, v in local_topo.items() if v & ranks_set}
   print(actual)
   ```

3. **需要观察的现象**：两个网卡组消失，一个组原样保留。
4. **预期结果**：`{'mlx5_1@A': {4, 5, 6, 7}}`——`mlx5_0@A` 交完为空被整组剔除，`mlx5_0@B` 与目标组无交集同样剔除。此为纯字典运算，可手工确认；实际运行输出「待本地验证」。
5. 如果把 `ranks` 改成 `[1]`，请先预测结果再看输出（预期 `{'mlx5_0@A': {1}}`）。

#### 4.2.5 小练习与答案

**练习 1**：`ranks` 为空时为什么不调用 `_assign_receiver_ranks`？

**答案**：colocate/broadcast 下 owner 就是 receiver，`_copy_to_buffer` 做的是本地锁页内存到 GPU 的 H2D 拷贝，不经过任何网卡，不存在网卡争用，也就没有分配优化空间。短路的另一个好处是 broadcast 路径完全不依赖两张拓扑表的正确性。

**练习 2**：为什么 remote_topo 不像 local_topo 那样按 ranks 过滤？

**答案**：owner 集合由 `_current_global_parameter_metas`（权重真实所在）决定，是客观事实；`ranks` 只圈定接收方。若按 ranks 过滤 remote_topo，凡是 owner 不在目标组里的桶（P2P 场景的常态——目标组是新扩的 rank，权重在老 rank 上）就会因 `rank_to_rdma_device[owner_rank]` 查不到而 KeyError。过滤接收侧、放行持有侧，正是「数据不动、视角切换」的体现。

### 4.3 `_assign_receiver_ranks`：分组、列展平、轮转去重

#### 4.3.1 概念说明

这是本讲的主角，一个 55 行的纯函数。它解决的问题可以表述为：给定桶清单（每个桶有 owner）、接收侧拓扑、持有侧拓扑，为每个桶指定一个 receiver，使得**逐桶串行下发时，相邻的拉取天然分布在不同源网卡与不同目的网卡上**。

直觉版的三步贪心：

1. **按 owner 的网卡分组（行）**：同一张源网卡上的桶进同一行。一行内部的桶注定要瓜分这张网卡，行与行之间才能并行。
2. **列优先展平**：交错取桶（所有行的第 0 个、所有行的第 1 个……），保证展平序列中**相邻的桶几乎总来自不同行**。如果按行优先展平，队头会长期被同一张网卡占据。
3. **轮转配对 + 轮内源网卡去重**：每一轮让 receiver 依次从展平序列队头领桶，但一轮之内每张源网卡只允许被领一次（`occupied_devices` 集合）；撞卡的桶原地等待，下一轮重新当队头候选。

三步合起来的效果：任意一轮内，\( R \) 个 receiver 领走的 \( R \) 个桶来自 \( R \) 张互不相同的源网卡，而这 \( R \) 个 receiver 又分属 \( R \) 个不同的接收侧网卡组（`receiver_list` 本来就是每组选一个）——源、目的两侧的网卡同时全部打满。

为什么 receiver_list 是「每个接收侧网卡组选最小 rank」？因为同组的 rank 共享网卡，多选无益；`min(ranks)` 只是确定性的平局裁决。而 `num_receivers = min(两侧网卡组数)`：源网卡组更多时 receiver 轮流覆盖它们；receiver 网卡组更多时，多出的 receiver 一轮内必然撞卡（白配），索性不启用。

#### 4.3.2 核心流程

```text
输入: buckets = [(owner, bucket), ...]   local_topo   remote_topo

① 反查表: rank_to_rdma_device ← 把 remote_topo 的「网卡→rank集合」翻转成「rank→网卡」
② 分组:   按 owner 的网卡把桶归入 defaultdict(list)，得到矩阵 rows（保留插入序）
③ 选接收端: num_receivers = min(len(local_topo), len(分组数))
           receiver_list = local_topo 前 num_receivers 组、每组取 min(rank)
④ 展平:   列主序遍历矩阵 → flattened（短行的空位跳过）
⑤ 分配:   while 还有未分配桶:
             occupied ← ∅
             for receiver in receiver_list:
               桶 ← flattened 队头
               若 桶的源网卡 ∈ occupied: break   # 本轮提前结束，桶原地等待
               记录 (receiver, owner, 桶)；occupied += 源网卡
输出: [(receiver, owner, bucket), ...]
```

终止性：每轮的第一个 receiver 面对空 `occupied` 必然成功，因此每轮至少分配一个桶，循环最多进行「桶数」轮；总复杂度约 \( O(B \cdot R) \)，\( B \) 为桶数、\( R \) 为 receiver 数（通常 ≤ 16），近似线性。

#### 4.3.3 源码精读

函数签名与泛型——桶的类型是一个 `TypeVar`（`data_types.py` L19 定义 `T = TypeVar("T")`），所以单元测试可以直接用字符串当桶，不必构造真的 `H2DBucket`：

[checkpoint_engine/ps.py:108-118](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L108-L118)

① 空表保护与反查表（注意推导式里 `ranks` 是循环变量，指网卡组内的 rank 集合，与 update 的 ranks 参数无关）：

[checkpoint_engine/ps.py:119-124](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L119-L124)

② 按 owner 网卡分组。Python 3.7+ 的 dict/defaultdict 保持插入序，行的顺序 = 网卡在桶清单中首次出现的顺序，行内桶序 = 原清单序：

[checkpoint_engine/ps.py:126-132](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L126-L132)

③ 接收端选择——一网卡一 receiver，数量取两侧网卡组数的较小值：

[checkpoint_engine/ps.py:135-137](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L135-L137)

④ 列主序展平。外层 `for col`、内层 `for row`，`if col < len(...)` 跳过短行的空位：

[checkpoint_engine/ps.py:139-146](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L139-L146)

⑤ 轮转分配。这是 `occupied_devices` 的全部用法：**轮内**源网卡去重，撞卡即 `break`（推迟而非跳过——`assigned_cnt` 不前进，桶下一轮仍是队头）：

[checkpoint_engine/ps.py:148-161](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L148-L161)

**手工推演：测试文件的第 6 组用例。** 输入（见 [tests/test_assign_receiver_ranks.py:40-58](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_assign_receiver_ranks.py#L40-L58)）：13 个桶，owner 为 `i % 8`；接收侧 3 张网卡各带 rank {0}、{1}、{2}；持有侧 4 张网卡，rdma0→{0,1}、rdma2→{2,3}、rdma4→{4,5}、rdma6→{6,7}。

分组（行）与展平：

| 行（源网卡） | 组内桶序 |
| --- | --- |
| rdma0（owners 0,1） | b0, b1, b8, b9 |
| rdma2（owners 2,3） | b2, b3, b10, b11 |
| rdma4（owners 4,5） | b4, b5, b12 |
| rdma6（owners 6,7） | b6, b7 |

```text
列优先展平 = b0 b2 b4 b6 | b1 b3 b5 b7 | b8 b10 b12 | b9 b11
receiver_list = [0, 1, 2]        # num_receivers = min(3, 4) = 3
```

逐轮配对：

| 轮 | receiver 0 | receiver 1 | receiver 2 | 本轮占用源网卡 |
| --- | --- | --- | --- | --- |
| 0 | b0（rdma0） | b2（rdma2） | b4（rdma4） | rdma0/2/4 |
| 1 | b6（rdma6） | b1（rdma0） | b3（rdma2） | rdma6/0/2 |
| 2 | b5（rdma4） | b7（rdma6） | b8（rdma0） | rdma4/6/0 |
| 3 | b10（rdma2） | b12（rdma4） | b9（rdma0） | rdma2/4/0 |
| 4 | b11（rdma2） | — | — | rdma2 |

把上表逐桶改写成 `(receiver, owner, bucket)`：b0→0、b1→1、b2→1、b3→2、b4→2、b5→0、b6→0、b7→1、b8→2、b9→2、b10→0、b11→0、b12→1——与测试文件 L43-L57 的期望列表**逐桶一致**。注意每一轮的三个桶都来自三张不同源网卡：这就是贪心要的交错。

`break` 什么时候真的触发？第 4 组用例（[tests/test_assign_receiver_ranks.py:27-32](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_assign_receiver_ranks.py#L27-L32)，接收侧 4 张网卡、持有侧同上 4 张）：第 2 轮 receiver 0/1/2 领走 b8（rdma0）、b10（rdma2）、b12（rdma4）后，receiver 3 的候选是 b9（rdma0）——已占用，`break`，b9 推迟到第 3 轮由 receiver 0 领走。手工推演的结果同样与该用例的期望公式 `(i // 2 % 4)` 一致。

最后两条设计性质：

- **纯函数、零通信**：所有 rank 用同一份全局状态（gather/load_metas 之后的 metas 与两张拓扑表）独立计算，输入相同则结果相同（依赖 dict 插入序的确定性），无需任何额外集合通信来「协商」分配。
- **按桶数、不按字节数**：`bucket_size` 是软上限，各桶实际大小不一（每个 owner 的最后一个桶往往偏小），贪心只平衡桶的个数，不按累计字节加权——这是静态调度的已知近似，改进方向见综合练习。

#### 4.3.4 代码实践

1. **实践目标**：在纯 CPU 环境跑通现有单测，再用一个「收窄接收侧」的变体观察行为变化。
2. **操作步骤**：
   - 第一步：`pip install -e .` 后运行 `pytest tests/test_assign_receiver_ranks.py -v`，确认 6 组用例全部通过（该文件无 gpu marker）。
   - 第二步：在 Python REPL 里把接收侧从 3 张网卡收窄到 2 张：

   ```python
   # 示例代码：REPL 片段（需要已安装本仓库，纯 CPU 即可）
   from checkpoint_engine.ps import _assign_receiver_ranks

   buckets = [(i % 8, f"bucket{i}") for i in range(13)]
   two_local = {f"rdma{i}": {i} for i in range(2)}   # 只有 2 个接收网卡组
   remote = {f"rdma{i}": {i, i + 1} for i in range(0, 8, 2)}
   for item in _assign_receiver_ranks(buckets, two_local, remote):
       print(item)
   ```

3. **需要观察的现象**：结果序列中 receiver 在 0、1 之间严格交替；receiver 2 永不出现。
4. **预期结果**（手工推演：`num_receivers = min(2, 4) = 2`，展平序列与 4.3.3 相同，每轮两个 receiver 领两张不同源网卡）：`(0,…,b0), (1,…,b2), (0,…,b4), (1,…,b6), (0,…,b1), (1,…,b3), (0,…,b5), (1,…,b7), (0,…,b8), (1,…,b10), (0,…,b12), (1,…,b9), (0,…,b11)`。待本地验证。
5. 若输出与预期不符，回到 4.3.2 的伪代码逐步核对——这正是练习的价值所在。

#### 4.3.5 小练习与答案

**练习 1**：如果把列主序展平换成行主序（先做满 rdma0 的 4 个桶，再做 rdma2……），第 6 组用例会分配成什么样？

**答案**：展平变为 `b0 b1 b8 b9 | b2 b3 b10 b11 | b4 b5 b12 | b6 b7`。第 0 轮：receiver 0 领 b0（rdma0），receiver 1 的候选 b1 也是 rdma0 → `break`，一轮只配出 1 个桶；第 1、2 轮同理（b8、b9 都在 rdma0 上）。直到 rdma0 的桶耗尽，receiver 1、2 才有机会领 rdma2 的桶。最终 receiver 0 几乎串行拉完大部分桶、接收侧另外两张网卡长期闲置——列主序的意义正是避免这种「队头被单网卡垄断」。

**练习 2**：被 `break` 拦下的桶会丢失吗？算法一定终止吗？

**答案**：不会丢失。`break` 时 `assigned_cnt` 不前进，该桶仍是展平序列的队头，下一轮 `occupied_devices` 清空后由第一个 receiver 领走（第 4 组用例的 b9 正是如此）。终止性：`while` 的每次迭代中，第一个 receiver 面对空 `occupied_devices` 必然成功分配一个桶，所以迭代次数 ≤ 桶数，必然终止。

**练习 3**：8 个 owner 全在一台单网卡机上、接收侧有 2 张网卡，算法退化成什么？这是缺陷吗？

**答案**：分组矩阵只剩 1 行，`num_receivers = min(2, 1) = 1`，`receiver_list` 只有 1 个 rank——每轮只配一个桶，全部桶由单个 receiver 串行拉取。这不是算法缺陷而是物理上限：源侧只有一张网卡，并行度 \( P = \min(1, 2) = 1 \)，无论怎么分配都不可能超过单网卡带宽。

### 4.4 分配结果的消费：拉取清单与广播编排

#### 4.4.1 概念说明

`(receiver, owner, bucket)` 三元组不是终点，它要被 `_update_per_bucket` 翻译成两类动作：

- **拉取清单**：`receiver == self._rank` 的桶构成「我的拉取清单」——这些桶要由本 rank 通过 mooncake 从 owner 的锁页内存单边读进 GPU。
- **广播编排**：所有桶按 receiver 分桶后，循环的第 i 轮里，每个「还有第 i 个桶」的 receiver 把自己的桶广播给整个目标进程组（`dist.broadcast(buffer_b, src=receiver_rank)`，u3-l4 讲过的「倒置广播源」——数据在谁手里谁当源）。

于是每个目标 rank 的每轮迭代都是三选一：当 receiver（拉取 + 当广播源）、当普通成员（纯接收），或者无事可做（自己的桶已发完）。拉取被放在广播循环每轮的**开头**写进 `h2d_buffer`（预取），与上一轮的广播重叠——P2P 模式完整继承了 u3-l4 的流水线，只是把「H2D」换成了「RDMA 远端读」。

#### 4.4.2 核心流程

```text
每个参与 rank（rank ∈ ranks）:
  分配结果 → receiver_rank_buckets   = [(owner, bucket) for (r, owner, bucket) if r == 自己]
  分配结果 → buckets_by_receiver_rank = {receiver: [bucket, ...]}，max_len = 最长清单长度
  for i in range(max_len):
      若 i < len(自己的拉取清单): RDMA 读 owner 的锁页内存 → h2d_buffer   # 预取，与广播重叠
      for (receiver, 桶列表) in buckets_by_receiver_rank:
          若 i ≥ len(桶列表): continue
          若 receiver == 自己: h2d_buffer → 广播半区 buffer_b（gidx%2 双缓冲）
          dist.broadcast(buffer_b, src=receiver, group=目标组)            # 数据持有者当源
          等待 worker ACK（ZMQ）→ 发张量清单
```

#### 4.4.3 源码精读

从分配结果中筛出自己的拉取清单：

[checkpoint_engine/ps.py:818-822](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L818-L822)

按 receiver 重新分桶并求最大轮数（决定广播循环长度）：

[checkpoint_engine/ps.py:835-840](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L835-L840)

每轮开头的预取（`owner_rank` 非 None 即走 P2P 远端读分支）：

[checkpoint_engine/ps.py:855-862](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L855-L862)

「拉取」的最终落点：`_copy_to_buffer` 把桶的每段 `BucketRange` 翻译成「远端绝对地址 → 本地 buffer 偏移」，聚合为一次批量同步读（u5-l5 的 `batch_transfer_sync_read`）；`_get_addr_ptrs` 从全局 metas 里查 owner 的引擎地址与各块 buffer 的 `(ptr, size)`：

[checkpoint_engine/ps.py:700-713](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L700-L713)

[checkpoint_engine/ps.py:716-719](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L716-L719)

轮内的广播与双缓冲半区选择（`gidx % 2`，u3-l4/u3-l6 已讲）：

[checkpoint_engine/ps.py:863-890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L863-L890)

顺带一提：P2P 模式下每个参与 rank 会先把自己的 GPU buffer（或 h2d_buffer）以 `__ipc_buffer__` 为名注册进本机的 p2p store（L827-L832），因为单边读的**目的地址**同样必须是 transfer engine 注册过的内存——u5-l5 说「两侧都要注册」在此处兑现。

#### 4.4.4 代码实践

1. **实践目标**：源码阅读型实践——为一个具体 rank 画出「轮次 × 行为」表，检验对消费方式的理解。
2. **操作步骤**：沿用 4.3.3 第 6 组用例的分配结果。receiver 2（rank 2）分到的桶按领取顺序是 b4、b3、b8、b9，共 4 个；`max_len = 5`（rank 0 有 5 个）。画一张 5 行的表，每行三列：「本轮 rank 2 预取谁的第几个桶」「本轮有哪些 receiver 当广播源、src 分别是几」「rank 2 是否当源」。
3. **需要观察的现象**：前 4 轮 rank 2 每轮都先拉取再当源；第 5 轮（i=4）它既不预取也不当源，只剩 rank 0 当源。
4. **预期结果**（手工推演，依据 L855-L862 与 L863-L890 的循环结构）：

   | 轮 i | rank 2 预取 | 广播源（按 buckets_by_receiver_rank 迭代序） | rank 2 角色 |
   | --- | --- | --- | --- |
   | 0 | b4（owner 4） | 0、1、2 各广播一个桶 | 拉取 + 当源 |
   | 1 | b3（owner 3） | 0、1、2 | 拉取 + 当源 |
   | 2 | b8（owner 0） | 0、1、2 | 拉取 + 当源 |
   | 3 | b9（owner 1） | 0、1、2 | 拉取 + 当源 |
   | 4 | 无（清单耗尽） | 仅 0 | 纯接收 |

5. 该表无需运行即可从源码推出；如需实证，可在真实 GPU 集群上跑 `examples/update.py` 的 p2p 分支并观察 rank 0 日志中每桶的 `receiver_rank` 字段（L871-L875 的日志行）——GPU 环境缺失时标注「待本地验证」即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 rank 2 的拉取（预取）发生在广播调用之前，而不是等轮到自己当源时才拉？

**答案**：为了让 RDMA 拉取离开广播的关键路径。第 i 轮开头就把自己的第 i 个桶读进 `h2d_buffer`，此时线路上正在广播的是上一拍的内容；等轮到它当源时只需做一次 GPU 内拷贝（`h2d_buffer` → 广播半区）。拉取与广播在时间上重叠，总时长由较慢的一方决定，而不是两者相加——这与 u3-l4 双缓冲的重叠思想一脉相承。

**练习 2**：`dist.broadcast` 的 `src` 是 `receiver_rank` 而不是 0，为什么？

**答案**：数据在 receiver 手里（它刚拉完这个桶）。若固定 src=0，就得先把桶从 receiver 再传到 rank 0，多一跳全量搬运。让数据持有者直接当广播源，每个桶零额外转发；代价是 src 逐桶变化，接收端靠集合通信库内部的 rank 约定保持一致。

**练习 3**：`ranks` 之外的 rank（need_update 为 False）会执行这段循环吗？

**答案**：不会。L793-L800 里 `need_update = self._rank in ranks`，非目标 rank 在进入切桶与分配之前就 `return` 了（此前先做一次 `dist.barrier` 防止后续设备 OOM）。因此广播组 `ranks_group = new_group(ranks)` 里只有目标 rank，`(receiver, owner, bucket)` 的 receiver 也全部取自目标组——分配算法过滤 local_topo（4.2）正是为了让 receiver_list 与这个组严格相容。

## 5. 综合实践

**任务：写一个 40 行的「逐轮配对可视化器」，与真实实现对账，再观察两种极端拓扑。**

这个实践把本讲四个模块串成一条线：构造拓扑（4.1）→ 复刻分岔语义（4.2）→ 逐轮复现贪心（4.3）→ 解释每轮里谁在拉谁（4.4）。

**第一步：跑基线。** 确认环境可用：`pytest tests/test_assign_receiver_ranks.py -v`（纯 CPU，无 gpu marker）应 6 组全过。

**第二步：写可视化脚本**（示例代码，建议存为仓库外的 `round_trace.py`，不要修改仓库）：

```python
# 示例代码：逐轮复刻 _assign_receiver_ranks，并按轮打印配对
from collections import defaultdict

from checkpoint_engine.ps import _assign_receiver_ranks


def trace(buckets, local_topo, remote_topo):
    rank_to_dev = {r: d for d, rs in remote_topo.items() for r in rs}
    by_dev = defaultdict(list)
    for owner, bucket in buckets:
        by_dev[rank_to_dev[owner]].append((owner, bucket))
    matrix = list(by_dev.values())
    num_receivers = min(len(local_topo), len(by_dev))
    receiver_list = [min(rs) for rs in list(local_topo.values())[:num_receivers]]
    max_cols = max(len(row) for row in matrix) if matrix else 0
    flat = [matrix[row][col]
            for col in range(max_cols)
            for row in range(len(matrix)) if col < len(matrix[row])]
    print(f"网卡分组(行): {list(by_dev)}")
    print(f"receiver_list: {receiver_list}")
    print(f"列优先展平:   {[b for _, b in flat]}")

    assigned, round_no, traced = 0, 0, []
    while assigned < len(flat):
        occupied, pairs = set(), []
        for receiver in receiver_list:
            if assigned >= len(flat):
                break
            owner, bucket = flat[assigned]
            dev = rank_to_dev[owner]
            if dev in occupied:
                print(f"  第 {round_no} 轮: receiver {receiver} 撞上已占用网卡 {dev}，本轮提前结束")
                break
            pairs.append((receiver, owner, bucket))
            occupied.add(dev)
            assigned += 1
        print(f"  第 {round_no} 轮: {[(r, b) for r, _, b in pairs]}  占用网卡 {sorted(occupied)}")
        traced.extend(pairs)
        round_no += 1
    return traced


# 复现 tests/test_assign_receiver_ranks.py 的第 6 组用例
buckets = [(i % 8, f"bucket{i}") for i in range(13)]
local_topo = {f"rdma{i}": {i} for i in range(3)}
remote_topo = {f"rdma{i}": {i, i + 1} for i in range(0, 8, 2)}

traced = {(r, o, b) for r, o, b in trace(buckets, local_topo, remote_topo)}
real = set(_assign_receiver_ranks(buckets, local_topo, remote_topo))
assert traced == real, traced ^ real
print("对账通过：逐轮复刻与 _assign_receiver_ranks 输出完全一致")
```

**第三步：观察输出。** 预期输出（手工推演，待本地验证）：

```text
网卡分组(行): ['rdma0', 'rdma2', 'rdma4', 'rdma6']
receiver_list: [0, 1, 2]
列优先展平:   ['bucket0', 'bucket2', 'bucket4', 'bucket6', 'bucket1', 'bucket3',
               'bucket5', 'bucket7', 'bucket8', 'bucket10', 'bucket12', 'bucket9', 'bucket11']
  第 0 轮: [(0, 'bucket0'), (1, 'bucket2'), (2, 'bucket4')]  占用网卡 ['rdma0', 'rdma2', 'rdma4']
  第 1 轮: [(0, 'bucket6'), (1, 'bucket1'), (2, 'bucket3')]  占用网卡 ['rdma0', 'rdma2', 'rdma6']
  第 2 轮: [(0, 'bucket5'), (1, 'bucket7'), (2, 'bucket8')]  占用网卡 ['rdma0', 'rdma4', 'rdma6']
  第 3 轮: [(0, 'bucket10'), (1, 'bucket12'), (2, 'bucket9')]  占用网卡 ['rdma0', 'rdma2', 'rdma4']
  第 4 轮: [(0, 'bucket11')]  占用网卡 ['rdma2']
对账通过：逐轮复刻与 _assign_receiver_ranks 输出完全一致
```

对照检查两点：每轮「占用网卡」的元素个数 = 该轮配出的桶数（去重生效）；13 个桶 5 轮完成，\( \lceil 13/3 \rceil = 5 \)，轮数贴近下界。

**第四步：两个改造实验。**

1. 把 `local_topo` 换成 `{f"rdma{i}": {i} for i in range(4)}`（即测试第 4 组用例的接收侧）：预期在第 2 轮看到「撞上已占用网卡」的提示——receiver 3 的候选 b9 与 receiver 0 的 b8 同在 rdma0，`break` 触发，b9 顺延到第 3 轮。这验证 4.3.3 讲的 break 语义。
2. 把 `remote_topo` 换成 `{"rdma0": set(range(8))}`（全部 owner 挤一张网卡）：预期矩阵只剩 1 行、`receiver_list` 只剩 1 个 rank、13 轮每轮 1 桶——并行度退化到 \( P=1 \)，印证练习 4.3-3 的物理上限。

**验收标准**：`pytest` 基线通过、脚本对账断言通过、两个改造实验的观察与手工推演一致。

## 6. 本讲小结

- P2P 更新的带宽瓶颈是**物理网卡**，`_assign_receiver_ranks` 用 55 行纯函数把桶分配组织成「按 owner 网卡分组 → 列优先展平 → 轮转配对」，使任意一轮的活跃拉取流两两占用不同的源网卡与目的网卡，并行度贴近 \( \min(两侧网卡组数) \)。
- 两张拓扑表分工明确：`local_topo`（接收侧，gather_metas 构建、按 ranks 过滤）选出 receiver（每网卡组取最小 rank），`remote_topo`（持有侧，默认同 local、join 模式由 load_metas 重建）决定分组；算法纯函数、零通信，所有 rank 独立算出相同结果。
- `occupied_devices` 是轮内源网卡去重：撞卡的桶 `break` 推迟（指针不前进），下一轮重新当队头候选；每轮至少配出一个桶保证终止。
- 列主序展平是算法的灵魂：保证相邻桶异源，避免行主序下「队头被单网卡垄断、receiver 串行拉取」的退化。
- 分配结果被 `_update_per_bucket` 消费为「我的拉取清单」（mooncake 单边读，目的地址也须注册）与「广播编排」（`dist.broadcast` 以 receiver 为源），拉取预取与广播重叠，完整继承三阶段流水线。
- 局限与方向：贪心按桶数、不按字节加权（桶大小因软上限而参差）；改造实验表明单网卡源侧会退化为串行——这是物理上限而非算法缺陷。

## 7. 下一步学习建议

本讲结束第五单元（分布式后端与 P2P 传输）的全部内容，接下来进入第六单元：

- **u6-l1（测试体系）**：本讲反复使用的 `tests/test_assign_receiver_ranks.py` 是「纯 CPU 验证分布式算法」的范本——用字符串当桶（泛型 `T`）、用字典当拓扑、用集合相等当断言。学完 u6-l1 你可以模仿这个范式为自己写的逻辑补测试。
- **u6-l3（metas 导出与 join）**：本讲 4.1 只讲了 remote_topo 在 join 模式下的**来源**；u6-l3 会完整走通「旧实例导出 metas → 新实例 load_metas 重建拓扑 → P2P 拉取」的全链路，与本讲的分配算法首尾相接。
- 建议同时通读 [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) 中 `--update-method p2p` 的分支，把本讲的算法放回编排层看一次它在真实更新里的位置。
