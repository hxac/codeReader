# 交互式分割的数据侧：点提示生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `point_prompts`、`point_prompt_instance_ids`、`point_prompt_sp_ids` 三个键各自存什么、如何逐条对齐，以及模型侧分别用它们做什么。
2. 读懂训练时的 `PointPromptGeneration`：它如何为每个 GT 实例采样一个「靠近质心」的点（含 500 点子采样策略），以及两套实例 id 体系（训练/评估）在代码中如何被探测与换算。
3. 读懂测试时的 `PointPromptTest`：agile 模式如何用 0.05 体素量化后在「前景—背景」距离矩阵中挑选离背景最远的「最深内部点」。
4. 理解 `pts_instance_objextId_shuffle` 的用途——它是点提示与文本提示跨任务对齐的钥匙。
5. 通过动手实践，把「距质心最近」的选点策略改成「距质心最远」，并用合成点云观察两种策略选出点的差异。

## 2. 前置知识

### 2.1 什么是交互式分割与「点提示」

交互式分割（interactive segmentation）是六类任务中最像「人机对话」的一类：用户在点云中**点击一个点**，模型输出**包含该点的完整物体掩码**。这个点击点就叫**点提示（point prompt）**。SAM（Segment Anything）在 2D 图像上做过同样的事情，UniSeg3D 把它搬到了 3D 点云上。

训练时没有真实用户来点击，所以**数据管线必须「模拟点击」**——为每个 GT 实例自动选一个点当作提示。怎么选这个点，就是本讲两个 transform 的核心逻辑：

- 训练用 `PointPromptGeneration`：选「离实例质心近」的点——稳妥、正样本属性明确；
- 测试用 `PointPromptTest`：选「离背景远」的点——模拟一个经验丰富的用户，点在物体最内部，即使掩码边缘有噪声也绝不点错物体。

### 2.2 你需要带着进来的旧知识（承接前几讲）

| 概念 | 出处 | 本讲如何使用 |
| --- | --- | --- |
| 超点与 `sp_pts_mask` | u1-l1、u3-l3 | 点到超点的映射，形状 (N,)；点提示最终落在某个超点上 |
| `results` 字典在 pipeline 中流转 | u3-l2 | transform 读入 `pts_instance_mask` 等键，写出提示键 |
| 两套实例 id 体系 | u3-l3 | 训练体系：-1 表示背景/stuff，things 从 0 起；评估体系：stuff 为 0/1，things 从 2 起 |
| `pts_instance_objextId` | u3-l3 | `AddSuperPointAnnotations` 产出的 0 起原始 objectId 数组 |
| 键的三站通道（transform→Pack→Preprocessor） | u3-l2 | 提示键必须三站同名才能进入模型 |

### 2.3 两套实例 id 体系（本讲的关键前置）

回顾 u3-l3：`AddSuperPointAnnotations` 会把 `pts_instance_mask` **重映射**成「-1 = 背景/stuff/未标注，0..K-1 = things 实例」的训练 id 体系。同时，若 results 里存在 `eval_ann_info` 键（评估/测试链路），它还会把 things 加上 `num_stuff_cls`（本配置为 2）偏移写入评估标注。

这里有个容易被忽略的源码细节：[uniseg3d/transforms_3d.py:L362-L363](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L362-L363) 用 `.numpy()` 把重映射后的张量写回 `input_dict['pts_instance_mask']`——**CPU 张量的 `.numpy()` 与张量共享内存**。因此当评估分支在 [uniseg3d/transforms_3d.py:L435-L441](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L435-L441) 中原地执行 `pts_instance_mask[...] += num_stuff_cls` 时，`input_dict['pts_instance_mask']` 也随之变成评估 id 体系（stuff 为 0/1，things 从 2 起）。本讲的两个点提示 transform 都用一个 `-2` 分支来把评估 id 还原成 0 起的 things id，后面会看到。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `uniseg3d/transforms_3d.py` | `PointPromptGeneration`（L112-L165） | 训练时为每个 GT 实例采样一个近质心点，产出三个提示键 |
| `uniseg3d/transforms_3d.py` | `PointPromptTest`（L167-L235） | 测试时在体素化点集上选「离背景最远」的点 |
| `uniseg3d/transforms_3d.py` | `AddSuperPointAnnotations` 的 L357、L362-L363、L435-L441 | 产出 `pts_instance_objextId`、重映射 id、评估分支的 +2 偏移 |
| `uniseg3d/uniseg3d.py` | `add_point_prompt_queries`（L685-L744） | 模型侧消费三个提示键，拼装提示查询与提示 GT |
| `uniseg3d/uniseg3d.py` | `split_original_and_point_prompt_queries`（L587-L653） | 把解码输出拆回「原始查询」与「提示查询」 |
| `uniseg3d/uniseg3d.py` | `point_prompt_predict_by_feat`（L20-L39） | 推理时把提示查询的掩码预测映射回原始点 |
| `uniseg3d/formatting.py`、`uniseg3d/data_preprocessor.py` | L15-L18、L73-L80 | 提示键通过 INPUTS_KEYS 分拣、白名单透传进入 `batch_inputs` |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | L182-L184、L235-L237 | 两个 transform 在 train/test pipeline 中的挂载点 |

## 4. 核心概念与源码讲解

### 4.1 三个提示键与模型侧消费的对应关系

#### 4.1.1 概念说明

一条「点提示」听起来只是一个点，但要让它可训练、可评估，数据侧必须同时回答三个问题：

1. **有几个提示？**（决定解码器要多拼几条查询）
2. **每个提示指向哪个 GT 实例？**（决定这条提示查询的监督信号——分类标签和掩码真值）
3. **每个提示点落在哪个超点上？**（决定用什么特征来初始化这条提示查询——「超点即查询」范式下，提示查询就是被点击超点的特征）

