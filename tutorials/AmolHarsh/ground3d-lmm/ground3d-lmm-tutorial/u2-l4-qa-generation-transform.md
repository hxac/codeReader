# QA 采样与超点标注生成（QA_Generation_v2）

## 1. 本讲目标

上一讲（u2-l3）我们把点云、超点标签、部件 mask、物体 mask 和 QA 数据都装进了内存。本讲解决数据管线的最后一公里：

1. **点级 mask 如何变成超点级 mask**：`AddSuperPointAnnotations_Part3DGlamm` 用 scatter-mean + 多数投票，把逐点标注聚合成逐超点标注。
2. **嵌套 QA 数据如何被展平与采样**：`QA_Generation_v2.flatten_qa_data` 把 `part_qa_data / object_qa_data / multi_turn_qa_data` 三层结构压成一张平铺的 QA 列表，再随机采样。
3. **回答文本如何决定 GT 形状**：`count_p_tags` 数出答案里 `<p>` 标签的个数，`get_seg_from_single_qa` 据此为每条 QA 造出形状严格的 `(num_p_tags, num_superpoints)` 超点 GT 张量，无效目标用 NaN 行占位。
4. **ElasticTransfrom 弹性增强**：它扰动的是「体素化坐标」而不是点本身，也不碰任何标签。

学完本讲，你应该能独立回答：一条 QA 从 pkl 里的原始字典，到模型训练时可用的 `(文本, 超点 GT mask)` 样本对，中间每一步发生了什么、形状如何变化、为什么用 NaN 而不是丢弃。

## 2. 前置知识

- **超点（superpoint）**：把场景点云过聚类（如 SSTNet）得到的局部小块，同一超点内的点被认为属于同一物体。`sp_pts_mask` 是长度为 N（点数）的数组，第 i 个元素是第 i 个点所属超点的编号。它是本项目中 mask 的最细粒度——模型只在超点级别上预测分割。
- **点级 mask 与超点级 mask 的区别**：数据集标注（`pts_part_mask`、`pts_object_mask`）是逐点的，长度 N、取值是实例编号（-1 表示无标注，见 u2-l1/u2-l3）。但模型的查询解码器在超点特征上打分，所以 GT 必须聚合到超点级：一张形状 `(实例数, 超点数)` 的 0/1 矩阵。
- **scatter-mean（散射平均）**：`torch_scatter.scatter_mean` 按 index 把一组行分桶求平均。对超点聚合而言：

  \[ \bar{x}_{j,c} \;=\; \frac{\sum_{i:\, s_i = j} x_{i,c}}{\left|\{\,i:\, s_i = j\,\}\right|} \]

  其中 \(s_i\) 是点 i 的超点编号，\(x_{i,c}\) 是点 i 的 one-hot 实例指示。再二值化（> 0.5）就是「多数投票」：一个超点只有当超过一半的点属于某实例时，才被判定属于它。
- **`<p>…</p><SEG>` 接地标记**：数据集的回答文本用 `<p>名称</p><SEG>` 标出一个被接地的目标（见 u1-l4、u2-l2）。模型每输出一组该标记，就会产生一个分割查询；因此**答案里有几个 `<p>`，GT mask 就必须有几行**。
- **NaN 占位约定**：文本 QA（如「场景里有几把椅子？」）没有接地目标，或者目标 ID 在本帧标注里不存在——这时 GT 行不能删（会错位），而是整行填 NaN。损失函数与解码器都识别 NaN 并跳过它，本讲 4.3 会给出证据链。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | 本讲主角：`AddSuperPointAnnotations_Part3DGlamm`、`QA_Generation_v2`、`get_seg_from_single_qa`、`count_p_tags`、`ElasticTransfrom` 全部在此 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 特殊 token 清单（`<p>`、`<SEG>` 等）与指令模板 `PART_GLAMM_INSTRUCTION`，解释答案里为什么会有 `<p>` 标签 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练管线的顺序：加载 → 几何增强 → 超点标注 → QA 采样 → 弹性增强 → 打包 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 下游消费：`sp_gt_seg` 被拼接进损失、`elastic_coords` 进入稀疏量化（只看几行，细节在 u3/u5） |
| [uniseg3d/text_prompt_instance_criterion.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py) | 损失侧如何用 `isnan` 过滤 NaN GT 行（一句话证据） |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器 NaN 安全注意力、指令模板的使用位置（一句话证据，详解在 u4/u5） |

管线在配置中的位置（顺序很重要）：

- [configs/ground3dlmm_train.py:L131-L136](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L131-L136)：第 5 步 `AddSuperPointAnnotations_Part3DGlamm`，聚合超点标注。
- [configs/ground3dlmm_train.py:L137-L140](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L137-L140)：第 6 步 `QA_Generation_v2(num_qa=10, random_select=True)`，采样 QA 并生成超点 GT。
- [configs/ground3dlmm_train.py:L144-L149](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L144-L149)：第 7 步 `ElasticTransfrom(gran=[6,20], mag=[40,160], voxel_size=0.02, p=0.5)`。
- [configs/ground3dlmm_train.py:L150-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L150-L158)：第 8 步打包，`keys` 里明确带走了 `selected_qa_data`、`sp_gt_seg`、`elastic_coords`（u2-l5 的主题）。

注意几何增强（`GlobalRotScaleTrans`、`NormalizePointsColor_`，L122-L130）只改点的坐标和颜色，不改任何 mask 数组——点与标签靠数组下标对齐，这一约定是本讲所有 transform 安全工作的前提（u2-l3 已讲）。

## 4. 核心概念与源码讲解

### 4.1 AddSuperPointAnnotations_Part3DGlamm：点级 mask → 超点级 mask

#### 4.1.1 概念说明

