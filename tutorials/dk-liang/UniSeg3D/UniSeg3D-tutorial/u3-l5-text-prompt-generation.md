# 指代分割的数据侧：文本提示生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `text_info`（ScanRefer 描述 + 离线分词结果）在 infos 中的结构，以及训练与测试分别消费它的哪些字段。
2. 讲解 `TextPromptGeneration` 如何为每个被描述物体随机挑一条描述，并用**行拷贝**的方式从 `gt_sp_masks` 直接拿到超点级真值。
3. 讲解 `TextPromptTest` 如何把「每物体 × 每条描述」全量展开，并用**点级 one-hot + `scatter_mean` + 0.5 阈值**把真值投影到超点。
4. 掌握 `text_token / label_text / gt_text_prompt / text_object_id` 四个键的语义与对齐关系，以及它们经 transform → Pack → Preprocessor「三站通道」进入模型的完整闭环。
5. 独立运行并验证：`gt_text_prompt` 每行覆盖的超点集合与该实例点集触及的超点集合满足「子集，且纯超点时相等」。

## 2. 前置知识

### 2.1 什么是指代分割与「文本提示」

指代分割（referring segmentation）的输入不是类别名，而是一句**自然语言描述**，例如：

> "the brown wooden chair next to the sofa"（沙发旁边那把棕色木椅）

模型要做的，是把这句话**指向的那一个物体**从场景里分割出来。它与交互式分割（u3-l4 的点提示）互为镜像：一个用「点」指定物体，一个用「话」指定物体。在 UniSeg3D 的统一框架里，两者共享同一条前向——描述先被语言模型编码成一个 32 维向量，然后作为一条**文本提示查询**拼进超点查询序列的尾部，与普通查询一起过同一个 Transformer 解码器。

### 2.2 你需要带着进来的旧知识（承接前几讲）

- **u3-l2 的三站通道**：transform 生成键 → `Pack3DDetInputs_` 按 `INPUTS_KEYS` 清单分拣进 `inputs` → `Det3DDataPreprocessor_` 白名单透传进 `batch_inputs`。三站键名必须一字不差。
- **u3-l3 的行布局**：`AddSuperPointAnnotations` 产出的 `gt_sp_masks` 前 K 行是实例行（K = 清洗后的物体数），随后是语义行；`pts_instance_objextId` 是与这 K 行**逐行对齐**的原始 objectId 向量（`idxs[1:] - 1`），它与 ScanRefer 标注里的 `object_id` 同一命名空间。
- **u3-l4 的点提示**：点提示由 `point_prompts / point_prompt_instance_ids / point_prompt_sp_ids` 三个逐条对齐的键描述；本讲的文本提示是同构的四键版本，只是多了一个 `text_token`。
- **u1-l4**：`text_info` 不在仓库内生成，而是随官方 Release 的 `uniseg3d_infos_train/val.pkl` 下发，其中 `text_token` 是**离线**用 OpenCLIP 分词器算好的 token id 序列——训练时不再分词。

### 2.3 ScanRefer 是什么

ScanRefer 是建立在 ScanNet 之上的指代分割数据集：对每个 ScanNet 场景，标注者针对场景中的**things 类物体**写下多句描述，每句描述绑定一个 `object_id`。因此一个场景的文本标注天然是「按物体分组、每组若干句」的两层结构。UniSeg3D 把它原样存进 infos 的 `text_info` 字段，一个物体 id 对应一个 dict，里面同时放着原文、分词、类别和点下标。

## 3. 本讲源码地图

| 位置 | 作用 |
| --- | --- |
| [uniseg3d/transforms_3d.py:L12-L60](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12-L60) | `TextPromptGeneration`：训练侧文本提示生成 |
| [uniseg3d/transforms_3d.py:L63-L109](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L63-L109) | `TextPromptTest`：测试侧全量展开 + 点级投影 |
| [uniseg3d/transforms_3d.py:L348-L363](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L348-L363) | 上游 `AddSuperPointAnnotations` 产出 `pts_instance_objextId` 与重映射后的实例掩码（u3-l3 已精读） |
| [uniseg3d/transforms_3d.py:L374-L390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L374-L390) | 实例行 `sp_masks_inst` 的聚合与 `insts` 行索引向量 |
| [uniseg3d/lang_module.py:L38-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L38-L71) | `LangModule`：`text_token` 的模型侧消费者 |
| [uniseg3d/uniseg3d.py:L655-L683](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L683) | `add_text_prompt_queries`：文本嵌入注入查询尾部 + 组装提示真值 |
| [uniseg3d/uniseg3d.py:L520-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L520-L585) | `split_original_and_text_prompt_queries`：把解码输出切回「原始查询 / 文本提示查询」 |
| [uniseg3d/uniseg3d.py:L465-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L491) | 注入顺序（先点后文）与拆分顺序（先文后点） |
| [uniseg3d/formatting.py:L15-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18) | 四个文本键在 `INPUTS_KEYS` 清单里 |
| [uniseg3d/data_preprocessor.py:L85-L95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L85-L95) | 四个文本键的白名单透传 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L171-L184](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L171-L184) | 训练管线中的调用位置（紧跟 `AddSuperPointAnnotations`） |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L226-L243](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L226-L243) | 测试管线中的调用位置与 `Pack3DDetInputs_` 键清单 |

