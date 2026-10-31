# 模型主入口 Grounded_LMM_main：init / loss / predict

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Grounded_LMM_main.__init__` 中 `unet` / `decoder` / `criterion` 三大组件分别由配置里的哪个字段构建、对应哪个真实类，以及哪些参数是「从 UniSeg3D 继承下来但本类已不再使用」的遗留开关。
2. 手推 `loss()` 中的 `batch_offsets` / `superpoint_bias` 对齐算法：给定每个场景的超点标签，算出全局偏移表，并解释 `extract_feat` 为什么先全局池化、再按偏移切回逐场景。
3. 对比 `loss()` 与 `predict()` 的共用主干与差异点（elastic 坐标、GT 组织方式、后处理、mask dump），并解释 `qa_predict_by_feat` 里预测 mask 与 GT mask 的行数对齐规则。

本讲是第 3 单元（3D 侧）的入口：它把第 2 单元产出的 `batch_inputs_dict` / `batch_data_samples` 接进来，把 3D 特征加工好之后交给解码器（第 4、5 单元的主角）。

## 2. 前置知识

- **Base3DDetector 与三条前向约定**：mmdet3d 的模型基类约定训练时由 Runner 调用 `loss(batch_inputs_dict, batch_data_samples)`、测试时调用 `predict(...)`，`_forward` 是纯前向（本项目将其置空）。因此一个模型类的骨架就是 `__init__` + `loss` + `predict`。
- **两条数据通道（承接 u2-l5）**：`batch_inputs_dict` 是前向输入通道（训练配置下含 `points`、`elastic_coords`、`scene_name`、`selected_qa_data` 四键），`batch_data_samples` 是监督信号通道（每个样本的 `gt_pts_seg` 里装着 `sp_pts_mask`——逐点超点 ID，和 `sp_gt_seg`——逐 QA 的超点级 GT 掩码列表，由 `Pack3DDetInputs_Part_Glamm` 打包，见 [uniseg3d/formatting.py:L134-L143](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L134-L143)）。
- **超点（superpoint）**：把点云过分割成的小块，每点属于一个超点，`sp_pts_mask[i] ∈ {0..M-1}`。模型所有掩码预测都在超点级别进行，最后再「按列扩展」回点级。
- **scatter-mean 池化**：把同一超点内所有点的特征取平均，得到该超点的特征向量。它是点级特征 → 超点 token 的桥梁。
- **伪批组装（pseudo batch）**：`pseudo_collate` 不堆叠张量，`points` 是「按样本的列表」，每个元素是 `(N_i, 6)` 的张量（XYZ+RGB）。本讲的 `collate()` 要把这堆大小不一的张量组装成一个稀疏张量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 本讲主角。`UniSeg3DMixin`（L20 起）、`UniSeg3D`（L261 起）、**`Grounded_LMM_main`（L906 起）**、`UniSeg3D_Reason`（L1336 起）四个类同文件；本项目实际使用的入口是 `Grounded_LMM_main` |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置：`model` 字典声明三大组件、Pack 键白名单、batch_size 等 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器 `Grounded_Decoder_Joint`。本讲只看它 `forward` 的**返回契约**（训练/推理两个分支各返回什么字典），内部机制留给第 4、5 单元 |
| [uniseg3d/unified_criterion.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py) | `Part3DGlammCriterion_v2`：把分割损失与语言损失合并，本讲只看调用签名 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | 评估配置：提供 `predict()` 后处理需要的 `test_cfg.sp_score_thr` 等字段 |

一个容易迷路的提示：`uniseg3d.py` 全文 3000+ 行、四个结构相似的类。读的时候**先认准类名和行号区间**（`Grounded_LMM_main` 在 L906–L1327），否则很容易把父类 `UniSeg3D` 的同名方法当成入口类的逻辑读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：`__init__` 组件构建、`loss()` 前向链路、`predict()` 与 mask dump。

### 4.1 `__init__`：三大组件的构建方式

#### 4.1.1 概念说明

`Grounded_LMM_main` 是一个「组装者」：它自己几乎不含可学的重活，而是把三件事委托给三个可配置的子组件——

| 属性 | 配置来源（train config） | 实际类 | 职责 |
| --- | --- | --- | --- |
| `self.unet` | `model.backbone` | `SpConvUNet` | 稀疏卷积 U-Net，逐点/逐体素特征提取（u3-l2 详解） |
| `self.decoder` | `model.decoder` | `Grounded_Decoder_Joint` | Qwen3-VL + 查询解码器，语言推理与掩码预测（u4、u5 详解） |
| `self.criterion` | `model.criterion` | `Part3DGlammCriterion_v2` | 分割损失 + 语言损失的组合器（u5-l2 详解） |

三者都通过 `MODELS.build(cfg)` 从注册表构建——这正是 u1-l3 讲过的机制：配置里的 `type` 字符串查表得到类。除这三件外，`__init__` 还直接用 `spconv` / `torch.nn` 搭了两个小件：`input_conv`（SubMConv3d 稀疏卷积入口）和 `output_layer`（BatchNorm1d + ReLU 出口），它们与 `unet` 一起构成 3D 主干。

另有一批「看起来能配、实际上不用」的参数，是理解这个类最快的一把钥匙，详见 4.1.3 末尾的对照表。

#### 4.1.2 核心流程

```text
__init__(配置字段...)
  ├─ super().__init__(data_preprocessor, init_cfg)     # 注册数据预处理器
  ├─ self.unet      = MODELS.build(backbone)            # SpConvUNet
  ├─ self.decoder   = MODELS.build(decoder)             # Grounded_Decoder_Joint（内含 Qwen3-VL + LoRA）
  ├─ self.criterion = MODELS.build(criterion)           # Part3DGlammCriterion_v2
  ├─ 记录标量超参：voxel_size / num_classes / query_thr / train_cfg / test_cfg ...
  ├─ self._init_layers(in_channels, num_channels)       # input_conv + output_layer
  ├─ is_type_embedding=True → 建 self.type_embedding    #（本类中不再被引用）
  └─ pred_iou=True → 建 self.pred_iou_proj 三层 MLP     #（本类中不再被引用）
