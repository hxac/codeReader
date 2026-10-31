# 提示查询注入与结果拆分

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `add_point_prompt_queries` / `add_text_prompt_queries` 如何把点提示特征与文本嵌入拼进查询列表尾部，以及 3 维 `type_embedding`（一个 `Embedding(3, 32)`）如何标记三种查询来源。
2. 逐格写出 `interaction_masks` 四个区域的布尔取值，解释 `set_query_mask` 与 `set_all_mask` 两个开关各控制哪几块，并说明它如何被 `nn.MultiheadAttention` 的 `attn_mask` 消费。
3. 解释 `split_original_and_text_prompt_queries` / `split_original_and_point_prompt_queries` 的「从尾部链式拆分」逻辑，以及 `mode='train'` 与 `mode='val'` 两个分支在写法上的差异。
4. 列出提示 GT（`InstanceData_`）的各个字段及其来源，理解为什么必须使用放宽断言的 `InstanceData_` 而不是原版 `InstanceData`。

本讲承接 u3-l4（点提示三个键）、u3-l5（文本提示四个键）与 u4-l3（解码器结构），是「数据侧提示键」与「解码器」之间的桥梁。

## 2. 前置知识

- **查询序列布局**：在 u4-l1 中我们看到，`loss()` 先用 `extract_feat` 得到每个场景的超点特征（每个超点一行，32 维），`_select_queries` 随机采样后作为查询。本讲在这些「原始超点查询」的**尾部**依次追加两类提示查询，最终布局是：

  ```text
  [ 原始超点查询 n_orig | 点提示查询 n_point | 文本提示查询 n_text ]
  ```

  追加顺序由 `loss()` 中的调用顺序决定：先点提示、后文本提示。

- **`attn_mask` 的语义**：PyTorch `nn.MultiheadAttention` 的 `attn_mask` 是一个布尔矩阵，`attn_mask[a, b] = True` 表示第 `a` 个查询**不允许**注意第 `b` 个键。也就是「True = 屏蔽、False = 可见」。本讲的 `interaction_masks` 就是喂给自注意力层的这样一张表。

- **提示查询自带答案**：点提示特征取自 GT 实例所在超点的特征，文本嵌入来自 GT 物体的描述文本。它们携带了监督信息，如果不加隔离，原始查询可能通过自注意力「偷看」提示查询，学到依赖提示的捷径，损害无提示任务（全景/语义/实例分割）的训练。这就是 `interaction_masks` 存在的设计动机（从代码机制推断的设计意图）。

- **`InstanceData`**：mmengine 的通用数据容器，允许像访问属性一样存取字段（如 `labels_3d`、`sp_masks`），但原版要求所有字段长度一致。本讲会看到提示 GT 的字段长度并不一致，因此项目定义了放宽断言的 `InstanceData_`。

- **三站键通道回顾**（u3-l4/u3-l5 已讲）：`point_prompts`、`point_prompt_instance_ids`、`point_prompt_sp_ids`、`text_token`、`label_text`、`gt_text_prompt`、`text_object_id` 等键经 transform → Pack → Preprocessor 三站白名单进入 `batch_inputs_dict`。本讲是这些键的最终消费端。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型。本讲涉及 `__init__` 中三个开关与 `type_embedding` 的创建、`loss()`/`predict()` 中提示注入与 `interaction_masks` 构造、两个 `add_*_prompt_queries`、两个 `split_original_and_*_prompt_queries` |
| [uniseg3d/structures.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py) | `InstanceData_`：放宽长度断言的数据容器，提示 GT 的载体 |
| [uniseg3d/query_decoder.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py) | `SelfAttentionLayer` 消费 `interaction_masks` 的位置；`sem_preds` 仅在末层非 None（拆分函数要处理 None 的原因） |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | 三个开关的默认取值（全部为 True）、`num_channels=32`、语言分支 `out_features=32` |

## 4. 核心概念与源码讲解

### 4.1 type_embedding 与查询拼接

#### 4.1.1 概念说明

解码器里三类查询「长」得一模一样——都是 32 维向量（超点特征 32 维、点提示特征取自超点特征、LangModule 把 768 维 CLIP 文本特征投影到 32 维），进入解码器后统一被 `query_proj` 投到 256 维（u4-l3）。模型凭什么区分「这是普通超点查询」「这是点提示」「这是文本提示」？

答案是给三者各加一个**可学习的类型偏置**：`type_embedding` 是一个 `torch.nn.Embedding(3, 32)`，3 行 32 维向量分别对应：

| 槽位 | 加给谁 | 加的位置 |
| --- | --- | --- |
| `weight[0]` | 文本提示嵌入 | `add_text_prompt_queries` |
| `weight[1]` | 点提示特征 | `add_point_prompt_queries` |
| `weight[2]` | 原始超点查询 | `add_point_prompt_queries` |

由于是逐元素相加（广播），拼接后查询张量的形状不变，仍是 `(n_queries, 32)`；类型信息被「编码」进向量本身，解码器在后续注意力中可以学会利用它。

