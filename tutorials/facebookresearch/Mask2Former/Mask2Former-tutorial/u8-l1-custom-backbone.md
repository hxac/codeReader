# 扩展实践一：替换 Backbone

## 1. 本讲目标

前三单元我们一直在「读」Mask2Former：backbone、pixel decoder、Transformer decoder 三段式如何协作。从本讲开始转向「写」：**动手实现一个自己的组件并接入框架**。

学完本讲，你应该能够：

1. 说清 detectron2 的 `BACKBONE_REGISTRY` 注册机制，以及「import 即注册」在新 backbone 场景下的一个关键坑（`backbone/__init__.py` 是空文件）。
2. 掌握 Backbone 接口契约的三件事：`forward(x)` 返回特征字典、`output_shape()` 上报通道数与 stride、`size_divisibility` 决定输入 padding。
3. 以 `D2SwinTransformer` 为模板，理解「纯 PyTorch 网络 + detectron2 适配器」的双类继承写法。
4. 理解 backbone 的 `OUT_FEATURES` 命名必须与 `SEM_SEG_HEAD.IN_FEATURES` 等消费方对齐，错一个字符就会在构建期报 KeyError。
5. 独立实现一个 4-stage CNN backbone，注册后仅改一行配置 `MODEL.BACKBONE.NAME`，让整个 Mask2Former 在你的 backbone 上跑通前向。

## 2. 前置知识

本讲是 advanced 层的第一次动手扩展，建立在两讲之上，不重复其内容：

- **u3-l3（Swin Backbone）**：已经精读过 Swin 内部——PatchEmbed、window attention、PatchMerging、res2–res5 命名约定。本讲不再讲 Swin 的注意力细节，只把它当作「注册写法的范本」。
- **u1-l5（配置系统）**：五层配置叠加、`_BASE_` 继承、`merge_from_list`。本讲切换 backbone 时会直接用到这些机制。

在此之上，补充三个本讲反复使用的概念：

1. **注册表（Registry）**：detectron2 维护的一张「字符串名字 → 类或工厂函数」的全局表。`build_backbone(cfg)` 拿 `cfg.MODEL.BACKBONE.NAME` 这个字符串去表里查，查到后用 `(cfg, input_shape)` 两个参数调用它。名字查不到就抛 `KeyError`（detectron2 宿主框架行为，报错形如 `Cannot find object 'XXX' in 'BackboneRegistry'!`）。这张表本身不在本仓库，但读写两端都在。
2. **接口契约（contract）**：backbone 是框架与你的代码之间的「插座」。插座规格由 detectron2 的 `Backbone` 抽象类规定（宿主框架代码，不在本仓库）：它本身继承 `nn.Module`，约定了 `output_shape()`、`size_divisibility` 和「forward 返回 `dict[str, Tensor]`」。你的网络插头形状不对，框架立刻断电报错。
3. **适配器模式（adapter pattern）**：`D2SwinTransformer` 不是从零写的网络——它同时继承「纯 PyTorch 的 `SwinTransformer`」和「detectron2 的 `Backbone` 接口」，中间做一层薄薄的翻译。这样上游 Swin 官方代码可以原样跟随升级，detectron2 适配层不受影响。我们写自己的 CNN backbone 时可以更简单：直接继承 `Backbone` 一个类。

另外回顾一个数据流事实（u3-l1 已讲）：`MaskFormer.forward` 中 backbone 的输出特征字典会被 `ImageList.from_tensors` 按 `size_divisibility` padding 后的图像消费，特征名字（键）一路传给 `MaskFormerHead` 与 pixel decoder。本讲的「名称对齐」正是围绕这条数据流。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ADVANCED_USAGE.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md) | 官方给出的扩展指南：替换三组件各两步 |
| [mask2former/modeling/backbone/swin.py:L686-L770](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L686-L770) | `D2SwinTransformer` 适配器：注册、cfg 读参、接口三件套（本讲主精读对象） |
| [mask2former/modeling/backbone/__init__.py:L1](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/__init__.py#L1) | 只有一行版权注释——空文件，正是新 backbone 注册的坑所在 |
| [mask2former/modeling/__init__.py:L2-L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L2-L6) | 显式 import 各组件模块，触发注册；新文件不会自动进入这条链 |
| [mask2former/config.py:L73-L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L73-L90) | `cfg.MODEL.SWIN.*` 全部配置键的定义处（含 `OUT_FEATURES`） |
| [configs/coco/panoptic-segmentation/maskformer2_swin_tiny_bs16_50ep.yaml:L1-L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L1-L15) | 官方切换 backbone 的范例配置：只改 `BACKBONE.NAME` + `SWIN` 段 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L14-L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L14-L17) | 消费侧：`SEM_SEG_HEAD.IN_FEATURES` 等三个特征名单键 |
| [mask2former/maskformer_model.py:L97-L98](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L97-L98) | `from_config` 中 `build_backbone(cfg)` 的调用点：注册表查表入口 |
| [mask2former/modeling/meta_arch/mask_former_head.py:L72-L75](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L72-L75) | `output_shape()` 的消费端：按 stride 排序、取通道数 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**4.1 注册机制**（名字如何进表）、**4.2 接口契约**（`output_shape` 等三件事）、**4.3 Swin 注册示例**（官方范本逐段精读）。

