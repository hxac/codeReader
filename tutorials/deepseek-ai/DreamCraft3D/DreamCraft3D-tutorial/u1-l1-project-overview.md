# 项目概览：从一张参考图到三维资产

> 本讲是 DreamCraft3D 学习手册的第一讲。我们不写任何训练代码，只做一件事：把「这个项目到底是什么、它分几步干活、论文里的概念对应仓库里哪些文件」彻底搞清楚。学完本讲，你手里会有一张自己画的流程图和一张「阶段 → 配置文件」对照表，它们将贯穿整本手册。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 DreamCraft3D 的**输入**（一张 RGBA 参考图 + 一段文本提示词）和**输出**（一个带纹理、可导出为 obj 的三维资产），以及「几何雕刻（geometry sculpting）+ 纹理提升（texture boosting）」的两阶段划分。
2. 理解两个核心动机：
   - 为什么粗阶段要用**视图相关的扩散先验**（view-dependent diffusion model，即 Zero123）来保证多面一致性；
   - 为什么纹理阶段要提出**自举得分蒸馏 BSD**（Bootstrapped Score Distillation），让扩散模型和三维场景**交替优化、互相促进**。
3. 建立「论文概念 ↔ 代码模块」的对应关系：NeRF/NeuS、Zero123、DMTet、DreamBooth/BSD 分别落在 `configs/` 目录下的哪份配置、`threestudio/` 下的哪个子包。

## 2. 前置知识

本讲不需要你写过扩散模型代码，但下面几个名词会反复出现，我们用最通俗的方式先建立直觉。

### 2.1 三维物体的三种「存法」

DreamCraft3D 在不同阶段用不同方式表示三维物体，这是一个「由粗到细、由软到硬」的过程：

| 表示方式 | 通俗理解 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **NeRF（神经辐射场）** | 用一个神经网络记住「空间中每个点的密度和颜色」，渲染时沿视线积分 | 表达能力强、易优化 | 密度场是"雾状"的，几何不干净 |
| **SDF / NeuS（符号距离场）** | 网络输出「每个点到物体表面的距离」，距离为 0 的等值面就是几何 | 表面光滑、精确 | 训练更难，需要 Eikonal 等正则 |
| **DMTet（可变形四面体网格）** | 在一堆四面体里做 marching tetrahedra，直接抽出**显式三角网格**，网格顶点还可微调 | 得到真正的 mesh，可用光栅化高效渲染、可直接导出 | 依赖前序阶段提供好的初始化 |

一句话：**先在"软"的体积场里把形状捏出来，再转成"硬"的网格去精修几何和纹理。**

### 2.2 扩散模型与「得分蒸馏」

扩散模型（如 Stable Diffusion、DeepFloyd IF）的训练目标是学会「给一张加噪的图去掉噪声」。所谓**得分蒸馏（Score Distillation Sampling, SDS）**，就是借用这个去噪能力来优化另一个对象（这里是三维场景）：把当前三维场景渲染出来的图加噪，喂给扩散模型，看它预测的噪声和真实加的噪声差多少，把这个差异作为梯度反传回三维场景。直觉公式：

\[ \nabla_x \mathcal{L}_{\mathrm{SDS}} = w(t)\,\bigl(\epsilon_\phi(x_t; t, y) - \epsilon\bigr) \]

其中 \(x_t\) 是渲染图加噪后的结果，\(\epsilon_\phi(x_t; t, y)\) 是扩散模型预测的噪声，\(y\) 是条件（文本或参考图），\(w(t)\) 是和时间步 \(t\) 相关的权重。你不需要现在推导它，只需记住：**SDS 的本质是"让渲染图看起来更像扩散模型眼中的真实图片"**。

### 2.3 DreamBooth 与 LoRA

- **DreamBooth**：拿几张特定物体的照片微调（个性化）一个文生图模型，让模型学会"这个特定物体长什么样"，之后用专属触发词（如 `a sks mushroom`）就能生成它。
- **LoRA**：一种轻量微调技术，不改动原模型权重，只训练插入的小矩阵，因此一个模型可以挂载/卸载不同的 LoRA。DreamCraft3D 的 BSD 阶段大量使用 LoRA。

### 2.4 Janus 问题（多面问题）

纯文本驱动的 3D 生成常见病：模型从每个视角看都想生成"正面"，于是长出多个脑袋/多个正面，像罗马双面神 Janus。DreamCraft3D 用「视图相关的扩散先验（Zero123）+ 可选的 DreamBooth 个性化」来对症下药。