#### 4.1.2 核心流程

以 `loss()` 中的执行顺序为准：

```text
queries = extract_feat 产出的每场景超点特征（经 _select_queries 采样）
record original_lengths            # 先记下每个场景的原始查询数
if 'point_prompts' in batch_inputs_dict:
    queries, point_gt = add_point_prompt_queries(...)   # 尾部追加点提示
if 'text_token' in batch_inputs_dict:
    queries, text_gt  = add_text_prompt_queries(...)    # 再追加文本提示
# 布局: [ 原始 n_orig | 点提示 n_point | 文本提示 n_text ]
```

`add_point_prompt_queries` 的单场景伪代码：

```text
queries[i] += type_embedding.weight[2]        # 原始查询打上「超点」标签
for 每条点提示 j:
    feat = x[i][sp_ids[j] : sp_ids[j]+1]      # 取提示落点超点的一行 (1,32)
    feat += type_embedding.weight[1]          # 打上「点提示」标签
    收集 feat; 同时按 instance_ids[j] 从 GT 切一行 label 与 sp_mask
queries[i] = cat(queries[i], 所有 feat)        # 尾部拼接
point_gt[i] = InstanceData_(labels_3d, sp_masks, point_object_id, ...)
```

`add_text_prompt_queries` 的单场景伪代码：

```text
text_embeds = self.lang(text_token)           # LangModule 编码，(n_text, 32)
text_embeds += type_embedding.weight[0]       # 打上「文本提示」标签
queries[i] = cat(queries[i], text_embeds)
text_gt[i] = InstanceData_(labels_3d=label_text, sp_masks=gt_text_prompt,
                           text_object_id=...)
```

#### 4.1.3 源码精读

**① 开关与 `type_embedding` 的创建。** 三个开关的默认值在签名里都是关闭（`set_query_mask=True` 除外），但配置显式全开了：见 [uniseg3d/uniseg3d.py:L292-L294](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L292-L294)（三个参数定义）与 [uniseg3d/uniseg3d.py:L323-L327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L323-L327)（存储开关；当 `is_type_embedding=True` 时创建 `Embedding(3, num_channels)`，`num_channels` 由配置的 `num_channels = 32` 传入，见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L9) 与 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L32-L34](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L32-L34)）。

**② 注入的触发点。** [uniseg3d/uniseg3d.py:L460-L468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L460-L468)：当 `set_query_mask=True` 时，先在注入**之前**把每个场景的原始查询数记进 `original_lengths`（这是 4.2 节掩码的分界线）；随后用 `'point_prompts' in batch_inputs_dict` 与 `'text_token' in batch_inputs_dict` 两个键探测决定是否注入——这也解释了 u3-l5 强调的「键名不符则文本分支静默跳过而非报错」。

**③ 点提示注入。** [uniseg3d/uniseg3d.py:L685-L691](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L685-L691) 读入三个数据侧键、克隆查询（防止原地修改污染后续分支），并给**所有原始查询**统一加上 `weight[2]`（`unsqueeze(0)` 是为了与 `(n_orig, 32)` 广播对齐）。注意形参名叫 `batch_sp_feat`，但调用点（[uniseg3d/uniseg3d.py:L466](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L466)）传入的其实是 `extract_feat` 的输出 `x`——即**全量**超点特征列表（`_select_queries` 采样的是 `queries`，`x` 本身未动），`point_prompt_sp_ids` 正是按全量超点编号索引的。

逐条提示的循环在 [uniseg3d/uniseg3d.py:L702-L716](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L702-L716)：每条提示只取**一行**特征 `x[i][sp_ids:sp_ids+1]`（形状 `(1, 32)`），加上 `weight[1]`；同时用 `point_prompt_instance_ids` 从 `sp_gt_instances[i]` 里逐条切出一行 `labels_3d` 与一行 `sp_masks` 作为该提示的监督真值。拼接与 GT 组装在 [uniseg3d/uniseg3d.py:L718-L735](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L718-L735)：`torch.cat((queries[i], cur_point_prompt_queries), 0)` 把提示查到询尾部；`point_object_id` 直接取数据侧的 `pts_instance_objextId_shuffle`（跨任务对比对齐的钥匙，长度是场景实例数而非提示数——这正是需要 `InstanceData_` 的原因，见本节 ④）；`point_prompt_distance_norms` 与 `point_prompt_instance_ids` 为可选字段，键存在才写入。空提示分支在 [uniseg3d/uniseg3d.py:L736-L740](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L736-L740) 用空张量占位。

**④ 文本提示注入。** [uniseg3d/uniseg3d.py:L655-L668](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L668)：先用 `self.lang(batch_inputs_dict['text_token'])` 把 token 编码为每场景 `(n_text, 32)` 的嵌入（LangModule 的输出维度由配置 `out_features=32` 决定，见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L39-L46](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L39-L46)，与查询同维才可拼接）；随后 `cur_text_prompts + self.type_embedding.weight[0]` 广播加文本类型嵌入，拼接到尾部。GT 组装在 [uniseg3d/uniseg3d.py:L670-L681](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L670-L681)：`labels_3d ← label_text`、`sp_masks ← gt_text_prompt`（转 bool）、`text_object_id ← text_object_id`；无文本时同样空张量占位。

