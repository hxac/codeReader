# 讲义 u6-l3：training_substep（一）：参考图监督损失

## 1. 本讲目标

上一讲（u6-l2）我们看到 `training_step` 本身不算损失，它只是一个调度器：每个训练步决定跑「参考图监督（ref 子步）」还是「扩散引导（guidance 子步）」。本讲钻进 ref 子步内部，逐项解读 `training_substep` 中 `guidance == "ref"` 分支的六类监督信号。

学完本讲，你应该能够：

1. 说出 ref 子步的五类损失——RGB、mask、深度（最小二乘对齐）、相对深度（Pearson 相关）、法向（余弦相似度）——各自监督什么、在哪些阶段被配置启用。
2. 推导 `torch.linalg.lstsq` 深度对齐的数学意义：为什么要在损失里「先对齐、再比较」，以及它与 Pearson 相关损失在数学上的亲戚关系。
3. 解释 texture 阶段把 RGB 损失从 MSE 换成 L1+grow_mask 的动机（几何冻结 + 1024 分辨率 + 边缘不可对齐）。
4. 掌握法向损失的坐标约定：GT 与预测两侧各做了一次 `[0,1] → [-1,1]` 映射，且预测的 x 通道被翻转。

## 2. 前置知识

本讲默认你已读过 u6-l2（交替调度）、u4-l2（单图数据管线的 batch 结构）、u2-l1（图像预处理三件套）与 u2-l2（`cmaxgt0` resolver）。再补三个背景概念：

- **有效像素（valid pixels）**：参考图的 alpha 通道经 `> 0.5` 阈值化得到布尔 mask（u4-l2 已讲）。深度、法向这类逐像素监督只在 mask 内的像素上计算——背景像素的 Omnidata 深度是置零的（u2-l1），拿来监督毫无意义。
- **仿射对齐（affine alignment）**：两个量纲、零点都不同的标量场 \( a \) 与 \( b \)，先求一个最合适的 \( a\cdot s + o \approx b \)（同时优化尺度 \( s \) 和偏移 \( o \)），再比较残差。Omnidata 输出的是「相对深度」（min-max 归一化到 0~1），而渲染深度是世界坐标系下的距离（约 3.8），二者必须先对齐。
- **torchmetrics 的 `PearsonCorrCoef`**：计算两个一维张量的皮尔逊相关系数 \( r \in [-1,1] \)，衡量线性相关程度，对任意仿射变换（乘正数、加常数）不变。这正是「相对深度」监督需要的性质。

另外回顾一个贯穿全项目的机制：损失权重 `lambda_*` 既可以是常数，也可以是 `[start_step, start_val, end_val, end_step]` 四元组，由 `C()` 插值出当前步的值（见 [threestudio/systems/base.py:L92-L93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L92-L93)，实现在 [threestudio/utils/misc.py:L65-L83](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L83)）。每个损失分支都以 `self.C(self.cfg.loss.lambda_xxx) > 0` 作开关——权重调度到 0 的分支整段跳过，连前向里的张量都不会去取。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 本讲主战场：`training_substep` 的 ref 分支（L127-L189）与损失加权汇总（L321-L334） |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | coarse 阶段各 `lambda_*` 权重的取值与调度 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段损失权重的取舍（L1+grow_mask 的出处） |
| [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py) | GT 四件套（rgb/mask/ref_depth/ref_normal）的加载与形状 |
| [threestudio/models/renderers/nvdiff_rasterizer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py) | `comp_normal_viewspace` 与参考视角可见性 mask 的产出地 |
| [threestudio/models/geometry/tetrahedra_sdf_grid.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py) | `fix_geometry` 冻结几何的实现（理解 grow_mask 动机的关键） |

## 4. 核心概念与源码讲解

### 4.1 ref 子步的输入契约与损失装配线

#### 4.1.1 概念说明

ref 子步回答的问题是：**「我渲染出来的参考视角，和用户给的那张参考图到底有多像？」** 它是整条流水线里唯一的「像素级强监督」——guidance 子步的扩散先验只能给一个模糊的分数，而 ref 子步能逐像素告诉你差在哪。DreamCraft3D 能做到「输出和输入参考图外观一致」，靠的就是这条子步。

#### 4.1.2 核心流程

```text
training_step（u6-l2 的调度器判定 do_ref=True）
  └─ training_substep(batch, batch_idx, guidance="ref")
       ├─ 1. 从顶层 batch 取 GT 四件套：mask / rgb / ref_depth / ref_normal
       │     以及参考视角相机矩阵 mvp_mtx、c2w4x4
       ├─ 2. guidance=="ref" 时不切换相机 ── 渲染的就是参考视角本身
       │     （对照：guidance 子步会把 batch 换成 batch["random_camera"]）
       ├─ 3. 把参考矩阵改名 mvp_mtx_ref / c2w_ref 注入 batch，
       │     供 nvdiff 渲染器计算参考视角可见性 mask（u4-l2 已讲）
       ├─ 4. out = self(batch) 渲染，得到 comp_rgb / opacity / depth /
       │     comp_normal_viewspace 等预测
       ├─ 5. 按 C(lambda_*) > 0 逐项计算损失，set_loss 装入字典
       └─ 6. 汇总段：每项乘 C(lambda_*) 后求和，日志记录原始值与加权值
```

#### 4.1.3 源码精读

