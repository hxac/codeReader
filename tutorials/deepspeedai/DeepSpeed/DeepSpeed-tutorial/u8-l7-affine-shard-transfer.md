# Affine 分片迁移：跨切分方式的参数直搬

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `affine.py` 如何用「仿射映射」（一组 strided 视图描述）统一刻画 ZeRO、AutoTP、AutoEP 任意一种并行方式产生的参数分片布局，以及为什么这种几何描述比「按参数名匹配正则类别」的旧路线更强。
2. 解读本周期新增的 [deepspeed/checkpoint/affine_geometry.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_geometry.py)（#9010）：七个纯几何原语如何从三个模块的两三份重复实现中收敛为一处，且行为完全不变。
3. 追踪 `plan_transfer` 如何把「源映射 × 目标映射」组合成一份按 segment 组织、可直接执行的搬运计划，并理解计划中每一步校验（分片按地址恰好铺满、holder 声明与实际覆盖一致、目标恰好被填满一次）。
4. 解读 `execute_transfer` 的执行协议：先对计划摘要（digest）达成共识，再按「先挂接收、后发发送」的轮次协议搬运，用 `StagingBudget` 约束每个方向每个 peer 的在途字节数。
5. 理解 `TransferEndpoints` 为什么把源 rank 与目标 rank 拆成两个独立映射（两个 rank 命名空间不是同一个 world_size）。
6. 解读 `rebuild`/`extract` 的「每个元素恰好写一次」校验体系：`_overlaps_itself` 拒绝自重叠视图、`_place`/`_require_placed` 拒绝分片内地址复用或漏写、`written` 掩码区分 `_UNWRITTEN`/`_SINGLE_HOLDER`/`_REPLICA` 以抓住两个 rank 争写同一块或整体覆盖不全。

本讲是 Universal Checkpoint（u8-l3）之外另一条跨并行度迁移参数的底层通道：universal checkpoint 解决「格式统一」，本讲的四件套解决「不重建完整逻辑张量的直搬」。一次同步的批量规划与可插拔重副本策略（`plan_transfers`）留到下一讲 u8-l9。

## 2. 前置知识

**仿射映射（affine map）与 strided 视图。** 一个 \( n \) 维张量的任意「块」都可以由三元组 `(offset, strides, shape)` 唯一刻画：块内第 \( i \) 个元素在底层存储中的地址是

\[
\text{addr}(i) = \text{offset} + \sum_{k=0}^{n-1} i_k \cdot \text{strides}_k
\]

这正是一个线性（仿射）映射，也正是 `torch.as_strided(tensor, size, stride, storage_offset)` 接受的参数。本讲全篇的「几何」都建立在这个三元组上——**没有乘法、没有逐元素变换，只有索引映射**，所以两个布局之间的转换可以化简为纯几何问题。

**逻辑张量与分片（logical tensor / shard）。** 一个参数在切分前的完整形状叫 `logical_shape`；每个 rank 持有的那一块叫 shard，有自己的 `shard_shape`。同一个 rank 的 shard 里可能装着完整张量中**不连续**的若干块（比如列切分时，每个 shard 行只含部分列）。

**行主序（row-major）。** PyTorch 的默认布局：最后一维 stride 为 1，其余每维 stride 等于后所有维大小的乘积。`(3, 4)` 张量的行主序 strides 是 `(4, 1)`。

**副本（replica）。** 数据并行或行并行会把同一块参数复制到多个 rank；此时一个元素被多个 rank 同时持有。迁移数据时从哪个副本读是一个**调度决策**，不是几何事实——这个区分会贯穿 planner 与下一讲的 replica policy。

**为什么需要「直搬」。** 朴素做法是：各 rank 把分片聚成完整张量（gather），再按目标布局重新切分（scatter）。这要求某个时刻某处能装下完整参数——对超大模型正是要避免的。本讲的执行器从头到尾不在任何地方形成完整逻辑张量。

**transport group（传输组）。** 执行搬运时实际参与通信的进程组（通常是 `deepspeed.comm` 管理的某个 process group）。组内 rank 是「传输坐标系」下的编号，与源/目标映射里的 rank 编号是两套东西。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `deepspeed/checkpoint/affine.py` | 定义 `AffinePiece`（一块元素在完整张量与分片两侧的描述）与 `ParamAffineMap`（一个参数如何摊在各 rank 上），外加五个布局建造者与 `rebuild`/`extract` 双向转换、全部覆盖/重叠校验 |
| `deepspeed/checkpoint/affine_geometry.py` | #9010 新增：七个对 piece/rank/tensor 一无所知的纯几何原语（`product`/`row_major_strides`/`coordinate`/`dot`/`strided_address`/`row_major_range_boxes`/`LogicalBox`） |
| `deepspeed/checkpoint/affine_transfer.py` | 规划器：把「源映射 + 目标映射」组合成 `TransferPlan`（一串 `TransferSegment` 直拷指令），带 `PlanBudget` 预算与逐层校验；下一讲的 `plan_transfers` 批量接口也在这里 |
| `deepspeed/checkpoint/affine_transfer_executor.py` | 执行器：digest 共识、轮次协议、staging 竞技场、本地拷贝、全部 preflight 拒止 |
| `tests/unit/checkpoint/test_affine_shard_map.py` | 用真实 AutoTP 切分函数反向探测布局、验证 rebuild/extract 往返与各类拒绝路径 |
| `tests/unit/checkpoint/test_affine_transfer_executor.py` | 两进程与单进程执行器测试：payload 与「从完整张量 extract」对照、digest 分歧、预算收敛、别名拒绝 |
| `docs/code-docs/source/affine-transfer.rst` | 官方契约文档：用法、约束、staging 的归属与未来演进 |