`QA_Generation_v2` 需要「第 k 个物体/部件覆盖哪些超点」这样的查询表，而磁盘上只有逐点 mask。`AddSuperPointAnnotations_Part3DGlamm` 就是这座桥：它把 `pts_part_mask`、`pts_object_mask` 各自聚合成超点级 0/1 矩阵，并记录「原始实例 ID → 矩阵行」的对应关系，供后续按 QA 里的 `object_id / part_id` 查表。

之所以必须在 QA 采样**之前**做，是因为 `get_seg_from_single_qa` 直接在这些聚合结果上做行索引。

#### 4.1.2 核心流程

```
输入: sp_pts_mask (N,), pts_part_mask (N,), pts_object_mask (N,)   # -1 = 无标注
对 part / object 各自:
  1. unique(mask) → [-1, 0, 1, ..., K-1]   （要求标注 ID 从 0 连续编号）
     pts_*_objextId = unique[1:]           （原始 ID 表，供后续 in 判断）
  2. one-hot 化:
     -1 → max+1, one_hot 后砍掉最后一列 → (N, K) 的 0/1 矩阵
  3. 转置 (K, N) → scatter_mean(按 sp_pts_mask 分桶) → (K, M)
  4. > 0.5 二值化 → 多数投票
输出: gt_sp_part_masks (K_part, M), gt_sp_object_masks (K_obj, M),
      gt_sp_masks = vstack(两者), gt_labels_3d (部件=0, 物体=1),
      pts_part_objextId / pts_object_objextId（原始 ID 表）
```

两处兜底：对应 mask 为 `None`（该帧没有部件/物体标注）时，产出 0 行的空矩阵，ID 表置空（[transforms_3d.py:L1332-L1335](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1332-L1335)、[L1346-L1349](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1346-L1349)），保证下游键永远存在。

#### 4.1.3 源码精读

聚合核心是复用的 `create_sp_masks`：

- [transforms_3d.py:L1288-L1305](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1288-L1305)：先处理 -1（改成 max+1、one-hot 后 `[:, :-1]` 砍掉垫底的那一列，这是编码「忽略」的经典技巧），再转置成 `(K, N)`，`scatter_mean(..., dim=-1)` 按超点分桶平均，最后 `> 0.5` 多数投票得到 `(K, M)` 布尔矩阵。

part 分支与 ID 表：

- [transforms_3d.py:L1323-L1335](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1323-L1335)：对 `pts_part_mask` 取 `unique`，断言最小值是 -1（标注从 0 开始连续编号的隐含契约），`pts_part_objextId = part_idxs[1:]` 记录原始部件 ID；mask 缺失时给 0 行空矩阵。

object 分支完全对称：

- [transforms_3d.py:L1337-L1349](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1337-L1349)：对 `pts_object_mask` 做同样的事，产出 `gt_sp_object_masks` 与 `pts_object_objextId`。

写出结果：

- [transforms_3d.py:L1368-L1381](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1368-L1381)：分别写入 `gt_sp_part_masks`、`gt_sp_object_masks`，再把两者按行拼接成 `gt_sp_masks`（行 0..K_part-1 是部件，之后是物体）；两者都为空时给 `(0, M)` 空矩阵。
- [transforms_3d.py:L1383-L1397](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1383-L1397)：`gt_labels_3d` 给每行一个类型标签——部件为 0、物体为 1，长度与 `gt_sp_masks` 行数严格一致。

两个值得注意的读码发现：

- 配置里传入的 `num_classes / stuff_classes / merge_non_stuff_cls / merge_ov`（[configs/ground3dlmm_train.py:L131-L136](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L131-L136)）在这个类的 `transform` 里**一个都没用到**——构造签名沿用自同文件的 `AddSuperPointAnnotations`（ScanNet 语义分割版，L1126 起），Ground3D 路径只需要三个 mask 键。读码时不要被参数表面意义误导。
- 「多数投票」是有损压缩：若某超点恰好一半点属于 A、一半属于 B，两个实例都拿不到它（`> 0.5` 是严格大于）。聚合后某个实例可能一行全 0——这正是 4.3 里 `seg.sum()==0 → NaN` 兜底存在的原因之一。

#### 4.1.4 代码实践

**实践目标**：用 12 个点的小合成场景，亲手验证 one-hot → scatter-mean → 多数投票的每一步结果，特别观察「50/50 平分超点谁都拿不到」。

**操作步骤**（示例代码，需要 u1-l2 装好的环境，因为 `import uniseg3d` 会触发 MinkowskiEngine 等导入）：

```python
# practice_sp_ann.py（示例代码）
import numpy as np
from mmdet3d.registry import TRANSFORMS
import uniseg3d  # 触发注册（custom_imports 的手动版）

t = TRANSFORMS.build(dict(
    type='AddSuperPointAnnotations_Part3DGlamm',
    num_classes=20, stuff_classes=[0, 1],
    merge_non_stuff_cls=False, merge_ov=True))

# 12 个点、4 个超点：sp0=点0-3，sp1=点4-5，sp2=点6-8，sp3=点9-11
sp_pts_mask    = np.array([0,0,0,0, 1,1, 2,2,2, 3,3,3], dtype=np.int64)
# 物体：点1-5 属物体0，点6-8 属物体1，其余无标注
pts_object_mask = np.array([-1,0,0,0,0,0, 1,1,1, -1,-1,-1], dtype=np.int64)
# 部件：点1-4 属部件0，点5 属部件1
pts_part_mask   = np.array([-1,0,0,0,0,1, -1,-1,-1, -1,-1,-1], dtype=np.int64)

out = t(dict(sp_pts_mask=sp_pts_mask,
             pts_part_mask=pts_part_mask,
             pts_object_mask=pts_object_mask))
print('gt_sp_part_masks =\n',   out['gt_sp_part_masks'])
print('gt_sp_object_masks =\n', out['gt_sp_object_masks'])
print('pts_part_objextId =',   out['pts_part_objextId'].tolist())
print('pts_object_objextId =', out['pts_object_objextId'].tolist())
print('gt_labels_3d =', out['gt_labels_3d'])
```

