# 超点标注构造：AddSuperPointAnnotations

## 1. 本讲目标

上一讲（u3-l2）我们看到 `results` 字典里的 `pts_instance_mask`、`pts_semantic_mask`、`sp_pts_mask` 三份数组被 `Pack3DDetInputs_` 打包送进模型。但模型最终训练用的并不是这些「点级」标注，而是一个合成的布尔矩阵 `gt_sp_masks`。本讲聚焦管线中承上启下的一个 transform——`AddSuperPointAnnotations`，读完本讲你应当能够：

1. 说清「点级掩码 → 超点级掩码」的完整转换链：实例 id 清洗与重映射 → one-hot → `scatter_mean` 聚合 → `> 0.5` 二值化。
2. 解释为什么一个超点列在实例行上至多只有一个 `True`（多数投票 + 严格过半），以及语义部分为什么需要一行「unlabeled 兜底」。
3. 画出 `gt_sp_masks` 的行布局（base 实例行 + SAM3D 伪掩码行 + 语义行），并理解 `is_novel` 标记与 `gt_labels_3d` 是如何与这个布局逐行对齐的。
4. 理解测试管线中 `eval_ann_info['pts_instance_mask']` 的「+2 偏移」约定，以及它与后续点提示 transform 里「−2 还原」的呼应。

## 2. 前置知识

- **超点（superpoint）与 `sp_pts_mask`**：回顾 u1-l4，segmentator 把整个场景 mesh 过分割成数百上千个局部平滑的小块，每个点属于且仅属于一个超点。`sp_pts_mask` 是形状为 `(N,)` 的 int64 数组，`sp_pts_mask[p] = j` 表示第 `p` 个点属于第 `j` 个超点（`N` 为点数，超点总数记为 `num_sp`）。超点是**原子的**：模型以超点为最小预测单元，点级标签必须先「投票」聚合到超点上。
- **stuff 与 things**：语义类别 20 类中，`wall(0)`、`floor(1)` 这两类是 stuff（墙面、地面，无独立实例，只有类别），其余 18 类是 things（椅子、桌子等，有独立实例）。配置里 `stuff_classes=[0, 1]`，实例类数 `num_instance_classes = 18`。
- **语义 id 的取值**：管线中 `PointSegClassMapping`（mmdet3d 内置）已把原始语义 id 重映射为连续值：有效类 `0..19`，未标注点为 `20`，恰好等于 `num_classes = num_semantic_classes = 20`，也就是数据集配置中的 `ignore_index`。
- **实例 id 的取值**：`pts_instance_mask` 直接来自数据准备阶段的产物（u1-l4），实例 id 从 1 开始，`0` 表示未标注；并且 ScanNet 的聚合标注里**墙体、地板也有自己的实例 id**，需要在本 transform 中清洗掉。
- **one-hot 编码**：把整数标签 `k` 变成只有第 `k` 位是 1 的向量。`torch.nn.functional.one_hot` 要求输入非负，后面会看到源码用「先加一位、再切掉最后一列」的技巧绕开负数。
- **`torch_scatter.scatter_mean`**：`scatter_mean(src, index, dim=-1)` 把 `src` 中 `index` 相同的位置归为一组并求均值。本讲用它实现「每个超点内部对点级掩码求平均」，即多数投票。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L312-L443) | 本讲主角：`AddSuperPointAnnotations`（L312-L443） |
| [uniseg3d/__init__.py:L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L10) | 把 `AddSuperPointAnnotations` 注册进 `TRANSFORMS`，配置字符串才能找到它 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L171-L176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L171-L176) | 训练管线中的调用位置与四个参数 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L227-L230](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L227-L230) | 测试管线中的调用（无 `merge_ov`） |
| [uniseg3d/loading.py:L44-L52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L44-L52) | 上游：`sp_pts_mask` 的读入，以及 `eval_ann_info` 的写入约定 |
| [uniseg3d/formatting.py:L26-L29](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L26-L29) | 下游：`gt_sp_masks`、`is_novel` 被分拣进 `gt_instances_3d` |
| [uniseg3d/unified_criterion.py:L27-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L27-L41) | 消费方：损失总调度按行布局切分 `gt_sp_masks` |

一句话定位：`AddSuperPointAnnotations` 在管线中位于 `PointSegClassMapping`、几何/颜色增广之后，`TextPromptGeneration`、`PointPromptGeneration` 之前——因为后两者要消费它产出的 `gt_sp_masks` 与 `pts_instance_objextId`（见 u3-l4、u3-l5）。

## 4. 核心概念与源码讲解

先给全景。`transform()`（[uniseg3d/transforms_3d.py:L336-L443](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L336-L443)）执行如下流水线：

```
pts_instance_mask ──①清洗(stuff/unlabeled→-1)──②重映射(连续0..K-1)──③one-hot──④scatter_mean+0.5──> 实例行 (K, num_sp)
                                                                                                          │ (+merge_ov: 拼接 M 行伪掩码)
pts_semantic_mask ──────────────────────③one-hot(21类)──④scatter_mean+0.5──unlabeled兜底──> 语义行 (21, num_sp)
                                                                                                          │
gt_labels_3d / is_novel / pts_instance_objextId ──────与上面行数逐行对齐──────────────────────────────────┤
                                                                                                          ▼
                                                          torch.vstack ──> gt_sp_masks (K+M+21, num_sp)
```

训练配置（`merge_ov=True`、`merge_non_stuff_cls=False`）下 `gt_sp_masks` 的最终行布局：