先看函数头与 GT 解包：[threestudio/systems/dreamcraft3d.py:L91-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L91-L109)。这段做了三件事：取出 GT 四件套和参考相机矩阵；在 guidance 子步才把 batch 换成随机相机（ref 子步保持参考视角）；无条件把参考矩阵以 `_ref` 后缀注入 batch。

再看损失装配线：[threestudio/systems/dreamcraft3d.py:L112-L117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L112-L117)。`set_loss` 把每项损失以 `loss_ref_` 前缀（`loss_prefix` 由子步类型决定）存入字典——所以 TensorBoard 里你会看到 `train/loss_ref_rgb`、`train/loss_guidance_sd` 这样的成对日志。

最后是加权汇总：[threestudio/systems/dreamcraft3d.py:L321-L334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334)。每个条目乘上 `C(lambda_对应名)`（名字由 `loss_ref_` 前缀替换成 `lambda_` 得到），加权前后分别记录为 `train/<name>` 与 `train/<name>_w`；所有 `lambda_*` 的当前调度值也整批记进 `train_params/`。

GT 侧的形状来源（数据侧已在 u4-l2 详述，这里只列结论）：

- mask：alpha 通道 `> 0.5` 的布尔张量，形状 `[1,H,W,1]`，见 [threestudio/data/image.py:L191-L193](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L191-L193)；
- ref_depth：Omnidata 深度 png 是单通道灰度图（[preprocess_image.py:L168-L196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L168-L196) 保存的 `final_depth` 是二维数组），读入后为 `[1,H,W]`；
- ref_normal：三通道 png，读入后为 `[1,H,W,3]`，见 [threestudio/data/image.py:L198-L234](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L198-L234)；
- 这些键在 collate 时打包进顶层 batch，见 [threestudio/data/image.py:L259-L281](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L259-L281)。

预测侧 `out["depth"]` 由体渲染器输出为 `[B,H,W,1]`，见 [threestudio/models/renderers/nerf_volume_renderer.py:L358-L365](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L358-L365)。注意这个形状不对称（GT 深度少一维），下文 4.4 会看到代码用两种不同的布尔索引方式把它们都化成 `[N]`。

配置开关与数据加载的联动也在此处收口：coarse 配置里 `requires_depth: true` 但 `lambda_depth: 0.0`、`lambda_depth_rel: 0.05`（[configs/dreamcraft3d-coarse-nerf.yaml:L125-L139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L125-L139)）——深度数据是为 rel 损失加载的；而 `requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}`（[configs/dreamcraft3d-coarse-nerf.yaml:L16-L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L16-L17)）让法向图的加载随 `lambda_normal` 权重自动开关（u2-l2 讲过的 resolver 联动）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 ref 子步产出的日志条目，验证「原始值 / 加权值 / 调度参数」三套日志的存在。

**操作步骤**：

1. 确认环境可用后，用极短步数启动 coarse 训练：
   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       --gpu 0 system.prompt_processor.prompt="a hamburger" \
       trainer.max_steps=20 trainer.val_check_interval=20
   ```
2. 打开 `outputs/dreamcraft3d-coarse-nerf/<tag>@<时间戳>/csv_logs/` 下的 `metrics.csv`（由 CSVLogger 写出）。
3. 在表头中找三组列：`train/loss_ref_rgb`（原始值）、`train/loss_ref_rgb_w`（乘以 `C(lambda_rgb)` 后的值）、`train_params/lambda_orient`（四元组调度值）。
4. 任选一行，手算 `loss_ref_rgb × 1000.0`，与 `loss_ref_rgb_w` 列对比。

**需要观察的现象**：`loss_ref_rgb_w` 恰好是 `loss_ref_rgb` 的 1000 倍；`train_params/lambda_orient` 在第 20 步应等于 1.0（四元组 `[2000, 1., 10., 2001]` 在 2000 步前保持起始值 1.0）。

**预期结果**：三套日志列都存在且数值满足上述关系，证明 4.1.3 的汇总段逻辑与配置一致。

**若无法运行训练（缺 GPU/权重）**：改为源码阅读型验证——对照 [dreamcraft3d.py:L321-L334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334) 手工推演 `loss_ref_rgb` 这个名字如何被 `replace(loss_prefix, "lambda_")` 变换成 `lambda_rgb`。此路径**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_loss` 要用 `loss_prefix` 给损失名加前缀，而不是直接叫 `loss_rgb`？

**答案**：ref 子步与 guidance 子步共用同一个 `training_substep`。前缀区分了 `train/loss_ref_rgb` 与 `train/loss_guidance_sd` 等同名不同源的条目，更关键的是汇总段（L324）用 `name.startswith(loss_prefix)` 筛选「属于本子步」的条目加权求和——若不加前缀，guidance 子步会把 ref 的损失也算进去（或反之）。

**练习 2**：ref 子步里 `batch["mvp_mtx_ref"]` 的注入对深度损失有帮助吗？

**答案**：没有。深度监督直接用参考视角渲染出的 `out["depth"]`（ref 子步渲染矩阵本身就是参考视角，u4-l2 已论证）。`mvp_mtx_ref`/`c2w_ref` 是给 nvdiff 渲染器算「参考视角可见性 mask」用的（texture 阶段 BSD 需要），对逐像素的深度/法向比较没有作用。

---

### 4.2 RGB 损失：背景回填、MSE 与 texture 阶段的 L1+grow_mask

#### 4.2.1 概念说明

RGB 损失是参考图监督的主菜：让参考视角的渲染图与参考图本身逐像素一致。但它有两个不那么显然的设计：