## 4. 核心概念与源码讲解

### 4.1 text_info 的结构与训练侧取样逻辑

#### 4.1.1 概念说明

`text_info` 是 infos 里挂在每个场景上的一个 dict，**键是物体 id 的字符串**，值是该物体的文本标注包。从两个 transform 实际读取的字段可以反推出它的结构（示例代码）：

```python
text_info = {
    "2": {                          # 键 = ScanRefer 的 object_id（字符串形式的原始 objectId）
        "text": ["a brown chair", "the chair near the desk"],  # 该物体的多条描述
        "text_token": [tok1, tok2], # 与 text 逐条对齐的离线 OpenCLIP 分词结果
        "label_id": 3,              # 该物体的类别标签
        "pts_id": array([...]),     # 该物体覆盖的点下标（TextPromptTest 消费）
    },
    "6": {...},
}
```

训练侧的 `TextPromptGeneration` 只消费 `text / text_token / label_id` 三个字段；`pts_id` 只有测试侧用。仓库内没有生成 `text_info` 的代码（u1-l4 已确认 infos 必须从官方 Release 下载），所以上述结构是从消费端代码读出来的；`label_id` 的取值空间见 4.1.3 的推断。

为什么训练时要做「取样」而不是全用？因为一个物体可能被描述了五六次，如果每条都进批次，文本提示查询的数量会远超普通查询，训练信号被指代任务主导。于是训练时**每个物体只随机抽一条描述**，这既是数量控制，也是一种数据增广——不同 epoch 同一物体配的是不同的话。

#### 4.1.2 核心流程

`TextPromptGeneration.transform` 的流程：

1. `text_info` 为空 → 四个输出键全置空 `np.array`，直接返回（很多场景没有 ScanRefer 标注）。
2. 取全部物体 id 键；`random_select=False`（配置取值）时 `num_ins = 物体数`，`True` 时随机抽 1..物体数 个。
3. `random.sample` 选出 `num_ins` 个物体——**即使全取，顺序也是随机的**。
4. 对每个选中物体：随机挑 1 条描述 → 取它的 `text_token`；记下 `label_id`；把字符串键 `eval` 成 int 得到 `text_object_id`。
5. 在 `pts_instance_objextId` 里查这个 objectId 出现的下标——该下标正是 `gt_sp_masks` 实例区的**行号**，直接整行拷贝为 `gt_text_prompt` 的一行。
6. 写回四个键：`label_text / gt_text_prompt / text_object_id / text_token`。

#### 4.1.3 源码精读

先看类定义与构造参数（[uniseg3d/transforms_3d.py:L12-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12-L18)）：

```python
@TRANSFORMS.register_module()
class TextPromptGeneration(BaseTransform):
    def __init__(self, num_ins = 3, random_select=False, seq_length=126, embedding_dim=300):
        self.num_ins = num_ins
        self.seq_length = seq_length
        ...
```

一个值得注意的事实：`num_ins / seq_length / embedding_dim` 三个参数被存到 `self` 上，但在 `transform` 中**从未被使用**（L20-L60 只引用了 `self.random_select`）。配置里传的 `num_ins=3, seq_length=126, embedding_dim=300`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L177-L181](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L177-L181)）因此是**无效的历史遗留参数**，实际提示数量由 `random_select` 分支决定。读源码时要能识别这种「配置写了但不生效」的陷阱。

主流程（[uniseg3d/transforms_3d.py:L29-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L29-L41)）：

```python
text_infos = input_dict['text_info']
text_infos_id = list(text_infos)            # 所有物体 id 键（字符串）
if self.random_select:
    num_ins = np.random.randint(len(text_infos_id), size=1)[0] + 1
else:
    num_ins = len(text_infos_id)            # 配置走这里：全取
select_texts_infos_id = random.sample(text_infos_id, num_ins)   # 全取但乱序
gt_sp_masks = torch.as_tensor(input_dict['gt_sp_masks'])
gt_text_prompt = torch.zeros((num_ins, gt_sp_masks.shape[1]))   # (物体数, 超点数)
pts_instance_objextId = input_dict['pts_instance_objextId']
```

逐物体的取样与行拷贝（[uniseg3d/transforms_3d.py:L43-L53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L43-L53)）：

```python
for i, select_texts_info_id in enumerate(select_texts_infos_id):
    id = random.sample(range(len(text_infos[select_texts_info_id]['text'])), 1)[0]
    token = text_infos[select_texts_info_id]['text_token'][id]   # 随机挑一条描述的分词
    text_token.append(token)
    label_text[i] = text_infos[select_texts_info_id]['label_id']
    text_object_id[i] = eval(select_texts_info_id)               # "6" -> 6
    object_id = torch.tensor(eval(select_texts_info_id))
    id = torch.where(pts_instance_objextId == object_id)[0]       # objectId -> 实例行号
    assert len(id)>0
    gt_text_prompt[i] = gt_sp_masks[id[0]]                        # 整行拷贝超点掩码
```

三个关键点：

