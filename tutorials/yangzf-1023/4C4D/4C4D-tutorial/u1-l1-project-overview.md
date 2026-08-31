# 4C4D 项目定位与核心思想

## 1. 本讲目标

学完本讲,你应该能够:

1. 用自己的话说出 4C4D 的**输入是什么、输出是什么**,以及它与传统 4DGS(4D Gaussian Splatting)在数据条件上的本质差异。
2. 复述论文的核心洞察:**在极稀疏视角下,几何学习比外观学习难得多**,以及 Neural Decaying Function(神经衰减函数)是如何回应这个洞察的。
3. 在源码中**精确定位** opacity decay(不透明度衰减)相关的三个关键代码位置:`train.py` 的参数区、`gaussian_renderer/__init__.py` 的 `render()`、`scene/gaussian_model.py` 的 `opacity_decay()` 方法。

本讲是整本手册的第一讲,不要求你读懂每一行代码——只要求建立正确的"全局地图",后续讲义会逐块展开。

## 2. 前置知识

本讲用通俗语言解释几个基础概念。如果你已熟悉 3DGS,可以快速浏览本节。

### 2.1 新视角合成(Novel View Synthesis)

给定一个场景从若干已知位置拍到的照片,目标是**合成出从全新位置看到的画面**。比如 4 台相机围着一张餐桌拍视频,我们希望渲染出"第 5 个位置"看到的画面,甚至"第 5 个位置在第 3 秒"看到的画面。

### 2.2 3D 高斯泼溅(3D Gaussian Splatting, 3DGS)

3DGS 是近年来新视角合成的主流方法之一。它的想法非常直观:

- 把场景表示成**几十万个带颜色的半透明"小椭球"**(即 3D 高斯);
- 每个小椭球有这些可优化的属性:中心位置 xyz、三个方向的缩放 scale、朝向 rotation、不透明度 opacity、颜色(用球谐函数表示,以支持视角相关的外观);
- 通过一个**可微的光栅化(rasterization)**过程把这些椭球"泼"到屏幕上得到图像,于是可以用梯度下降让渲染结果逼近真实照片。

### 2.4 从 3D 到 4D

动态场景在 3D 之外多了一个时间维。4DGS 的做法是给每个高斯再加上**时间属性**:时间中心 \( t \)、时间尺度等,使高斯变成 4D 空间(xyz + 时间)中的"四维椭球"。渲染某一时刻的画面时,把 4D 高斯在该时刻做数学上的"切片/边缘化",得到一组 3D 高斯再进行泼溅(细节在第 3 单元展开)。

### 2.5 稀疏视角问题

传统动态重建(如 Neural 3D Video 数据集的标准设定)往往需要**几十上百台同步相机**组成的相机阵列,采集成本极高。4C4D 的目标是把相机数量降到 **4 台便携相机**。相机少了,视角之间的重叠区域急剧缩小,重建难度骤增——这正是本项目要解决的核心问题。

### 2.5 不透明度(opacity)与几何

在 3DGS/4DGS 中,opacity 决定一个高斯"多大程度参与成像"。一个高斯如果 opacity 低,它对渲染图像的影响就小,收到的梯度也小;反之,opacity 高的高斯会被优化过程"重点关照"。记住这一点,是理解本讲核心创新的关键伏笔。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `README.md` | 项目说明书:摘要、安装、数据格式、训练/渲染/评估命令 | Abstract 中的核心洞察 |
| `assets/pipeline.png` | 框架总览图(pipeline 图) | 建立整体视觉印象 |
| `train.py` | **训练入口**:参数解析、训练主循环 | 参数区中 opacity decay 一族开关 |
| `gaussian_renderer/__init__.py` | 渲染主函数 `render()` | opacity decay 如何接入渲染 |
| `scene/gaussian_model.py` | 4D 高斯模型定义 | `opacity_decay()` 方法(本讲只定位,精读在第 6 单元) |
| `module/__init__.py` | Neural Decaying Function 的网络实现 `Coefficient` | 只看结构轮廓(精读在第 6 单元) |
| `LICENSE_gaussian_splatting.md` | 继承自 Inria gaussian-splatting 的许可证 | 了解代码血缘与非商用限制 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:**README 与项目定位**、**train.py 参数区**、**render() 中的衰减接入点**。