两个值得注意的代码细节（读码发现，非臆测）：

- 文本分支使用 `weight[0]` 时**没有** `is_type_embedding` 守卫（[uniseg3d/uniseg3d.py:L666](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L666)）。若配置 `is_type_embedding=False` 却启用文本提示，会因 `self.type_embedding` 不存在而抛 `AttributeError`。当前发布配置两者同开，不会触发。
- 原始查询的类型嵌入 `weight[2]` 只在**点提示注入**里添加（[uniseg3d/uniseg3d.py:L690-L691](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L690-L691)）。若某次前向只有文本提示而没有点提示，原始查询不会被打上任何类型标签。在本仓库的训练/测试管线中两类提示总是同时出现，因此实际不受影响。

**⑤ 提示 GT 的字段总表**（两个 `InstanceData_`，逐场景一个）：

| 字段 | 点提示 GT | 文本提示 GT |
| --- | --- | --- |
| `labels_3d` | 按每条提示的 `instance_id` 从 GT 逐行切出再 cat，`(n_point,)` | `label_text[i]`，`(n_text,)` |
| `sp_masks` | 同上逐行切出，`(n_point, n_superpoints)` | `gt_text_prompt[i].bool()`，`(n_text, n_superpoints)` |
| 对齐钥匙 | `point_object_id = pts_instance_objextId_shuffle[i]`（长度为实例数） | `text_object_id = text_object_id[i]`（长度为文本数） |
| 可选字段 | `point_prompt_distance_norms`、`point_prompt_instance_ids` | 无 |

这些容器随后作为 `point_prompt_instances` / `text_prompt_instances` 传给 `criterion`（[uniseg3d/uniseg3d.py:L496](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L496)），是 u5-l5 提示损失的输入。

**⑥ 为什么必须用 `InstanceData_`。** 原版 mmengine `InstanceData` 在 `__setattr__` 里断言新字段与已有字段**等长**；而点提示 GT 中 `labels_3d` 长度是提示数、`point_object_id` 长度是实例数，二者可以不同，原版容器会直接报错。[uniseg3d/structures.py:L5-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py#L5-L25) 的 `InstanceData_` 继承后只删掉这条等长断言，保留「必须有 `__len__`」的检查——这正是类名后缀下划线「改造版」命名约定（u1-l3）的一个典型实例。

#### 4.1.4 代码实践

**实践目标**：用纯 PyTorch（无需数据、无需 GPU、无需 MinkowskiEngine）复现「类型嵌入相加 + 尾部拼接」，验证三类查询确实被打上不同标签且拼接后形状正确。

**操作步骤**（以下为示例代码，非项目原有代码）：

```python
import torch

torch.manual_seed(0)
n_orig, n_point, n_text, d = 100, 3, 2, 32

type_embedding = torch.nn.Embedding(3, d)
with torch.no_grad():
    type_embedding.weight.normal_()

# 用全零基底构造三类查询：加完类型嵌入后，行向量就等于类型嵌入本身
orig_queries  = torch.zeros(n_orig, d)   # 模拟 extract_feat 的超点查询
point_feats   = torch.zeros(n_point, d)  # 模拟 x[i][sp_id:sp_id+1]
text_embeds   = torch.zeros(n_text, d)   # 模拟 self.lang(text_token)

# 仿照 add_point_prompt_queries：原始查询加 weight[2]，点提示加 weight[1]
queries = orig_queries + type_embedding.weight[2].unsqueeze(0)
point_feats = point_feats + type_embedding.weight[1]
queries = torch.cat((queries, point_feats), 0)

# 仿照 add_text_prompt_queries：文本嵌入加 weight[0] 后拼接
text_embeds = text_embeds + type_embedding.weight[0]
queries = torch.cat((queries, text_embeds), 0)

# 验证
print(queries.shape)                                  # 期望 torch.Size([105, 32])
assert torch.equal(queries[:100], type_embedding.weight[2].expand(100, d))
assert torch.equal(queries[100:103], type_embedding.weight[1].expand(3, d))
assert torch.equal(queries[103:105], type_embedding.weight[0].expand(2, d))
print('type embedding 拼接验证通过')
```

**需要观察的现象**：打印 `queries.shape` 为 `(105, 32)`；三段断言全部通过——说明逐元素相加不改变形状，且三段行向量分别等于三个类型嵌入（基底为零时）。

**预期结果**：脚本输出 `torch.Size([105, 32])` 与 `type embedding 拼接验证通过`。若把 `weight[1]` 误换成 `weight[0]`，第二段断言会失败——这正是「槽位语义」的直观检验。本实践在只有 CPU 的环境即可运行，结果确定（`normal_` 前固定了种子）。

#### 4.1.5 小练习与答案

**练习 1**：为什么点提示特征要取 `x[i][sp_ids:sp_ids+1]`（一行），而不是把该超点特征 repeat 成提示数的倍数？

**答案**：每条点提示恰好对应一个落点超点，一条提示产生一条查询（形状 `(1, 32)`）；提示数由数据侧 `point_prompts` 的长度决定，逐条收集后 `cat` 成 `(n_point, 32)`。repeat 没有意义——提示查询与提示是一一对应的（这也是 u5-l5 中提示损失无需匹配器的原因）。

**练习 2**：如果希望给提示查询「更强」的类型区分（而不是加性偏置），可以怎么改？

**答案**：可学习的加性偏置是最轻量的做法。备选方案包括：把类型 id 拼接到特征后再过一层 MLP、或用 LayerNorm 按类型分别归一化。但要注意维度契约：无论怎么改，三类查询必须保持同维（当前 32 维）才能在 dim 0 上 `cat`，且不能破坏 `query_proj` 的输入维度（`in_channels=32`，见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L55](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L55)）。