```

#### 4.1.3 源码精读

**① 类注册与继承**。类通过装饰器注册进 `MODELS`，配置里 `type='Grounded_LMM_main'` 即解析到它；它混入 `UniSeg3DMixin`（一组可复用的预测后处理方法）并继承 `Base3DDetector`：

- [uniseg3d/uniseg3d.py:L905-L906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L905-L906)：`@MODELS.register_module()` 注册 `class Grounded_LMM_main(UniSeg3DMixin, Base3DDetector)`。

**② 三大组件构建**。注意这里 `super()` 的写法是 `super(Base3DDetector, self)`——跳过 `Base3DDetector` 自身的 `__init__`，直接调用更上层 `BaseModule` 的初始化（只注册 `data_preprocessor` 与 `init_cfg`，不执行检测器特有的逻辑）：

- [uniseg3d/uniseg3d.py:L954-L958](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L954-L958)：依次调用上层初始化，然后 `MODELS.build` 依次构建 `self.unet`、`self.decoder`、`self.criterion`——整个模型的核心组件只有这三行加两个小层。

**③ 内联小层**。`_init_layers` 用稀疏卷积搭输入投影、用普通 BN+ReLU 搭输出层：

- [uniseg3d/uniseg3d.py:L991-L1002](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L991-L1002)：`input_conv` 是 kernel=3 的 `SubMConv3d`（子流形稀疏卷积，只在被占体素上计算），把 `in_channels=6` 升到 `num_channels=32`；`output_layer` 是 `BatchNorm1d + ReLU`。

**④ 配置到代码的映射**。训练配置里的 `model` 字段逐项对应构造参数：

- [configs/ground3dlmm_train.py:L28-L42](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L28-L42)：`type='Grounded_LMM_main'`、`in_channels=6`（XYZ+RGB）、`voxel_size=0.02`、`min_spatial_shape=128`、`query_thr=0.5`、`set_query_mask=True`、`set_all_mask=True`、`is_type_embedding=True` 等标量。
- [configs/ground3dlmm_train.py:L43-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L43-L46)：`backbone=dict(type='SpConvUNet', num_planes=[32,64,96,128,160], return_blocks=True)`——`num_planes` 由 `num_channels * (i+1)` 推导。
- [configs/ground3dlmm_train.py:L47-L78](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L78)：`decoder=dict(type='Grounded_Decoder_Joint', ...)`，含 `qwen_model_path`（L68）、`base_model='qwen3_vl'` 与 `max_point_tokens=2000`（L75-L76）等 LMM 侧字段。
- [configs/ground3dlmm_train.py:L79-L93](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L79-L93)：`criterion=dict(type='Part3dGlammCriterion_v2', ...)`，内嵌 `TextPrompt_Criterion_Glamm`（分割）与 `CoTCriterion`（语言）两个子损失。

**⑤ 遗留开关对照表（重要）**。下面这些参数会被正常构建、写进 state_dict，但在 `Grounded_LMM_main` 自己的 `loss()` / `predict()` 里**没有任何调用**（调用处已被注释或不存在）：

| 参数 / 属性 | 构建位置 | 在本类中的状态 |
| --- | --- | --- |
| `query_thr`、`_select_queries` | [uniseg3d/uniseg3d.py:L1066-L1094](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1066-L1094) 定义；调用处 [L1137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1137) 已注释 | 死代码。查询改由解码器内部从 `<SEG>` 隐状态产生 |
| `set_query_mask` / `set_all_mask`（interaction mask） | 存于 [L967-L968](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L967-L968)；loss 中的构造块 [L1140-L1162](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1140-L1162) 整段注释 | 死配置。解码器自建 `~eye` 掩码（u5-l1） |
| `is_type_embedding` → `type_embedding` | [L970-L971](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L970-L971) 创建 | 建而未用 |
| `pred_iou` → `pred_iou_proj` / `get_pred_iou` | [L976-L982](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L976-L982) 创建，[L1189](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1189) 定义；loss 中的调用 [L1180-L1181](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1180-L1181) 已注释 | 建而未用 |

这张表也解释了训练配置第 6 行的 [find_unused_parameters = True](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L6)：存在建而不用的参数（外加 LoRA 场景本身的未激活分支），DDP 必须放宽「所有参数都收到梯度」的检查，否则训练直接报错。初学者常在这两处浪费时间——以为 `query_thr=0.5` 会影响训练，或想不通为什么要 `find_unused_parameters`。

#### 4.1.4 代码实践：用 Config 只读检查组件树（无需 GPU/数据）

1. **实践目标**：不构建模型（不下 Qwen 权重、不用 GPU），仅解析配置，验证「配置 type 字符串 → 三大组件」的对应关系，并确认遗留开关的取值。
2. **操作步骤**：在仓库根目录、u1-l2 装好的环境里执行：

   ```bash
   python -c "
   from mmengine.config import Config
   c = Config.fromfile('configs/ground3dlmm_train.py')
   m = c.model
   print('entry      :', m.type)
   print('backbone   :', m.backbone.type, m.backbone.num_planes)
   print('decoder    :', m.decoder.type, 'd_model =', m.decoder.d_model)
   print('criterion  :', m.criterion.type)
   print('  seg loss :', m.criterion.lang_seg_criterion.type)
   print('  lang loss:', m.criterion.lang_criterion.type)
   print('dead flags : query_thr =', m.query_thr,
         'set_query_mask =', m.set_query_mask,
         'is_type_embedding =', m.is_type_embedding,
         'pred_iou =', m.pred_iou)
   "
   ```

3. **需要观察的现象**：`Config.fromfile` 会执行配置文件并递归合并 `_base_`（这里会去 mmdet3d 包里找基配置，所以必须在装好 mmdet3d 的环境运行），但**不会**触发 `custom_imports` 的导入，因此不会下载模型权重。
4. **预期结果**：打印出 `entry: Grounded_LMM_main`、`backbone: SpConvUNet [32, 64, 96, 128, 160]`、`decoder: Grounded_Decoder_Joint d_model = 256`、`criterion: Part3DGlammCriterion_v2`（子损失为 `TextPrompt_Criterion_Glamm` 与 `CoTCriterion`），以及四个遗留开关的取值（`0.5 / True / True / True`）。若报找不到 `_base_` 配置，说明当前环境没装 mmdet3d——回看 u1-l2。
5. 若无法确定运行结果（例如 mmdet3d 版本差异导致 `_base_` 路径解析失败），标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：配置里 `backbone.num_planes` 写的是 `[num_channels * (i + 1) for i in range(5)]`，`num_channels=32`。不运行代码，说出它的值，并解释这个列表在 U-Net 里的含义。
**答案**：`[32, 64, 96, 128, 160]`，即 U-Net 自底向上 5 个尺度的通道数（每下采样一层通道加 32）；u3-l2 会看到解码侧按相反顺序用它做上采样。

**练习 2**：如果把 `model.pred_iou` 改成 `False`，模型的数值行为会变吗？为什么？
**答案**：不会。`pred_iou_proj` 在 `Grounded_LMM_main` 的 loss/predict 中从未被调用（调用处已注释），它既不参与前向也不参与损失；唯一差别是 state_dict 里少一组参数、`find_unused_parameters` 的压力略小。

**练习 3**：`__init__` 里为什么写 `super(Base3DDetector, self).__init__(...)` 而不是 `super().__init__(...)`？
**答案**：这是跳过 `Base3DDetector.__init__`、直接调用其父类（`BaseModule`）的初始化，只注册 `data_preprocessor` 与 `init_cfg`，避免执行基类检测器里多余的构造逻辑。这也是从 UniSeg3D 原项目沿袭下来的写法。

### 4.2 `loss()`：训练前向链路与 batch_offsets 对齐

#### 4.2.1 概念说明

`loss()` 要解决的核心问题是：**一个 batch 里有 B 个场景，每个场景的超点编号都从 0 开始，直接混在一起会撞号**。解决办法是给每个场景的超点 ID 加一个累计偏移，把所有场景的超点「平铺」进一个全局编号空间，池化出 `(M_total, C)` 的超点特征矩阵，再按偏移表切回逐场景。这套机制就是 `batch_offsets`。

同时，`loss()` 是「两条数据通道的汇合点」：`points`/`elastic_coords` 走前向加工，`sp_pts_mask`/`sp_gt_seg` 走监督对齐，最后在 `criterion` 处合并。

一个必须建立的直觉：**`batch_offsets` 只服务于池化这一步**。`extract_feat` 切回逐场景后，特征又是场景局部的了；GT `sp_gt_seg` 本来就是场景局部的（u2-l4 产出），因此两者天然对齐，无需再偏移。

#### 4.2.2 核心流程

```text
loss(batch_inputs_dict, batch_data_samples)
  1. 对每个场景 i（按顺序）：
       sp_pts_mask_i += superpoint_bias          # 原地平移到全局编号
       superpoint_bias = max(sp_pts_mask_i) + 1  # 更新累计偏移
       batch_offsets.append(superpoint_bias)     # 偏移表 = [0, M0, M0+M1, ...]
       sp_gt_masks_i = torch.cat(sp_gt_seg_i, dim=0)   # 该场景所有 QA 的 GT 按行拼接
  2. collate(points, elastic_coords)             # ME 组装稀疏张量（训练用弹性坐标）
  3. spconv.SparseConvTensor + hstack(sp_pts_masks)    # 得到全局逐点超点 ID（长度 ΣN_i）
  4. extract_feat(...)                           # 稀疏卷积 + scatter_mean 全局池化 + 按 offsets 切回
  5. 计算超点质心 point_positions（同样全局池化后按 offsets 切回）
  6. x = decoder(x, scene_names, qa_data, point_positions)
       └─ 返回 dict：original_queries / masks / scores / aux_outputs / language_losses
  7. loss = criterion(x, sp_gt_masks, language_losses, original_queries)