1. **背景回填**：GT 的背景像素不用原图（本来就是空的或被换成了纯色），而是用**当前渲染的背景** `comp_rgb_bg` 填充。这样背景区域误差恒为零，损失只监督前景物体；同时（u5-l5 讲过）背景仍然参与 `comp_rgb` 的 over 合成，能被别的损失间接约束。
2. **随阶段变形**：coarse/geometry 阶段用 MSE；texture 阶段先做一个 `grow_mask` 腐蚀，再在剩下的像素上算 L1。

#### 4.2.2 核心流程

```text
若 C(lambda_rgb) > 0:
  gt_rgb ← gt_rgb·mask + comp_rgb_bg·(1−mask)        # 背景回填
  pred_rgb ← comp_rgb                                  # 含背景的完整渲染
  分阶段：
    stage ∈ {coarse, geometry}: MSE(gt, pred)
    stage == texture:
      grow_mask ← 1 − maxpool2d(1−gt_mask, 9×9, s=1, p=4)   # 前景腐蚀约 4 像素
      L1(gt·grow_mask, pred·grow_mask)
    其他 stage: L1(gt, pred)                            # 防御分支
```

`grow_mask` 的几何意义：对「背景指示图」（1−mask）做 9×9、步长 1、padding 4 的最大池化——只要某像素的 9×9 邻域内存在背景，池化后它就是背景。这相当于把背景**膨胀** 4 像素；再取反，得到**腐蚀** 4 像素的前景。RGB 损失只在腐蚀后的前景内部计算，剪影边缘约 4 像素宽的环形带被放弃监督。

#### 4.2.3 源码精读

RGB 损失主体：[threestudio/systems/dreamcraft3d.py:L127-L143](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L127-L143)。L130-L131 是背景回填（注意 `gt_mask.float()` 把布尔转成 0/1 参与混合）；L135-L136 是 coarse/geometry 的 MSE；L138-L141 是 texture 的 grow_mask + L1。

grow_mask 两行：[threestudio/systems/dreamcraft3d.py:L138-L141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L138-L141)。第一次 `permute` 把 `[B,H,W,1]` 换成 `[B,1,H,W]` 以满足 `max_pool2d` 的输入约定，池化后再 `permute` 回去。

texture 阶段的配置上下文：`lambda_rgb: 1000.`、`lambda_mask: 100.`，深度/法向监督全部归零，见 [configs/dreamcraft3d-texture.yaml:L123-L137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L123-L137)；渲染分辨率 1024 见同文件 L9-L10。而几何被冻结的根因在 DMTet：`fix_geometry: true`（[configs/dreamcraft3d-texture.yaml:L59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)），它使 `sdf` 与 `deformation` 根本不注册为参数——见 [threestudio/models/geometry/tetrahedra_sdf_grid.py:L80-L93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L93) 中 `if not self.cfg.fix_geometry:` 的分支被跳过。

#### 4.2.4 代码实践

**实践目标**：用纯 CPU 的 PyTorch 复现 `grow_mask`，直观看到「腐蚀 4 像素」的效果，并解释 texture 阶段的动机。

**操作步骤**（以下为示例代码，可直接保存为 `grow_mask_demo.py` 用 `python grow_mask_demo.py` 运行，只依赖 torch）：

```python
# 示例代码：复现 texture 阶段的 grow_mask
import torch
import torch.nn.functional as F

H = W = 64
gt_mask = torch.zeros(1, H, W, 1)               # [B,H,W,1]，模拟布尔 mask
gt_mask[:, 16:48, 16:48, :] = 1.0               # 中央 32×32 的方块前景

grow_mask = F.max_pool2d(1 - gt_mask.permute(0, 3, 1, 2), (9, 9), 1, 4)
grow_mask = (1 - grow_mask).permute(0, 2, 3, 1) # 与源码 L139-L140 完全一致

print("原前景像素数:", int(gt_mask.sum()))
print("腐蚀后像素数:", int(grow_mask.sum()))
print("边长缩了:", (32 - grow_mask.sum().sqrt()) / 2, "像素（理论值 4）")

# 可视化（可选）：pip install matplotlib 后取消注释
# import matplotlib.pyplot as plt
# fig, ax = plt.subplots(1, 2)
# ax[0].imshow(gt_mask[0, ..., 0], vmin=0, vmax=1); ax[1].imshow(grow_mask[0, ..., 0].detach(), vmin=0, vmax=1)
# plt.savefig("grow_mask.png")
```

**需要观察的现象**：腐蚀后前景从 32×32 缩到约 24×24（每边缩 4 像素）；`grow_mask` 是 0/1 图，与源码一致地充当逐像素权重。

**预期结果**：`边长缩了` 打印约 `4.0`（浮点误差内），证明 9×9/padding 4 的池化等价于半径 4 像素的腐蚀。

然后写下你对「texture 阶段为什么改用 L1+grow_mask」的解释，对照参考答案（见练习 3）。

#### 4.2.5 小练习与答案

**练习 1**：背景回填 `gt_rgb*mask + comp_rgb_bg*(1-mask)` 之后，背景像素的 RGB 损失恒为 0 吗？

**答案**：几乎恒为 0——GT 背景被替换成了渲染背景自己，逐像素差为零（`comp_rgb` 的背景区域与 `comp_rgb_bg` 由同一 over 合成产生）。严格说抗锯齿边缘处二者有细微差别（合成公式在半透明过渡带的处理），但设计意图就是「放弃监督背景，只管前景」。