### 4.1 注册机制：BACKBONE_REGISTRY.register 与 import 链

#### 4.1.1 概念说明

detectron2 用「注册表 + 字符串引用」解耦配置文件与 Python 代码：yaml 里写 `NAME: "D2SwinTransformer"`，运行时才由 `BACKBONE_REGISTRY.get("D2SwinTransformer")` 查表得到类。你的新 backbone 要能被配置引用，必须完成两件事：

1. **注册**：类（或工厂函数）头顶 `@BACKBONE_REGISTRY.register()` 装饰器，名字默认取类名/函数名的字符串。
2. **触发 import**：装饰器只有在模块被 import 时才执行。这一步是新手最容易栽的坑，见下文。

ADVANCED_USAGE.md 对替换 backbone 的官方指引只有两句话——「在 `mask2former/modeling/backbone` 下定义并注册（参照 Swin）」+「改配置」，本讲就是把这两句话展开成可执行的细节：

```markdown
[ADVANCED_USAGE.md:L8-L11](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md#L8-L11)
→ 官方替换指引：1) 在 mask2former/modeling/backbone 下定义并注册 backbone，可参照 Swin；2) 相应地改配置文件。
```

#### 4.1.2 核心流程

```text
import mask2former                        # 入口脚本 train_net.py / demo.py 的第一行
  └─ mask2former/modeling/__init__.py 执行
       └─ from .backbone.swin import D2SwinTransformer
            └─ swin.py 模块顶层执行
                 └─ @BACKBONE_REGISTRY.register() 生效，"D2SwinTransformer" 进表
...
build_model(cfg) → MaskFormer.from_config → build_backbone(cfg)
  └─ BACKBONE_REGISTRY.get("你配置里的 NAME")(cfg, input_shape)
```

关键结论：**注册的触发靠 modeling/__init__.py 的显式 import，而不是 backbone/ 目录自动扫描。**

#### 4.1.3 源码精读

注册的源头一行 import，来自 detectron2（宿主框架，不在本仓库）：

```python
[swin.py:L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L18)
→ 从 detectron2.modeling 导入 BACKBONE_REGISTRY 注册表、Backbone 抽象基类和 ShapeSpec（channels+stride 的轻量描述结构）。
```

触发注册的 import 链在 modeling 包的 `__init__.py`：

```python
[mask2former/modeling/__init__.py:L2-L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L2-L6)
→ 显式 import 五个组件：D2SwinTransformer、BasePixelDecoder、MSDeformAttnPixelDecoder、MaskFormerHead、PerPixelBaselineHead。每个 import 都触发对应模块顶层的注册装饰器。
```

而 backbone 子包自己的 `__init__.py`：

```python
[mask2former/modeling/backbone/__init__.py:L1](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/__init__.py#L1)
→ 整个文件只有一行版权注释。它不会 import 子目录下的任何模块。
```

**这就是坑**：你把 `mini_cnn.py` 放进 `mask2former/modeling/backbone/`，如果只是 `import mask2former`，`mini_cnn.py` 根本不会被执行——`modeling/__init__.py` 只 import 了 `swin` 这一个子模块。于是 `cfg.MODEL.BACKBONE.NAME = "MiniCNNBackbone"` 在 `build_backbone` 查表时抛 KeyError。两种解法：

- **路径 A（不改源码，推荐）**：在你的验证/训练脚本里 `import mask2former` 之后，再手动 `from mask2former.modeling.backbone.mini_cnn import MiniCNNBackbone`。registry 是进程级全局对象，谁 import 都一样。
- **路径 B（一劳永逸，改仓库一行）**：在 `modeling/__init__.py` 里仿照 L2 加一行 import。这属于对本仓库源码的修改，建议只在自己的实验分支做。

再看消费端——查表发生在哪：