## 3. 本讲源码地图

本讲的核心阅读对象是 `README.md`，四份配置文件作为对照材料。下表是它们在本讲中的角色：

| 文件 / 目录 | 作用 | 本讲怎么看 |
| --- | --- | --- |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | 项目说明书：摘要、安装、四阶段训练命令、导出命令 | **逐段精读** |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 阶段 1a：NeRF 粗几何 | 只看 `system_type` / `geometry_type` / `renderer_type` / `guidance_type` 四个键 |
| [configs/dreamcraft3d-coarse-neus.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml) | 阶段 1b：NeuS 精细几何 | 同上 |
| [configs/dreamcraft3d-geometry.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml) | 阶段 2：DMTet 几何精修 | 同上，另注意 `geometry_convert_from` |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | 阶段 3：BSD 纹理提升 | 同上，另注意 `stable-diffusion-bsd-guidance` 与双 LoRA 优化器 |
| `preprocess_image.py` / `launch.py` / `threestudio/` / `extern/` / `load/` | 预处理入口 / 训练入口 / 核心包 / 外部依赖 / 权重目录 | 本讲只需知道存在，后续讲义逐一深入 |

## 4. 核心概念与源码讲解

本讲把 README 拆成三个最小模块来读：**项目定位与分层生成**、**两大核心技术**、**四个阶段与配置映射**。

### 4.1 DreamCraft3D 是什么：分层生成的 3D 资产工厂

#### 4.1.1 概念说明

DreamCraft3D 是 deepseek-ai 开源的**层次化 3D 内容生成**系统：输入**一张参考图**（去掉背景的 RGBA 图）和**一段文本描述**，输出**高保真、多视角一致的三维物体**（最终可导出为带纹理的 obj 网格）。

它要解决的核心矛盾是：现有方法很难同时做到「**多面一致**」（从各个角度看都是同一个物体，不长出多余脑袋）和「**纹理逼真**」（渲染质量接近真实照片）。DreamCraft3D 的答案是**分层（hierarchical）**：

- **几何雕刻阶段**：优先保证几何一致性，此时纹理保真度可以妥协；
- **纹理提升阶段**：冻结/继承已雕刻好的几何，专门用 BSD 提升纹理到照片级。

这就像先捏泥塑定型，再上色精修——两个阶段的目标函数不同，所以用不同的模型、不同的损失、不同的配置文件。

#### 4.1.2 核心流程

从一张图片到三维资产的整体流水线（对应 README 的 Quickstart 部分）：

```text
输入图片 (png)
   │  python preprocess_image.py image.png --recenter
   ▼
RGBA 参考图 + 深度图 + 法向图      ← carvekit 去背景、BLIP2 生成提示词、Omnidata 估计深度/法向
   │
   ▼
┌─────────────── 几何雕刻（Stage 1）───────────────┐
│ 1a. coarse-nerf ：NeRF 体积，先把大致形状捏出来      │
│ 1b. coarse-neus ：换成 SDF 表示，几何变得更干净      │
└──────────────────────────────────────────────────┘
   │  ckpt 通过 system.weights 传给下一阶段
   ▼
┌─────────────── 几何精修（Stage 2）────────────────┐
│ geometry ：DMTet 显式网格 + 可微光栅化，细节雕刻     │
└──────────────────────────────────────────────────┘
   │  ckpt 通过 system.geometry_convert_from 传入
   ▼
┌─────────────── 纹理提升（Stage 3）────────────────┐
│ texture ：冻结几何，BSD 自举扩散先验，纹理冲到照片级 │
└──────────────────────────────────────────────────┘
   │  python launch.py --export ... system.exporter_type=mesh-exporter
   ▼
带纹理的 obj + mtl 网格文件
```

注意两个细节（后续讲义会反复出现）：

- 阶段 1a → 1b 用 `system.weights` 传递检查点（整个系统权重加载）；
- 阶段 1b → 2、阶段 2 → 3 用 `system.geometry_convert_from` 传递（把上一阶段的几何**转换**成下一阶段的表示，例如从 NeuS 的 SDF 采样出 DMTet 网格）。

#### 4.1.3 源码精读

**① 项目自我定位与摘要**。[README.md:L7-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L7-L21) 给出了论文标题 "Hierarchical 3D Generation with Bootstrapped Diffusion Prior"（层次化 3D 生成 + 自举扩散先验）和完整摘要。摘要里有三句关键的话，直接对应本讲三个学习目标：