1. **`eval` 的用法**。`text_info` 的键是字符串 `"6"`，`eval` 把它变成整数 6。这能工作，但 `eval` 会执行任意表达式——若 pkl 被篡改是安全隐患。更稳妥的写法是 `int(select_texts_info_id)`；读源码时应意识到这是一处可改进的写法。
2. **`text_object_id` → 行号的桥梁**。u3-l3 里 `AddSuperPointAnnotations` 同时产出了两个逐行对齐的向量（[uniseg3d/transforms_3d.py:L355-L357](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L355-L357) 的 `pts_instance_objextId = idxs[1:] - 1` 与 [uniseg3d/transforms_3d.py:L384](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L384) 的 `insts = new_idxs[1:] - 1`，两者是同一个向量），因此「`gt_sp_masks` 实例区第 j 行」对应「`pts_instance_objextId[j]` 这个原始 objectId」。`torch.where(...)` 做的就是这个 id → 行号的翻译。
3. **`assert len(id)>0` 是一道安全阀**。如果某句 ScanRefer 描述绑定的物体在 `AddSuperPointAnnotations` 里被清洗掉了（比如它的语义类是 stuff、或实例没有点），这里会直接断言失败、训练崩溃。官方 pkl 应已做过过滤（待本地验证），但自己接入新数据集时这是一个高频踩坑点。

`label_id` 的类别空间可以从损失侧反推：`TextPromptInstanceCriterion` 把 `labels_3d`（即 `label_text`）直接填进大小为 `cls_pred.shape[1]-1 = num_instance_classes = 18` 的 `cls_target`（[uniseg3d/text_prompt_instance_criterion.py:L161-L167](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L161-L167)，配置 `num_instance_classes=18` 见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L10)）。所以 `label_id` 必须落在 **0..17 的实例类别空间**，等价于 u3-l3 里的 `语义 id − 2`（如语义类 2「cabinet」→ 0）。

最后看空分支（[uniseg3d/transforms_3d.py:L21-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L21-L27)）：没有 ScanRefer 标注的场景照样产出四个空键——键存在但值为空数组。下游 `Pack` 与 `Preprocessor` 仍会放行它们，模型侧靠 `len(...)==0` 走空分支，这是「批次内混合有无描述场景」的关键设计。

#### 4.1.4 代码实践：观察取样行为

实践目标：直观感受「全取但乱序」「每物体随机一条描述」两个随机性。

操作步骤（示例代码，需在 u1-l2 搭好的环境中运行，因为 `transforms_3d.py` 顶层 import 了 MinkowskiEngine 等依赖）：

```python
# text_sample.py —— 放在仓库外任意目录，运行前 export PYTHONPATH=仓库根目录
import numpy as np, random
from uniseg3d.transforms_3d import TextPromptGeneration

random.seed(0); np.random.seed(0)
text_info = {
    "2": {"text": ["a brown chair", "wooden chair", "chair near desk"],
           "text_token": [np.arange(5), np.arange(5), np.arange(5)], "label_id": 3},
    "6": {"text": ["white cabinet", "cabinet by the wall"],
           "text_token": [np.arange(5), np.arange(5)], "label_id": 0},
}
tpg = TextPromptGeneration()
for trial in range(3):
    out = tpg.transform({"text_info": text_info,
                         "gt_sp_masks": np.zeros((23, 12), dtype=bool),
                         "pts_instance_objextId": np.array([2, 6])})
    print(trial, "object_id:", out["text_object_id"], "labels:", out["label_text"])
```

需要观察的现象：

1. 每轮 `text_object_id` 都是 `[2, 6]` 或 `[6, 2]`——元素齐全但**顺序在变**（`random.sample` 的效果）。
2. 把实例换成有 3 条描述的物体反复跑，`text_token` 每次取到的下标会变（每物体随机 1 条）。
3. 把 `TextPromptGeneration(random_select=True)` 再跑几轮，观察物体数在 1..2 之间随机。

预期结果：输出键形状为 `label_text (2,)`、`text_object_id (2,)`、`gt_text_prompt (2, 12)`、`text_token` 为含 2 个元素的 object 数组。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：配置里 `num_ins=3` 会限制每场景最多 3 条文本提示吗？

答：不会。`num_ins` 存到 `self` 后从未被 `transform` 引用，实际数量由 `random_select` 决定：`False`（本项目配置）时取全部被描述物体，`True` 时随机取 1..物体数 个。

**练习 2**：为什么 `TextPromptGeneration` 在管线里必须排在 `AddSuperPointAnnotations` 之后？

答：它读取 `gt_sp_masks` 与 `pts_instance_objextId`，两者都是 `AddSuperPointAnnotations` 的产物。配置中的顺序也如此（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L171-L181](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L171-L181)）。排反了会直接 `KeyError`。

**练习 3**：`text_object_id` 为什么要保留原始 objectId，而不是直接存行号？

答：因为它还要充当**跨任务对齐的钥匙**——`ContrastiveCriterion` 靠它把「描述同一物体的文本提示与点提示」配成正样本对（[uniseg3d/contrastive_criterion.py:L67](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L67)）。点提示侧的 `pts_instance_objextId_shuffle` 也是同一命名空间（u3-l4），两边都存原始 id 才能对上。

