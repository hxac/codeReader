# 二次开发指南：扩展点与改造清单

## 1. 本讲目标

本讲是全书的收官篇。前面五单元我们沿着「数据 → 模型 → 损失 → 推理 → 评估」把 UniSeg3D 读了一遍，本讲把这些知识兑换成**动手改造的能力**。学完本讲你应该能够：

1. 说出 UniSeg3D 的四类扩展点（新提示类型、替换骨干/解码器、接入新数据集、调整多任务损失），以及每个扩展点必须遵守的**接口契约**。
2. 独立实现一个最小化的新提示类型（如「框提示」），并定位它需要改动的**每一个文件与每一行**。
3. 理解注册器机制为什么让新增模块「即插即用」，以及「三站通道」（transform 写键 → Pack 分拣键 → Preprocessor 透传键）为什么让键名一处不符就静默断流。
4. 在动手前评估改动对**六任务联动**的影响——例如删掉一个提示 transform 会让对比损失直接崩溃、关掉 `merge_ov` 会让损失总调度取不到 `is_novel` 字段。

## 2. 前置知识

本讲默认你已完成前面讲义，以下认知直接承接、不再重复推导：

- **配置即代码**（u2-l1/u2-l2）：配置文件是可执行 Python，`type='XXX'` 字符串经 Registry 查表实例化，`custom_imports = dict(imports=['uniseg3d'])` 触发全仓库 29 个类注册进 5 张注册表。
- **三站通道**（u3-l2/u3-l4/u3-l5）：提示键要在 `transform()` 写入、`Pack3DDetInputs_` 的 `INPUTS_KEYS` 白名单分拣、`Det3DDataPreprocessor_` 的 `simple_process()` 白名单透传，三站键名必须一字不差。
- **提示四件套**（u3-l4/u3-l5/u4-l6/u5-l5）：一个提示类型 = 数据侧 transform + 模型侧注入 `add_*_prompt_queries` + 拆分 `split_original_and_*_prompt_queries` + 损失 `*PromptInstanceCriterion`。
- **尾部链式拆分**（u4-l6）：注入顺序决定查询布局「原始 | 点 | 文本」，拆分必须从尾部反向进行（先文本后点）。
- **球面分类**（u4-l4）：分类头不是线性层，而是与 `data/scannet/scannet_cls_embedding.pth` 词表原型的余弦相似度——换词表必须换这个文件。
- **对比蒸馏**（u5-l6）：`ContrastiveCriterion` 用 `text_object_id` / `point_object_id` 把同物体的文本与点提示配成正样本对。
- **一次前向六任务**（u6-l1/u6-l2/u6-l3）：推理与评估侧如何消费提示输出。

还要提醒一个工程事实：本仓库 `uniseg3d/` 内各模块之间**几乎没有 import 关系**，全靠配置字符串经 `MODELS.build` 运行时组装（u1-l3）。这意味着二次开发的主旋律是「**新建文件 + 注册 + 改配置**」，而不是「改老代码」——能不改老代码就不改，这是把回归风险控制在最小的关键。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `uniseg3d/uniseg3d.py` | 主模型：注入/拆分/interaction_masks/损失调用的落点，新提示的主要手术台 |
| `uniseg3d/transforms_3d.py` | 数据侧：`PointPromptGeneration` 等提示 transform 与 `AddSuperPointAnnotations` 的兼容分支 |
| `uniseg3d/unified_criterion.py` | 损失总调度：五路子损失的组装与调用顺序，新提示损失的接入点 |
| `uniseg3d/scannet_dataset.py` | 数据集类模板：新数据集子类要仿写的 33 行 |
| `uniseg3d/formatting.py` / `uniseg3d/data_preprocessor.py` | 三站通道的后两站：键白名单 |
| `uniseg3d/contrastive_criterion.py` | 跨任务联动陷阱的现场：无 None 旁路 |
| `uniseg3d/__init__.py` | 注册清单：新模块必须在这里 import |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 配置联动：pipeline / criterion / evaluator 的修改现场 |
| `tools/indoor_converter.py` | 新数据集 infos 的参考实现（注意 dump 已被注释） |

## 4. 核心概念与源码讲解

### 4.1 扩展点总览与「配置-注册」联动

#### 4.1.1 概念说明

什么是「扩展点」？在 UniSeg3D 里，一个位置是扩展点需要满足两个条件：

1. **边界清晰**：它与其他代码只通过少量明确的键名或函数签名交互（接口契约）。
2. **可注册**：新增实现只需 `@XXX.register_module()` + 配置里改一个 `type` 字符串，无需改动消费方代码。

为什么第二个条件成立？回顾 u2-l1 的结论：Registry 是「名字 → 类」的全局字典，`MODELS.build(cfg)` 拿 `cfg['type']` 查表、其余键全部作为 `__init__` 参数。所以只要新类签名兼容，配置里的 `type` 从 `'SpConvUNet'` 换成 `'MyBackbone'`，主模型一行不用改——这就是「即插即用」的机械原理。注册的触发是 import 的副作用：主模型在构造子模块时统一走 `MODELS.build`：

[uniseg3d/uniseg3d.py:311-314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314) —— 主模型 `__init__` 中骨干、解码器、语言分支、损失四件套全部由配置 dict 经 `MODELS.build` 实例化，这是所有「替换型」扩展的挂载点。

而注册清单本身在包的入口文件里：

[uniseg3d/__init__.py:1-19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19) —— 19 行 import 就是全仓库的注册清单；你新建的任何模块文件必须在这里加一行 import，`custom_imports` 才会在训练启动时触发它的 `@register_module()` 装饰器执行。**漏加这一行是最常见的新手错误**：配置写了 `type='BoxPromptGeneration'`，报错 `BoxPromptGeneration is not in the TRANSFORMS registry`。

#### 4.1.2 核心流程

UniSeg3D 的四类扩展点及其契约：

