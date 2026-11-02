# 模型分析：FLOPs 与参数统计

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **FLOPs、参数量、激活量** 三种模型分析口径分别衡量什么、各自是否依赖输入数据。
2. 会用仓库自带的 `tools/analyze_model.py` 对任意 Mask2Former 配置生成 **FLOPs 表、参数量表、激活量表与模型结构**，并理解 `--use-fixed-input-size`、`--num-inputs` 等参数的作用。
3. 理解 `FlopCountAnalysis` 与 `flop_count_table` 的工作方式：基于 **JIT trace 的按算子计数**、只统计注册过规则的算子、表格只反映单个样本。
4. 解释**自定义算子的统计局限**：多尺度可变形注意力（MSDeformAttn）的 CUDA/pybind 核心对 trace 不可见，它的计算量如何「消失」或以何种形态出现在表格中，以及作者在源码里留下的专门对策。
5. 在统一输入尺寸下对比 R50 与 Swin 配置的计算量并计算 FLOPs 比值。

## 2. 前置知识

### 2.1 FLOPs：模型要算多少次浮点运算

**FLOPs**（Floating Point Operations，浮点运算次数）衡量模型跑一次前向要执行多少次浮点乘/加。常用单位是 GFLOPs（\(10^9\) 次运算）。以一个卷积层为例（忽略 bias）：

\[
\text{FLOPs}_{conv} \approx H_{out} \cdot W_{out} \cdot C_{out} \cdot k^2 \cdot C_{in}
\]

即「每个输出位置、每个输出通道，都要做 \(k^2 \cdot C_{in}\) 次乘加」。注意：

- FLOPs **依赖输入尺寸**：同一模型，1024×1024 输入约是 512×512 输入的 4 倍 FLOPs（对于与分辨率线性相关的层）。
- 不同计数工具对「一次乘加算 1 还是 2」约定不同，**横向比较必须用同一工具**。
- FLOPs 高 ≠ 更慢：它不反映访存带宽、并行度和算子的工程实现质量。Mask2Former 的可变形注意力 FLOPs 很低（每个 query 只采样少量点），但这恰恰依赖精心编写的 CUDA kernel 才能快。

### 2.2 参数量与激活量

- **参数量（parameters）**：模型里所有可学习张量的元素总数。它只由网络结构决定，与输入尺寸无关，因此统计时既不需要数据也不需要权重文件。
- **激活量（activations）**：前向过程中各算子输出张量的元素总数，是显存占用的主要来源之一（与 u10-l2 的 OOM 主题直接相关）。它同样依赖输入尺寸。

### 2.3 fvcore 与「trace 计数」的黑盒问题

本讲使用的计数器来自 FAIR 的 **fvcore** 库（`pip install fvcore`，通常随 detectron2 一起装好）。它的思路是：用 `torch.jit.trace` 把模型前向录成一张计算图，图上每个 **aten 算子**（如 `aten::conv2d`、`aten::linear`）按注册过的规则折算 FLOPs。这带来一个天然盲区：

> 凡是 trace 录不进图的计算，FLOPs 一律不计。**自定义 CUDA/C++ 扩展（pybind 函数）就是这样的黑盒。**

Mask2Former 的核心组件 MSDeformAttn 恰好有一个编译型 CUDA 扩展（见 u1-l2），所以这个问题在本项目里不是理论风险，而是每次跑 FLOPs 都会遇到的现实——4.4 节会精读源码证据。

### 2.4 与前置讲义的衔接