- "leveraging a 2D reference image to guide the stages of **geometry sculpting and texture boosting**" —— 两阶段划分；
- "score distillation sampling via a **view-dependent diffusion model**" —— 粗阶段用视图相关先验保一致性；
- "**Bootstrapped Score Distillation** ... **alternating optimization** of the diffusion prior and 3D scene representation" —— 纹理阶段的 BSD 交替优化。

**② Quickstart：四条训练命令**。[README.md:L102-L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L102-L126) 是全仓库最重要的 25 行。第一条命令（[L113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L113)）：

```sh
python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
  system.prompt_processor.prompt="$prompt" data.image_path="$image_path"
```

这条命令透露了项目的运行方式：入口是 `launch.py`，一切行为由 `--config` 指定的 yaml 决定，且可以用 `a.b.c=value` 的点号语法在命令行覆盖配置中的任意字段（这里覆盖了提示词和输入图路径）。随后三条命令（[L115-L116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L115-L116)、[L119-L120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L119-L120)、[L124-L125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L124-L125)）分别启动 coarse-neus、geometry、texture 阶段，并且**每一条都把上一阶段 `outputs/.../ckpts/last.ckpt` 接力传入**，这就是 4.1.2 流程图中「ckpt 接力」的出处。

**③ 预处理与权重准备**。[README.md:L103-L106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L103-L106) 说明输入图要先经过 `preprocess_image.py` 去背景并估计深度/法向；[README.md:L88-L100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L88-L100) 列出需要下载的两个预训练模型：`stable-zero123.ckpt`（视图条件扩散先验）和 Omnidata 的深度/法向权重（预处理用）。环境安装细节属于下一讲，这里只需建立「训练前要先备好两份权重」的印象。

#### 4.1.4 代码实践

**实践：亲眼确认「一个项目 = 四份配置」**

1. **实践目标**：不靠记忆，通过命令行亲自确认仓库的四份阶段配置和它们声明的组件类型。
2. **操作步骤**（在仓库根目录执行，无需 GPU）：

   ```sh
   # 1. 列出所有阶段配置
   ls configs/

   # 2. 提取每份配置的关键组件声明（阶段、几何、渲染器、引导）
   grep -n "stage:\|geometry_type\|renderer_type\|guidance_type\|system_type" configs/dreamcraft3d-*.yaml
   ```

3. **需要观察的现象**：第 2 条命令会按 `文件名:行号:内容` 的格式输出约十几行，四个文件都出现同一个 `system_type: "dreamcraft3d-system"`，但 `geometry_type` / `renderer_type` / `guidance_type` 各不相同。
4. **预期结果**：你会看到 coarse-nerf 是 `implicit-volume` + `nerf-volume-renderer`，coarse-neus 换成 `implicit-sdf` + `neus-volume-renderer`，geometry 和 texture 都是 `tetrahedra-sdf-grid` + `nvdiff-rasterizer`，而 texture 的 `guidance_type` 独一份地变成了 `stable-diffusion-bsd-guidance`。这正是 4.3 节要完整梳理的映射。
5. 本实践只读文件、不启动训练，任何机器都可执行。

#### 4.1.5 小练习与答案

**练习 1**：DreamCraft3D 的输入除了 RGBA 参考图还需要什么？它从哪里来？
**答案**：还需要一段文本提示词（`$prompt`）。它既可以在命令行通过 `system.prompt_processor.prompt=` 手工指定，也可以在预处理时由 BLIP2 模型对输入图自动生成（见 [README.md:L103-L106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L103-L106) 所指的 `preprocess_image.py`，第二讲会精读）。

**练习 2**：阶段 1a（coarse-nerf）和阶段 2（geometry）之间，检查点分别通过什么配置键传递？有什么区别？
**答案**：coarse-nerf → coarse-neus 用 `system.weights`（[README.md:L116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L116)），是整系统权重加载，两阶段同属 `implicit` 体积/SDF 家族；coarse-neus → geometry、geometry → texture 用 `system.geometry_convert_from`（[README.md:L120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L120)、[L125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L125)），因为要**跨越表示形式**（从 SDF 场转成 DMTet 网格），需要走几何转换逻辑而非简单的权重恢复。