### 4.2 gt_text_prompt 的超点投影：训练「行拷贝」 vs 测试「点级多数投票」

#### 4.2.1 概念说明

`gt_text_prompt` 的语义是：第 i 条文本提示应当分割出的**超点集合**，形状 `(提示数, 超点数)`，取值 0/1。问题是——真值从哪来？两条路径给出了两种答案：

- **训练（行拷贝）**：`AddSuperPointAnnotations` 已经把点级 GT 投影成超点级 `gt_sp_masks`，直接把对应实例行整行拷过来即可，零额外计算。
- **测试（点级多数投票）**：从 `text_info` 自带的 `pts_id`（物体覆盖的点下标）出发，在**点级**铺 one-hot，再用 `scatter_mean + >0.5` 投影到超点——与 u3-l3 完全相同的多数投票数学：

\[ \text{gt}_{t,s} \;=\; \mathbb{1}\!\left[\;\frac{1}{|P_s|}\sum_{p \in P_s} m_{t,p} \;>\; 0.5\;\right] \]

其中 \(P_s\) 是超点 s 的点集合，\(m_{t,p}\in\{0,1\}\) 表示第 t 条描述的目标物体是否覆盖点 p。直觉解释：**一个超点归给谁，由超点内点的多数投票决定**——超过一半的点属于该物体才判 True。

为什么测试不复用行拷贝？一个从代码可观察的理由：测试管线的 `AddSuperPointAnnotations` 不带 `merge_ov`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L226-L230](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L226-L230)），行布局与训练不同；而 `text_info` 自带 `pts_id`，让 `TextPromptTest` 自包含、不依赖任何行序约定，鲁棒性更好。这是分析，不是源码注释里写的理由（待确认）。

#### 4.2.2 核心流程

训练路径（上一节已精读）：

```
objectId(字符串) --eval--> int
        |--(A)--> 在 pts_instance_objextId 中查下标 = 实例行号 --> gt_sp_masks 整行拷贝
        └--(B)--> 存入 text_object_id（供跨任务对齐）
```

测试路径：

```
对每条描述 t:
    m_t = 长度为 N 的 0/1 向量, m_t[pts_id] = 1        # 点级 one-hot
堆叠成 (T, N)
scatter_mean(m, sp_pts_mask, dim=-1)  --> (T, S)       # 每个超点内取均值
> 0.5                                                 # 多数投票二值化
```

两条路径产出的行内容一致（同为多数投票语义），但有三个工程差异：

| 差异点 | 训练 `TextPromptGeneration` | 测试 `TextPromptTest` |
| --- | --- | --- |
| 真值来源 | `gt_sp_masks` 实例行（间接） | `text_info['pts_id']`（直接，点级） |
| 每物体描述数 | 1 条（随机） | 全部描述 |
| dtype | float32 的 0./1. | bool |

dtype 差异由模型侧统一抹平：[uniseg3d/uniseg3d.py:L672](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L672) 做了 `.clone().bool()`。

#### 4.2.3 源码精读

训练侧行拷贝见 [uniseg3d/transforms_3d.py:L50-L53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L50-L53)（上节已逐行注释）。测试侧的点级投影（[uniseg3d/transforms_3d.py:L88-L102](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L88-L102)）：

```python
for i, select_texts_info_id in enumerate(select_texts_infos_id):
    for id in range(len(text_infos[select_texts_info_id]['text'])):  # 每条描述一行
        token = text_infos[select_texts_info_id]['text_token'][id]
        text_token.append(token)
        ...
        pts_id = text_infos[select_texts_info_id]['pts_id']         # 点级下标
        gt = torch.zeros(sp_pts_mask.shape[0])
        gt[pts_id] = 1.                                             # 点级 one-hot
        gt_text_prompt.append(gt)

gt_text_prompt = torch.stack(gt_text_prompt, dim=0)                 # (T, N)
gt_text_prompt = scatter_mean(gt_text_prompt, sp_pts_mask, dim=-1)  # (T, S)
gt_text_prompt = gt_text_prompt > 0.5                               # 多数投票
```

对照 u3-l3 的 [uniseg3d/transforms_3d.py:L376-L379](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L376-L379)：`scatter_mean(insts_mask.float(), sp_pts_mask, dim=-1)` 再 `> 0.5`——**同一套聚合数学被三处复用**（实例行、语义行、文本提示行），这是理解「超点即查询」框架的关键一致性。

由此可推出一条**覆盖不变式**：设某物体的点集触及的超点集合为 \(\mathcal{T} = \{\,s \mid \exists p \in P_s,\ m_p = 1\,\}\)，多数投票胜出的超点集合为 \(\mathcal{G}\)，则恒有 \(\mathcal{G} \subseteq \mathcal{T}\)（多数票胜出的超点必然含该物体的点）；当且仅当**没有任何跨界超点**（该物体在该超点中不占多数）时取等号。真实数据里超点由几何过分割产生，物体边界处的超点常被背景占多数，所以严格相等并不总成立——这正是下面实践要验证的性质。

#### 4.2.4 代码实践：手工复现多数投票并验证覆盖不变式