于是数据侧产出一组**逐条对齐**（第 k 个元素都描述第 k 条提示）的键：

| 键 | 形状 | 语义 | 模型侧消费点 |
| --- | --- | --- | --- |
| `point_prompts` | (K,)，float64 | 每条提示的点**全局下标**（`np.append` 到空 float 数组导致是浮点型） | 模型只用它的**长度**（`len(cur_point_prompts)`）；下标本身只在数据侧用于算超点 id |
| `point_prompt_instance_ids` | (K,)，int | 每条提示对应的 0 起 GT 实例 id（`select_ids`，被 shuffle 打乱过） | 索引 `labels_3d` / `sp_masks` 切出该提示的分类标签与掩码真值 |
| `point_prompt_sp_ids` | (K,)，int | 每个提示点所在的**超点 id** | 取该超点的特征向量作为提示查询 |
| `pts_instance_objextId_shuffle` | (K,) | 打乱顺序后与提示对齐的**原始 objectId**（ScanNet 全局实例号） | 跨任务对比学习中与 `text_object_id` 匹配（u5-l6 详述） |
| `point_prompt_distance_norms` | — | 预留给距离加权的对比损失 | **当前代码未填充**（见 4.3.1），默认配置下不产生 |

#### 4.1.2 核心流程

一条点提示从生成到被模型消费的全链路：

```text
transform（PointPromptGeneration / PointPromptTest）
    └─ 写入 results：point_prompts / point_prompt_instance_ids / point_prompt_sp_ids /
                     pts_instance_objextId_shuffle
→ Pack3DDetInputs_：三个键在 INPUTS_KEYS 清单里 → 分拣进 data_sample 的 inputs
→ DataLoader：伪批组装（样本浅合并成列表）
→ Det3DDataPreprocessor_：白名单检测到这些键 → 透传进 batch_inputs
→ UniSeg3D.loss() / predict()：
    ├─ add_point_prompt_queries()：按 sp_ids 取超点特征 → 拼进查询列表尾部；
    │   按 instance_ids 切出 labels_3d / sp_masks 组装提示 GT（InstanceData_）
    ├─ decoder 统一前向（interaction_masks 屏蔽组间注意力，u4-l6 详述）
    └─ split_original_and_point_prompt_queries()：按提示数量从尾部切出提示查询的输出
```

注意通道约束（承接 u3-l2）：这三个键在 [uniseg3d/formatting.py:L15-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18) 的 `INPUTS_KEYS` 白名单里，又在 [uniseg3d/data_preprocessor.py:L73-L80](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L80) 的透传名单里——**任何一站改了键名，提示就会在无声中丢失**（Pack 与 Preprocessor 都是「不在就跳过」的静默语义）。

#### 4.1.3 源码精读

模型侧的消费者是 `add_point_prompt_queries`，先看它如何用三个键（前两个参数是提示键批次，第三个 `batch_sp_feat` 是 `extract_feat` 输出的**按场景切分**的超点特征列表）：

**① 按长度遍历、按超点 id 取特征**（[uniseg3d/uniseg3d.py:L696-L709](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L696-L709)）：外层循环 `for i, cur_point_prompts in enumerate(batch_point_prompts)` 遍历场景，内层 `for j in range(len(cur_point_prompts))` 遍历该场景的每条提示——这就是「模型只用 `point_prompts` 的长度」的落点。`cur_point_prompt_feat = batch_sp_feat[i][sp_ids:sp_ids+1]` 用 `point_prompt_sp_ids` 切出 (1, C) 的超点特征行，作为这条提示查询的初始特征；`is_type_embedding=True`（本配置 L34 打开）时再叠加类型嵌入 `type_embedding.weight[1]`（索引 0 是文本提示、1 是点提示、2 是原始查询，见 L690-L691 对原始查询加 weight[2]）。

**② 按实例 id 切出监督真值**（[uniseg3d/uniseg3d.py:L710-L716](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L710-L716)）：`labels_3d[instance_ids:instance_ids+1]` 与 `sp_masks[instance_ids:instance_ids+1]` 各切出一行——第 k 条提示的分类标签与超点掩码真值，恰好来自第 k 条提示指向的 GT 实例。**这就是三个键必须逐条对齐的原因**：`point_prompts` 的第 k 个点若不属于 `point_prompt_instance_ids` 的第 k 个实例，监督信号就从根上错了。

**③ 拼接与提示 GT 组装**（[uniseg3d/uniseg3d.py:L718-L735](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L718-L735)）：提示查询 `torch.cat` 到该场景查询列表**尾部**（尾部追加决定了之后能按数量从尾部拆分）；同时把逐条收集的 `labels_3d`、`sp_masks` 拼成 `point_prompt_gt_instances[i]` 的字段，并把 `pts_instance_objextId_shuffle` 存为 `point_object_id` 字段——供 `ContrastiveCriterion` 用 `point_object_id == text_object_id` 找同一物体的「文本-点」正样本对（[uniseg3d/contrastive_criterion.py:L68-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L68-L71)），也被 `OVInstanceCriterion` 用 `argsort` 恢复打乱前的顺序（[uniseg3d/instance_criterion.py:L407-L415](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L407-L415)）。

**④ 注入与拆分时机**（[uniseg3d/uniseg3d.py:L465-L466](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L466) 与 [uniseg3d/uniseg3d.py:L490-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L490-L491)）：训练前向中，只要 `batch_inputs_dict` 里有 `point_prompts` 键（`if 'point_prompts' in batch_inputs_dict.keys()`），就在解码器**前**注入提示查询、解码器**后**按提示数量把输出拆成 `original_outputs` 与 `point_prompt_outputs` 两份（拆分函数 `split_original_and_point_prompt_queries` 在 [uniseg3d/uniseg3d.py:L616-L632](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L616-L632) 对每个输出键做 `[-n:]` / `[:-n]` 切片）。推理路径同理（[uniseg3d/uniseg3d.py:L796-L797](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L796-L797)、[uniseg3d/uniseg3d.py:L822-L824](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L822-L824)），最终由 [uniseg3d/uniseg3d.py:L20-L39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L20-L39) 的 `point_prompt_predict_by_feat` 把提示查询的掩码预测经 `superpoints` 索引映射回原始点，写入 `data_sample.point_prompt_pred_pts_seg` 与 `point_prob`（[uniseg3d/uniseg3d.py:L879-L884](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L879-L884)）交给评估器（u6-l2 详述）。