**练习 3**：`add_point_prompt_queries` 开头的 `queries = [query.clone() for query in queries]` 去掉行不行？

**答案**：不行（至少是危险的）。后续 `queries[i] = queries[i] + self.type_embedding.weight[2]...` 与 `torch.cat(...)` 虽生成新张量，但 `clone` 保证该函数不意外修改调用方传入的原列表元素；在 `predict()` 中查询来自 `copy.deepcopy(x)`（[uniseg3d/uniseg3d.py:L789](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L789)），语义同样是不污染原始超点特征 `x`——`x` 之后还要作为解码器的键值输入（[uniseg3d/uniseg3d.py:L486](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486)）。

### 4.2 interaction_masks 构造

#### 4.2.1 概念说明

`interaction_masks` 是每个场景一张 `(n_queries, n_queries)` 布尔矩阵（每个场景查询数不同，所以是 `List[Tensor]`），描述自注意力层里「谁可以看到谁」。两个开关分工：

- `set_query_mask`：是否构造这张表。`False` 时传 `None`，自注意力完全不隔离。
- `set_all_mask`：在 `set_query_mask=True` 的基础上，是否把隔离「升级」为双向 + 组内隔离。

以 `orig = original_lengths[m]`（原始查询数）、提示块大小 `p = n_q - orig`（点提示 + 文本提示合计）划分四个区域，`True` 表示「屏蔽」：

| 区域 \ 开关 | `set_query_mask=True, set_all_mask=False` | `set_query_mask=True, set_all_mask=True`（默认配置） |
| --- | --- | --- |
| ① 原始 × 原始 `[:orig, :orig]` | False（彼此可见） | False（彼此可见） |
| ② 原始 × 提示 `[:orig, orig:]` | **True（屏蔽）** | **True（屏蔽）** |
| ③ 提示 × 原始 `[orig:, :orig]` | False（可见） | **True（屏蔽）** |
| ④ 提示 × 提示 `[orig:, orig:]` | False（全部可见） | `~eye(p)`：对角线 False，非对角线 **True** |

两个要点：

1. 区域 ② 无条件设置——原始查询（负责 PS/SS/IS 三个无提示任务）永远看不到携带 GT 线索的提示查询，防止信息泄漏。
2. 区域 ④ 的 `~eye(p)` 意味着**每条提示只看得到它自己**：点提示看不到文本提示，文本提示互相之间也不可见（`p` 是两类提示的总数，点/文在矩阵上并不分区）。设计意图（从机制推断）：每条提示的答案已经在自身特征里，互相看只会引入噪声。

#### 4.2.2 核心流程

```text
for 每个场景 m:
    n_q = len(queries[m])                      # 注入后的总查询数
    orig = original_lengths[m]                 # 注入前记录的原始查询数
    M = zeros(n_q, n_q, dtype=bool)            # 全 False = 全可见
    M[:orig, orig:] = True                     # 区域②（无条件）
    if set_all_mask:
        M[orig:, :orig] = True                 # 区域③
        if n_q - orig > 0:
            M[orig:, orig:] = ~eye(n_q - orig) # 区域④：只留对角线
    interaction_masks.append(M)
```

#### 4.2.3 源码精读

**① 训练侧构造。** [uniseg3d/uniseg3d.py:L470-L484](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L470-L484)：先 `torch.zeros(...).bool()` 建全 False 矩阵，再按上表逐区域赋值；`if query.shape[0] - original_lengths[m] > 0` 是对「该场景没有提示」的防御（此时区域③④为空块）。`set_query_mask=False` 时直接 `interaction_masks = None`（[uniseg3d/uniseg3d.py:L483-L484](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L483-L484)）。

**② 推理侧是完全重复的一段。** [uniseg3d/uniseg3d.py:L791-L815](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L791-L815) 在 `predict()` 中逐字重复了记录长度、注入、构造掩码的代码。修改隔离逻辑时**两处都要改**——这是典型的复制粘贴式维护风险，也是 u6-l4 二次开发清单里要提示的点。