**练习 2**：coarse 阶段为什么不用 grow_mask？mask 不也是 carvekit 抠出来、边缘有噪声的吗？

**答案**：coarse 阶段的几何本身就是要被 mask 损失（`lambda_mask: 100.`）塑形的对象——轮廓错了，梯度会去修正几何（`implicit-volume` 的密度场可动）。边缘失配在 coarse 阶段是「可修复的信号」而非「不可修复的噪声」。texture 阶段几何已冻结（`fix_geometry: true`），轮廓永远对不上了，边缘误差只能变成伤害外观优化的常数噪声，所以干脆腐蚀掉。

**练习 3**：text 阶段从 MSE 换成 L1 的动机是什么？

**答案**：两点。其一，分辨率从 128/384 升到 1024，MSE 对离群像素按平方惩罚，少量边缘大误差像素会主导整个损失的梯度方向；L1 对离群值更稳健，让优化专注于大面积纹理区域。其二，配合 grow_mask：即便腐蚀后，高分辨率下仍有亚像素级的对不齐，L1 的常数梯度（不随误差大小放大）比 MSE 的线性放大梯度更温和。二者的共同目标是「纹理阶段让纹理细节学习不被几何遗留误差绑架」。

---

### 4.3 mask 损失：opacity 的两副面孔

#### 4.3.1 概念说明

mask 损失监督「物体该占哪些像素」：GT 是布尔前景 mask，预测是渲染出的 `opacity`（不透明度图）。它有两条实现路线：

- `lambda_mask`：MSE，把 opacity 拉向 0/1 两端，过渡带也被平滑惩罚；
- `lambda_mask_binary`：BCE，对数尺度惩罚，对接近目标的 0/1 给极小损失、对「自信地错」给大损失。

四份默认配置里 `lambda_mask_binary` 全为 0（休眠分支），实战启用的是 MSE 版。

#### 4.3.2 核心流程

```text
若 C(lambda_mask) > 0:
  loss_ref_mask = MSE(gt_mask, opacity)
若 C(lambda_mask_binary) > 0:
  p ← opacity.clamp(1e-5, 1−1e-5)          # 防 log(0)
  loss_ref_mask_binary = BCE(p, gt_mask)    # 直接用 F.binary_cross_entropy
```

注意第二项的目标写的是 `batch["mask"]` 而非 `gt_mask`——在 ref 子步里 `batch` 未被替换，二者是同一个张量。

#### 4.3.3 源码精读

两条分支：[threestudio/systems/dreamcraft3d.py:L145-L153](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L145-L153)。clamp 的上下限 `1.0e-5 / 1−1.0e-5` 是为混合精度下的 `log` 数值稳定准备的（框架另有一个手写的稳定版 BCE，见 [threestudio/utils/ops.py:L304-L308](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L304-L308)，本分支没有用它）。

一个值得注意的现象：texture 配置里 `lambda_mask: 100.` 仍然非零（[configs/dreamcraft3d-texture.yaml:L128-L130](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L128-L130)），但该阶段 `opacity` 由冻结几何的光栅化覆盖决定（配合抗锯齿），对可训练参数（外观编码网络与 BSD 的 UNet/LoRA）几乎不产生梯度——mask 的实际塑形工作在 coarse/geometry 阶段就完成了。这是「配置保留但作用退化」的例子。

#### 4.3.4 代码实践

**实践目标**：比较 MSE 与 BCE 对同一个误差的惩罚方式，理解为什么需要 clamp。

**操作步骤**（示例代码，CPU 可跑）：

```python
# 示例代码：MSE vs BCE 的惩罚曲线
import torch
import torch.nn.functional as F

target = torch.tensor([1.0])
for p in [0.5, 0.9, 0.99, 0.999999, 1e-7]:
    p_t = torch.tensor([p]).clamp(1e-5, 1 - 1e-5)   # 复现源码的 clamp
    mse = F.mse_loss(torch.tensor([p]), target)
    bce = F.binary_cross_entropy(p_t, target)
    print(f"pred={p:<10} MSE={mse.item():.6f}  BCE={bce.item():.6f}")
```

**需要观察的现象**：pred=0.5 时两者同数量级；pred 越接近目标，BCE 下降到远小于 MSE（如 1e-4 量级 vs 1e-6 量级），而 pred 严重错误（1e-7，被 clamp 到 1e-5）时 BCE 爆炸到 11.5（即 −log(1e-5)）而 MSE 只有约 1。