一个值得注意的坐标系细节：`loss()`/`predict()` 里会先给每个场景的 `sp_pts_mask` 累加 `superpoint_bias` 再横向拼接成全局超点表（[uniseg3d/uniseg3d.py:L439-L447](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L439-L447)），但 `extract_feat` 返回的 `x` 是**按场景切回局部下标**的特征列表；`add_point_prompt_queries` 用的是局部超点 id 索引 `batch_sp_feat[i]`，而数据侧的 `point_prompt_sp_ids` 恰好也是 transform 阶段（未加偏移）的局部 id——两侧约定一致，索引才不会错位。

#### 4.1.4 代码实践：验证三个键的对齐不变式

**实践目标**：用合成数据跑一遍 `PointPromptGeneration`，用程序验证三个键满足对齐不变式，把「逐条对齐」从口头结论变成可执行断言。

**操作步骤**（在完成 u1-l2 环境安装的 conda 环境中、仓库根目录下运行；以下为**示例代码**，非项目原有代码）：

```python
# align_check.py —— 放在仓库外任意目录，例如 /tmp/u3l4/align_check.py
import numpy as np
from uniseg3d.transforms_3d import PointPromptGeneration  # 需要 PYTHONPATH 指向仓库根目录

np.random.seed(0)
N = 600
results = {
    # 两团高斯点云各 250 点 + 100 个背景点；XYZ+RGB 六列，points 用 ndarray 即可
    'points': np.random.randn(N, 6).astype(np.float32),
    'pts_instance_mask': np.concatenate([
        np.zeros(250, dtype=np.int64),       # 实例 0（训练 id 体系）
        np.ones(250, dtype=np.int64),        # 实例 1
        -np.ones(100, dtype=np.int64)]),     # 背景 -1
    'sp_pts_mask': np.arange(N) % 8,         # 8 个超点轮流覆盖 600 个点
    'pts_instance_objextId': np.array([17, 42]),  # 假装原始 objectId
}
# 让两团点云在空间上分开，实例 1 平移 5 米
results['points'][:250, :3] += 0.0
results['points'][250:500, :3] += 5.0

out = PointPromptGeneration(samplePoint=True).transform(results)
pp   = out['point_prompts'].astype(int)          # (K,) 点下标
pids = out['point_prompt_instance_ids']          # (K,) 实例 id
spid = out['point_prompt_sp_ids']                # (K,) 超点 id
oid  = out['pts_instance_objextId_shuffle']      # (K,) 原始 objectId

# 三条对齐不变式
assert len(pp) == len(pids) == len(spid) == len(oid)
for k in range(len(pp)):
    assert out['pts_instance_mask'][pp[k]] == pids[k], '点必须落在声明的实例上'
    assert out['sp_pts_mask'][pp[k]] == spid[k], '超点 id 必须来自该点的超点'
    assert oid[k] == out['pts_instance_objextId'][pids[k]], 'objectId 必须按实例 id 对齐'
print('对齐不变式全部通过；K =', len(pp))
```

运行：`PYTHONPATH=/path/to/UniSeg3D python align_check.py`。

**需要观察的现象**：断言全部通过；`pids` 的顺序（例如 `[1, 0]` 或 `[0, 1]`）每次运行可能不同——这是 shuffle 的效果；`pp` 的具体下标因随机子采样也会变化。

