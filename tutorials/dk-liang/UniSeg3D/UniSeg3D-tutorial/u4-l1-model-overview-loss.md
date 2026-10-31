# u4-l1 UniSeg3D 模型骨架与训练前向 loss()

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `UniSeg3D` 类由哪四个子模块组成、`__init__` 里每个关键配置项(`type_embedding`/`pred_iou`/`alpha`/`beta`/`query_thr`)的真实作用。
2. 讲清 `collate()` 如何用一个 `MinkowskiEngine.TensorField` 把一批点云变成稀疏张量,以及 `extract_feat()` 如何用 `scatter_mean` 把体素特征聚合回超点。
3. 理解 `batch_offsets` 与 `sp_pts_mask` 的「全局编号 + 偏移切分」批处理技巧。
4. 把 `loss()` 的完整前向拆成十个步骤,并理解 `_select_queries` 的 `query_thr` 随机采样策略。
5. 亲手跟踪一个假 batch 在前向各环节的 shape 变化,整理出一张数据流表。

## 2. 前置知识

本讲假设你已读完 u2-l2(配置系统)与 u3-l2(加载打包),这里先把要用到的几个概念说透。

**稀疏张量(sparse tensor)**:3D 点云在体素化之后,空间里绝大部分体素是空的。如果用稠密张量表示一个 \( 128\times128\times128 \) 的场景,要存 200 万个数,而其中非空的可能只有几万个。稀疏卷积框架(spconv、MinkowskiEngine,简称 ME)只存「非空体素的坐标 + 它的特征」两块内存,卷积时也只在非空位置计算。两个库约定相同:坐标张量形状为 \((N, 1+3)\),第 0 列是 batch 索引,后 3 列是体素坐标 \((x,y,z)\)。

**体素化(voxelization)**:把连续坐标映射为整数格点索引——坐标除以体素大小 `voxel_size`(本项目 0.02 米)再取整。落入同一体素的多个点会被合并成一个体素条目,合并记录保存在 `inverse_mapping` 里,之后可以把体素特征「还原」回每个点。

**超点与「超点即查询」**:回顾 u1-l1,点云先被 segmentator 过分割成数千个超点;本项目的解码器没有可学习的 query 参数(`num_instance_queries=0`),**每个超点聚合出的特征向量就是一条查询**。所以 `extract_feat` 的输出名字叫 `x`,在 `loss()` 里直接被当作 `queries` 使用——这是读懂本讲最重要的一句话。

**Base3DDetector 的双入口**:mmdet3d 的检测器基类约定两条前向路径:训练时 Runner 调 `loss(batch_inputs_dict, batch_data_samples)` 返回损失字典,测试时调 `predict(...)` 返回预测。两者共享骨干网络代码,本讲聚焦前者,并在 4.4 对照两者差异。

**键的三站通道**:回顾 u3-l2,transform 生成键、`Pack3DDetInputs_` 分拣键、`Det3DDataPreprocessor_` 透传键。`loss()` 里读到的 `batch_inputs_dict['points']`、`'elastic_coords'`、`'point_prompts'`、`'text_token'`,就是三站通道的终点。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `uniseg3d/uniseg3d.py` | 主模型,本项目最核心文件 | `__init__`(L285)、`_init_layers`(L345)、`collate`(L381)、`extract_feat`(L358)、`_select_queries`(L226)、`loss`(L419)、`get_pred_iou`(L499)、`predict`(L746) |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 主配置 | `model` 五件套的取值(L20-L66) |
| `uniseg3d/structures.py` | `InstanceData_` 容器 | 假数据构造时使用 |

提示注入(`add_*_prompt_queries`)与结果拆分(`split_*`)在本讲只作为 `loss()` 流程中的两步出现,详细机制留给 u4-l6;解码器内部留给 u4-l3;损失体系留给第五单元。

## 4. 核心概念与源码讲解

### 4.1 类的组装:`__init__` 与 `_init_layers`

#### 4.1.1 概念说明

`UniSeg3D` 是一个「组装壳」:它自己只定义薄薄的数据搬运与流程编排逻辑,重活全部委托给四个经注册器实例化的子模块——稀疏卷积骨干 `unet`、统一查询解码器 `decoder`、语言分支 `lang`、损失总调度 `criterion`。这正是 u2-l1 讲过的「配置即代码」:配置文件里四个 `dict` 各自带 `type='XXX'`,在这里被 `MODELS.build` 递归实例化成完整对象图。

此外它还持有若干**流程开关**与**小部件**:`type_embedding` 区分查询来源、`pred_iou_proj` 预测掩码 IoU、`input_conv`/`output_layer` 是骨干外壳。

#### 4.1.2 核心流程

```text
UniSeg3D.__init__(配置参数)
├── super(Base3DDetector, self).__init__(data_preprocessor, init_cfg)
│       # 越过 Base3DDetector 直接调其父类(BaseModel),
│       # 只为挂上 data_preprocessor;Base3DDetector 仅提供抽象骨架
├── MODELS.build(backbone)  → self.unet       (SpConvUNet)
├── MODELS.build(decoder)   → self.decoder    (UnifiedQueryDecoder)
├── MODELS.build(lang)      → self.lang       (LangModule)
├── MODELS.build(criterion) → self.criterion  (ScanNetUnifiedCriterion)
├── 记录超参: voxel_size / num_classes / min_spatial_shape / query_thr
├── _init_layers: input_conv + output_layer
├── is_type_embedding=True 时: type_embedding = Embedding(3, 32)
├── 记录 alpha / beta
└── pred_iou=True 时: pred_iou_proj = 3 层 MLP(256→256→256→1)
```