**预期结果**：BCE 呈现「接近时极宽容、错误时极严厉」的对数特性；clamp 把最坏情况封顶在 −log(1e-5) ≈ 11.5，防止混合精度下 log(0) 产生 inf。**待本地验证**（数值以实际运行为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lambda_mask_binary` 在四份默认配置里都是 0？

**答案**：MSE 版已足够完成「轮廓塑形」任务，且与其他逐像素损失（RGB 的 MSE/L1）在数值尺度上更匹配。BCE 在 16-mixed 精度下对数值稳定性要求更高（源码因此加了 clamp），收益有限。这是上游 threestudio 保留的实验分支（DreamFusion 一系的传统写法），DreamCraft3D 未启用。

**练习 2**：mask 损失在 ref 子步用的是参考视角的 opacity。guidance 子步（随机相机）有 mask 监督吗？

**答案**：没有。mask 损失整段位于 `if guidance == "ref":` 分支内（L127），随机相机视角没有对应的多视角 GT mask，无从监督。随机视角下的形状约束来自扩散先验（以及 u6-l4 将讲的 orient/sparsity 等正则）。

---

### 4.4 深度损失：最小二乘尺度对齐

#### 4.4.1 概念说明

深度损失想让「渲染出的参考视角深度」与「Omnidata 预测的参考图深度」一致，从而给几何一个先验的起伏结构（不只是剪影）。但两个深度不可直接比较：

- GT 深度：单目相对深度，仅 min-max 归一化（u2-l1），量纲任意；
- 渲染深度：世界坐标下沿射线的期望终点距离，量级约等于相机距离 3.8。

直接算 MSE 会把「尺度差」当成误差。解法是**仿射对齐**：先求最优的 \( s, o \) 使 \( s\cdot d_{gt} + o \approx d_{pred} \)，再在对齐后的 GT 上算损失。这样监督的只是深度的「形状」而非绝对值。

#### 4.4.2 核心流程

```text
若 C(lambda_depth) > 0:
  取 mask 内有效像素：valid_gt [N]、valid_pred [N]
  构造 A = [valid_gt, 1]  （[N,2] 的设计矩阵）
  X = lstsq(A, valid_pred) （解 min‖A·X − valid_pred‖²，X=[s,o]ᵀ，no_grad）
  aligned_gt = A·X          （= s·valid_gt + o）
  loss_ref_depth = MSE(aligned_gt, valid_pred)
```

数学上，这是标准线性回归。\( X^\star \) 满足正规方程 \( A^\top A X^\star = A^\top y \)（\( y \) 为 valid_pred），一元情形有闭式解：

\[
s^\star = \frac{\operatorname{Cov}(d_{gt},\, y)}{\operatorname{Var}(d_{gt})}, \qquad
o^\star = \bar{y} - s^\star \bar{d}_{gt}
\]

而最优残差的均方（即本损失的值）有一个漂亮的恒等式：

\[
\text{MSE} = \operatorname{Var}(y)\left(1 - r^2\right), \qquad
r = \frac{\operatorname{Cov}(d_{gt}, y)}{\sigma_{d_{gt}}\sigma_y}
\]

其中 \( r \) 正是 4.5 要讲的 Pearson 相关系数。换句话说，**对齐后的深度 MSE 与相对深度损失是同一枚硬币的两面**：前者按 \( 1-r^2 \) 计罚，后者按 \( 1-r \) 计罚。\( r\to 1 \) 时前者以平方速度趋零，对「已经很好」的情形更宽容。

注意实现细节：`lstsq` 在 `torch.no_grad()` 里执行，对齐系数 \( s,o \) 是常数——梯度只通过 MSE 项流向 `valid_pred`（即流向几何），不会有人去「优化 GT」。

#### 4.4.3 源码精读

完整分支：[threestudio/systems/dreamcraft3d.py:L156-L165](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L156-L165)。

几个精读要点：

- **两种布尔索引**：GT 侧 `batch["ref_depth"][gt_mask.squeeze(-1)]` 用 3 维 mask `[1,H,W]` 索引 `[1,H,W]` 的张量得 `[N]`；预测侧 `out["depth"][gt_mask]` 用 4 维全形 mask 索引 `[1,H,W,1]` 的张量也得 `[N]`。两侧维度不一致（GT 少一维，见 4.1.3），所以索引写法不同，结果却对齐成同样的 `[N]`。
- **`A` 的构造**：`torch.cat([valid_gt_depth, ones], dim=-1)` 拼出 `[N,2]` 设计矩阵，第二列全 1 对应偏移项 \( o \)。
- **`torch.linalg.lstsq(A, valid_pred_depth).solution`**：返回 `[2,1]` 的 \( X=[s,o]^\top \)；`A @ X` 得到对齐后的 GT `[N,1]`。
- 源码注释里的 `[B, 2]`、`[2, 1]`、`[B, 1]` 中的 `B` 指 N 个有效像素，不是 batch 维。

配置现状：`lambda_depth: 0.0`（coarse 与 texture 都是），默认未启用；但数据侧 `requires_depth: true`（为 rel 损失服务）。若你在 coarse 配置里把它改为正值（如 `10.`），此分支即激活。

#### 4.4.4 代码实践（本讲核心实践之一）

**实践目标**：① 推导并验证 lstsq 对齐的数学意义；② 确认「对齐 MSE = Var(pred)·(1−r²)」恒等式。

**操作步骤**（示例代码，CPU 可跑，只依赖 torch；放仓库外任意目录运行，勿改源码）：

```python
# 示例代码：lstsq 深度对齐的数值验证
import torch
import torch.nn.functional as F

torch.manual_seed(0)
N = 5000
gt = torch.rand(N)                      # 模拟 Omnidata 相对深度 [0,1]
pred = 3.8 + 2.5 * gt + 0.3 * torch.randn(N)  # 模拟渲染深度：世界量级 + 噪声

# --- 复现源码 L159-L165 ---
valid_gt, valid_pred = gt.unsqueeze(1), pred.unsqueeze(1)   # [N,1]
with torch.no_grad():
    A = torch.cat([valid_gt, torch.ones_like(valid_gt)], dim=-1)  # [N,2]
    X = torch.linalg.lstsq(A, valid_pred).solution                # [2,1]
    aligned_gt = A @ X
loss_code = F.mse_loss(aligned_gt, valid_pred)

