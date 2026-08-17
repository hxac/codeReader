# 四阶段配置对比：从 coarse 到 texture

## 1. 本讲目标

学完本讲，你应该能够：

1. 不看资料说出四个训练阶段（coarse-nerf、coarse-neus、geometry、texture）各自的 `geometry_type`、`renderer_type`、`guidance_type`、`prompt_processor_type`。
2. 理解「分辨率从 128 爬到 1024」的粗到细策略：哪个阶段在训练中途翻倍分辨率，哪个阶段一步到位用高分辨率。
3. 说清阶段之间检查点（ckpt）是如何接力的：`system.weights` 与 `system.geometry_convert_from` 各走哪条代码路径、各自适合什么场景。
4. 解释 texture 阶段两个特殊配置的含义：`fix_geometry: true` 为什么冻结几何，`optimizer.params` 里为什么出现 `guidance.train_unet` 与 `guidance.train_unet_lora` 两组学习率。

本讲是「读配置」的收官之作：u1-l1 建立了论文概念与模块的对照，u2-l2 讲透了配置系统的机制，本讲把四份 yaml 并排放在一起，看懂整条流水线的「演变剧本」。

## 2. 前置知识

### 2.1 阶段化训练：课程式学习

一次性从零训练高分辨率 3D 资产几乎必然失败——噪声大、显存爆、视角不一致。DreamCraft3D 采用**课程式（curriculum）策略**：先用便宜的低分辨率表示粗雕形状，再逐级换用更精细的表示与更高的分辨率。每换一级，就从上一级的成果（检查点）出发。这就像雕塑：先捏泥坯，再刻细节，最后上色。

### 2.2 三种几何表示的软硬程度（承接 u1-l1，一句话回顾）

- **implicit-volume（NeRF）**：用密度场 `σ(x)` 表示物体，最「软」，易优化但表面模糊。
- **implicit-sdf（NeuS）**：用符号距离场 `sdf(x)` 表示物体，SDF 的零水平集即表面，可产生光滑表面。
- **tetrahedra-sdf-grid（DMTet）**：在四面体网格顶点上存 SDF 值，用 marching tetrahedra **显式提取出三角网格**，最「硬」，可配合光栅化渲染器做高分辨率渲染。

### 2.3 体渲染 vs 光栅化

- **体渲染（nerf/neus-volume-renderer）**：沿每条相机射线采样若干点、逐点查询密度再积分成像素，天然可微，但计算量随分辨率平方增长。
- **光栅化（nvdiff-rasterizer）**：直接把三角网格投影到屏幕上填像素，快得多，但要求几何是显式网格——这就是为什么 DMTet 出场后渲染器也跟着换了。

### 2.4 LoRA 一句话版

LoRA（Low-Rank Adaptation）是一种参数高效微调技术：不改动大模型全部权重，只插入少量低秩可训练矩阵。本讲只需知道：texture 阶段的 BSD 引导里有两个可训练 UNet，其中一个最终以 LoRA 方式参与优化，因此优化器里出现独立的参数组。细节留到 u7-l4/u7-l5 展开。

### 2.5 四元组调度 C() 回顾（承接 u2-l2）

配置里形如 `[2000, 5., 1., 2001]` 的值会被系统按「步数感知」方式解析，其线性插值公式为：

\[
C(s) = v_s + (v_e - v_s)\,\frac{\min(\max(s - s_s, 0),\, s_e - s_s)}{s_e - s_s}
\]

即从第 \(s_s\) 步的值 \(v_s\) 线性过渡到第 \(s_e\) 步的值 \(v_e\)。本讲会看到它在损失权重（如 `lambda_orient`）与扩散时间步区间（如 `max_step_percent`）两处的应用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 阶段 1a：NeRF 粗雕，低分辨率起步 + 训练中翻倍 |
| [configs/dreamcraft3d-coarse-neus.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml) | 阶段 1b：NeuS 精修表面，中分辨率 |
| [configs/dreamcraft3d-geometry.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml) | 阶段 2：DMTet 显式网格几何精修，高分辨率 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | 阶段 3：冻结几何，BSD 扩散引导提升纹理 |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | Quickstart 四条接力命令，是 ckpt 传递方式的权威出处 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | 消费 `weights` 与 `geometry_convert_from` 的地方 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | `stage` 字段驱动的行为分支与双引导组装 |
| [threestudio/systems/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py) | `parse_optimizer`：把 `optimizer.params` 的键映射到参数组 |
| [threestudio/models/geometry/tetrahedra_sdf_grid.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py) | DMTet 几何，`fix_geometry` 的实现处 |
| [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) | BSD 引导，`train_unet`/`train_unet_lora` 的定义处 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `find_last_path`：解析 ckpt 路径中的 `@LAST` 通配 |