- u3-l1：`MaskFormer.forward` 训练/推理两条路径——本讲统计的是 **eval 模式**的前向（含三个推理分支的后处理），不是训练损失路径。
- u1-l5：配置系统五层叠加——`tools/analyze_model.py` 的 `setup` 与 `train_net.py` 用同一套合并顺序。
- u1-l2：MSDeformAttn CUDA 算子编译——本讲实践要求扩展已编译（即使纯 CPU 跑，`import` 期也强依赖编译产物）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tools/analyze_model.py` | 本讲主角：改自 detectron2 的分析脚本，提供 flop / activation / parameter / structure 四个任务 |
| `mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py` | 被统计的 MSDeformAttn 模块：4 个 Linear（可被统计）+ 核心采样（不可见），以及作者留下的 FLOPs 专用注释行 |
| `mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py` | CUDA/pybind 封装 `MSDeformAttnFunction` 与纯 PyTorch 参考实现 `ms_deform_attn_core_pytorch`（含 `grid_sample`） |
| `configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml` | 对照配置一：R50 backbone |
| `configs/coco/panoptic-segmentation/swin/maskformer2_swin_base_384_bs16_50ep.yaml` | 对照配置二：Swin-Base backbone |
| `configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml` | 两个配置的共同基底（数据集、IMAGE_SIZE、WEIGHTS 等） |
| `mask2former/maskformer_model.py` | 被分析的对象 `MaskFormer` 本体（u3-l1 已精读，本讲当黑盒用） |

## 4. 核心概念与源码讲解

### 4.1 三种分析口径：脚本骨架与四任务分发

#### 4.1.1 概念说明

`tools/analyze_model.py` 把「分析模型」拆成四个正交任务，每个任务由一个 `do_*` 函数承担：

| 任务 | 函数 | 需要数据集？ | 需要权重？ | 原因 |
| --- | --- | --- | --- | --- |
| `parameter` | `do_parameter` | 否 | 否 | 参数量只由结构决定 |
| `structure` | `do_structure` | 否 | 否 | 只打印 `str(model)` |
| `flop` | `do_flop` | **是** | **是** | FLOPs 依赖输入形状，需真实（或定长）输入跑前向；脚本里会加载 checkpoint |
| `activation` | `do_activation` | **是** | **是** | 激活量同样依赖输入 |

这个「口径决定依赖」的对应关系是使用脚本前必须建立的第一认知：**只想看参数量，什么都不用准备；想看 FLOPs，就得备好测试数据（和任一可加载的权重文件）**。

#### 4.1.2 核心流程

脚本主流程伪代码：

```text
解析命令行（detectron2 的 default_argument_parser + --tasks/--num-inputs/--use-fixed-input-size）
├── 断言：不允许 --eval-only；必须单卡（num_gpus == 1）
├── setup(args)：组装配置（yaml 或 LazyConfig 两分支）
└── for task in args.tasks:
        {flop, activation, parameter, structure}[task](cfg)
```

`setup` 与 u2-l1 的 `train_net.py::setup` 同构，只是针对分析场景做了两处调整：把 `DATALOADER.NUM_WORKERS` 强制为 0（避免多进程加载干扰统计），并额外给 fvcore 配了 logger（`setup_logger(name="fvcore")`），让 fvcore 的告警（如 unsupported ops）能打印出来。

#### 4.1.3 源码精读

**进口清单——三件套从哪来。** fvcore 提供表格渲染，detectron2 提供分析器与参数表：

- [tools/analyze_model.py:9](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L9)：`from fvcore.nn import flop_count_table  # can also try flop_count_str`——注释提示还有 `flop_count_str` 这个更紧凑的备选输出格式。
- [tools/analyze_model.py:17-21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L17-L21)：从 `detectron2.utils.analysis` 导入 `FlopCountAnalysis`（FLOPs 分析器）、`activation_count_operators`（激活量）、`parameter_count_table`（参数量表）。detectron2 的 `FlopCountAnalysis` 是对 fvcore `JitModelAnalysis` 的封装，使它直接接受 detectron2 dataloader 产出的 `list[dict]` 输入。
- [tools/analyze_model.py:26-30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L26-L30)：把仓库上一级目录插进 `sys.path`，再 `from mask2former import add_maskformer2_config`。这一行同时触发了 u1-l3 讲过的「import 即注册」——没有它，`build_model` 找不到 `MaskFormer` 这个 meta arch。

**配置组装。** [tools/analyze_model.py:35-49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L35-L49)：`.yaml` 走 `get_cfg → add_deeplab_config → add_maskformer2_config → merge_from_file → NUM_WORKERS=0 → merge_from_list → freeze` 五层叠加，与 u1-l5 的顺序完全一致；另一分支支持 detectron2 新式 `LazyConfig`（本仓库 configs 全是 yaml，不会走到）。

**参数量表。** [tools/analyze_model.py:116-121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L116-L121)：`do_parameter` 只 `build_model(cfg)` 后调 `parameter_count_table(model, max_depth=5)`——按模块层级（最深 5 层）打印各子模块参数量，不碰数据、不碰权重。`do_structure`（[L124-129](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L124-L129)）同理。

**命令行契约。** [tools/analyze_model.py:146-151](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L146-L151) 定义 `--tasks`（可多选）；[L160-164](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L160-L164) 定义 `--use-fixed-input-size`；[L166-167](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L166-L167) 两个断言：`assert not args.eval_only`（分析脚本不接受评估模式）与 `assert args.num_gpus == 1`（分析必须单进程）。[L171-177](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L171-L177) 用字典把任务名映射到函数并逐个执行。

一个容易踩的坑：[tools/analyze_model.py:134-144](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L134-L144) 的帮助示例是从 detectron2 原版脚本带过来的**过时路径**（`../configs/COCO-InstanceSegmentation/...`），在本仓库不存在；实际使用要换成 `configs/coco/panoptic-segmentation/...`。