#### 4.1.3 源码精读

先看构造函数签名与组件组装:[uniseg3d/uniseg3d.py:L285-L321](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L285-L321) 逐行完成上面的流程图;其中四个子模块的构建只有四行:[uniseg3d/uniseg3d.py:L311-L314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314) 把配置里的四个 dict 分别实例化为 `self.unet`、`self.decoder`、`self.lang`、`self.criterion`。

关键配置项逐个说清:

- **`type_embedding`(配置名 `is_type_embedding=True`)**:[uniseg3d/uniseg3d.py:L323-L327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L323-L327) 创建一个 `Embedding(3, 32)` 查找表——三行可学习向量分别对应三种查询来源。后续消费点在提示注入处:`weight[2]` 加给全部原始超点查询([L691](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L691))、`weight[1]` 加给点提示特征([L708](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L708))、`weight[0]` 加给文本嵌入([L666](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L666))。它让同一个 Transformer 能区分「这条查询是超点、是点提示还是文本提示」。维度是 `num_channels=32`,与查询维度一致(解码器内部的 `d_model=256` 由解码器自己的 `input_proj` 升维)。
- **`pred_iou=True`**:[uniseg3d/uniseg3d.py:L332-L338](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L332-L338) 构建三层 MLP(`d_model→d_model→d_model→1`),之后在 `get_pred_iou` 中把查询嵌入回归成「预测掩码 IoU 分数」,训练受 MSE 监督、推理乘进实例分数(细节在 u5-l4)。`d_model` 直接从 `decoder` 配置字典里取(L333),体现壳与子模块的参数联动。
- **`alpha=0.4`、`beta=0.8`**:[uniseg3d/uniseg3d.py:L329-L330](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L329-L330) 只把这两个数存成属性。**注意:主配置并未传入它们(用默认值),且在当前 HEAD 全仓库范围内没有任何读取 `self.alpha`/`self.beta` 的代码**(可用 `grep -n 'self\.alpha\|self\.beta' uniseg3d/` 验证,只有这两行赋值)。它们自首次发布训练代码的提交起就是保留参数,讲义不猜测其原定用途。
- **`query_thr=0.5`**:训练时每个场景随机保留的查询比例下限,详见 4.4。
- **`inst_test_iou=False` / `pano_test_iou=True`**:控制推理时实例/全景分数是否乘上 IoU 预测,属于 u6-l1 的内容。

外壳层定义在 [uniseg3d/uniseg3d.py:L345-L356](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L345-L356):`input_conv` 是一个 `SubMConv3d`(3×3×3 子流形卷积,`indice_key='subm1'`),把 6 通道输入特征(见 4.2)升到 `num_channels=32`;`output_layer` 是 `BatchNorm1d + ReLU`,给骨干输出收尾。它们与 `self.unet` 串联构成完整骨干(见 4.3 的调用顺序)。

配置侧的对照:[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L20-L34](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L20-L34) 给出 `in_channels=6`、`num_channels=32`、`voxel_size=0.02`、`num_classes=18`、`min_spatial_shape=128`、`query_thr=0.5`、`set_query_mask=True`、`set_all_mask=True`、`is_type_embedding=True`、`pred_iou=True`;[L35-L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L35-L66) 是四个子模块的 dict。

最后提一句 `_forward`:[uniseg3d/uniseg3d.py:L415-L417](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L415-L417) 是空实现,只为满足 `Base3DDetector` 的抽象接口约定——本项目只用 `loss`/`predict` 两条路径。

#### 4.1.4 代码实践

**目标**:验证「配置即代码」——把主配置里的 model dict 实例化成对象,打印它的子模块清单与关键属性。

1. 进入仓库根目录,激活 u1-l2 搭好的 conda 环境。
2. 新建脚本 `practice_build_model.py`(示例代码,非仓库原有文件,放在仓库根目录即可):
```python
# 示例代码
import torch, uniseg3d  # noqa: F401  import 即注册
from mmengine.config import Config
from mmdet3d.registry import MODELS

cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
cfg.model.lang = None                    # 暂时去掉语言分支,避免加载 CLIP 权重文件
cfg.model.decoder['sphere_cls'] = False  # 关闭球面分类,避免加载类别嵌入文件
model = MODELS.build(cfg.model)
print(type(model).__name__)
print('unet =', type(model.unet).__name__)
print('decoder =', type(model.decoder).__name__)
print('lang =', model.lang)
print('criterion =', type(model.criterion).__name__)
print('type_embedding =', model.type_embedding)
print('pred_iou_proj =', model.pred_iou_proj)
print('voxel_size =', model.voxel_size, ', query_thr =', model.query_thr)
print('alpha =', model.alpha, ', beta =', model.beta)
```
3. 运行 `python practice_build_model.py`。

**需要观察的现象**:脚本不需要任何预训练权重即可跑通(去掉 `lang` 与 `sphere_cls` 后没有任何文件加载);打印出的 `type_embedding` 是 `Embedding(3, 3)` 形式的 repr(3 个词表、32 维),`pred_iou_proj` 是三层 `Linear` 结构。