## 4. 核心概念与源码讲解

### 4.1 几何与渲染器的演变：从软体积到硬网格

#### 4.1.1 概念说明

四个阶段共用同一个 `system_type: "dreamcraft3d-system"`，变的只是它内部的插件组合。几何与渲染器必须**成对**更换，因为渲染方式取决于几何表示：

- 体积表示（implicit-volume / implicit-sdf）必须配体渲染器（nerf-volume-renderer / neus-volume-renderer），逐点积分；
- 显式网格表示（tetrahedra-sdf-grid）配光栅化渲染器（nvdiff-rasterizer），直接投影三角形。

这套「表示决定渲染器」的约束，就是四份配置中 `geometry_type` 与 `renderer_type` 同步演变的根本原因。

#### 4.1.2 核心流程

四阶段几何/渲染器演变一览（→ 表示「换用」）：

```text
coarse-nerf    implicit-volume    + nerf-volume-renderer   (软，128→384)
coarse-neus    implicit-sdf       + neus-volume-renderer   (半硬，256)
geometry       tetrahedra-sdf-grid+ nvdiff-rasterizer      (硬网格，1024)
texture        tetrahedra-sdf-grid+ nvdiff-rasterizer      (硬网格，1024，几何冻结)
```

除几何/渲染器外的其它差异也在换阶段时联动：混合精度 `precision` 从 `16-mixed`（粗阶段省显存）切到 `32`（DMTet 的 marching tetrahedra 数值敏感，全精度更稳），并新增 DDP 策略 `ddp_find_unused_parameters_true`（冻结部分参数后并非所有参数都收到梯度，需要该选项）。

#### 4.1.3 源码精读

**coarse-nerf：NeRF 体积 + Magic3D 密度初始化。** [configs/dreamcraft3d-coarse-nerf.yaml:41-44](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L41-L44) 声明系统类型与 `stage: coarse`、几何类型 `implicit-volume`。配置作者特意把 DreamFusion 的密度初始化注释掉、保留 Magic3D 版本（[L49-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L49-L60)），并用 [L64-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L64-L73) 的 `ProgressiveBandHashGrid` 做由粗到细的位置编码（`start_level: 8`、`start_step: 2000`：2000 步后才解锁更高分辨率层级）。渲染器为 `nerf-volume-renderer`，每射线采样 512 点（[L81-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L81-L86)）。

**coarse-neus：换成 SDF。** 相比 coarse-nerf，几何变为 `implicit-sdf` 并以半径 0.5 的球面作 SDF 初始偏置（[configs/dreamcraft3d-coarse-neus.yaml:42-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L42-L48)），位置编码换成普通 `HashGrid`（[L51-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L51-L60)，注意 `start_level` 等键只有 `ProgressiveBand*` 编码才消费，普通 `HashGrid` 并不使用它们），渲染器变为 `neus-volume-renderer` 并新增 `cos_anneal_end_steps` 退火参数（[L68-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L68-L73)）。

**geometry：DMTet + 光栅化。** [configs/dreamcraft3d-geometry.yaml:46-50](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L46-L50) 换成 `tetrahedra-sdf-grid`，128 分辨率四面体网格且 `isosurface_deformable_grid: true`（网格顶点也参与训练）；[L58-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L58-L60) 渲染器换为 `nvdiff-rasterizer`。训练器层面 [L121-128](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L121-L128) 用 `precision: 32` 与 DDP find_unused_parameters 策略。

**texture：几何同款但冻结。** [configs/dreamcraft3d-texture.yaml:46-59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L46-L59) 几何配置与 geometry 阶段几乎一致，新增 `fix_geometry: true`（详见 4.4）与 `isosurface_remove_outliers: true`。

#### 4.1.4 代码实践

1. **实践目标**：用 diff 直观看到「换表示时到底改了哪几行」。
2. **操作步骤**：在仓库根目录运行
   ```bash
   diff configs/dreamcraft3d-coarse-nerf.yaml configs/dreamcraft3d-coarse-neus.yaml | head -80
   diff configs/dreamcraft3d-geometry.yaml configs/dreamcraft3d-texture.yaml | head -80
   ```