### 4.1 README.md:4C4D 是什么

#### 4.1.1 概念说明

4C4D 的名字是 "**4 C**amera **4D** Gaussian Splatting" 的缩写:用 4 台相机做 4D 高斯泼溅。

- **输入**:4 台便携相机同步拍摄的视频帧(按 `cam{XX}_{YYYY}.png` 命名),外加一份 COLMAP 格式的相机参数与初始点云(`sparse/0/` 目录)。
- **输出**:一组优化好的 4D 高斯,可以渲染**任意新视角、任意时刻**的画面,也可以在预留的测试视角上定量评估(PSNR/SSIM/L1)。

它的两个直接"前辈"是:

| 前辈 | 关系 |
|---|---|
| 3DGS / 4DGS | 4C4D 的代码库直接建立在 4DGS 之上(README 致谢部分明确说明),训练框架、4D 高斯表示、光栅化器都继承自它;4C4D 的贡献是让它**在极稀疏视角下也能工作** |
| MASt3R | 一个基于学习的重建方法;因为 4 个视角下 COLMAP 重建出的点云过于稀疏,4C4D 用 MASt3R 生成**稠密点云**作为高斯初始化 |

#### 4.1.2 核心流程

README 的 Abstract 段给出了整篇论文的逻辑链,可以概括为:

```text
问题:只用 4 台相机 → 视角极稀疏
  ↓
观察:稀疏条件下,学"几何"(高斯的位置/形状)远比学"外观"(颜色)困难
      → 几何与外观的学习不平衡
  ↓
方案:Neural Decaying Function f_θ(一个轻量神经网络)
      输入高斯的关键属性 → 输出一个衰减因子 → 调控高斯不透明度
  ↓
效果:4DGS 的梯度被引导得更多聚焦于几何学习
  ↓
结果:在多个稀疏视角数据集上超越先前方法
```

用一句话理解 Neural Decaying Function:**它在训练过程中对每个高斯的不透明度乘上一个 0 到 1 之间的因子,并且这个因子本身由一个小神经网络根据高斯的属性自适应预测、随训练一起被优化**。注意默认配置里因子范围是 `[0.996, 0.998]`——衰减非常轻微,它的意义不在"把不透明度压低"这个动作本身,而在于通过这个可微的操作改变梯度的流向(第 6 单元会深入展开)。

#### 4.1.3 源码精读

**Abstract 中的核心洞察**。[README.md:20-22](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L20-L22) 这一段是整个项目的"题眼":先指出先前工作需要几十甚至上百视角的密集采集,然后给出关键洞察——"geometric learning under sparse settings is substantially more difficult than modeling appearance"(稀疏设定下几何学习比外观建模困难得多),并引出 Neural Decaying Function 的两大作用:增强 4D 高斯的几何建模能力、缓解几何与外观建模之间的固有失衡。

**Pipeline 一段的机制描述**。[README.md:24-32](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L24-L32) 配合 [assets/pipeline.png](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/assets/pipeline.png) 说明:Neural Decaying Function \( f_\theta \) 是一个轻量神经网络,输入高斯的关键属性,输出控制不透明度衰减的因子;训练时 \( f_\theta \) 与 4D 高斯在光度渲染损失下**联合优化**(梯度反向传播同时更新两者)。建议你打开 pipeline.png 对照阅读。

**数据目录约定**。[README.md:66-88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L66-L88) 规定了输入数据的组织方式:图像文件名必须是 `cam{XX}_{YYYY}.png`(XX 为补零的相机编号,YYYY 为补零的帧号),`sparse/0/` 下放 COLMAP 格式的 `cameras.bin`(内参)、`images.bin`(外参)、`points3D.bin`(三维点)。这两个约定是第 2 单元数据加载讲义的基础。

**为什么用 MASt3R**。[README.md:47-49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L47-L49) 给出了理由:"Since COLMAP produces extremely sparse point clouds with few input views, we use MASt3R-based reconstruction instead"——输入视角太少时 COLMAP 重建的点云过于稀疏,不足以初始化高斯。

**三个入口命令**。[README.md:158-165](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L158-L165) 是训练命令(`python train.py --config ... --training_view ... --output_dir ...`);[README.md:167-178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L167-L178) 是训练后渲染新视角轨迹视频;[README.md:180-191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L180-L191) 是在预留测试视角上评估。注意三者都围绕 `train.py` 和 `render.py` 这两个入口展开。