**预期结果**:`UniSeg3D` / `SpConvUNet` / `UnifiedQueryDecoder` / `None` / `ScanNetUnifiedCriterion` 依次打印;`query_thr=0.5`、`alpha=0.4`、`beta=0.8`。若报 `KeyError: 'XXX is not in the registry'`,说明第 2 行的 `import uniseg3d` 缺失——注册是 import 的副作用(u2-l1)。

#### 4.1.5 小练习与答案

**练习 1**:`Embedding(3, num_channels)` 的两个参数各是什么含义?为什么第一个参数是 3?
**答案**:第一个参数是词表大小(可查询的离散 id 数),第二个是每个 id 的嵌入维度。是 3 因为查询有三种来源:原始超点查询、点提示查询、文本提示查询,分别使用 `weight[2]`、`weight[1]`、`weight[0]`。

**练习 2**:为什么 `pred_iou_proj` 的输入输出维度是 `d_model→d_model→d_model→1`?
**答案**:它作用在解码器输出的查询嵌入(`contras_embeds`,维度为解码器的 `d_model=256`)上,把每条查询嵌入回归成一个标量——该查询预测掩码与真值的 IoU 分数,所以最后一层输出维度是 1。

**练习 3**:如果把配置里 `is_type_embedding` 改为 `False`,训练还能正常跑吗?
**答案**:能跑。`type_embedding` 不会创建,提示注入处的 `if self.is_type_embedding` 分支会跳过向量加法;代价是模型失去区分三种查询来源的能力,提示查询与超点查询在表示上不再可区分。

### 4.2 collate():从点云列表到 MinkowskiEngine 稀疏张量

#### 4.2.1 概念说明

`collate()` 解决的问题是:**一批形状各异的点云,如何拼成一个稀疏张量喂给 spconv 骨干**。难点有两个:一是每个场景点数不同,二是体素化后需要知道「每个点落进了哪个体素」以便之后把特征还原回点。项目选择 ME 的 `TensorField` 完成体素化,再把产出的坐标与特征交给 spconv——两个库的坐标约定一致(第 0 列 batch 索引),所以可以「ME 建场、spconv 计算」混用。

它同时是**弹性增广的汇聚点**:u3-l6 讲过,`ElasticTransfrom` 不改 `points`,只新增 `elastic_coords`。`collate` 的两个分支正是「无弹性:用原始坐标体素化 / 有弹性:用形变后坐标体素化」,两条路径的特征完全相同——增广只改点的体素归属。

#### 4.2.2 核心流程

```text
collate(points, elastic_points=None)
对每个样本 p:
  无弹性分支:
      坐标 = (xyz - 场景内最小值) / voxel_size        # 平移到原点,再量化
  有弹性分支:
      坐标 = elastic_coords - 最小值                   # 已是体素单位(u3-l6),只做对齐
  特征 = hstack(RGB, xyz - 质心)                       # 6 通道 = 3 颜色 + 3 中心化坐标
ME.utils.batch_sparse_collate: 逐样本坐标加 batch 列,堆叠
spatial_shape = clip(坐标最大值 + 1, 下限 128)
field = ME.TensorField(features, coordinates)
tensor = field.sparse()                # 体素化去重:多点落同一体素合并成一条
inverse_mapping = field.inverse_mapping(...)   # (n_points,) 每个点 → 它的体素行号
返回 (coordinates, features, inverse_mapping, spatial_shape)
```

两个值得注意的细节:

- **平移到原点再量化**:减去场景内坐标最小值,保证体素索引从 0 开始,`spatial_shape` 才能用 `max+1` 度量。
- **特征构造把「位置」也编码进特征**:`p[:, :3] - p[:, :3].mean(0)` 是相对场景质心的偏移(米制)。注意这与坐标量化相互独立——卷积感受的是量化格点,特征携带的是连续位置信息。

#### 4.2.3 源码精读

两条分支的特征拼装:[uniseg3d/uniseg3d.py:L394-L403](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L394-L403)。L394-L398 是无弹性分支:`(p[:, :3] - p[:, :3].min(0)[0]) / self.voxel_size` 生成体素坐标,`torch.hstack((p[:, 3:], p[:, :3] - p[:, :3].mean(0)))` 拼出 6 通道特征(RGB 在前——注意 `NormalizePointsColor_` 已把 RGB 归一化到约 \([-1,1]\),见 u3-l2)。L399-L403 是弹性分支:坐标换成 `elastic_coords`(仅减最小值对齐),特征一行未变。

空间形状与体素化:[uniseg3d/uniseg3d.py:L405-L413](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L405-L413)。`torch.clip(coordinates.max(0)[0][1:] + 1, self.min_spatial_shape)` 取后三维(跳过第 0 列 batch 索引)的最大坐标加 1 作为张量形状,并用 `min_spatial_shape=128` 兜底,防止极小场景退化;随后 `ME.TensorField → field.sparse()` 完成体素化,返回去重后的 `tensor.coordinates` / `tensor.features`,并经 `field.inverse_mapping(...)` 拿到「点 → 体素」映射。

关于同体素多点的合并策略:函数 docstring 写明设计意图是「训练随机采样、推理无权重平均」([L386-L388](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L386-L388)),但当前代码调用 `field.sparse()` 时并未显式传入量化模式参数,实际合并行为由 ME 的默认行为决定——此处**待本地验证**(可在实践中打印 `field.sparse()` 前后的特征行数对比点数来观察)。

`inverse_mapping` 为什么重要:去重后 `features` 是体素级的(行数 = 体素数 < 点数),而 4.3 的超点聚合要按「点」归属的超点做平均,所以必须先用 `inverse_mapping` 把体素特征广播回每个点:`x.features[inverse_mapping]`。