3. **需要观察的现象**：第一组 diff 中 `geometry_type`/`renderer_type`/`sdf_bias`/`cos_anneal_end_steps` 等行成对出现；第二组 diff 中 `geometry` 段几乎不变，差异集中在 `guidance_type`、`prompt_processor_type`、`optimizer` 与 `fix_geometry`。
4. **预期结果**：确认「coarse→coarse 是换表示，geometry→texture 是换引导」——两次阶段切换改动的重心不同。
5. 运行结果待本地验证（diff 输出取决于本地文件，以上为源码静态对比结论）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `implicit-sdf` 不能直接配 `nvdiff-rasterizer`？
**答案**：nvdiff-rasterizer 的输入是显式三角网格；implicit-sdf 只是一个连续函数 `sdf(x)`，没有现成网格可投影，必须先经 neus 体渲染（或显式提取等值面）才能成像。配置系统不会阻止你乱配，但运行时会因参数不匹配而报错。

**练习 2**：`precision` 为什么在 geometry/texture 阶段从 `16-mixed` 改成 `32`？
**答案**：DMTet 的 marching tetrahedra 提取与网格变形涉及 SDF 阈值比较等数值敏感操作，半精度易产生伪影或网格破碎；同时这两阶段渲染用光栅化、显存压力小于体渲染，全精度开销可以接受。

**练习 3**：四份配置的 `system_type` 是什么？为什么不变？
**答案**：都是 `dreamcraft3d-system`。因为它是总装调度器，负责组装 geometry/material/background/renderer/guidance 并实现训练循环；换阶段换的是插进来的插件（由 `*_type` 决定），而不是总装逻辑本身。

### 4.2 分辨率的粗到细策略：128 → 256 → 1024

#### 4.2.1 概念说明

分辨率决定两个成本：**显存**（体渲染随像素数平方增长）与**优化难度**（高分辨率下扩散引导的梯度噪声更大）。DreamCraft3D 的策略分两层：

1. **阶段间爬坡**：coarse-nerf 用 128 起步，coarse-neus 升到 256，geometry/texture 直接 1024。
2. **阶段内爬坡**：coarse-nerf 更进一步，配置了一个「里程碑」——训练到第 3000 步时把训练分辨率从 128 翻到 384。

`data.height/width` 控制参考图监督分支的分辨率，`data.random_camera.height/width` 控制随机相机（扩散引导分支）的分辨率，两者要保持一致，否则两条分支的损失尺度不匹配。

#### 4.2.2 核心流程

```text
coarse-nerf:  data.height = [128, 384], resolution_milestones = [3000]
              → 0..2999 步用 128×128，3000 步起用 384×384（训练中途翻倍）
coarse-neus:  256×256（参考图与随机相机一致）
geometry:     1024×1024（光栅化，高分辨率开销可承受）
texture:      1024×1024（同上，且几何已冻结）
```

里程碑切换的判定发生在数据模块的 `update_step` 钩子中：每到一个里程碑步数，就用 `bisect` 选出当前档位索引（这正是 [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py) 中 `resolution_milestones` 的消费方式，机制细节在 u4-l2 展开）。

#### 4.2.3 源码精读

[configs/dreamcraft3d-coarse-nerf.yaml:9-11](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L9-L11) 写着 `height: [128, 384]`、`resolution_milestones: [3000]`——一个「档位列表 + 里程碑列表」，第 3000 步前取第 0 档、之后取第 1 档。随机相机子配置同样翻倍（[L18-22](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L18-L22)），且评估分辨率固定 512（[L23-24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L23-L24)）。coarse-neus 固定 256（[configs/dreamcraft3d-coarse-neus.yaml:9-10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L9-L10) 与 [L17-18](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L17-L18)）。geometry/texture 一律 1024，连评估分辨率也升到 1024（[configs/dreamcraft3d-geometry.yaml:9-10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L9-L10)、[L18-24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L18-L24)；[configs/dreamcraft3d-texture.yaml:9-10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L9-L10)、[L18-24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L18-L24)）。README 还提示显存不足时可以手动调低 NeuS 阶段分辨率（[README.md:169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L169)）。

#### 4.2.4 代码实践