**需要观察的现象与预期结果**（以下为按源码推演的预期，精确打印值**待本地验证**）：

- `gt_sp_object_masks` 应为两行：物体0 覆盖 sp0（3/4 点 > 0.5）和 sp1（2/2），物体1 覆盖 sp2（3/3）——形如 `[[1,1,0,0],[0,0,1,0]]`。
- `gt_sp_part_masks` 里部件1（只占 sp1 的一半点，均值恰为 0.5，不过半）应当一行**全 0**——这就是多数投票丢弃的实例。
- `gt_labels_3d` 应为 `[0, 0, 1, 1]`（两个部件、两个物体）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 one-hot 前要先把 -1 改成 `max+1`，one-hot 后又砍掉最后一列？
**答案**：`one_hot` 不接受负数。把 -1 统一挪到 max+1 这个「垫底桶」，one-hot 后 `[:, :-1]` 恰好把这一列丢掉，等价于「无标注的点对所有实例贡献 0」，一行代码实现了 ignore 语义。

**练习 2**：如果某帧的 `pts_object_mask` 全是 -1，这个 transform 会输出什么？下游会不会崩？
**答案**：`object_idxs = [-1]`，`pts_object_objextId` 变成空表，`create_sp_masks` 走 `mask.shape[1] != 0` 的分支后得到 0 行矩阵（准确路径是 one-hot 后仅剩垫底列被砍掉、列为 0，走 else 给 `(0, M)` 空矩阵）；`gt_sp_masks` 退化为只剩部件行。下游 `get_seg_from_single_qa` 遇到 0 行 mask 会返回 NaN 行，不会崩。

**练习 3**：QA 里的 `object_id` 是原始标注 ID，而 `gt_sp_object_masks` 的行号是紧凑序号。两者何时一致？
**答案**：当标注 ID 从 0 连续编号时（`unique` 后为 -1,0,1,…,K-1），原始 ID k 恰好等于矩阵第 k 行。代码里 `assert object_idxs[0] == -1`（[transforms_3d.py:L1341](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1341)）就是在守这条契约；自制数据集若 ID 有空洞（如 0,2,5），行号与 ID 错位，必须重编号。

### 4.2 QA_Generation_v2 的 flatten_qa_data：三级 QA 展平与采样

#### 4.2.1 概念说明

u2-l2 讲过，pkl 里的 `qa_data` 是三层嵌套字典：`qa_data[qa_level][i_key][i_task_type] = [qa字典, ...]`，其中 `qa_level ∈ {part_qa_data, object_qa_data, multi_turn_qa_data}`，`multi_turn_qa_data` 直接是「对话列表的列表」。训练时不可能把一帧的几百条 QA 全喂给模型（上下文长度和训练效率都不允许），所以先把嵌套结构**展平成一张列表**，再**随机采样** `num_qa` 条。同时给每条 QA 打上 `qa_keys`（part/object/multi_turn，决定 4.3 里查哪张 mask 表）和 `qa_type`（完整任务路径字符串）两个运行时标签。

#### 4.2.2 核心流程

```
flatten_qa_data(qa_data):
  for qa_level in qa_data:
    if qa_level == 'part_qa_data':   qa_keys = 'part';    遍历 [i_key][i_task_type]
    elif qa_level == 'object_qa_data': qa_keys = 'object'; 遍历 [i_key][i_task_type]
    elif qa_level == 'multi_turn_qa_data':
        每段对话 → 逐轮检查是否有 question/answer
        → 有效轮打上 qa_keys='multi_turn' 后逐条 extend（拆成独立单轮！）
        → 整段无效则丢弃
    else: 静默跳过
    单轮层：若列表第一条没有 question/answer → 整个子任务列表丢弃
transform:
  flat = flatten(qa_data)
  select = random.sample(flat, num_qa)  （不足 num_qa 则全取）
  → 进入 4.3 的逐条 GT 生成
```

#### 4.2.3 源码精读

- [transforms_3d.py:L427-L438](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L427-L438)：遍历三个层级；`None` 层跳过；**只认识** `part_qa_data` 和 `object_qa_data` 两个名字，分别映射到 `qa_keys='part'/'object'`，其他层级名静默丢弃。
- [transforms_3d.py:L439-L450](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L439-L450)：单轮层的双层循环；`.copy()` 只是列表浅拷贝；**只检查列表第一条**是否有 `question/answer`，没有就丢弃整个子任务列表；通过的每条 QA 被就地打上 `qa_keys` 与 `qa_type = qa_level + '_' + i_key + '_' + i_task_type`（如 `part_qa_data_qa_data_seg_functional_part_grounding`）。注意这是对 `input_dict['qa_data']` 内部字典的**就地修改**（副作用）。
- [transforms_3d.py:L451-L471](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L451-L471)：多轮层。每段对话先过滤出含 `question/answer` 的有效轮（注释点名了「LLM 输出解析失败、只剩 `raw_model_output`」的真实脏数据场景），有效轮数 为 0 则整段丢弃；否则**把各轮作为独立 dict 逐条 extend 进平铺列表**。也就是说：**训练版把多轮对话拆散成单轮 QA**。
- [transforms_3d.py:L476-L481](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L476-L481)：采样。`len < num_qa` 时全取，否则 `random.sample`。配置里 `num_qa=10`。
- 两个「摆设参数」：`random_select` 与 `task_level/task_type` 存在 `__init__`（[transforms_3d.py:L418-L425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L418-L425)）但 `transform` **从不读取**——采样永远在「够数就随机抽」；任务过滤只在评估版 `QA_Generation_Test`（u1-l4 的三个旋钮）里生效。这是接口兼容留下的痕迹。