```python
[maskformer_model.py:L97-L98](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L97-L98)
→ MaskFormer.from_config 第一行就是 backbone = build_backbone(cfg)：detectron2 用 cfg.MODEL.BACKBONE.NAME 查 BACKBONE_REGISTRY，再以 (cfg, input_shape) 构造。meta arch 自己完全不关心 backbone 是什么网络。
```

顺带一个有价值的观察：R50 配置里 `BACKBONE.NAME` 的值是 `"build_resnet_backbone"`（见 [Base-COCO-PanopticSegmentation.yaml:L2-L4](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L2-L4)，该函数由 detectron2 的 resnet.py 注册），而 Swin 配置里是类名 `"D2SwinTransformer"`。**注册表不区分类和工厂函数**——只要能以 `(cfg, input_shape)` 调用并返回满足 Backbone 接口的对象即可。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「注册表内容 = import 链执行结果」，并验证路径 A 的手动注册可行。
2. **操作步骤**：在仓库根目录运行下面的最小脚本（示例代码）：

   ```python
   # peek_registry.py（示例代码）
   from detectron2.modeling import BACKBONE_REGISTRY

   names_before = set(BACKBONE_REGISTRY._obj_map.keys())
   import mask2former  # 触发注册链
   names_after = set(BACKBONE_REGISTRY._obj_map.keys())
   print("import mask2former 新增的 backbone 注册名:",
         sorted(names_after - names_before))

   # 路径 A 演示：手动 import 一个尚不存在的模块会 ImportError；
   # 反过来，任何被 import 的模块都会在此处留下名字。
   print("表中与 swin/resnet 相关的名字:",
         [n for n in names_after if "Swin" in n or "resnet" in n])
   ```

3. **需要观察的现象**：第一段输出应包含 `D2SwinTransformer`（外加 detectron2 自带的 `build_resnet_backbone` 等在 import 前就已在表中的名字）；第二段应能列出 resnet 相关名字。
4. **预期结果**：证明「import mask2former → 表中多出 D2SwinTransformer」这条链路真实存在；`_obj_map` 是 detectron2 `Registry` 的内部字典（私有属性，仅用于观察，不要在正式代码里依赖它）。
5. 本实践不依赖 GPU 与数据集，纯 CPU 即可；具体输出以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么把新 backbone 文件放进 `mask2former/modeling/backbone/` 后，`import mask2former` 并不会注册它？
**答案**：注册靠装饰器执行，而装饰器在模块被 import 时才执行。`mask2former/modeling/__init__.py` 只显式 import 了 `backbone.swin`，backbone 子包的 `__init__.py` 是空文件，不做目录扫描。文件躺在目录里不等于被 import。

**练习 2**：注册表里放类和放工厂函数（如 `build_resnet_backbone`）有什么共同要求？
**答案**：detectron2 的 `build_backbone` 会用统一的 `(cfg, input_shape)` 签名去调用注册对象，因此无论类还是函数都必须接受这两个位置参数；返回值还需通过 `isinstance(backbone, Backbone)` 断言。

### 4.2 接口契约：output_shape、size_divisibility 与 forward 字典

#### 4.2.1 概念说明

「替换 backbone」之所以只要改一行配置，是因为框架对 backbone 的全部期待被压缩成三条契约。你的实现少一条，链条就断：

1. **`forward(x) -> dict[str, Tensor]`**：输入 (N, 3, H, W) 图像张量，输出「特征名 → 特征图」字典。特征名是 backbone 与下游沟通的「暗号」。
2. **`output_shape() -> dict[str, ShapeSpec]`**：上报每个输出特征的通道数与 stride（分辨率缩放倍数）。注意这是**元信息查询**，不跑前向——框架构建 pixel decoder 时就要用它定卷积通道数，不可能先喂一张图试试。
3. **`size_divisibility`（property，int）**：要求输入 H、W 能被该数整除。框架用它 padding 批内图像。

#### 4.2.2 核心流程

三条契约在框架里的消费点：

```text
构建期（不跑前向）:
  build_backbone(cfg) ──> 你的 Backbone(cfg, input_shape)
  MaskFormerHead.from_config(cfg, input_shape)      # input_shape 来自 backbone.output_shape()
      └─ input_shape 按 stride 排序 ──> 取出 in_features / feature_strides / feature_channels
      └─ build_pixel_decoder(cfg, input_shape)      # pixel decoder 各层通道数由 feature_channels 决定
运行期:
  MaskFormer.forward
      └─ ImageList.from_tensors(images, size_divisibility)   # 契约 3：批内 padding 到整除
      └─ features = self.backbone(images.tensor)             # 契约 1：拿到特征字典
      └─ self.sem_seg_head(features)                         # 按契约 2 上报过的名字取特征
```