```

**偏移算法的数学描述**。设第 \(i\) 个场景有 \(M_i\) 个超点、\(N_i\) 个点，场景内超点 ID 记为 \(s_i(p) \in \{0,\dots,M_i-1\}\)，则全局 ID 与偏移表为：

\[
\tilde{s}_i(p) = s_i(p) + \sum_{j<i} M_j, \qquad
\mathrm{offsets}_k = \sum_{j<k} M_j,\quad k=0,\dots,B
\]

池化把点级特征 \(h_p\) 聚合成全局超点特征：

\[
f_m = \frac{1}{|P_m|}\sum_{p \in P_m} h_p, \qquad m \in \{0,\dots,M_{\text{total}}-1\},\quad M_{\text{total}} = \sum_j M_j
\]

随后第 \(i\) 个场景取回 \(f_{\mathrm{offsets}_i : \mathrm{offsets}_{i+1}}\)，形状 \((M_i, C)\)。注意 \(M_i\) 恰好等于该场景超点数，是因为偏移表就是按「上一场景最大 ID + 1」累加的。

#### 4.2.3 源码精读

**① 开头的显存清理**：

- [uniseg3d/uniseg3d.py:L1109](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1109)：`torch.cuda.empty_cache()`。解码器里跑 4B 参数的 Qwen，显存压力大，训练配置还配了 `EmptyCacheHook(after_iter=True)`（[configs/ground3dlmm_train.py:L215](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L215)），两处都是在为 LMM 让路。

**② batch_offsets 循环（本模块核心）**：

- [uniseg3d/uniseg3d.py:L1111-L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1111-L1125)：对每个样本先 `gt_pts_seg.sp_pts_mask += superpoint_bias`（**原地修改** data sample），再取 `max().item() + 1` 作为新的累计偏移并 `append` 进 `batch_offsets`；同时把该场景的 `sp_gt_seg`（逐 QA 张量列表）沿 `dim=0` 拼成一张 GT 表收进 `sp_gt_masks`。两个阅读提示：L1118 的 `batch_data_samples[i].gt_pts_seg` 是一行什么都不做的遗留语句；`+=` 是原地操作，但因为每个迭代的数据样本都是新对象，不会造成跨迭代污染。

**③ 稀疏批组装与全局超点 ID**：

- [uniseg3d/uniseg3d.py:L1127-L1129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1127-L1129)：调用 `self.collate(points, elastic_coords)`。传了 `elastic_coords` 时（训练管线由 `ElasticTransfrom` 生成，见 u2-l4），量化坐标用加噪副本、特征仍用原始点——这是训练期的数据增强入口。
- [uniseg3d/uniseg3d.py:L1131-L1133](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1131-L1133)：把坐标/特征包成 `spconv.SparseConvTensor`（batch_size = 场景数），并把各场景已平移过的 `sp_pts_mask` `hstack` 成一个长度 \(\sum_i N_i\) 的全局逐点超点 ID 向量。

**④ 池化与切回**（`extract_feat` 与 `collate` 的实现在 u3-l3 逐行精读，这里看调用契约）：

- [uniseg3d/uniseg3d.py:L1134-L1135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1134-L1135)：`extract_feat(x, sp_pts_masks, inverse_mapping, batch_offsets)` 返回「长度为 B 的列表，第 i 项 `(M_i, C)`」。
- [uniseg3d/uniseg3d.py:L1022-L1025](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1022-L1025)：`extract_feat` 内部——先用 `inverse_mapping` 把体素特征映射回原始点，`scatter_mean` 按**全局**超点 ID 池化成 `(M_total, C)`，再用 `batch_offsets[i]: batch_offsets[i+1]` 切回逐场景。这就是「先全局、再切回」的全部实现，只有两行。

**⑤ 超点质心**：

- [uniseg3d/uniseg3d.py:L1164-L1167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1164-L1167)：把所有场景的原始 XYZ `vstack` 后，按同一个全局超点 ID 向量 `scatter_mean` 出每个超点的质心，再按 `batch_offsets` 切回逐场景，得到 `point_positions`（第 i 项 `(M_i, 3)`）。它与超点特征使用**同一套偏移机制**，只是池化的对象从卷积特征换成了坐标。

**⑥ 交给解码器并收损失**：

- [uniseg3d/uniseg3d.py:L1169-L1173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1169-L1173)：取出 `scene_name` 与 `selected_qa_data` 两个来自数据管线的键，连同超点特征和质心一起喂给 `self.decoder(...)`。注意变量 `x` 在此处被重新绑定：从「逐场景超点特征列表」变成「解码器输出的字典」。解码器训练分支的返回字典见 [uniseg3d/query_decoder.py:L1899-L1910](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1899-L1910)：`original_queries`、`masks`、`scores`、`aux_outputs`、`language_losses`。
- [uniseg3d/uniseg3d.py:L1184](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1184)：`loss = self.criterion(x, sp_gt_masks, language_losses, original_queries=x['original_queries'])`。`Part3DGlammCriterion_v2` 的实现只有几行（[uniseg3d/unified_criterion.py:L127-L141](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py#L127-L141)）：`lang_seg_criterion` 吃 `(pred, insts, original_queries)` 算分割损失，`lang_criterion` 吃 `language_losses` 算语言损失，两个字典 `update` 合并后返回。**本类不算任何损失，只负责搬运与对齐。**

顺带说明 L1137–L1162 的大段注释：那是从 `UniSeg3D` 拷贝来又废弃的旧查询选择 / interaction mask 逻辑，保留下来只为对照。第一次阅读时建议直接跳过，以免误以为它们参与计算。

#### 4.2.4 代码实践：纸上推演 + 脚本复现 batch_offsets

1. **实践目标**：亲手验证偏移算法——给定两个场景的超点标签，算出 `batch_offsets`、全局 ID、池化切分区间。
2. **操作步骤**：以下为**示例代码**（模拟 `loss()` L1111–L1125 与 `extract_feat` L1022–L1025 的行为，可在任意装有 torch 的 CPU 环境运行，与项目其余部分零依赖）：

   ```python
   # simulate_batch_offsets.py（示例代码）
   import torch
   from torch_scatter import scatter_mean

   # 两个场景的超点标签：场景 0 有 3 个超点，场景 1 有 2 个
   sp_masks = [torch.tensor([0, 1, 1, 2, 2, 2]), torch.tensor([0, 0, 1, 1])]
   feats    = [torch.randn(6, 4), torch.randn(4, 4)]   # 每点 4 维特征

   batch_offsets, superpoint_bias, shifted = [0], 0, []
   for m in sp_masks:                       # 对应 loss() 的循环
       m = m + superpoint_bias
       superpoint_bias = m.max().item() + 1
       batch_offsets.append(superpoint_bias)
       shifted.append(m)

   sp_all  = torch.hstack(shifted)          # 全局逐点超点 ID
   feat_all = torch.cat(feats, dim=0)
   pooled  = scatter_mean(feat_all, sp_all, dim=0)   # (M_total, 4)

   print('batch_offsets:', batch_offsets)   # 期望 [0, 3, 5]
   print('sp_all       :', sp_all.tolist())
   for i in range(len(batch_offsets) - 1):  # 对应 extract_feat 的切回
       print(f'scene {i} superpoint feats:', pooled[batch_offsets[i]:batch_offsets[i+1]].shape)
   # 期望输出:
   # batch_offsets: [0, 3, 5]
   # sp_all       : [0, 1, 1, 2, 2, 2, 3, 3, 4, 4]
   # scene 0 superpoint feats: torch.Size([3, 4])
   # scene 1 superpoint feats: torch.Size([2, 4])
   ```

   再做一个反例实验：把循环里的 `m = m + superpoint_bias` 一行删掉（模拟「忘记平移」），观察 `scatter_mean` 后的形状与切分错位——场景 1 的超点 0/1 会与场景 0 的超点 0/1 混在一起。
3. **需要观察的现象**：偏移表首项恒为 0、末项等于全局超点总数；`sp_all` 中场景 1 的 ID 从 3 开始；删除平移后 `pooled` 行数变少（撞号合并），两个场景的切分区间互相覆盖。
4. **预期结果**：与代码注释中的「期望输出」一致；反例中 `pooled.shape[0] < 5`，即不同场景的同名超点被错误平均。
5. 想进一步对齐真实代码：把 `feats` 换成 `(N, 6)` 的点云坐标，用同样的 `scatter_mean(sp_all)` 求质心，即可复现 L1165–L1167 的 `point_positions` 计算。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sp_gt_seg` 不需要像 `sp_pts_mask` 一样加偏移？
**答案**：`sp_gt_seg` 的列维度对应**场景内**超点（u2-l4 产出时就是场景局部的），而 `extract_feat` 在池化后已按 `batch_offsets` 把特征切回逐场景，预测掩码与 GT 都生活在场景局部坐标系里；偏移只在池化那一步内部短暂存在。