**预期结果**：不变式断言通过，`K = 2`（两个实例各生成一条提示）。若把 `pts_instance_mask` 里某点改成与 `pids` 不一致的值，第二条断言应立即失败——这正说明监督对齐完全依赖这三个键的一致性。（待本地验证：依赖 u1-l2 环境中 mmcv/mmdet3d/MinkowskiEngine 可正常 import。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Pack3DDetInputs_` 配置里的 `'point_prompt_sp_ids'` 键删掉（其余不动），训练还能正常跑吗？会发生什么？

**答案**：`Pack3DDetInputs_` 按 `if key in results` 分拣键（[uniseg3d/formatting.py:L117-L120](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L117-L120)），删掉配置键只是**静默不打包**；`Det3DDataPreprocessor_` 同样按「不在就跳过」处理，于是 `batch_inputs_dict` 里没有 `point_prompt_sp_ids`，而 `add_point_prompt_queries` 在 [uniseg3d/uniseg3d.py:L688](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L688) 直接下标访问会抛 `KeyError`——不会静默出错，但报错位置在模型侧而非数据侧，排查成本高。这正是 u3-l2 讲的「三站键名必须一字不差」。

**练习 2**：`point_prompts` 里存的是点下标，模型为什么不直接用它去索引点特征，而要绕道 `point_prompt_sp_ids` 取**超点**特征？

**答案**：UniSeg3D 的查询空间是超点空间（「超点即查询」，u4-l1）：解码器的所有查询（原始查询、点提示查询、文本提示查询）必须在同一特征空间里做自注意力与掩码预测。单点的原始特征维度与语义层级都和解码器查询不一致；取被点击超点在 `extract_feat` 聚合后的特征，天然与常规查询同构，直接 `torch.cat` 即可参与统一 Transformer。同时超点特征聚合了局部邻域上下文，比单点特征更稳。

**练习 3**：`pts_instance_objextId_shuffle` 里的 `point_object_id` 字段最终被谁消费？

**答案**：主要两个消费者：① `ContrastiveCriterion`（[uniseg3d/contrastive_criterion.py:L68-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L68-L71)）用 `point_object_id == text_object_id` 找同一物体的文本提示做跨任务对比学习的正样本对；② `OVInstanceCriterion`（[uniseg3d/instance_criterion.py:L407-L415](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L407-L415)）用 `point_prompt_instance_ids` 的 `argsort` 恢复打乱前与 GT 行的对应关系。详见 u5-l5/u5-l6。

### 4.2 训练侧采样流程：PointPromptGeneration

#### 4.2.1 概念说明

`PointPromptGeneration` 回答的问题是：**训练时如何替用户「点击」？**

它的三个设计决定：

1. **全实例提示**：`select_ids` 取全部 things 实例 id（不是子集），即每个训练迭代中，场景里每个 GT 实例都会得到一条点提示，只是**顺序随机打乱**（`np.random.shuffle`）。乱序有两重意义：避免模型记住固定的实例排列；同时乱序结果被记录进 `point_prompt_instance_ids`，下游可以用 `argsort` 恢复（见练习 3）。
2. **近质心选点**（`samplePoint=True`，本配置默认开启）：在实例点集中选一个**靠近实例质心**的点。直觉：质心附近必然在物体内部，是「最不会点错」的正样本点。
3. **500 点子采样**：大实例（ScanNet 里一张床、一面墙可能有上万个点）先无放回随机抽至多 500 个点，再在子集上算质心与距离。这是纯计算量的削减——排序的复杂度由 \(O(n \log n)\) 中的 \(n\) 决定，500 的上限把每实例开销封顶。

#### 4.2.2 核心流程

对每个（打乱后的）实例 id `select_id`：

```text
1. pts_select_id = 实例 select_id 的全部点下标
2. 若 samplePoint=True：
   a. 取这些点的 XYZ
   b. 子采样：若 n > 500，采样率 r = 500/n，否则 r = 1.0；
      采样数 m = int(n * r)，无放回随机选 m 个点
   c. 质心：c̄ = (1/m) Σ p_i
   d. 每点到质心的欧氏距离：d_i = ||p_i - c̄||_2
   e. 升序排序后取第 1 个（距离最小）→ 还原成全局点下标
   否则：从实例点里均匀随机选 1 个点
3. 追加进 point_prompts；记录 select_ids / 对应超点 id / 对应 objectId
```

写成公式，训练策略选中的点是子采样集 \(S\) 中到质心距离最小者：

\[
i^{*} = \arg\min_{i \in S} \left\| p_i - \frac{1}{|S|}\sum_{j \in S} p_j \right\|_2
\]

另外，入口处有一段**id 体系探测**（这是本模块最绕的一段）：

```text
若 results 中存在 'eval_ann_info'（评估 id 体系，things 从 2 起）：
    pts_instance_mask -= 2，实例 id 集合 -= 2，再删去 < 0 的 → 还原为 0 起 things id
否则（训练 id 体系，-1 为背景）：
    直接从唯一 id 集合中删去 -1
两条分支殊途同归：select_ids 都是 0..K-1 的 things 实例 id
```

之所以需要 `-2`：如 2.3 节所述，评估链路中 `AddSuperPointAnnotations` 的评估分支会把 `input_dict['pts_instance_mask']` 原地改成「stuff 0/1、things 从 2 起」的体系，`-2` 恰好把 things 平移回 0 起、把 stuff（0/1 → -2/-1）和背景（-1 → -3）一起挤进负数区被删掉。

#### 4.2.3 源码精读

**① id 体系探测分支**（[uniseg3d/transforms_3d.py:L118-L128](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L118-L128)）：先取 `sp_pts_mask`（L118，之后用于把提示点换算成超点 id），再复制 `pts_instance_mask` 并 `np.unique` 出实例 id 集合。L122-L126 是评估分支（`-2` 偏移后删负），L127-L128 是训练分支（只删 `-1`）。

**② 全实例乱序**（[uniseg3d/transforms_3d.py:L133-L137](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L133-L137)）：`select_ids = pts_instance_id.copy()` 后 `np.random.shuffle(select_ids)`——注意打乱的是「实例访问顺序」，每个实例仍然恰好被提示一次；`pts_select_id = np.where(pts_instance_mask == select_id)[0]` 取该实例全部点下标。

**③ 近质心选点**（[uniseg3d/transforms_3d.py:L138-L153](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L138-L153)）：

```python
instance_points = input_dict['points'][pts_select_id][:, :3]   # 该实例的 XYZ
if len(instance_points) > 500:
    sample_ratio = 500.0 / len(instance_points)
else:
    sample_ratio = 1.0