与评估版的对照（理解训练/评估差异的关键）：

- [transforms_3d.py:L574-L586](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L574-L586)：评估版 `flatten_qa_data` 对多轮对话**保持 list 原样 append**（注释写明：让 eval 解码器按对话分派到多轮路径），因此 eval 平铺列表里 dict 与 list 混存。
- [transforms_3d.py:L632-L686](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L632-L686)：评估版 `transform` 全量不采样（`select_qa_list = flat_qa_list`），且多轮分支**逐轮单独 append** GT mask（注释强调不要把各轮拼成一个条目），使 GT 与解码器逐轮产出的预测一一对应。

顺带一个读码发现：训练版 `transform` 的多轮分支（[transforms_3d.py:L515-L536](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L515-L536)）在当前实现下**不会被执行**——训练版展平只产出 dict，不产出 list。该分支继承自被注释掉的 v1 版本（L67-L269），里面还遗留着 `print` + `time.sleep(1)` 调试代码（L528-L530）；若哪天它被走到，训练会显著变慢。读开源代码时对「死分支 + 调试残留」要保持警觉。

#### 4.2.4 代码实践

**实践目标**：构造一个涵盖「单轮 part、空 object 层、多轮对话、解析失败对话」四种情况的最小 `qa_data`，观察展平输出的条目数、打上的标签，以及多轮被拆散的行为。

**操作步骤**（示例代码）：

```python
# practice_flatten.py（示例代码）
import random
from mmdet3d.registry import TRANSFORMS
import uniseg3d

random.seed(0)
t = TRANSFORMS.build(dict(type='QA_Generation_v2', num_qa=10, random_select=True))

qa_data = {
    'part_qa_data': {                      # qa_level → i_key → sub_task → 列表
        'qa_data_seg': {
            'functional_part_grounding': [
                {'question': 'Where is the backrest?<SEG>',
                 'answer': '<p>backrest</p><SEG>', 'object_id': [3]},
            ],
        },
    },
    'object_qa_data': None,                # 空层：直接跳过
    'multi_turn_qa_data': [
        [                                   # 一段两轮的对话
            {'question': 'How many chairs?', 'answer': 'Two.', 'object_id': [2]},
            {'question': 'Point to the seat.', 'answer': '<p>seat</p><SEG>', 'part_id': [5]},
        ],
        [                                   # LLM 解析失败的对话：整段被丢
            {'raw_model_output': '...garbage...'},
        ],
    ],
}

flat = t.flatten_qa_data(qa_data)
for q in flat:
    print(q.get('qa_keys'), '|', q.get('qa_type'), '|', q['question'][:30])
```

**需要观察的现象与预期结果**（待本地验证）：

- 输出应为 **3 条**：1 条 part 单轮（`qa_keys='part'`，`qa_type='part_qa_data_qa_data_seg_functional_part_grounding'`），2 条来自第一段对话的**独立**轮次（`qa_keys='multi_turn'`）；解析失败的那段对话完全消失。
- 3 < num_qa=10，若接着调 `t.transform`，采样不会发生，全部保留。
- 再检查 `qa_data['part_qa_data']...[0]` 这个原字典：它已被就地写入 `qa_keys`/`qa_type` 字段——验证 4.2.3 说的副作用。

#### 4.2.5 小练习与答案

**练习 1**：为什么训练时把多轮对话拆成单轮，评估时却要保持对话结构？
**答案**：训练目标是让模型学会「一问一答 + 接地」，拆成单轮后每条样本独立计算损失，简单高效；评估要对齐论文协议——多轮任务考察模型在连续上下文中的表现，GT 与预测都要按对话、按轮组织（评估版逐轮 append mask 正是为了与逐轮预测一一对齐），拆散就无法评估多轮能力。

**练习 2**：某子任务列表的第一条 QA 缺 `answer` 字段但其余 50 条完好，会发生什么？
**答案**：整条子任务列表被丢弃（[transforms_3d.py:L444-L450](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L444-L450) 只探查 `i_qa[0]`）。这是有意的粗过滤：转换阶段（u2-l2）已保证过格式，这里只拦「整批解析失败」的脏数据。

**练习 3**：`num_qa` 调大（比如 50）对训练意味着什么？
**答案**：每帧采样的 QA 条数增多，单步语言损失覆盖更多问答，但前向序列更长、显存与时间开销更大；配置默认 `num_qa=10` 是精度与成本的折中（u5-l3 讲训练调参时还会回到这个旋钮）。

### 4.3 `<p>` 标签计数与 get_seg_from_single_qa：QA → 超点 GT 的对齐

#### 4.3.1 概念说明

这是本讲最核心的模块，回答「一条 QA 如何变成一行行的超点 GT mask」。

先看「约」的一端：`uniseg3d/prompt_template.py` 的 [SEG_TOKEN 定义（L2-L4）](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L2-L4) 把 `<p>`、`</p>`、`<SEG>`、`<name>`、`<sentence>` 列为特殊 token；[PART_GLAMM_INSTRUCTION（L36-L43）](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43) 这条指令模板明确要求模型「在回答中用 `<p>object name</p><SEG>` 或 `<p>part name</p><SEG>` 标注被接地的对象」（训练时在 [query_decoder.py:L1547](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1547) 被格式化成输入文本）。因此**数据侧 GT 回答里有几个 `<p>`，模型就被期望产出几个分割查询**——数据与模型靠同一套标记语言对齐。