**练习 2**：`loss()` 中 GT 用 `torch.cat(sp_gt_seg, dim=0)` 拼接，`predict()` 中却保留 list 不拼。为什么？
**答案**：训练时解码器把一个场景全部 QA 的查询合在一趟前向里输出掩码，损失需要与「整场景所有 `<p>` 标签」对齐，所以按行拼成一张表；推理时是逐 QA 处理、逐 QA 对齐 GT（见 4.3），拼起来反而无法对应。

**练习 3**：若把训练配置的 `batch_size` 从 1 改成 2，`loss()` 里的这段代码需要改动吗？
**答案**：不需要。整段逻辑本来就是按 `len(batch_data_samples)` 写的通用实现；真正的瓶颈在解码器里 Qwen 的显存占用（这也是配置取 `batch_size=1` 的原因，见 [configs/ground3dlmm_train.py:L162-L163](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L162-L163)）。

### 4.3 `predict()`：推理链路与 mask dump

#### 4.3.1 概念说明

`predict()` 与 `loss()` 共用同一套 3D 主干（offsets 循环 → collate → 稀疏卷积 → 池化切回 → 质心），差异集中在两端：

- **入口端**：不传 `elastic_coords`（推理不做弹性增强，用原始坐标量化）。
- **出口端**：走解码器的**推理分支**（`infer_reason_eval`：先 `generate` 生成文本，再做一次前向取 `<SEG>` 隐状态，逐 QA 产出查询），随后 `qa_predict_by_feat` 把超点级 logits 二值化并**上采样到点级**、与 GT 逐 QA 对齐，最后可选地把预测/GT 掩码 dump 成 `.npy` 供可视化（u6-l4 消费）。