| 行区间 | 行数 | 内容 | `is_novel` | `gt_labels_3d` |
| --- | --- | --- | --- | --- |
| `0 .. K-1` | K | base 实例（真实 GT） | `False` | 实例语义类别 − 2（取值 0..17） |
| `K .. K+M-1` | M | SAM3D 伪掩码（novel） | `True` | 0（占位，损失侧不用，见 u5-l3） |
| `K+M .. K+M+20` | 21 | 语义行：stuff 0/1 + things 2..19 + 末行 unlabeled | `False` | 0（占位） |

其中 K 为该场景清洗后的真实实例数，M 为伪掩码数（仅训练，M=0 时即测试布局）。这个布局不是摆设——损失总调度正是按「倒数 n+1 行 = 语义、其余 = 实例」切分的（[uniseg3d/unified_criterion.py:L30-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L30-L38)，`sp_masks[:-n-1, :]` 与 `sp_masks[-n-1:, :]`）。

另外提醒一句：类的 docstring 只声明了 `pts_semantic_mask` 一个 Required Key（[uniseg3d/transforms_3d.py:L314-L324](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L314-L324)），实际还依赖 `pts_instance_mask` 与 `sp_pts_mask`——读源码时以代码为准。

### 4.1 实例 id 重映射与 one-hot 构造

#### 4.1.1 概念说明

原始 `pts_instance_mask` 有三个问题：① 含 `0`（未标注）；② 墙体、地板等 stuff 也有实例 id，而实例分割只关心 things；③ 保留下来的实例 id 是稀疏的（比如某场景只有 id 3、5、12），不利于直接做 one-hot（会浪费大量空列）。这一步做三件事：

1. **清洗**：把语义为「未标注(=20)」或「stuff 类」的点的实例 id 一律改成 `-1`；
2. **重映射**：把留下的实例 id 压缩成连续的 `0..K-1`，同时记录原始 id 供跨模块对齐；
3. **one-hot**：把点级整数掩码变成 `(N, K)` 的 0/1 矩阵，为下一节的聚合做准备。

#### 4.1.2 核心流程

```
输入: pts_instance_mask (N,), pts_semantic_mask (N,)
1. pts_instance_mask[sem == num_classes] = -1        # 未标注 → 无效
2. 对每个 stuff_cls in stuff_classes: 同样置 -1        # 墙/地 → 无效
3. idxs = unique(...)                                 # 例如 [-1, 3, 5, 12]，断言 idxs[0] == -1
4. pts_instance_objextId = idxs[1:] - 1               # 原始 0 起 objectId，供文本/点提示对齐
5. 查表 mapping[old_id] = 新_id - 1 的恒等压缩，-1 保持 -1
6. 无效点占一个临时"第 K+1 类"，one_hot 后切掉最后一列 → (N, K)
```

`-1` 的保留贯穿始终：重映射后无效点依旧是 `-1`，one-hot 前把 `-1` 改写为 `max+1`、编码后立刻切掉最后一列，等效于「无效点在所有实例列上全为 0」。这是源码里一个很干净的小技巧。

#### 4.1.3 源码精读

清洗与重映射（[uniseg3d/transforms_3d.py:L346-L363](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L346-L363)）：

```python
pts_instance_mask[pts_semantic_mask == self.num_classes] = -1      # 未标注 → -1
for stuff_cls in self.stuff_classes:                                # wall/floor → -1
    pts_instance_mask[pts_semantic_mask == stuff_cls] = -1

idxs = torch.unique(pts_instance_mask)                              # [-1, 3, 5, 12]
assert idxs[0] == -1                                                # 场景必有墙/地/未标注
input_dict['pts_instance_objextId'] = idxs[1:] - 1                  # [2, 4, 11]

mapping = torch.zeros(torch.max(idxs) + 2, dtype=torch.long)
new_idxs = torch.arange(len(idxs), device=idxs.device)
mapping[idxs] = new_idxs - 1                                        # -1→-1, 3→0, 5→1, 12→2
pts_instance_mask = mapping[pts_instance_mask]                      # 点级 id 替换为连续 id
input_dict['pts_instance_mask'] = pts_instance_mask.numpy()         # 覆写回 results！
```

两个值得咀嚼的细节：

- `mapping[idxs] = new_idxs - 1` 中 `idxs` 含 `-1`，PyTorch 的花式索引把 `-1` 解释为**最后一个元素**。`mapping` 长度是 `max(idxs)+2`，最后一位（`max_id+1`）不会与任何真实实例 id 冲突，于是「`-1` 映射到 `-1`」借助越界一位的尾槽实现，无需特判。
- `pts_instance_objextId = idxs[1:] - 1` 把 1 起的实例 id 还原成 ScanNet 聚合文件里 0 起的 `objectId`。这个数组是行索引 ↔ 原始物体的对照表：`TextPromptGeneration` 用它与 ScanRefer 的 `object_id` 对齐找 `gt_sp_masks` 的行（[uniseg3d/transforms_3d.py:L42-L53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L42-L53)），`PointPromptGeneration` 用它打乱输出 `pts_instance_objextId_shuffle`（[uniseg3d/transforms_3d.py:L160](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L160)）。详见 u3-l4 / u3-l5。

one-hot 构造（[uniseg3d/transforms_3d.py:L366-L372](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L366-L372)）：

```python
insts_mask = pts_instance_mask.clone()                 # 值域 {-1, 0..K-1}
if torch.sum(insts_mask == -1) != 0:
    insts_mask[insts_mask == -1] = torch.max(insts_mask) + 1   # -1 → K（临时桶）
    insts_mask = torch.nn.functional.one_hot(insts_mask)[:, :-1]  # (N, K+1)→(N, K)
else:
    insts_mask = torch.nn.functional.one_hot(insts_mask)
```

无效点先被塞进第 K+1 个桶，切掉最后一列后它们的全 0 行恰好表达「不属于任何实例」。