1. **实践目标**：用脚本一次性核对四份配置的分辨率字段，验证上面的爬坡表。
2. **操作步骤**：新建 `check_res.py`（示例代码，放任意可运行环境执行）：
   ```python
   # 示例代码：解析四份配置并打印分辨率策略
   import glob
   from threestudio.utils.config import load_config

   for path in sorted(glob.glob("configs/dreamcraft3d-*.yaml")):
       cfg = load_config(path, cli_args=[], n_gpus=1)
       print(f"{path}")
       print(f"  data.height          = {cfg.data.height}")
       print(f"  milestones           = {cfg.data.get('resolution_milestones', '无')}")
       print(f"  random_camera.height = {cfg.data.random_camera.height}")
       print(f"  eval                 = {cfg.data.random_camera.eval_height}")
   ```
   （函数签名以 u2-l2 精读的 `load_config` 为准，若参数名有出入请按本地版本调整。）
3. **需要观察的现象**：coarse-nerf 打印出列表 `[128, 384]`，其余三个打印标量。
4. **预期结果**：与 4.2.2 的爬坡表一致；`load_config` 需要 `prompt: ???` 已被解析，若报 missing 值错误，可先在命令行覆盖 prompt 再解析。
5. 运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 coarse-nerf 不一开始就用 384？
**答案**：体渲染的采样数与像素数相乘，128² 起步能在早期快速迭代形状（梯度信噪比高、显存省）；等大形稳定后（3000 步）再翻倍细化，是典型的「先粗后细」课程。

**练习 2**：`data.height` 与 `data.random_camera.height` 为什么要一起改？
**答案**：前者决定参考图监督分支的渲染分辨率，后者决定扩散引导分支的渲染分辨率。两者不一致时，两个分支的损失尺度与有效监督内容不匹配，训练会失衡。

**练习 3**：README 提示省显存时可以 `data.height=128 data.width=128 data.random_camera.height=128 data.random_camera.width=128`，这利用了配置系统的什么特性？
**答案**：u2-l2 讲过的命令行点号语法覆盖——不修改 yaml 就能在启动时改写任意嵌套字段。

### 4.3 引导模型的演变：DeepFloyd+Zero123 双先验 → BSD 个性化先验

#### 4.3.1 概念说明

「引导（guidance）」是给 3D 场景提供 2D 先验的扩散模型。四阶段的引导配置分两个阵营：

- **coarse-nerf / coarse-neus / geometry（几何雕刻阵营）**：`guidance_type: deep-floyd-guidance`（DeepFloyd IF 文生图，提供文本先验）+ `guidance_3d_type: stable-zero123-guidance`（Zero123 单图生多视角，提供视图相关先验，是对抗 Janus 多面问题的关键）。`prompt_processor_type` 相应为 `deep-floyd-prompt-processor`（词表须与引导模型对齐）。
- **texture（纹理提升阵营）**：只保留 `stable-diffusion-bsd-guidance`（BSD：用场景自身渲染图 DreamBooth/LoRA 训练出的个性化 SD），`guidance_3d` 整段被注释掉，prompt processor 换成 `stable-diffusion-prompt-processor`。原因：几何已定，不再需要 Zero123 管几何一致性；纹理保真改由「见过本场景多视角渲染的专属扩散模型」负责。

#### 4.3.2 核心流程

```text
coarse/geometry 阶段:
  guidance     = deep-floyd-guidance        (lambda_sd    权重生效)
  guidance_3d  = stable-zero123-guidance    (lambda_3d_sd 权重生效)
texture 阶段:
  guidance     = stable-diffusion-bsd-guidance (lambda_sd=0.01, 另有 lambda_lora / lambda_pretrain)
  guidance_3d  = 未配置 → system 中 guidance_3d = None → lambda_3d_sd = 0.0
```

两个阵营的 `freq`（参考图与扩散引导的调度）也不同：coarse 两阶段用 `alternate`（每步二选一），geometry 用 `accumulate`（每步两个子步都做，且每 4 步才渲染一次 RGB、其余步渲染法向供 Zero123 监督），texture 回到 `alternate` 且 `no_diff_steps: -1`。`guidance` 子步是否执行由 [threestudio/systems/dreamcraft3d.py:191](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L191) 的 `true_global_step > no_diff_steps` 控制——`-1` 意味着从第 0 步起扩散引导就可用。

#### 4.3.3 源码精读