上游生产者（本讲只作背景）：`deepspeed/checkpoint/autoep_affine.py` 为 AutoEP 组合布局映射与 ZeRO 存储映射（见 [deepspeed/checkpoint/autoep_affine.py:12-14](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/autoep_affine.py#L12-L14)），`deepspeed/checkpoint/universal_checkpoint.py` 导入 `ParamAffineMap` 与 `SCALE_POWER_BY_STATE`。

## 4. 核心概念与源码讲解

### 4.1 affine 分片描述：AffinePiece 与 ParamAffineMap

#### 4.1.1 概念说明

模块开头的 docstring 说明了这套东西存在的理由：Universal Checkpoint 目前靠**参数名匹配正则类别**（词表、行并行、融合子参数……）来决定怎么合并一个参数，一个类别描述不了的布局就完全无法转换。affine 描述换成**几何语言**：每个 rank 持有的是完整张量的一组 strided 视图，视图本身就说明了怎么重组（[deepspeed/checkpoint/affine.py:5-19](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L5-L19)）。

一条边界线被明确画下：piece 可以携带**可逆的逐元素缩放**（`scale`），但绝不能携带**归约**（几个元素平均成一个）——后者会让完整张量从分片不可恢复。这条线保证了双向转换都可逆。

#### 4.1.2 核心流程

一块元素（piece）在两侧的描述：

```
完整逻辑张量                        rank 的 shard
  shape（两侧共享，同一排列）
  source_offset/strides ──映射──▶  dest_offset/strides
  （读哪里的坐标）                 （写到 shard 哪个地址）
  locations：持有它的 rank 集合（副本）
  scale：shard == full * scale
```

- `source_*` 寻址**完整张量**，`dest_*` 寻址**分片**；两侧 shape 相同、只有 offset 和 strides 不同。
- 显式描述 dest 侧，是因为一个 piece 可以落在 shard 中间任意位置——列切分时 piece 逐行交错，「依次追加」不够用。
- `locations` 是**集合**而非单一属主：piece 可能被复制。命名单一属主是调度决策，而这种描述不关心拓扑。
- **同质性（homogeneous）约束**：piece 覆盖的每个元素必须由同一组 rank 持有、带同一 scale，否则 `locations` 字段就是谎言。

一个参数的整体布局由 `ParamAffineMap` 承载：`logical_shape` + 每个 rank 的 `shard_shapes` + `pieces_by_rank`。

#### 4.1.3 源码精读

`AffinePiece` 的构造与两侧视图。注意 offsets/strides 直接就是 `torch.as_strided` 的参数，piece 可以不经任何解释步骤直接作用到张量上（[deepspeed/checkpoint/affine.py:43-99](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L43-L99)）：

```python
def source_view(self, full_param):
    """This piece's elements as a view of the full parameter, without copying."""
    return torch.as_strided(full_param, size=self.shape,
                            stride=self.source_strides, storage_offset=self.source_offset)

def dest_view(self, shard):
    """This piece's elements as a view of the shard that holds it."""
    return torch.as_strided(shard, size=self.shape, stride=self.dest_strides, storage_offset=self.dest_offset)
```

构造函数只拒绝一件事——零缩放不可逆（[deepspeed/checkpoint/affine.py:72-81](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L72-L81)）：

```python
if self.scale == 0.0:
    raise ValueError('A zero scale is not invertible, so the full tensor could not be rebuilt.')
```

`scale` 的语义与优化器状态的关系记录在模块级常量里：参数缩放 \( s \) 会让梯度缩放 \( 1/s \)，于是 Adam 一阶矩需要 -1 次幂、二阶矩 -2 次幂（[deepspeed/checkpoint/affine.py:36-40](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L36-L40)）：

```python
SCALE_POWER_BY_STATE = {'fp32': 1, 'exp_avg': -1, 'exp_avg_sq': -2}
```

五个布局建造者覆盖了实际并行方式产生的全部形态（均在 [deepspeed/checkpoint/affine.py:568-804](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L568-L804)）：

| 建造者 | 布局 | 典型来源 |
|---|---|---|
| `replicated_map(shape, tp_degree, scale)` | 每个 rank 持完整参数，单一 piece、全体 rank 命名 | 行并行的 bias（预除以 world size，`scale=1/N`） |
| `contiguous_split_map(shape, per_rank_sizes, partition_dim)` | 每个 rank 持某轴上一段连续块，可不等宽 | 行并行 / 列并行权重 |
| `sub_param_map(shape, sub_dim_sizes, shard_widths, partition_dim)` | 多个子参数拼接后各自切分，rank 的 shard 依次装各子参数的切片 | 融合 QKV |
| `segmented_map(shape, segments, partition_dim, tp_degree, split_widths)` | 沿一轴的若干段各自独立选择「切分」或「复制」，每段独立成 piece（同质性要求） | Q 行切分 + KV 段整体复制 |
| `block_gather_map(shape, block_ids_by_rank, block_size, partition_dim)` | 每个 rank 持一组等大小块的任意挑选，连续 id 合并成一个 piece | shared-QK 注意力的 value heads |

例如 `contiguous_split_map` 的核心循环：每 rank 一个 piece，source 侧偏移随切分起点推进，dest 侧偏移为 0（[deepspeed/checkpoint/affine.py:598-623](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L598-L623)）：

```python
pieces_by_rank[rank] = [
    AffinePiece(shape=shard_shape,
                source_offset=start * source_strides[partition_dim],
                source_strides=source_strides,
                dest_offset=0,
                dest_strides=_row_major_strides(shard_shape),
                locations=[rank])
]
```

更复杂的布局可以**复合**：`nest_affine_maps(outer, inner_by_outer_rank)` 把「参数在 TP 组上的布局映射」与「每个布局 shard 又如何在存储进程间摊开（如 ZeRO 分区）」组合成一张以存储进程为 rank 的总映射（[deepspeed/checkpoint/affine.py:807-863](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L807-L863)）。`autoep_affine.py` 正是用它把 AutoEP 布局与 ZeRO 存储叠在一起。

序列化方面，`to_dict`/`from_dict` 只存纯标量，映射可以不导入 torch 就被读回（[deepspeed/checkpoint/affine.py:471-495](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L471-L495)）；编码版本独立于 universal checkpoint 版本（`AFFINE_MAP_FORMAT_VERSION = 1`），读者拒绝早于自己的版本而不是误读未知字段。

#### 4.1.4 代码实践

1. **实践目标**：用三个建造者各建一张映射，肉眼检查 piece 的两侧几何。
2. **操作步骤**（纯 CPU，无需进程组）：

```python
# 示例代码
from deepspeed.checkpoint.affine import replicated_map, contiguous_split_map, sub_param_map

# 行切分：2 rank 各持 (8, 16) 中的 4 行
m = contiguous_split_map((8, 16), [4, 4], 0)
for rank, pieces in m.pieces_by_rank.items():
    for p in pieces:
        print(rank, p)

# 融合 QKV 列切分：3 个子参数各 4 列，每 rank 各拿 2 列
qkv = sub_param_map(shape=(4, 12), sub_dim_sizes=[4, 4, 4],
                    shard_widths=[[2, 2], [2, 2], [2, 2]], partition_dim=1)
print(qkv)  # ParamAffineMap(logical_shape=(4, 12), pieces_per_rank={0: 3, 1: 3})

# 复制：bias 预除以 world size
bias = replicated_map((4, ), tp_degree=2, scale=0.5)
print(bias.pieces_by_rank[0][0].locations, bias.pieces_by_rank[0][0].scale)
```

3. **需要观察的现象**：行切分每个 rank 只有 1 个 piece，`source_offset` 分别是 0 和 `4*16`；融合 QKV 每个 rank 有 3 个 piece（Q/K/V 各一），dest 偏移依次为 0、2、4；`replicated_map` 的 piece `locations={0,1}` 且 scale=0.5。
4. **预期结果**：piece 的 `shape` 两侧一致；`numel` 之和等于逻辑张量元素数。
5. 具体打印格式以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `locations` 必须是集合而不能是「属主 rank」？
**答案**：piece 可能被复制到多个 rank（数据并行、行并行 bias、`segmented_map` 的复制段）。属主选择是调度决策，取决于拓扑成本；描述层只记录事实（谁持有），把「从谁读」留给规划层（下一讲的 replica policy）。`plan_transfer` 默认取 `holders[0]`，策略可另行选择。

**练习 2**：`segmented_map` 为什么要求「复制段与切分段不能合成一个 piece」？
**答案**：piece 必须同质——覆盖的每个元素由同一组 rank 持有、同一 scale。复制段由全体 rank 持有，切分段只由一个 rank 持有，二者的 `locations` 不同，合并后 `locations` 字段对部分元素就是谎言，读者会漏掉或虚构副本。

**练习 3**：`scale=1/N` 的 bias 在 `rebuild` 时会发生什么？
**答案**：`rebuild` 里 `_scaled` 按 `scale ** scale_power`（参数用 1）除回去，恢复出未预除的完整 bias；优化器状态则因 `_reject_scaled_optimizer_state`（见 4.3）被直接拒绝。

### 4.2 affine_geometry：三个模块重复实现的几何原语收敛为一处（#9010）

#### 4.2.1 概念说明

在本周期之前，`product`、`row_major_strides` 在 `affine.py`、`affine_transfer.py`、`autoep_affine.py` 里各有一份私有实现（`_product` 等）；`coordinate` 藏在 `affine_transfer`，`dot`/`strided_address`/`row_major_range_boxes` 藏在 `affine`，`LogicalBox` 又在 `affine_transfer`。#9010（提交 84679e9d3）把它们收敛为一个新模块 `affine_geometry.py`，各处以**旧的私有别名**导入，调用点与行为零变化——这是一次刻意保持纯粹的搬迁。

为什么值得单独成模块？新模块 docstring 一句话说明：这些函数**对 piece、rank、张量一无所知**，只做形状/步长/偏移/轴对齐框的纯运算，因此一份实现可以服务每一个「描述或规划分片」的模块（[deepspeed/checkpoint/affine_geometry.py:3-6](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_geometry.py#L3-L6)）。提交信息还点明了后续动机：把 planner 中逐对 O(n²) 的校验换成基于这些原语的扫描算法时，评审只需关注算法变化本身。

#### 4.2.2 核心流程

七个原语分工如下：

| 原语 | 输入 → 输出 | 用途 |
|---|---|---|
| `product(shape)` | 形状 → 元素数 | 处处要算的 numel |
| `row_major_strides(shape)` | 形状 → 各轴步长 | 行主序基准步长，校验 piece 两侧是否「盒状」 |
| `coordinate(offset, strides, shape)` | 存储偏移 → 网格坐标或 None | 把 dest 偏移反解为 shard 内坐标（越界/非坐标返回 None） |
| `dot(index, strides)` | 坐标 → 偏移 | `coordinate` 的逆运算之一 |
| `strided_address(shape, offset, strides, index)` | 行主序序号 → 实际地址 | 把「块内第 i 个元素」翻译到任意 strided 布局的地址 |
| `row_major_range_boxes(shape, low, high, dense_rows)` | 行主序区间 → `(origin, extent)` 框列表 | 把一段连续元素区间切成尽量少的轴对齐框 |
| `LogicalBox` | origin + extent 的不可变框 | 框的交、包含、体积运算，是 planner 的通用货币 |

其中 `row_major_range_boxes` 是最精巧的一个：给定 `shape` 的行主序索引区间 \([low, high)\)，按递归把它拆成框——首行残段、整行中段、末行残段，每轴至多产生两个框；`dense_rows` 参数声明前多少轴**不允许跨位置合并**（因为存储在该轴上不连续），`dense_rows=0` 时中段合并成一个跨多行的大框。`nest_affine_maps` 用它把「ZeRO 分区可能从行中间开始/结束」的存储段切成首尾残行 + 中间整行。

#### 4.2.3 源码精读

`row_major_range_boxes` 的递归骨架（[deepspeed/checkpoint/affine_geometry.py:57-91](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_geometry.py#L57-L91)）：

```python
inner = product(shape[1:])
first, low_rest = divmod(low, inner)
last, high_rest = divmod(high, inner)
...
if low_rest:                       # 首行残段
    boxes.extend(within(first, low_rest, inner)); first += 1
if dense_rows > 0:                 # 不许合并：逐行展开
    for row in range(first, last):
        boxes.extend(within(row, 0, inner))
elif last > first:                 # 允许合并：一个跨 (last-first) 行的大框
    boxes.append(((first, ) + (0, ) * (len(shape) - 1), (last - first, ) + tuple(shape[1:])))
if high_rest:                      # 末行残段
    boxes.extend(within(last, 0, high_rest))
```

`LogicalBox` 是冻结 dataclass，提供 `volume`、`is_empty`、`upper`、`intersection`、`contains`（[deepspeed/checkpoint/affine_geometry.py:94-121](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_geometry.py#L94-L121)）。框不可变且可哈希，这让 planner 能把它当字典键（`_sources` 按框分组）。

消费方以旧私有名导入，例如 `affine.py`（[deepspeed/checkpoint/affine.py:23-24](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L23-L24)）：

```python
from .affine_geometry import (dot as _dot, product as _product, row_major_range_boxes as _row_major_range_boxes,
                              row_major_strides as _row_major_strides, strided_address as _strided_address)
```

`affine_transfer.py` 则导入 `LogicalBox` 与 `coordinate`（[deepspeed/checkpoint/affine_transfer.py:34-35](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L34-L35)），`autoep_affine.py` 导入 `product`/`row_major_strides`（[deepspeed/checkpoint/autoep_affine.py:12](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/autoep_affine.py#L12)）。

#### 4.2.4 代码实践

1. **实践目标**：手算 `row_major_range_boxes` 的输出并与函数对照，理解「至多每轴两框 + dense_rows 语义」。
2. **操作步骤**（示例代码）：

```python
from deepspeed.checkpoint.affine_geometry import row_major_range_boxes, product

# 案例 A：shape=(3,4)，区间 [5,11)，dense_rows=0
# 手算：inner=4；divmod(5,4)=(1,1)，divmod(11,4)=(2,3)
#   首行残段：第 1 行列 [1,4) -> ((1,1),(1,3))；first 变 2；last=first 故无中段
#   末行残段：第 2 行列 [0,3) -> ((2,0),(1,3))
print(row_major_range_boxes((3, 4), 5, 11, 0))
# 预期：[((1, 1), (1, 3)), ((2, 0), (1, 3))]，体积 3+3=6=11-5

# 案例 B：同形状，区间 [1,11)，dense_rows=0
# 首行残段 ((0,1),(1,3))；中段 last(2)>first(1) -> ((1,0),(1,4))；末行残段 ((2,0),(1,3))
print(row_major_range_boxes((3, 4), 1, 11, 0))
# 预期：[((0, 1), (1, 3)), ((1, 0), (1, 4)), ((2, 0), (1, 3))]，体积 3+4+3=10

# 案例 C：整个张量 [0,12)，对比 dense_rows=0 与 1
print(row_major_range_boxes((3, 4), 0, 12, 0))  # 一个框 ((0,0),(3,4))
print(row_major_range_boxes((3, 4), 0, 12, 1))  # 三个行框
```

3. **需要观察的现象**：案例 A/B 中每框体积之和恒等于 \( high - low \)；案例 C 中 `dense_rows` 的开关决定「一个整块」还是「逐行展开」。
4. **预期结果**：与上面注释中的手算一致；可用 `sum(product(e) for _, e in boxes) == high - low` 断言。
5. 三维形状（如 `(2, 5, 7)`）的行为建议额外打印对照（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`coordinate(offset, strides, shape)` 什么时候返回 `None`？
**答案**：当 `offset` 不是该形状/步长下的合法坐标时——某轴商越界（`position >= size`），或除尽各轴后仍有余数（`rest != 0`）。planner 的 `_origin` 正是用它把 piece 的存储偏移反解为网格坐标，解不出即抛 `InvalidMapError`。

**练习 2**：为什么 #9010 坚持「行为不变的纯搬迁」而不顺手改算法？
**答案**：提交说明写明：把 O(n²) 逐对校验换成基于这些原语的扫描是**算法变化**，分开评审才能让后续 PR 只审算法、不被搬迁噪音干扰。这也是大型重构的通用纪律：一次 PR 只做一件事。

**练习 3**：`row_major_range_boxes` 产出的框数上界是多少？
**答案**：每轴至多两个（首残段、末残段），中间整段在允许合并时是一个框、在 `dense_rows` 约束下逐位置展开；对二维且 `dense_rows=0`，典型上界是 3（首行残段 + 中段大框 + 末行残段）。框数由布局的碎片度而非元素数决定——这是「不按元素枚举」的关键前提。

### 4.3 rebuild/extract 与「每个元素恰好写一次」的校验体系

#### 4.3.1 概念说明

`rebuild`（分片 → 完整张量）与 `extract`（完整张量 → 分片）互为逆运算，是信任这张映射之前的最后一道关卡。设计哲学是：**一张能通过元数据校验的映射仍可能在数据面上撒谎**——两个 rank 声称持有同一块、或整体覆盖不全，元素计数都能对上。所以校验分两层：

- **便宜层（`validate`）**：只读 piece 元数据——shard 元素计数、dest 侧按地址不重叠不越界。从不逐元素枚举（有测试用 10⁹ 大小张量 + monkeypatch 保证这一点）。
- **昂贵层（`validate_coverage` / `rebuild` 内建）**：逐元素走地址，O(numel)，供测试与新映射验收。

#### 4.3.2 核心流程

`validate` 的三步（对每个 rank）：

1. piece numel 之和 == shard 元素数（否则「元素计数对不上」）；
2. 每个 piece 的 source/dest 视图**不得自重叠**（`_overlaps_itself`）——一个视图两次碰到同一地址，就有一个元素被静默覆盖；
3. dest 侧区间/框不越界、互不重叠（`_validate_destinations`）：稠密 piece 当区间排序后扫描，带 stride 的 piece 当 shard 网格中的框两两求交。

`rebuild` 在此之上加三道数据面防线：

```
written 掩码（uint8，逐元素）:  0=_UNWRITTEN  1=_SINGLE_HOLDER  2=_REPLICA
placed  掩码（bool，逐 shard 地址）: _place 先占位、_require_placed 收尾查漏

每写一块：
  _place:      若该地址已被占 -> ValueError("writes shard address twice")
  seen = written != _UNWRITTEN
  若 seen 且（本次或上次写入者非 _REPLICA）-> ValueError("single holder")
  若 seen 且两块数据不相等        -> ValueError("different data")   # 副本漂移
  复制数据、written 置为写入者标记
最后：
  placed 未全 True -> ValueError("unwritten")        # 分片内有地址从未被写
  written 有 _UNWRITTEN -> ValueError("uncovered")   # 完整张量有元素无人供
```

#### 4.3.3 源码精读

自重叠检测只做几次比较而不逐元素走（[deepspeed/checkpoint/affine.py:515-528](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L515-L528)）：按 stride 排序各轴，前面的轴累计到达范围 `reach` 后，后面每轴的 stride 必须跨过它才无碰撞。

```python
def _overlaps_itself(shape, strides):
    reach = 0
    for stride, size in sorted((stride, size) for size, stride in zip(shape, strides) if size > 1):
        if stride <= reach:
            return True
        reach += stride * (size - 1)
    return False
```

`_place`/`_require_placed` 用 shard 形状的布尔掩码精确落位任意 stride（包括跨行末尾回卷、不是 shard 网格中「框」的视图）（[deepspeed/checkpoint/affine.py:531-551](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L531-L551)）：

```python
def _place(placed, piece, rank):
    view = piece.dest_view(placed)
    if view.any():
        ...  # 计算冲突地址并 raise ValueError('... twice ...')
    view.fill_(True)

def _require_placed(placed, rank):
    if not placed.all():
        ...  # raise ValueError('... leave N shard addresses unwritten ...')
```

`rebuild` 的核心：written 掩码 + 副本一致性（[deepspeed/checkpoint/affine.py:390-421](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L390-L421)）。三个常量在 [deepspeed/checkpoint/affine.py:510-512](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L510-L512)：

```python
writer = _REPLICA if len(piece.locations) > 1 else _SINGLE_HOLDER
previous = piece.source_view(written)
seen = previous != _UNWRITTEN
if seen.any():
    if writer != _REPLICA or not (previous[seen] == _REPLICA).all():
        raise ValueError(... 'single holder' ...)   # 一方声明独占，另一方也写
    if not torch.equal(target[seen], source[seen]):
        raise ValueError(... 'different data' ...)  # 副本数据漂移
```

结尾的全量覆盖检查（[deepspeed/checkpoint/affine.py:424-428](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L424-L428)）：

```python
missing = (written == _UNWRITTEN).nonzero()
if missing.numel():
    raise ValueError(... f'leaves {missing.numel()} element(s) uncovered ...')
```

优化器状态走缩放 piece 被显式拒绝（[deepspeed/checkpoint/affine.py:431-449](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py#L431-L449)）：moments 本身可以按幂次正确变换，但 Adam 的 `lr`/`eps` 仍活在源坐标系里，不随之变换的恢复会逐步漂移且无任何报错——所以干脆 `NotImplementedError`。

测试侧的守护用例值得对照阅读：自重叠视图三类（source / zero-stride / destination）见 [tests/unit/checkpoint/test_affine_shard_map.py:664-698](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_shard_map.py#L664-L698)；漏覆盖与双属主见 [tests/unit/checkpoint/test_affine_shard_map.py:635-648](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_shard_map.py#L635-L648)；非框视图的地址碰撞只有全量检查能抓（`validate()` 接受、`validate_coverage()` 拒绝）见 [tests/unit/checkpoint/test_affine_shard_map.py:556-577](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_shard_map.py#L556-L577)。

#### 4.3.4 代码实践

1. **实践目标**：亲手构造自重叠与漏覆盖的坏映射，验证 `rebuild` 的两类 `ValueError`。
2. **操作步骤**（示例代码，改写自 test_affine_shard_map.py 的守护用例）：

```python
import torch
from deepspeed.checkpoint.affine import AffinePiece, ParamAffineMap

# 坏映射 1：dest strides (1,1) 的 (2,2) 视图反复访问同一地址（自重叠）
bad1 = ParamAffineMap((4, ), {0: (4, )},
                      {0: [AffinePiece((2, 2), 0, (2, 1), 0, (1, 1), [0])]})
try:
    bad1.rebuild({0: torch.arange(4.)})
except ValueError as e:
    print('caught 1:', e)          # 预期含 "reaches one destination address twice within itself"

# 坏映射 2：两个 rank 各持 4 元素、各写满自己的 shard，但逻辑张量有 9 个元素
bad2 = ParamAffineMap((9, ), {0: (4, ), 1: (4, )},
                      {0: [AffinePiece((4, ), 0, (1, ), 0, (1, ), [0])],
                       1: [AffinePiece((4, ), 4, (1, ), 0, (1, ), [1])]})
bad2.validate()                    # 元数据层通过！
try:
    bad2.rebuild({0: torch.zeros(4), 1: torch.zeros(4)})
except ValueError as e:
    print('caught 2:', e)          # 预期含 "uncovered"
```

3. **需要观察的现象**：`bad2.validate()` 不抛错（每个 rank 的元素计数与 dest 都合法），只有 `rebuild` 的 written 掩码能发现第 8 号元素无人持有——这正是「元数据校验抓不住数据面谎言」的实例。
4. **预期结果**：两次都抛 `ValueError`，错误信息分别匹配 `itself`/`twice` 与 `uncovered`（与 [tests/unit/checkpoint/test_affine_shard_map.py:691-698](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_shard_map.py#L691-L698)、[L635-648](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_shard_map.py#L635-L648) 的断言一致）。
5. 已对照测试确认错误文案关键词；本地完整运行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_place`/`_require_placed` 用掩码而不是像 `validate` 那样用框求交？
**答案**：`validate` 从元数据读 dest，无法安放「跨行末尾回卷」的视图（它不是 shard 网格中的框）；shard 形状的布尔掩码对任意 stride 都精确。测试 `_colliding_strided_map` 专门构造了两个非框 piece 共写地址 2、地址 3 无人写的例子，`validate()` 通过而 `validate_coverage()` 拒绝。

**练习 2**：副本一致性检查为什么要「同时检查先前写入者的标记」而不只看当前 piece？
**答案**：遍历顺序由字典决定，谁先被访问不确定。若 rank 0 声明独占、rank 1 声明副本，无论先访问谁都必须报错——只查一边就会漏掉另一种顺序。测试 `test_a_replica_overlapping_a_single_holder_is_refused_in_either_order` 参数化了两种顺序。

**练习 3**：`rebuild` 为什么比较副本的**全部**重叠元素而不是只比较与先前 piece 起点对齐的部分？
**答案**：两个 rank 可能把同一复制块切成不同的 piece 边界（rank 0 一个整块、rank 1 两个半块）。只比对齐部分会漏查 rank 1 的后半段，让它静默覆盖 rank 0 未检查的数据；逐元素 `torch.equal` 对任意切法都成立（见 `test_replicas_cut_at_different_boundaries_are_compared_in_full`）。

### 4.4 plan_transfer：从两张映射到一份 TransferPlan

#### 4.4.1 概念说明

`affine_transfer.py` 解决的问题：给定「字节现在按什么布局分布」（source map）与「要变成什么布局」（target map），生成**从源分片缓冲直拷到目标分片缓冲**的指令序列——任何 rank 在任何时刻都不需要寻址完整逻辑张量（[deepspeed/checkpoint/affine_transfer.py:5-26](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L5-L26)）。

模块 docstring 第一句就警告最容易犯的错——**方向**：piece 的 `source_*` 寻址完整逻辑张量，`dest_*` 才寻址分片。所以「读源分片的哪里」要用源 piece 的 `dest_*`。本模块发出的每个地址都指向分片；offset/strides 以元素计（`as_strided` 口径），换算成字节是搬运层的事。

另外两条边界：只规划 `scale == 1`（理由与 4.3 的拒绝一致）；从不做任何按元素的分配，因此 `validate_coverage`（O(numel)）从不被调用——覆盖性论证改在 piece 层完成。

#### 4.4.2 核心流程

```
plan_transfer(target_map, source_map)
  ├─ _prepare
  │   ├─ 两侧 logical_shape 必须相等
  │   ├─ _normalized: 每 side
  │   │   ├─ _frame_of: piece → _Frame（只接受轴对齐框 + 行主序步长；一维 shard 可整框平铺存储）
  │   │   ├─ _shards_tile: 每 rank 的 piece 按分片地址恰好铺满（相交为空 + 体积和相等）
  │   │   └─ _validate_holders: 声明的 locations 与实际覆盖恰好一致
  │   ├─ _sources: 源框按 (origin, extent) 排序、两两不相交 → (region, holders) 列表
  │   ├─ 源体积 == 逻辑体积；目标框经 _covers_union 盖住整个逻辑框
  │   └─ 逐目标 piece：与各源 region 求交 → 交集体积和 == 目标体积（_covers_exactly）
  ├─ 为每个交集选 holder（默认 holders[0]；可传 replica_selector）
  └─ _emit: (region, holder) → TransferSegment；_coalesce 沿单轴合并相邻段；预算终检
```

预算由 `PlanBudget` 承载（[deepspeed/checkpoint/affine_transfer.py:72-87](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L72-L87)）：`max_ndim=8`、`max_pieces_per_map=4096`、`max_pair_checks=2^18`、`max_segments=2^16`、`max_merge_passes=8`、`max_plan_bytes=2^24`、`max_merge_checks=2^18`。注释强调这些是**策略而非正确性**——超一倍的方案仍是正确方案，预算存在是为了让「piece 数本身就是 bug」的映射无法逼出无界枚举，且耗尽是调用方看得见的失败而非被悄悄截断的方案。

#### 4.4.3 源码精读

规划的原子是 `_Frame`：piece 规范化后的（逻辑框, 分片内框, 分片步长, 分片偏移）（[deepspeed/checkpoint/affine_transfer.py:101-115](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L101-L115)）。`_frame_of` 只接受轴对齐框——两侧 stride 必须是各自张量的行主序 stride（未跨越的轴不比较），并明确拒绝缩放 piece（[deepspeed/checkpoint/affine_transfer.py:230-288](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L230-L288)）：

```python
if piece.scale != 1.0:
    raise UnsupportedLayoutError(f'{where}: scale {piece.scale}, which would leave a resumed optimizer '
                                 'in the source coordinate')
...
flat_storage = len(shard_shape) == 1 and len(piece.shape) != 1
if flat_storage:
    # 一维 shard（如 ZeRO 存储）把多维框当作一段稠密地址运行来存放
    shard_strides = _row_major_strides(tuple(piece.shape))
```

异常体系有意三分（[deepspeed/checkpoint/affine_transfer.py:44-69](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L44-L69)）：`InvalidMapError`（映射自相矛盾——回退会掩盖损坏的检查点，不许绕）、`UnsupportedLayoutError`（合法但本规划器不表达的几何——调用方可以 rebuild 后重切，慢但不错）、下一讲的 `InvalidReplicaPolicyError`。

`_shards_tile` 论证「piece 按分片地址划分 shard」这一前提：仅凭逻辑框不相交**推不出**分片地址不撞——三个不相交的逻辑块可能两块落到相同分片地址而 numel 恰好加总相等（[deepspeed/checkpoint/affine_transfer.py:333-359](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L333-L359)）。`_validate_holders` 再验证声明的 `locations` 与逐框实际覆盖严格一致（[deepspeed/checkpoint/affine_transfer.py:362-386](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L362-L386))。

覆盖论证的核心一行（[deepspeed/checkpoint/affine_transfer.py:420-430](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L420-L430)）：

```python
def _covers_exactly(region, parts):
    """... Disjoint parts inside a region account for it exactly when their volumes sum to it. ..."""
    return sum(part.volume for part in parts) == region.volume
```

docstring 讲透了为什么「方案的总元素数 == 参数总元素数」证明不了任何事：**只有按目标逐块 tiling 才能抓住缝隙，而等大的缝隙与重叠在任何总量里互相抵消**。

最终产物 `TransferSegment` 是一条可直接执行的 strided 拷贝：两侧共享 `shape`，但两组 stride 一般不同——一侧稠密的块可能在另一侧跨步，把它当双侧稠密来读正是这个拆分要防的错误（[deepspeed/checkpoint/affine_transfer.py:172-200](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L172-L200)）：

```python
@dataclass(frozen=True)
class TransferSegment:
    source_rank: int
    source_offset: int
    source_strides: Tuple[int, ...]
    target_rank: int
    target_offset: int
    target_strides: Tuple[int, ...]
    shape: Tuple[int, ...]
```

`_coalesce` 按 `(source_rank, target_rank, strides, ndim)` 分组、组内按目标偏移排序、反复合并至不动——**确定性**且与生成顺序无关；注释明说不追求最少段数（那需要搜索，不该在这条路径上）（[deepspeed/checkpoint/affine_transfer.py:831-870](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L831-L870)）。

#### 4.4.4 代码实践

1. **实践目标**：单进程跑一次「行切 → 列切」的规划，对照「先 gather 再 re-scatter」的朴素做法验证逐元素一致，并读懂统计信息。
2. **操作步骤**（示例代码，仿照 [tests/unit/checkpoint/test_affine_transfer_executor.py:59-63](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L59-L63) 的 `_row_to_column`）：

```python
import torch
from deepspeed.checkpoint.affine import contiguous_split_map
from deepspeed.checkpoint.affine_transfer import PlanBudget, plan_transfer

full = torch.arange(16 * 16, dtype=torch.float32).reshape(16, 16)
source = contiguous_split_map((16, 16), [8, 8], 0)   # 2 rank 行切
target = contiguous_split_map((16, 16), [8, 8], 1)   # 2 rank 列切

plan = plan_transfer(target, source, PlanBudget())
print(plan.statistics)                                # 段数、合并前后段数、pair_checks...
for seg in plan.segments:
    print(seg)
# 朴素做法对照：目标分片 == target.extract(rebuild(sources), r)
```

3. **需要观察的现象**：每个 segment 的 `shape` 是某个 \( 8\times 8 \) 的交集体；`source_strides` 与 `target_strides` 通常**不相等**（源行分片内跨 16 步、目标列分片内跨 8 步）；`sum(seg.numel for seg in plan.segments) == 256`。
4. **预期结果**：4 个交集（2 目标 × 2 源 region），合并后段数 ≤ 4（是否可合并取决于 rank 配对）；总量恰好等于逻辑 numel 一次。
5. 具体段数与是否合并以本地打印为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`_frame_of` 为什么允许「一维 shard 存多维框」这个例外？
**答案**：ZeRO 把 TP shard 摊平成一维存储，一个多维框在其中就是一段稠密地址运行。此时 piece 的 dest strides 是**框自身**的行主序步长而非 shard 的（一维 shard 只有一个步长）。这是 `nest_affine_maps` 产出的存储形态。

**练习 2**：`_sources` 为什么要求源 region 两两「要么不相交、要么完全相等」，部分重叠时抛 `UnsupportedLayoutError` 而不是 `InvalidMapError`？
**答案**：完全相等的重叠是副本（同一块多个持有者各有一份）；部分重叠意味着一个元素有两个不相关的属主，没有任何回退能修复——但几何本身可能合法（比如两个副本被切成了不同边界），所以归为「本规划器不表达」，调用方可退回 rebuild 重切。而自相矛盾（如 holder 谎报）才是 `InvalidMapError`。

**练习 3**：`plan_transfer` 如何检查「两张映射描述的是同一个参数」？
**答案**：它不查——docstring 明说形状相等不是证据（历史上融合参数曾按字节计数错配）。它只检查两个几何能复合（logical_shape 相等 + 覆盖论证）；「是否同一参数」是调用方的问题。

### 4.5 execute_transfer：先收后发的轮次协议与 staging 预算

#### 4.5.1 概念说明

执行器把 `TransferPlan` 变成数据移动：从源缓冲打包 → 跨传输组 → 经 staging 缓冲 → 落进目标缓冲，全程不形成完整逻辑参数（[deepspeed/checkpoint/affine_transfer_executor.py:5-43](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L5-L43)）。模块 docstring 列出三件**刻意不在这里决定**的事：

- **Endpoints**：计划里的 location 由哪个进程持有，由调用方给。计划的两个 rank 命名空间不是同一个 world size——源 rank 0 没有理由恰好是目标 rank 0 那个进程，把两者混为一谈正是「把一个 rank 的分片读成另一个 rank 的」这种事故的来源。
- **Budget**：在途多少字节由调用方定，因为设备上还剩多少空间只有调用方知道；预算耗尽是错误，绝不是悄悄变大。
- **Dtype**：显式传入而非从缓冲探测，因为一个完全不持缓冲的进程也必须算出与持缓冲进程相同的 digest（见 4.6）。

协议一句话（docstring L24-29）：**先就日程摘要达成共识，再按轮次工作；每轮每个 rank 先挂好全部接收、然后才发发送**——两个互有数据给对方的 rank 不会都在等对方的发送。这个顺序就是全部的死锁论证，而它成立的前提正是 digest 保证了每个 rank 算出同样的轮次。

#### 4.5.2 核心流程

```
execute_transfers(requests, endpoints, dtype, budget, group)
  ├─ 预检（任何一步失败 -> rejected=1，不移动一个字节）
  │   ├─ 预算 ≥ 1 元素；endpoint 绑定合法；batch key 唯一非空
  │   ├─ _validate_buffers: 缓冲连续、dtype/设备一致、owned 位置有缓冲、reach 不越界
  │   ├─ _validate_aliases: 批内 source/target 存储窗口不相交
  │   └─ build_operations: plan → chunk → _Operation（解析到进程对），按 key 排序
  ├─ digest 共识（见 4.6）
  ├─ 按 (from==to==rank | to==rank | from==rank) 分流：本地拷贝 / 接收 / 发送
  ├─ _packed_rounds: 每 peer 把操作按 max_elements 打包成轮次
  ├─ 逐轮：
  │   ├─ 接收侧：单段且稠密 → 直接用目标视图（免 staging）；否则用接收竞技场
  │   ├─ 发送侧：打包进发送竞技场（或直接视图）
  │   └─ dist.batch_isend_irecv(全部 irecv+isend 一起提交)
  │   ├─ 每 32 轮或最后一轮：drain 挂起的句柄
  │   └─ 接收侧 unpack：竞技场 → 目标 strided 区域
  ├─ 本地拷贝（strided → strided copy_）
  └─ 返回 TransferStats{segments, chunks, rounds, local_copy_bytes, sent/received_bytes}
```

`StagingBudget` 只有一个字段：`bytes_per_peer = 8 << 20`（即 8 MiB），约束的是**每 peer 每方向**的在途字节数，不是设备总内存（[deepspeed/checkpoint/affine_transfer_executor.py:74-80](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L74-L80)）。

#### 4.5.3 源码精读

分块：把 strided 矩形按「打包后载荷 ≤ max_elements」切片，从最后一维向前贪心取整，两个地址空间（源/目标偏移）同步平移（[deepspeed/checkpoint/affine_transfer_executor.py:127-149](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L127-L149)）：

```python
def _chunks(segment, max_elements):
    if segment.numel <= max_elements:
        yield segment
        return
    tile = [1] * len(segment.shape)
    remaining = max_elements
    for axis in range(len(tile) - 1, -1, -1):
        tile[axis] = min(segment.shape[axis], remaining)
        remaining //= tile[axis]
```

`build_operations` 产出「每个 rank 都完全相同推导出的」日程并按 `(target_rank, target_offset, source_rank, source_offset, chunk)` 排序（[deepspeed/checkpoint/affine_transfer_executor.py:152-172](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L152-L172)）。确定性是共识的前提：同样的 (plan, endpoints, budget, element_size) 在每个 rank 上产生逐字节相同的操作表。

轮次主体：接收先行（[deepspeed/checkpoint/affine_transfer_executor.py:426-456](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L426-L456)）：

```python
for round_index in range(rounds):
    p2p = []
    for peer, peer_rounds in sorted(recv_rounds.items()):      # 先收集接收载荷
        ...
        p2p.append(('recv', payload, peer))
    for peer, peer_rounds in sorted(send_rounds.items()):      # 再打包发送
        ...
        p2p.append(('send', payload, peer))
    operations = [dist.P2POp(dist.irecv if direction == 'recv' else dist.isend,
                             buffer, ..., group=group) for direction, buffer, peer in p2p]
    for handle in dist.batch_isend_irecv(operations):
        ...
```

免 staging 的捷径：一轮只有一段且该段在缓冲中本来就稠密连续时，直接把缓冲视图当载荷（[deepspeed/checkpoint/affine_transfer_executor.py:288-301](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L288-L301)）：

```python
def _direct_payload(entries, requests, dtype, source):
    if len(entries) != 1:
        return None
    ...
    # A view can replace staging only when the transport sees the same packed element order.
    return region.view(-1) if region.is_contiguous() else None
```

落位用 `as_strided` 且**保留缓冲自身的 storage_offset**——计划的偏移从 shard 第一个元素数起，`as_strided` 从 storage 开头数起，显式相加才对；另一个选择（把缓冲变连续）要为修算术拷贝整个 shard（[deepspeed/checkpoint/affine_transfer_executor.py:515-522](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L515-L522)）：

```python
def _region(flat, offset, shape, strides):
    return torch.as_strided(flat, size=shape, stride=strides, storage_offset=flat.storage_offset() + offset)
```

测试对轮次与预算的关系有直接断言：预算 1024 → 32 → 16 字节时 `stats.rounds` 严格递增（[tests/unit/checkpoint/test_affine_transfer_executor.py:153-158](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L153-L158)）。

#### 4.5.4 代码实践

1. **实践目标**：单进程完成一次「行切 → 列切」直搬（全部走本地拷贝路径），再调小 `StagingBudget` 观察 `chunks` 变化；（可选）双进程 torchrun 版本观察 `rounds` 变化。
2. **操作步骤**（示例代码。单进程版不需要初始化分布式——`execute_transfers` 在未初始化时按 rank=0/world=1/CPU 处理，见 [deepspeed/checkpoint/affine_transfer_executor.py:318-328](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L318-L328)）：

```python
# single_transfer.py —— 单进程：两个源 rank、两个目标 rank 都在本进程
import torch
from deepspeed.checkpoint.affine import contiguous_split_map
from deepspeed.checkpoint.affine_transfer import PlanBudget, plan_transfer
from deepspeed.checkpoint.affine_transfer_executor import (StagingBudget, TransferEndpoints,
                                                           execute_transfer)

full = torch.arange(16 * 16, dtype=torch.float32).reshape(16, 16)
source = contiguous_split_map((16, 16), [8, 8], 0)
target = contiguous_split_map((16, 16), [8, 8], 1)
sources = {r: source.extract(full, r).contiguous() for r in source.shard_shapes}
targets = {r: torch.full(target.shard_shapes[r], float('nan')) for r in target.shard_shapes}

plan = plan_transfer(target, source, PlanBudget())
# 两个命名空间的所有 location 都映射到进程 0
endpoints = TransferEndpoints(source={0: 0, 1: 0}, target={0: 0, 1: 0})

for budget_bytes in (8 << 20, 256, 128):
    for r in targets:                       # 重置为“未写”标记
        targets[r].fill_(float('nan'))
    stats = execute_transfer(plan, sources, targets, endpoints, torch.float32,
                             StagingBudget(bytes_per_peer=budget_bytes))
    for r in targets:                       # 与朴素做法（extract 自完整张量）逐元素对照
        assert torch.equal(targets[r], target.extract(full, r)), r
    print(f'budget={budget_bytes}B chunks={stats.chunks} rounds={stats.rounds} '
          f'local_copy_bytes={stats.local_copy_bytes}')
```

双进程版本（需 GPU 或 CPU 多进程；仿照 [tests/unit/checkpoint/test_affine_transfer_executor.py:66-90](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L66-L90)）：`endpoints = TransferEndpoints({0: 0, 1: 1}, {0: 0, 1: 1})`，每个 rank 只构造自己 `endpoints` 命中的缓冲（源用 `source.extract(full, r)`、目标填 NaN），`torchrun --nproc_per_node=2 single_transfer.py` 启动；读者/写者完全不重叠的变体见 `TestB2DisjointEndpoints`（[tests/unit/checkpoint/test_affine_transfer_executor.py:230-262](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L230-L262)），此时 `stats.local_copy_bytes == 0` 且收发字节数各等于源/目标分片总字节。

3. **需要观察的现象**：单进程下所有操作都是本地拷贝（`local_copy_bytes == 256*4`），`rounds` 恒为 0，但 `chunks` 随预算变小而增多（段被切成更小的块）；双进程下 `rounds` 随预算变小而严格递增。
4. **预期结果**：三种预算下断言全部通过——目标分片与 `target.extract(full, r)` 完全相等，即「直搬」与「先 gather 完整张量再 re-scatter」逐元素一致。
5. 单进程 chunk 数可推算（每段 64 元素：默认预算 1 段 1 块；128B=32 元素时 1 段 2 块），双进程轮次数待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么「先挂接收、后发发送」能防死锁？
**答案**：若两个 rank 都先发后收且互有数据给对方，双方都可能阻塞在发送上等对方接收。先把自己这轮的接收全部挂起（异步 irecv 已登记缓冲），发送才有机会被对端的接收匹配。该论证成立的前提是所有 rank 的轮次划分一致——由 digest 保证。

**练习 2**：`_direct_payload` 什么条件下可以免掉 staging 竞技场？
**答案**：该轮对该 peer 只有一个操作，且它在对应侧缓冲中的区域本来就稠密连续（`region.is_contiguous()`）。此时传输看到的元素序与打包序一致，视图即可充当载荷。跨步或多段的一轮必须经竞技场打包成一条连续消息——这是 torch p2p「每消息一个连续张量」的限制（docstring：staging 属于当前传输层而非 affine transfer 本身，未来零拷贝传输改用 scatter/gather 列表）。

**练习 3**：`_validate_aliases` 拒绝什么？为什么必要？
**答案**：同一批 `TransferRequest` 内，任何 source 缓冲的存储窗口不得与任何 target 缓冲重叠，target 之间也不得互相重叠。没有它，一个参数的写入可能改写另一个参数还没读的源数据，结果依赖执行顺序。注意「同一存储的两个不相交窗口」是合法的（`test_disjoint_windows_of_one_storage_are_valid`）。

### 4.6 计划摘要共识与错误拒止：digest、preflight 与两个 rank 命名空间

#### 4.6.1 概念说明

分布式执行最大的风险不是单 rank 算错，而是**各 rank 算得不一致**：一个 rank 的日程说本轮 3 条消息，另一个说 5 条，轻则挂起、重则错位写入。执行器的解法分两步：

1. 每个 rank 从（计划几何、endpoint 绑定、预算、元素大小）算出一个 `plan_digest`——刻意**不含缓冲内容**（内容本来就该不同），且 dtype 是参数而非探测，这样不持任何缓冲的进程也能算出同样的值。
2. 动任何数据之前，全体 `all_gather` 各自的 `(digest, rejected)` 二元组：任何 rank 拒绝 → 全体失败；digest 不一致 → 全体失败。**预检失败时目标缓冲一个字节都不动**。

#### 4.6.2 核心流程

```
digest = crc32( repr(sorted({
    schema: 3, segments: [...], logical_shape,
    source_endpoints, target_endpoints, bytes_per_peer, element_size })) )
```

批处理时再套一层指纹：`(3, dtype, bytes_per_peer, 两个 endpoint 映射, [(key, plan_digest), ...])`——参数顺序不同也会改变指纹（测试 `test_batch_parameter_order_must_match` 专门验证 rank 间顺序相反时被拒）。

#### 4.6.3 源码精读

`plan_digest` 的载荷构成（[deepspeed/checkpoint/affine_transfer_executor.py:175-193](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L175-L193)）：

```python
framed = {
    'schema': 3,
    'segments': payload,
    'logical_shape': plan.logical_shape,
    'source_endpoints': dict(sorted(endpoints.source.items())),
    'target_endpoints': dict(sorted(endpoints.target.items())),
    'bytes_per_peer': budget.bytes_per_peer,
    'element_size': element_size,
}
return zlib.crc32(repr(sorted(framed.items(), key=lambda item: item[0])).encode())
```

共识与集体拒止（[deepspeed/checkpoint/affine_transfer_executor.py:407-420](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L407-L420)）：

```python
if world_size > 1:
    local = torch.tensor([digest, rejected], dtype=torch.int64, device=device)
    seen = torch.empty((world_size, 2), dtype=torch.int64, device=device)
    dist.all_gather(list(seen.unbind(0)), local, group=group)
    decisions = seen.cpu().tolist()
    if any(pair[1] for pair in decisions):
        raise AffineTransferExecutionError('a rank rejected the transfer batch before it began; no data moved')
    if len({pair[0] for pair in decisions}) != 1:
        raise AffineTransferExecutionError('ranks disagree about the transfer schedule (batch schedule)')
```

预检的缓冲验证：owned 位置必须有缓冲、连续、dtype/设备一致、每个 segment 的 reach 不越界（[deepspeed/checkpoint/affine_transfer_executor.py:213-242](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L213-L242)）；非法元数据（负偏移、零步长、错 dtype、子元素预算、无主缓冲……）被参数化测试逐一钉死（[tests/unit/checkpoint/test_affine_transfer_executor.py:329-397](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L329-L397)），并断言**所有目标缓冲保持未写**。

两个命名空间的契约写在 `TransferEndpoints` 的 docstring 里（[deepspeed/checkpoint/affine_transfer_executor.py:66-69](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L66-L69)）：`source` 覆盖源 rank、`target` 覆盖目标 rank，两张独立映射。官方文档给出最小用法（[docs/code-docs/source/affine-transfer.rst:17-24](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst#L17-L24)）：

```python
plan = plan_transfer(target_map, source_map)
execute_transfer(plan, local_sources, local_targets,
                 TransferEndpoints(source_bindings, target_bindings),
                 dtype, StagingBudget(bytes_per_peer=8 << 20))
```

#### 4.6.4 代码实践

1. **实践目标**：复现「rank 间预算不一致 → digest 不一致 → 全体拒绝」。
2. **操作步骤**：直接运行现成测试（CPU 或 GPU 双进程均可）：

```bash
pytest tests/unit/checkpoint/test_affine_transfer_executor.py \
  -k "ranks_disagreeing" -x -q
```

对应源码 [tests/unit/checkpoint/test_affine_transfer_executor.py:109-133](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py#L109-L133)：rank 0 用 `4 << 20`、rank 1 用 `1 << 20` 的预算。测试 docstring 点明要害：**单个 rank 拒绝会搁浅它的对端，所以分歧必须变成集体失败**——断言的是所有 rank 都抛错而不是「某个 rank 抛错」。

3. **需要观察的现象**：错误信息匹配 `'transfer schedule'`；没有任何目标缓冲被写入。
4. **预期结果**：双进程均抛 `AffineTransferExecutionError`。
5. 运行该测试通常需要 DeepSpeed 的多进程测试基建（`DistributedTest` 由 `unit.common` 提供）；本地执行方式待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `plan_digest` 不把缓冲内容纳入哈希？
**答案**：digest 要回答的唯一问题是「各 rank 的日程是否一致」。缓冲内容本来就该不同（每个 rank 只持自己的分片），把它纳入只会让所有 rank 永远不一致。这也是 dtype 作为参数传入的原因——不持缓冲的进程也要能算出同一摘要。

**练习 2**：`schema: 3` 与批处理指纹开头的 `3` 起什么作用？
**答案**：给摘要结构本身标版本。未来 wire contract 变化（docstring 预告了零拷贝传输落地时「每 peer 每轮一条打包消息」的契约会变）时递增它，让新旧结构的摘要必然不同，避免跨版本误判一致。

**练习 3**：预检拒绝为什么能保证「目标缓冲一字节不动」？
**答案**：所有验证（预算、endpoint、缓冲、别名、digest 共识）都发生在任何 `batch_isend_irecv`/本地 `copy_` 之前；任何一处失败设置 `rejected=1` 并跳过轮次循环，且 all_gather 让全体在通信开始前一致退出。测试在断言异常后逐一检查目标缓冲仍是初始哨兵值。注意契约的另一半：**载荷开始之后的传输失败可能留下部分写入的目标**（docstring 与 [docs/code-docs/source/affine-transfer.rst:30-31](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst#L30-L31) 明示）。

## 5. 综合实践

**任务**：写一个可重复运行的对照脚本 `affine_direct_vs_naive.py`，对同一个参数完成「行切 + 融合子参数」布局到「列切」布局的迁移，验证 affine 直搬与朴素 gather/re-scatter 逐元素一致，并观察预算对分块的影响。

```python
# 示例代码（单进程版，CPU 即可运行；双进程变体见 4.5.4）
import torch
from deepspeed.checkpoint.affine import sub_param_map, contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import PlanBudget, plan_transfer
from deepspeed.checkpoint.affine_transfer_executor import (StagingBudget, TransferEndpoints,
                                                           execute_transfer)

torch.manual_seed(0)
rows, cols = 8, 12
full = torch.randn(rows, cols)

# 源布局：Q/K/V 三个子参数沿列拼接，各 4 列，每 rank 拿每段的一半（2 列）
source = sub_param_map((rows, cols), sub_dim_sizes=[4, 4, 4],
                       shard_widths=[[2, 2], [2, 2], [2, 2]], partition_dim=1)
# 目标布局：沿行 4/4 切分
target = contiguous_split_map((rows, cols), [4, 4], 0)

sources = {r: source.extract(full, r).contiguous() for r in source.shard_shapes}
targets = {r: torch.full(target.shard_shapes[r], float('nan')) for r in target.shard_shapes}

plan = plan_transfer(target, source, PlanBudget())
print('statistics:', plan.statistics)
assert sum(seg.numel for seg in plan.segments) == rows * cols

endpoints = TransferEndpoints(source={0: 0, 1: 0}, target={0: 0, 1: 0})
stats = execute_transfer(plan, sources, targets, endpoints, torch.float32,
                         StagingBudget(bytes_per_peer=16))   # 故意很小，逼出多 chunk
print('stats:', stats)

# 朴素对照：直搬结果 == 从完整张量 extract 的分片
for r in targets:
    assert torch.equal(targets[r], target.extract(full, r)), f'rank {r} mismatch'

# 坏布局守卫：漏覆盖必须被 rebuild 拒绝（对照 4.3）
from deepspeed.checkpoint.affine import AffinePiece, ParamAffineMap
bad = ParamAffineMap((rows * cols + 1, ), {0: (rows * cols, )},
                     {0: [AffinePiece((rows * cols, ), 0, (1, ), 0, (1, ), [0])]})
try:
    bad.rebuild({0: full.reshape(-1)})
    raise SystemExit('FAIL: uncovered map accepted')
except ValueError as e:
    print('uncovered refused:', str(e)[:60])
print('ALL CHECKS PASSED')
```

**验收标准**：

1. `plan.statistics` 中 `segments` 非零、段 numel 之和等于参数 numel；
2. 所有 `torch.equal` 断言通过——直搬与朴素 gather/re-scatter 逐元素一致；
3. `stats.chunks` 在 16 字节预算下明显大于段数（每段被切块）；
4. 漏覆盖映射被 `rebuild` 以 `ValueError`（含 `uncovered`）拒绝。

进阶（需两进程）：把 endpoints 改为 `{0:0, 1:1}` 双侧、每 rank 只建自己的缓冲，`torchrun --nproc_per_node=2` 运行，对比不同 `StagingBudget` 下 `stats.rounds` 的变化，并跑一遍 `pytest tests/unit/checkpoint/test_affine_transfer_executor.py -k "disagreeing or disjoint"`。具体轮次数待本地验证。

## 6. 本讲小结

- **描述层（affine.py）**：`AffinePiece` 用 `(shape, source_*, dest_*, locations, scale)` 把任意并行方式的分片化为两侧 strided 视图；可逆性边界（允许缩放、禁止归约）保证双向转换；五个建造者 + `nest_affine_maps` 覆盖复制/连续切分/融合子参数/混合段/块挑选/ZeRO 存储复合。
- **几何层（affine_geometry.py，#9010）**：七个对 piece/rank/tensor 一无所知的纯函数从三处重复实现收敛为一处，旧私有名导入、行为零变化，为后续把 O(n²) 逐对校验换成基于原语的扫描铺路。
- **校验体系（validate/rebuild/extract）**：元数据层便宜检查（自重叠、分片地址铺满）+ 数据面 `written`/`placed` 掩码抓「两个 rank 争写、副本漂移、整体覆盖不全」；`_UNWRITTEN`/`_SINGLE_HOLDER`/`_REPLICA` 三态让访问顺序不影响结论。
- **规划层（plan_transfer）**：两张映射规范化为框（`_Frame`），验证分片按地址铺满、holder 声明属实、目标恰好被填满一次（体积论证——总量对不上证明不了任何事），产出可合并（`_coalesce`）的 `TransferSegment` 序列；`PlanBudget` 是看得见的策略上限。
- **执行层（execute_transfer）**：先 digest 共识、再「先挂接收后发发送」的轮次协议，`StagingBudget` 约束每 peer 每方向在途字节；稠密单段免 staging 直发；预检失败全体一致退出、目标一字节不动。
- **两个 rank 命名空间**：计划的 source_rank 与 target_rank 分属两张映射，`TransferEndpoints` 用两张独立映射翻译到传输组进程——合并两者正是跨 rank 读错分片的事故来源。

## 7. 下一步学习建议

- **下一讲 u8-l9（批量重副本策略）**：`plan_transfers` 如何把一次同步的全部参数一次性交给可插拔 `replica_policy`（`affine_replica_policy.py` 的 `first_holder`/`local_first_least_loaded`/`per_request`），答案协议（沿 axis 0 的 `ReplicaPart` 切分）如何被 `InvalidReplicaPolicyError`、`min_split_bytes` 与 `PlanBudget.max_pair_checks` 拒止，以及「所有 rank 必须返回相同答案」对成本输入的约束。
- **回看 u8-l3（Universal Checkpoint）**：对比「按名字类别合并」与本讲「按几何直搬」两条通道的分工与互补。
- **继续阅读源码**：`deepspeed/checkpoint/autoep_affine.py` 与 `deepspeed/checkpoint/zero_affine.py`（真实生产者如何组合布局映射与 ZeRO 存储映射）、`deepspeed/checkpoint/affine_ir_spec.md`（affine 中间表示的完整规格书）、以及 [docs/code-docs/source/affine-transfer.rst](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst) 的 "Transport and staging" 一节（staging 归属与零拷贝传输的演进方向）。
- **测试 companion**：`tests/unit/checkpoint/test_affine_shard_map.py` 后半部分（L700 起）的随机布局生成器 `_random_layout` 把五种建造者随机组合做往返一致性压测，是理解「任意布局」语义的最佳材料。