#### 4.1.4 代码实践

**实践目标**：脱离仓库，用 10 个点的小数组手动复现 L346-L363 的清洗+重映射，验证三个断言。

**操作步骤**（示例代码，仅需 PyTorch）：

```python
import torch

raw_sem = torch.tensor([20, 3, 3, 2, 2, 20, 0, 5, 5, 1])   # 语义: 20=未标注, 0/1=stuff
raw_ins = torch.tensor([0,  7, 7, 3, 3, 0,  2, 5, 5, 4])   # 实例: 0=未标注, 其余为原始 id

# 1) 清洗：未标注与 stuff → -1
raw_ins[raw_sem >= 20] = -1
for stuff_cls in [0, 1]:
    raw_ins[raw_sem == stuff_cls] = -1

# 2) 重映射（与源码逐行对应）
idxs = torch.unique(raw_ins)
assert idxs[0] == -1, '清洗后必含 -1'
obj_ids = idxs[1:] - 1
mapping = torch.zeros(torch.max(idxs) + 2, dtype=torch.long)
new_idxs = torch.arange(len(idxs))
mapping[idxs] = new_idxs - 1
remapped = mapping[raw_ins]

print('idxs =', idxs.tolist())                     # 期望 [-1, 3, 5, 7]
print('obj_ids =', obj_ids.tolist())               # 期望 [2, 4, 6]
print('remapped =', remapped.tolist())
print('mapping[-1] =', mapping[-1].item())         # 期望 -1
```

**需要观察的现象**：`idxs` 首元素必为 `-1`；`remapped` 中原始 id 7/3/5 分别变成 1/0/2，无效点保持 `-1`。

**预期结果**：`remapped = [-1, 1, 1, 0, 0, -1, -1, 2, 2, -1]`；`mapping[-1] == -1`（负索引落在尾槽）。若把 `raw_sem` 中的 20 改成有效类，`assert idxs[0] == -1` 之外的墙/地点仍会触发清洗——可自行实验。

#### 4.1.5 小练习与答案

1. **为什么 `assert idxs[0] == -1` 在 ScanNet 上总能通过？**
   答：`torch.unique` 返回升序去重结果，只要场景中存在至少一个无效点（未标注、墙体或地板的点），`-1` 就一定出现且排在首位；真实 ScanNet 场景不可能没有墙/地。
2. **某场景清洗后实例 id 为 `{2, 9, 9, 4}`，`pts_instance_objextId` 是什么？这些值和 ScanRefer 的 `object_id` 有什么关系？**
   答：`idxs = [-1, 2, 4, 9]`，`pts_instance_objextId = [1, 3, 8]`。它们是 0 起的原始 objectId，与 ScanRefer 标注里的 `object_id` 同一命名空间，因此可直接用作 `gt_sp_masks` 实例部分的行索引对照（见 u3-l5 的对齐代码）。
3. **如果把 L369 的 `torch.max(insts_mask) + 1` 改成 `torch.max(insts_mask)`，会发生什么？**
   答：`-1` 会与最大的合法实例 id 撞在同一列，one_hot 后该列同时覆盖「最大实例的点」和「无效点」，切掉最后一列会把最大实例的真实掩码一并丢掉，实例数凭空少一个。

### 4.2 scatter_mean 聚合与 0.5 阈值二值化

#### 4.2.1 概念说明

模型不预测点，只预测超点（「超点即查询」，u1-l1），所以点级 one-hot 必须聚合成 `(行数, num_sp)` 的超点级掩码。做法是**超点内多数投票**：对超点 \(S_j\) 与第 \(k\) 行（某实例或某语义类），计算

\[
\text{frac}_{k,j} \;=\; \frac{1}{|S_j|}\sum_{p \in S_j} \mathbb{1}\!\left[m_p = k\right],
\qquad
\hat{M}_{k,j} \;=\; \mathbb{1}\!\left[\text{frac}_{k,j} > 0.5\right].
\]

即「该超点内有多大比例的点属于第 k 行」，严格过半才算命中。由 one-hot 性质，同一列各行的比例之和不超过 1：

\[
\sum_k \text{frac}_{k,j} \;\le\; 1 \;\;\Longrightarrow\;\; \text{每列至多一行} > 0.5,
\]

这就是「一个超点至多属于一个实例/一个语义类」的数学保证，也是本讲实践要验证的核心约束。注意两点：

- **实例部分允许「空列」**：无效点不参与投票（全 0 行），一个混有大量未标注点的超点可能没有任何实例行过半——它就是一个背景超点，合法。
- **语义部分不允许空列**：每个点的语义 id 总在 `0..20` 内（one_hot 显式给了 `num_classes+1=21` 列），必须保证每个超点都被分到一个语义行。为此源码加了「unlabeled 兜底」：没有任何语义行过半的超点直接判给最后一行 unlabeled。

`merge_non_stuff_cls` 开关（本配置为 `False`）是语义行的可选压缩：为 `True` 时把 21 行压成 3 行（wall、floor、所有 things 合并成一行），用于只关心 stuff/things 之分的全景设定；本统一配置保留全部 21 行。

#### 4.2.2 核心流程

```
实例: (N,K) one-hot --转置--> (K,N) --scatter_mean(sp_pts_mask, dim=-1)--> (K,num_sp) --">0.5"--> 布尔
语义: (N,21) one-hot --转置--> (21,N) --同上--> (21,num_sp) --">0.5"--> 空列填 unlabeled --> 断言每列≥1个True
(可选) merge_non_stuff_cls: 21 行 → [stuff 2 行, things 求和 1 行]
最终: gt_sp_masks = vstack(实例行, 语义行)
```