#### 4.1.4 代码实践

**实践目标**：零数据、零权重跑通两个无依赖任务，建立对输出形态的直观感受。

操作步骤（在仓库根目录；CPU 机器加 `MODEL.DEVICE cpu`）：

```bash
python tools/analyze_model.py --tasks parameter structure \
    --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
    MODEL.DEVICE cpu
```

需要观察的现象：

1. `Parameter Count` 表：顶层 `model` 一行是总参数量，往下是 `backbone`、`sem_seg_head` 等分层行（`max_depth=5`）。
2. `Model Structure`：完整的模块树，可用来核对 u3-l1 讲过的三段式（`backbone` / `sem_seg_head.pixel_decoder` / `sem_seg_head.predictor`）。

预期结果：参数表顶层约几千万量级（R50 backbone 本身约 23.5M，加上 head 远大于此——具体数字**待本地验证**，以本地输出为准）；结构打印不应报任何缺权重/缺数据错误。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--tasks parameter` 不需要数据集和权重，而 `--tasks flop` 两者都需要？
**答案**：参数量是模型结构的静态属性，`build_model` 之后数一遍 `nn.Parameter` 即得；FLOPs 与激活量依赖每个算子的输入形状，必须喂进真实（或定长）的输入跑一遍前向；而 `do_flop` 源码里显式调用了 `DetectionCheckpointer(model).load(...)`，所以还要求权重路径可加载。

**练习 2**：FLOPs 更低的模型一定更快吗？
**答案**：不一定。FLOPs 只数浮点运算次数，不反映访存模式、并行度与算子实现。MSDeformAttn 每个 query 只采样 \(L \times K\) 个点、FLOPs 极低，但若改用 4.4 节的纯 PyTorch 参考实现（大量小算子 + `grid_sample`），速度会显著慢于 CUDA 实现。

**练习 3**：同一模型输入从 1024×1024 改为 512×512，卷积/线性层为主的 backbone 部分 FLOPs 大约变为多少？
**答案**：约 \(1/4\)。这类层 FLOPs 与输出像素数成正比，面积缩小 4 倍即缩小 4 倍（归一化坐标相关的逐 query 计算不受影响）。

### 4.2 do_flop 主流程：逐样本统计与聚合

#### 4.2.1 概念说明

`do_flop` 是本讲的核心函数。它要解决的问题是：**FLOPs 随输入尺寸波动**——测试集中每张图尺寸不同（最短边缩放后长宽各异，还受 padding 影响），单张图的 FLOPs 没有代表性。脚本的对策是统计意义上的：

1. 从真实测试集取若干样本（默认 100 个）；
2. 每个样本独立跑一次 `FlopCountAnalysis`；
3. 对「每类算子的 FLOPs」与「总 FLOPs」跨样本求均值与标准差。

另一个口径问题：测试图尺寸不一会导致均值混入「图片大小分布」这个无关变量。`--use-fixed-input-size` 把每张图替换成 `crop_size × crop_size` 的零张量，统计的就纯粹是「定长输入下模型的计算量」，这也正是论文表格里 GFLOPs 的通常口径（固定输入分辨率）。

#### 4.2.2 核心流程

```text
do_flop(cfg):
    data_loader = build_detection_test_loader(cfg, DATASETS.TEST[0])   # 需要 COCO 数据
    model = build_model(cfg); 加载 WEIGHTS; model.eval()
    counts = Counter(); total_flops = []
    for idx, data in zip(trange(num_inputs), data_loader):   # 实际迭代 min(num_inputs, 数据集大小)
        if use_fixed_input_size:
            data[0]["image"] = zeros(3, crop_size, crop_size)  # 消除尺寸波动
        flops = FlopCountAnalysis(model, data)
        if idx > 0: 关闭 unsupported/uncalled 告警          # 首个样本报一次就够
        counts += flops.by_operator()                        # 按算子累计
        total_flops.append(flops.total())                    # 总量累计
    打印 flop_count_table(flops)          # 注意：表格只基于最后一个样本
    打印各算子平均 GFLOPs、总 GFLOPs（均值±标准差）