**③ 消费端：自注意力层。** [uniseg3d/query_decoder.py:L100-L120](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L100-L120)：`SelfAttentionLayer.forward` 逐样本调用 `self.attn(y, y, y, attn_mask=interaction_masks[i])`。注意判断写的是 `if not interaction_masks`——`None` **或空列表**都走无掩码分支；而正常批次传入的是非空列表（哪怕矩阵全 False），走掩码分支但效果等同无掩码。传递链路：`decoder(x, queries, interaction_masks=...)` → `forward_iter_pred` → 每层的 `self_attn_layers[i](queries, interaction_masks=interaction_masks)`，见 [uniseg3d/uniseg3d.py:L486](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486) 与 [uniseg3d/query_decoder.py:L556-L558](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L556-L558)。掩码注意力只作用于**自注意力**；交叉注意力（查询→超点特征）不受 `interaction_masks` 影响，其掩码来自上一层的预测掩码（u4-l3 的 `attn_mask` 机制）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：按规格构造 batch=1、原查询 100 条、点提示 3 条、文本提示 2 条的假数据，逐格复现 `interaction_masks`，验证四个区域的布尔取值随 `set_all_mask` 的变化。

**操作步骤**（以下为示例代码，逻辑逐行对应 [uniseg3d/uniseg3d.py:L470-L484](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L470-L484)）：

```python
import torch

def build_interaction_mask(n_queries, original_length, set_all_mask):
    """逐行复现 UniSeg3D.loss() 中的 interaction_masks 构造。"""
    m = torch.zeros((n_queries, n_queries)).bool()
    m[:original_length, original_length:] = True          # 区域②
    if set_all_mask:
        m[original_length:, :original_length] = True      # 区域③
        if n_queries - original_length > 0:
            size = n_queries - original_length
            inter_mask = torch.eye(size).bool()
            m[original_length:, original_length:] = ~inter_mask  # 区域④
    return m

n_orig, n_point, n_text = 100, 3, 2
n_total = n_orig + n_point + n_text          # 105
p = n_point + n_text                          # 提示块大小 5

for set_all_mask in (True, False):
    M = build_interaction_mask(n_total, n_orig, set_all_mask)
    print(f'set_all_mask={set_all_mask}, shape={tuple(M.shape)}')
    # 四个区域逐格验证
    print('  区域① 原始×原始 全 False :', not M[:n_orig, :n_orig].any())
    print('  区域② 原始×提示 全 True  :', M[:n_orig, n_orig:].all().item())
    print('  区域③ 提示×原始 全 True  :', M[n_orig:, :n_orig].all().item())
    block4 = M[n_orig:, n_orig:]
    print('  区域④ 对角线全 False     :',
          not block4.diag().any())
    print('  区域④ 非对角线全 True    :',
          (block4[~torch.eye(p).bool()]).all().item())
    # 每个查询实际可见的键数（False 的个数）
    visible = (~M).sum(1)
    print('  原始查询可见键数:', visible[0].item(),
          ' 点提示可见键数:', visible[n_orig].item(),
          ' 文本提示可见键数:', visible[-1].item())
```

**需要观察的现象**：

- `set_all_mask=True`：区域②③全 True、区域④对角线 False 非对角线 True；原始查询可见 100 个键（只有彼此），每条提示只可见 1 个键（自己）——点提示也看不到文本提示。
- `set_all_mask=False`：区域②仍全 True（无条件），区域③④全 False；原始查询可见 100 个键，每条提示可见全部 105 个键。

**预期结果**：脚本在纯 CPU 环境即可运行，输出与上表完全一致；两种开关下区域②都为 True，验证「原始查询屏蔽提示」不依赖 `set_all_mask`。结果确定，无需本地试跑即可断言（逻辑为纯张量赋值）。

#### 4.2.5 小练习与答案

**练习 1**：`set_query_mask=True, set_all_mask=False` 时，提示查询的输出会受到原始查询的影响吗？反过来呢？

**答案**：会。区域③为 False，提示查询可以注意全部原始查询；但反向（区域②）始终为 True，原始查询永远不受提示查询影响。也就是信息只能从「原始侧」流向「提示侧」，且只在 `set_all_mask=False` 时流动。

**练习 2**：如果把 `torch.eye(size)` 误写成 `torch.eye(n_queries)`（整矩阵大小），会发生什么？

**答案**：`~inter_mask` 形状为 `(105, 105)`，赋值给 `m[orig:, orig:]`（形状 `(5, 5)` 的视图）会因形状不匹配直接抛 RuntimeError。这提示 `size` 必须是提示块大小而非总查询数。

**练习 3**：为什么 `interaction_masks` 是「每个场景一张」的列表，而不能拼成一个 `(B, n_q, n_q)` 的批张量？

**答案**：每个场景的超点数（进而查询数）不同，`n_q` 逐样本变化，无法对齐成固定形状的批张量。这也是解码器里注意力层逐样本循环的原因（u4-l3）。

### 4.3 split_original_and_*_prompt_queries 的输出拆分

#### 4.3.1 概念说明