理解本模块的关键是「**超点级 → 点级**」的上采样算子：对形状 `(n_rows, M)` 的超点级掩码做 `mask[:, superpoints]`，其中 `superpoints` 是长度为 N 的逐点超点 ID 向量，结果是 `(n_rows, N)`——每个点的取值就是它所属超点那一列。预测与 GT 用**同一个**向量扩展，天然逐点对齐。

#### 4.3.2 核心流程

```text
predict(batch_inputs_dict, batch_data_samples)
  1. 与 loss 相同的 offsets 循环（但 sp_gt_seg 保持 list，不 cat）
  2. collate(points)                      # 无 elastic_coords
  3. SparseConvTensor → hstack → extract_feat（切回逐场景）
  4. 质心 point_positions（同 loss）
  5. x = decoder(x, scene_names, qa_data, point_positions)   # 推理分支
       └─ 返回 dict：original_queries / masks（嵌套 [B][QA]）/ language_losses（推理态不使用）
  6. results_list = qa_predict_by_feat(x, sp_pts_masks, sp_gt_masks)
       对每个 batch、每条 QA：
         mask_logits → sigmoid → > test_cfg.sp_score_thr → mask[:, superpoints] 上采样
         若该 QA 有 GT：GT[:, superpoints] 上采样；行数不足补零行、超出则截断
       → PointData(qa_pred_masks, qa_gt_masks)
  7. data_sample.pred_pts_seg = results[i]
  8. 可选 mask dump：环境变量 MASK_DUMP_DIR 或 decoder.save_viz_dir
       → 每场景写 {scene}_qa_pred_masks.npy / {scene}_qa_gt_masks.npy
```