coarse-nerf 的双引导配置见 [configs/dreamcraft3d-coarse-nerf.yaml:88-111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L111)：prompt processor 与 guidance 都指向 `DeepFloyd/IF-I-XL-v1.0`，两者的扩散时间步区间 `min/max_step_percent` 均用四元组 `[0, 0.7, 0.2, 200]`（前 200 步从 0.7–0.9 的高噪声区间退火到 0.2–0.5——先塑形、后细化）。coarse-neus 与 geometry 阶段把四元组换成固定值 `0.2`/`0.5`（如 [configs/dreamcraft3d-coarse-neus.yaml:85-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L85-L86)），因为形状已经稳定，无需再退火。

texture 阶段的引导切换见 [configs/dreamcraft3d-texture.yaml:71-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L86)：prompt processor 与 guidance 都换成 `stabilityai/stable-diffusion-2-1-base`，guidance 还多出 `pretrained_model_name_or_path_lora`（BSD 的第二个模型源）与 `only_pretrain_step: 1000`（前 1000 步以「预训练专属扩散模型」为主，见 u7-l5）；`max_step_percent` 重新启用四元组 `[0, 0.5, 0.2, 5000]`。被注释掉的 Zero123 与 ControlNet 正则段在 [L88-109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L88-L109)。

「注释掉 guidance_3d 就真的没有 3D 引导」的代码依据在 [threestudio/systems/dreamcraft3d.py:44-49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L44-L49)：`guidance_3d_type` 为空字符串时 `self.guidance_3d = None`；而 `stage`、`guidance_3d_type`、`control_guidance_type` 这些字段本身就定义在系统 Config 上（[L21-36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L21-L36)），注释掉即取默认空值，整套机制天然支持「可选组件」。调度策略的消费处在 [L344-362](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L344-L362)：`accumulate` 时 `do_ref` 与 `do_guidance` 同步为真，`alternate` 时按 `n_ref` 取模交替；geometry 阶段还按 `n_rgb` 决定本步渲染 RGB 还是法向。

#### 4.3.4 代码实践

1. **实践目标**：验证「texture 阶段没有 3D 引导」这条推断的配置依据与代码依据。
2. **操作步骤**：
   - 打开 [configs/dreamcraft3d-texture.yaml:88-98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L88-L98)，确认 `guidance_3d_type` 整段处于注释状态；
   - 打开 [threestudio/systems/dreamcraft3d.py:44-49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L44-L49)，跟踪 `guidance_3d_type == ""` 的分支；
   - 再看 [configs/dreamcraft3d-texture.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L127) 的 `lambda_3d_sd: 0.0`，确认即使误配了 3D 引导，其损失权重也是零。
3. **需要观察的现象**：配置缺省、对象为 None、损失权重为 0 三重保险指向同一结论。
4. **预期结果**：能独立向他人解释「为什么删掉 guidance_3d 不会导致报错，也不会有 3D 先验梯度」。
5. 本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 coarse 阶段的 prompt processor 必须是 `deep-floyd-prompt-processor`，texture 阶段必须是 `stable-diffusion-prompt-processor`？
**答案**：prompt processor 的职责是把文本编码成引导模型能吃的嵌入，其分词器/文本编码器必须与 guidance 用的扩散模型一致。换了 guidance 就必须同步换 prompt processor，两份配置里两者指向同一个 `pretrained_model_name_or_path` 正是这种配对关系。

**练习 2**：`min_step_percent: [0, 0.7, 0.2, 200]` 与 `min_step_percent: 0.2` 在效果上有什么区别？
**答案**：前者是 C() 四元组——0 步时取 0.7，200 步时线性退火到 0.2，之后保持 0.2；后者恒为 0.2。前者让早期蒸馏集中在高噪声时间步（先定大局），后者适合形状已稳的中后段。

**练习 3**：geometry 阶段 `ref_or_guidance: "accumulate"` 而 coarse 阶段是 `"alternate"`，结合 `n_rgb: 4` 猜想动机。
**答案**：几何精修阶段既要参考图监督锁形状，又要 Zero123 管多视角一致，信息都要、不能省，于是每步两个子步都执行；`n_rgb: 4` 表示每 4 步渲染一次 RGB，其余步渲染法向图供引导监督，进一步分摊成本。

### 4.4 检查点接力与 texture 阶段的两大特殊配置

#### 4.4.1 概念说明

四个阶段是四次独立进程，靠**检查点文件**串联。接力方式有两种，语义完全不同：