sample_size = int(len(instance_points) * sample_ratio)
sample_indices = np.random.choice(len(instance_points), sample_size, replace=False)
sampled_points = instance_points[sample_indices]
centroid = np.mean(sampled_points, axis=0)
distances = np.linalg.norm(sampled_points - centroid, axis=1)
distance_order = np.argsort(distances)
ordered_pts_select_id = pts_select_id[sample_indices][distance_order[0]]  # ← 距质心最近
```

两级索引链 `pts_select_id[sample_indices][distance_order[0]]` 值得拆开看：`pts_select_id` 是实例内点的**全局下标**；`sample_indices` 是子采样在**实例内**的行号；`distance_order[0]` 是子采样集中离质心最近者的行号。三者串联即「子采样集里最近质心那个点」的全局下标。L154-L156 是 `samplePoint=False` 的退化分支：均匀随机选一个点。

**④ 写出四个键**（[uniseg3d/transforms_3d.py:L160-L163](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L160-L163)）：`pts_instance_objextId_shuffle = pts_instance_objextId[select_ids]`——按打乱后的提示顺序取原始 objectId；`point_prompts`（点下标，float64）；`point_prompt_instance_ids = select_ids`；`point_prompt_sp_ids = sp_pts_mask[list(point_prompts.astype(int))]`——把每个提示点经 `sp_pts_mask` 映射到超点 id。注意此处的 `astype(int)`：因为 `point_prompts` 是用 `np.append` 追加到空 float 数组生成的，天然是 float64，消费前必须转整型。

**⑤ 管线挂载位置**（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L177-L190](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L177-L190)）：`PointPromptGeneration(samplePoint=True)` 排在 `AddSuperPointAnnotations` **之后**（依赖它产出的重映射 id 与 `pts_instance_objextId`）、`ElasticTransfrom` **之前**。因此选点用的是**弹性形变前**但已过 `GlobalRotScaleTrans` 旋转缩放的坐标——距离在增广坐标系下计算，但由于只输出点**下标**（不输出坐标），增广不影响后续任何环节。

#### 4.2.4 代码实践：把「距质心最近」改成「距质心最远」

**实践目标**：亲手改一行采样策略，用合成两实例点云对比两种策略选出点的位置差异，直观理解「选点策略」这一超参数对提示质量的影响。

**操作步骤**：

1. 构造合成场景（沿用 4.1.4 的脚本骨架）：两个实例各 250 点（实例 0 在原点附近、实例 1 平移 5 米），100 个背景点，`sp_pts_mask = arange(N) % 8`。为了让「最近/最远」差异明显，可把高斯标准差调大（如 1.0），让实例呈扁长形。
2. 运行原始策略，记录每条提示的 `(点下标, 所属实例, 该点到本实例质心的距离)`。
3. **临时修改源码**：把 [uniseg3d/transforms_3d.py:L152](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L152) 的

   ```python
   ordered_pts_select_id = pts_select_id[sample_indices][distance_order[0]]
   ```

   改为

   ```python
   ordered_pts_select_id = pts_select_id[sample_indices][distance_order[-1]]
   ```

   即把升序排序的第 1 个（最近）换成最后 1 个（最远）。
4. 再跑一次同样的脚本，记录同样的三元组。
5. **务必还原**：`git checkout -- uniseg3d/transforms_3d.py`（本教程规定不得改动源码，实验后立即恢复）。

对比脚本（**示例代码**）：

```python
# far_vs_near.py
import numpy as np
from uniseg3d.transforms_3d import PointPromptGeneration

np.random.seed(7)
N, per = 600, 250
inst = np.concatenate([np.zeros(per, np.int64), np.ones(per, np.int64), -np.ones(100, np.int64)])
pts = np.zeros((N, 6), np.float32)
pts[:per, :3]   = np.random.randn(per, 3) * np.array([1.0, 0.2, 0.2])  # 扁长实例 0
pts[per:2*per, :3] = np.random.randn(per, 3) * np.array([1.0, 0.2, 0.2]) + 5.0  # 实例 1
results = {'points': pts, 'pts_instance_mask': inst,
           'sp_pts_mask': np.arange(N) % 8,
           'pts_instance_objextId': np.array([3, 9])}

out = PointPromptGeneration(samplePoint=True).transform(results)
for k in range(len(out['point_prompts'])):
    idx  = int(out['point_prompts'][k])
    pid  = out['point_prompt_instance_ids'][k]
    centroid = pts[inst == pid, :3].mean(axis=0)
    d = np.linalg.norm(pts[idx, :3] - centroid)
    print(f'提示{k}: 点下标={idx}, 实例={pid}, 超点={out["point_prompt_sp_ids"][k]}, '
          f'到本实例质心距离={d:.3f}')