`scatter_mean(src, index, dim=-1)` 中 `src` 形状 `(R, N)`、`index` 即 `sp_pts_mask (N,)`，输出 `(R, num_sp)`，第 `(k, j)` 个元素就是上式的 \(\text{frac}_{k,j}\)。

#### 4.2.3 源码精读

实例行聚合（[uniseg3d/transforms_3d.py:L374-L382](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L374-L382)）：

```python
if insts_mask.shape[1] != 0:                    # 场景里至少有一个实例
    insts_mask = insts_mask.T                   # (N,K) → (K,N)
    sp_pts_mask = torch.tensor(input_dict['sp_pts_mask'])
    sp_masks_inst = scatter_mean(insts_mask.float(), sp_pts_mask, dim=-1)  # (K,num_sp)
    sp_masks_inst = sp_masks_inst > 0.5
else:                                           # 无实例的边界场景
    sp_masks_inst = insts_mask.new_zeros((0, input_dict['sp_pts_mask'].max() + 1), dtype=torch.bool)
```

注意边界分支：`K=0`（该场景没有 things 实例）时返回 `(0, num_sp)` 空矩阵而不是崩溃，保证 `vstack` 维度兼容。

语义行聚合与兜底（[uniseg3d/transforms_3d.py:L411-L432](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L411-L432)）：

```python
sem_mask = torch.nn.functional.one_hot(sem_mask, num_classes=self.num_classes + 1)  # (N,21)
sem_mask = sem_mask.T
sp_masks_seg = scatter_mean(sem_mask.float(), sp_pts_mask, dim=-1)   # (21,num_sp)
sp_masks_seg = sp_masks_seg > 0.5

sp_masks_seg[-1, sp_masks_seg.sum(axis=0) == 0] = True   # 空列 → unlabeled 兜底
assert sp_masks_seg.sum(axis=0).max().item()             # 每列至少一个 True

if self.merge_non_stuff_cls:                             # 本配置为 False
    sp_masks_seg = torch.vstack((
        sp_masks_seg[:num_stuff_cls, :],
        sp_masks_seg[num_stuff_cls:, :].sum(axis=0).unsqueeze(0)))

sp_masks_all = torch.vstack((sp_masks_inst, sp_masks_seg))
input_dict['gt_sp_masks'] = sp_masks_all.numpy()
```

训练管线在 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L171-L176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L171-L176) 以 `num_classes=20, stuff_classes=[0,1], merge_non_stuff_cls=False, merge_ov=True` 调用；测试管线（L227-L230）参数相同但没有 `merge_ov`，因此测试布局是 `K + 21` 行。

产出的 `gt_sp_masks` 随后被 `Pack3DDetInputs_` 放进 `gt_instances_3d` 容器（`INSTANCEDATA_3D_KEYS` 含 `gt_sp_masks`，[uniseg3d/formatting.py:L26-L29](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L26-L29)），`_remove_prefix` 去掉 `gt_` 前缀后即损失函数看到的 `insts[i].sp_masks`（[uniseg3d/formatting.py:L121-L122](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L121-L122)）。

#### 4.2.4 代码实践

**实践目标**：用随机数据（1000 点、20 超点、3 实例）单独复现「one-hot → scatter_mean → >0.5」三步，验证输出形状与「每列至多一个 True」。

**操作步骤**：保存为 `sp_agg_demo.py`（示例代码，只依赖 `torch` 与 `torch_scatter`，二者在 u1-l2 搭好的环境中均已安装；无需安装 MMDetection3D）：

```python
import torch
from torch_scatter import scatter_mean

torch.manual_seed(0)
N, NUM_SP, K = 1000, 20, 3

# 1) 每点随机归属一个超点，并保证 20 个超点都非空
sp_pts_mask = torch.randint(0, NUM_SP, (N,))
sp_pts_mask[torch.arange(NUM_SP)] = torch.arange(NUM_SP)

# 2) 点级实例掩码：600 点分给 3 个实例，其余 400 点为无效(-1)
pts_instance_mask = torch.full((N,), -1, dtype=torch.long)
perm = torch.randperm(N)[:600]
pts_instance_mask[perm[:200]], pts_instance_mask[perm[200:400]], pts_instance_mask[perm[400:]] = 0, 1, 2

# 3) one-hot：照抄源码技巧——-1 先塞进临时桶，编码后切掉最后一列
insts_mask = pts_instance_mask.clone()
insts_mask[insts_mask == -1] = insts_mask.max() + 1
insts_mask = torch.nn.functional.one_hot(insts_mask)[:, :-1]      # (N, K)
print('one-hot shape:', tuple(insts_mask.shape))                  # 期望 (1000, 3)

# 4) 聚合 + 二值化（与 L377-L379 一致）
frac = scatter_mean(insts_mask.T.float(), sp_pts_mask, dim=-1)    # (K, NUM_SP)
sp_masks_inst = frac > 0.5
print('gt 实例行 shape:', tuple(sp_masks_inst.shape))             # 期望 (3, 20)

# 5) 核心约束：每列至多一个 True
col_sum = sp_masks_inst.sum(dim=0)
print('每列 True 数最大值 =', col_sum.max().item())
assert (col_sum <= 1).all()

# 6) 观察混合超点：含多个实例的点、但无人严格过半 → 不属于任何实例
for j in torch.where(((frac > 0).sum(dim=0) > 1) & (col_sum == 0))[0][:3]:
    vals = pts_instance_mask[sp_pts_mask == j]
    dist = {int(v): int((vals == v).sum()) for v in torch.unique(vals)}
    print(f'超点 {j}: 点级实例分布 {dist}, 超点级归属行 {sp_masks_inst[:, j].nonzero().flatten().tolist()}')
```