#### 4.3.3 源码精读

**① offsets 循环（与 loss 的差别）**：

- [uniseg3d/uniseg3d.py:L1272-L1284](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1272-L1284)：同样的平移与偏移表构造；唯一差别是 `sp_gt_masks.append(batch_data_samples[i].gt_pts_seg.sp_gt_seg)`——保留逐 QA 的张量列表，不拼接。

**② 无弹性坐标的稀疏组装**：

- [uniseg3d/uniseg3d.py:L1286-L1293](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1286-L1293)：`self.collate(batch_inputs_dict['points'])` 只传一个参数，走 `collate` 里 `elastic_points is None` 的分支，用原始坐标除以 `voxel_size` 量化（对比 loss 的双参调用）。
- [uniseg3d/uniseg3d.py:L1296-L1299](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1296-L1299)：质心计算与 loss 完全相同（这段代码在两个方法间是复制粘贴关系）。

**③ 解码器推理分支的返回契约**：

- [uniseg3d/uniseg3d.py:L1301-L1305](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1301-L1305)：取出 `scene_name` / `selected_qa_data`（L1301–L1302 是重复赋值同一变量的无害冗余行），调用 `self.decoder(...)`。推理分支的返回见 [uniseg3d/query_decoder.py:L1954-L1959](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1954-L1959)：`masks` 是嵌套列表 `[batch][qa]`，每项是**最后一层**解码器对该 QA 的掩码 logits；`language_losses` 在推理态由 `infer_reason_eval` 返回但不进入损失。

**④ `qa_predict_by_feat`：二值化、上采样与对齐**：