```

**需要观察的现象**：同一种子下，修改前后每个实例的提示点都仍落在正确实例内部（可用 `inst[idx] == pid` 断言）；但「最远」策略选出的点到本实例质心的距离显著大于「最近」策略（理想情况下接近该实例的空间半径上限），且「最远」点常出现在扁长实例的两端边缘，「最近」点聚集在质心附近。

**预期结果**：两张策略各自输出 2 条提示；`到本实例质心距离` 一列在修改后明显增大（例如从 ~0.2m 量级升到 ~1.0m 量级，取决于标准差设置）；`point_prompt_instance_ids` 的顺序两次运行可能不同（shuffle 仍在起作用）。进一步思考：最远点往往贴近实例边界，一旦 GT 掩码边缘有标注噪声，它比近质心点更容易「点到物体外面」——这正是训练策略选择近质心点的原因。（待本地验证：需 u1-l2 环境；具体数值依赖随机种子与实例形状。）

#### 4.2.5 小练习与答案

**练习 1**：`sample_ratio` 的计算为什么要写成 `if len > 500: 500.0/len else: 1.0`，然后 `sample_size = int(len * sample_ratio)`？直接 `min(len, 500)` 有什么不同？

**答案**：两者在「采样数」上等价（`len > 500` 时 `int(len * 500/len) = 500`；否则 `int(len * 1.0) = len`），但代码选择先算比例再乘回去，语义是「按比例下采样、至多 500 点」。写成 `min(len, 500)` 更直白且避免浮点截断的边界风险（极端情况下 `int(len * ratio)` 可能因浮点误差得到 499）。这是等价但更稳妥的写法，读者重构时应优先后者。

**练习 2**：训练时为什么给**所有**实例都生成提示，而不是每个迭代随机挑几个？

**答案**：点提示分支的监督是一对一的（每条提示查询直接对着自己实例的 `labels_3d` 与 `sp_masks`，无需匹配器，见 u5-l5），全实例提示让每个迭代该分支的监督覆盖场景中全部物体，信号最密集；同时提示查询与常规查询在同一解码器里前向，多几条查询的边际计算开销很小。此外跨任务对比学习（u5-l6）需要同一物体「文本提示 + 点提示」成对出现——ScanRefer 只覆盖部分物体，点提示全量生成可以保证凡有文本描述的物体必有点提示与之配对。

**练习 3**：`np.random.shuffle(select_ids)` 打乱的是实例顺序。如果去掉这个 shuffle，`pts_instance_objextId_shuffle` 与 `point_prompt_instance_ids` 会变成什么？有影响吗？

**答案**：会变成恒等的 `0..K-1` 顺序。功能上仍能正确对齐（两个键仍然逐条一致），但提示查询的排列将固定为「按实例编号升序」，模型可能在逐层自注意力中对固定排列产生顺序依赖，降低泛化；同时 [uniseg3d/instance_criterion.py:L409](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L409) 的 `argsort` 恢复逻辑依旧成立（恒等映射的 argsort 还是恒等）。所以 shuffle 是廉价的数据增广，不是正确性必需。

### 4.3 测试侧采样：PointPromptTest 的 agile 模式

#### 4.3.1 概念说明

测试时（`PointPromptTest`）要模拟的不再是「随机用户」，而是「**最理想的一次点击**」：评估交互式分割的通行做法是替用户在每个 GT 实例上点一个**保证在物体内部**的点，再看模型能否还原整个物体。点错了（点到背景或别的物体）对模型不公平，点得太靠边（掩码边缘噪声区）也不稳妥。

agile 模式的策略因此变为：**在前景（该实例）与背景（其余全部点）之间，选一个离背景尽可能远的点**——即物体的「最深内部点」。它与训练策略的哲学差异：

| | 训练 `PointPromptGeneration` | 测试 `PointPromptTest`（agile） |
| --- | --- | --- |
| 目标点 | 离**本实例质心**最近 | 离**背景点**最远 |
| 计算依据 | 实例自身的点集 | 前景/背景二分 + 跨集合距离 |
| 预处理 | ≤500 点随机子采样 | 0.05 体素量化去重 |
| 输入前提 | 需要实例级分组（算质心） | 只需该实例的二值掩码（fg/bg） |
| 直觉 | 「点在物体中心最安全」 | 「离边界越远越不可能是误点」 |

两个工程细节先打预防针：构造参数里的 `max_num_point`（配置传了 3）、`samplePoint`、`size_file` 在 agile 路径中**均未被使用**；`is_distance=True` 时会写出键 `point_prompt_distance_norms`，但该数组从 L200 初始化后**从未被追加**，恒为空数组——这是留给距离加权对比损失的预留接口，当前默认配置（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L235-L237](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L235-L237) 只传 `max_num_point=3`，其余默认）下不会产生这个键。读代码时不要在这些死参数上浪费时间。

#### 4.3.2 核心流程

```text
1. 体素量化：对全部原始坐标按 quantization_size=0.05 做稀疏量化，
   得到每个体素的代表点下标 unique_map（体素 → 首个落入点）
2. 对每个实例 id select_id：
   a. gt_inst = (pts_instance_mask == select_id) 的 0/1 向量
   b. 投影到体素代表点：me_gt_inst = gt_inst[unique_map]
      （前景体素集合 F、背景体素集合 B）
   c. 若 F 为空 → 跳过该实例（不计入提示）
   d. pairwise = cdist(B, F)         # |B| × |F| 距离矩阵
      d = min_over_B(pairwise)       # 每个前景体素到最近背景体素的距离
      i* = argmax_F(d)               # 离背景最远的前景体素
   e. 经 unique_map 还原为全局点下标，断言该点确属前景
3. 写出三个键 + pts_instance_objextId_shuffle（与训练侧完全同名同义）
```

公式化：设量化后的前景体素集合为 \(F\)、背景体素集合为 \(B\)，选中的体素是

\[
v^{*} = \arg\max_{v \in F}\; \min_{b \in B} \|v - b\|_2
\]

这个「max-min」结构与训练侧的「min 到质心」形成对照：质心要求实例形状凸性良好（凹形物体如椅子，质心可能悬在空腔里，但选的是**离质心最近的实际点**，所以并不出错），agile 则完全不关心形状，只关心「离异类多远」。体素量化有两重作用：去掉近乎重复的点（ScanNet 单场景十几万点，直接 cdist 是 \(O(|B||F|)\) 的平方级开销）；与模型输入的稀疏卷积粒度（`voxel_size=0.02`，同一量级）对齐，选出的点在体素意义上就是「一个位置」。

#### 4.3.3 源码精读

**① 0.05 体素量化**（[uniseg3d/transforms_3d.py:L180-L185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L180-L185)）：`raw_coords = input_dict['points'].coord.contiguous().clone()` 取原始 XYZ（注意这里是测试管线，`points` 尚未经任何几何增广）；`ME.utils.sparse_quantize(coordinates=raw_coords, quantization_size=0.05, return_index=True, return_inverse=True)` 返回 `coords_qv`（去重后的体素坐标）、`unique_map`（每个体素首个落入点的下标）、`inverse_map`（每点所属体素）。后续只用 `unique_map`。

**② 同款 id 体系探测**（[uniseg3d/transforms_3d.py:L188-L196](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L188-L196)）：与 `PointPromptGeneration` 的 L120-L128 逐行相同——测试管线中 `eval_ann_info` 存在，走 `-2` 偏移分支把评估 id 还原为 0 起 things id（原理见 2.3 与 4.2.2）。

**③ agile 主循环**（[uniseg3d/transforms_3d.py:L203-L221](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L203-L221)）：

```python
gt_inst = (pts_instance_mask == select_id).astype(np.int32)   # 点级 0/1
me_coord = raw_coords[unique_map]        # 每体素一个代表坐标
me_gt_inst = gt_inst[unique_map]         # 每体素一个 0/1 标签
valid_index = torch.where(torch.as_tensor(me_gt_inst))[0]
zero_indices = (me_gt_inst == 0)         # 背景体素
one_indices = (me_gt_inst == 1)          # 前景体素
if one_indices.sum() == 0:
    continue                             # 空前景实例直接跳过