**需要观察的现象**：步骤 3/4 的形状分别是 `(1000, 3)` 与 `(3, 20)`；步骤 5 的最大列和为 0 或 1；步骤 6 打印的超点中，点级同时存在实例 0/1（或带 -1）的点，但超点级要么整体判给严格多数方、要么全空。

**预期结果**：`每列 True 数最大值 = 1`，断言通过；混合超点的归属行要么恰有一行（多数方），要么为空列表（无严格多数，例如 40%/35%/25% 或被无效点稀释）。随机种子固定，具体数值可复现——待本地验证具体打印值。

#### 4.2.5 小练习与答案

1. **为什么阈值必须用 `> 0.5` 而不是 `>= 0.5`？**
   答：若用 `>=`，当超点内两个实例恰好各占 50% 时该列会同时命中两行，破坏「每列至多一个 True」的不变量，下游匹配与损失都会把一个超点同时监督给两个实例。严格大于保证平局时宁缺毋滥。
2. **语义部分为什么需要 `sp_masks_seg[-1, sum==0] = True` 这行兜底，实例部分却不需要？**
   答：语义 one-hot 有 21 列且覆盖所有点（未标注也是一列），但「严格过半」仍可能无解（三分类 40/35/25）；语义分割要求每个超点都有类别，所以空列强制判给 unlabeled。实例部分允许背景超点不属于任何实例，空列合法，无需兜底。
3. **一个超点内 60% 的点属于椅子、40% 属于桌子，聚合结果是什么？这暴露了超点表示的什么局限？**
   答：该超点整体判给椅子，桌子的 40% 点被「带走」。这说明超点是原子单元，过分割误差会直接引入不可修正的标注噪声——这正是论文里需要足够细的超点过分割（segmentator）的原因。

### 4.3 merge_ov 伪掩码合并与 is_novel 标记

#### 4.3.1 概念说明

开放词汇分割要求模型能识别训练标注之外的类别（novel 类）。UniSeg3D 的做法（回顾 u1-l1/u1-l4）：训练时额外引入 SAM3D 生成的**伪实例掩码**（`sam3d_pseudo_sp_masks`，存于官方 infos pkl，仅训练集有），把它们作为 novel 行拼进 `gt_sp_masks`，并用 `is_novel` 向量标记哪些行是伪掩码。这样一份标注里同时存在两路监督：

- **base 行**（真实 GT 实例）：走完整监督——分类 + 掩码损失；
- **novel 行**（伪掩码）：只在掩码上监督（伪掩码类别不可靠，分类标签不可信），由损失侧依据 `is_novel` 路由（详见 u5-l3 的 base/novel 双路匹配）。

与之配套的是 `gt_labels_3d`：与 `gt_sp_masks` **逐行对齐**的整数标签向量，长度等于总行数，只有 base 实例行填真实类别，其余行是占位 0。

#### 4.3.2 核心流程

```
insts = [0..K-1]                                # 重映射后的实例行号
if merge_ov:
    novel_sp_masks = input_dict['sam3d_pseudo_sp_masks']      # (M, num_sp) 伪掩码
    is_novel = zeros(K + M + num_classes + 1)                 # 与总行数对齐
    is_novel[K : K+M] = 1
    sp_masks_inst = cat(sp_masks_inst, novel_sp_masks)        # 实例行变成 K+M 行
gt_labels = zeros(K + M + 21)                                # 长度与行布局一致
for inst in [0..K-1]:
    gt_labels[inst] = 该实例第一个点的语义 id - 2              # things 类 0..17
```

注意 `is_novel` 与 `gt_labels` 的长度公式里都带 `num_classes + 1 = 21`：它们和 `gt_sp_masks` 一样是「每行一个条目」的行级标注，语义行也占位（全 False / 全 0），损失侧切掉后 21 行时一并丢弃（[uniseg3d/unified_criterion.py:L36-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L36-L38) 取 `labels_3d[:-n-1]`、`is_novel[:-n-1]`）。

#### 4.3.3 源码精读

伪掩码拼接与标记（[uniseg3d/transforms_3d.py:L384-L390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L384-L390)）：

```python
insts = new_idxs[1:] - 1                     # 即 [0..K-1]，实例行号
if self.merge_ov:
    novel_sp_masks = input_dict['sam3d_pseudo_sp_masks']        # (M, num_sp)
    is_novel = torch.zeros(len(insts) + len(novel_sp_masks) + self.num_classes + 1)
    is_novel[len(insts):len(insts)+len(novel_sp_masks)] = 1     # 第 K..K+M-1 行为 novel
    input_dict['is_novel'] = is_novel.bool()
    sp_masks_inst = torch.cat((sp_masks_inst, novel_sp_masks), dim=0)
```

`gt_labels_3d` 的构造（[uniseg3d/transforms_3d.py:L392-L409](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L392-L409)）：

```python
num_stuff_cls = len(self.stuff_classes)       # 2
if self.merge_ov:
    gt_labels = insts.new_zeros(len(insts) + len(novel_sp_masks) + num_stuff_cls + 1)
else:
    gt_labels = insts.new_zeros(len(insts) + num_stuff_cls + 1)

for inst in insts:
    index = pts_semantic_mask[pts_instance_mask == inst][0]   # 该实例第一个点的语义 id
    gt_labels[inst] = index - num_stuff_cls                   # 语义 2..19 → 实例类 0..17

input_dict['gt_labels_3d'] = gt_labels.numpy()
```

三处对齐关系值得记住：