s, o = X[0, 0].item(), X[1, 0].item()
print(f"拟合系数: s={s:.4f}（真值 2.5）  o={o:.4f}（真值 3.8）")
print(f"源码路径的 loss   = {loss_code.item():.6f}")

# --- 手写闭式解（正规方程）对拍 ---
s_closed = ((gt - gt.mean()) * (pred - pred.mean())).sum() / ((gt - gt.mean()) ** 2).sum()
o_closed = pred.mean() - s_closed * gt.mean()
print(f"闭式解:            s={s_closed:.4f}  o={o_closed:.4f}")

# --- 恒等式 MSE = Var(y) * (1 - r^2) 对拍 ---
r = ((gt - gt.mean()) * (pred - pred.mean())).sum() / \
    (((gt - gt.mean()) ** 2).sum().sqrt() * ((pred - pred.mean()) ** 2).sum().sqrt())
identity = pred.var(unbiased=False) * (1 - r.item() ** 2)
print(f"恒等式 Var(y)(1-r²) = {identity.item():.6f}")
```

**需要观察的现象**：拟合系数逼近真值（2.5/3.8，受噪声影响有小偏差）；源码路径的 loss 与恒等式计算值在浮点误差内一致。

**预期结果**：三组数字两两吻合，证明：lstsq 分支等价于「去掉最优仿射失配后的残差」，且其值由相关系数唯一决定。**待本地验证**（不同随机种子数值略有浮动，但相等关系稳定）。

最后为源码 L159-L165 逐行写中文注释（写在你的笔记里，不改源码），标注每步张量形状：`[N] → [N,1] → [N,2] → [2,1] → [N,1] → 标量`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `lstsq` 的方向反过来（求 \( t\cdot d_{pred}+u \approx d_{gt} \)，再算 `MSE(d_gt, aligned_pred)`），损失值会变吗？

**答案**：最小二乘的残差（投影距离）在两个方向上是同一个——都等于 \( y \) 到 \( \operatorname{span}(d_{gt}, \mathbf{1}) \) 子空间的投影残差。但残差被归到哪一侧不同：原实现中 `aligned_gt` 是常数（no_grad），梯度全部指向 pred；反过来会把「对齐」做在 pred 上，`MSE(gt, aligned_pred)` 对 pred 的梯度多一条经过对齐系数的链（除非同样 no_grad，但那样梯度反而缺失）。原实现既保证了正确的监督方向，又切断了无意义的梯度旁路。

**练习 2**：为什么对齐要放在 `torch.no_grad()` 里？

**答案**：\( s, o \) 是「为了比较而临时估计的校准量」，不是模型的一部分。若允许梯度流过，优化器可以通过调整 pred 来影响 \( s, o \)（例如整体放大 pred 方差让残差相对变小），引入作弊路径。no_grad 把对齐变成纯数据预处理。

**练习 3**：mask 边缘的半透明像素（opacity 介于 0~1）也参与了深度损失，这合理吗？

**答案**：是的，这是布尔 mask 索引的天然结果（`alpha > 0.5` 即入选）。渲染侧这些像素的期望深度是「前后表面的加权混合」，GT 侧 Omnidata 在边缘同样模糊，二者误差特性相近，实践中可接受；若要更严格，可以改用 `opacity > 0.9` 之类的软筛（需改源码，本项目未这样做）。

---

### 4.5 相对深度损失与法向损失：Pearson 相关与 x 轴翻转的余弦

#### 4.5.1 概念说明

- **相对深度损失 `lambda_depth_rel`**：直接用 \( 1 - r \) 计罚（\( r \) 为 Pearson 相关系数）。它跳过显式对齐，天然对任何正尺度缩放和平移不变——监督「深度的排序与起伏形状」。coarse 配置中它是默认**唯一启用**的深度类监督（`0.05`）。
- **法向损失 `lambda_normal`**：GT 法向图（Omnidata，RGB 编码，`[0,1]`）与渲染的视角空间法向图做余弦相似度。两侧都要先从颜色空间映射回向量空间 \([-1,1]\)，且预测的 x 通道要做一次翻转——这是两个坐标系手性（chirality）不一致导致的工程补丁，源码以 `FIXME` 自嘲。

#### 4.5.2 核心流程

```text
相对深度：r = pearson(pred_depth[valid], gt_depth[valid])  → loss = 1 − r

法向：
  gt:   n_gt = 1 − 2·gt_normal            # [0,1] → [1,−1]
  pred: n_pred = comp_normal_viewspace
        n_pred.x ← 1 − n_pred.x            # 先在 [0,1] 空间翻转 x 通道
        n_pred = 2·n_pred − 1              # [0,1] → [−1,1]
  loss = 1 − mean(cos(n_pred[valid], n_gt[valid]))