pairwise_distances = torch.cdist(me_coord[zero_indices, :], me_coord[one_indices, :])
pairwise_distances, _ = torch.min(pairwise_distances, dim=0)   # 每个前景体素 → 最近背景距离
me_index = valid_index[torch.argmax(pairwise_distances)]       # 离背景最远的前景体素
global_index = unique_map[me_index].numpy().astype(np.int32)
assert gt_inst[global_index] == 1        # 保险：选出的点确属该实例
point_prompts = np.append(point_prompts, global_index)
select_ids_filter = np.append(select_ids_filter, select_id)
```

四个关键点：`torch.cdist` 的输出形状是 `(|B|, |F|)`，`torch.min(..., dim=0)` 对背景维取 min，得到每个前景体素的「到最近背景体素距离」；`argmax` 再取其中最大者，完成 max-min；被跳过的实例**只进入 `select_ids` 不进入 `select_ids_filter`**，因此最终输出的 `point_prompt_instance_ids` 用的是 `select_ids_filter`（L230），保证三个键仍然逐条对齐；`assert` 是一道面向未来的防线——量化与索引链任何一环出错都会在这里暴露。

**④ 输出与遗留开关**（[uniseg3d/transforms_3d.py:L224-L233](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L224-L233)）：L224-L227 的 `if len(select_ids_filter)==0/==1: pass` 是空操作占位（无任何效果）；L228-L231 写出与训练侧**完全同名**的四个键，保证模型侧 `add_point_prompt_queries` 无需区分训练/测试；L232-L233 仅当 `is_distance=True` 时写出（恒空的）`point_prompt_distance_norms`。

**⑤ 测试管线挂载**（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L217-L243](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L217-L243)）：`PointPromptTest(max_num_point=3)` 位于 `MultiScaleFlipAug3D` 内部、紧跟 `AddSuperPointAnnotations`（注意测试侧该 transform 不开 `merge_ov`，没有 SAM3D 伪掩码）与 `TextPromptTest` 之后，随后 `Pack3DDetInputs_` 打包的键集合与训练侧一致——**数据侧的键名约定是训练/测试共用的**，这就是一次前向能同时产出常规五任务结果与提示任务结果的接口基础。

#### 4.3.4 代码实践：验证 agile 选点的「最深内部」性质

**实践目标**：在合成两实例点云上运行 `PointPromptTest`，用数值验证选出的点确实比随机实例点离背景更远。

**操作步骤**（**示例代码**；`points` 需用 mmdet3d 的 `LiDARPoints` 包装，因为 L180 访问 `.coord` 属性）：

```python
# agile_check.py
import numpy as np
import torch
from mmdet3d.structures import LiDARPoints
from uniseg3d.transforms_3d import PointPromptTest

np.random.seed(1)
per, bg = 400, 200
N = per * 2 + bg
xyz = np.zeros((N, 3), np.float32)
xyz[:per]          = np.random.randn(per, 3) * 0.5            # 实例 0：半径~1m 团块
xyz[per:per*2]     = np.random.randn(per, 3) * 0.5 + 4.0      # 实例 1：平移 4m
xyz[per*2:]        = np.random.randn(bg, 3) * 3.0 + 8.0       # 远处背景
pts6 = np.hstack([xyz, np.zeros((N, 3), np.float32)])

results = {
    'points': LiDARPoints(torch.from_numpy(pts6), points_dim=6),
    'pts_instance_mask': np.concatenate([
        np.zeros(per, np.int64), np.ones(per, np.int64), -np.ones(bg, np.int64)]),
    'sp_pts_mask': np.arange(N) % 16,
    'pts_instance_objextId': np.array([5, 6]),
}
out = PointPromptTest(mode='agile').transform(results)
xyz_t = torch.from_numpy(xyz)
for k in range(len(out['point_prompts'])):
    idx = int(out['point_prompts'][k]); pid = out['point_prompt_instance_ids'][k]
    fg = xyz_t[results['pts_instance_mask'] == pid]
    bgd = xyz_t[results['pts_instance_mask'] != pid]
    d_chosen  = torch.cdist(xyz_t[idx:idx+1], bgd).min()
    d_others  = torch.cdist(fg, bgd).min(dim=1).values   # 每个实例点到背景的最近距离
    print(f'实例{pid}: 选中点到背景 {d_chosen:.3f} m | 实例内该距离的均值 {d_others.mean():.3f} m')
    assert results['pts_instance_mask'][idx] == pid