| 扩展点 | 契约（必须满足的接口） | 主要触达文件 |
| --- | --- | --- |
| 新提示类型 | 输出三键（提示定位/实例行号/超点 id）+ 同序 GT；模型侧尾部追加查询并可反向拆分 | `transforms_3d.py`、`uniseg3d.py`、`unified_criterion.py`、`formatting.py`、`data_preprocessor.py`、config |
| 替换骨干 | 接收 spconv `SparseConvTensor`，返回 `(SparseConvTensor, ...)` 元组，输出特征通道 = `num_channels`（32） | 新文件 + config |
| 替换解码器 | 签名 `forward(x, queries, interaction_masks)`，返回含 `cls_preds/sem_preds/masks/scores/contras_embeds/aux_outputs` 的 per-scene dict | 新文件 + config |
| 接入新数据集 | 四类 bin + infos pkl + dataset 子类 + config 四层齐备 | `data/`、`tools/`、新 dataset 文件、config |
| 损失/训练策略 | 旋钮几乎全在 config 的 `model.criterion.*` 与顶层变量 | config（多数免改代码） |

骨干契约的证据在特征提取处：

[uniseg3d/uniseg3d.py:372-375](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L372-L375) —— `input_conv` → `self.unet(x)`（返回元组，取第一个）→ `output_layer`，之后 `scatter_mean` 把体素特征聚合到超点作为查询。你的新骨干输出通道不是 32，`decoder` 的 `in_channels=32`（config）就会维度不匹配。

解码器契约的证据：

[uniseg3d/query_decoder.py:531-559](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L531-L559) —— `forward_iter_pred` 接收 `(x, queries, interaction_masks)`，逐层 cross-attn / self-attn / FFN，每层产出预测。主模型只以 `self.decoder(x, queries, interaction_masks=interaction_masks)` 调用（[uniseg3d/uniseg3d.py:486](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486)），返回 dict 的键被损失与拆分代码消费，这就是全部契约。

#### 4.1.3 源码精读：一个键的一生（提示键消费地图）

以点提示三键为例，追踪 `batch_inputs_dict` 中一个键的全部消费点，这张地图是 4.2 实践的基础：

| 站点 | 位置 | 作用 |
| --- | --- | --- |
| ① 生成 | `transforms_3d.py` L160-163 | transform 写入 `point_prompts` / `point_prompt_instance_ids` / `point_prompt_sp_ids` |
| ② 分拣 | `formatting.py` L15-18 | 三键在 `INPUTS_KEYS` 白名单，进 `inputs` |
| ③ 透传 | `data_preprocessor.py` L73-80 | 三键在 `simple_process` 白名单，进 `batch_inputs` |
| ④ 探测 | `uniseg3d.py` L465 / L796 | `if 'point_prompts' in batch_inputs_dict.keys()` 决定是否注入 |
| ⑤ 消费 | `uniseg3d.py` L686-688 | 注入时读取三键内容 |
| ⑥ 拆分 | `uniseg3d.py` L490-491 / L822-823 | 用键长度从解码输出尾部切出提示输出 |
| ⑦ 损失 | `unified_criterion.py` L43-53、L68-69 | 组装提示 GT 并调用提示损失 |
| ⑧ 回写 | `uniseg3d.py` L877-884 | 预测结果与置信度写回 data_sample（评估器消费） |

对应源码：

[uniseg3d/transforms_3d.py:160-163](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L160-L163) —— 点提示 transform 的输出：三个逐条对齐的键（提示点下标、GT 实例行号、提示点所在超点 id），外加跨任务对齐钥匙 `pts_instance_objextId_shuffle`。

[uniseg3d/formatting.py:15-18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18) —— `INPUTS_KEYS` 白名单：点/文本提示的全部键都在这里，新键不加进来，`Pack` 会直接抛 `NotImplementedError`（L130-134），这是三站中唯一「大声报错」的一站。

[uniseg3d/data_preprocessor.py:73-101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L101) —— `simple_process` 逐键判断透传：不在白名单的键**静默丢弃**，不报错。②站漏加会崩、③站漏加会静默断流，调试难度完全不同。

#### 4.1.4 代码实践：亲手画出消费地图

1. **实践目标**：用 grep 亲手验证上表的「提示键消费地图」，建立二次开发时的第一反应——「改一个键，先查它所有消费点」。
2. **操作步骤**：
   ```bash
   # 所有提示相关键在主模型中的使用点
   grep -n "point_prompt\|text_token\|label_text\|gt_text_prompt\|text_object_id" uniseg3d/uniseg3d.py
   # 三站白名单现状
   grep -n "point_prompt\|text_" uniseg3d/formatting.py uniseg3d/data_preprocessor.py
   # 注册清单
   grep -n "register_module" uniseg3d/*.py
   ```
3. **需要观察的现象**：`uniseg3d.py` 中 `batch_inputs_dict` 的键访问集中在 L449-467（训练）与 L780-799（推理）两个函数里，且两边**探测逻辑完全对称**——训练注入了什么键，推理就必须同样处理，否则测试时提示查询会混进原始查询污染实例预测。
4. **预期结果**：得到一张与 4.1.3 表格一致的行号清单；特别确认 L796-799（predict 的探测）与 L465-468（loss 的探测）成对出现。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「漏改 `__init__.py`」和「漏改 `data_preprocessor.py`」的故障表现完全不同？

**答案**：`__init__.py` 漏 import → 新类从未注册 → `build` 时抛 `xxx is not in the registry`，启动即失败、原因明确；`data_preprocessor.py` 漏透传 → 键在第③站被静默丢弃 → `batch_inputs_dict` 里没有该键 → 模型侧 `if 'xxx' in ...` 探测为 False → 该提示分支整体静默跳过，训练照常跑但新功能无效，极难发现。

**练习 2**：如果把骨干换成输出 64 通道的新实现，最少还要改哪里？