```

#### 4.2.3 源码精读

**准备阶段**：[tools/analyze_model.py:54-56](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L54-L56)——用 detectron2 的**通用** test loader 读 `cfg.DATASETS.TEST[0]`（R50 配置下即 `coco_2017_val_panoptic_with_sem_seg`，见 [Base-COCO-PanopticSegmentation.yaml:18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L18)）。注意这里**没有**用 Mask2Former 的自定义 dataset mapper——FLOPs 只需要图像张量，不需要 GT 标注。随后 `build_model(cfg)`（注册表构建，u1-l3）与 `DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)`。两个权重相关的现实约束：

- R50 配置的 `WEIGHTS` 默认是 `detectron2://ImageNetPretrained/torchvision/R-50.pkl`（[Base yaml L5](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L5)），联网时可自动下载——FLOPs 只看形状，权重内容无关紧要。
- Swin 配置的 `WEIGHTS` 是**本地相对路径** `"swin_base_patch4_window12_384.pkl"`（[swin yaml L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_base_384_bs16_50ep.yaml#L14)），本地没有该文件时加载会失败，需在命令行用 `MODEL.WEIGHTS` 指向任一可加载的 checkpoint。

**eval 模式**：[tools/analyze_model.py:62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L62) `model.eval()`——统计的是**推理前向**：backbone → pixel decoder → transformer decoder → mask 上采样 → 语义/实例/全景三个推理分支（u3-l1、u6 系列）。也就是说这个 FLOPs **包含后处理**，与训练时（匈牙利匹配 + 逐点损失）的计算路径并不相同。

**定长输入替换**：[tools/analyze_model.py:67-70](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L67-L70)——`data[0]["image"]` 被换成 `(3, crop_size, crop_size)` 的全零张量，`crop_size = cfg.INPUT.CROP.SIZE[0]`。`INPUT.CROP.SIZE` 不由 `add_maskformer2_config` 注册（本仓库的 [config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L18) 只加了 `CROP.SINGLE_CATEGORY_MAX_AREA`），其默认值来自 detectron2/add_deeplab_config，**随版本可能不同，建议运行前打印确认**（待确认项）。注意该开关只在 yaml（`CfgNode`）分支生效。

**统计与聚合**：[tools/analyze_model.py:66-75](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L66-L75)——`zip(trange(args.num_inputs), data_loader)` 实际迭代次数是两者的较小值；每个样本构造一个 `FlopCountAnalysis(model, data)`（`data` 就是 loader 产出的 `list[dict]`），`by_operator()` 返回 `Counter`（算子名 → FLOPs）逐样本累加，`total()` 是总数。

**三段输出**：[tools/analyze_model.py:77-84](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L77-L84)——(a) `flop_count_table(flops)` 明确标注「computed from only one input sample」，只反映**最后一个样本**；(b) 各算子平均 GFLOPs，除以 `(idx + 1)` 即实际样本数；(c) 总 GFLOPs 的均值±标准差。

#### 4.2.4 代码实践

**实践目标**：确认「表格 = 单样本、均值 = 多样本」这一口径分离。

操作步骤（需要按 u2-l2 备好 COCO 数据，或先做 5. 综合实践的无数据版）：

```bash
python tools/analyze_model.py --tasks flop --num-inputs 10 --use-fixed-input-size \
    --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
    MODEL.DEVICE cpu
```

需要观察的现象：

1. 运行结束前 fvcore 打印的告警（首个样本会打印，之后被 [L72-73](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L72-L73) 关闭）：unsupported operators 与 uncalled modules 两类清单。
2. `Flops table` 与 `Average GFlops for each type of operators` 两段输出。

预期结果：开 `--use-fixed-input-size` 时各样本 FLOPs 几乎不变（`Total GFLOPs` 的标准差接近 0）；不开时标准差明显变大（图片尺寸分布所致）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`zip(tqdm.trange(args.num_inputs), data_loader)`，当 `--num-inputs 100` 而测试集只有 5000 张时，迭代多少次？如果测试集只有 50 张呢？
**答案**：`zip` 取较短者——前者迭代 100 次，后者迭代 50 次。`args.num_inputs` 只是上限，不是保证值。

**练习 2**：为什么脚本在 `idx > 0` 后才关闭告警，而不是一开始就关？
**答案**：unsupported/uncalled 告警对每个样本内容相同，保留首个样本的一次打印既能提示盲区，又避免逐样本刷屏；这是 `unsupported_ops_warnings(False).uncalled_modules_warnings(False)` 链式调用放在 `if idx > 0` 分支里的原因。

**练习 3**：`do_activation` 的平均输出 [tools/analyze_model.py:107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L107) 用 `v / idx` 而不是 `v / (idx + 1)`，有什么隐患？
**答案**：`idx` 是最后一个样本的下标（从 0 计），样本数为 `idx + 1`，所以平均值被高估了一点点；更严重的是当 `--num-inputs 1` 且数据集只有 1 张图时 `idx = 0`，会直接 `ZeroDivisionError`。这是脚本从 detectron2 原版继承来的小瑕疵，读源码时值得注意。

### 4.3 FlopCountAnalysis 与 flop_count_table 的 API 与口径

#### 4.3.1 概念说明

`FlopCountAnalysis` 的对象上主要用到 4 个 API（均可在脚本里看到用法）：

| API | 返回 | 含义 |
| --- | --- | --- |
| `flops.total()` | `int` | 总 FLOPs（**只含注册过规则的算子**） |
| `flops.by_operator()` | `Counter` | 算子名 → FLOPs，如 `aten::conv2d`、`aten::linear` |
| `flops.unsupported_ops_warnings(False)` | `self`（可链式） | 关闭「遇到无规则算子」的告警 |
| `flops.uncalled_modules_warnings(False)` | `self`（可链式） | 关闭「有参数的模块没被前向调用到」的告警 |

`flop_count_table(flops)` 则把 `by_operator` 的结果渲染成按模块层级展开的表格。三个必须记住的口径限制：

1. **只数有规则的算子**：fvcore 为 `conv`、`linear`、`matmul` 等常见 aten 算子注册了折算规则；逐元素操作（加、乘、softmax 的一部分）通常不计入 FLOPs，无规则算子计入 unsupported 清单（告警里能看到）。
2. **表格是单样本的**：见 4.2.3。
3. **eval 前向含后处理**：三个推理分支的计算都被统计在内。

#### 4.3.2 核心流程

```text
FlopCountAnalysis(model, data)
    └── torch.jit.trace(model, data)          # 录前向计算图（aten 算子节点）
        └── 对每个节点查 flop 规则表 → 无规则 → 记入 unsupported_ops（告警）
    total() / by_operator() 均来自这张图
```

「uncalled modules」则相反：模型里存在参数、但本次前向没执行到的子模块（例如本讲实践中从未触发的某些分支），fvcore 也会提示——这对发现「配置了但没用上的模块」很有用。

#### 4.3.3 源码精读

**构造与链式调用**：[tools/analyze_model.py:71-73](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L71-L73)——一行构造、两行关闭告警，返回 `self` 所以可以连写。`data` 原样来自 dataloader，即 detectron2 风格的 `list[dict]`（`{"image": tensor, "height": ..., "width": ..., ...}`），`MaskFormer.forward`（u3-l1）接受的正是这个格式。

**聚合与输出**：[tools/analyze_model.py:74-77](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L74-L77)：`counts += flops.by_operator()` 是 `Counter` 的逐键相加；`flop_count_table(flops)` 的日志前缀「Flops table computed from only one input sample」是脚本作者特意写明的口径声明。

**表格渲染入口**：[tools/analyze_model.py:9](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L9) 导入的 `flop_count_table` 来自 fvcore；detectron2 侧的 `parameter_count_table(model, max_depth=5)`（[L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/analyze_model.py#L121)）是同构的参数量版本，两者常配套看：FLOPs 看「算得多少」，参数量看「存得多少」。

#### 4.3.4 代码实践

**实践目标**：脱离数据集，直接对一个合成输入构造 `FlopCountAnalysis`，亲眼看到 `by_operator` 与 unsupported 清单。

操作步骤——在仓库根目录新建 `flops_probe.py`（**示例代码**，不属于本仓库）：

```python
import sys, torch
from collections import Counter
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from detectron2.utils.analysis import FlopCountAnalysis
from mask2former import add_maskformer2_config  # 触发注册

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.DEVICE = "cpu"
cfg.freeze()

model = build_model(cfg).eval()
inputs = [{"image": torch.zeros(3, 1024, 1024)}]   # 合成定长输入
flops = FlopCountAnalysis(model, inputs)
print("total GFLOPs =", flops.total() / 1e9)
for k, v in sorted(flops.by_operator().items()):
    print(f"{k:40s} {v/1e9:10.2f} G")
```

运行 `python flops_probe.py`（前置：u1-l2 的 CUDA 扩展已编译，且 `MODEL.DEVICE cpu` 走 CPU）。

需要观察的现象：算子清单里 `aten::conv2d`、`aten::linear`/`addmm`、`aten::matmul`、`bmm` 等的分布；特别留意是否出现 `grid_sampler`（4.4 节的关键线索）以及告警里的 unsupported/uncalled 清单。

预期结果：能跑通并输出总量与分布（具体数值**待本地验证**）；`conv` 类应主要来自 backbone 与像素解码器。

#### 4.3.5 小练习与答案

**练习 1**：`flop_count_table(flops)` 打印的是 100 个样本的平均 FLOPs 吗？
**答案**：不是。日志明说「computed from only one input sample」——表格只基于最后一次构造的 `flops`（最后一个样本）；跨样本平均体现在单独的「Average GFlops」与「Total GFLOPs 均值±标准差」两行。

**练习 2**：`by_operator()` 各项之和与 `total()` 是什么关系？
**答案**：`total()` 等于 `by_operator()` 所有注册算子之和。被记入 unsupported 的算子不计入两者，所以这个「总」永远是**下界**。

**练习 3**：假设某模块有参数但从未被前向执行，哪个告警会提示它？这个现象在本讲实践里可能来自哪里？
**答案**：uncalled modules 告警。例如配置了但当前任务分支没走到的子模块（读者可在实践中对照告警清单与 `do_structure` 打印的模块树逐一核对）。

### 4.4 自定义算子的黑盒：MSDeformAttn 如何「消失」在统计里

#### 4.4.1 概念说明

MSDeformAttn 模块由两部分组成，它们在 FLOPs 统计中的命运截然不同：

1. **4 个 `nn.Linear`**（`sampling_offsets`、`attention_weights`、`value_proj`、`output_proj`）——标准 aten 算子，trace 可见、有规则，**会被正常计数**。
2. **核心的多尺度采样与加权求和**——两种实现：
   - CUDA/pybind 路径 `MSDeformAttnFunction.apply` → `MSDA.ms_deform_attn_forward`：编译型扩展，**JIT trace 完全看不到**，FLOPs 记 0，图里连节点都没有；
   - CPU 回退路径 `ms_deform_attn_core_pytorch`：用 `F.grid_sample` 等标准算子实现，trace **可见**，但 `grid_sample` 是否有 fvcore 计数规则、还是落进 unsupported 清单，取决于安装的 fvcore 版本（**待本地验证**）。

所以「deformable attention 在表中的呈现方式」取决于你跑在哪条路径上：GPU + 编译扩展时，MSDeformAttn 只有 4 个 Linear 被计入，采样核心**静默缺失**（不报错、不告警，只是少了）；CPU 时核心以 `grid_sample`/`stack`/`mul`/`sum` 等算子形态出现（计入与否另说）。这不是 bug，而是 trace 计数方法的原理性局限——**含自定义扩展的模型，FLOPs 表永远低估真实计算量**。

最强的证据是作者自己：源码里专门留了一行为 FLOPs 统计切换实现路径的注释（见下）。

#### 4.4.2 核心流程

```text
MSDeformAttn.forward(query, reference_points, input_flatten, ...)
├── value = value_proj(input_flatten)                 # Linear → 可统计
├── sampling_offsets = sampling_offsets(query)         # Linear → 可统计
├── attention_weights = softmax(attention_weights(query))  # Linear+softmax
├── try: MSDeformAttnFunction.apply(...)              # CUDA/pybind → 黑盒，FLOPs=0
│   except: ms_deform_attn_core_pytorch(...)          # grid_sample 等 → trace 可见
└── output = output_proj(output)                      # Linear → 可统计
```

#### 4.4.3 源码精读

**可统计的四个投影层**：[mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py:59-62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L59-L62)——`sampling_offsets`（\(d \to n_{heads} \cdot n_{levels} \cdot n_{points} \cdot 2\)）、`attention_weights`（\(d \to n_{heads} \cdot n_{levels} \cdot n_{points}\)）、`value_proj`、`output_proj`（均 \(d \to d\)）。它们是表格中 MSDeformAttn 唯一「实打实」的贡献。

**双路径分派与作者的证据**：[ms_deform_attn.py:116-123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L116-L123)——`try` 里调 CUDA 实现，`except` 回退 PyTorch 参考实现（u1-l2 讲过：CPU 张量会让 C++ 分发层抛错）。紧接着两行注释是本讲最值得划线的代码：

```python
# # For FLOPs calculation only
# output = ms_deform_attn_core_pytorch(value, input_spatial_shapes, sampling_locations, attention_weights)
```

「For FLOPs calculation only」——作者明确知道 CUDA 路径对 FLOPs 工具不可见；想统计可 deformable attention 的计算量时，把这两行解开、注释掉上面的 `try/except`，让前向强制走 `grid_sample` 版本。这是「用可trace的参考实现替换黑盒扩展」的通用对策，代价是数值上两实现等价、但算子构成完全不同，统计到的也只是 `grid_sample` 等算子的口径。

**黑盒的入口**：[mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py:32-39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L32-L39)——`MSDeformAttnFunction.forward` 调用 `MSDA.ms_deform_attn_forward(...)`（pybind 绑定的 C++/CUDA 函数）。trace 录图时这个调用不产生任何 aten 节点，输出张量对图而言「凭空出现」。[L21-29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L21-L29) 还说明：即使纯 CPU 跑，`import` 期也强依赖编译产物（找不到就 `raise`），所以本讲的实践前置仍是 u1-l2 的 `sh make.sh`。

**可见的参考实现**：[ms_deform_attn_func.py:52-72](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L52-L72)——`ms_deform_attn_core_pytorch` 按尺度循环做 `F.grid_sample`（[L66-67](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/functions/ms_deform_attn_func.py#L66-L67)）双线性采样再加权求和。全部是标准 aten 算子——这就是 CPU 回退路径下表格里会多出 `grid_sampler` 一族算子的原因。

顺带一个量化直觉（示例推导，非仓库代码）：MSDeformAttn 核心的真实计算量约为

\[
\text{FLOPs}_{core} \sim N \cdot Q \cdot L \cdot K \cdot D \cdot c
\]

其中 \(N\) 为批、\(Q\) 为 query 数、\(L\) 尺度数、\(K\) 每尺度采样点数、\(D\) 通道数、\(c\) 为每采样点每通道的双线性插值+加权求和常数。相比全局注意力 \(O(N Q S D)\)（\(S\) 为全部空间位置数），\(L \cdot K \ll S\) 正是 Mask2Former 选它的原因——但如上，这部分在 GPU 统计里是 0。

#### 4.4.4 代码实践

**实践目标**：亲眼对比「黑盒路径」与「可见路径」在算子表中的差别。

操作步骤：

1. 用 4.3.4 的 `flops_probe.py`（CPU，走 `grid_sample` 回退）记录 `by_operator()` 输出。
2. 把 [ms_deform_attn.py:116-121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L116-L121) 的 `try/except` 临时替换为直接调用 `MSDeformAttnFunction.apply(...)`（在有 GPU 的机器上），或反过来解开 [L122-123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/modules/ms_deform_attn.py#L122-L123) 的注释——在**副本目录**中做，勿改仓库源码；无条件改代码时，改为阅读对比亦可。
3. 重新运行脚本，对比两次 `by_operator()`。

需要观察的现象：`grid_sampler`（或 `aten::grid_sampler_2d`，版本相关）是否只在回退路径出现；`aten::linear` 的计数在两条路径下是否一致（应一致——4 个 Linear 不受影响）；`total()` 在两条路径下的差值即「黑盒吞掉」的部分。

预期结果：GPU 路径总 FLOPs 低于 CPU 回退路径（若 fvcore 对 grid_sample 有规则）或两者接近但算子构成不同（若无规则，落 unsupported）。具体形态**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `aten::linear` 能被统计，而 `MSDA.ms_deform_attn_forward` 不能？
**答案**：`nn.Linear` 前向最终落到标准 aten 算子（`addmm`/`linear`），JIT trace 能录成图节点且 fvcore 注册了折算规则；`MSDA.ms_deform_attn_forward` 是 pybind 编译扩展，Python→C++ 的边界 trace 无法进入，图中没有对应节点，自然无规则可套。

**练习 2**：源码里 `# For FLOPs calculation only` 两行注释说明作者遇到了什么问题、对策是什么？
**答案**：作者发现走 CUDA 路径时 FLOPs 工具统计不到可变形注意力的核心计算；对策是统计 FLOPs 时临时切换到纯 PyTorch 参考实现（`grid_sample` 版），让计算对 trace 可见。这印证了「自定义扩展 = 统计盲区」。

**练习 3**：除了切换实现路径，还有什么办法能把 MSDeformAttn 的计算量纳入报表？
**答案**：手工估算并补偿——按 4.4.3 的公式 \(\text{FLOPs}_{core} \sim NQLKDc\) 算出核心部分，加上 4 个 Linear（fvcore 已统计），自行加总；或给 fvcore 注册自定义算子规则/用 `flops.set_op_handle` 类机制把黑盒入口映射到手工公式（具体 API 随 fvcore 版本变化，需查所用版本文档，待确认）。

## 5. 综合实践

**任务**：在统一输入尺寸下，对比 R50 与 Swin-Base 两个配置的 FLOPs 与参数量，算出比值，并说明 deformable attention 相关算子在表中的呈现方式。

**路线 A（推荐，零数据集）**：在仓库根目录新建 `flops_compare.py`（**示例代码**，不属于本仓库）：

```python
import torch
from fvcore.nn import flop_count_table
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from detectron2.utils.analysis import FlopCountAnalysis, parameter_count_table
from mask2former import add_maskformer2_config

CONFIGS = {
    "R50":      "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml",
    "SwinB384":"configs/coco/panoptic-segmentation/swin/maskformer2_swin_base_384_bs16_50ep.yaml",
}

results = {}
for name, path in CONFIGS.items():
    cfg = get_cfg(); add_deeplab_config(cfg); add_maskformer2_config(cfg)
    cfg.merge_from_file(path)
    cfg.MODEL.DEVICE = "cpu"; cfg.freeze()          # 不加载权重：FLOPs 只看形状
    model = build_model(cfg).eval()
    flops = FlopCountAnalysis(model, [{"image": torch.zeros(3, 1024, 1024)}])
    g = flops.total() / 1e9
    n_params = sum(p.numel() for p in model.parameters()) / 1e6
    results[name] = (g, n_params)
    print(f"===== {name} =====")
    print(f"total GFLOPs = {g:.1f}, params = {n_params:.1f} M")
    print(flop_count_table(flops, max_depth=3))
    print("by_operator:", {k: round(v/1e9, 2) for k, v in flops.by_operator().items()})

r = results["SwinB384"][0] / results["R50"][0]
print(f"\nFLOPs ratio SwinB384 / R50 = {r:.2f}")
```

运行 `python flops_compare.py`（前置：u1-l2 已编译 CUDA 扩展；CPU 下 MSDeformAttn 走 `grid_sample` 回退）。

**路线 B（有 COCO 数据时）**：分别执行

```bash
python tools/analyze_model.py --tasks flop parameter --use-fixed-input-size \
    --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml MODEL.DEVICE cpu
python tools/analyze_model.py --tasks flop parameter --use-fixed-input-size \
    --config-file configs/coco/panoptic-segmentation/swin/maskformer2_swin_base_384_bs16_50ep.yaml \
    MODEL.DEVICE cpu MODEL.WEIGHTS /path/to/any/loadable/checkpoint.pkl
```

（Swin 配置的默认 `WEIGHTS` 是本地相对路径，见 4.2.3，必须显式覆盖。）

**需要观察与记录**：

1. 两个配置的总 GFLOPs、总参数量，及比值（**待本地验证**；预期 Swin 版明显更重——backbone 参数量从 R50 的约 23.5M 跳到 Swin-Base 的 88M 量级）。
2. 在 `by_operator()` / 告警里找 deformable attention 的痕迹：CPU 路径下应能看到 `grid_sampler` 一族算子（是否计入 FLOPs 视 fvcore 版本）；4 个 Linear 的贡献在 `pixel_decoder.encoder` 相关层级下。
3. `flop_count_table` 中 backbone 与 head 的占比：两配置差异应集中在 backbone 行。

**预期结果**：得到一张「R50 vs Swin」的 GFLOPs/参数对照结论，并能用一句话说明「deformable attention 的核心计算在（CPU/GPU 哪条路径的）表里以什么形式出现、为什么」。

## 6. 本讲小结

- `tools/analyze_model.py` 提供四个正交任务：`parameter`/`structure` 零依赖，`flop`/`activation` 需要测试数据与可加载权重——口径决定依赖。
- `do_flop` 逐样本构造 `FlopCountAnalysis`，`by_operator()` 跨样本累计求平均；`flop_count_table` 只反映**单个样本**；`--use-fixed-input-size` 用零张量锁定输入尺寸，消除尺寸波动。
- 统计基于 JIT trace：只有注册过规则的 aten 算子计入 FLOPs，`total()` 永远是下界；unsupported/uncalled 两类告警是发现盲区的窗口。
- MSDeformAttn 是典型黑盒：4 个 Linear 被正常统计，CUDA/pybind 核心在 GPU 路径下静默缺失；CPU 回退路径以 `grid_sample` 形态可见。作者在 `ms_deform_attn.py` 留下的 `# For FLOPs calculation only` 注释行就是对策：统计时切到纯 PyTorch 参考实现。
- 统计的是 **eval 前向**（含三个推理分支的后处理），与训练路径不同；含自定义扩展的模型，FLOPs 报表必须注明口径。

## 7. 下一步学习建议

- **u10-l2（显存优化与 OOM 重试）**：本讲的「激活量」口径直接连接显存占用；`retry_if_cuda_oom` 的三段降级是高分辨率推理的工程保障。
- **u10-l3（测试时增强 TTA）**：TTA 会成倍放大本讲统计的前向计算量，两者对照看很有启发。
- 源码延伸阅读：`ops/test.py`（u1-l2 用过）验证 CUDA 与 PyTorch 两实现数值等价——正是 4.4 节「切换实现统计 FLOPs」可信的前提；fvcore 的 `JitModelAnalysis` 文档则解释规则注册机制的全貌。