- **`system.weights`（整体热启动）**：把上一阶段的**全部可训练状态**（几何、材质、背景、渲染器参数）直接加载进新系统，用于「同类表示之间」的接力（NeRF→NeuS 之外的常见场景；本流水线中 coarse-nerf→coarse-neus 用它）。加载时 `strict=False`，结构对不上的模块自动忽略。
- **`system.geometry_convert_from`（几何转换）**：当新旧几何**表示类型不同**（NeuS→DMTet）时，无法直接搬权重。系统会读取旧 ckpt 同目录的 `parsed.yaml` 重建旧几何、加载其权重，再调用新几何类的 `create_from` 把旧表示「翻译」成新表示（如从 NeuS 的 SDF 场采样出 DMTet 网格初始值）。

texture 阶段在此基础上做两件特殊的事：

1. **`fix_geometry: true` 冻结几何**——纹理提升阶段只优化外观（`geometry.encoding` + `geometry.feature_network`，即「网格顶点上的特征与颜色 MLP」），SDF 与网格变形不再更新。这样 BSD 扩散模型的梯度全部流向纹理，几何不会被纹理目标带偏。
2. **优化器出现 `guidance.train_unet` 与 `guidance.train_unet_lora` 两个参数组**——BSD 的核心是「交替优化扩散模型与 3D 场景」，因此扩散模型的 UNet 本身也是被优化对象。BSD 引导里装了两个可训练 UNet（[threestudio/models/guidance/stable_diffusion_bsd_guidance.py:189-206](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L189-L206)）：`train_unet` 与 `train_unet_lora`，分别服务于 VSD 式蒸馏与 LoRA 个性化两条优化通道（细节在 u7-l5），两者学习率需求不同，所以拆成两个参数组分别给 `lr: 0.00001`。

#### 4.4.2 核心流程

ckpt 接力全景（对应 [README.md:107-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L107-L126) 的 Quickstart）：

```text
coarse-nerf ──(last.ckpt via system.weights)──▶ coarse-neus          同类表示: 整体热启动
coarse-neus ──(last.ckpt via geometry_convert_from)──▶ geometry      换表示: create_from 转换
geometry   ──(last.ckpt via geometry_convert_from)──▶ texture        换表示: create_from 转换
```

`geometry_convert_from` 分支的执行过程（[threestudio/systems/base.py:243-286](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L286)）：

```text
1. find_last_path 解析路径中的 @LAST 通配
2. 条件: 指定了 geometry_convert_from 且未指定 weights 且非 resume
3. 读旧 ckpt 同目录 ../configs/parsed.yaml → 重建旧 geometry 配置与实例
4. load_module_weights(module_name="geometry") 载入旧几何权重
5. 新几何类.create_from(旧几何, 新配置, copy_net=geometry_convert_inherit_texture)
   → copy_net=true 时连纹理特征网络也一并拷贝（geometry→texture 阶段继承纹理）
```

`fix_geometry` 的实现（[threestudio/models/geometry/tetrahedra_sdf_grid.py:80-113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L113)）：false 时 SDF 与 deformation 用 `register_parameter`（可训练），true 时改用 `register_buffer`（随模型保存但不进优化器）；[L238-239](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L238-L239) 还会在几何固定后缓存提取的网格省去重复的 marching tetrahedra 计算。

`optimizer.params` 的键如何变成参数组：[threestudio/systems/utils.py:34-53](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L34-L53) 的 `parse_optimizer` 对每个键调用 [get_parameters](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L25-L31)，后者用 `getattr_recursive` 按点号逐层取属性——所以 `guidance.train_unet` 就是「system 的 guidance 属性 → 其 train_unet 属性 → 它的 parameters()」。

#### 4.4.3 源码精读

接力命令的权威出处：[README.md:115-125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L115-L125)。注意三处差异：coarse-neus 用 `system.weights="$ckpt"`，geometry/texture 用 `system.geometry_convert_from="$ckpt"`，且路径中 `@LAST` 会被 [threestudio/utils/misc.py:138-156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L138-L156) 的 `find_last_path` 展开为字典序最新的试验目录。

`system.weights` 的消费在 [threestudio/systems/base.py:46-47](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L46-L47)（构造后立即调用 `load_weights`）与 [L50-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L50-L56)（`load_state_dict(strict=False)` 并恢复步数敏感状态）。`geometry_convert_from` 的 Config 字段定义在 [L211-220](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L211-L220)，转换分支见上文 4.4.2 引用的 [L243-286](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L286)。