**答案**：config 中 `num_channels=32`（它同时决定 `input_conv` 输出、`num_planes[0]`、decoder 的 `in_channels`、`type_embedding` 维度与语言分支 `out_features=32` 的对齐）。整个 32 维是「超点查询空间」的公共维度，骨干输出必须能被 `scatter_mean` 聚合后仍为 32 维，否则要么改 config 全链路维度、要么在新骨干尾部加投影。

**练习 3**：主模型与各子模块之间没有 import，这个设计对二次开发意味着什么？

**答案**：意味着替换实现零侵入——新类只要注册 + 配置改 `type` 即可；同时也意味着**签名兼容是口头约定而非编译期检查**，接口不匹配只会在运行时（甚至训练第一个 iter 才）暴露，所以新模块必须先跑一次冒烟前向再投入训练。

---

### 4.2 新提示类型四件套：transform → 注入 → 拆分 → 损失

#### 4.2.1 概念说明

「提示」是 UniSeg3D 区别于普通分割框架的核心抽象：一条提示 = 一个自带答案的查询，拼在原始超点查询尾部参与解码，输出再从尾部拆出来单独监督。u4-l6 已讲清机制，本讲从**改造者视角**重新组织：新增一种提示类型，等于在四个位置各补一段「同构」的代码：

1. **transform**（数据侧）：为每个 GT 实例生成一条提示，产出「提示定位 / GT 实例行号 / 超点 id」三键。
2. **注入**（模型侧 `add_*_prompt_queries`）：把提示特征拼进查询列表尾部，并同步组装提示 GT（`InstanceData_`）。
3. **拆分**（模型侧 `split_original_and_*_prompt_queries`）：把解码输出的提示部分从尾部切出。
4. **损失**（`*PromptInstanceCriterion`）：提示查询与 GT 天然一一对应，恒等匹配，只剩 cls/BCE/Dice。

关键洞察：**模型对提示类型的感知只到「键名」为止**。`loss()` 用 `if 'point_prompts' in batch_inputs_dict.keys()` 探测（L465），它不关心提示是怎么选出来的。这给了我们一条捷径——新提示**复用现有键名**，四件套里三件直接免写。

#### 4.2.2 核心流程

注入与拆分的顺序规则（后进先出栈）：

```text
注入（loss: L465-468）              拆分（loss: L488-491）
────────────────────────           ────────────────────────
1. add_point_prompt_queries   →    2. split_..._text_prompt   （先拆文本）
2. add_text_prompt_queries    →    1. split_..._point_prompt  （后拆点）

查询布局：[ 原始超点查询 | 点提示 | 文本提示 ]
尾部追加 ⇒ 尾部切出，顺序必须严格镜像
```

两类「免费」机制——新增提示类型不需要碰的代码：

- **interaction_masks 自动覆盖**：掩码在注入**前**记录原始查询长度 `original_lengths`，注入**后**凡下标 ≥ 原始长度的位置一律归入「提示区」。新提示拼在尾部，自动落入提示区、自动被隔离，一行不用改（见 [uniseg3d/uniseg3d.py:460-484](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L460-L484)）。
- **恒等匹配损失可复用**：提示损失不做匹配（u5-l5），只要提示 GT 的 `labels_3d` / `sp_masks` 与查询同序，`PointPromptInstanceCriterion` 的实现原样可用。

一处**必须**改的硬编码：

- `type_embedding` 是 `torch.nn.Embedding(3, num_channels)`，3 个槽位分别给原始查询（weight[2]）、点提示（weight[1]）、文本提示（weight[0]）。新提示要么扩容到 4、要么复用点提示槽位。扩容的代价：resume 官方 checkpoint 时该层形状不匹配（`load_from` 的 SSTNet 预训练不含此层则无影响）。

#### 4.2.3 源码精读

**(a) 数据侧模板**——点提示怎么产出三键：

[uniseg3d/transforms_3d.py:117-165](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L117-L165) —— `PointPromptGeneration.transform`：清洗实例 id（L122-128 有无 `eval_ann_info` 的两套 id 体系分支）→ 打乱顺序逐实例选点 → L160-163 写入三键与 `pts_instance_objextId_shuffle`。你的新 transform 照抄这个骨架即可，唯一自由度是「怎么选提示锚点」。

**(b) 三站白名单**：

[uniseg3d/formatting.py:15-18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18) 与 [uniseg3d/data_preprocessor.py:73-101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L101) —— 新键名（不复用 point 通道时）必须同时加进这两处，外加 config 里 `Pack3DDetInputs_` 的 `keys` 列表（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:191-199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L191-L199)）——注意 config 的 keys 是显式清单，加白名单不加 config 同样白搭。

**(c) 注入模板**——新提示的 `add_*_prompt_queries` 长什么样：

[uniseg3d/uniseg3d.py:685-744](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L685-L744) —— `add_point_prompt_queries` 五步：克隆查询并给原始查询加 type_embedding（L690-691）→ 逐提示取超点特征 `batch_sp_feat[i][sp_id:sp_id+1]` 并加提示类 type_embedding（L706-708）→ `torch.cat` 拼接（L721）→ 同序组装 `labels_3d` / `sp_masks`（L712-716、L724-728）→ 存对齐钥匙（L730-735）。空提示走 else 分支写空张量（L736-740）。注意 L710 的 `assert len(sp_gt_instances[i]) > 0`：无实例场景必须保证循环体不执行，否则断言崩溃。

**(d) 拆分模板**：

[uniseg3d/uniseg3d.py:587-653](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L587-L653) —— `split_original_and_point_prompt_queries`：按每场景提示数从尾部切片，`train` 模式按行切、`val` 模式对 `masks` 键额外按列切（L624-630），主输出与六层 `aux_outputs` 同样处理。新提示的拆分函数照抄即可。

**(e) 损失接入点**：