- **类别空间换算** `index - num_stuff_cls`：语义类 2（cabinet）对应实例类 0，语义 19（otherfurniture）对应实例类 17，与配置中 `inst_mapping = sem_mapping[2:]`、`num_instance_classes=18`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L10-L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L10-L11)、L283）一致。
- **novel 行标签是占位 0**：`is_novel` 标 True 的行不会被用于分类监督（OVInstanceCriterion 对 novel 命中的查询把分类目标设为忽略值、只算掩码损失，见 u5-l3），所以占位 0 无害。
- **测试管线没有 `merge_ov`**（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L227-L230](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L227-L230)），因此测试时 `results` 里没有 `is_novel` 键——训练管线的 Pack 键清单里有 `'is_novel'` 而测试清单没有（L195 与 L239-L243），两边自洽。

`is_novel` 与 `gt_sp_masks` 一起被 Pack 进 `gt_instances_3d`（[uniseg3d/formatting.py:L26-L29](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L26-L29)），最终在损失总调度里被切成 `is_novel[:-n-1]`（只保留实例部分的 K+M 行，[uniseg3d/unified_criterion.py:L35-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L38)）。

#### 4.3.4 代码实践

**实践目标**：复现 `merge_ov` 分支，验证 `is_novel` 的长度公式与 True 的落点、拼接后的行数。

**操作步骤**（示例代码，仅依赖 torch）：

```python
import torch
K, M, num_classes, num_sp = 4, 2, 20, 50

# 模拟：4 个 base 实例行 + 2 个 SAM3D 伪掩码行（随机超点布尔掩码）
sp_masks_inst = (torch.rand(K, num_sp) > 0.7)
novel_sp_masks = (torch.rand(M, num_sp) > 0.8)

insts = torch.arange(K)
is_novel = torch.zeros(len(insts) + len(novel_sp_masks) + num_classes + 1)   # 照抄 L387
is_novel[len(insts):len(insts)+len(novel_sp_masks)] = 1
sp_masks_inst = torch.cat((sp_masks_inst, novel_sp_masks), dim=0)

gt_labels = torch.zeros(len(insts) + len(novel_sp_masks) + num_classes + 1)  # 照抄 L396
print('合并后实例行 shape:', tuple(sp_masks_inst.shape))               # 期望 (6, 50)
print('is_novel 长度:', is_novel.numel())                              # 期望 27 = 4+2+20+1
print('is_novel=True 的下标:', is_novel.nonzero().flatten().tolist())  # 期望 [4, 5]
print('gt_labels 长度:', gt_labels.numel())                            # 期望 27
```

**需要观察的现象**：合并后实例行为 6 行；`is_novel` 与 `gt_labels` 长度都等于 `K+M+num_classes+1`。

**预期结果**：`is_novel` 长度 = 4+2+20+1 = **27**，True 下标恰为 `[4, 5]`；最终 `gt_sp_masks` 总行数 = 6 + 21 = **27**，与 `is_novel`、`gt_labels` 逐行对齐。请把打印结果与 4.2 节行布局表逐一对照——三个「27」同源，这正是「行级标注与掩码矩阵同长」约定的直观体现。

#### 4.3.5 小练习与答案

1. **为什么 novel 行的 `gt_labels` 保持 0 却不会误导训练？**
   答：伪掩码来自 SAM3D，类别不可靠；损失侧（OVInstanceCriterion）依据 `is_novel=True` 把这些行路由到「仅掩码损失」分支，分类目标被置为忽略值，标签数值本身不参与损失（机制细节在 u5-l3 展开）。
2. **`is_novel` 的长度为什么是 `K+M+num_classes+1` 而不是 `K+M`？**
   答：它按 `gt_sp_masks` 的**总行数**（含 21 个语义行）构造，语义行全为 False；这样它与掩码矩阵同长，损失侧统一用 `[:-n-1]` 切片即可拿到实例部分，避免维护两套索引。
3. **如果测试管线误开了 `merge_ov=True` 会怎样？**
   答：`transform` 会在 `input_dict['sam3d_pseudo_sp_masks']` 处抛 KeyError——官方 `uniseg3d_infos_val.pkl` 不携带该键（伪掩码仅存在于训练 infos，见 u1-l4），这也解释了测试配置为何省略此参数。

### 4.4 eval_ann_info 评估标注生成

#### 4.4.1 概念说明

`eval_ann_info` 是一条「绕过模型」的旁路：测试/验证模式下，mmdet3d 数据集侧会在 `results` 里预先放一个 `eval_ann_info` 字典，管线各 transform 向其中**追加**评估所需的原始标注（`LoadAnnotations3D_` 已写入 `sp_pts_mask` 与场景号 `lidar_idx`，见 [uniseg3d/loading.py:L47-L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L47-L51)）。本 transform 收尾时检查 `'eval_ann_info' in input_dict`：存在（即处于 val/test 管线）就把**重映射后的实例掩码**翻译成评估器期望的 id 体系写进去。

评估 id 体系的约定（与评估器配置 `stuff_class_inds=[0,1]`、`thing_class_inds=[2..19]` 对应，[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L284-L292](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L284-L292)）：

| 点的归属 | 评估用实例 id |
| --- | --- |
| 无效（未标注，`-1`） | `-1`（保持） |
| stuff：语义 0（wall） | `0` |
| stuff：语义 1（floor） | `1` |
| 第 i 个 things 实例（重映射 id `i`） | `i + 2` |

#### 4.4.2 核心流程

```
if 'eval_ann_info' in input_dict:                    # 仅 val/test 管线成立
    pts_instance_mask[pts_instance_mask != -1] += 2  # things: 0..K-1 → 2..K+1
    for idx, stuff_cls in enumerate(stuff_classes):  # 语义 0/1 的点 → 0/1
        pts_instance_mask[pts_semantic_mask == stuff_cls] = idx
    eval_ann_info['pts_instance_mask'] = ...
```