**名称对齐规则**：`backbone.forward` 返回字典的键 ⊇ `cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES`，且每个键在 `output_shape()` 里有相同名字的条目。三个消费方都在配置里点名要特征：

```yaml
[maskformer2_R50_bs16_50ep.yaml:L14-L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L14-L17)
→ SEM_SEG_HEAD 段：IN_FEATURES 要求 backbone 给出 res2..res5 四个特征；MSDeformAttn 像素解码器另用 DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES 点名 res3/res4/res5（stride 8/16/32）。
```

#### 4.2.3 源码精读

先看 `output_shape()` 的消费端——`MaskFormerHead.__init__` 开头三行就把元信息拆开：

```python
[mask_former_head.py:L72-L75](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L72-L75)
→ 把传入的 input_shape（即 backbone.output_shape() 的结果，经 from_config 过滤）按 stride 从小到大排序，拆出特征名列表、stride 列表和通道列表。channel 数直接决定 pixel decoder 里各卷积的输入通道。
```

再看 from_config 如何按配置过滤特征：

```python
[mask_former_head.py:L100-L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L100-L105)
→ input_shape 只保留出现在 cfg.MODEL.SEM_SEG_HEAD.IN_FEATURES 里的键，随后原样传给 build_pixel_decoder。backbone 多给的特征会被丢弃，少给则 KeyError。
```

然后是 `size_divisibility` 的两条路径——backbone 声明值与 meta arch 覆盖值的优先级：

```python
[maskformer_model.py:L79-L82](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L79-L82)
→ 仅当配置给的 size_divisibility 为负（未指定）时才回退用 backbone 自己的 size_divisibility；否则配置值优先。
```

```python
[config.py:L62-L64](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L62-L64)
→ 注释写明动机：ResNet 的 size_divisibility 为 0（不限制），所以默认显式给 32 兜底。COCO 配置里 MASK_FORMER.SIZE_DIVISIBILITY: 32 也是显式设 32（见同文件 yaml L35）。
```

```python
[maskformer_model.py:L195](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L195)
→ 运行期消费点：ImageList.from_tensors 用 size_divisibility 把批内图像 pad 到公共整除尺寸。若 backbone 实际需要 32 整除而这里给了 16，Swin 这类网络会在内部形状运算处崩掉。
```

#### 4.2.4 代码实践

1. **实践目标**：体会「名称对齐」是字符串级硬约束，掌握改名的完整影响面。
2. **操作步骤**：假设你的 backbone 把输出键从 `res2..res5` 改成 `f2..f5`（只做思想实验+查配置，不真改代码）。用编辑器在 `configs/coco/panoptic-segmentation/` 下搜索 `res2`，列出所有需要同步修改的配置键。
3. **需要观察的现象**：至少命中三处——`SEM_SEG_HEAD.IN_FEATURES`（[maskformer2_R50_bs16_50ep.yaml:L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L15)）、`DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES`（L16）、以及若 `TRANSFORMER_IN_FEATURE` 直接写特征名时的那个键（本配置用的是 `"multi_scale_pixel_decoder"`，不点名特征，可不动；v1 配置才会写 `"res5"` 之类）。
4. **预期结果**：漏改任何一处，构建期在 [mask_former_head.py:L101](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L101) 的字典推导处抛 `KeyError: 'f3'`（从 input_shape 里取不存在的键）。这就是「OUT_FEATURES/forward 键/IN_FEATURES 三处字符串必须一字不差」的机制根源。
5. 结论可纯靠源码推导，无需运行（本条为源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：`output_shape()` 为什么必须与 `forward` 的实际输出严格一致？如果不一致（比如上报 256 通道实际 128）会发生什么？
**答案**：`output_shape()` 是构建期元信息，pixel decoder 的 1×1 lateral 卷积按它声明通道数建权重；若实际输出通道不符，第一次前向卷积时形状不匹配直接报运行时错。上报错误不会在构建期被发现，只会推迟到 forward，排查更难——所以契约两端口径必须同源（最好像 Swin 那样共用同一份成员字典，见 4.3.3）。

**练习 2**：一个普通 CNN（所有下采样都是 stride 2 卷积，stem 后到 1/4）的 `size_divisibility` 最小可以设多少？设成 1 行不行？
**答案**：理论最小值是最大 stride 32（保证 1/32 特征图每个像素都有完整感受野支撑），工程上取 32 是保守且通用的值。设成 1「行不行」取决于网络：对本讲的 MiniCNN，任何偶数尺寸输入在数学上都能前向（卷积对任意尺寸有效），padding 32 只是为了批内对齐与多尺度网格整齐；但对 Swin 这种含窗口划分与 PatchMerging 的网络，非整除输入会在 reshape 处崩溃，必须如实声明。