[uniseg3d/unified_criterion.py:18-20](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L18-L20) —— 损失总调度的 `__call__` 签名**硬编码了 point/text 两路**参数；[unified_criterion.py:43-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L63) 组装提示 GT，[unified_criterion.py:65-74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74) 按 实例 → 语义 → 点提示 → 文本提示 → 对比 的顺序调用五路。新提示走独立键名时，这里要加第六个参数与第四个提示分支；复用 point 键名时一行不改。

**(f) type_embedding 硬编码**：

[uniseg3d/uniseg3d.py:326-327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L326-L327) —— `torch.nn.Embedding(3, num_channels)`，三槽位在 L666（文本 weight[0]）、L708（点 weight[1]）、L691（原始 weight[2]）被消费。

#### 4.2.4 代码实践：最小「框提示」两级实现

**实践目标**：实现框提示（以 GT 实例质心为球心、半径 r 圈选球内点，取离质心最近者为提示锚点），先走零侵入捷径，再升级为完整四件套。

**Level 1 —— 复用 point 通道（只写 1 个新文件 + 改 config 两行）**

核心思路：通道只认键名不认来源。框提示的三键直接命名为 `point_prompts` 三键，注入/拆分/损失/推理/评估全部免改。

第一步，新建 `uniseg3d/box_prompt_transform.py`（**示例代码**，骨架照抄 `PointPromptGeneration`，仅选点策略不同）：

```python
# 示例代码：Level 1 最小框提示，输出键复用点提示通道
import numpy as np
from mmcv.transforms import BaseTransform
from mmdet3d.registry import TRANSFORMS


@TRANSFORMS.register_module()
class BoxPromptGeneration(BaseTransform):
    """以 GT 实例质心为球心、radius 为半径圈选球内点，
    取球内离质心最近的点作为提示锚点。"""

    def __init__(self, radius=0.3):
        self.radius = radius

    def transform(self, input_dict):
        sp_pts_mask = np.array(input_dict['sp_pts_mask'])
        pts_instance_mask = np.array(input_dict['pts_instance_mask']).copy()
        pts_instance_id = np.unique(pts_instance_mask)
        if 'eval_ann_info' in input_dict.keys():      # 测试：评估 id 体系（-2 偏移）
            pts_instance_mask = pts_instance_mask - 2
            pts_instance_id = pts_instance_id - 2
            pts_instance_id = np.delete(pts_instance_id,
                                        np.where(pts_instance_id < 0))
        else:                                          # 训练：-1 为未标注
            pts_instance_id = np.delete(pts_instance_id,
                                        np.where(pts_instance_id == -1))

        point_prompts = np.array([])
        select_ids = np.array([])
        if len(pts_instance_id) > 0:
            select_ids = pts_instance_id.copy()
            np.random.shuffle(select_ids)
            for select_id in select_ids:
                pts_select_id = np.where(pts_instance_mask == select_id)[0]
                instance_points = np.asarray(
                    input_dict['points'][pts_select_id][:, :3])
                centroid = instance_points.mean(axis=0)
                dists = np.linalg.norm(instance_points - centroid, axis=1)
                inner = dists < self.radius
                if inner.sum() == 0:                   # 实例比球小：退化取最近点
                    inner = dists <= dists.min() + 1e-6
                inner_idx = np.where(inner)[0]
                anchor = pts_select_id[inner_idx[np.argmin(dists[inner_idx])]]
                point_prompts = np.append(point_prompts, anchor)

        # 关键：与点提示完全同名、同序的四行，缺一不可
        input_dict['pts_instance_objextId_shuffle'] = \
            input_dict['pts_instance_objextId'][select_ids]
        input_dict['point_prompts'] = point_prompts
        input_dict['point_prompt_instance_ids'] = select_ids
        input_dict['point_prompt_sp_ids'] = \
            sp_pts_mask[list(point_prompts.astype(int))]
        return input_dict
```

第二步，注册：在 [uniseg3d/__init__.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19) 末尾加一行 `from .box_prompt_transform import BoxPromptGeneration`。

第三步，改 config（**示例代码**）：

```python
# train_pipeline 中，把 dict(type='PointPromptGeneration', samplePoint=True)
dict(type='BoxPromptGeneration', radius=0.3),     # 替换
# test_pipeline 的 MultiScaleFlipAug3D.transforms 中，把 PointPromptTest
dict(type='BoxPromptGeneration', radius=0.3),     # 替换（有 eval_ann_info 分支，兼容测试）
```

**Level 2 —— 独立四件套（真正的新键名 `box_prompts`）**

改动清单（按依赖顺序）：

1. `box_prompt_transform.py`：输出 `box_prompts` / `box_prompt_instance_ids` / `box_prompt_sp_ids` 三键（把 Level 1 代码的四行键名换掉即可）。
2. [uniseg3d/formatting.py:15-18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18)：`INPUTS_KEYS` 追加三键。
3. [uniseg3d/data_preprocessor.py:73-101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L101)：仿 L73-80 追加三个 `if 'box_...' in inputs` 透传。
4. [uniseg3d/uniseg3d.py:326-327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L326-L327)：`Embedding(3, ...)` → `Embedding(4, ...)`，框提示用 `weight[3]`。
5. 新增 `add_box_prompt_queries`（仿 [uniseg3d/uniseg3d.py:685-744](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L685-L744)）。框提示的独特语义在这里实现（**示例代码**）：
   ```python
   # 示例代码：圈选超点集合取均值特征作查询（区别于点提示的单超点）
   cur_sp_ids = batch_box_prompt_sp_ids[i][j]        # 该提示圈选的超点 id 集合
   cur_feat = batch_sp_feat[i][cur_sp_ids].mean(0, keepdim=True)
   cur_feat = cur_feat + self.type_embedding.weight[3]
   ```
   注意 `batch_sp_feat[i]` 是 `extract_feat` 按 `batch_offsets` 切出的**场景局部**超点特征（[uniseg3d/uniseg3d.py:376-379](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L376-L379)），而 transform 写入的超点 id 也是场景局部值（`loss()` 里 `+= superpoint_bias` 只改 data_sample 副本，[uniseg3d/uniseg3d.py:442-444](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L442-L444)），二者天然对齐。