要特别留意：此时 `pts_instance_mask` 是**已重映射**的张量（L363 已覆写 `input_dict['pts_instance_mask']`），训练管线那份 0 起 id 不受影响——eval 版本只写进 `eval_ann_info`，随后由 `Pack3DDetInputs_` 挂到 `data_sample.eval_ann_info`（[uniseg3d/formatting.py:L139-L142](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L139-L142)），最终被评估器 `PromptSupportedUnifiedSegMetric` 消费（[uniseg3d/unified_metric.py:L62](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L62)，详见 u6-l2）。

#### 4.4.3 源码精读

[uniseg3d/transforms_3d.py:L434-L441](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L434-L441)：

```python
if 'eval_ann_info' in input_dict.keys():
    pts_instance_mask[pts_instance_mask != -1] += num_stuff_cls   # things 实例 +2
    for idx, stuff_cls in enumerate(self.stuff_classes):
        pts_instance_mask[pts_semantic_mask == stuff_cls] = idx   # 墙/地 → 0/1
    input_dict['eval_ann_info']['pts_instance_mask'] = pts_instance_mask.numpy()
```

这 6 行建立了测试管线的「+2 偏移」约定，而下游的 `PointPromptGeneration` / `PointPromptTest` 在检测到 `eval_ann_info` 存在时会先做 `-2` 还原再取实例 id（[uniseg3d/transforms_3d.py:L122-L128](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L122-L128)）：

```python
if 'eval_ann_info' in input_dict.keys():
    pts_instance_mask = pts_instance_mask - 2          # 评估 id 还原回重映射 id
    pts_instance_id = pts_instance_id.copy() - 2
    pts_instance_id = np.delete(pts_instance_id, np.where(pts_instance_id < 0))
```

一加一减恰好闭环：`AddSuperPointAnnotations` 前脚把 id 抬高 2 写进评估旁路，点提示 transform 后脚从同一份 `input_dict['pts_instance_mask']`（0 起版本，L363 写入、未被 eval 分支覆写）读值——注意还原分支操作的是 `input_dict` 里的 0 起掩码而非 `eval_ann_info` 里的副本，两者数值上恰好相差 2。这个细节是 u3-l4 的伏笔。

#### 4.4.4 代码实践

**实践目标**：用 6 个点的小数组手工复现 eval 分支，验证 +2 偏移与 stuff 改写的落点。

**操作步骤**（示例代码，仅依赖 torch）：

```python
import torch
# 重映射后的实例掩码与语义掩码（模拟 L362-L363 的产物）
pts_instance_mask = torch.tensor([-1, 0, 0, 1, 2, -1])   # -1 无效, 0/1/2 为三个实例
pts_semantic_mask = torch.tensor([20, 2, 2, 3, 5, 0])    # 未标注, 椅子, 椅子, 床, 桌, 墙
num_stuff_cls, stuff_classes = 2, [0, 1]

out = pts_instance_mask.clone()
out[out != -1] += num_stuff_cls                # 照抄 L436
for idx, stuff_cls in enumerate(stuff_classes):
    out[pts_semantic_mask == stuff_cls] = idx  # 照抄 L437-L438
print(out.tolist())
```

**需要观察的现象**：实例 0/1/2 变成 2/3/4；最后一个点虽是 `-1`，但因语义是墙（0）被改写为 `0`；未标注点保持 `-1`。

**预期结果**：`[-1, 2, 2, 3, 4, 0]`。对照本节开头的评估 id 表逐项核对。若再模拟 `PointPromptGeneration` 的 `-2` 还原（对前 5 个点），应得到 `[-1, 0, 0, 1, 2]`——与重映射 id 完全一致，闭环成立。

#### 4.4.5 小练习与答案

1. **`'eval_ann_info' in input_dict` 这个判断为什么能区分训练与测试？**
   答：该字典由 mmdet3d 数据集侧仅在 `test_mode=True` 时注入 `results`（u3-l1：val/test_dataloader 配置了 `test_mode=True`）；训练管线的 `results` 从不含此键，因此整段 eval 代码在训练时是死代码。仓库内 `LoadAnnotations3D_`、`PointPromptGeneration`、`PointPromptTest`、`Pack3DDetInputs_` 都用同一成员判断做模式分叉。
2. **为什么评估器的 `thing_class_inds` 从 2 开始？**
   答：评估 id 体系把 0/1 留给 wall/floor 两个 stuff，things 实例从 2 起编号（`+num_stuff_cls`），于是评估器可以用 `stuff_class_inds=[0,1]`、`thing_class_inds=range(2,20)` 两段式区分类别性质，与 `gt_labels[inst] = index - num_stuff_cls` 的换算互为逆操作。
3. **`eval_ann_info` 里的 `pts_instance_mask` 与 `input_dict['pts_instance_mask']` 有何不同？**
   答：前者是「评估体系」id（-1/0/1/2..K+1），只随 `data_sample.eval_ann_info` 旁路给评估器；后者是「训练体系」id（-1/0..K-1），继续留在管线里供点/文本提示生成与打包使用。二者数值相差 2（对 things 点）。

## 5. 综合实践

把四个模块串起来：**用合成数据直接驱动真实的 `AddSuperPointAnnotations`**，一次性核对所有输出键与行布局。