实践目标：用 1 个物体 + 3 个超点的小例子，亲手算一遍 `scatter_mean > 0.5`，体感「子集 vs 相等」。

操作步骤（示例代码，只需 numpy + torch）：

```python
# vote_check.py
import numpy as np, torch
from torch_scatter import scatter_mean

sp_pts_mask = torch.tensor([0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2])  # 3 个超点
pts_id      = torch.tensor([0, 1, 2, 3, 5])    # 物体的点：超点0 的 5 个点里占 4 个，超点1 的 5 个里占 1 个

m = torch.zeros(sp_pts_mask.shape[0]); m[pts_id] = 1.
g = scatter_mean(m, sp_pts_mask, dim=-1)
print("每超点均值:", g)          # 预期 [0.8, 0.2, 0.0]
print("投票结果:", g > 0.5)      # 预期 [True, False, False]

touched = set(np.unique(sp_pts_mask[pts_id].numpy()))   # {0, 1}
won     = set(torch.where(g > 0.5)[0].numpy().tolist()) # {0}
print("触及超点:", touched, " 胜出超点:", won, " 子集成立:", won <= touched)
```

需要观察的现象：超点 0（物体占 4/5）胜出，超点 1（物体占 1/5）被多数投票淘汰——它**被触及但未胜出**，因此 `won` 是 `touched` 的真子集。

预期结果：`每超点均值: tensor([0.8000, 0.2000, 0.0000])`，子集关系成立。待本地验证（`torch_scatter` 装好后即可跑）。

#### 4.2.5 小练习与答案

**练习 1**：某超点共 100 个点，其中 60 个属于目标物体、40 个属于背景，`gt_text_prompt` 在该超点上的取值是多少？训练与测试路径会一致吗？

答：均值为 0.6 > 0.5，取 True。一致——训练路径的 `gt_sp_masks` 实例行也是同一多数投票算出来的（u3-l3），两条路径只是「谁来做投票」不同。

**练习 2**：训练的 `gt_text_prompt` 是 float、测试是 bool，模型侧会因此出问题吗？

答：不会。[uniseg3d/uniseg3d.py:L672](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L672) 统一 `.clone().bool()` 归一了 dtype。

**练习 3**：若把 0.5 阈值改成 0.3，超点掩码会变「胖」还是变「瘦」？对训练意味着什么？

答：变胖——原本物体只占 30%~50% 的跨界超点也会被判 True，掩码边界扩张、引入噪声监督。源码中实例/语义/文本三处都用严格 `> 0.5`，保证「每列至多一个 True」（u3-l3），改阈值会破坏多数投票的中立性。这是思想实验，源码未提供该开关。

### 4.3 TextPromptTest 的全量展开与模型侧消费闭环

#### 4.3.1 概念说明

训练时每个物体只出 1 条提示；测试时则要**每物体 × 每条描述**全量展开——场景里有 3 个被描述物体、共 7 句描述，就生成 7 条文本提示查询，一次前向同时输出 7 个掩码。原因在评估：指代分割基准对**每条 referring expression** 都打分（u6-l2 的 AP@25/50 与 IoU），漏掉任何一句都无法完整评估。于是「全量展开 + 单次前向」既是效率选择，也是任务定义的要求。

注意一个细节：同一物体的多条描述会生成**内容相同的多行** `gt_text_prompt`（都从同一个 `pts_id` 投影而来），但它们是各自独立的行——因为每条描述对应一条独立的提示查询与独立的预测。

#### 4.3.2 核心流程

```
TextPromptTest (transform)
  双层循环: 物体 id × 描述 → 每条描述产出 (token, label_id, objectId, 点级 gt)
  点级 gt 堆叠 (T, N) → scatter_mean → (T, S) → >0.5
        │  写入 label_text / gt_text_prompt / text_object_id / text_token
        ▼
Pack3DDetInputs_   按 INPUTS_KEYS 分拣四键进 inputs        (formatting.py)
        ▼
Det3DDataPreprocessor_  白名单透传四键进 batch_inputs      (data_preprocessor.py)
        ▼
UniSeg3D.loss / predict
  add_text_prompt_queries:  self.lang(text_token) → (T_i, 32) 嵌入
                            + type_embedding[0]（文本类型标记）
                            cat 到查询序列最尾部
                            同时组装提示真值 InstanceData_(labels_3d / sp_masks / text_object_id)
        ▼
  解码器一次前向 → split_original_and_text_prompt_queries
                            用 len(gt_text_prompt) 计数，[-n:] 切文本提示输出，[:-n] 切原始输出
```

#### 4.3.3 源码精读

全量展开的双层循环见 [uniseg3d/transforms_3d.py:L88-L98](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L88-L98)（4.2.3 已引用），四个输出键写回在 [uniseg3d/transforms_3d.py:L104-L107](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L104-L107)。配置侧的调用位置在 `MultiScaleFlipAug3D` 的内部 transform 列表里，紧跟测试版 `AddSuperPointAnnotations`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L226-L237](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L226-L237)）。

**三站透传**。第一站：四键在 `INPUTS_KEYS` 清单（[uniseg3d/formatting.py:L15-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18)）：