#### 4.2.4 代码实践

**目标**:不依赖模型与权重,单独复现 `collate` 的体素化过程,观察坐标、特征、inverse_mapping 的 shape 与取值。

1. 新建脚本 `practice_collate.py`(示例代码):
```python
# 示例代码
import torch
import MinkowskiEngine as ME

voxel_size = 0.02
torch.manual_seed(0)
points = [torch.rand(5000, 6) * torch.tensor([5., 5., 3., 1., 1., 1.])]  # 假场景:xyz 5x5x3 米,rgb 0~1

# 复现 uniseg3d.py L395-L398
coordinates, features = ME.utils.batch_sparse_collate(
    [((p[:, :3] - p[:, :3].min(0)[0]) / voxel_size,
      torch.hstack((p[:, 3:], p[:, :3] - p[:, :3].mean(0))))
     for p in points])

print('collate 后 coordinates:', coordinates.shape, coordinates.dtype)
print('collate 后 features   :', features.shape)
print('batch 列(第0列)取值:', coordinates[:, 0].unique())

spatial_shape = torch.clip(coordinates.max(0)[0][1:] + 1, 128)
field = ME.TensorField(features=features, coordinates=coordinates)
tensor = field.sparse()
inv = field.inverse_mapping(tensor.coordinate_map_key)
print('spatial_shape        :', spatial_shape)
print('体素化后 coordinates :', tensor.coordinates.shape)
print('体素化后 features    :', tensor.features.shape)
print('inverse_mapping      :', inv.shape)
```
2. 运行 `python practice_collate.py`。

**需要观察的现象**:`coordinates` 是 `(5000, 4)`(每个点一条,batch 列全 0);体素化后 `tensor.coordinates` 行数明显小于 5000(随机点有不少落入同一体素);`inverse_mapping` 长度恒等于点数 5000,取值范围是 `[0, 体素数)`。

**预期结果**:shape 输出形如 `coordinates: torch.Size([5000, 4])`、`inverse_mapping: torch.Size([5000])`,体素数大约在数千量级(与随机分布有关,具体数值**待本地验证**)。把 `voxel_size` 改成 `0.2` 再跑一次,体素数会大幅减少——直观感受量化粒度的作用。

#### 4.2.5 小练习与答案

**练习 1**:为什么特征里要放「中心化坐标」而不是原始绝对坐标?
**答案**:绝对坐标与场景在世界系中的位置耦合,同一类物体换个房间位置特征就完全不同;减去场景质心后特征只表达「物体在场景中的相对位置」,提升跨场景泛化。同时体素坐标那一套(除以 voxel_size)已负责空间几何,特征里的连续坐标是补充信息。

**练习 2**:两个分支的 `features` 完全一样意味着什么?
**答案**:弹性增广只改变每个点被分派到的体素(坐标路径),不改点的颜色特征与任何标注。这是 u3-l6 结论在模型侧的印证:增广是一种「体素归属抖动」。

**练习 3**:`spatial_shape` 为什么要 `clip(..., 128)`?
**答案**:对点数极少的退化场景,`max+1` 可能只有几格,稀疏卷积多层下采样后空间会塌缩到 1,梯度与感受野都不正常;下限 128 保证张量空间足够深。

### 4.3 extract_feat():稀疏卷积 + scatter_mean 聚合到超点

#### 4.3.1 概念说明

`extract_feat()` 是「体素特征 → 超点查询」的桥梁,共四步:外壳卷积升维 → UNet 提特征 → BN+ReLU 收尾 → **按超点平均**。前三步是常规骨干;最后一步是本项目的灵魂:体素级特征先经 `inverse_mapping` 还原到每个点,再对同一超点内的所有点做均值池化,得到每个超点一个 32 维向量——它既是解码器的输入 `x`,也是训练的查询。

用公式表达(\(v(p)\) 表示点 \(p\) 落入的体素,由 `inverse_mapping` 给出;\(s(p)\) 表示点 \(p\) 所属超点,由 `sp_pts_mask` 给出):

\[
\mathbf{f}_p = \mathbf{F}_{v(p)}, \qquad
\mathbf{q}_s = \frac{1}{|\{p : s(p)=s\}|}\sum_{s(p)=s} \mathbf{f}_p
\]

第一步是逐点「查表」,第二步就是 `torch_scatter.scatter_mean` 完成的分组均值。

#### 4.3.2 核心流程

```text
extract_feat(x, superpoints, inverse_mapping, batch_offsets)
输入: x = spconv.SparseConvTensor(体素级), superpoints = 全局超点id (n_total_sp,)
1. x = input_conv(x)              # SubMConv3d: 6 → 32 通道
2. x, _ = unet(x)                 # 五层 U-Net,详见 u4-l2;输出仍 32 通道
3. x = output_layer(x)            # BatchNorm1d + ReLU
4. x = scatter_mean(x.features[inverse_mapping], superpoints, dim=0)
        # 体素特征 → 点特征 → 按超点均值 → (n_total_sp, 32)
5. 按 batch_offsets 切片: out_i = x[offsets[i]: offsets[i+1]]
返回 List[Tensor],每个样本一个 (n_sp_i, 32)
```

#### 4.3.3 源码精读