解码器输出 `x` 是一个 dict：`cls_preds / sem_preds / masks / scores / contras_embeds` 各是长度为 batch 的列表（逐场景），外加 `aux_outputs`（前 6 层的深监督输出，每层又是同结构的 dict）。由于三类查询被拼在同一序列里联合解码，**输出的行也混在一起**：

```text
cls_preds[i]  的行: [ 原始 n_orig | 点提示 n_point | 文本提示 n_text ]
masks[i]      的行: 同上（列是 n_superpoints）
```

但下游消费方各取所需：常规损失（全景/语义/实例）只该看原始查询的输出；点提示损失只该看点提示的输出；文本提示损失只该看文本提示的输出。拆分函数就是把混合输出按行切成两个 dict：`original_outputs` 与 `*_prompt_outputs`。

由于拼接顺序是「点在前、文本在后」（4.1 节），拆分必须**从尾部链式进行**：

```text
x 行布局:            [ 原始 100 | 点 3 | 文本 2 ]
第 1 步 split text:  尾部 2 行 → text_prompt_outputs；其余 → x'
                     x' 布局: [ 原始 100 | 点 3 ]
第 2 步 split point: x' 尾部 3 行 → point_prompt_outputs；其余 → 原始输出
```

#### 4.3.2 核心流程

两个函数（text 版与 point 版）结构完全同构，单键处理流程：

```text
对每个输出键 key、每个场景 i:
    若 x[key][i] 是 None: 跳过（如 aux 层的 sem_preds）
    n = 该场景提示数
    if n > 0:
        train 模式: 提示输出 = x[key][i][-n:] ; 原始输出 = x[key][i][:-n]
        val   模式: 同上，但对 key == 'masks' 显式写成 [-n:, :] / [:-n, :]
    else:
        原始输出 = x[key][i] 全量，提示输出保持 None
aux_outputs 的每一层重复同样一遍
```

`train` 与 `val` 的差异值得专门说明：对 `(n_queries, n_points)` 的二维张量，`x[-n:]` 与 `x[-n:, :]` 的切分结果**完全相同**（都按第 0 维取行、保留全部列）。也就是说 `mode='val'` 中对 `masks` 的显式二维切片在当前代码库的张量形状下与 `mode='train'` 数值等价——两个分支更多是写法上的区分（实践 4.3.4 可直接验证 `torch.equal`）。调用点也印证：训练用默认 `mode='train'`（[uniseg3d/uniseg3d.py:L488-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L488-L491)），推理显式传 `mode='val'`（[uniseg3d/uniseg3d.py:L819-L824](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L819-L824)）。

#### 4.3.3 源码精读

**① 骨架与 None 初始化。** [uniseg3d/uniseg3d.py:L520-L546](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L520-L546)（text 版）：先建两个输出 dict，五个键各初始化为 `[None] * batch_size`，`aux_outputs` 初始化为「每层一个含五键、每键 `[None] * batch_size`」的嵌套结构。提示数为 0 的场景保持 None——下游 `point_prompt_predict_by_feat` 正是用 `out['masks'][0] == None` 判空（[uniseg3d/uniseg3d.py:L21-L23](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L21-L23)）。

**② 主输出的切分。** [uniseg3d/uniseg3d.py:L548-L564](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L548-L564)：外层 `for key in keys`，内层枚举 `batch_gt_text_prompts`（其长度即提示数来源）；`if x[key][i] is None: continue` 跳过缺失键——这之所以必要，是因为解码器里 `sem_preds` 仅在 `last_flag=True` 的末层计算，aux 层恒为 None（[uniseg3d/query_decoder.py:L501](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L501)）。train/val 两个分支见 4.3.2 的分析。

**③ aux_outputs 的切分。** [uniseg3d/uniseg3d.py:L566-L583](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L566-L583)：对每一层深监督输出重复同样的切分；若整层某键为 None，则把提示侧也置 None 并 `continue`。这样 u5-l5 的提示损失可以逐层累加深监督。

**④ point 版同构。** [uniseg3d/uniseg3d.py:L587-L653](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L587-L653) 与 text 版几乎逐行相同，仅三处不同：提示数取自 `len(batch_point_prompts[i])`、依据的键是 `point_prompts` 等、以及函数名。可以对照阅读，把 text 版读懂后 point 版自然清楚。

**⑤ 链式调用与后续消费。** 训练侧 [uniseg3d/uniseg3d.py:L488-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L488-L491)：先 text 后 point，注意第二次调用传入的 `x` 已经是第一次拆分返回的 `original_outputs`——变量被重新赋值，实现「先摘文本、再摘点提示」的链式剥离；拆完的三个 dict（`x`、`point_prompt_outputs`、`text_prompt_outputs`）连同两个提示 GT 一起喂给 `criterion`（[uniseg3d/uniseg3d.py:L496](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L496)）。推理侧 [uniseg3d/uniseg3d.py:L819-L824](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L819-L824)：拆分后立刻用提示输出算交互式/指代分割结果（`point_prompt_predict_by_feat` / `text_prompt_predict_by_feat` 按 `sp_score_thr` 阈值化掩码，见 [uniseg3d/uniseg3d.py:L20-L60](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L20-L60)），而剥离后的 `x` 继续走 `predict_by_feat` 输出全景/语义/实例三个无提示任务的结果——**一次前向、六任务结果**在代码上就落实在这几行。