### 4.3 SwinTransformer 注册示例：D2SwinTransformer 适配器

#### 4.3.1 概念说明

`D2SwinTransformer` 是官方给出的「如何接入自定义 backbone」的参考实现，约 85 行，结构是标准的适配器：

- **不重写网络**：纯 PyTorch 的 `SwinTransformer` 类（上游 Swin 仓库移植）原样保留，u3-l3 已精读。
- **双继承做翻译**：`class D2SwinTransformer(SwinTransformer, Backbone)`——同时是「Swin 网络」和「detectron2 Backbone」。
- **职责只有三件**：从 `cfg.MODEL.SWIN` 读参数并转发给父类构造器；维护 stride/通道两张元信息表；实现契约三件套。

我们后面写 CNN backbone 时不需要双继承（网络本来就在同一个类里写），但「cfg 读参 → 父类构造 → 元信息表 → 契约实现」这个骨架完全照抄即可。

#### 4.3.2 核心流程

```text
cfg.MODEL.BACKBONE.NAME = "D2SwinTransformer"
        │ build_backbone 查表
        ▼
D2SwinTransformer(cfg, input_shape)
        │ ① 逐键读取 cfg.MODEL.SWIN.*（约 15 个超参）
        │ ② 转发给 SwinTransformer.__init__ 构建真正的网络
        │ ③ 补齐 _out_features / _out_feature_strides / _out_feature_channels
        ▼
forward(x)          → 复用 SwinTransformer.forward，再按 _out_features 过滤
output_shape()      → 把两张表 zip 成 ShapeSpec 字典
size_divisibility   → 返回 32
```

#### 4.3.3 源码精读

注册与双继承声明：

```python
[swin.py:L686-L687](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L686-L687)
→ 装饰器把类名 "D2SwinTransformer" 写入注册表；类同时继承 SwinTransformer（网络实现）与 Backbone（detectron2 接口）。
```

构造器第一段——cfg 到构造参数的翻译：

```python
[swin.py:L688-L726](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L688-L726)
→ 从 cfg.MODEL.SWIN 逐键读出 PRETRAIN_IMG_SIZE、PATCH_SIZE、EMBED_DIM、DEPTHS、WINDOW_SIZE 等约 15 个超参，再原样转发给父类 SwinTransformer 的构造器。适配器本身不产生任何网络结构。
```

这些键的定义处（也是你新增 `cfg.MODEL.MINI_CNN` 之类自定义段时要仿照的地方）：

```python
[config.py:L73-L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L73-L90)
→ add_maskformer2_config 注册 cfg.MODEL.SWIN 全部键：结构超参（EMBED_DIM/DEPTHS/NUM_HEADS/WINDOW_SIZE）、训练超参（DROP_PATH_RATE 等）与 OUT_FEATURES（默认 res2..res5）。
```

构造器第二段——元信息三件套，这是适配器的核心资产：

```python
[swin.py:L728-L741](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L728-L741)
→ _out_features 从配置读；_out_feature_strides 硬编码 {res2:4, res3:8, res4:16, res5:32}——Swin 的 patch 4 倍下采样 + 三次 PatchMerging 决定了这组 stride 是结构常量；_out_feature_channels 取自父类的 num_features（各 stage 通道 = EMBED_DIM×2^stage），通道随配置变、stride 不随配置变，所以一硬一软。
```

契约一：forward 过滤输出：

```python
[swin.py:L743-L758](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L743-L758)
→ 断言输入是 (N,C,H,W) 四维；调用父类 forward 得到全部特征字典 y，再只保留 _out_features 里点名的键。注意 res 命名的产生在父类：outs["res{}".format(i + 2)]（见 swin.py:L666-L676，第 i 个 stage 输出叫 res(i+2)，把 Transformer stage 编号对齐到 ResNet 习惯命名）。
```

契约二、三——output_shape 与 size_divisibility：

```python
[swin.py:L760-L770](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L760-L770)
→ output_shape() 用同一份成员字典把每个输出特征包装成 ShapeSpec(channels, stride)；size_divisibility 是 property，返回硬编码 32。两张表在构造器与 output_shape 之间共用，天然保证「上报=实际」。
```

最后看官方怎么在配置里切换 backbone——整个 Swin tiny 配置就是对 R50 基线的差分：

```yaml
[maskformer2_swin_tiny_bs16_50ep.yaml:L1-L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L1-L15)
→ _BASE_ 继承 R50 全部配置后，只覆写三块：BACKBONE.NAME 换成 "D2SwinTransformer"（唯一决定性的一行）、SWIN 段给结构超参、WEIGHTS 换 Swin 预训练权重。SEM_SEG_HEAD.IN_FEATURES 无需改动，因为 Swin 适配器沿用了 res2..res5 命名。
```