```python
INPUTS_KEYS = ['points', 'img', 'elastic_coords',
               'point_prompts', ..., 'emb_text',
               'label_text', 'gt_text_prompt', 'text_token', 'text_object_id', ...]
```

第二站是配置里的 Pack 键清单（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L239-L243](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L239-L243)）。第三站：`Det3DDataPreprocessor_` 逐键 `if` 透传（[uniseg3d/data_preprocessor.py:L85-L95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L85-L95)）：

```python
if 'label_text' in inputs:      batch_inputs['label_text'] = inputs['label_text']
if 'gt_text_prompt' in inputs:  batch_inputs['gt_text_prompt'] = inputs['gt_text_prompt']
if 'text_token' in inputs:      batch_inputs['text_token'] = inputs['text_token']
if 'text_object_id' in inputs:  batch_inputs['text_object_id'] = inputs['text_object_id']
```

注意这是**白名单 + if 探测**：任何一站改了名，后面不是报错而是**静默断流**——模型侧 [uniseg3d/uniseg3d.py:L467](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L467) 的 `'text_token' in batch_inputs_dict` 不成立，文本分支整个被跳过，训练照常进行、只是指代任务的监督凭空消失。这是比崩溃更危险的失败模式，排障时应先核对三站键名。

**模型侧注入**（[uniseg3d/uniseg3d.py:L655-L683](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L683)）：

```python
batch_text_prompts = self.lang(batch_inputs_dict['text_token'])   # 每样本: (T_i, 32) 文本嵌入
for i, cur_text_prompts in enumerate(batch_text_prompts):
    if len(cur_text_prompts) > 0:
        cur_text_prompts = cur_text_prompts + self.type_embedding.weight[0]  # 文本类型标记
        queries[i] = torch.cat((queries[i], cur_text_prompts), 0)            # 拼到查询尾部
        text_prompt_gt_instances[i].labels_3d      = batch_label_text[i].clone().long()
        text_prompt_gt_instances[i].sp_masks       = batch_gt_text_prompts[i].clone().bool()
        text_prompt_gt_instances[i].text_object_id = batch_inputs_dict['text_object_id'][i]
```

两个要点：

1. `LangModule.forward`（[uniseg3d/lang_module.py:L59-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L59-L71)）逐样本循环：空样本（无描述场景）直接跳过，非空样本把 token 序列交给冻结的 OpenCLIP 文本塔得到 768 维特征，再过 `Mlp` 投到 32 维查询空间（塔的构建与冻结见 [uniseg3d/lang_module.py:L51-L56](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L51-L56)，细节 u4-l5 展开）。
2. 注入顺序是**先点提示、后文本提示**（[uniseg3d/uniseg3d.py:L465-L468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L468)），因此文本提示位于查询序列**最末尾**；解码后的拆分顺序则相反、先文后点（L488-L491），两个拆分函数都从尾部 `[-n:]` 切——LIFO 的注入/拆分配对。

**结果拆分**的核心切片逻辑（[uniseg3d/uniseg3d.py:L548-L564](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L548-L564)）：

```python
for i, cur_text_prompts in enumerate(batch_gt_text_prompts):
    cur_num_text_prompts = len(cur_text_prompts)
    if cur_num_text_prompts > 0:
        text_prompt_outputs[key][i]  = x[key][i][-cur_num_text_prompts:]   # 尾部 = 文本提示
        original_outputs[key][i]     = x[key][i][:-cur_num_text_prompts]   # 其余 = 原始查询
```

这里有一个精妙的复用：拆分的提示计数**直接来自 `len(gt_text_prompt)`**——真值字典同时充当了「提示有几条」的账本。这也意味着 `gt_text_prompt` 的行数必须与文本提示查询数严格相等，数据侧与模型侧由此咬合。

#### 4.3.4 代码实践：验证全量展开与同物体行重复

实践目标：确认 `TextPromptTest` 的输出行数 = Σ(每物体描述数)，且同一物体的多行 `gt_text_prompt` 完全相同。

操作步骤（示例代码，需要 `torch_scatter` 与完整环境）：

```python
# expand_check.py —— 复用第 5 节综合实践构造的 text_info（含 pts_id）与 sp_pts_mask
from uniseg3d.transforms_3d import TextPromptTest
out = TextPromptTest().transform({"text_info": text_info_with_pts_id,
                                  "sp_pts_mask": sp_pts_mask})
print("text_token:", out["text_token"].shape, " gt_text_prompt:", out["gt_text_prompt"].shape,
      " text_object_id:", out["text_object_id"])          # (3, 2, 6): 2 物体共 3 句描述
same_obj = out["text_object_id"] == out["text_object_id"][0]
rows = out["gt_text_prompt"][same_obj]
print("同物体各行是否全等:", all(torch.equal(rows[0], r) for r in rows))
```

需要观察的现象：2 个物体（描述数分别为 2 和 1）展开成 3 行；描述数多的物体其多行真值逐元素相等；`text_object_id` 形如 `[2, 2, 6]`。