texture 的冻结与双学习率配置在 [configs/dreamcraft3d-texture.yaml:44-59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L44-L59)（`geometry_convert_from: ???`、`fix_geometry: true`）与 [L139-152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L139-L152)（优化器四组参数：`geometry.encoding`、`geometry.feature_network` 管场景外观，`guidance.train_unet`、`guidance.train_unet_lora` 管扩散模型）；与之呼应的损失项 `lambda_lora`、`lambda_pretrain` 出现在 [L123-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L123-L126)。两个可训练 UNet 的构建与 `requires_grad_(True)` 见 [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:189-206](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L189-L206)。

对比之下，coarse 两阶段的优化器没有 `params` 段（[configs/dreamcraft3d-coarse-nerf.yaml:141-146](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L141-L146)），`parse_optimizer` 走 `model.parameters()` 全量分支——因为那时扩散模型是冻结的先验，不是被优化对象；coarse-neus 则因 SDF 网络与编码器需要不同学习率而启用了 `params` 分模块（[configs/dreamcraft3d-coarse-neus.yaml:129-142](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L129-L142)）。

#### 4.4.4 代码实践（本讲综合实践）

1. **实践目标**：亲手制作四阶段对比表，并回答两个「为什么」——texture 阶段 `fix_geometry: true` 的动机、优化器里两组 guidance 学习率的来历。
2. **操作步骤**：
   - 通读四份 yaml，按下表模板填写（参考答案已给出，先自己填再对照）：

   | 维度 | coarse-nerf | coarse-neus | geometry | texture |
   | --- | --- | --- | --- | --- |
   | `stage` | coarse | coarse | geometry | texture |
   | `geometry_type` | implicit-volume | implicit-sdf | tetrahedra-sdf-grid | tetrahedra-sdf-grid（fix_geometry） |
   | `renderer_type` | nerf-volume-renderer | neus-volume-renderer | nvdiff-rasterizer | nvdiff-rasterizer |
   | `guidance_type` | deep-floyd-guidance | deep-floyd-guidance | deep-floyd-guidance | stable-diffusion-bsd-guidance |
   | `guidance_3d_type` | stable-zero123-guidance | stable-zero123-guidance | stable-zero123-guidance | 未配置（None） |
   | `prompt_processor_type` | deep-floyd-… | deep-floyd-… | deep-floyd-… | stable-diffusion-… |
   | 训练分辨率 | 128→384（3000 步） | 256 | 1024 | 1024 |
   | ckpt 接力入口 | 无（起点） | system.weights | geometry_convert_from | geometry_convert_from |
   | 主要损失/正则 | rgb/mask/depth_rel + orient/sparsity/opaque（C() 渐进） | 同左（固定权重）+ eikonal 槽位 | normal_consistency（C()） | rgb/mask + sd/lora/pretrain |
   | `precision` | 16-mixed | 16-mixed | 32 | 32 |

   - 回答「为什么 `fix_geometry: true`」：对照 [tetrahedra_sdf_grid.py:80-113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L113) 说明 buffer 与 parameter 的区别，并指出优化器 `params` 里只剩 `geometry.encoding`/`geometry.feature_network` 两个几何侧键作为佐证。
   - 回答「为什么两组 guidance 学习率」：对照 [stable_diffusion_bsd_guidance.py:189-206](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L189-L206) 与 [systems/utils.py:34-53](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L34-L53)，说明 BSD 把扩散模型本身纳入优化，两个 UNet 各司其职所以拆组。
3. **需要观察的现象**：填表过程中应发现「表示变硬、引导换血、优化对象扩容」三条主线贯穿四阶段。
4. **预期结果**：一张完成的对比表 + 两段有代码依据的解释。
5. 本实践为源码阅读型，无需 GPU，所有结论可直接从静态代码推出。

#### 4.4.5 小练习与答案

**练习 1**：如果同时指定了 `system.weights` 和 `geometry_convert_from`，会发生什么？
**答案**：看 [base.py:246-250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L246-L250) 的三重条件——指定了 weights 时转换分支被跳过，几何按 `weights` 整体加载，`geometry_convert_from` 实际不生效。配置里写了不代表会执行，要回到消费代码确认。