- [uniseg3d/uniseg3d.py:L1218-L1222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1218-L1222)：对该 QA 的掩码 logits 做 `sigmoid`，用 `self.test_cfg.sp_score_thr` 阈值化成 bool，再 `mask_pred[:, superpoints]` 把超点级掩码按逐点超点 ID 扩展成点级。`sp_score_thr=0.4` 来自评估配置的 [configs/ground3dlmm_eval_ground3d_scannet.py:L92-L98](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L92-L98)（训练配置里没有 `model.test_cfg`，所以这个字段只在 eval 配置里给）。
- [uniseg3d/uniseg3d.py:L1226-L1233](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1226-L1233)：**GT 对齐规则**——只有当该 QA 存在对应 GT（`i_qa_pred < len(gt_masks[i_batch])`）时才对齐：先把 GT 同样按 `superpoints` 上采样到点级；若 GT 行数多于预测（一条回答里有多个 `<p>` 但解码器只出了部分查询），用**零行**补齐预测；反之截断多余预测行。源码注释（L1223–L1225）解释了动机：多轮 QA 每轮都会产出一条预测，但 GT 只存在于发生接地的轮次，因此「有 GT 才对齐，没 GT 保留原始预测」。
- [uniseg3d/uniseg3d.py:L1240-L1246](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1240-L1246)：每个场景打包成 `PointData(qa_pred_masks=..., qa_gt_masks=...)` 返回——这两个键正是 u6-l1 分割指标的输入。

**⑤ 写回与 mask dump**：

- [uniseg3d/uniseg3d.py:L1306-L1308](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1306-L1308)：结果写入 `data_sample.pred_pts_seg`，随后由 mmengine 的 TestLoop 交给 evaluator（`Part3DGlamm_SegMetric`）。
- [uniseg3d/uniseg3d.py:L1310-L1325](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1310-L1325)：**mask dump 开关**——读取环境变量 `MASK_DUMP_DIR` 或 `self.decoder.save_viz_dir`，任一非空则按场景写出两个 `dtype=object` 的 object 数组：`{scene}_qa_pred_masks.npy` 与 `{scene}_qa_gt_masks.npy`（`allow_pickle=True`，因为每条 QA 的掩码行数不同）。这两个文件是 u6-l4 彩色 PLY 可视化的原料。

#### 4.3.4 代码实践：跑一次评估并打开 mask dump

1. **实践目标**：在 u1-l4 已经跑通的推理流程上加一个环境变量，把逐 QA 的预测/GT 掩码落盘，观察 `predict()` 出口端的真实产物。
2. **操作步骤**（在完成 u1-l4 环境与数据准备的前提下）：

   ```bash
   MASK_DUMP_DIR=data/mask_dump python tools/test.py \
       configs/ground3dlmm_eval_ground3d_scannet.py \
       <checkpoint路径> \
       --cfg-options task_level=object_qa task_type=grounding \
       test_dataloader.batch_size=1
   ```

   结束后检查目录：

   ```bash
   ls data/mask_dump | head
   python -c "
   import numpy as np
   a = np.load('data/mask_dump/<某场景名>_qa_pred_masks.npy', allow_pickle=True)
   print(type(a), len(a))          # ndarray, QA 条数
   print(a[0].shape, a[0].dtype)   # (n_rows, N_points), bool —— 已是点级
   "
   ```

3. **需要观察的现象**：每个评估场景产生两个 `.npy`；`len(a)` 等于该场景评估到的 QA 条数；`a[0].shape[1]` 等于该场景点数（说明已完成 `[:, superpoints]` 上采样）；`a[0].shape[0]` 是该 QA 的掩码行数（含 GT 行数不足时的补零行）。
4. **预期结果**：掩码数组为 bool 型、点级宽度；若某 QA 无 GT，对应预测掩码仍会写出（对齐规则里「没 GT 保留原始预测」）。整个流程需要 GPU 与 checkpoint，本环境未验证，**待本地验证**。
5. **无 GPU 的替代实践（源码阅读型）**：对照 L1226–L1233 写出三种情况的行为表——GT 行数 = / > / < 预测行数，分别发生什么；再用 u2-l4 的结论回答「一条回答含 2 个 `<p>` 标签时 GT 有几行」。

#### 4.3.5 小练习与答案

**练习 1**：`qa_predict_by_feat` 里 `mask_pred = mask_pred[:, superpoints]`，`superpoints` 是全局 hstack 后的 ID 向量。为什么评估时它恰好等于「单场景」的超点 ID？
**答案**：因为评估配置的 `test_dataloader.batch_size=1`（[configs/ground3dlmm_eval_ground3d_scannet.py:L215](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L215)），batch 里只有一个场景，全局 ID 与场景内 ID 一致（偏移为 0）。
**练习 2**：为什么 dump 出的 `.npy` 要 `dtype=object` + `allow_pickle=True`，而不是普通的二维数组堆叠？
**答案**：不同 QA 的掩码行数不同（由答案中 `<p>` 标签个数决定，且对齐后可能补零行），列长（点数）也随场景变化，无法规整堆叠成同形数组，只能存 object 数组逐条保存。
**练习 3**：`predict()` 里有没有计算语言损失？`x['language_losses']` 去了哪里？
**答案**：没有使用。推理分支的解码器返回里虽有 `language_losses`，但 `predict()` 既不取也不传给任何 criterion——语言侧在推理时的产物是写盘的预测 JSON（由解码器内部完成，见 u4-l5），损失只在训练态存在。

