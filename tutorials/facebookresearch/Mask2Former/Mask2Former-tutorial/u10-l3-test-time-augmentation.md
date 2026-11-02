# 测试时增强 SemanticSegmentorWithTTA

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出**测试时增强(Test-Time Augmentation, TTA)**的直觉:同一张图做多个增强版本分别推理,再把预测聚合,以换取更稳的结果。
2. 精读 [mask2former/test_time_augmentation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py) 全部 104 行:包装器 `SemanticSegmentorWithTTA` 如何构造、如何逐版本推理、如何把水平翻转的预测「翻回来」、如何聚合。
3. 掌握 `TEST.AUG` 四个配置项(`ENABLED`/`MIN_SIZES`/`MAX_SIZE`/`FLIP`)的含义,并知道本仓库各 config 对它们的实际覆盖情况。
4. 说清 TTA 与普通评估在 `train_net.py` 中的**触发差异**:TTA 只在 `--eval-only` 且 `TEST.AUG.ENABLED=True` 时运行,训练中的周期评估永远走普通路径。
5. 理解 TTA 的适用边界:它只聚合 `sem_seg` 概率图,事实上只服务于语义分割任务;视频版入口则直接 `raise NotImplementedError`。

本讲是单元十(工程化、性能与部署)的第三讲,前置知识主要来自 u6-l1(语义分割推理分支)。

## 2. 前置知识

### 2.1 测试时增强(TTA)是什么

训练时我们会做数据增强(随机缩放、翻转、裁剪)让模型见过更多样的输入;TTA 把同样的想法搬到**推理时**:对一张测试图生成若干增强版本,逐版本送入同一个模型,再把各版本的预测**在统一坐标系下聚合**(通常是平均或投票)。

直觉:模型对单个输入的预测总带有噪声——某个像素的分类可能在 512 分辨率下判为 A、在 896 分辨率下判为 B。多个视角的平均能抹平这种抖动,通常带来小幅但稳定的指标提升,代价是推理次数成倍增加。

### 2.2 增强的「可逆性」:聚合的前提

TTA 使用的增强必须是**几何上可逆的**,否则各版本的预测无法对齐到同一坐标系。本实现涉及两种:

| 增强 | 输入侧操作 | 聚合时的逆操作 |
| --- | --- | --- |
| 多尺度缩放(ResizeShortestEdge) | 把最短边缩放到指定尺寸 | **无需显式逆操作**,因为模型内部的 `sem_seg_postprocess` 已把输出插值回原图尺寸(见 4.2.1) |
| 水平翻转(HFlip) | 图像左右镜像 | `sem_seg.flip(dims=[2])`,把预测图沿宽维翻回 |

### 2.3 回顾:sem_seg 是一张「原图尺寸的每类概率图」

承接 u6-l1:MaskFormer 推理时,`semantic_inference` 用 softmax(去掉 no-object 通道)与 sigmoid mask 做 `einsum("qc,qhw->chw")`,把 Q 个 per-mask 预测融合成 K 张逐类概率图;随后 `sem_seg_postprocess` 把它插值回数据集 dict 里记录的原始 `height × width`。也就是说,**不管网络输入是什么分辨率,模型输出的 `sem_seg` 永远是 (K, H_原图, W_原图) 的概率图**——这正是 TTA 各版本能直接逐像素相加的根本前提。

### 2.4 包装器(Wrapper)模式

`SemanticSegmentorWithTTA` 不是新的 meta architecture,也不训练、不修改内层模型的任何权重。它把一个已构建好的模型「包」起来,对外提供与内层模型**完全相同的调用接口**(接收 `batched_inputs`、返回 `processed_results`)。这样,detectron2 的评估器根本感觉不到自己收到的是一个 TTA 包装器——同一套 `SemSegEvaluator` 代码无需任何改动。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mask2former/test_time_augmentation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py) | 本讲主角,仅 104 行:TTA 包装器的全部实现 |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | `test_with_TTA` 与 `main` 的触发路径 |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 被包装的内层模型:eval 分支产出原图尺寸 sem_seg |
| [configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml) | 覆盖 `TEST.AUG` 的尺度列表(论文 TTA 结果的配置来源) |
| [train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py) | 视频版对 TTA 直接抛 `NotImplementedError` 的反例 |
| (detectron2 包,非本仓库文件)[detectron2/data/test_time_augmentation.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/data/test_time_augmentation.py) | `DatasetMapperTTA` 的实现,负责生成增强版本 |
| (detectron2 包,非本仓库文件)[detectron2/config/defaults.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/config/defaults.py) | `TEST.AUG` 各键的默认值 |

> 注意:最后两个文件属于 detectron2 依赖包,不在本仓库内,且行号随版本漂移,故本讲只链接到文件(不锚定行号),并给出本地核对命令。

## 4. 核心概念与源码讲解

### 4.1 包装器构造与调用入口:`__init__`、`__call__` 与 `_maybe_read_image`

#### 4.1.1 概念说明

这个模块回答三个问题:

- **包装谁?** 一个已经构建好、(通常)已加载权重的语义分割模型。构造函数还会顺手剥掉 `DistributedDataParallel(DDP)` 外壳——TTA 是纯推理,不需要 DDP 在多进程间做梯度同步通信,直接持有裸模块即可。
- **怎么生成增强版本?** 委托给 `tta_mapper`,默认是 detectron2 提供的 `DatasetMapperTTA`(见 4.3)。
- **接口长什么样?** 与内层模型的 `forward` 完全一致:`batched_inputs`(dataset dict 列表)进,`processed_results`(结果 dict 列表)出。这依赖一个 Python 细节:该类继承了 `nn.Module`,却**自己定义了 `__call__` 方法**,覆盖了 `nn.Module` 的 `_wrapped_call_impl` 分发路径——所以 `tta_model(x)` 直接进入自定义的 `__call__`,并不需要一个 `forward`。

#### 4.1.2 核心流程