```

运行：`PYTHONPATH=/path/to/UniSeg3D python agile_check.py`。

**需要观察的现象**：两个实例各产出一条提示；选中点的「到背景最近距离」明显大于该实例所有点到背景最近距离的均值——它是实例内的深度极大值点。

**预期结果**：断言通过；两个实例的 `d_chosen` 均显著大于对应均值（例如团块场景下约为均值的 2 倍上下，具体数值依赖随机种子与尺度）。若把两实例拉近到几乎相接，`d_chosen` 会急剧下降——因为对方实例成了「背景」，最内点被挤压到远离接触面的一侧。（待本地验证：需 u1-l2 环境且 MinkowskiEngine 可用；`quantization_size=0.05` 要求实例尺度远大于 5cm，本例半径 0.5m 满足。）

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在原始点集上做 cdist，而要先 0.05 体素量化？

**答案**：两个原因。① 计算量：ScanNet 单场景点数常在十万级，背景-前景距离矩阵大小为 \(|B| \times |F|\)，量化去重能把两侧集合各压缩一个数量级以上；② 冗余：同一位置的成百个重复点到背景的距离完全相同，参与计算不改变 argmax 的结果。量化取「每体素首个点」作代表，还与模型侧稀疏卷积「一个体素一个特征」的粒度在直觉上对齐。

**练习 2**：`select_ids_filter` 与 `select_ids` 有什么区别？为什么输出键用前者？

**答案**：`select_ids` 是全部候选实例 id；`select_ids_filter` 只包含**实际产出了提示点**的实例——某实例在 0.05 量化后可能没有任何前景体素（极小实例被量化「吞掉」），L213-L214 会 `continue` 跳过它。三个输出键的语义是「第 k 条提示」的属性，空前景实例没有提示，必须剔除才能维持 `point_prompts` / `point_prompt_instance_ids` / `point_prompt_sp_ids` 的逐条对齐（L228-L231 全部使用 `select_ids_filter`）。

**练习 3**：训练选「近质心」、测试选「离背景最远」，两者选出的点通常都在物体内部。这种「训练-测试策略不一致」有没有风险？

**答案**：两者的共同保证是「点在物体内部且远离边界」，提示查询的语义（属于哪个物体）在两侧一致，所以不一致本身不构成分布漂移的主要风险——点提示查询的特征来自被点超点，两侧的超点特征分布是同一套骨干产出的。真正的差异是：近质心点多位于物体「语义核心区」（如桌面中心），最内点则偏向几何上离边界最远的厚实处（如沙发的坐垫中央）。若训练只见过核心区提示而测试点击落在物体边缘薄壁处（评估协议之外的真实用户可能这样点），泛化会打折扣。要验证这一点，可以在验证集上把 `PointPromptTest` 的选点改为随机实例点（模拟粗糙用户），对比交互式分割 AP 的降幅——这是一个很好的二次开发实验。

## 5. 综合实践

**任务：在同一个合成场景上并排评测两种「模拟点击」策略，产出一张对照报告。**

1. **场景构造**：两个形状不同质的实例——一个凸的实心球状团块、一个凹的马蹄形（用半圆环采样模拟）——外加远处背景点，全部配上 `pts_instance_mask`（0/1/-1）、`sp_pts_mask`、`pts_instance_objextId`（凹形实例能暴露「质心悬空」的讨论点：马蹄形质心落在空腔里，但算法选的是**离质心最近的实际点**，并非质心本身）。
2. **跑训练策略**：`PointPromptGeneration(samplePoint=True)`，记录每条提示的点坐标、所属实例、到本实例质心的距离、到背景的最近距离。
3. **跑测试策略**：`PointPromptTest(mode='agile')`，记录同样的四列（提示点用 `point_prompts` 下标从 `xyz` 取坐标）。
4. **对照分析**：
   - 两种策略各自选出的点在空间上位于物体的什么部位？（用 matplotlib 3D 散点画出实例点云 + 两种策略的选中点）
   - 对凹形实例，「到质心距离」与「到背景距离」两种度量选中的点差多远？
   - 三个提示键在两种策略下是否都满足 4.1.4 的对齐不变式？
5. **结论沉淀**：用一段话回答——如果你要为本项目新增「框提示」（box prompt）任务（u6-l4 的二次开发方向），训练与测试各应如何模拟「理想框提示」？从本讲得到的启示是：**训练策略追求稳妥正样本，测试策略追求无歧义评估协议**，两者的模拟目标不同。

预期产物：一张四列表格 + 一张散点图 + 一段设计论述。全程不需要 GPU 与真实数据（待本地验证：依赖 u1-l2 环境的 mmdet3d / MinkowskiEngine / torch_scatter 可 import）。

## 6. 本讲小结

- 一条点提示由三个**逐条对齐**的键描述：`point_prompts`（点下标，模型只用其长度）、`point_prompt_instance_ids`（0 起实例 id，用于切监督真值）、`point_prompt_sp_ids`（超点 id，用于取超点特征作提示查询）；外加跨任务对齐用的 `pts_instance_objextId_shuffle`。
- 模型侧 `add_point_prompt_queries` 把提示查询拼到查询列表尾部、按 `instance_ids` 切出提示 GT，解码后 `split_original_and_point_prompt_queries` 按提示数量从尾部拆分输出——训练/测试共用同一套键名与消费逻辑。
- 训练侧 `PointPromptGeneration`：全实例、乱序、≤500 点子采样后取**离质心最近**的点；入口处用 `eval_ann_info` 是否存在探测 id 体系，两分支（`-2` 偏移 vs 删 `-1`）殊途同归得到 0 起 things id。
- 测试侧 `PointPromptTest` agile 模式：0.05 体素量化后，对每实例算前景到背景的最近距离矩阵，argmax 取**离背景最远**的「最深内部点」；空前景实例被 `select_ids_filter` 剔除以维持对齐。
- 两个工程提醒：`point_prompts` 是 float64（`np.append` 副作用），消费前须 `astype(int)`；`max_num_point` / `samplePoint` / `size_file` / `point_prompt_distance_norms` 在 agile 路径中是未启用的预留参数，阅读时可直接跳过。

## 7. 下一步学习建议

- **下一讲 u3-l5（文本提示生成）**：与点提示完全平行的姊妹篇——`TextPromptGeneration` / `TextPromptTest` 如何从 ScanRefer 描述构造 `text_token`、`gt_text_prompt`、`text_object_id`，读完你会看到「提示 = 数据侧三键 + 模型侧注入/拆分」是本项目的通用范式。
- **u4-l6（提示查询注入与结果拆分）**：深入 `interaction_masks` 如何屏蔽原始查询与提示查询之间的自注意力、`type_embedding` 三类向量如何区分查询来源——本讲只触及了注入函数，那里才是解码器内部的完整故事。
- **u5-l5 / u5-l6**：点提示的一对一损失（为什么不需要匹配器），以及 `point_object_id` 与 `text_object_id` 如何在对比学习中配成正样本对——本讲种下的伏笔在那两讲兑现。
- 建议顺带精读 [uniseg3d/data_preprocessor.py:L73-L101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L101)，把点提示与文本提示全部键的透传清单对照一遍，巩固「三站键通道」的心智模型。