**⑥ point_prob 的额外用途。** [uniseg3d/uniseg3d.py:L882-L884](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L882-L884)：推理时从 `point_prompt_outputs['cls_preds']` 取 softmax 概率，用 `gather` 按每条点提示的 GT 类别挑出「预测正确类别的概率」，写进 `data_sample.point_prob`——供评估器计算交互式分割的分类精度指标（u6-l2 会再遇到它）。

#### 4.3.4 代码实践

**实践目标**：用假解码器输出复现「先拆文本、再拆点提示」的链式剥离，验证拆分后各行数正确、且 train/val 两个分支对 `masks` 的切分数值等价。

**操作步骤**（以下为示例代码，切分逻辑对应 [uniseg3d/uniseg3d.py:L548-L564](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L548-L564)）：

```python
import torch

torch.manual_seed(0)
n_orig, n_point, n_text, n_sp, n_cls = 100, 3, 2, 50, 19
n_total = n_orig + n_point + n_text                    # 105

# 假解码器输出（batch=1：主输出 + 1 层 aux 深监督，sem_preds 仅末层非 None）
x = dict(
    cls_preds=[torch.randn(n_total, n_cls)],
    masks=[torch.randn(n_total, n_sp)],
    sem_preds=[torch.randn(n_total, 21)],
    scores=[torch.randn(n_total, 1)],
    contras_embeds=[torch.randn(n_total, 32)],
    aux_outputs=[dict(
        cls_preds=[torch.randn(n_total, n_cls)],
        masks=[torch.randn(n_total, n_sp)],
        sem_preds=None, scores=None, contras_embeds=None)],
)

def split_from_tail(t, n, key=None, mode='train'):
    """复现 split_original_and_*_prompt_queries 对单个张量的切分。

    返回 (原始输出, 提示输出)。
    """
    if mode == 'val' and key == 'masks':
        return t[:-n, :], t[-n:, :]
    return t[:-n], t[-n:]

# 第 1 步：拆文本（尾部 n_text 行）
for key in ('cls_preds', 'masks', 'sem_preds', 'scores', 'contras_embeds'):
    if x[key][0] is None:
        continue
    x[key][0], text_out = split_from_tail(x[key][0], n_text, key)
    print(f'text 拆分 {key}: 提示行数 {text_out.shape[0]}，剩余 {x[key][0].shape[0]}')

# 第 2 步：对剥离后的 x 再拆点提示（尾部 n_point 行）
for key in ('cls_preds', 'masks', 'sem_preds', 'scores', 'contras_embeds'):
    if x[key][0] is None:
        continue
    x[key][0], point_out = split_from_tail(x[key][0], n_point, key)
    print(f'point 拆分 {key}: 提示行数 {point_out.shape[0]}，剩余 {x[key][0].shape[0]}')

print('最终原始输出行数:', x['cls_preds'][0].shape[0])   # 期望 100

# 验证 train/val 两分支对 masks 数值等价
m = torch.randn(105, n_sp)
o1_train, p1_train = split_from_tail(m, 3, 'masks', mode='train')
o1_val,   p1_val   = split_from_tail(m, 3, 'masks', mode='val')
assert torch.equal(o1_train, o1_val) and torch.equal(p1_train, p1_val)
print('train/val 两个分支对 masks 的切分 torch.equal 验证通过')
```

**需要观察的现象**：text 拆分后各键提示行数为 2、剩余 103；point 拆分后提示行数为 3、剩余 100；最后一行断言通过。

**预期结果**：`最终原始输出行数: 100`，且 `torch.equal` 验证通过——说明链式拆分精确还原三类查询的行归属，且 `mode` 的两个分支对二维 `masks` 切分结果一致。若把两次拆分顺序颠倒（先 point 后 text），由于点提示不再位于尾部，拆出的「点提示输出」实际会包含文本行——这正是「必须先拆文本」的原因。本实践纯 CPU 可运行，结果确定。

（注：示例中 `aux_outputs` 的写法保持了「`sem_preds` 为 None、其余键可为 None」的结构，用于体会 4.3.3 ③ 的 None 处理分支；脚本中 `x['sem_preds']` 等主输出非 None，aux 层不参与主流程演示。）

#### 4.3.5 小练习与答案

**练习 1**：为什么拆分顺序必须是「先 text 后 point」，而不能反过来？

**答案**：拼接顺序是 `[原始 | 点 | 文本]`，文本在最后。拆分函数只会取「尾部 n 行」。先拆文本（尾部 2 行）后，点提示才暴露在剩余序列的尾部，第二次拆分才能正确取到。若先拆 point，取到的尾部 3 行是「1 条点提示 + 2 条文本」，行归属就乱了。

**练习 2**：某场景提示数为 0 时（例如验证集中一个没有任何文本描述的场景），拆分函数与下游分别如何表现？