这就是「换 backbone = 改一行 NAME（+ 可选超参段）」的完整证据链：**命名沿用比网络本身更重要**——Swin 明明不是 ResNet，却把输出叫 res2..res5，正是为了让下游配置零改动。

#### 4.3.4 代码实践

1. **实践目标**：建立「cfg 键 ↔ 构造参数 ↔ 元信息表」三者的映射直觉。
2. **操作步骤**：对照 [config.py:L74-L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L74-L90) 与 [swin.py:L690-L706](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L690-L706)，手绘一张三列映射表：列 1 是 cfg 键名，列 2 是构造参数名，列 3 是「结构超参 / 训练超参 / 输出控制」分类。
3. **需要观察的现象**：`OUT_FEATURES` 是唯一不进父类构造器、只被适配器自己消费的键（L728）；`PRETRAIN_IMG_SIZE`、`APE` 等结构键与 `DROP_PATH_RATE` 等训练键都直通父类。
4. **预期结果**：得到约 15 行的映射表，其中「适配器自留键」一栏只有 `OUT_FEATURES` 一项——这说明适配器对网络本身几乎零干预，职责边界非常干净。
5. 纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_out_feature_strides` 硬编码而 `_out_feature_channels` 要从 `self.num_features` 取？
**答案**：stride 由网络下采样拓扑决定——Swin 的 patch embedding（×4）加三次 PatchMerging（×2×2×2）在任何 EMBED_DIM/DEPTHS 配置下都固定产出 1/4、1/8、1/16、1/32，是结构常量，写死即可；而通道数 = EMBED_DIM × 2^stage，随 cfg.MODEL.SWIN.EMBED_DIM 变化，必须从父类构建结果里读，才不会出现「上报与实际不符」。

**练习 2**：如果想让 Swin 只输出 res3/res4/res5（省掉 res2），配置和模型各要动哪里？会有什么副作用？
**答案**：配置上把 `MODEL.SWIN.OUT_FEATURES` 和 `MODEL.SEM_SEG_HEAD.IN_FEATURES` 都改成三键。副作用：pixel decoder 拿不到 1/4 特征，无法产出 1/4 分辨率的 mask_features（u3-l4 讲过 res2 只经 mask_features 流出），mask 质量会下降；这正说明 res2 不是可有可无的冗余输出。

**练习 3**：`D2SwinTransformer.forward` 里那行 `assert x.dim() == 4` 防的是什么错？
**答案**：防止调用方传入 (C,H,W) 三维单图或 (B,T,C,H,W) 视频帧等错误形状，把「形状错误」提前到 backbone 入口、给出明确报错，而不是让它在网络深处的 reshape/window 划分处以难懂的索引错误爆发。

## 5. 综合实践

现在把三个模块串成一个完整任务：**实现 MiniCNNBackbone 并让整个 Mask2Former 在它上面跑通 CPU 前向**。这也是 ADVANCED_USAGE 两步指引的完整落地。

### 5.1 实践目标

写一个 4-stage CNN backbone（stem 降到 1/4，res2..res5 每 stage 两层 3×3 卷积、stage 首层 stride 2 下采样，通道 64→128→256→512），注册后仅通过 `MODEL.BACKBONE.NAME` 切换，先单独验证 backbone，再接入完整模型对一张随机图前向。

### 5.2 步骤一：编写 backbone 文件

在 `mask2former/modeling/backbone/` 下新建 `mini_cnn.py`（示例代码——这是你要写的实验文件，不属于原仓库）：

```python
# mask2former/modeling/backbone/mini_cnn.py（示例代码）
import torch.nn as nn
from detectron2.modeling import BACKBONE_REGISTRY, Backbone, ShapeSpec