6. 新增 `split_original_and_box_prompt_queries`（照抄 [uniseg3d/uniseg3d.py:587-653](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L587-L653)，全文替换键名）。
7. 在 `loss()` 与 `predict()` 中插入注入/拆分调用（**示例代码**）：
   ```python
   # 注入（顺序决定布局）：
   if 'point_prompts' in batch_inputs_dict.keys():  # → 布局 [原始|点]
       ...
   if 'box_prompts' in batch_inputs_dict.keys():    # → 布局 [原始|点|框]
       ...
   if 'text_token' in batch_inputs_dict.keys():     # → 布局 [原始|点|框|文本]
       ...
   # 拆分必须严格反向：先文本、再框、最后点。
   ```
8. 新建 `BoxPromptInstanceCriterion`：最省事的做法是复制 `PointPromptInstanceCriterion`（[uniseg3d/point_prompt_instance_criterion.py:48-58](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L48-L58)）改类名注册——恒等匹配的逻辑与提示来源无关。
9. [uniseg3d/unified_criterion.py:8-16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L8-L16) 与 [L18-20](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L18-L20)：`__init__` 末尾加 `box_prompt_inst_criterion=None` 参数并 build，`__call__` 加 `box_prompt_pred=None, box_prompt_insts=None` 参数，仿 [L68-72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L68-L72) 加调用分支。注意 Python 语法：新参数必须放在已有无默认参数之后。
10. `uniseg3d/__init__.py` 注册新 transform 与新 criterion；config 的 `Pack3DDetInputs_.keys`、train/test pipeline、`model.criterion` 各加对应条目。
11. **推理侧联动**（易漏）：`predict()` 中 [L796-799](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L796-L799) 的注入与 [L819-824](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L819-L824) 的拆分必须与训练对称，否则测试时框提示查询不拆出，会混进原始查询参与 topk 实例筛选，污染全部五任务输出。若还要评估框提示，再仿 [L877-888](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L877-L888) 回写结果并扩展 `unified_metric.py` 的 `process()`（u6-l2）。

**需要观察的现象 / 预期结果**（**待本地验证**，本环境无数据与 GPU）：

- Level 1 验证：数据就绪后运行
  ```bash
  ./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 \
      --cfg-options train_cfg.max_epochs=1 train_dataloader.batch_size=1
  ```
  预期日志正常出现 `point_prompt_inst_loss`（损失不知道提示来自框还是点，键名相同即生效），且无注册报错、无 `assert len(sp_gt_instances[i]) > 0` 崩溃。
- Level 2 验证：同上跑一个 iter，预期 loss dict 新增 `box_prompt_inst_loss`（键名由你新 criterion 的返回决定）；再用 `tools/test.py` 跑一个 val 场景，确认五任务常规指标不掉（证明拆分对称、提示查询未泄漏进原始输出）。

#### 4.2.5 小练习与答案

**练习 1**：Level 1 里为什么必须原样保留 `pts_instance_objextId_shuffle` 这一行的写入？

**答案**：`add_point_prompt_queries` 在 [uniseg3d/uniseg3d.py:730](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L730) 无条件读取 `batch_inputs_dict['pts_instance_objextId_shuffle'][i]` 写入 `point_object_id`，它是 u5-l6 对比损失配对文本/点提示的钥匙。漏写 → 训练启动后在对比损失里 KeyError，且报错位置远离改动点。

**练习 2**：如果把框提示放在注入顺序的第一位（点之前），拆分顺序应该怎么排？

**答案**：布局变为 `[原始 | 框 | 点 | 文本]`，拆分仍从尾部反向：先文本、再点、最后框。规则与顺序无关，只与「尾部追加、尾部切出」有关；但每次调整注入顺序都要同步镜像拆分顺序，这是最容易引入静默错位（提示输出串行）的地方。

**练习 3**：为什么说 interaction_masks 是新提示的「免费午餐」？给出代码证据。

**答案**：掩码构造在注入后进行，用注入前记录的 `original_lengths` 把坐标空间分成原始区与提示区（[uniseg3d/uniseg3d.py:460-481](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L460-L481)），`set_all_mask=True` 时提示区内部再用 `~eye` 互斥。它只区分「原始 vs 提示」，不区分提示的具体类型，因此第 N 种提示自动被正确隔离，无需任何修改。

---

### 4.3 新数据集接入清单

#### 4.3.1 概念说明

接新数据集不是写一个类那么简单——UniSeg3D 的训练输入由**四层**构成，每层都有必须满足的键约定：

1. **裸数据层**：四类 bin——`points`（N×6 float32，XYZ+RGB）、`instance_mask` / `semantic_mask` / `super_points`（N×1 int64）。超点标签由 segmentator 对完整 mesh 过分割生成（u1-l4），这一步**无法绕过**：没有超点就没有查询。
2. **infos 层**：每场景一个 dict 的 pkl，携带路径 basename、`text_info`（ScanRefer 文本 + 离线分词）、`sam3d_pseudo_sp_masks`（SAM3D 伪掩码）等。
3. **dataset 层**：子类负责拼路径字符串（不读点云文件，u3-l1）。
4. **config 层**：`dataset_type` / `ann_file` / `data_prefix` / 类别表 / evaluator。

#### 4.3.2 核心流程：接入清单（checklist）

| # | 改造项 | 文件 | 要点 |
| --- | --- | --- | --- |
| 1 | 原始数据解析 | 新建 `data/<newset>/`（仿 `data/scannet/batch_load_scannet_data.py`） | 产出四类 bin；超点必须跑 segmentator |
| 2 | infos 生成 | 仿 [tools/create_data.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L5) + [tools/indoor_converter.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L10-L68) | 注意 dump 已被注释（见下），需自行恢复/重写 |
| 3 | dataset 子类 | 新建（仿 `scannet_dataset.py` 33 行） | 重写 `parse_data_info` 注入 `super_pts_path` |
| 4 | config | 新建或覆盖 | dataset/dataloader/类别表/evaluator/model 类别数 |
| 5 | 词表原型 | 重新生成 `*_cls_embedding.pth` | 球面分类按词表查原型（u4-l4） |