## 5. 综合实践

**任务：双场景、双 QA 的全链路纸上推演 + 脚本验证。** 把本讲三个模块串起来：

设定场景 A 有 4 个超点（逐点标签 `[0,0,1,1,2,3,3]`）、场景 B 有 3 个超点（`[0,1,1,2,2]`）；场景 A 的 `selected_qa_data` 选了 2 条 QA，其中一条答案含 1 个 `<p>`、另一条含 2 个 `<p>`。

1. 手推：写出 `batch_offsets`、全局 `sp_all`、`extract_feat` 切分后两个场景的特征形状、`point_positions` 两个元素的形状；再写出场景 A 拼接后 `sp_gt_masks` 的形状（行 = 1+2 = 3，列 = 4）。
2. 用 4.2.4 的示例脚本验证前三问。
3. 模拟推理侧对齐：假设解码器给含 2 个 `<p>` 的那条 QA 只输出了 1 行掩码 logits（形状 `(1, 4)`），按 L1218–L1233 的规则写出对齐后 `qa_pred_masks` 的形状与补齐方式（答案：先阈值化、`[:, superpoints]` 上采样到 `(1, 7)`，GT 上采样为 `(2, 7)`，预测补一行零 → `(2, 7)`）。
4. 把手推结果与 4.3.4 落盘的 `.npy`（若已在本地跑通评估）对照：`a[i].shape[0]` 是否等于对应 QA 的 `<p>` 个数（或补齐后的行数）。

预期收获：`batch_offsets` 从「循环里两行代码」变成你可以徒手执行的确定过程，且能一眼判断「形状不对齐」类 bug 出现在哪一环。

## 6. 本讲小结

- `Grounded_LMM_main` 是纯组装者：`__init__` 用 `MODELS.build` 构建 `SpConvUNet` / `Grounded_Decoder_Joint` / `Part3dGlammCriterion_v2` 三大组件，外加 `input_conv` / `output_layer` 两个内联小层；损失计算全部委托给 criterion。
- `query_thr` / `set_query_mask` / `is_type_embedding` / `pred_iou` 等配置项在本类的 loss/predict 中是死开关（调用被注释或不存在），却仍产生参数——这正是配置里 `find_unused_parameters = True` 的原因之一。
- `batch_offsets` 机制：逐场景把 `sp_pts_mask` 原地加累计偏移、`max()+1` 累进，得到 `[0, M0, M0+M1, ...]`；`extract_feat` 先按全局 ID `scatter_mean` 池化成 `(M_total, C)`，再按偏移切回逐场景。偏移只在池化阶段存在，切回后一切回到场景局部。
- `loss()` 与 `predict()` 共用 offsets → collate → 稀疏卷积 → 池化 → 质心这段主干；差异在两端：训练传 `elastic_coords`、GT 按行 `cat`；推理不传弹性坐标、GT 保持逐 QA 列表，走 `infer_reason_eval` 分支。
- `qa_predict_by_feat` 统一用 `mask[:, superpoints]` 把超点级预测/GT 上采样到点级，并按「有 GT 才对齐、不足补零、超出截断」的规则对齐行数，产出 `PointData(qa_pred_masks, qa_gt_masks)` 供指标与可视化消费。
- mask dump 通过环境变量 `MASK_DUMP_DIR` 或 `decoder.save_viz_dir` 开启，按场景写出两个 object 数组 `.npy`，是 u6-l4 可视化的数据源。

## 7. 下一步学习建议

- 下一讲 **u3-l2 稀疏卷积主干 SpConvUNet**：深入 `self.unet` 内部——`ResidualBlock`、`indice_key` 复用、五级 U-Net 与 `return_blocks` 多尺度返回，弄清 `extract_feat` 里 `x, _ = self.unet(x)` 那个被丢弃的返回值是什么。
- 之后 **u3-l3 超点池化**：逐行精读本讲只看调用契约的 `collate()`（MinkowskiEngine 的 `TensorField`、`sparse()`、`inverse_mapping`），补全稀疏批组装的细节。
- 若更关心语言侧，可先跳到 **u4-l1 / u4-l2** 看 `self.decoder` 的构建与点云 token 注入，再回读 u5-l1 的解码器逐层结构；`qa_predict_by_feat` 产出的两个键如何在指标里变成 mIoU，见 **u6-l1**。
- 建议带着一个问题读下一讲：`collate` 里为什么需要 `inverse_mapping` 才能把体素特征还给原始点？（提示：多个点可能落进同一体素。）