**练习 2**：`geometry_convert_inherit_texture: true` 在 geometry→texture 接力时有什么额外好处？
**答案**：它作为 `create_from` 的 `copy_net` 参数（[base.py:278-282](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L278-L282)），把上一阶段已学到的纹理特征网络一并拷贝过来，纹理提升阶段从一个「已有底色」的起点继续优化，而不是从零学外观。

**练习 3**：coarse-nerf 的优化器为什么可以不写 `params`？
**答案**：该阶段所有可训练参数（密度 MLP、哈希编码、背景色等）共用一个学习率 0.01 就够用，`parse_optimizer` 的无 `params` 分支直接把 `model.parameters()` 全量交给 Adam。一旦出现「不同模块要不同学习率」（coarse-neus 的 SDF 网络 vs 编码器）或「要优化扩散模型」（texture），就必须显式分组。

## 5. 综合实践

**任务：为四个阶段各写一条「一句话画像」，并模拟一次 ckpt 接力排错。**

1. 给每个阶段写一句话画像，格式如「我是 ___ 阶段，用 ___ 表示几何、___ 渲染、由 ___ 提供先验，分辨率 ___，我的任务是 ___」。
2. 排错模拟：假设你把 Quickstart 里 geometry 阶段的命令误写成 `system.weights="$ckpt"`（而不是 `geometry_convert_from`），根据 [base.py:46-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L46-L56) 与 [L243-286](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L286) 推断会发生什么（提示：implicit-sdf 的 state_dict 与 tetrahedra-sdf-grid 的参数名对不上，`strict=False` 之下多数权重被静默丢弃，DMTet 从零初始化）。写出你的推断与验证方法（例如对比训练前 100 步的 mask loss 是否异常偏高）。

参考画像（对照检查）：

- coarse-nerf：密度场 + 体渲染 + DeepFloyd/Zero123 双先验，128→384，任务是快速捏出大致形状与轮廓。
- coarse-neus：SDF 场 + NeuS 体渲染 + 同款双先验，256，任务是把模糊表面修光滑。
- geometry：DMTet 显式网格 + 光栅化 + 同款双先验（法向渲染配合），1024，任务是高分辨率精修几何细节。
- texture：冻结的 DMTet + 光栅化 + BSD 个性化 SD，1024，任务是只提升纹理保真度。

## 6. 本讲小结

- 四阶段共用 `dreamcraft3d-system`，靠 `geometry_type`/`renderer_type`/`guidance_type` 等 `*_type` 键换插件：几何沿 implicit-volume → implicit-sdf → tetrahedra-sdf-grid 由软变硬，渲染器随之从体渲染切换到光栅化。
- 分辨率执行 128（中途翻 384）→ 256 → 1024 的课程式爬坡，参考图分支与随机相机分支的分辨率必须同步。
- 引导分两个阵营：几何雕刻阵营用 DeepFloyd + Zero123 双先验（对抗 Janus），纹理阵营换成 BSD 个性化 SD 并注释掉 Zero123，`guidance_3d_type` 留空即 None，三重保险（缺省/None/权重为 0）确保无 3D 先验梯度。
- ckpt 接力有两种语义：同类表示用 `system.weights` 整体热启动；换表示用 `geometry_convert_from` 走 `create_from` 转换，且与 weights、resume 互斥。
- texture 的 `fix_geometry: true` 把 SDF 与变形从 parameter 降级为 buffer，冻结几何只训外观；BSD 让扩散模型本身可训练，`guidance.train_unet` 与 `guidance.train_unet_lora` 经 `parse_optimizer` 的点号寻址成为两个独立学习率组。

## 7. 下一步学习建议

本讲之后，配置层的「是什么」已经打通，接下来该看「怎么跑」：

- 下一讲 [u2-l4](u2-l4-outputs-and-export.md) 讲训练产物：trial 目录里 ckpts/save/code/configs 由哪些回调生成，以及如何用 `--export` 导出带纹理的 obj 网格。
- 若想立刻深入「配置如何被消费」，可先读 [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) 的 `configure` 与 `configure_optimizers`，把本讲 4.4 的接力链路亲手走一遍。
- 单元三将展开注册机制与 BaseSystem 生命周期（u3-l1、u3-l3），单元五逐个精读三种几何与渲染器（u5-l1 起），单元七攻破 DeepFloyd/Zero123/BSD 引导族（u7-l2 起）——届时本讲的对比表就是最好的导航图。