骨干串联与聚合:[uniseg3d/uniseg3d.py:L358-L379](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L358-L379)。核心是 L372-L374 的三步骨干调用(`input_conv → unet → output_layer`),以及 L375 的一行聚合:

[uniseg3d/uniseg3d.py:L375-L379](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L375-L379) —— `scatter_mean(x.features[inverse_mapping], superpoints, dim=0)`:先用 `inverse_mapping` 做花式索引把体素特征 \((n_{vox}, 32)\) 展开成点特征 \((n_{pts}, 32)\),再按 `superpoints` 里的超点 id 分组求均值,得到全局超点特征 \((n_{total\_sp}, 32)\);随后 L377-L378 的循环按 `batch_offsets` 把这个全局张量切回每个样本。

这里的 **`batch_offsets` 偏移拼接技巧**值得单独讲(它在 `loss()` 里构造,见 4.4):`scatter_mean` 只接受一维分组 id。若各样本的超点 id 都从 0 开始,直接拼接会互相混叠。做法是给样本 \(i\) 的超点 id 统一加上前面样本的超点总数,使全局 id 连续不重叠;`batch_offsets=[0, n_0, n_0+n_1, ...]` 记录切分点。一次 scatter 完成整批聚合,再各切各的——「先全局编号、按偏移切分」是处理变长分组批的通用手法。

参数上,`superpoints` 是 `loss()` 里 `torch.hstack` 拼好的全局超点 id(见 L455),`inverse_mapping` 来自 `collate`。同一个超点内的点通常落在多个相邻体素里,所以这个均值同时完成了「跨体素、回点、并超点」的三级信息汇聚。

#### 4.3.4 代码实践

**目标**:用假数据走通「collate → SparseConvTensor → extract_feat」,记录体素特征与超点特征的 shape,验证 `batch_offsets` 的切分逻辑。

1. 接着 4.1 的脚本思路,新建 `practice_extract.py`(示例代码):
```python
# 示例代码
import torch, uniseg3d  # noqa: F401
import spconv.pytorch as spconv
from torch_scatter import scatter_mean
from mmengine.config import Config
from mmdet3d.registry import MODELS

cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
cfg.model.lang, cfg.model.decoder['sphere_cls'] = None, False
model = MODELS.build(cfg.model).eval()
torch.manual_seed(0)

N, S = 5000, 200
points = torch.rand(N, 6) * torch.tensor([5., 5., 3., 1., 1., 1.])
sp_pts_mask = torch.randint(0, S, (N,))          # 每点随机归属 200 个超点之一

coordinates, features, inverse_mapping, spatial_shape = model.collate([points])
print('collate: coordinates', tuple(coordinates.shape),
      '| features', tuple(features.shape),
      '| inverse_mapping', tuple(inverse_mapping.shape))
print('spatial_shape', spatial_shape)

x = spconv.SparseConvTensor(features, coordinates, spatial_shape, 1)
with torch.no_grad():
    sp_feat = model.extract_feat(x, sp_pts_mask, inverse_mapping, [0, S])
print('超点特征:', tuple(sp_feat[0].shape))       # 期望 (200, 32)

# 手动复现 L375 的聚合,与模型输出对拍
vox = model.output_layer(model.unet(model.input_conv(x))[0]).features
manual = scatter_mean(vox.features[inverse_mapping], sp_pts_mask, dim=0)
print('对拍最大误差:', (manual - torch.cat(sp_feat, 0)).abs().max().item())
```
2. 运行 `python practice_extract.py`。

**需要观察的现象**:`features` 行数等于体素数(小于 5000);`extract_feat` 返回长度 1 的列表,元素形状 `(200, 32)`——200 个超点、32 维,与「超点即查询」对上;对拍误差为 0(浮点求和顺序可能引入极小误差,**待本地验证**量级)。

**预期结果**:输出形如 `collate: coordinates (n_vox, 4) | features (n_vox, 6) | inverse_mapping (5000,)` 与 `超点特征: (200, 32)`。若把 `[0, S]` 故意改成 `[0, 100]`,返回的张量只剩前 100 行——直观体会 `batch_offsets` 只负责切分、聚合本身在全局完成。

#### 4.3.5 小练习与答案

**练习 1**:如果不做 `x.features[inverse_mapping]` 这步索引,直接对体素特征按超点 scatter 会怎样?
**答案**:无法执行。`superpoints` 的长度是点数,而 `x.features` 的行数是体素数,两个分组向量长度不匹配;而且超点归属定义在点上,不在体素上(一个体素内的点可能属于不同超点),必须先回到点级。

**练习 2**:批里有 2 个场景、超点数分别为 150 与 180,`batch_offsets` 是什么?样本 2 的超点 id 在全局张量中范围是多少?
**答案**:`batch_offsets=[0, 150, 330]`;样本 2 的超点在聚合前被加上偏移 150,全局 id 范围 \([150, 330)\),聚合后切出的第二段是全局张量的第 150~329 行。

**练习 3**:为什么用 `scatter_mean` 而不是 `scatter_max` 或直接取超点中心点的特征?
**答案**:均值是无参数、可微且对超点内点数不敏感的聚合;max 只回传部分点的梯度,对点数稀疏的超点不稳定;取中心点则丢弃了超点内其余体素的上下文。均值池化让每个超点查询稳定地代表其覆盖区域的平均表征。

### 4.4 `_select_queries` 采样与 `loss()` 十步前向

#### 4.4.1 概念说明