**练习 3**：为什么 DreamCraft3D 不用一个模型、一份配置端到端完成，而要拆成多个阶段？
**答案**：因为「几何一致」和「纹理逼真」两个目标对表示形式和先验的要求冲突：几何阶段需要易于优化的隐式场 + 强 3D 先验（Zero123），可以容忍低分辨率和模糊纹理；纹理阶段需要高分辨率显式网格 + 个性化扩散先验（BSD），且要冻结几何防止破坏已雕好的形状。分层让每阶段用最合适的工具，这正是论文标题中 "Hierarchical" 的含义。

### 4.2 两大核心技术：视图相关扩散先验与自举得分蒸馏 BSD

#### 4.2.1 概念说明

**技术一：视图相关扩散先验（粗阶段保一致性）。**
普通文生图模型（如 DeepFloyd IF）只知道"这个提示词的图长什么样"，不知道当前渲染的是哪个视角，容易诱发 Janus 问题。DreamCraft3D 在粗阶段额外引入 **Zero123**：一个以「参考图 + 相对相机姿态变化（方位角/仰角）」为条件的扩散模型。渲染某个视角时，Zero123 知道"这是从参考视角转过多少度看到的样子"，从而提供**视角感知**的得分蒸馏，把各视角往同一个物体上拉。所以粗阶段是**双引导**：`deep-floyd-guidance`（文本先验）+ `stable-zero123-guidance`（3D 视图先验），两者按 `freq` 调度交替施加。

**技术二：自举得分蒸馏 BSD（纹理阶段冲保真度）。**
视图相关先验保证了几何一致，却"compromises the texture fidelity"（摘要原话，[README.md:L14-L15](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L14-L15)）——通用扩散模型没见过**这个特定物体**，给出的纹理监督天然不准。BSD 的思路分三步：

1. **自产数据**：拿当前三维场景的多视角渲染图当作"这个物体的照片"；
2. **个性化**：用 DreamBooth/LoRA 把一个 Stable Diffusion 微调成"专属于这个物体"的扩散模型；
3. **回灌**：再用这个私有模型做得分蒸馏，反过来优化三维场景的纹理。

关键在"**自举（bootstrapped）**"和"**交替（alternating）**"：优化后的三维场景渲染质量更高 → 训练出的私有扩散模型更懂这个物体 → 蒸馏出的纹理指导更准 → 场景又变得更好……两个优化目标互相喂饭、螺旋上升，这是它区别于普通 VSD/SDS 的本质。

#### 4.2.2 核心流程

BSD 交替优化的循环（texture 阶段每个训练步都在做类似决策）：

```text
                 ┌──────────────────────────────────┐
                 │  三维场景（DMTet 网格 + 纹理场）    │
                 └───────────────┬──────────────────┘
                        渲染多视角 │ ①
                                 ▼
                 ┌──────────────────────────────────┐
                 │  渲染图作为"该物体的训练照片"        │
                 └───────────────┬──────────────────┘
                      DreamBooth │ ② 用渲染图训练 LoRA（train_lora）
                                 ▼
                 ┌──────────────────────────────────┐
                 │  个性化扩散模型（越来越懂这个物体）   │
                 └───────────────┬──────────────────┘
                     得分蒸馏    │ ③ 用它计算 VSD 梯度（compute_grad_vsd）
                                 ▼
                     反传更新三维场景纹理 ──→ 回到 ①
```

配套还有一个防遗忘机制：**预训练步**。用冻结的原始扩散模型对渲染图加噪-去噪再回灌（`train_pretrain`），防止 LoRA 个性化越训越偏、丢掉通用图像先验。这一切的源码级精读在单元七（u7-l4、u7-l5），本讲先在配置层确认它们的存在。

#### 4.2.3 源码精读

**① BSD 的配置证据**。纹理阶段配置里，引导模型换成了 BSD 专用实现：[configs/dreamcraft3d-texture.yaml:L78-L86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L78-L86) 声明 `guidance_type: "stable-diffusion-bsd-guidance"`，并加载**两份** Stable Diffusion（`pretrained_model_name_or_path` 与 `pretrained_model_name_or_path_lora`），还带一个 `only_pretrain_step: 1000`——前 1000 步只做预训练回灌，先把 LoRA 拉到正确起点。