```

x 通道翻转的代数等价形式：在 \([0,1]\) 空间做 \( 1-x \) 再映射到 \([-1,1]\)，等价于先映射再取负：

\[
2(1-x) - 1 = 1 - 2x = -(2x-1)
\]

即「翻转」就是把映射后的 x 分量取反。GT 侧不翻转、预测侧翻转 x，说明两个法向图约定相差一次左右手翻转（关于 yz 平面的镜像）。

#### 4.5.3 源码精读

相对深度：[threestudio/systems/dreamcraft3d.py:L168-L173](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L168-L173)。`self.pearson` 是在 `on_fit_start` 里实例化的 torchmetrics 度量：[threestudio/systems/dreamcraft3d.py:L74-L89](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L74-L89) 的最后一行 `self.pearson = PearsonCorrCoef().to(self.device)`（import 来自 L11）。同一段还把全部训练图存成 `all_training_images.png`，方便开训前肉眼核对 GT。

法向：[threestudio/systems/dreamcraft3d.py:L176-L189](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L176-L189)。要点：

- GT 映射 `1 - 2 * gt_normal[...]` 与预测映射 `2 * pred_normal[...] - 1` 方向相反——`[0,1]→[1,−1]` vs `[0,1]→[−1,1]`。这**不是**笔误造成的双重翻转：GT 的 y、z 分量走 `1−2x` 映射（等价于整体取反再加平移），与 x 通道翻转配合后，两侧只在 x 上相差符号、在 y/z 上…… 具体对应关系由 Omnidata 的通道约定决定（`FIXME: reverse x axis` 表明作者也未完全确认为什么恰好需要这一翻转，是「以实验对齐为准」的补丁）。理解到「两侧各做一次仿射映射 + 预测侧 x 取反，使两个法向空间对齐」即可。
- 预测取自 `out["comp_normal_viewspace"]`。全仓库只有 nvdiff 光栅化器产出这个键（[threestudio/models/renderers/nvdiff_rasterizer.py:L138-L141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L138-L141)，经抗锯齿后更新进输出）——coarse 阶段的 `nerf-volume-renderer` 没有。这就是为什么四份默认配置 `lambda_normal` 全为 0：在 coarse（体渲染）阶段启用它会在取 `out["comp_normal_viewspace"]` 时直接 KeyError；只有 geometry/texture（nvdiff）阶段才有启用条件。
- `pred_normal[..., 0] = 1 - pred_normal[..., 0]` 是原地赋值，autograd 以 index_put 记录，梯度仍能回传。

法向 GT 的形状 `[1,H,W,3]` 与布尔索引得 `[N,3]`，源码注释 `# [B, 3]` 同样以有效像素数计。

#### 4.5.4 代码实践（本讲核心实践之二）

**实践目标**：把手写的相关系数实现与 `torchmetrics.PearsonCorrCoef` 数值对拍，验证二者一致——这正是「把 `depth_rel` 从 PearsonCorrCoef 换成手写实现」的可行性证明（换成手写版可以避免在 `on_fit_start` 里挂载度量对象的时序依赖）。

**操作步骤**（示例代码；在有本仓库环境的机器上运行，因为要用 requirements 里已有的 torchmetrics）：

```python
# 示例代码：手写 Pearson 相关系数 vs torchmetrics 对拍
import torch
from torchmetrics import PearsonCorrCoef

def pearson_hand(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    # 示例代码：等价于 1 − r 里的 r，仿射不变
    x = x - x.mean()
    y = y - y.mean()
    return (x * y).sum() / (
        (x * x).sum().sqrt() * (y * y).sum().sqrt() + 1e-12
    )

torch.manual_seed(0)
x = torch.rand(4096)
y = 5.0 * x + 1.7 + 0.2 * torch.randn(4096)   # 正相关 + 噪声

meter = PearsonCorrCoef()
print("torchmetrics:", meter(y, x).item())     # 复现源码调用方向 (pred, gt)
print("手写实现:    ", pearson_hand(y, x).item())

# 仿射不变性检查：y 乘 100 加 3，r 不应变化
print("仿射后 r:    ", pearson_hand(100 * y + 3, x).item())
```

**需要观察的现象**：两个实现的输出在小数点后 5~6 位内一致；对 y 做任意正缩放和平移后 r 不变。

**预期结果**：一致性与不变性均成立——证明 `1 − r` 监督的只是深度「形状」，与 4.4 的 lstsq 对殊途同归（见恒等式）。**待本地验证**。

**附加小实验（x 翻转等价性）**：在上述脚本里追加

```python
# 示例代码：验证 4.5.2 的翻转等价式 2(1−x)−1 = −(2x−1)
v = torch.rand(7)
print(torch.allclose(2 * (1 - v) - 1, -(2 * v - 1)))   # 预期 True
```

**预期结果**：打印 `True`。

#### 4.5.5 小练习与答案

**练习 1**：`loss_depth_rel = 1 − r` 中，r 的取值范围是 [−1,1]，那这个损失的负值可能吗？

**答案**：可能——当两个深度负相关时 \( 1-r > 1 \)，损失大于 1；r=1 时损失为 0（完全正相关，深度形状一致）；r 最低到 −1 时损失达 2。没有负值下界问题（r ≤ 1 恒成立），但要注意它**只约束线性相关的形状**：\( d_{pred} = 2d_{gt}+5 \) 与完全一致同罚（零罚），这正是「相对深度」的本意。

**练习 2**：既然 4.4 证明了「对齐 MSE = Var(y)(1−r²)」，那 `lambda_depth` 与 `lambda_depth_rel` 同时启用会怎样？

**答案**：二者高度冗余（都是 r 的函数，分别按 \( 1-r^2 \) 与 \( 1-r \) 计罚，且前者还乘上 pred 方差这个随训练变化的因子）。区别在量纲稳定性：\( \operatorname{Var}(y) \) 会随几何尺度漂移使损失尺度不稳定，\( 1-r \) 则是无量纲的。这解释了为什么默认配置启用的是 rel 版（`0.05`）而把绝对版置 0。