`SemanticSegmentorWithTTA(cfg, model)(batched_inputs)` 的执行流程:

```text
__init__(cfg, model)
  ├─ 若 model 被 DDP 包裹 → 取 model.module(剥壳)
  ├─ cfg.clone() 克隆配置,避免污染外部 cfg
  ├─ tta_mapper 未提供 → 默认 DatasetMapperTTA(cfg)
  └─ 记录 batch_size(注意:实际推理中从未使用,见下文)

__call__(batched_inputs)
  ├─ 对每个 dataset dict:
  │    _maybe_read_image:兜底补全
  │      ├─ 缺 "image" 键 → 按模型输入格式读盘,HWC ndarray → CHW tensor
  │      └─ "height" 与 "width" **都**缺失 → 从 image 形状补上
  └─ 逐图调用 _inference_one_image(下一模块)
```

#### 4.1.3 源码精读

**构造函数**——[mask2former/test_time_augmentation.py:L27-L47](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L27-L47):

```python
def __init__(self, cfg, model, tta_mapper=None, batch_size=1):
    super().__init__()
    if isinstance(model, DistributedDataParallel):
        model = model.module          # 剥掉 DDP 外壳
    self.cfg = cfg.clone()            # 克隆,防止意外修改外部配置
    self.model = model
    if tta_mapper is None:
        tta_mapper = DatasetMapperTTA(cfg)   # 默认增强版本生成器
    self.tta_mapper = tta_mapper
    self.batch_size = batch_size      # 存了,但推理时从未读取(死参数)
```

这段代码做包装器的四件事:剥 DDP、克隆 cfg、挂 tta_mapper、记 batch_size。其中 `batch_size` 参数文档写明「batch the augmented images into this batch size for inference」,但通读全文件,`_inference_one_image` 是**逐版本单样本**调用 `self.model([input])`,`self.batch_size` 从未被消费——它是预留而未接线的参数。

**调用入口**——[mask2former/test_time_augmentation.py:L49-L69](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L49-L69):

```python
def __call__(self, batched_inputs):
    def _maybe_read_image(dataset_dict):
        ret = copy.copy(dataset_dict)
        if "image" not in ret:
            image = read_image(ret.pop("file_name"), self.model.input_format)
            image = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1)))  # CHW
            ret["image"] = image
        if "height" not in ret and "width" not in ret:
            ret["height"] = image.shape[1]
            ret["width"] = image.shape[2]
        return ret

    processed_results = []
    for x in batched_inputs:
        result = self._inference_one_image(_maybe_read_image(x))
        processed_results.append(result)
    return processed_results
```

逐图处理(外层 batch 循环,内层版本循环在下一模块)。`_maybe_read_image` 是一个**兜底函数**:detectron2 测试加载器产出的 dict 本来就带 `image` tensor 与 `height`/`width`,此时它只做一次浅拷贝;只有手工构造、仅有 `file_name` 的 dict 才会走读盘分支。

⚠️ **两个值得注意的细节**(都可以当作阅读练习):

1. 读盘分支引用了 `self.model.input_format`——这是一个 detectron2 meta arch 的常见属性,但**本仓库的 `MaskFormer` 并没有定义它**(在 `mask2former/maskformer_model.py` 中全文搜索 `input_format` 无结果)。也就是说,对 MaskFormer 传一个只有 `file_name` 的 dict,会在 `read_image(...)` 处抛 `AttributeError`。正常评估流程不会触发(test loader 已填好 `image`),自己写脚本时要**预先提供 image tensor**。
2. `height`/`width` 的补全条件是「**两者都缺**」(`and`)。若 dict 带 `image` 但只缺 `height`,不会走补全分支,后续语义后处理会取不到原图尺寸。同样,这是边角情况,标准数据管线不会踩到。

#### 4.1.4 代码实践

**实践目标**:确认 `TEST.AUG` 在你本地 detectron2 中的默认值,并验证本仓库 ADE20K 配置对它的覆盖。(源码阅读 + 配置检查型实践,不需要模型权重。)

**操作步骤**:

1. 核对本地 detectron2 的 `TEST.AUG` 默认值:

   ```bash
   python -c "from detectron2.config import get_cfg; print(get_cfg().TEST.AUG)"
   ```

2. 定位本地 `DatasetMapperTTA` 源码文件并通读其 `__call__`:

   ```bash
   python -c "import detectron2.data.test_time_augmentation as m; print(m.__file__)"
   ```

3. 在项目根目录用如下示例代码查看「detectron2 默认值 + ADE20K yaml 覆盖」的合并结果:

   ```python
   # 示例代码:检查 TEST.AUG 的合并结果(无需 GPU 与权重)
   from detectron2.config import get_cfg
   from detectron2.projects.deeplab import add_deeplab_config
   from mask2former import add_maskformer2_config   # import 副作用同时完成组件注册

   cfg = get_cfg()
   add_deeplab_config(cfg)
   add_maskformer2_config(cfg)
   cfg.merge_from_file(
       "configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml"
   )
   print(cfg.TEST.AUG)
   n = len(cfg.TEST.AUG.MIN_SIZES) * (2 if cfg.TEST.AUG.FLIP else 1)
   print(f"开启 TTA 后每张图将推理 {n} 个版本")
   ```