预期结果：`gt_text_prompt` 形状 `(3, S)`，同物体行全等输出 `True`。待本地验证。另请顺手 `grep -n "gt_text_prompt" uniseg3d/formatting.py uniseg3d/data_preprocessor.py configs/uniseg3d_1xb4_*.py`，确认三站键名逐字一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么注入顺序是「先点后文」，而拆分顺序是「先文后点」？

答：两种提示都追加在查询序列尾部，后注入的排在更后面。点先注入、文后注入 → 文本在末端；拆分从尾部数起（`[-n:]`），必须先切最末端的文本提示，剩下的序列尾部才是点提示，再切一次。LIFO 配对（[uniseg3d/uniseg3d.py:L465-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L491)）。

**练习 2**：`split_original_and_text_prompt_queries` 用什么知道每个样本有几条文本提示？如果 `gt_text_prompt` 与 `text_token` 行数不一致会发生什么？

答：用 `len(batch_gt_text_prompts[i])`（[uniseg3d/uniseg3d.py:L549-L552](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L549-L552)）。行数不一致时切片位置错位：提示输出会混入普通查询的结果（或反之），监督信号张冠李戴——不会报错，但指标会莫名变差。两个 transform 同步生成四键正是为了守住这个一致性。

**练习 3**：模型怎么同时处理「有描述的场景」和「没描述的场景」？

答：三处配合——transform 空分支产出空键（[uniseg3d/transforms_3d.py:L71-L77](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L71-L77)）、`LangModule` 循环里 `len(...)==0` 跳过（[uniseg3d/lang_module.py:L64-L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L64-L66)）、`add_text_prompt_queries` 的 else 分支填空真值不拼查询（[uniseg3d/uniseg3d.py:L675-L681](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L675-L681)）。批次内两类场景因此可以混跑。

## 5. 综合实践

**任务**：构造一个「2 个实例、每实例 2 条描述」的假场景，从 `AddSuperPointAnnotations` 的产物约定出发伪造数据，分别运行 `TextPromptGeneration` 与 `TextPromptTest`，打印全部输出键的形状，并验证覆盖不变式：`gt_text_prompt` 每行胜出的超点集合 ⊆ 该实例点集触及的超点集合，且在超点纯净时取等。

设计要点（伪造数据必须守住的两条约定）：

1. `pts_instance_objextId` 与 `gt_sp_masks` 实例区**逐行对齐**（u3-l3）：本例令原始实例 id 为 3 和 7，则 `idxs = [-1, 3, 7]`，`pts_instance_objextId = [2, 6]`，text_info 的键必须是 `"2"` 和 `"6"`。
2. 实例行按「多数投票」由点级掩码算出——直接用与源码相同的 `scatter_mean > 0.5` 复现，保证与真实管线语义一致。

```python
# u3l5_text_prompt.py —— 示例代码；放仓库外目录运行，export PYTHONPATH=仓库根目录
# 依赖: torch, numpy, torch_scatter；import transforms_3d 需 u1-l2 的完整环境
import numpy as np, torch
from torch_scatter import scatter_mean
from uniseg3d.transforms_3d import TextPromptGeneration, TextPromptTest

torch.manual_seed(0); np.random.seed(0)

# ---- 1) 假场景: N 点, S 超点, 2 实例(原始 id 3/7), 1 个跨界超点演示子集关系 ----
S, per = 12, 25                       # 12 个超点、每超点 25 点
N = S * per                           # 300 点
sp_pts_mask = np.repeat(np.arange(S), per)
# 超点归属: 0-4 -> 实例3(objectId 2), 5-9 -> 实例7(objectId 6), 10-11 -> 背景
pts_instance_mask = np.full(N, -1)
for sp in range(5):  pts_instance_mask[sp*per:(sp+1)*per] = 3
for sp in range(5,10): pts_instance_mask[sp*per:(sp+1)*per] = 7
# 超点 10 故意跨界: 10 个点给实例3, 其余背景 -> 实例3 触及但不占多数
pts_instance_mask[10*per:10*per+10] = 3

# ---- 2) 按 AddSuperPointAnnotations 的约定伪造其两个产物 ----
idxs = np.unique(pts_instance_mask)                     # [-1, 3, 7]
pts_instance_objextId = torch.tensor(idxs[1:] - 1)      # [2, 6]  <- text_info 的键
oh = torch.zeros(2, N)                                   # (实例数, N) 点级 one-hot, 同 u3-l3
oh[0, torch.tensor(pts_instance_mask) == 3] = 1.
oh[1, torch.tensor(pts_instance_mask) == 7] = 1.
inst_rows = scatter_mean(oh, torch.tensor(sp_pts_mask), dim=-1) > 0.5  # (2, S) 多数投票
gt_sp_masks = np.vstack([inst_rows.numpy(), np.zeros((21, S), dtype=bool)])  # +21 行语义占位

# ---- 3) 假 text_info: 2 实例 × 各 2 句 ----
pts_id_2 = np.where(pts_instance_mask == 3)[0]
pts_id_6 = np.where(pts_instance_mask == 7)[0]
text_info = {
    "2": {"text": ["a brown chair", "the chair near the desk"],
           "text_token": [np.arange(77), np.arange(77)], "label_id": 3,
           "pts_id": pts_id_2},
    "6": {"text": ["white cabinet", "the cabinet by the wall"],
           "text_token": [np.arange(77), np.arange(77)], "label_id": 0,
           "pts_id": pts_id_6},
}

# ---- 4) 训练侧 ----
out_tr = TextPromptGeneration().transform(
    {"text_info": text_info, "gt_sp_masks": gt_sp_masks,
     "pts_instance_objextId": pts_instance_objextId})
print("[train]", {k: np.shape(v) for k, v in out_tr.items()
                  if k in ("label_text", "gt_text_prompt", "text_object_id", "text_token")})

# ---- 5) 测试侧 ----
out_te = TextPromptTest().transform({"text_info": text_info,
                                     "sp_pts_mask": sp_pts_mask})
print("[test] ", {k: np.shape(v) for k, v in out_te.items()
                  if k in ("label_text", "gt_text_prompt", "text_object_id", "text_token")})

# ---- 6) 覆盖不变式: 胜出超点 ⊆ 触及超点, 纯超点时相等 ----
pts_of = {2: pts_id_2, 6: pts_id_6}
for tag, out in (("train", out_tr), ("test", out_te)):
    for i, oid in enumerate(out["text_object_id"]):
        won     = set(np.where(np.asarray(out["gt_text_prompt"][i]))[0].tolist())
        touched = set(np.unique(sp_pts_mask[pts_of[int(oid)]]).tolist())
        print(f"{tag} 提示{i}(objectId={int(oid)}): 胜出{sorted(won)} ⊆ 触及{sorted(touched)} -> {won <= touched}")
```