于是「标」的一端规则非常明确：`count_p_tags` 数 `<p>` 个数（封顶 2），`get_seg_from_single_qa` 必须返回**恰好** `(num_p_tags, num_superpoints)` 的张量，行与 `<p>` 按出现顺序一一对应；取不到有效 mask 的行用 NaN 占位，绝不改变行数。

#### 4.3.2 核心流程

```
对采样到的每条 qa（dict）:
  num_p_tags = count_p_tags(qa['answer'])        # 数 '<p>'，>2 封顶为 2；0 → 返回 (1, M) 全 NaN
  get_seg_from_single_qa(qa, num_p_tags, M, gt_sp_object_masks, gt_sp_part_masks, input_dict):
    1. 按 qa['qa_keys'] 分派查哪张表:
       'object'    → object_id ↔ gt_sp_object_masks, 参照表 pts_object_objextId
       'part'      → object_id ↔ gt_sp_part_masks,  参照表 pts_part_objextId   ← 注意：part 单轮用的也是 object_id
       'multi_turn'→ 键里有 object_id 用物体表、有 part_id 用部件表（可两者都有）
       其他        → 全 NaN
    2. 每个 id: 归一化为 int → 检查是否出现在参照表（本帧是否存在）→ 越界检查
       → 任一不过: 该行置 NaN
    3. seg = mask 表[target_id]，一行 (M,)
    4. 行内全 0（聚合后被多数投票吞掉）→ 该行置 NaN
    5. 拼接后行数 != num_p_tags（数据不一致）→ 整块替换为 (num_p_tags, M) 全 NaN
输出: sp_gt_seg = [每条 QA 一个 (num_p_tags, M) 张量], selected_qa_data = [qa dict]
```

NaN 的下游证据链（为什么敢用 NaN 占位）：

- 解码器自注意力对 NaN 查询先置 0 再计算：[query_decoder.py:L397-L408](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L397-L408)。
- 损失侧用 `~torch.isnan(mask).all(dim=1)` 过滤 NaN GT 行：[text_prompt_instance_criterion.py:L550-L554](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L550-L554)。

#### 4.3.3 源码精读

计数器：

- [transforms_3d.py:L406-L414](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L406-L414)：`count_p_tags` 只数开标签 `<p>`（闭标签成对出现，不重复计数），**超过 2 个封顶为 2**——数据集的接地 QA 设计上每条答案最多标两个目标（如比较类任务「A 比 B 更近」）。

零标签短路：

- [transforms_3d.py:L273-L274](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L273-L274)：`num_p_tags == 0`（纯文本问答）直接返回 `(1, M)` 全 NaN——保留 1 行而不是 0 行，与解码器「找不到 `<p>` 时返回一个 NaN 查询 token」的行为对齐，保证 batch 里每条 QA 都有 GT 条目。

类型分派：

- [transforms_3d.py:L279-L311](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L279-L311)：按 `qa_keys` 选表。注意 L289 的行内注释——part 单轮 QA 用 **`object_id`** 查**部件表**「不是 bug」：原始 part_qa 数据只带 object_id（部件以其宿主物体的口吻引用），只有 multi_turn 数据才带真正的 `part_id`。这是数据集构建历史的产物，读码时最容易误解的一行。

ID 归一化与存在性检查：

- [transforms_3d.py:L316-L329](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L316-L329)：把每个 id 强转 int（容忍 float / 可解析字符串），转失败的保留原值、稍后判无效。
- [transforms_3d.py:L331-L332](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L331-L332)：`target_id in reference_id`——QA 提到的实例必须是**本帧标注里真实存在**的 id（多帧场景中 QA 可能引用其他帧的实例）。
- [transforms_3d.py:L348-L349](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L348-L349)：目标列表为空或 mask 表 0 行 → 整块 NaN。

逐 id 取行与三重兜底：

- [transforms_3d.py:L352-L358](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L352-L358)：不在参照表 → 该行 NaN。
- [transforms_3d.py:L360-L384](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L360-L384)：越界（`target_id > max_valid_index`）→ 该行 NaN；否则 `seg = mask_tensors[target_id]` 取出 `(M,)` 行并 `unsqueeze` 成 `(1, M)`。
- [transforms_3d.py:L386-L389](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L386-L389)：取出的行**全 0**（实例在多数投票中被吞掉，见 4.1）→ 该行 NaN。

形状契约：

- [transforms_3d.py:L391-L404](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L391-L404)：所有行拼接后若行数 ≠ `num_p_tags`（标注数量与 `<p>` 数对不上，属数据不一致），打印诊断信息并**整块替换**为 `(num_p_tags, M)` 全 NaN。宁可放弃这条 QA 的分割监督，也不交付错位的 GT——位置对应关系（第 k 行 ↔ 第 k 个 `<p>`）比内容更优先。

主循环与产出：