**② 交替优化的损失证据**。同一文件 [L123-L127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L123-L127) 出现三个普通 SDS 配置里没有的损失键：`lambda_sd`（蒸馏梯度）、`lambda_lora`（LoRA 个性化损失）、`lambda_pretrain`（预训练回灌损失）；优化器部分 [L149-L152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L149-L152) 同时给 `guidance.train_unet` 与 `guidance.train_unet_lora` 两组参数设置了学习率——**三维场景和扩散模型被放进同一个优化循环**，这就是"交替优化"落在配置上的铁证。

**③ 几何被冻结的证据**。纹理阶段 [L59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59) 明确写着 `fix_geometry: true`，几何不再参与训练，本阶段的一切梯度都服务于纹理。

**④ 粗阶段双引导的证据**。回到 [configs/dreamcraft3d-coarse-nerf.yaml:L94-L111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L94-L111)：`guidance_type: "deep-floyd-guidance"`（文本先验，DeepFloyd IF-I-XL）与 `guidance_3d_type: "stable-zero123-guidance"`（加载 `./load/zero123/stable_zero123.ckpt`，并把参考图路径、参考相机姿态作为条件）并存；[L113-L118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L113-L118) 的 `freq` 块里 `n_ref: 2`、`ref_or_guidance: "alternate"` 规定了"每 2 步参考图监督、其余步做扩散引导"的交替节奏。

**⑤ 可选的 DreamBooth 路线**。README 折叠块 [L128-L166](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L128-L166) 给出缓解 Janus 的另一条路：先用 Zero123++ 从单图生成多视图（`threestudio/scripts/img_to_mv.py`），再 DreamBooth 训练一个个性化 DeepFloyd LoRA（`threestudio/scripts/train_dreambooth_lora.py`），最后通过 `system.guidance.lora_weights_path=...` 挂回粗阶段。这可以看作 BSD"个性化先验"思想在粗阶段的手动版。

#### 4.2.4 代码实践

**实践：在配置里「数出」BSD 的三个组成部分**

1. **实践目标**：不看论文、只看仓库，用 grep 找出纹理阶段「蒸馏 + LoRA + 预训练」三股力量的全部配置落点。
2. **操作步骤**：

   ```sh
   # BSD 相关的损失权重
   grep -n "lambda_sd\|lambda_lora\|lambda_pretrain" configs/dreamcraft3d-texture.yaml

   # BSD 相关的优化器参数组
   grep -n "train_unet" configs/dreamcraft3d-texture.yaml

   # 预训练步数与两份基础模型
   grep -n "only_pretrain_step\|pretrained_model_name_or_path" configs/dreamcraft3d-texture.yaml
   ```

3. **需要观察的现象**：三条命令分别命中 `loss` 段、`optimizer.params` 段和 `guidance` 段。
4. **预期结果**：与 4.2.3 中引用的行号一一对应——`lambda_lora: 0.1`、`lambda_pretrain: 0.1` 出现在 [L125-L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L125-L126)，`train_unet` / `train_unet_lora` 出现在 [L149-L152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L149-L152)。对照组：把第一条命令里的文件换成 `configs/dreamcraft3d-coarse-nerf.yaml`，`lambda_lora` 与 `lambda_pretrain` 将**一无所获**——这两个概念只属于纹理阶段。
5. 本实践同样只需读文件。若想真正跑通 BSD 训练验证行为，需要约 20GB 显存的 NVIDIA GPU，属后续讲义内容，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么粗阶段用 Zero123 而不是继续加大文本引导的权重来保证多面一致？
**答案**：文本引导是"视角盲"的——模型不知道当前渲染的是背面还是正面，加大权重只会更用力地把每个视角都拉向"典型正面"，反而加剧 Janus。Zero123 以「参考图 + 相对姿态」为条件，天然视角感知，能对每个视角给出该视角的正确监督。

**练习 2**：BSD 里"自举"二字体现在哪里？如果去掉 DreamBooth 个性化、直接用原始 Stable Diffusion 做蒸馏会怎样？
**答案**：自举体现在闭环：三维场景的渲染图是训练扩散模型的数据，而扩散模型又是优化三维场景的老师，两者互相抬高。若去掉个性化，蒸馏信号来自一个从未见过该物体的通用模型，其对纹理细节的判断不准确，这正是摘要所说视图相关先验 "compromises the texture fidelity" 的原因，纹理无法冲到照片级。