需要观察的现象与预期结果：

1. `[train]` 各键形状为 `(2,) / (2, 12) / (2,) / (2,)`——每物体 1 条提示；`[test]` 为 `(4,) / (4, 12) / (4,) / (4,)`——2 物体 × 2 描述全量展开。
2. 训练侧若某轮只选到部分物体，形状随之变化（`random.sample` 乱序的体现）。
3. objectId=2 的行：胜出超点为 `{0..4}`，触及超点为 `{0..4, 10}`——超点 10 只有 10/25 的点属于该实例（均值 0.4 < 0.5），被多数投票淘汰，**子集成立、严格不等**；objectId=6 的行胜出 = 触及 = `{5..9}`，两者相等（其超点纯净）。
4. 测试侧同一物体（objectId=2）的两行 `gt_text_prompt` 逐元素相等（都从同一 `pts_id` 投影）。

以上均为静态推演的预期，**待本地验证**。如果环境缺 MinkowskiEngine 等依赖无法 import，可把两个 transform 的逻辑（共约 50 行）复制成独立函数运行，验证结论不变。

## 6. 本讲小结

- `text_info` 按物体组织：键为字符串 objectId，含 `text`（多条描述）、`text_token`（离线 OpenCLIP 分词）、`label_id`（0..17 实例类空间）与 `pts_id`（点级下标，仅测试消费）。
- 训练侧 `TextPromptGeneration`：每物体**随机 1 条**描述、全部物体（乱序），`gt_text_prompt` 直接**整行拷贝** `gt_sp_masks` 的实例行，行号由 `text_object_id` 在 `pts_instance_objextId` 中查得；`num_ins/seq_length/embedding_dim` 是不生效的遗留参数。
- 测试侧 `TextPromptTest`：**每物体 × 每条描述全量展开**，真值从 `pts_id` 点级 one-hot 经 `scatter_mean > 0.5` 多数投票投影到超点——与 u3-l3 的聚合数学同源；同物体多行真值相同。
- 覆盖不变式：胜出超点集合 ⊆ 实例点集触及的超点集合，仅当无跨界超点时取等；`assert len(id)>0` 是描述指向被清洗物体时的安全阀。
- 四个键 `label_text / gt_text_prompt / text_object_id / text_token` 走 transform → Pack → Preprocessor 三站白名单通道，任何一站改名都会让文本分支**静默跳过**而非报错。
- 模型侧闭环：`add_text_prompt_queries` 用 `LangModule` 编码 token、加 `type_embedding[0]` 拼到查询最尾部（先点后文）；拆分时用 `len(gt_text_prompt)` 当计数账本，从尾部 `[-n:]` 切出提示输出。

## 7. 下一步学习建议

- **u4-l5（语言分支）**：`text_token` 进入 `LangModule` 之后发生了什么——OpenCLIP 文本塔的前向（token embedding → transformer → EOT 池化 → 投影）与 768→32 维 MLP 投影的细节。
- **u4-l6（提示查询注入）**：本讲只讲了注入与拆分，`interaction_masks` 如何屏蔽「提示组 × 原始查询」之间的自注意力、三类查询的 `type_embedding` 如何区分，是该讲的主体。
- **u5-l5（提示实例损失）**：文本提示的一对一监督（恒等匹配、`use_cls_supervise` 开关）如何消费 `labels_3d` 与 `sp_masks`。
- **u5-l6（跨任务对比蒸馏）**：`text_object_id` 作为钥匙，如何把文本提示与点提示配成正样本对做 InfoNCE 与排序蒸馏——本讲埋下的伏笔在那里兑现。