#### 4.3.3 源码精读

**(a) dataset 子类模板**——为什么只需 33 行：

[uniseg3d/scannet_dataset.py:18-33](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L18-L33) —— `parse_data_info` 在调父类前把 `super_pts_path` 按 `data_prefix['sp_pts_mask']` 拼成绝对路径；`text_info` 等额外键由父类原样透传。新数据集若路径布局不同，改这一个方法即可。

**(b) 官方预留的三个兼容旁路**——新数据集缺文本/伪掩码时的救命分支：

- [uniseg3d/transforms_3d.py:21-27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L21-L27) —— `text_info` 为空时 `TextPromptGeneration` 仍写入四个**空键**后返回。这保证了 `text_token` 键始终存在 → 注入/拆分照常执行（空提示分支）→ 对比损失里 `len(text_object_id)==0` 被 continue 跳过。**结论：无文本数据集的正确做法是把 `text_info` 置为空 dict、保留 transform，而不是删 transform**（删了会崩，见 4.4.3）。
- [uniseg3d/transforms_3d.py:122-128](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L122-L128) —— `PointPromptGeneration` 靠 `eval_ann_info` 探测两套实例 id 体系，无实例场景走 L157-158 的空旁路，不会触发注入处的 assert。
- [uniseg3d/transforms_3d.py:385-390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L385-L390) —— `merge_ov=True` 直接读 `input_dict['sam3d_pseudo_sp_masks']` 并写 `is_novel` 键。**新数据集没有 SAM3D 伪掩码时不能简单改 `merge_ov=False`**：config 的 Pack keys 显式含 `'is_novel'`（[configs:195](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L191-L199)），键不存在第②站就抛 `NotImplementedError`；即使删掉该键，损失总调度 [unified_criterion.py:38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L38) 仍会取 `insts[i].is_novel` 抛 `AttributeError`。要么为新数据生成伪掩码，要么同步改 Pack keys + 总调度 + `OVInstanceCriterion` 去掉 novel 路。

**(c) infos 生成的现实**：

[tools/indoor_converter.py:34-39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L34-L39) 与 [tools/indoor_converter.py:54-67](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L54-L67) —— 官方 pkl 的 dump 语句全部被注释，且 [L28-29](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L28-L29) 断言只支持 `scannet`/`scannet200`。所以新数据集的 infos 必须自己写生成脚本；此外官方 pkl 还携带本脚本不产出的 `text_info` 与 `sam3d_pseudo_sp_masks`（u1-l4），这两项新数据集要么自行构造、要么走上面的空旁路。

**(d) config 层的类别与词表联动**：

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:268-293](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L268-L293) —— `class_names` / `sem_mapping` / `inst_mapping` / `metric_meta` / evaluator 是一组联动配置，类别表一改全改；同时 `num_instance_classes` / `num_semantic_classes`（[L10-11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L10-L11)）控制模型分类维度，`vocabulary_cls_embedding_path`（[L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L66)）指向的 [C,P,D] 原型文件必须按新类别表重新生成（用仓库内嵌 open_clip 对新词表文本编码；官方未提供生成脚本，**待确认**其确切生成参数）。

#### 4.3.4 代码实践：源码阅读型——「最小 infos 字段表」

1. **实践目标**：为假想的「无文本、无伪掩码」新数据集写出一页最小 infos 字段表与必改代码点，训练前把联动风险全部列清。
2. **操作步骤**：
   - 对照 4.3.3 的三个旁路，列表回答：`text_info` 缺失时哪些键仍会被写入？`sam3d_pseudo_sp_masks` 缺失时哪些代码点会崩？
   - 用 mmengine 读官方 pkl 做对照实验（**示例代码**，**待本地验证**，需先下载官方 pkl）：
     ```python
     # 示例代码：检查一个场景的 infos 键构成
     import mmengine
     infos = mmengine.load('data/scannet/uniseg3d_infos_val.pkl')
     info = infos[0] if isinstance(infos, list) else infos
     print(sorted(info.keys()))
     # 再打印 info['text_info'] 的一个物体、info['super_pts_path']，
     # 对照 scannet_dataset.py 的 parse_data_info 确认每个键的消费方
     ```
   - 输出一张三列表格：infos 键 → 消费它的代码（文件:行）→ 新数据集缺失时的对策（空 dict 旁路 / 必改代码点 / 必须自行生成）。
3. **需要观察的现象**：`text_info` 的空 dict 旁路链路完整（transform → 注入空分支 → 损失 continue），而 `sam3d_pseudo_sp_masks` 没有空旁路——`merge_ov=True` 是唯一训练路径。
4. **预期结果**：得出结论——新数据集最小可行方案 = 空的 `text_info` + 必须提供的超点标签 + 三选一的伪掩码策略（自行生成 / 改造三处代码去掉 novel 路 / 先用 `merge_ov` 相关代码做兼容改造）。

#### 4.3.5 小练习与答案

**练习 1**：为什么超点标签是四类 bin 中唯一「无法绕过」的？

**答案**：模型没有可学习查询（`num_instance_queries=0`，u4-l3），查询直接来自 `extract_feat` 按超点 `scatter_mean` 聚合的特征（[uniseg3d/uniseg3d.py:375](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L375)）；GT 掩码、语义监督、点/文本提示真值也全部构建在超点坐标系上（u3-l3）。没有 `super_points`，整条「超点即查询」链路不存在。

**练习 2**：`scene_idxs` 配置项在新数据集上为什么失效？