**练习 3**：若在 coarse-nerf 配置里把 `lambda_normal` 改成 `0.05` 会发生什么？

**答案**：两处防线同时失效报错。其一，`requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}` 变为 true，数据侧会要求 `_normal.png` 存在（有预处理产物则通过）；其二，渲染侧 `nerf-volume-renderer` 的输出里没有 `comp_normal_viewspace` 键，`out["comp_normal_viewspace"]` 抛 KeyError。法向监督只能在 nvdiff 渲染的 geometry/texture 阶段启用——这也是它被归入「coarse-neus 阶段可选增强」的实践约束。**待本地验证**（报错类型以实际运行为准）。

---

## 5. 综合实践

**任务：制作一份「参考图监督损失体检报告」。** 把本讲的五类损失串起来：

1. **表格部分**：完成下表（阶段列填 coarse / geometry / texture，默认权重查四份 yaml，代码位置给出行号）：

   | 损失项 | 监督对象（GT → Pred） | 默认权重（coarse-nerf） | 默认权重（texture） | 代码位置（dreamcraft3d.py） |
   | --- | --- | --- | --- | --- |
   | rgb | 参考图 RGB（背景回填）→ comp_rgb | 1000.0 | 1000.0（L1+grow_mask） | L130-L143 |
   | mask | alpha mask → opacity | ？ | ？ | L146-L147 |
   | mask_binary | alpha mask → opacity（BCE） | ？ | ？ | L150-L153 |
   | depth | lstsq 对齐后深度 → 渲染深度 | ？ | ？ | L156-L165 |
   | depth_rel | Pearson 1−r | ？ | ？ | L168-L173 |
   | normal | Omnidata 法向 → comp_normal_viewspace | ？ | ？ | L176-L189 |

   （`？`处请自查 [configs/dreamcraft3d-coarse-nerf.yaml:L125-L139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L125-L139) 与 [configs/dreamcraft3d-texture.yaml:L123-L137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L123-L137)。）

2. **脚本部分**：把 4.4.4 与 4.5.4 的两个对拍脚本合并成一个 `ref_loss_check.py`，依次输出：lstsq 系数、闭式解系数、`MSE = Var·(1−r²)` 三方对拍、手写 Pearson 与 torchmetrics 对拍、翻转等价性布尔值。

3. **分析部分**（写 200 字以内）：用一句话回答「为什么 DreamCraft3D 的 ref 监督要设计成『粗阶段全面监督（RGB+mask+相对深度）、纹理阶段只留 RGB 且带容差』？」提示：结合几何可动性（`fix_geometry`）与分辨率课程（128→1024）。

**验收标准**：表格六行无空缺且权重与 yaml 一致；脚本三方对拍数值吻合；分析覆盖「几何冻结 + 高分辨率边缘失配」两个关键词。

## 6. 本讲小结

- ref 子步是流水线中唯一的逐像素强监督，GT 四件套（rgb/mask/ref_depth/ref_normal）来自单图数据管线，预测来自参考视角渲染；所有分支由 `C(lambda_*) > 0` 门控，权重支持四元组调度。
- RGB 损失先做背景回填（GT 背景换成渲染背景 `comp_rgb_bg`，从而只监督前景），coarse/geometry 用 MSE；texture 阶段因几何冻结（`fix_geometry` 使 sdf/deformation 不注册为参数）与 1024 分辨率，改用 L1 并用 9×9 最大池化构造的 `grow_mask` 腐蚀掉剪影边缘约 4 像素。
- mask 损失用 MSE 把 opacity 拉向 GT 剪影（BCE 版为休眠分支）；texture 阶段虽保留 `lambda_mask: 100.`，但冻结几何使其实际近乎无梯度。
- 深度损失先在 mask 内取有效像素，用 `torch.linalg.lstsq` 求 `s·gt+o ≈ pred` 的最优仿射对齐（no_grad 切断梯度旁路），再算对齐后 MSE；其值恒等于 `Var(pred)·(1−r²)`，与相对深度损失 `1−r` 是同一监督的两种计罚，默认只启用后者（`0.05`）。
- 法向损失把 Omnidata 法向与 `comp_normal_viewspace` 各自从 [0,1] 映射到 [-1,1] 并对预测 x 通道取反（源码 FIXME 标记的手性补丁），以余弦相似度计罚；该键只有 nvdiff 渲染器产出，故法向监督仅适用于 geometry/texture 阶段，四份默认配置均未启用。
- 汇总段统一做「乘 `C(lambda_*)`、记原始值与 `_w` 加权值、整批记录 `train_params/`」的日志装配，方便在 TensorBoard/CSV 中直接审计每个损失的实时权重。

## 7. 下一步学习建议

本讲读完了 `training_substep` 的 ref 分支；下一讲 **u6-l4《training_substep（二）：正则化与阶段专属损失》**接着读同一个函数的其余部分：位于子步公共区的 `normal_smooth`/`3d_normal_smooth`，coarse 阶段的 `orient`/`sparsity`/`opaque`/`eikonal`/`z_variance`，geometry 阶段的 `normal_consistency`/`laplacian_smoothness` 网格正则，以及 texture 阶段的 ControlNet 感知正则 `lambda_reg`。建议先自行通读 [threestudio/systems/dreamcraft3d.py:L226-L319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L226-L319) 并留意：为什么其中 `lambda_sparsity` 带 `guidance != "ref"` 的额外条件（提示：本讲 4.3 练习 2 的答案）。