**需要观察的现象**:第 1 步打印的是 detectron2 `defaults.py` 中的默认值(9 档最短边 400–1200、`FLIP=True`、`ENABLED=False`);第 3 步打印的是被 [Base-ADE20K-SemanticSegmentation.yaml:L51-L57](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml#L51-L57) 覆盖后的值(`MIN_SIZES=[256, 384, 512, 640, 768, 896]`、`FLIP=True`),版本数为 6×2=12。

**预期结果**:默认关闭(`ENABLED=False`)、但尺度列表已按数据集定制。detectron2 默认值与 `DatasetMapperTTA` 的具体行为以你本地安装的版本为准(本仓库不携带这两个文件);打印的具体格式**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `__init__` 要把 `DistributedDataParallel` 剥掉?

**答案**:TTA 是 `torch.no_grad()` 下的纯推理,不需要 DDP 的梯度 all-reduce 与进程间同步;DDP 的 forward 会对参数做广播等通信开销。直接持有 `model.module`(裸模块)即可单进程逐版本调用,也避免「包装器包住 DDP、DDP 再包住模型」的层级混乱。

**练习 2**:`batch_size=1` 参数在实际推理中生效吗?

**答案**:不生效。`_inference_one_image` 用 `for input, tfm in zip(augmented_inputs, tfms)` 逐版本调用 `self.model([input])`,单样本、无分批;`self.batch_size` 存储后从未被读取,属于预留的死参数。

**练习 3**:给包装器传入 `{"file_name": "a.jpg"}`(无 `image` 键)会发生什么?

**答案**:走 `_maybe_read_image` 的读盘分支,执行 `read_image(ret.pop("file_name"), self.model.input_format)`;而本仓库的 `MaskFormer` 类没有 `input_format` 属性,将抛出 `AttributeError`。detectron2 自带的 `SemanticSegmentor`(per-pixel 基线的 meta arch,u8-l4)定义了该属性,不会出这个问题——这也是类名「SemanticSegmentorWithTTA」的由来。规避方法:调用前自己读图并填好 `image`/`height`/`width`。

### 4.2 `_inference_one_image`:逐版本推理与等权平均聚合

#### 4.2.1 概念说明

这是 TTA 的心脏,回答「N 个版本的预测如何变成一个预测」。

**聚合公式**:设第 \(v\) 个版本的网络预测为 \(p_v\)(形状 (K, H, W) 的逐类概率图),其逆几何变换为 \(\Phi_v\)(翻转版为沿宽维翻转,缩放版为恒等),则最终预测为**等权算术平均**:

\[
\bar{p}(c, x, y) = \frac{1}{N} \sum_{v=1}^{N} \Phi_v\big(p_v\big)(x, y)
\]

**一个必须澄清的点**:本讲规格与不少资料把该聚合描述为「按分辨率加权」,但源码实现是**等权平均**——先逐版本累加,最后除以版本计数(`final_predictions / count_predictions`),没有任何与分辨率相关的权重。读源码要以代码为准。

**为什么缩放版本不需要显式逆变换?** 因为每个版本的网络输入分辨率不同,但内层模型在 eval 分支里已经把 `sem_seg` 通过 `sem_seg_postprocess` 双线性插值回**原图的 height×width**(见下方精读),输出坐标系天然统一;而水平翻转模型自己是不知道的,必须由包装器在外面把预测翻回来。**为什么平均是合法的?** 每张 sem_seg 的每个像素是 (近似)非负、和约为 1 的类分布(softmax × sigmoid 的融合结果,u6-l1),逐像素平均后仍是合法分布,下游 `SemSegEvaluator` 照常对每像素 argmax。

#### 4.2.2 核心流程

`_inference_one_image(input)` 的执行流程:

```text
1. orig_shape = (input["height"], input["width"])   # 取了但未使用(死变量)
2. augmented_inputs, tfms = _get_augmented_inputs(input)
     ├─ augmented_inputs = tta_mapper(input)   # N 个增强版本 dict
     └─ tfms = [x.pop("transforms") for x in ...]  # 逐版本弹出几何变换记录
3. 对每个 (input_v, tfm_v):
     ├─ 在 torch.no_grad() 下调用 self.model([input_v])
     ├─ 取 [0].pop("sem_seg")  —— 只消费语义概率图
     ├─ 若 tfm_v 中含 HFlipTransform → 预测沿宽维 flip(dims=[2]) 翻回
     └─ 累加到 final_predictions(首个版本为赋值)
4. final_predictions /= 版本数 N   —— 等权平均
5. 返回 {"sem_seg": final_predictions}  —— 输出 dict 只含这一个键
```

#### 4.2.3 源码精读

**核心循环**——[mask2former/test_time_augmentation.py:L71-L98](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L71-L98):

```python
def _inference_one_image(self, input):
    orig_shape = (input["height"], input["width"])        # 记录但未使用
    augmented_inputs, tfms = self._get_augmented_inputs(input)

    final_predictions = None
    count_predictions = 0
    for input, tfm in zip(augmented_inputs, tfms):
        count_predictions += 1
        with torch.no_grad():                             # 推理不建计算图
            if final_predictions is None:                 # 首个版本:赋值
                if any(isinstance(t, HFlipTransform) for t in tfm.transforms):
                    final_predictions = self.model([input])[0].pop("sem_seg").flip(dims=[2])
                else:
                    final_predictions = self.model([input])[0].pop("sem_seg")
            else:                                         # 后续版本:累加
                if any(isinstance(t, HFlipTransform) for t in tfm.transforms):
                    final_predictions += self.model([input])[0].pop("sem_seg").flip(dims=[2])
                else:
                    final_predictions += self.model([input])[0].pop("sem_seg")

    final_predictions = final_predictions / count_predictions   # 等权平均
    return {"sem_seg": final_predictions}
```

逐行要点:

- **L78 的 `orig_shape` 是死变量**,赋值后从未使用——输出尺寸的对齐完全依赖内层模型的后处理,而不是包装器自己。
- **`any(isinstance(t, HFlipTransform) ...)`**:每个版本的 `tfm` 是一个 `TransformList`,检查其中是否含水平翻转。缩放变换(ResizeTransform)不触发特殊处理。
- **`self.model([input])[0]`**:把该版本当作 batch=1 的普通输入调用内层模型,取第一张图的结果;`pop("sem_seg")` 取出并**移除**语义概率图键。
- **`.flip(dims=[2])`**:sem_seg 形状为 (K, H, W),`dims=[2]` 即宽维,把「翻转输入产生的预测」翻回原图方向,实现 2.2 节的几何对齐。
- **`+=` 与 `/count_predictions`**:等权求和再平均。首版本赋值、后续累加的写法等价于从零累加。
- **`return {"sem_seg": ...}`**:输出 dict **只含 sem_seg 一个键**——这就是 TTA 只服务语义分割的直接证据(适用边界详见 4.4)。

**配套的版本生成**——[mask2former/test_time_augmentation.py:L100-L103](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L100-L103):

```python
def _get_augmented_inputs(self, input):
    augmented_inputs = self.tta_mapper(input)                    # 生成 N 个版本
    tfms = [x.pop("transforms") for x in augmented_inputs]       # 弹出每版本的变换记录
    return augmented_inputs, tfms
```

`DatasetMapperTTA` 返回的每个版本 dict 里都带着一份 `transforms`(记录该版本用了哪些几何变换);包装器把它们 pop 出来,既避免多余字段流入内层模型,也拿到后面判断 HFlip 的依据。

**聚合可行性的源头(承接 u6-l1)**——[mask2former/maskformer_model.py:L235-L250](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L235-L250):

```python
height = input_per_image.get("height", image_size[0])   # 原图尺寸来自 dataset dict
width = input_per_image.get("width", image_size[1])
...
if self.semantic_on:
    r = retry_if_cuda_oom(self.semantic_inference)(mask_cls_result, mask_pred_result)
    if not self.sem_seg_postprocess_before_inference:
        r = retry_if_cuda_oom(sem_seg_postprocess)(r, image_size, height, width)
    processed_results[-1]["sem_seg"] = r
```

内层模型把语义概率图(`semantic_inference`,[L280-L284](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L280-L284),`einsum("qc,qhw->chw")`)插值回 `height×width`。TTA 的增强版本 dict 保留了原图 `height/width`,所以**无论输入被缩放到哪个分辨率,各版本的 sem_seg 形状恒为 (K, H_原图, W_原图)**,逐像素相加合法。

#### 4.2.4 代码实践

**实践目标**:用纯张量操作复现「翻转回翻 + 等权平均」的聚合逻辑,建立对几何对齐的手感。本实践只需 PyTorch,不需要 detectron2、模型权重或 GPU。

**操作步骤**:

1. 保存以下示例代码为独立脚本并运行:

   ```python
   # 示例代码:模拟 _inference_one_image 的翻转聚合(不依赖任何模型)
   import torch

   torch.manual_seed(0)
   K, H, W = 5, 8, 12
   p_orig = torch.softmax(torch.randn(K, H, W), dim=0)   # 模拟原图版本预测

   image = torch.randn(3, H, W)
   image_flip = image.flip(dims=[2])                     # 模拟 TTA 的水平翻转输入
   p_flip_back = p_orig.flip(dims=[2])                   # 模拟「翻转输入产生 p_orig,翻回原方向」

   # 两版本等权平均,对照源码的 += 再 / count_predictions
   final = (p_orig + p_flip_back) / 2

   print("翻转自反性:", torch.equal(image_flip.flip(dims=[2]), image))
   print("平均后 argmax 与原版一致:", torch.equal(final.argmax(0), p_orig.argmax(0)))

   # 构造「两版本判定相反」的像素,观察平均如何仲裁
   a = torch.tensor([0.6, 0.4]); b = torch.tensor([0.1, 0.9])   # 单像素两类的概率
   print("版本1判类", a.argmax().item(), "版本2判类", b.argmax().item(),
         "平均后判类", ((a + b) / 2).argmax().item())
   ```

2. 把 `(p_orig + p_flip_back) / 2` 与源码的「赋值 → `+=` → `/count`」三种语句改写成等价形式,互相验证。

**需要观察的现象**:`flip` 的自反性(翻两次还原)成立;两个完全「镜像一致」的版本平均后 argmax 不变;构造的反例中,版本 1 判类 0、版本 2 判类 1 的像素,平均后 (0.35, 0.65) 判类 1——**平均可以推翻单版本的判定**。

**预期结果**:以上断言均成立(翻转自反性是确定性数学事实);具体打印值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:为什么翻转版本的 sem_seg 要 `flip(dims=[2])`,而缩放版本不需要任何显式逆操作?

**答案**:`dims=[2]` 对应形状 (K, H, W) 的宽维 W。水平翻转改变了图像的左右方向,模型输出的预测图是「翻转坐标系」下的,必须翻回原方向才能与其他版本对齐。缩放则由内层模型的 `sem_seg_postprocess` 统一插值回原图 `height×width`,输出坐标系本来就一致,无需包装器处理。

**练习 2**:三个版本的某个像素概率分别为 (0.9, 0.1)、(0.6, 0.4)、(0.2, 0.8)。等权平均后该像素判哪类?这说明了 TTA 的什么机制?

**答案**:平均为 (0.567, 0.433),判类 0。三版本中有两版倾向类 0,平均后类 0 胜出——TTA 等权平均本质上是**软投票(soft voting)**:每个版本按置信度投票,多数且高置信的一方主导结果,单版本的极端噪声被稀释。

**练习 3**:如果内层模型配置为 `SEMANTIC_ON=False`,调用 TTA 包装器会发生什么?

**答案**:MaskFormer 的 eval 分支只在 `semantic_on` 为 True 时写入 `sem_seg` 键;此时 `self.model([input])[0].pop("sem_seg")` 会因 dict 中无该键抛出 `KeyError`。所以 TTA 隐含要求 `MODEL.MASK_FORMER.TEST.SEMANTIC_ON=True`。

### 4.3 `TEST.AUG` 配置项与 `DatasetMapperTTA` 契约

#### 4.3.1 概念说明

TTA 的「多尺度 + 翻转」具体生成多少个版本、每个版本长什么样,由 detectron2 的 `TEST.AUG` 配置组与 `DatasetMapperTTA` 决定。四个配置键(detectron2 官方项目默认值,非本仓库文件):

| 配置键 | 含义 | detectron2 默认值 | 本仓库覆盖情况 |
| --- | --- | --- | --- |
| `TEST.AUG.ENABLED` | TTA 总开关 | `False` | 所有 Base 均 `False`,需手动打开 |
| `TEST.AUG.MIN_SIZES` | 多尺度最短边列表(逐档缩放) | 9 档 `(400, 500, ..., 1200)` | ADE20K 语义 6 档、ADE20K 全景/实例与 Swin res640、Cityscapes 系各 6 档;**COCO/Mapillary 系未覆盖** |
| `TEST.AUG.MAX_SIZE` | 缩放时长边上限 | `4000` | ADE20K 语义 `3584` 等,随 Base 各异 |
| `TEST.AUG.FLIP` | 是否为每个尺度追加水平翻转版 | `True` | 均为 `True` |

**版本数公式**:\(N = |\text{MIN\_SIZES}| \times (2 \text{ if FLIP else } 1)\)。例如 ADE20K 语义配置 6 档 × 翻转 = **每张图推理 12 次**;COCO 系走 detectron2 默认则是 9 × 2 = 18 次。TTA 的指标提升就是用成倍的推理成本换来的。

`DatasetMapperTTA` 的行为契约(源码在 [detectron2/data/test_time_augmentation.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/data/test_time_augmentation.py),以本地安装为准):

- 输入:一个 dataset dict(能取到图像即可);
- 对 `MIN_SIZES` 中每个尺度 `s`:按 `ResizeShortestEdge(s, MAX_SIZE)` 缩放;`FLIP=True` 时每个尺度再产出一份水平翻转版;
- 输出:版本 dict 列表,每个含增强后的 CHW `image` tensor 与记录几何变换的 `transforms` 字段——正是 4.2 中 `pop("transforms")` 消费的东西。

注意:TTA mapper 的尺度来自 `TEST.AUG.MIN_SIZES`,与普通推理的 `INPUT.MIN_SIZE_TEST`(ADE20K 语义为 512,见 [Base-ADE20K-SemanticSegmentation.yaml:L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml#L39))是**两套独立的配置**——关闭 TTA 时后者决定输入尺度,开启后前者接管。

#### 4.3.2 核心流程

```text
DatasetMapperTTA(cfg) 初始化
  └─ 为 MIN_SIZES 每档预建一个 ResizeShortestEdge(s, MAX_SIZE)

__call__(dataset_dict)
  ├─ for 每个尺度 s:
  │     ├─ 缩放到最短边 = s
  │     └─ for (未翻转, [水平翻转] if FLIP):
  │           └─ 产出版本 dict {image: CHW tensor, transforms: [...] , ...}
  └─ 返回 N 个版本 → 交给 4.2 的逐版本推理循环
```

#### 4.3.3 源码精读

**本仓库侧的 TTA 配置覆盖**——[configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml:L51-L57](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml#L51-L57):

```yaml
TEST:
  EVAL_PERIOD: 5000
  AUG:
    ENABLED: False                        # 默认关闭
    MIN_SIZES: [256, 384, 512, 640, 768, 896]   # 6 档尺度
    MAX_SIZE: 3584
    FLIP: True                             # 每档追加翻转版 → 共 12 版本
```

这段就是论文 ADE20K 结果表里「多尺度 + 翻译翻转 TTA」的配置来源。同样的 `AUG` 块也出现在 ADE20K 全景/实例、Cityscapes 系与 Swin res640 的配置中(尺度列表各异,如 [configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml:L53-L57](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml#L53-L57) 用 `[320, 480, 640, 800, 960, 1120]`),但 `ENABLED` 一律为 `False`;COCO/Mapillary 系配置完全未写 `AUG` 块,将落到 detectron2 默认的 9 档。

**消费侧的入口**——[mask2former/test_time_augmentation.py:L12-L13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L12-L13):

```python
from detectron2.data.detection_utils import read_image
from detectron2.modeling import DatasetMapperTTA
```

`DatasetMapperTTA` 直接从 detectron2 导入(注意它在 `detectron2.modeling` 的命名空间下,文件却在 `detectron2/data` 包内),在 [L44-L46](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/test_time_augmentation.py#L44-L46) 被默认实例化。本仓库**没有复制或修改**它的任何实现——这是「detectron2 插件式项目,引擎能力尽量复用宿主框架」(u1-l3)的又一个体现。

#### 4.3.4 代码实践

**实践目标**:验证「改 `MIN_SIZES`/`FLIP` → 版本数变化」,并核对 TTA 尺度与普通测试尺度互不影响。(配置实验型实践,不需要权重。)

**操作步骤**:

1. 在 4.1.4 示例脚本的基础上追加:

   ```python
   # 示例代码:修改 TTA 配置并观察版本数
   cfg.merge_from_list(["TEST.AUG.ENABLED", "True"])
   print(cfg.TEST.AUG.ENABLED, cfg.TEST.AUG.MIN_SIZES, cfg.TEST.AUG.FLIP)

   # 只保留一档尺度、关掉翻转 → 退化为单版本
   cfg.TEST.AUG.MIN_SIZES = (512,)
   cfg.TEST.AUG.FLIP = False
   n = len(cfg.TEST.AUG.MIN_SIZES) * (2 if cfg.TEST.AUG.FLIP else 1)
   print(f"现在每张图推理 {n} 个版本;普通测试最短边仍为 {cfg.INPUT.MIN_SIZE_TEST}")
   ```

2. 通读本地 `DatasetMapperTTA.__call__`,确认「`FLIP=True` 时每个尺度产出未翻转 + 翻转两版」与版本数公式一致。

**需要观察的现象**:合并 ADE20K yaml 后 `MIN_SIZES` 为 6 档、`FLIP=True` → 12 版本;改成单档且 `FLIP=False` 后 → 1 版本;`INPUT.MIN_SIZE_TEST` 始终是 512,不受 `TEST.AUG` 影响。

**预期结果**:版本数按公式计算(12 → 1);`DatasetMapperTTA` 内部循环结构与公式一致。detectron2 侧细节以本地源码为准,具体打印**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:ADE20K 语义 Base 配置下,开启 TTA 后一张图要推理多少次?若推理一张图 0.5 秒,评估 2000 张验证集需要多久?

**答案**:`[256, 384, 512, 640, 768, 896]` 共 6 档,`FLIP=True` → 6 × 2 = 12 次/图。2000 张 × 12 × 0.5s = 12000s ≈ 3.3 小时(未计各版本缩放开销),约为普通评估的 12 倍。

**练习 2**:希望 TTA 只做「原图尺度 + 水平翻转」两个版本,该怎么配置?

**答案**:把 `MIN_SIZES` 设为与普通测试一致的单一尺度并保持 `FLIP=True`,例如脚本内 `cfg.TEST.AUG.MIN_SIZES = (512,); cfg.TEST.AUG.FLIP = True`(freeze 之前),或写入自定义 yaml 的 `TEST.AUG` 块。此时 N = 1 × 2 = 2。

**练习 3**:`MIN_SIZES` 只有一档且 `FLIP=False` 时,TTA 退化为什什么?

**答案**:单版本、无翻转——等价于以该尺度做一次普通推理再除以 1,聚合毫无作用。这常被用作 TTA 实现正确性的对照实验(综合实践正会用到)。

### 4.4 `test_with_TTA`:训练入口中的触发路径与适用边界

#### 4.4.1 概念说明

最后一个模块回答「TTA 什么时候才会真正运行」。三个关键结论:

1. **唯一触发点**:`--eval-only` 且 `TEST.AUG.ENABLED=True`。训练过程中的周期评估(detectron2 的 EvalHook,按 `TEST.EVAL_PERIOD` 触发)以及训练结束时的评估,都只调用普通 `Trainer.test`,**不会**跑 TTA。
2. **结果与产物隔离**:TTA 评估的落盘目录是 `OUTPUT_DIR/inference_TTA`(普通评估在 `inference`),返回的结果键统一追加 `_TTA` 后缀,与普通结果合并在同一个 dict 中交给 `verify_results` 校验。
3. **适用边界:事实上的语义分割专属**。包装器输出只有 `sem_seg` 键;若模型三开(SEMANTIC/INSTANCE/PANOPTIC 全开),`build_evaluator`(u7-l1)会构建需要 `instances`/`panoptic_seg` 键的评估器,TTA 结果缺键,评估将失败。因此能完整跑通 TTA 的是 `SEMANTIC_ON=True`、实例/全景评估不参与的纯语义配置(如 ADE20K 语义分割)。视频版入口则更直接——对 `TEST.AUG.ENABLED` 抛 `NotImplementedError`。

#### 4.4.2 核心流程

```text
python train_net.py --eval-only --config-file <yaml> \
    --opts MODEL.WEIGHTS <ckpt> TEST.AUG.ENABLED True

main(args)
  ├─ setup(args)                         # 五层配置合并(u2-l1)
  ├─ args.eval_only 分支:
  │    ├─ Trainer.build_model(cfg)       # 从 META_ARCHITECTURE_REGISTRY 构建模型
  │    ├─ DetectionCheckpointer.resume_or_load(MODEL.WEIGHTS)
  │    ├─ res = Trainer.test(cfg, model)            # ① 普通评估
  │    ├─ if cfg.TEST.AUG.ENABLED:
  │    │      res.update(Trainer.test_with_TTA(cfg, model))   # ② TTA 评估
  │    └─ verify_results(cfg, res)                  # 两套结果一起校验
  └─ (训练分支:Trainer(cfg).train(),全程无 TTA)
```

#### 4.4.3 源码精读

**`test_with_TTA` 类方法**——[train_net.py:L264-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L264-L278):

```python
@classmethod
def test_with_TTA(cls, cfg, model):
    logger = logging.getLogger("detectron2.trainer")
    # In the end of training, run an evaluation with TTA.
    logger.info("Running inference with test-time augmentation ...")
    model = SemanticSegmentorWithTTA(cfg, model)          # 关键一步:换成 TTA 包装器
    evaluators = [
        cls.build_evaluator(
            cfg, name, output_folder=os.path.join(cfg.OUTPUT_DIR, "inference_TTA")
        )
        for name in cfg.DATASETS.TEST
    ]
    res = cls.test(cfg, model, evaluators)                # 复用引擎的评估循环
    res = OrderedDict({k + "_TTA": v for k, v in res.items()})   # 键加后缀
    return res
```

三步走:用 `SemanticSegmentorWithTTA` **原地替换** model 变量(引擎的 `cls.test` 拿到的就是包装器,评估器无感知);为 `DATASETS.TEST` 的每个数据集构建评估器,输出目录换成 `inference_TTA` 与普通评估落盘隔离;结果键统一加 `_TTA` 后缀。注释「In the end of training」描述的是设计意图,但如上所述,本脚本中它**只在 eval-only 分支被调用**。

**触发点**——[train_net.py:L301-L311](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L301-L311):

```python
if args.eval_only:
    model = Trainer.build_model(cfg)
    DetectionCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
        cfg.MODEL.WEIGHTS, resume=args.resume
    )
    res = Trainer.test(cfg, model)                 # 先跑普通评估
    if cfg.TEST.AUG.ENABLED:
        res.update(Trainer.test_with_TTA(cfg, model))   # 再追加 TTA 结果
    if comm.is_main_process():
        verify_results(cfg, res)
    return res
```

普通评估与 TTA 评估**串行先后执行**,结果合并在一个 dict(`{"bbox": ..., "bbox_TTA": ...}` 风格),一次命令同时得到两组指标便于对比。训练分支([L313-L315](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L313-L315))直接 `trainer.train()`,没有任何 TTA 调用。

**纯语义配置才能配合 TTA**——[configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml:L39-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml#L39-L44):

```yaml
TEST:
  SEMANTIC_ON: True
  INSTANCE_ON: False
  PANOPTIC_ON: False
```

三个开关只开语义:模型输出仅含 `sem_seg` 键,`build_evaluator` 对 `ade20k_panoptic_seg` 类型数据集也只组装 `SemSegEvaluator`——与 TTA 包装器的输出契约恰好匹配。

**视频版反例**——[train_net_video.py:L263-L273](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L263-L273):

```python
if args.eval_only:
    ...
    res = Trainer.test(cfg, model)
    if cfg.TEST.AUG.ENABLED:
        raise NotImplementedError
```

视频版 eval-only 只跑普通评估,`TEST.AUG.ENABLED=True` 时直接抛 `NotImplementedError`——`VideoMaskFormer` 输出的是实例 tube 而非语义概率图(u9-l1),`SemanticSegmentorWithTTA` 的聚合方式无从套用,作者选择显式失败而非静默错跑。

#### 4.4.4 代码实践

**实践目标**:梳理「从命令行到 TTA 评估」的完整调用链,并确认周期评估不触发 TTA。(源码阅读型实践。)

**操作步骤**:

1. 在 [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) 中搜索 `test_with_TTA` 的全部出现(共两处:定义于 L265、调用于 L308),确认它只被 `main` 的 eval-only 分支调用。
2. 追溯周期评估路径:`Trainer` 继承 detectron2 的 `DefaultTrainer`,其 `__init__` 按 `cfg.TEST.EVAL_PERIOD` 挂载 EvalHook;阅读 detectron2 的 `DefaultTrainer.test`(本地包内),确认 EvalHook 调用的是这个普通 `test`,与 `test_with_TTA` 无关。
3. 写出对比表:普通评估 vs TTA 评估的触发条件、模型对象、输出目录、结果键名。

**需要观察的现象**:`test_with_TTA` 全仓库只有一个调用点(L308);EvalHook 链路中没有任何 TTA 参与。

**预期结果**:整理出类似下表的对照(参考答案):

| | 普通评估 | TTA 评估 |
| --- | --- | --- |
| 触发 | eval-only / 训练中周期 EvalHook | 仅 eval-only 且 `TEST.AUG.ENABLED=True` |
| 模型 | 原模型 | `SemanticSegmentorWithTTA(cfg, model)` 包装器 |
| 每图前向次数 | 1 | `len(MIN_SIZES) × (2 if FLIP else 1)`,ADE20K 语义为 12 |
| 输出目录 | `OUTPUT_DIR/inference` | `OUTPUT_DIR/inference_TTA` |
| 结果键 | 原名 | 追加 `_TTA` 后缀 |

EvalHook 与 `DefaultTrainer.test` 的行为以本地 detectron2 源码为准;**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:训练到一半的周期评估会跑 TTA 吗?为什么?

**答案**:不会。周期评估由 detectron2 的 EvalHook 触发,调用的是普通 `Trainer.test`;本仓库中 `test_with_TTA` 唯一的调用点是 `main` 的 eval-only 分支(L307-L308)。注释「In the end of training, run an evaluation with TTA」描述的是设计意图,实际接线只在 eval-only。

**练习 2**:TTA 评估的结果如何与普通评估的结果区分与合并?

**答案**:`test_with_TTA` 把 TTA 结果的每个键加 `_TTA` 后缀(L277),`main` 里 `res.update(...)` 把两套结果合进同一个 OrderedDict;`verify_results` 对合并后的 dict 整体校验。落盘文件也分别写在 `inference` 与 `inference_TTA` 目录下。

**练习 3**:若对 ADE20K 全景配置(三开)执行 `TEST.AUG.ENABLED=True` 的 eval-only,预计会发生什么?

**答案**:普通评估正常;TTA 阶段包装器能取到 `sem_seg`(三开时模型也输出语义键),但 `build_evaluator` 会因 INSTANCE_ON/PANOPTIC_ON 组装出需要 `instances`/`panoptic_seg` 键的评估器,而包装器返回的 dict 只有 `sem_seg`,评估器 `process` 取键时将抛 `KeyError` 而失败。这就是「TTA 事实上只服务纯语义配置」的机制性原因(**待本地验证**——可与综合实践结合实测)。

## 5. 综合实践

**任务**:对同一张图片,分别以「TTA 关」「TTA 开(12 版本)」「TTA 退化(单尺度无翻转)」三种方式推理,量化 sem_seg 预测的差异像素占比,并调整 `TEST.AUG` 观察变化。这是贯穿本讲三个核心(包装器、聚合、配置)的收官实验。

**准备工作**:

- 环境已按 u1-l2 搭好(CPU 推理可参考 u1-l4 的 `MODEL.DEVICE cpu` 路径);
- 从 MODEL_ZOO.md 定位 ADE20K 语义分割 R50 模型的权重(u1-l1 的方法),下载到本地。

**操作步骤**:

1. 将以下示例代码保存为 `tta_compare.py`(示例代码,放在项目根目录外或随意目录均可,**不要**写入仓库源码):

   ```python
   # 示例代码:TTA 开/关对比(需要权重,CPU 可跑)
   import cv2, torch
   from detectron2.config import get_cfg
   from detectron2.checkpoint import DetectionCheckpointer
   from detectron2.modeling import build_model
   from detectron2.data.detection_utils import read_image
   from detectron2.projects.deeplab import add_deeplab_config
   from mask2former import add_maskformer2_config, SemanticSegmentorWithTTA

   # 1) 组装配置(五层合并的复刻,u1-l5)
   cfg = get_cfg()
   add_deeplab_config(cfg)
   add_maskformer2_config(cfg)
   cfg.merge_from_file(
       "configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml"
   )
   cfg.MODEL.DEVICE = "cpu"
   cfg.freeze()

   # 2) 构建模型并加载权重
   model = build_model(cfg)
   DetectionCheckpointer(model).load("MODEL_ZOO下载的/model_final_R50_ade20k.pkl")  # 换成本地路径
   model.eval()

   # 3) 构造 dataset dict:image/height/width 必须齐全,
   #    规避 4.1.3 提到的 input_format 读盘坑
   img = read_image("你的图片.jpg", format=cfg.INPUT.FORMAT)   # 注意 ADE20K 配置为 RGB
   h, w = img.shape[:2]
   d = {
       "image": torch.from_numpy(img.transpose(2, 0, 1).copy()),  # CHW
       "height": h, "width": w,
   }

   with torch.no_grad():
       # 4) TTA 关:普通单尺度推理(INPUT.MIN_SIZE_TEST=512)
       base = model([d])[0]["sem_seg"].argmax(0)

       # 5) TTA 开:默认 6 档尺度 × 翻转 = 12 版本
       tta = SemanticSegmentorWithTTA(cfg, model)
       tta_pred = tta([d])[0]["sem_seg"].argmax(0)

       # 6) TTA 退化:单尺度(与 MIN_SIZE_TEST 同为 512)且关翻转 → 应与 4) 几乎一致
       cfg2 = cfg.clone(); cfg2.defrost()
       cfg2.TEST.AUG.MIN_SIZES = (512,); cfg2.TEST.AUG.FLIP = False
       tta1 = SemanticSegmentorWithTTA(cfg2, model)
       tta1_pred = tta1([d])[0]["sem_seg"].argmax(0)

   def diff(a, b):
       return (a != b).float().mean().item()

   print(f"TTA(12版本) vs 关闭:  差异像素占比 = {diff(tta_pred, base):.4%}")
   print(f"TTA(1版本)  vs 关闭:  差异像素占比 = {diff(tta1_pred, base):.4%}")
   ```

2. 运行脚本,记录两个差异占比。
3. 调整实验变量重跑:把第 5 步改为 `cfg2.TEST.AUG.MIN_SIZES = (896,)`(只保留最高档)、或 `FLIP` 单独关闭(尺度仍 6 档),观察差异占比如何变化。

**需要观察的现象与预期结果**:

- 「TTA(12 版本) vs 关闭」的差异占比应当是**一个小的非零值**(通常出现在物体边界与低置信区域)——多尺度与翻转改变了边界处的判定;
- 「TTA(1 版本) vs 关闭」理论上**趋近 0**:单尺度 512、无翻转时,TTA 路径与普通路径的输入几乎一致(若非严格为 0,思考一下两条路径的缩放实现是否存在细微差别,例如 `MAX_SIZE` 上限不同导致的非等比缩放);
- 只留最高档 896 时差异占比通常变大——高分辨率输入改变小物体与边界的预测。
- 以上现象与具体数值**待本地验证**(本讲写作环境无权重与运行环境,未实际执行)。

**思考题**(选做):把 12 版本的聚合从等权平均改成「只平均 argmax 前 2 名之外的版本」会怎样?动手修改 `_inference_one_image` 的副本验证你的想法。

## 6. 本讲小结

- **TTA = 多版本推理 + 几何对齐 + 等权平均**:`SemanticSegmentorWithTTA` 包装已构建的模型,`DatasetMapperTTA` 按 `MIN_SIZES × FLIP` 生成增强版本,逐版本推理后把预测累加除以版本数——聚合是**等权平均**,而非按分辨率加权。
- **聚合可行的两个前提**:各版本 sem_seg 已被内层模型后处理回**原图尺寸**(坐标系统一);翻转版本的预测被 `flip(dims=[2])` **翻回原方向**(几何对齐)。
- **配置四键**:`TEST.AUG.ENABLED`(默认 False)、`MIN_SIZES`(多尺度列表,detectron2 默认 9 档;ADE20K 语义 6 档)、`MAX_SIZE`、`FLIP`;版本数 = 档数 × (2 if FLIP),推理成本同比膨胀。
- **触发差异**:TTA 只在 `--eval-only` 且 `TEST.AUG.ENABLED=True` 时经 `test_with_TTA` 运行,输出目录 `inference_TTA`、结果键加 `_TTA` 后缀;训练中的周期评估永远走普通 `Trainer.test`。
- **适用边界**:包装器只输出 `sem_seg` 键,事实上只服务纯语义分割配置(如 ADE20K/Cityscapes 语义);三开的全景配置会因评估器缺键而失败,视频版入口直接 `NotImplementedError`。
- **阅读源码的诚实收获**:本文件里有一个死变量(`orig_shape`)、一个死参数(`batch_size`)、一个对 MaskFormer 会踩坑的读盘分支(`input_format`)——教科书式的「实现与文档/直觉有出入,以源码为准」。

## 7. 下一步学习建议

- **衔接 u10-l2(显存优化与 OOM 重试)**:TTA 的多尺度版本里有远高于普通测试的分辨率档(如 ADE20K 语义的 896),正是高分辨率推理显存压力的典型场景;回头体会 `sem_seg_postprocess_before_inference` 与 `retry_if_cuda_oom` 在这条路径上的位置。
- **对照阅读 detectron2 的 TTA 基类**:[detectron2/modeling/test_time_augmentation.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/modeling/test_time_augmentation.py) 提供了 `SemanticSegmentorWithTTA` 基类与目标检测版 `GeneralizedRCNNWithTTA`(含未匹配预测的 NMS 合并),对比本仓库 104 行简化版,体会「够用就好」的裁剪。
- **动手扩展(选做)**:仿照本讲实现,给 `instance_inference` 的结果写一个「翻转回翻 + topk 合并」的实例分割 TTA 原型——你会很快发现实例级聚合比逐像素平均难得多(集合的无序性),这正好引出集合预测范式的深水区。
- 下一讲 u10-l4 将讲解权重转换与 checkpoint 版本兼容,完成单元十的收官。