@BACKBONE_REGISTRY.register()          # ← 4.1：注册，类名即注册名
class MiniCNNBackbone(Backbone):       # ← 4.2：直接继承接口基类，无需双继承
    def __init__(self, cfg, input_shape):   # ← 签名约定：build_backbone 以 (cfg, input_shape) 调用
        super().__init__()
        base = 64
        # 4.3 元信息三件套：与 Swin 相同的 res 命名 + 相同的 stride 表
        self._out_features = ["res2", "res3", "res4", "res5"]
        self._out_feature_strides = {"res2": 4, "res3": 8, "res4": 16, "res5": 32}
        self._out_feature_channels = {
            "res2": base, "res3": 2 * base, "res4": 4 * base, "res5": 8 * base,
        }

        def stage(in_c, out_c, first_stride):
            # 每 stage 两层 3x3 conv；下采样由首层 stride 承担
            return nn.Sequential(
                nn.Conv2d(in_c, out_c, 3, stride=first_stride, padding=1, bias=False),
                nn.BatchNorm2d(out_c), nn.ReLU(inplace=True),
                nn.Conv2d(out_c, out_c, 3, stride=1, padding=1, bias=False),
                nn.BatchNorm2d(out_c), nn.ReLU(inplace=True),
            )

        # stem：两次 stride-2 卷积，3 → 1/4 分辨率
        self.stem = nn.Sequential(
            nn.Conv2d(3, base // 2, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(base // 2), nn.ReLU(inplace=True),
            nn.Conv2d(base // 2, base, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(base), nn.ReLU(inplace=True),
        )
        self.res2 = stage(base, base, 1)          # 1/4,  64
        self.res3 = stage(base, 2 * base, 2)      # 1/8,  128
        self.res4 = stage(2 * base, 4 * base, 2)  # 1/16, 256
        self.res5 = stage(4 * base, 8 * base, 2)  # 1/32, 512

    def forward(self, x):
        # 契约一：返回 特征名 → 特征图 字典
        outputs = {}
        x = self.stem(x)
        for name in self._out_features:
            x = getattr(self, name)(x)
            outputs[name] = x
        return outputs

    def output_shape(self):
        # 契约二：上报通道与 stride，与 forward 输出共用同一份表
        return {
            name: ShapeSpec(
                channels=self._out_feature_channels[name],
                stride=self._out_feature_strides[name],
            )
            for name in self._out_features
        }

    @property
    def size_divisibility(self):   # 契约三：最大 stride 是 32
        return 32
```

注意三个刻意的设计决定（都来自 4.3 的观察）：沿用 `res2..res5` 命名让下游配置零改动；`output_shape` 与 `forward` 共用成员字典保证口径一致；构造签名保持 `(cfg, input_shape)`（即使本实现不读 cfg，也不能改签名，否则注册表调用失败）。

### 5.3 步骤二：backbone 单元验证（不改任何源码）

仓库根目录新建 `run_mini_cnn.py`（示例代码）：

```python
# run_mini_cnn.py（示例代码）
import torch
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_backbone
from detectron2.modeling import ShapeSpec

from mask2former import add_maskformer2_config
from mask2former.modeling.backbone.mini_cnn import MiniCNNBackbone  # ← 4.1 路径 A：import 即注册

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.BACKBONE.NAME = "MiniCNNBackbone"   # ← 唯一决定性的切换

backbone = build_backbone(cfg, ShapeSpec(channels=3)).eval()
print("output_shape:", backbone.output_shape())
# 预期: {'res2': Strided(channels=64, stride=4), 'res3': ..., 'res4': ..., 'res5': Strided(channels=512, stride=32)}

feats = backbone(torch.randn(1, 3, 384, 384))  # 384 可被 32 整除
for k in ["res2", "res3", "res4", "res5"]:
    print(k, tuple(feats[k].shape))
# 预期: res2 (1,64,96,96) / res3 (1,128,48,48) / res4 (1,256,24,24) / res5 (1,512,12,12)
```

### 5.4 步骤三：接入完整模型跑一张图（CPU）

在上面脚本末尾追加（示例代码）：

```python
from detectron2.modeling import build_model
from mask2former import add_maskformer2_config  # 已 import；mask2former 本身已注册全部组件

model = build_model(cfg).eval()   # cfg.MODEL.BACKBONE.NAME 已指向 MiniCNNBackbone
print("backbone 是:", type(model.backbone).__name__)   # 预期 MiniCNNBackbone

with torch.no_grad():
    results = model([{"image": torch.randint(0, 255, (3, 384, 384)).float()}])
print("输出键:", [list(r.keys()) for r in results])
# 预期: [['sem_seg', 'panoptic_seg', 'instances']]（顺序可能不同，三开来自 Base yaml 的 TEST 段）
```

两个排障提示：

1. **CUDA 扩展依赖**：默认像素解码器是 `MSDeformAttnPixelDecoder`，其 ops 模块在 import 期强依赖已编译的 `MultiScaleDeformableAttention` 扩展（u1-l2 讲过：无 GPU 机器也要装 CUDA toolkit 后 `FORCE_CUDA=1` 编译；CPU 张量运行期会自动回退纯 PyTorch 实现）。若尚未编译，追加一行改用无需扩展的 FPN 版解码器即可继续实验：
   `cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"`（契约兼容，u3-l4 已验证两版即插即换）。
2. **数据集元数据**：eval 前向的 panoptic 分支需要 `MetadataCatalog` 里 COCO 数据集的类别信息。`import mask2former` 已完成注册（u2-l2 讲过注册不碰磁盘），**不需要**下载真实数据集。

### 5.5 需要观察的现象与预期结果

- `backbone.output_shape()` 打印四个 `ShapeSpec`，通道 64/128/256/512、stride 4/8/16/32——验证契约二。
- 各 stage 特征图空间尺寸严格按 1/4、1/8、1/16、1/32 缩小——验证 forward 与 stride 表一致。
- 完整模型的 `type(model.backbone).__name__` 为 `MiniCNNBackbone`，且前向不报通道不匹配错——说明 `output_shape` 上报的通道被 pixel decoder 正确消费（lateral 1×1 卷积按 64/128/256/512 建权重）。
- 全过程只在配置里改了 `BACKBONE.NAME`（及可选的 `PIXEL_DECODER_NAME`）——这就是三段式架构「组件可替换」的直接证据。
- 本实践的输出数值与键名以本地实际运行为准（待本地验证）；若在步骤三改用 `BasePixelDecoder`，`TRANSFORMER_IN_FEATURE: multi_scale_pixel_decoder` 无需改动，因其第二项返回值无人消费。

### 5.6 选做延伸

用 `sum(p.numel() for p in backbone.parameters())` 分别统计 MiniCNNBackbone、R50（`build_resnet_backbone`，DEPTH 50）、`D2SwinTransformer`（tiny 超参，参照 [maskformer2_swin_tiny_bs16_50ep.yaml:L5-L12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L5-L12) 设置 `cfg.MODEL.SWIN.*`）三者参数量，感受「同契约、不同容量」的替换谱系。

## 6. 本讲小结

- 替换 backbone 的完整闭环是三步：**写实现并注册**（`@BACKBONE_REGISTRY.register()`）、**保证被 import**（`modeling/__init__.py` 只显式 import `swin`，新文件要么手动 import、要么加一行；`backbone/__init__.py` 是空文件不会帮你扫描）、**改一行配置**（`MODEL.BACKBONE.NAME`）。
- Backbone 接口契约三件套：`forward` 返回特征字典、`output_shape()` 上报 `ShapeSpec(channels, stride)` 元信息（构建期被 `MaskFormerHead` 与 pixel decoder 消费）、`size_divisibility` 驱动 `ImageList.from_tensors` 的批内 padding；配置 `MASK_FORMER.SIZE_DIVISIBILITY` 为负时才回退用 backbone 的值。
- 命名对齐是字符串级硬约束：`forward` 输出键 = `output_shape` 键 ⊇ `SEM_SEG_HEAD.IN_FEATURES`，还需覆盖 `DEFORMABLE_TRANSFORMER_ENCODER_IN_FEATURES` 点名的 res3/4/5；错名在构建期抛 KeyError。
- `D2SwinTransformer` 是标准适配器范本：双继承「上游网络 + Backbone 接口」，职责压缩为 cfg 翻译、元信息表（stride 硬编码为结构常量、通道随配置从 `num_features` 读取）、契约实现三件事。
- Swin 故意沿用 `res2..res5` 命名，使 R50 配置零改动即可切换——**接口命名的兼容性价值高于实现本身**；写自己的 backbone 时同样建议沿用这套名字。
- 注册表不区分类与工厂函数（R50 的 `build_resnet_backbone` 是函数，`D2SwinTransformer` 是类），统一要求 `(cfg, input_shape)` 签名。

## 7. 下一步学习建议

本讲替换了三段式的第一段，接下来按依赖顺序：

1. **u8-l2（自定义 Pixel Decoder）**：替换第二段，学习 `forward_features` 的三元组返回契约（1/4 的 mask_features、None、长度 3 的 multi_scale_features）——它与本讲的 `output_shape` 契约同构，但多了一条分辨率约定。
2. **u8-l3（自定义 Transformer Decoder 与 Registry 机制）**：第三段用的是项目自建的 `TRANSFORMER_DECODER_REGISTRY` 而非 detectron2 注册表，对比两种 registry 的实现差异。
3. 若想给 MiniCNNBackbone 挂预训练权重，先读 [tools/convert-pretrained-swin-model-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py) 与 u3-l3 讲过的 `matching_heuristics` 机制，再回看 u10-l4 的权重转换主题。
4. 动手前建议重读 [ADVANCED_USAGE.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/ADVANCED_USAGE.md)——它只有 38 行，是三个扩展讲（u8-l1/l2/l3）共同的官方总纲。