`loss()` 是训练前向的总编排:从 `batch_inputs_dict` 与 `batch_data_samples` 进,到损失字典出。它把前面三个模块串成一条线,并处理三件「编排层」的事:批内超点的全局编号、训练查询的随机采样、提示查询的注入与拆分(概述,细节在 u4-l6)。

`_select_queries` 的动机:ScanNet 一个场景的超点常达数千,若每步全部送入解码器,自注意力开销是查询数的平方级。训练时按比例 \(n \in [0.5, 1.0]\) 随机采样一部分超点查询(比例上限 3500 条),既省算力又起到类似 dropout 的正则作用;测试时(`predict`)用全部超点(L789 `queries = copy.deepcopy(x)`),保证覆盖。

#### 4.4.2 核心流程

`loss()` 完整十步([L419-L497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L419-L497)):

```text
输入: batch_inputs_dict(points, 可选 elastic_coords/point_prompts/text_token 等),
      batch_data_samples(每个含 gt_pts_seg.sp_pts_mask 与 gt_instances_3d)
 1. 遍历样本:超点 id 加偏移成全局编号,累积 batch_offsets     [L439-L447]
 2. collate:点云列表 → (coordinates, features, inverse_mapping, spatial_shape) [L449-L451]
 3. spconv.SparseConvTensor 包装;hstack 拼全局超点 id       [L453-L455]
 4. extract_feat:稀疏卷积 + scatter_mean → 每样本超点特征列表  [L456-L457]
 5. _select_queries:按 query_thr 随机采样查询,同步切 query_masks [L458]
 6. set_query_mask 时记录每样本原始查询数 original_lengths    [L460-L463]
 7. 有 point_prompts / text_token 时:注入点/文本提示查询       [L465-L468]
 8. 构造 interaction_masks:屏蔽原始查询 ↔ 提示查询间的自注意力 [L470-L484]
 9. decoder 前向 → 拆回原始/提示查询 → pred_iou 计算 preds_iou  [L486-L494]
10. criterion:六路损失 → 返回 loss 字典                       [L496-L497]
```

第 1 步的偏移拼接(与 4.3 呼应):

```python
# 伪代码,对应 L442-L444
gt_pts_seg.sp_pts_mask += superpoint_bias     # 本样本超点 id 加上前面样本的总数
superpoint_bias = sp_pts_mask.max().item()+1  # 更新偏移
batch_offsets.append(superpoint_bias)         # 记录切分点
```

第 5 步 `_select_queries` 的采样公式(\(u\sim U(0,1)\)):

\[ n_i = \min\big(\lfloor ((1-\theta)\,u + \theta)\cdot N_i \rfloor,\ 3500\big),\qquad \theta=\text{query\_thr}=0.5 \]

即每个场景保留 50%~100% 的超点查询、绝对不超过 3500 条;被丢掉的超点,其 GT 掩码列也要同步丢弃(`query_masks = sp_masks[:, ids]`),保证查询与真值对齐。

第 8 步简述:`interaction_masks` 是与自注意力形状相同的布尔矩阵,`True` 表示「屏蔽」。`set_query_mask=True` 时屏蔽「原始查询 ← 提示查询」方向;`set_all_mask=True` 再加「提示 → 原始」与提示互相之间(除自身)的屏蔽,让三组查询互不可见、各自专注(机制细节在 u4-l6)。

#### 4.4.3 源码精读

**批内偏移与 GT 收集**:[uniseg3d/uniseg3d.py:L431-L447](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L431-L447)。循环里 `gt_pts_seg.sp_pts_mask += superpoint_bias` 把每个样本的超点 id 平移成全局连续编号,`batch_offsets` 逐步 append 出 `[0, n_0, n_0+n_1, ...]`;同时把各样本的 `gt_instances_3d`(携带 u3-l3 构造的 `sp_masks`/`labels`/`is_novel` 等)收集进 `sp_gt_instances`,最终喂给 criterion。

**稀疏化与特征提取**:[uniseg3d/uniseg3d.py:L449-L457](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L449-L457)。`self.collate(batch_inputs_dict['points'], batch_inputs_dict.get('elastic_coords', None))` 同时接收两条坐标路径;随后 `spconv.SparseConvTensor(features, coordinates, spatial_shape, len(batch_data_samples))` 把 ME 产物包装成 spconv 的输入张量,`torch.hstack(sp_pts_masks)` 拼出全局超点 id,再交给 `extract_feat`。

**查询采样**:[uniseg3d/uniseg3d.py:L226-L256](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L226-L256) 的 `_select_queries`(定义在 `UniSeg3DMixin`)。采样核心在 [L245-L251](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L245-L251):`n = (1 - self.query_thr) * torch.rand(1) + self.query_thr` 抽保留比例,`min(n, 3500)` 封顶,`torch.randperm(len(x[i]))[:n]` 随机选超点下标;`gt_instances[i].query_masks = gt_instances[i].sp_masks[:, ids]` 让 GT 的超点掩码矩阵按同一组列下标切片——被丢弃的查询对应的监督信号一并丢弃。`query_thr >= 1` 时走 L252-L254 全量分支。

**提示注入与拆分**:[uniseg3d/uniseg3d.py:L465-L468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L468) 以「键是否存在」为开关调用 `add_point_prompt_queries` / `add_text_prompt_queries`(u3-l4/u3-l5 的提示键在此被消费);L488-L491 再把解码输出拆回 `x`(原始查询)与 `point_prompt_outputs` / `text_prompt_outputs`。