- [transforms_3d.py:L486-L497](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L486-L497）：`num_superpoints` 的推导顺序：部件表行宽 → 物体表行宽 → `unique(sp_pts_mask)` 兜底（该帧完全没有实例标注时仍能得到正确的 M）。
- [transforms_3d.py:L502-L514](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L502-L514)：逐条采样 QA → 数 `<p>` → 生成 GT，分别追加进 `sp_gt_seg` 与 `valid_qa_list`，**两个列表等长、逐索引对应**。
- [transforms_3d.py:L539-L541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L539-L541)：写出 `sp_gt_seg` 与 `selected_qa_data`。

下游去向（下一讲 u2-l5 的引子）：两者都被 `Pack3DDetInputs_Part_Glamm` 收走（[configs/ground3dlmm_train.py:L150-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L150-L158)）；训练时模型把同一样本的所有 GT 块拼成一张大表送进损失（[uniseg3d.py:L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1125) `torch.cat(..., dim=0)`），`selected_qa_data` 则经数据预处理器进入 `batch_inputs_dict`（[data_preprocessor.py:L107-L108](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L107-L108)）喂给解码器构造对话（[uniseg3d.py:L1170-L1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1170-L1172)）。

#### 4.3.4 代码实践

**实践目标**：绕开 pipeline，直接调用 `count_p_tags` 与 `get_seg_from_single_qa`，用四种典型 QA 验证形状契约与 NaN 规则。

**操作步骤**（示例代码）：

```python
# practice_seg.py（示例代码）
import torch
from uniseg3d.transforms_3d import get_seg_from_single_qa, count_p_tags

M = 4  # 4 个超点
gt_sp_object_masks = torch.tensor([[1., 1., 0., 0.],
                                   [0., 0., 1., 0.]])   # 物体0、物体1 的超点行
gt_sp_part_masks   = torch.tensor([[1., 0., 0., 0.],
                                   [0., 0., 0., 0.]])   # 部件1 聚合后全 0（被多数投票吞掉）
input_dict = dict(pts_object_objextId=torch.tensor([0, 1]),
                  pts_part_objextId=torch.tensor([0, 1]))

cases = [
    # (说明, qa 字典, 预期)
    dict(qa={'answer': '<p>chair</p><SEG>', 'object_id': [0], 'qa_keys': 'object'}),
    dict(qa={'answer': 'There are two chairs.', 'object_id': [0], 'qa_keys': 'object'}),  # 无 <p>
    dict(qa={'answer': '<p>chair</p> and <p>table</p><SEG>',
             'object_id': [0, 7], 'qa_keys': 'object'}),                                  # 7 不在本帧
    dict(qa={'answer': '<p>leg</p><SEG>', 'part_id': [1], 'qa_keys': 'multi_turn'}),       # 部件行全 0
]
for c in cases:
    n = count_p_tags(c['qa']['answer'])
    seg = get_seg_from_single_qa(c['qa'], n, M, gt_sp_object_masks, gt_sp_part_masks, input_dict)
    print(f"n_p_tags={n} shape={tuple(seg.shape)} nan_rows={torch.isnan(seg).all(dim=1).sum().item()}")
    print(seg)
print(count_p_tags('<p>a</p><p>b</p><p>c</p>'))   # 封顶 → 2
```

**需要观察的现象与预期结果**（待本地验证，按源码推演）：

1. 第 1 条：`n=1`，`shape=(1,4)`，内容 `[[1,1,0,0]]`，无 NaN。
2. 第 2 条：`n=0`，短路返回 `shape=(1,4)` 全 NaN——纯文本 QA 也占一行。
3. 第 3 条：`n=2`，`shape=(2,4)`，第 1 行是物体 0 的 mask，第 2 行全 NaN（id 7 不在参照表）。
4. 第 4 条：`n=1`，`shape=(1,4)` 全 NaN（部件 1 聚合后全 0，触发 `seg.sum()==0` 兜底）。
5. 最后一行打印 `2`——三个 `<p>` 被封顶。

#### 4.3.5 小练习与答案

**练习 1**：一条 QA 答案含 2 个 `<p>`，但 `object_id` 列表只有 1 个有效 id，最终 GT 是什么？
**答案**：拼出的行数 1 ≠ 2，触发形状契约兜底（L393-L403），整块替换为 `(2, M)` 全 NaN。两个目标只监督了一个不如都不监督——错位监督比缺失监督危害更大。

**练习 2**：为什么 `num_p_tags == 0` 时返回 `(1, M)` NaN 而不是 `(0, M)` 空张量？
**答案**：为了与解码器行为对齐：模型找不到 `<p>` 时也产出一个 NaN 查询 token（v1 版注释 L135-L137 明确记录了这一对应关系）。GT 与预测在条目级一一对应，损失侧再用 isnan 过滤，两侧约定一致才不会错位。

**练习 3**：如果直接把 4.1 实践里那行全 0 的部件 1 行接给损失函数（不转 NaN），会发生什么？
**答案**：BCE/Dice 会把这个实例的 GT 当成「全背景」来监督对应查询，等于**明确教模型把该目标预测为空**——这是错误监督；而多数投票丢掉它只是「没有监督」。NaN 就是把前者降级为后者的机制。

### 4.4 ElasticTransfrom：不改标签的弹性扰动

#### 4.4.1 概念说明

`ElasticTransfrom`（类名拼写少了个 'a'，注册名以代码为准）源自点云分割训练的经典弹性增强传统。但本项目里的实现有一个关键特点：它**不修改 `points` 本身，也不碰任何 mask**，只是生成一份加了平滑噪声场的坐标副本 `elastic_coords`，供模型在稀疏量化（MinkowskiEngine collate）时替代原始坐标使用（[uniseg3d.py:L1127-L1129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1127-L1129)）。这样扰动只改变「点落进哪个体素、和谁成为稀疏邻居」，而下标对齐的一切标签（超点、mask、QA GT）原封不动——一种零对齐成本的几何增强。

#### 4.4.2 核心流程

```
transform:
  coords = points.tensor[:, :3] / voxel_size        # 换算到体素单位
  以概率 p 依次施加两组弹性扰动:
      coords = elastic(coords, gran=6,  mag=40)     # 细网格、小幅度 → 局部抖动
      coords = elastic(coords, gran=20, mag=160)    # 粗网格、大幅度 → 大尺度弯曲
  input_dict['elastic_coords'] = coords             # 只存副本，points 不动

elastic(x, gran, mag):
  1. 按 |x|.max // gran 生成三通道高斯噪声体 (噪声场网格)
  2. 三抽头可分离盒式滤波沿 x/y/z 各平滑两遍 → 低频平滑噪声场
  3. RegularGridInterpolator 在每个点坐标处插值出噪声
  4. 返回 x + 噪声 * mag
\[
  \mathbf{x}' \;=\; \mathbf{x} \;+\; \lambda \cdot \mathbf{g}(\mathbf{x}),
  \qquad \mathbf{g} = \text{平滑后的三通道高斯随机场}
\]
```

`gran` 是噪声网格的边长（体素单位）：网格越粗（gran 大）噪声随空间变化越慢，配合大 `mag` 产生大尺度弯曲；网格越细（gran 小）配合小 `mag` 产生局部抖动。两者叠加等价于对场景做一次随机「揉捏」。

#### 4.4.3 源码精读

- [transforms_3d.py:L1069-L1084](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1069-L1084)：入口。坐标除以 `voxel_size`（配置 0.02，即 1 单位 = 2cm）；以概率 `p`（配置 0.5）先后施加两组扰动；结果写入 `elastic_coords`。注意即使不触发扰动，`elastic_coords` 也会被写入（未扰动的体素坐标）。
- [transforms_3d.py:L1086-L1107](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1086-L1107）：噪声场构建。`noise_dim = |x|.max(0) // gran + 3` 决定网格分辨率；三个独立 `randn` 体分别对应 x/y/z 三个方向的位移。
- [transforms_3d.py:L1099-L1101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1099-L1101)：三抽头盒式滤波核（blur0/1/2 分别沿 x/y/z 轴）；它们在 [transforms_3d.py:L1109-L1113](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1109-L1113) 按 `blur0,blur1,blur2,blur0,blur1,blur2` 的顺序各卷一遍，把白噪声平滑成低频随机场。
- [transforms_3d.py:L1115-L1123](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1115-L1123)：规则网格插值器在每个点处采样三个方向的噪声并乘 `mag`，加到坐标上。

与管线顺序的关系：它排在 `QA_Generation_v2` **之后**（配置 L137-L149）。这很自然——它不依赖也不改变 QA 与 mask 的任何内容，只附加一份与点数等长的坐标数组；先做谁都不影响对方。真正的「消费」发生在模型侧 `collate` 里（u3-l3 详讲）。

#### 4.4.4 代码实践

**实践目标**：直观感受 `gran/mag` 两组参数的位移量级，并验证 `points` 本身不被修改。

**操作步骤**（示例代码）：

```python
# practice_elastic.py（示例代码）
import numpy as np, torch
from mmdet3d.registry import TRANSFORMS
from mmdet3d.structures import LiDARPoints
import uniseg3d

t = TRANSFORMS.build(dict(type='ElasticTransfrom',
                          gran=[6, 20], mag=[40, 160],
                          voxel_size=0.02, p=1.0))
pts = torch.rand(5000, 3) * 4.0            # 4m 见方的合成场景

r1 = t(dict(points=LiDARPoints(pts.clone())))
r2 = t(dict(points=LiDARPoints(pts.clone())))
d = np.linalg.norm(r1['elastic_coords'] - r2['elastic_coords'], axis=1)
print('两次随机的位移（体素）: mean=%.1f max=%.1f' % (d.mean(), d.max()))
print('points 未被修改:', torch.allclose(r1['points'].tensor, pts))

t0 = TRANSFORMS.build(dict(type='ElasticTransfrom',
                           gran=[6, 20], mag=[40, 160],
                           voxel_size=0.02, p=0.0))
r0 = t0(dict(points=LiDARPoints(pts.clone())))
print('p=0 时等于原始体素坐标:',
      np.allclose(r0['elastic_coords'], pts.numpy() / 0.02))
```

**需要观察的现象与预期结果**（数量级**待本地验证**）：

- `points 未被修改` 与 `p=0` 两个断言应为 True——确认「只生成副本」的设计。
- 两次随机位移的均值应在**几个到几十个体素**量级（即数厘米到分米级，1 体素 = 2cm）；`max` 可达上百体素。这解释了为什么概率只有 0.5：扰动相当强。

#### 4.4.5 小练习与答案

**练习 1**：为什么弹性增强不改标签也不会造成「点与标签错位」？
**答案**：标签数组与点通过**下标**对齐，弹性扰动只改变每个点的坐标数值（写进并行的 `elastic_coords` 数组），既不删除也不重排点。下标不变，对齐不变；扰动只体现在体素化时点的空间邻居关系发生变化。

**练习 2**：把 `p` 从 0.5 调到 1.0、`mag` 翻倍，可能带来什么风险？
**答案**：每个样本都被强扰动，点可能被推移到别的表面里，量化后局部几何被破坏，训练更难收敛；这类几何增强通常配合较小概率使用，配置选 p=0.5 是稳健点。

**练习 3**：`elastic_coords` 为什么在 `transform` 里就除以 `voxel_size`，而不是留给模型侧做？
**答案**：把单位统一到「体素」后，`gran/mag` 的语义与 `voxel_size` 解耦——同一组增强参数在不同体素大小的配置下产生可比的相对扰动；模型侧 `collate` 拿到的是可直接量化的整数化坐标（MinkowskiEngine 按 1.0 步长量化，见 u3-l3）。

## 5. 综合实践

**任务**：不跑训练，用一份合成数据把本讲三个 transform 按配置顺序串成迷你管线，并对输出做四项不变量检查。

**步骤**（示例代码）：

```python
# practice_pipeline.py（示例代码）
import random
import numpy as np, torch
from mmengine.dataset import Compose
from mmdet3d.registry import TRANSFORMS
from mmdet3d.structures import LiDARPoints
import uniseg3d

random.seed(0); np.random.seed(0); torch.manual_seed(0)

pipeline = Compose([
    TRANSFORMS.build(dict(
        type='AddSuperPointAnnotations_Part3DGlamm',
        num_classes=20, stuff_classes=[0, 1],
        merge_non_stuff_cls=False, merge_ov=True)),
    TRANSFORMS.build(dict(type='QA_Generation_v2', num_qa=4, random_select=True)),
    TRANSFORMS.build(dict(type='ElasticTransfrom',
                          gran=[6, 20], mag=[40, 160], voxel_size=0.02, p=1.0)),
])

N = 2000
sp = np.random.randint(0, 50, size=N).astype(np.int64)          # 50 个超点
obj = np.full(N, -1, dtype=np.int64); obj[:1000] = np.repeat(np.arange(20), 50)
part = np.full(N, -1, dtype=np.int64); part[:600] = np.repeat(np.arange(12), 50)
qa_data = {
    'part_qa_data': {'qa_data_seg': {'functional_part_grounding': [
        {'question': f'Where is part {k}?<SEG>',
         'answer': f'<p>part {k}</p><SEG>', 'object_id': [k]} for k in range(10)]}},
    'object_qa_data': {'qa_data': {'scale_estimation': [
        {'question': f'How wide is object {k}?',
         'answer': f'About {k/10:.1f} meters.', 'object_id': [k]} for k in range(10)]}},
    'multi_turn_qa_data': [
        [{'question': 'What is on the left?',
          'answer': 'A chair.', 'object_id': [0]},
         {'question': 'Point to it.', 'answer': '<p>chair</p><SEG>', 'object_id': [0]}],
    ],
}

out = pipeline(dict(points=LiDARPoints(torch.rand(N, 3) * 4.0),
                    sp_pts_mask=sp, pts_part_mask=part, pts_object_mask=obj,
                    qa_data=qa_data))

M = len(np.unique(sp))
# 不变量检查
print('① 等长:', len(out['selected_qa_data']) == len(out['sp_gt_seg']))
print('② 列数一致:', all(s.shape[1] == M for s in out['sp_gt_seg']))
print('③ 无 <p> 的 QA → 全 NaN 行:',
      all(torch.isnan(s).all() for q, s in zip(out['selected_qa_data'], out['sp_gt_seg'])
          if '<p>' not in q['answer']))
print('④ 弹性副本等长且 points 未动:',
      out['elastic_coords'].shape[0] == N)
for q, s in zip(out['selected_qa_data'], out['sp_gt_seg']):
    print(q.get('qa_keys'), q.get('qa_type'), '| <p>数=', q['answer'].count('<p>'),
          '| GT shape=', tuple(s.shape), '| NaN行=', int(torch.isnan(s).all(dim=1).sum()))
```

**预期结果**（待本地验证）：四项不变量全部为 True；打印表格里每条 QA 的「`<p>` 数 == GT 行数」严格成立（0 个 `<p>` 的 scale_estimation 显示 GT shape `(1, 50)` 且 NaN 行 = 1）；多轮对话的两轮被拆成两条独立 QA，各自 `qa_keys='multi_turn'`。

**延伸思考**：把 `num_qa` 改成 30 再跑一次，观察哪些 QA 被采到——体会 `random.sample` 的无偏性以及 `randomness.seed`（[configs/ground3dlmm_train.py:L224-L227](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L224-L227)）对可复现性的影响。

## 6. 本讲小结

- `AddSuperPointAnnotations_Part3DGlamm` 用 one-hot → `scatter_mean` → `>0.5` 多数投票，把逐点 part/object mask 压成 `(实例数, 超点数)` 的超点 GT 表，并维护原始 ID 参照表；多数投票可能让小实例整行变 0。
- `QA_Generation_v2.flatten_qa_data` 把三层嵌套 QA 展平：单轮逐条打上 `qa_keys/qa_type`，多轮对话在训练版里**拆散成独立单轮**（评估版则保持对话结构），脏数据整批跳过；`transform` 再随机采样 `num_qa` 条。
- **形状契约**是本讲的灵魂：`count_p_tags` 数出的 `<p>` 个数（封顶 2）严格决定 `sp_gt_seg` 每块的行数 `(num_p_tags, M)`；取不到有效 mask 的行用 NaN 占位而不是删除，解码端（NaN 安全注意力）与损失端（isnan 过滤）都认这个约定。
- `get_seg_from_single_qa` 按 `qa_keys` 分派查物体表还是部件表（part 单轮用 `object_id` 查部件表不是 bug），经存在性、越界、全 0 三重兜底，任何不一致最终都以整块 NaN 收场——永不交付错位 GT。
- `ElasticTransfrom` 只生成加噪的体素坐标副本 `elastic_coords`（细+粗两组平滑噪声场），不改点也不改标签，是一种零对齐成本的几何增强，`gran=[6,20]`、`mag=[40,160]`、`p=0.5`。

## 7. 下一步学习建议

本讲结束时，数据侧的全部产物（`points`、`sp_pts_mask`、`selected_qa_data`、`sp_gt_seg`、`elastic_coords`、`scene_name`）都已就绪。下一讲 **u2-l5（打包与预处理）** 讲 `Pack3DDetInputs_Part_Glamm` 如何把这些键分流进 data sample 与 `batch_inputs_dict`。之后再进入 3D 侧（u3 单元）看 `elastic_coords` 如何被 MinkowskiEngine 量化。建议提前浏览两处伏笔：[uniseg3d/formatting.py:L167-L177](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L167-L177)（键的分流注释）与 [uniseg3d/uniseg3d.py:L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1125)（GT 大表的拼接）。