**答案**：[uniseg3d/scannet_dataset.py:14-16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L14-L16) 的 `get_scene_idxs` 恒返回全量 `arange`，忽略传入参数（u3-l1）。想要按场景子集训练，需再重写此方法或直接截短 infos。

**练习 3**：类别数改了但 `vocabulary_cls_embedding_path` 没换，会发生什么？

**答案**：球面分类的类别维由原型张量的类别维决定（u4-l4），与 `num_instance_classes` 不一致会在解码器 `forward_logit` 的相似度矩阵处出现形状不匹配错误（或更隐蔽地错位分类），属于「启动晚、报错远」的典型配置联动问题。

---

### 4.4 损失权重、训练策略与六任务联动

#### 4.4.1 概念说明

UniSeg3D 是多任务联合训练：一次前向同时受 实例(base/novel) 、语义、点提示、文本提示、对比蒸馏 五路损失约束。调权重的意义在于：**任何一路的开关或权重都会同时影响六个任务的产出**，因为它们共享同一组查询与同一个解码器。理解联动前先记住总调度的调用顺序：

[uniseg3d/unified_criterion.py:65-74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74) —— 五路损失按 实例 → 语义 → 点提示 → 文本提示 → 对比 顺序调用并 `dict.update` 合并；提示路由 `point_prompt_pred is not None` 守护。

#### 4.4.2 核心流程：权重旋钮地图

对照 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:13-18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L13-L18) 与 [L67-124](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L67-L124)：

| 旋钮 | 位置 | 作用 / 联动 |
| --- | --- | --- |
| `sem_weight` / `inst_weight` | L15-16 | 分别缩放语义损失权重（L72 `0.2*sem_weight`）与实例损失权重（L92）——语义/实例任务的总平衡 |
| `inst_criterion.loss_weight` 六元组 | L92 | base 路各项 + novel 掩码 0.1 + …（u5-l3），改动直接影响实例与全景任务 |
| `hype_lambda` | L17 | 缩放蒸馏权重 `IntrinsicPointAndQuery_loss_weight=0.1*hype_lambda`（L98）：点提示知识向常规查询的迁移强度，调零≈关闭该蒸馏 |
| `contra_hype_lambda` | L18 | 同时缩放对比、排序、伪分布三个权重（L122-124）：跨任务知识共享总开关 |
| `total_weight=0.1` | L108/L117 | 点/文本提示实例损失的总缩放，调零≈提示分支只前向不受监督 |
| `use_cls_supervise` | L116 | 文本查询是否对齐词表分类空间（u5-l5），关闭后文本分支只学掩码 |
| `IoU_loss_weight` / `ratio` / `iou_thr` | L91/L90/L97 | IoU 分支监督强度、novel 掩码权重、低质匹配过滤（u5-l4） |

**一个重要的配置陷阱**：`hype_lambda` 等顶层变量是配置文件的 **Python 变量**，在文件执行时已参与求值（`0.1*hype_lambda` 已算成具体数字进 `model` dict）。因此 `--cfg-options hype_lambda=0.0` 只会改 cfg 里的孤立变量值，**不会**回溯影响 `model.criterion.*` 里已求值的权重。想用命令行调权，必须覆盖最终键路径，如 `--cfg-options model.criterion.contrastive_criterion.contrastive_loss_weight=0.0`。

#### 4.4.3 源码精读：一个已确认的联动陷阱——删提示 transform 会崩

如果想「关掉文本任务」而从 pipeline 里删除 `TextPromptGeneration`：

- `loss()` 探测不到 `text_token`（[uniseg3d/uniseg3d.py:467](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L467)）→ `text_prompt_outputs` 保持 `None`；
- 总调度的提示分支被 `is not None` 守住（[unified_criterion.py:71-72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L71-L72)），看似安全；
- **但**对比损失是无条件调用的（[unified_criterion.py:74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L74)），而它的第一行访问就是：

[uniseg3d/contrastive_criterion.py:50-53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L50-L53) —— 直接下标 `point_prompt_pred['contras_embeds'][i]` 与 `text_prompt_pred['contras_embeds'][i]`，**没有 None 旁路**。传入 None 立即 `TypeError: 'NoneType' object is not subscriptable`。

正确姿势（两选一）：

1. 保留 transform，让数据侧 `text_info` 为空 dict 走 4.3.3 的空键旁路（对比损失里 `len(text_object_id)==0` 被 [contrastive_criterion.py:57-59](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L57-L59) continue，损失输出 0）。
2. 真要删 transform，必须同时给 `ContrastiveCriterion.__call__` 加 None 守护。

这类「消费方比生产方多一个且无守护」的结构，是评估六任务联动影响时要逐个排查的模式。

#### 4.4.4 代码实践：命令行调权 dry-run

1. **实践目标**：验证「顶层变量覆盖无效、最终键路径覆盖有效」，并观察损失键的变化。
2. **操作步骤**（**待本地验证**，需数据与 GPU；无数据时可退化为阅读型——在 train.py 打印 `cfg.model.criterion` 对比两种覆盖）：
   ```bash
   # A: 覆盖顶层变量（预期无效）
   ./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 \
       --cfg-options contra_hype_lambda=0.0 train_cfg.max_epochs=1 train_dataloader.batch_size=1
   # B: 覆盖最终键路径（预期有效）
   ./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 \
       --cfg-options model.criterion.contrastive_criterion.contrastive_loss_weight=0.0 \
       model.criterion.text_prompt_inst_criterion.use_cls_supervise=False \
       train_cfg.max_epochs=1 train_dataloader.batch_size=1
   ```
3. **需要观察的现象**：两次运行第 50 iter 日志的 loss 键值；A 的 `contrastive_loss` 应与基线同量级（变量覆盖未生效），B 的 `contrastive_loss` 应为 0、`text_prompt_inst_loss` 数值变化（cls 监督关闭）。
4. **预期结果**：确认命令行调权必须走 `model.criterion.*` 键路径；同时从日志核对 u5-l1 总结的 10 个 loss key 是否齐全。