**答案**：拆分函数走 `else` 分支，`original_outputs[key][i] = x[key][i]` 全量保留，提示侧保持初始化的 None（[uniseg3d/uniseg3d.py:L563-L564](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L563-L564)）；推理下游 `point_prompt_predict_by_feat` / `text_prompt_predict_by_feat` 用 `out['masks'][0] == None` 判空并返回空张量结果。

**练习 3**：拆分后的 `original_outputs` 里还包含 `contras_embeds` 吗？它有什么用？

**答案**：包含。`contras_embeds` 是逐层的 `out_norm(queries)` 克隆（u4-l3），拆分函数把它与其他键一视同仁地按行切开。原始查询、点提示、文本提示三路的 `contras_embeds` 是 u5-l6 跨任务对比学习与伪分布蒸馏的输入——这正是「联合解码后拆分」设计带来的便利：三路表征天然对齐在同一次前向里。

## 5. 综合实践

**任务**：写一个纯 PyTorch 的「迷你版注入-隔离-解码-拆分」端到端脚本（无需仓库环境、无需数据），验证隔离掩码真的挡住了信息流。

1. **准备**：`n_orig=100, n_point=3, n_text=2, d=32`；用 `torch.nn.Embedding(3, 32)` 按本讲 4.1 的槽位表构造查询序列（基底随机即可），拼接成 `(105, 32)`。
2. **构造掩码**：按 4.2 复现 `interaction_masks`（`set_all_mask=True`）。
3. **迷你解码**：用 `torch.nn.MultiheadAttention(d, num_heads=4, batch_first=True)` 做一次自注意力 `attn(y, y, y, attn_mask=M)`（这是 `SelfAttentionLayer` 的核心调用）。
4. **信息流验证**：固定其余输入，只改动**某一条原始查询**的数值，比较改动前后输出的差异：
   - 点提示/文本提示对应的输出行应**逐元素不变**（它们只能看到自己，`set_all_mask=True` 时原始查询对它们不可见）；
   - 其他原始查询的输出行应**发生变化**（它们能看到被改的查询）。
5. **拆分**：对注意力输出按 4.3 的链式顺序拆回三路，核对行数 `100 / 3 / 2`。
6. **思考题**：把 `set_all_mask` 改为 `False` 重跑第 4 步，提示行这次会变吗？为什么？（答案：会变——区域③为 False，提示查询可以注意全部原始查询，原始查询的改动会传导过去。）

**预期结果**：第 4 步中提示行的最大绝对差应为 `0.0`，其余原始行差异非零；第 5 步行数核对通过。整个脚本只依赖 `torch`，CPU 即可运行，现象确定。

## 6. 本讲小结

- 查询序列布局是 `[原始超点查询 | 点提示查询 | 文本提示查询]`，由 `loss()` 中先 point 后 text 的注入顺序决定；三类查询同为 32 维，靠 `Embedding(3, 32)` 的三个槽位（0=文本、1=点、2=原始）做加性类型标记。
- `interaction_masks` 是逐场景的 `(n_q, n_q)` 布尔表，True=屏蔽：区域②（原始→提示）无条件屏蔽；`set_all_mask=True` 时再叠加区域③（提示→原始）与区域④（提示彼此只看自己）；配置默认全开。它只作用于自注意力，训练与推理两处代码逐字重复。
- `split_original_and_*_prompt_queries` 从尾部链式拆分（必须先 text 后 point），把混合输出切成 `original_outputs` 与两路提示输出，主输出与全部 `aux_outputs` 深监督层都拆；`mode='train'` 与 `'val'` 对二维 `masks` 的切分数值等价。
- 提示 GT 用放宽等长断言的 `InstanceData_` 承载：点提示的 `point_object_id` 长度是实例数、与提示数的 `labels_3d` 不等长，原版 `InstanceData` 会报错。
- 「一次前向、六任务结果」在代码上的落点：拆分后原始输出走 `predict_by_feat`（PS/SS/IS），点/文本提示输出各自走 `*_prompt_predict_by_feat`（交互式/指代），`point_prob` 另供评估用。

## 7. 下一步学习建议

- 下一讲 u5-l1 将进入损失总调度 `ScanNetUnifiedCriterion`：本讲产出的 `original_outputs`、两路提示输出与两个 `InstanceData_` GT 正是它的全部输入，建议先回看 [uniseg3d/uniseg3d.py:L496](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L496) 这一行调用。
- 提示查询与 GT 天然一一对应（本讲 4.1 的逐条切行），因此 u5-l5 的两个提示损失不再需要匹配器，可对照阅读。
- 三路 `contras_embeds` 的拆分为 u5-l6 的跨任务对比学习（`text_object_id` 与 `point_object_id` 在此对齐）做了铺垫，届时回看本讲的 GT 字段表。
- 若想检验对本讲的掌握，可尝试 u6-l4 的「新增一种提示类型」练习：注入（带类型槽位）、掩码区域、尾部拆分、GT 容器四件事都要同步扩展。