**解码与损失**:[uniseg3d/uniseg3d.py:L486-L497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486-L497)。`self.decoder(x, queries, interaction_masks=interaction_masks)` 完成六层解码(u4-l3);`self.pred_iou` 为真时 `get_pred_iou`(定义在 [L499-L518](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L499-L518))把主输出与每个 aux 层的 `contras_embeds` 过 `pred_iou_proj` 生成 `preds_iou`;最后 `self.criterion(x, sp_gt_instances, point_prompt_outputs, point_prompt_instances, text_prompt_outputs, text_prompt_instances)` 一次性算出全部损失(第五单元展开)。

**与 predict 的对照**:[uniseg3d/uniseg3d.py:L767-L789](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L767-L789) 显示 `predict()` 的前半段与 `loss()` 几乎逐行相同(偏移编号 → collate(不带弹性坐标)→ SparseConvTensor → extract_feat),差别在随后 L789 `queries = copy.deepcopy(x)` 使用**全部**超点查询且不构造 interaction_masks——「训练采样、测试全量」。

#### 4.4.4 代码实践(本讲主实践)

**目标**:在 `loss()` 关键位置跟踪一个 batch_size=1 假数据的 shape 变化,整理成数据流表。不修改仓库源码,用「子调用拼装」的方式等价复现 `loss()` 的前五步(提示注入之后的部分需要 u4-l6/u5-l1 的知识,留给后续课程)。

1. 新建脚本 `practice_trace_loss.py`(示例代码):
```python
# 示例代码:等价复现 uniseg3d.py L431-L458 的前五步
import torch, uniseg3d  # noqa: F401
import spconv.pytorch as spconv
from mmengine.config import Config
from mmdet3d.registry import MODELS
from uniseg3d.structures import InstanceData_

cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
cfg.model.lang, cfg.model.decoder['sphere_cls'] = None, False
model = MODELS.build(cfg.model).eval()
torch.manual_seed(0)

N, S, K = 5000, 300, 5
batch_inputs_dict = dict(points=[torch.rand(N, 6) * torch.tensor([5., 5., 3., 1., 1., 1.])])
gt = InstanceData_()                                  # 仿 gt_instances_3d
gt.sp_masks = torch.zeros(K, S, dtype=torch.bool)     # K 个实例的超点掩码
for k in range(K):
    gt.sp_masks[k, torch.randperm(S)[:30]] = True
gt.labels = torch.randint(0, 18, (K,))
sp_pts_mask = torch.randint(0, S, (N,))               # 仿 gt_pts_seg.sp_pts_mask

rows = []                                             # 数据流表
def log(stage, **kw):
    rows.append((stage, {k: tuple(v.shape) for k, v in kw.items()}))

# 步骤 1:全局编号 + batch_offsets(对应 L431-L447)
batch_offsets, bias, sp_list = [0], 0, [sp_pts_mask]
sp_list[0] += 0; bias = sp_list[0].max().item() + 1; batch_offsets.append(bias)
log('1 偏移编号', sp_pts_mask=sp_list[0])

# 步骤 2:collate(对应 L449-L451)
coordinates, features, inverse_mapping, spatial_shape = model.collate(batch_inputs_dict['points'])
log('2 collate', coordinates=coordinates, features=features,
    inverse_mapping=inverse_mapping)

# 步骤 3:SparseConvTensor + hstack(对应 L453-L455)
x = spconv.SparseConvTensor(features, coordinates, spatial_shape, 1)
sp_pts_masks = torch.hstack(sp_list)
log('3 稀疏张量', sparse_features=x.features)

# 步骤 4:extract_feat(对应 L456-L457)
x = model.extract_feat(x, sp_pts_masks, inverse_mapping, batch_offsets)
log('4 超点特征', sp_feat_0=x[0])

# 步骤 5:_select_queries(对应 L458)
gt_in = [gt]
queries, gt_out = model._select_queries(x, gt_in)
log('5 查询采样', queries_0=queries[0], query_masks_0=gt_out[0].query_masks)

for stage, shapes in rows:
    print(f'{stage:<12}', shapes)
print('batch_offsets =', batch_offsets)
print('查询数 / 超点数 =', queries[0].shape[0], '/', S,
      '(query_thr=0.5, 应约为一半 ~ 全部之间)')
```
2. 运行 `python practice_trace_loss.py`。
3. 多跑几次,观察「查询数 / 超点数」比例的随机波动。

**需要观察的现象**:数据流表逐行呈现 shape 演进——`coordinates/features` 为体素级、`inverse_mapping` 长度等于点数 5000、`sparse_features` 行数等于体素数、`sp_feat_0` 形状 `(300, 32)`、`queries_0` 行数在 150~300 之间随机、`query_masks_0` 形状 `(5, 查询数)` 与查询逐列对齐。

**预期结果**:典型输出如下(体素数与查询数依赖随机种子,**待本地验证**具体数值):

```text
1 偏移编号    {'sp_pts_mask': (5000,)}
2 collate     {'coordinates': (n_vox, 4), 'features': (n_vox, 6), 'inverse_mapping': (5000,)}
3 稀疏张量    {'sparse_features': (n_vox, 32)}
4 超点特征    {'sp_feat_0': (300, 32)}
5 查询采样    {'queries_0': (n_q, 32), 'query_masks_0': (5, n_q)},  150 ≤ n_q ≤ 300
```