#### 4.4.5 小练习与答案

**练习 1**：把 `total_weight`（点提示 criterion）调为 0，六任务里哪些指标大概率变化、为什么？

**答案**：交互式分割（点提示 AP）会明显退化——提示分支失去监督；同时实例/全景/指代任务也可能受影响，因为点提示输出还通过 `IntrinsicPointAndQuery_loss`（蒸馏给常规查询）和对比损失（与文本互迁移）反哺主任务；权重调零等于切断这两条知识流。

**练习 2**：`use_pseudo_cls_supervise`（L14/L121）控制什么？关掉它损失字典会少哪个键？

**答案**：控制伪分布蒸馏——把点提示的分类分布 BCE 蒸馏给文本分支（u5-l6）。关掉后 [contrastive_criterion.py:113-114](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L113-L114) 不再输出 `pseudo_ditribute_loss` 键。

**练习 3**：为什么`--cfg-options` 改不了 `hype_lambda` 的效果？用 mmengine 的机制解释。

**答案**：`--cfg-options` 作用在 `Config.fromfile` **执行之后**的最终 cfg dict 上；而配置文件是 Python，`0.1*hype_lambda` 在文件执行那一刻就已求值为字面数字写进 `model` dict。变量与 model dict 之间的引用关系在执行后即断裂，覆盖孤立变量无法传播。

---

## 5. 综合实践：把「框提示」完整落地

把 4.2 的 Level 1 → Level 2 串联成一个完整的小项目，全部改动只新增文件、不修改 `uniseg3d/` 原有逻辑（Level 2 的 `loss()`/`predict()` 插桩除外）：

1. **Day 1：Level 1 跑通**。新建 `BoxPromptGeneration`（复用 point 键名）+ 注册 + config 替换，跑一个 iter 训练确认 loss 键齐全；再跑 `tools/test.py` 确认五任务指标与基线一致（提示来源变化不应影响指标，只影响交互式任务的选点策略）。
2. **Day 2：语义升级**。把「球内最近点」改为「圈选超点集合均值特征」（4.2.4 Level 2 第 5 步），此时必须走独立键名四件套，按清单 1-10 逐项实施，每改一处就在 4.1.3 的消费地图上打勾。
3. **Day 3：对称性验收**。对照检查表逐项验证：
   - 三站白名单（formatting / data_preprocessor / config Pack keys）三处键名一致；
   - 注入顺序与拆分顺序严格镜像；
   - `predict()` 的注入/拆分与 `loss()` 对称（否则测试时提示查询污染原始输出）；
   - 无实例场景走空提示分支不触发 assert；
   - 空提示场景下新 criterion 不除零（`fix_mean_loss` 的语义，u5-l5）；
   - `type_embedding` 扩容后 resume 旧 checkpoint 的形状兼容性。
4. **验收标准**：训练日志出现新损失键且量级合理（参考点提示损失的数量级）；测试集五任务常规指标不劣化；若实现了评估回写，还能报告框提示的 IoU/precision（仿 u6-l2 的提示任务指标）。

本环境无数据与 GPU，以上运行结果均为**待本地验证**；但清单中的每一处改动点都直接对应本讲引用的真实源码行。

## 6. 本讲小结

- UniSeg3D 的扩展点分四类：新提示类型、替换骨干/解码器、接入新数据集、调整损失权重；每一类都有明确的接口契约（骨干返回 `(SparseConvTensor, ...)` 且 32 通道、解码器 `forward(x, queries, interaction_masks)` 返回固定键 dict、提示三键 + 同序 GT）。
- 「配置即代码 + 注册器」是即插即用的根源：新类注册后配置改一个 `type` 即可替换；但签名兼容是运行时才检查的口头约定，必须先冒烟。
- 新提示四件套 = transform + 注入 + 拆分 + 损失；「模型只认键名」提供了零侵入捷径（复用 point 通道），完整方案则要动 8-9 处文件；interaction_masks 与恒等匹配损失对新提示免费。
- 尾部链式规则：注入按序追加、拆分必须反向镜像；训练与推理的注入/拆分必须对称，否则测试时提示查询污染五任务输出。
- 三站通道（transform → Pack → Preprocessor）+ config keys 是事实上的四处白名单，任何一处键名不符分别导致跳过、报错、静默断流、不打包。
- 新数据集四层接入：四类 bin（超点必须用 segmentator 生成）+ infos（官方 dump 已注释、`text_info` 可空 dict 走旁路、`sam3d_pseudo_sp_masks` 无旁路）+ dataset 子类（33 行模板）+ config/词表联动。
- 六任务联动有真实陷阱：删提示 transform 会让无 None 旁路的 `ContrastiveCriterion` 崩溃；`merge_ov=False` 会连锁触发 Pack 与总调度的键缺失；命令行调权必须走 `model.criterion.*` 最终键路径。

## 7. 下一步学习建议

至此全书六单元讲义已完结。建议三条继续深入的路：

1. **动手路线**：按第 5 节把框提示做完整，再挑战更有价值的提示类型（如「掩码提示」——给定粗掩码取其覆盖超点的均值特征，语义上最接近 SAM 的交互范式）；每做一步回到 4.1.3 的消费地图核对。
2. **迁移路线**：把 UniSeg3D 接到一个新数据集（如 ScanNet200 之外的自采数据），完整走一遍 4.3 清单，重点解决伪掩码缺失时的 novel 路改造，这会逼你把 u5-l3 的 base/novel 双路监督真正读透。
3. **研究路线**：对照论文实验复现消融——用 4.4 的权重旋钮逐路关闭损失（对比、排序、伪分布、蒸馏），观察六任务指标的此消彼长，验证「跨任务知识共享」的贡献；同时关注仓库上游（oneformer3d、Open3DIS、SAM3D）的接口演化，评估本框架与更新的基础模型对接的可能性。