**练习 3**：`only_pretrain_step: 1000` 大概率是做什么用的？
**答案**：训练最初阶段先不追求纹理提升，而是用冻结的原始扩散模型对早期（还很粗糙的）渲染图做加噪-去噪回灌，先把 LoRA 参数从随机/初始状态拉到合理位置、注入通用先验，1000 步后再进入完整的交替优化。确切行为要到 u7-l5 精读 `stable_diffusion_bsd_guidance.py` 时确认。

### 4.3 四个训练阶段与四份配置文件的映射

#### 4.3.1 概念说明

DreamCraft3D 的「多阶段训练」在工程上就是**四个 yaml 配置文件**，每个文件声明该阶段的几何表示、渲染器、引导模型、损失权重等。读懂这份映射，就等于拿到了全仓库的索引：配置里每个 `*_type` 字符串（如 `implicit-volume`）都是 `threestudio/` 包里某个注册类的名字，后续讲义顺着名字就能找到源码。

论文概念与代码模块的对应关系：

| 论文概念 | 配置里的 `*_type` | 源码位置（后续讲义精读） |
| --- | --- | --- |
| NeRF 粗几何 | `implicit-volume` | `threestudio/models/geometry/implicit_volume.py` |
| NeuS 精细几何 | `implicit-sdf` | `threestudio/models/geometry/implicit_sdf.py` |
| DMTet 网格 | `tetrahedra-sdf-grid` | `threestudio/models/geometry/tetrahedra_sdf_grid.py` |
| 体渲染 | `nerf-volume-renderer` / `neus-volume-renderer` | `threestudio/models/renderers/` |
| 可微光栅化 | `nvdiff-rasterizer` | `threestudio/models/renderers/nvdiff_rasterizer.py` |
| 文本先验 SDS | `deep-floyd-guidance` | `threestudio/models/guidance/deep_floyd_guidance.py` |
| 视图相关先验（Zero123） | `stable-zero123-guidance` | `threestudio/models/guidance/stable_zero123_guidance.py` |
| BSD 纹理先验 | `stable-diffusion-bsd-guidance` | `threestudio/models/guidance/stable_diffusion_bsd_guidance.py` |
| 训练系统（把上面拼起来） | `dreamcraft3d-system` | `threestudio/systems/dreamcraft3d.py` |

#### 4.3.2 核心流程

四个阶段按「表示由软到硬、分辨率由低到高、先验由通用到个性化」演进：

| 阶段 | 配置文件 | 几何表示 | 渲染器 | 引导先验 | 训练分辨率 | ckpt 衔接 |
| --- | --- | --- | --- | --- | --- | --- |
| 1a 粗几何 | `dreamcraft3d-coarse-nerf.yaml` | `implicit-volume`（NeRF） | `nerf-volume-renderer` | DeepFloyd SDS + Zero123 | 128 → 384（渐进） | 起点 |
| 1b 精几何 | `dreamcraft3d-coarse-neus.yaml` | `implicit-sdf`（NeuS） | `neus-volume-renderer` | DeepFloyd SDS + Zero123 | 256 | `system.weights` ← 1a |
| 2 几何精修 | `dreamcraft3d-geometry.yaml` | `tetrahedra-sdf-grid`（DMTet） | `nvdiff-rasterizer` | DeepFloyd SDS + Zero123 | 1024 | `geometry_convert_from` ← 1b |
| 3 纹理提升 | `dreamcraft3d-texture.yaml` | `tetrahedra-sdf-grid`（冻结） | `nvdiff-rasterizer` | **BSD**（SD + 双 LoRA 交替） | 1024 | `geometry_convert_from` ← 2 |

三条演进线索值得注意：

- **几何线索**：NeRF 密度场 → NeuS SDF → DMTet 显式网格，表面越来越"硬"；
- **渲染线索**：体渲染（沿射线积分，慢但天然可微）→ nvdiffrast 光栅化（快，面向高分辨率），阶段 2 起分辨率直接跳到 1024；
- **先验线索**：通用文本 + 视图条件扩散 → 个性化的 BSD，纹理保真度节节攀升。

#### 4.3.3 源码精读

**① 每份配置的身份声明**。四份 yaml 开头结构完全相同：`name`（试验名）、`data_type: "single-image-datamodule"`（单图数据管线）、`system_type: "dreamcraft3d-system"`（训练系统）。例如 [configs/dreamcraft3d-coarse-nerf.yaml:L41-L44](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L41-L44) 声明了 system 类型、`stage: coarse` 和 `geometry_type: "implicit-volume"`。四份配置只有 `stage` 的值不同（coarse / coarse / geometry / texture），系统代码（`dreamcraft3d.py`）依据它走不同分支。