把 `query_thr` 想象成 1.0(或直接改脚本里 `model.query_thr = 1.0`)再跑,`n_q` 恒等于 300、`query_masks` 与 `sp_masks` 完全相同——验证全量分支。

#### 4.4.5 小练习与答案

**练习 1**:`loss()` 第 1 步里,如果把 `superpoint_bias` 的更新放在 `sp_pts_mask += superpoint_bias` **之前**,会发生什么?
**答案**:第一个样本不受影响,但第二个样本会被加上「自己加偏移前的最大超点 id + 1」,而正确值应是「前两个样本的超点总数」的偏移口径混乱——具体地,`superpoint_bias` 提前更新后,后续样本的偏移会基于错误基数,导致全局 id 重叠或跳跃,`scatter_mean` 把不同样本的超点混在一起、`batch_offsets` 切分错位,监督与特征全部张冠李戴。

**练习 2**:某场景有 6000 个超点,`query_thr=0.5` 时实际送入解码器的查询最多多少条?`query_masks` 的形状是什么?
**答案**:比例采样给出 3000~6000,但 `min(n, 3500)` 封顶,所以最多 3500 条;`query_masks` 形状为 `(n_gts, 实际查询数)`,是 `sp_masks` 按被保留超点的列切片。

**练习 3**:`predict()` 为什么不像 `loss()` 一样采样查询?
**答案**:训练采样是为了省算力并充当正则,丢掉部分查询只影响该步梯度;推理时每个超点都可能属于某个物体,丢查询就直接丢召回,所以 L789 用 `copy.deepcopy(x)` 全量输入(`deepcopy` 是因为后续会把提示查询拼进同一个列表,避免改动 `x` 本身)。

## 5. 综合实践

把 4.4.4 的脚本扩展成**双场景批**,并画出完整数据流图:

1. 仿照 `batch_offsets=[0, n_sp0, n_sp0+n_sp1]` 构造两个假场景(点数、超点数都不同,例如 4000 点/250 超点 与 6000 点/350 超点),分别作为 `points[0]`、`points[1]`,两份 `sp_pts_mask` 独立随机生成。
2. 依次执行 `loss()` 的第 1~5 步,打印:collate 后全局 `coordinates` 的 batch 列取值(应只有 0 和 1)、`sp_pts_masks` 拼接后的最大 id(应为 599)、`extract_feat` 返回列表的两个形状(应分别为 `(250, 32)` 与 `(350, 32)`)、两个场景各自的采样查询数。
3. 验证一个关键不变式:把两个场景的超点特征 `torch.cat` 起来,行数应等于 `batch_offsets[-1]`;任取样本 2 的某超点 id \(s\),手动用布尔掩码 `(sp_pts_masks == s)` 对 `vox.features[inverse_mapping]` 求均值,应与 `x[1][s - 250]` 一致。
4. 把全部观察整理成一张「阶段 | 张量 | shape | 说明」的数据流表,这就是本讲要求的学习产物,也是阅读 u4-l2/u4-l3 的索引图。

预期:所有对拍一致;若第 3 步不一致,优先检查你脚本里的偏移是否按 `loss()` L442-L444 的顺序累加。

## 6. 本讲小结

- `UniSeg3D` 是组装壳:`__init__` 经 `MODELS.build` 拼出 `unet/decoder/lang/criterion` 四件套,外加 `type_embedding`(三种查询来源的 3×32 查找表)、`pred_iou_proj`(256→1 的三层 MLP)与 `input_conv/output_layer` 骨干外壳;`alpha/beta` 在当前 HEAD 仅存储未消费。
- `collate()` 用 ME `TensorField` 完成体素化:坐标 `(xyz-min)/voxel_size`(弹性分支直接用已量化的 `elastic_coords`),特征 6 通道 = 归一化 RGB + 质心中心化坐标;`inverse_mapping` 记录点→体素映射,`spatial_shape` 以 128 为下限。
- `extract_feat()` = `input_conv → unet → output_layer → scatter_mean`:体素特征经 `inverse_mapping` 还原到点,再按全局超点 id 均值聚合成 \((n_{sp}, 32)\) 的「超点即查询」。
- `batch_offsets` + 超点 id 偏移拼接:批内各场景超点全局连续编号,一次 scatter 完成整批聚合,再按偏移切分——变长分组的通用批处理手法。
- `loss()` 十步:偏移编号 → collate → SparseConvTensor → extract_feat → `_select_queries` 采样(50%~100%、上限 3500,GT 掩码同步列切片)→ 记录原始查询数 → 注入点/文本提示 → interaction_masks → 解码+拆分+pred_iou → criterion 六路损失;`predict()` 前半段相同但用全量查询。

## 7. 下一步学习建议

本讲打通了「点云 → 超点查询」的上半程,接下来沿数据流往下走:

1. **u4-l2(稀疏卷积骨干 SpConvUNet)**:弄清 `self.unet(x)` 内部的 ResidualBlock、下采样/上采样与 `indice_key` 机制,理解 L373 返回的 `(x, blocks)` 中 blocks 的用途。
2. **u4-l3(查询解码器)**:看 `self.decoder(x, queries, interaction_masks)` 里六层 mask attention 与迭代预测如何产出 `cls_preds/masks/contras_embeds`。
3. 带着本讲的数据流表重读 [uniseg3d/uniseg3d.py:L746-L790](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L746-L790) 的 `predict()`,对照 `loss()` 找出全部共有行与差异行,为 u6-l1 的推理链路做铺垫。