1. **实践目标**：验证 `(K+M+21) × num_sp` 的行布局、`gt_labels_3d`/`is_novel` 与 `gt_sp_masks` 的逐行对齐，以及损失侧 `[:-n-1]` 切分的一致性。
2. **操作步骤**（示例代码，需 u1-l2 搭好的完整环境——`transforms_3d` 顶层 `import MinkowskiEngine`，故无法在裸 torch 环境导入；在仓库根目录运行以让 `uniseg3d` 可导入，或 `export PYTHONPATH=$PWD`）：

   ```python
   import numpy as np, torch
   from uniseg3d.transforms_3d import AddSuperPointAnnotations

   rng = np.random.default_rng(0)
   N, num_sp, K = 4000, 100, 5
   # 背景点全部给 stuff(0/1)或未标注(20)，再给 K 个实例各撒 200 个点并赋予 things 语义
   sem = rng.choice([0, 1, 20], N)
   ins = np.zeros(N, dtype=np.int64)                # 实例: 0=未标注, 1..K 为 things
   for i in range(1, K + 1):
       sel = rng.choice(np.where(ins == 0)[0], 200, replace=False)
       ins[sel] = i
       sem[sel] = rng.integers(2, 20)               # 该实例的点随机取一个 things 语义
   sp = rng.integers(0, num_sp, N); sp[:num_sp] = np.arange(num_sp)

   tf = AddSuperPointAnnotations(num_classes=20, stuff_classes=[0, 1],
                                 merge_non_stuff_cls=False, merge_ov=True)
   M = 3
   d = dict(pts_semantic_mask=sem, pts_instance_mask=ins.copy(), sp_pts_mask=sp,
            sam3d_pseudo_sp_masks=torch.rand(M, num_sp) > 0.8)
   out = tf.transform(d)

   K_, M_, n = K, M, 20
   gsm = torch.as_tensor(out['gt_sp_masks']); lab = torch.as_tensor(out['gt_labels_3d'])
   nov = torch.as_tensor(out['is_novel'])
   print('gt_sp_masks:', tuple(gsm.shape), 'gt_labels:', lab.numel(), 'is_novel:', nov.numel())
   assert gsm.shape == (K_ + M_ + n + 1, num_sp) and lab.numel() == nov.numel() == K_ + M_ + n + 1
   assert nov[K_:K_+M_].all() and not nov[:K_].any() and not nov[K_+M_:].any()
   assert ((lab[:K_] >= 0) & (lab[:K_] <= 17)).all()
   inst_part, sem_part = gsm[:-n-1, :], gsm[-n-1:, :]       # 与 unified_criterion L30-L38 同式
   assert inst_part.shape[0] == K_ + M_ and sem_part.shape[0] == n + 1
   assert ((sem_part.sum(dim=0) >= 1).all())                 # 语义每列至少一个 True
   print('行布局与切分全部通过；pts_instance_objextId =', out['pts_instance_objextId'])
   ```

3. **需要观察的现象**：`gt_sp_masks` 形状 `(28, 100)`（5+3+21=28 行）；`is_novel` 恰在第 5..7 位为 True；`gt_labels_3d` 前 5 个值落在 0..17；按损失侧同款切片得到的实例/语义两部分行数正确；语义部分每列至少一个 True（unlabeled 兜底生效）。
4. **预期结果**：全部断言通过；`pts_instance_objextId` 为 5 个 0 起的原始 objectId。若给 `d` 再加一个空字典键 `eval_ann_info={}`，可额外观察到 `out['eval_ann_info']['pts_instance_mask']` 出现 +2 偏移版本，而 `out['pts_instance_mask']` 仍是 0 起版本。随机数据下个别断言（如实例语义范围）依赖构造方式，若失败请先检查合成规则——待本地验证。

## 6. 本讲小结

- **核心技巧**：超点掩码 = 点级 one-hot 掩码在超点维度 `scatter_mean` 后 `> 0.5` 二值化，即「超点内严格多数投票」；由比例和 ≤ 1 保证每列至多一个 True。
- **id 清洗与重映射**：未标注与 stuff（wall/floor）点置 `-1` 后压缩为连续 id `0..K-1`；`mapping[-1]`（负索引尾槽）与「one_hot 多加一列再切掉」是两个精巧的工程技巧；`pts_instance_objextId` 保留 0 起原始 objectId 供提示任务对齐。
- **行布局是全项目的契约**：`gt_sp_masks = [K 个 base 实例行 | M 个 SAM3D 伪掩码行 | 21 个语义行(末行 unlabeled 兜底)]`，`gt_labels_3d` 与 `is_novel` 与之逐行同长，损失总调度用 `[:-n-1]` / `[-n-1:]` 两个切片消费它。
- **base/novel 双路**：`merge_ov=True`（仅训练）拼接伪掩码并置 `is_novel[K:K+M]=True`，novel 行只有掩码监督、分类标签是占位 0——这是开放词汇训练在数据侧的全部准备。
- **评估旁路**：`eval_ann_info['pts_instance_mask']` 采用「stuff=0/1、things 从 2 起」的评估 id 体系（+2 偏移），与点提示 transform 的 -2 还原互为闭环；该分支只在 `test_mode` 下激活。

## 7. 下一步学习建议

本讲产出的 `gt_sp_masks`、`pts_instance_objextId`、重映射后的 `pts_instance_mask` 是后续两个数据侧模块的直接输入：

- 下一讲 **u3-l4（点提示生成）**：看 `PointPromptGeneration` / `PointPromptTest` 如何为每个实例采样提示点，并利用本讲的 `sp_pts_mask` 把点换算成超点 id（`point_prompt_sp_ids`），以及 eval 模式下 `-2` 还原的完整逻辑。
- **u3-l5（文本提示生成）**：看 `TextPromptGeneration` 如何用 `pts_instance_objextId` 把 ScanRefer 的 `object_id` 映射到 `gt_sp_masks` 的行，取出该实例的超点掩码作为 `gt_text_prompt`。
- 若想提前看「行布局」的消费端，可直接跳读 [uniseg3d/unified_criterion.py:L27-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L27-L41)，对照本讲的行布局表验证切片语义。