**② 表示与渲染器的换装**。coarse-neus 换装：[configs/dreamcraft3d-coarse-neus.yaml:L42](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L42) `geometry_type: "implicit-sdf"`，[L68](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L68) `renderer_type: "neus-volume-renderer"`；geometry 阶段再换装：[configs/dreamcraft3d-geometry.yaml:L44-L58](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L44-L58) 出现 `geometry_convert_from: ???`（`???` 表示必须由命令行传入）、`geometry_type: "tetrahedra-sdf-grid"` 和 `renderer_type: "nvdiff-rasterizer"`。

**③ 阶段性格差异**。除了换表示，各阶段的训练策略也不同：coarse-nerf 在 [L9-L22](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L9-L22) 配置了 `[128, 384]` 的渐进分辨率和 `resolution_milestones: [3000]`（3000 步后升分辨率）；geometry 阶段 [L87-L94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L87-L94) 把 `ref_or_guidance` 从 `alternate` 改为 `accumulate`、新增 `n_rgb: 4`；texture 阶段 [L111-L116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L111-L116) 出现独特的 `no_diff_steps: -1`。这些开关的具体含义在单元六（u6-l2）展开，本讲只需感受到"阶段 = 表示 + 渲染 + 引导 + 调度策略"的总装。

**④ 导出成品的入口**。训练完成后，[README.md:L171-L176](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L171-L176) 给出导出命令：用试验目录里自动保存的 `configs/parsed.yaml`（解析后的完整配置）加 `--export` 和 `system.exporter_type=mesh-exporter`，即可产出 obj+mtl 网格。

#### 4.3.4 代码实践

**实践：生成你自己的「四阶段对照表」**

1. **实践目标**：用一条命令机械地提取四份配置的差异字段，亲手填出 4.3.2 的表格，并额外发现一个本讲没讲到的差异。
2. **操作步骤**：

   ```sh
   grep -n "stage:\|geometry_type:\|renderer_type:\|material_type:\|prompt_processor_type:\|guidance_type:\|guidance_3d_type:\|fix_geometry\|geometry_convert_from" \
     configs/dreamcraft3d-coarse-nerf.yaml configs/dreamcraft3d-coarse-neus.yaml \
     configs/dreamcraft3d-geometry.yaml configs/dreamcraft3d-texture.yaml
   ```

   然后打开四份文件各自的 `freq:` 和 `loss:` 段（用 `grep -n -A8 "freq:" configs/dreamcraft3d-*.yaml`）。
3. **需要观察的现象**：相同字段在不同文件中的取值变化；`guidance_3d_type` 在 texture 配置中是否被启用；`loss` 段里各 `lambda_*` 的有无。
4. **预期结果**：除了 4.3.2 表格的内容，你至少还能发现两个本讲未展开的差异，例如 texture 阶段的 `prompt_processor_type` 从 `deep-floyd-prompt-processor` 换成了 `stable-diffusion-prompt-processor`（[configs/dreamcraft3d-texture.yaml:L71-L74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L74)，因为引导模型从 DeepFloyd 换成了 Stable Diffusion，词表必须跟着换），以及 texture 阶段 `guidance_3d` 整段被注释掉（[L88-L98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L88-L98)，纹理阶段不再需要 Zero123）。
5. 把最终表格保存下来，它是你阅读后续所有讲义的"地图"。

#### 4.3.5 小练习与答案

**练习 1**：四份配置的 `system_type` 都是什么？这说明什么？
**答案**：都是 `"dreamcraft3d-system"`。说明四个阶段共用同一套训练系统代码（`threestudio/systems/dreamcraft3d.py`），差异完全由配置驱动——这正是 threestudio 插件化架构的精髓：系统是总装车间，几何/渲染/引导都是可插拔组件。

**练习 2**：`geometry_convert_from: ???` 中的 `???` 是什么意思？
**答案**：这是 OmegaConf 的「必填占位符」语法：该字段没有默认值，必须在命令行显式提供（README 中即 `system.geometry_convert_from="$ckpt"`），漏掉会直接报错。这保证跨阶段转换不会在无意识间被跳过。