**代码血缘与许可**。[README.md:206-208](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L206-L208) 的致谢说明代码库建立在 4DGS 与 MASt3R 之上;[LICENSE_gaussian_splatting.md:1-8](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/LICENSE_gaussian_splatting.md#L1-L8) 表明其中 gaussian-splatting 部分归 Inria 与 MPII 所有,**仅限非商业的研究与评估使用**——如果你计划做商业用途,需要先联系 Inria。

#### 4.1.4 代码实践

1. **实践目标**:通读 README,建立对项目输入输出与命令行的整体印象。
2. **操作步骤**:
   - 从头到尾读一遍 `README.md`,重点标记 Abstract、Pipeline、Dataset Structure、Training/Visualization/Evaluation 五个 section;
   - 打开 `assets/pipeline.png`,对照 README 第 24-32 行的文字描述,在图上找到"4D Gaussians""Neural Decaying Function \( f_\theta \)""渲染损失"等要素;
   - 不必真的运行训练,只需把三个入口命令抄录下来,标注每个参数(`--config`、`--training_view`、`--output_dir`、`--traj`、`--test`、`--start_checkpoint`)的作用。
3. **需要观察的现象**:pipeline 图中 \( f_\theta \) 的输入来自哪些高斯属性、输出连到了哪里(答案应是不透明度)。
4. **预期结果**:能在不看文档的情况下复述 4C4D 的输入(4 相机视频帧 + COLMAP 格式参数与点云)、输出(可渲染任意视角/时刻的 4D 高斯)与两大策略(MASt3R 稠密初始化、Neural Decaying Function)。

#### 4.1.5 小练习与答案

**练习 1**:4C4D 与 4DGS 的输入条件有什么本质不同?

**答案**:4DGS 等先前工作通常依赖几十甚至上百台相机的密集阵列采集;4C4D 只用 4 台便携相机,视角间重叠度低、COLMAP 点云极稀,因此必须额外引入 MASt3R 稠密初始化与 Neural Decaying Function 等策略来弥补信息不足。

**练习 2**:README 中"geometric learning under sparse settings is substantially more difficult than modeling appearance"这句话,对应了 4.1.2 流程图中的哪一步?它引出了什么方案?

**答案**:对应"观察"一步,即核心洞察——稀疏视角下几何与外观学习不平衡(几何更难)。它引出的方案是 Neural Decaying Function:用一个可优化的轻量网络对高斯不透明度做自适应衰减,促使 4DGS 梯度更多聚焦于几何学习。

### 4.2 train.py 参数区:opacity decay 的开关们

#### 4.2.1 概念说明

`train.py` 是训练的唯一入口。它的 `__main__` 段落负责注册所有命令行参数,再与 yaml 配置合并,最后调用 `training()` 函数。**论文的核心创新在参数区就有一席之地**——有一组以 `--opacity_decay` 为首的参数专门控制 Neural Decaying Function 的行为。读懂参数区,等于拿到了这个项目的"控制面板说明书"。

#### 4.2.2 核心流程

`train.py` 从命令行到训练启动的流程:

```text
解析命令行参数(parser.add_argument)
  ↓
OmegaConf.load(args.config) 读取 yaml 配置
  ↓
recursive_merge:yaml 中的键逐个写回 args(覆盖默认值)
  ↓
若干联动改写(本讲关心:opacity_decay → densify_until_iter)
  ↓
setup_seed 固定随机种子,safe_state 初始化
  ↓
调用 training(...),训练正式开始
```

#### 4.2.3 源码精读

**opacity decay 参数族**。[train.py:411-419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L411-L419) 注册了 Neural Decaying Function 的全部控制参数:

- `--opacity_decay`:总开关(注意它的写法是 `action="store_true", default=True`,见下文"值得注意的细节");
- `--f_max` / `--f_min`:衰减因子的上下界,默认 0.998 / 0.996,即因子被限制在 \([0.996, 0.998]\) 这个非常窄的区间;
- `--dropout_rate` / `--hidden_dim` / `--weight_decay`:\( f_\theta \) 网络本身的超参数(隐藏层维度默认 32,dropout 默认 0.1);
- `--decay_from_iter`:默认第 500 次迭代之后才启用衰减——先让高斯"热身",再开始衰减。

**衰减相关的三个辅助开关**。[train.py:427-429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L427-L429) 定义了 `--time_aware`(默认 True,只用"当前视角真正可见"的高斯做衰减)、`--reset_opacity`(默认 False,是否启用 3DGS 原生的周期性不透明度重置)、`--add_size_threshold`(默认 False,致密化时的尺寸阈值开关)。三者都与衰减策略如何与训练策略配合有关,第 6 单元会展开。

**训练开头的分支**。[train.py:57-63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L63) 在 `training()` 一开始就根据开关决定是否实例化 `Coefficient()`(即 \( f_\theta \) 网络,来自 `module/__init__.py`),并把它作为 `coefficient` 参数传入 `GaussianModel`——这是"网络属于模型"的挂载点。

**一个重要的联动逻辑**。[train.py:473-474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474):只要 `opacity_decay` 开启,就把 `densify_until_iter` 改写为 `iterations`(总迭代数),即**致密化贯穿整个训练过程**而不是像 3DGS 那样只在前段进行。这是"衰减策略改变训练策略"的第一个证据,细节留到第 6 单元。

**值得注意的细节**:`--opacity_decay` 的定义是 `action="store_true", default=True`。`store_true` 意味着命令行里**无法**通过 `--opacity_decay false` 把它关掉(传不传这个 flag 它都是 True);真正能关闭它的途径是在 yaml 配置文件里写 `opacity_decay: false`,因为 [train.py:434-443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443) 的 `recursive_merge` 会用 yaml 的值覆盖 `args` 中的同名属性。做消融实验时这一点非常关键。

#### 4.2.4 代码实践

1. **实践目标**:通过 `--help` 输出确认参数注册情况,并把 opacity decay 参数族整理成表。
2. **操作步骤**:
   - 运行 `python train.py --help`(需要先装好依赖;若环境未配置,直接阅读 [train.py:376-431](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L376-L431) 的参数注册代码也能完成本实践);
   - 在 `--help` 输出中找到 `--opacity_decay`、`--f_min`、`--f_max`、`--decay_from_iter`、`--time_aware` 五个参数,抄下它们的默认值;
   - 打开任一 `configs/dynerf/*.yaml`(如 `flame_steak.yaml`),检查里面是否覆盖了这些键。
3. **需要观察的现象**:`--help` 输出中这些参数的 default 值与源码中 `default=` 是否一致;yaml 中出现的键是否都能在 `--help` 里找到(`recursive_merge` 里有 `assert hasattr(args, key)` 强制了这一点)。
4. **预期结果**:得到一张"参数名 / 默认值 / 含义"三列小表,并确认 `--f_min` 默认 0.996、`--f_max` 默认 0.998、`--decay_from_iter` 默认 500。若环境中缺少 GPU 或依赖导致命令无法执行,标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**:`--f_min=0.996`、`--f_max=0.998` 说明衰减因子的活动范围有多大?这个设计暗示了什么?

**答案**:因子被限制在 \([0.996, 0.998]\) 这个极窄区间内,单次衰减最多把不透明度乘以 0.998,几乎"不衰减"。这暗示 Neural Decaying Function 的价值不在于大幅压低不透明度,而在于提供一个可微的、逐高斯自适应的调控通道,通过梯度间接引导优化方向。

**练习 2**:为什么命令行无法关闭 `--opacity_decay`?正确的关闭方式是什么?

**答案**:因为它用 `action="store_true"` 且 `default=True`——`store_true` 类参数只能把值设为 True,而默认值已经是 True。正确关闭方式是在 `--config` 指定的 yaml 里写 `opacity_decay: false`,`recursive_merge` 会把它写回 `args`。

**练习 3**:`train.py:473-474` 的联动把 `densify_until_iter` 设成了什么?这说明衰减与哪个训练环节深度耦合?

**答案**:设成 `args.iterations`(总迭代数),即开启衰减后致密化(densification)会持续到训练结束。这说明 Neural Decaying Function 与自适应致密化环节深度耦合——衰减持续"压制"旧高斯,致密化持续补充新高斯,两者配合完成几何的持续细化。

### 4.3 gaussian_renderer.render:衰减在哪里接入渲染

#### 4.3.1 概念说明

`gaussian_renderer/__init__.py` 中的 `render()` 是训练循环每一步都要调用的函数:输入一个相机视角和 4D 高斯模型,输出渲染图像及一批训练所需的中间量。**Neural Decaying Function 的"作用点"就在这个函数里**——在把不透明度送进 CUDA 光栅化器之前,先对它做一次衰减。这是"论文思想落到代码"的最直接位置。

#### 4.3.2 核心流程

`render()` 的一次调用可以概括为:

```text
1. 构造 GaussianRasterizationSettings(视场角、视图/投影矩阵、时间戳等)
2. 从高斯模型读取属性:means3D / opacity / scales / rotations / ts ...
3. 【opacity decay 接入点】
   若 args.opacity_decay 且 iteration > decay_from_iter:
     - time_aware 时:空间可见性(markVisible) AND 时间可见性(marginal_t > 0.05)
     - 调用 pc.opacity_decay(f_min, f_max, mask=visibility) 得到衰减后的 opacity
4. (可选)Python 回退路径:手动算协方差 / 球谐颜色
5. 调用 CUDA 光栅化器 rasterizer(...) 得到图像、radii、depth、alpha 等
6. 返回 {"render", "viewspace_points", "visibility_filter", "radii", ...}
```

衰减的数学形式(\( f_\theta \) 即 `Coefficient` 网络,`mode='net'` 为默认模式):

\[
\alpha' \;=\; \alpha \times \Big( f_{\min} + (f_{\max} - f_{\min}) \cdot f_\theta(\alpha, \mathbf{x}, \mathbf{s}) \Big)
\]

其中 \( \alpha \) 是高斯当前的不透明度,\( \mathbf{x} \)、\( \mathbf{s} \) 是该高斯的位置与尺度属性(详见第 6 单元)。由于 \( f_\theta \) 末层是 Sigmoid,输出落在 \( (0,1) \),整个括号因此被线性映射到 \([f_{\min}, f_{\max}]\)。

#### 4.3.3 源码精读

**衰减的触发条件与可见性掩码**。[gaussian_renderer/__init__.py:63-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L75) 是本讲最重要的代码段,逐行拆解:

- 第 64 行:三个条件同时满足才衰减——`args` 存在、`args.opacity_decay` 为真、当前迭代数大于 `decay_from_iter`(延迟启用);
- 第 66 行:`rasterizer.markVisible(means3D)` 判断每个高斯是否落在当前相机**视锥内**(空间可见性);
- 第 67 行:`pc.get_marginal_t(timestamp) > 0.05` 判断每个高斯在**当前时刻**是否还有足够的时间响应(时间可见性,来自 4D 高斯的时间边缘化,第 3 单元展开);
- 第 68 行:两者按位与——只衰减"当前视角此刻真正看得见"的高斯;
- 第 74 行:调用 `pc.opacity_decay(f_min=args.f_min, f_max=args.f_max, mask=visibility)`,用返回值**替换**即将送入光栅化器的 `opacity`。

**衰减真正发生的地方**。[scene/gaussian_model.py:584-620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584-L620) 的 `opacity_decay()` 方法实现了完整的模式族(`const` / `exp_asc` / `exp_desc` / `power_asc` / `power_desc` / `mlp` / `net`),本讲只需认识默认模式 `net` 的那一行:[scene/gaussian_model.py:605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L605) 把 `old_opacity` 乘上括号里的 \( f_{\min} + (f_{\max}-f_{\min}) f_\theta(\cdot) \),\( f_\theta \) 的输入是 `self.get_xyzt`(4D 位置)与 `self.get_scaling_xyzt`(4D 尺度);第 616-617 行处理 `mask` 选择性衰减;第 619 行用 `inverse_opacity_activation` 把衰减结果**原位写回** `_opacity.data`(绕过 autograd 的设计意图第 6 单元再讨论)。

**\( f_\theta \) 网络的真身**。[module/__init__.py:4-23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L4-L23) 定义了 `Coefficient` 类:一个 `Linear → ReLU → Dropout → Linear → Sigmoid` 的两层 MLP,输入维度由 `_calculate_input_dim` 计算(基础 7 维 = opacity + 位置 + 尺度,`use_4d_features=True` 时再加 2 维 4D 特征,共 9 维;见 [module/__init__.py:25-31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L25-L31))。整个网络只有一个隐藏层(默认 32 维),确实是"轻量神经网络"。

**训练循环如何调用 render**。[train.py:138-148](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138-L148) 展示了 `render()` 的消费方:每次迭代、batch 中每个视角调用一次 `render(...)`,取出图像算 L1 与 SSIM 损失后 `loss.backward()`。反传的梯度既流向 4D 高斯属性,也流向 \( f_\theta \) 的参数——这正是 README 所说"联合优化"的代码体现(两个优化器分别在 [train.py:260-265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L260-L265) 交替 step)。

#### 4.3.4 代码实践

1. **实践目标**:不改任何代码,纯靠阅读走通"从训练循环到 CUDA 光栅化器"的 opacity 传递路径。
2. **操作步骤**:
   - 在编辑器中打开三个文件:`train.py`、`gaussian_renderer/__init__.py`、`scene/gaussian_model.py`;
   - 从 [train.py:138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138) 的 `render(viewpoint_cam, gaussians, pipe, background, args=args, iteration=iteration)` 出发;
   - 跳到 `render()` 内部的 [gaussian_renderer/__init__.py:61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L61)(`opacity = pc.get_opacity`,原始不透明度)→ 第 64 行(触发判断)→ 第 74 行(调用 `pc.opacity_decay`)→ [scene/gaussian_model.py:605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L605)(`Coefficient` 前向)→ 回到 [gaussian_renderer/__init__.py:160-172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160-L172)(衰减后的 `opacity` 作为 `opacities` 传入 `rasterizer`);
   - 把这条链路画成一张 5 个节点的箭头图。
3. **需要观察的现象**:衰减发生在"读取属性之后、进入光栅化器之前";掩码 `visibility` 只影响衰减的对象,不影响哪些高斯参与渲染。
4. **预期结果**:得到一条类似 `train.py:138 → render():61 → render():64-74 → gaussian_model.py:605 → render():160` 的调用链草图,并能用一句话向别人解释"opacity 在哪一步被乘上了衰减因子"。

#### 4.3.5 小练习与答案

**练习 1**:`render()` 中 `space_visibility & time_visibility` 这个按位与的语义是什么?为什么默认只衰减可见高斯?

**答案**:`space_visibility` 来自 `markVisible`(高斯在当前相机视锥内),`time_visibility` 来自 `get_marginal_t > 0.05`(高斯在当前时刻有时间响应)。相与后得到"此视角此刻真正看得见"的高斯集合。只对它们衰减,是为了让衰减的作用(及其梯度)集中在当前正在被渲染监督的高斯上,避免对无关高斯做无意义的扰动。

**练习 2**:`opacity_decay()` 有 7 种模式,默认的 `mode='net'` 与 `mode='mlp'` 有什么区别?

**答案**:两者都调用 `self.coefficient` 网络,区别在输入——`mlp` 模式只把 `old_opacity` 一个标量喂给网络([scene/gaussian_model.py:601](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L601)),`net` 模式还额外输入 `get_xyzt`(4D 位置)与 `get_scaling_xyzt`(4D 尺度)([scene/gaussian_model.py:605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L605))。即 `net` 的衰减因子是"属性感知"的,能对不同位置/形状的高斯给出不同的衰减策略。

**练习 3**:`render()` 返回的字典里,哪一项与 opacity 的关系最直接?训练循环用它做什么?

**答案**:`"render"`(渲染图像)最直接——它就是衰减后的 opacity 参与光栅化得到的图像,训练循环拿它与真值图算 L1/SSIM 损失并反传([train.py:143-148](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L143-L148)),梯度因此同时回传到 4D 高斯属性与 \( f_\theta \),实现联合优化。

## 5. 综合实践

本讲的综合实践是 spec 规定的任务:**用 grep 找出仓库中所有包含 `opacity_decay` 的文件并列表,然后写一段 200 字摘要说明 4C4D 与 3DGS/4DGS 的关系**。

1. **实践目标**:验证"Neural Decaying Function 集中实现在三个文件中"这一结论,并锻炼用一段话概括项目定位的能力。
2. **操作步骤**:
   - 在仓库根目录执行(推荐 `git grep`,只会搜索 git 跟踪的文件,自动排除本讲义目录):

     ```bash
     git grep -n "opacity_decay"
     ```

     若想用普通 grep,注意排除 `4C4D-tutorial/` 目录以免被讲义自身干扰:

     ```bash
     grep -rn "opacity_decay" --exclude-dir=4C4D-tutorial --exclude-dir=.git .
     ```

   - 把输出整理成"文件 / 行号 / 该行在做什么"的表格;
   - 通读 README 后,写一段约 200 字的中文摘要,说明 4C4D 与 3DGS/4DGS 的关系。
3. **需要观察的现象**:搜索结果应只落在 3 个源码文件中,共 8 处(以当前 HEAD `ed6a3cb` 为准):

   | 文件 | 行号 | 该行在做什么 |
   |---|---|---|
   | `gaussian_renderer/__init__.py` | 64 | `render()` 中判断是否触发衰减(开关 + 迭代数) |
   | `gaussian_renderer/__init__.py` | 74 | 调用 `pc.opacity_decay(...)` 得到衰减后 opacity |
   | `scene/gaussian_model.py` | 584 | `opacity_decay()` 方法定义(模式族实现) |
   | `train.py` | 57 | `training()` 开头:开关开启则创建 `Coefficient` 网络 |
   | `train.py` | 248 | 致密化分支:开启衰减时把 `size_threshold` 置 None |
   | `train.py` | 254 | 开启衰减时跳过周期性 `reset_opacity` |
   | `train.py` | 412 | 注册 `--opacity_decay` 命令行参数(默认 True) |
   | `train.py` | 473 | 联动:开启衰减则 `densify_until_iter = iterations` |
4. **预期结果**:你会发现 `opacity_decay` 的分布本身就是一张"思想地图"——**定义**在 `gaussian_model.py`,**参数与训练策略联动**在 `train.py`,**接入渲染**在 `gaussian_renderer/__init__.py`。这正是本讲三个最小模块的来源。摘要应覆盖:4C4D 继承 4DGS 的训练框架与 4D 高斯表示,针对"4 台相机的极稀疏视角"这一新条件,提出作用于高斯不透明度的 Neural Decaying Function,与 MASt3R 稠密初始化配合,缓解稀疏设定下几何学习难于外观学习的失衡。

## 6. 本讲小结

- 4C4D = 4 Camera 4D Gaussian Splatting:输入 4 台相机的视频帧 + COLMAP 格式参数与点云,输出可渲染任意视角/时刻的 4D 高斯场景。
- 论文核心洞察:极稀疏视角下**几何学习远比外观学习困难**,二者失衡;对策是作用于不透明度的 Neural Decaying Function \( f_\theta \)。
- \( f_\theta \) 是一个两层 MLP(`module/__init__.py` 的 `Coefficient`),输出经线性映射限制在 \([f_{\min}, f_{\max}]\)(默认 \([0.996, 0.998]\)),与 4D 高斯在光度损失下联合优化。
- 代码三要点:`train.py:411-419` 注册参数族(默认开启、延迟到第 500 次迭代启用);`gaussian_renderer/__init__.py:63-75` 在渲染前对"空间且时间可见"的高斯做选择性衰减;`scene/gaussian_model.py:584-620` 实现 7 种衰减模式(默认 `net`)。
- 衰减与训练策略深度联动:开启后致密化贯穿全程(`train.py:473-474`)、禁用 opacity 周期重置(`train.py:254`)。
- 代码库建立在 4DGS 之上,其中 gaussian-splatting 部分遵循 Inria 非商业研究许可。

## 7. 下一步学习建议

- 下一讲(**u1-l2 环境搭建与四个 CUDA 子包**):了解 `diff-gaussian-rasterization`、`simple-knn`、`pointops2`、`fused-ssim` 四个本地 CUDA 扩展各自提供什么接口,把环境搭起来。
- 若你想先睹为快核心创新,可直接精读 `module/__init__.py`(仅 49 行)与 `scene/gaussian_model.py` 的 `opacity_decay()`,但完整的梯度分析要到第 6 单元。
- 若你更想先掌握数据从哪来,可以跳到第 2 单元(**u2-l1 COLMAP 数据格式与 colmap_loader**),那也是理解本项目输入约定的起点。