**练习 3**：为什么 coarse-nerf 用渐进分辨率 `[128, 384]`，而 geometry/texture 直接用 1024？
**答案**：粗阶段在低分辨率上优化又快又稳（先抓大形状、抑制高频噪声），随训练逐步升到 384；几何/纹理精修阶段已经在 DMTet 显式网格 + 光栅化渲染器上，高分辨率正是精修细节的目的所在。这也和 [README.md:L169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L169) 的显存技巧一致——分辨率是显存开销的第一杠杆。

## 5. 综合实践

**任务：手绘 DreamCraft3D 两阶段流水线图，并完成「阶段 → 配置文件」对照。**（本任务即本讲的验收作业，只需纸笔 + 终端，无需 GPU。）

1. **实践目标**：把本讲三个模块的内容收拢成一张图和一张表，作为后续所有讲义的导航。
2. **操作步骤**：
   1. 重读 [README.md:L12-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L12-L21)（摘要）与 [L102-L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L102-L126)（Quickstart）。
   2. 在纸上画两个大框：左边「几何雕刻」、右边「纹理提升」。按 4.1.2 的文字流程图补齐：输入预处理、NeRF、NeuS、DMTet、导出。
   3. 在图上标注每个技术的作用位置：
      - **NeRF/NeuS**：几何雕刻框内的两种相继使用的隐式表示；
      - **Zero123（stable-zero123-guidance）**：几何雕刻框上方的"视图相关扩散先验"，同时作用于 1a/1b/2 三个阶段；
      - **DMTet（tetrahedra-sdf-grid）**：几何雕刻后期的显式网格表示，延续到纹理提升阶段并被冻结（`fix_geometry: true`）；
      - **DreamBooth/BSD**：纹理提升框内部，画成一个"渲染 → 训练 LoRA → 蒸馏回渲染"的小循环，体现交替优化。
   4. 执行 4.3.4 的 grep 命令，把输出整理成 4.3.2 那样的表格（阶段、配置文件、几何、渲染、引导、分辨率、ckpt 衔接）。
3. **需要观察的现象**：画完后自测——能否不看书说出"为什么纹理阶段的优化器里有 `guidance.train_unet_lora` 而粗阶段没有"。
4. **预期结果**：一张两阶段流程图 + 一张四行对照表；grep 输出与表格逐行对得上。若你还能在图上标出 `system.weights` 与 `geometry_convert_from` 两条 ckpt 传递箭头，说明本讲目标全部达成。

## 6. 本讲小结

- DreamCraft3D 输入一张 RGBA 参考图 + 文本提示词，经「几何雕刻（coarse-nerf → coarse-neus → geometry）+ 纹理提升（texture）」的分层流水线，输出可导出为 obj 的照片级三维资产。
- 几何雕刻阶段靠**双引导**保证多面一致：DeepFloyd 文本 SDS + Zero123 视图相关扩散先验，由 `freq` 调度与参考图监督交替进行。
- 纹理提升阶段的 **BSD（自举得分蒸馏）** 是本项目最核心的创新：用场景渲染图 DreamBooth 训练专属 LoRA，再用它蒸馏回场景，扩散模型与三维表示交替优化、互相促进，配置上体现为 `stable-diffusion-bsd-guidance`、`lambda_lora`/`lambda_pretrain` 损失和 `train_unet`/`train_unet_lora` 双优化器参数组。
- 四个训练阶段 = 四份 yaml 配置：几何表示从 `implicit-volume` → `implicit-sdf` → `tetrahedra-sdf-grid` 逐级变"硬"，渲染器从体渲染换到 `nvdiff-rasterizer` 光栅化，分辨率从 128 渐进到 1024。
- 阶段间通过 `system.weights`（同族表示）与 `system.geometry_convert_from`（跨表示转换）传递检查点；所有配置共用 `system_type: "dreamcraft3d-system"`，行为差异完全由配置驱动。

## 7. 下一步学习建议

- **下一讲（u1-l2）「环境搭建与预训练权重下载」**：本讲多次提到 `stable_zero123.ckpt` 与 Omnidata 权重，下一讲动手把它们装进 `load/` 目录，并完成 threestudio 依赖安装（对应 [README.md:L47-L100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L47-L100) 与 `docs/installation.md`）。
- 之后再进入 u1-l3（仓库目录结构）和 u1-l4（`launch.py` 入口精读），正式推开源码的大门。
- 想提前建立全局感的读者，可以把 `threestudio/` 下 `models/`（geometry、renderers、guidance、prompt_processors）与 `systems/` 两个子包的文件名和 4.3.1 的对照表放在一起看——名字几乎一一对应。
