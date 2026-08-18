# Stable Zero123：视图条件 3D 先验

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释**视图条件扩散**（view-conditioned diffusion）为什么能缓解 Janus 多面问题：它给扩散模型看的不是一句文本，而是「参考图 + 相对相机姿态」，于是每个视角收到的先验信号天然互相一致。
2. 完整追踪 `load_model_from_config` 的加载链路：`stable_zero123.ckpt`（权重）+ `sd-objaverse-finetune-c_concat-256.yaml`（结构蓝图）如何经 `instantiate_from_config` 反射出 `LatentDiffusion`，再由 `load_state_dict(strict=False)`、EMA 拷贝、`vram_O` 删 decoder 三步收尾。
3. 读懂参考图的**双重嵌入**：CLIP 语义嵌入（(1,1,768)，走交叉注意力）与 VAE 像素潜码（(1,4,32,32)，拼进 UNet 输入通道），以及二者在 `get_cond` 中如何与相机条件 T 四元组经 `cc_projection` 融合。
4. 说清 `cond_elevation_deg` / `cond_azimuth_deg` 与随机相机 batch 中 `elevation` / `azimuth` 的对应关系——条件是**相对姿态**（目标极角−参考极角、方位角差），并理解 Stable Zero123 特有的第四维（参考视角绝对极角）与原版 Zero123 的差异。

本讲聚焦粗阶段双引导（u6-l1）中的 3D 通道 `stable-zero123-guidance`。它在 u7-l2 的 guidance 子步里紧随 DeepFloyd 之后被调用，**完整复用**了那一讲已经推导过的 SDS 骨架（同样的加噪、同样的 \(w(t)\)、同样的重参数化梯度注入），新增的只有「条件如何构造」这一件事。因此本讲的篇幅重心在条件构造与模型加载，SDS 数学不再重复推导。

## 2. 前置知识

### 2.1 Janus 问题与视图条件先验

u7-l1 与 u7-l2 讲过：纯文本先验（DeepFloyd）不知道「这个物体的侧面应该长什么样」，它对任何视角都倾向生成最典型、最正面的一面，于是 NeRF 学出多个正面拼起来的「多头怪」。这就是 Janus 问题（Janus 是罗马神话里的双面神）。

Zero123 系列模型的解法是换一个先验：它本身是一个**新视角合成**模型，在 Objaverse 百万级三维资产的多视角渲染图上训练。输入一张参考图和一个相对相机姿态，输出该姿态下应该看到的视图。它不是「文字描述这个物体」，而是「见过这个物体从这个角度看的样子」——把它的评分能力接到 SDS 上，每个随机视角得到的先验信号就都与参考图对齐，多面一致性问题从源头缓解。

### 2.2 潜空间扩散（LDM）与 hybrid 条件

与 DeepFloyd 的像素空间扩散不同（u7-l2 第 2.4 节），Zero123 基于 Stable Diffusion 架构，在 **VAE 潜空间**工作：256×256 的图像经 VAE 编码器下采样 8 倍得到 4×32×32 的潜码，扩散过程全部发生在潜码上，最后由 VAE 解码器还原成图像。

它的条件注入是 **hybrid（混合）式**的，两条通道同时生效：

- **concat 通道**：参考图的 VAE 潜码直接与带噪潜码在**通道维拼接**，UNet 输入从 4 通道变 8 通道——像素级的「长什么样」。
- **crossattn 通道**：参考图的 CLIP 嵌入（融合相机条件后）作为交叉注意力的 context——语义级的「这是什么、从哪个角度看」。

### 2.3 相机参数从哪来（回顾 u4-l1 / u6-l2）

guidance 子步用的 batch 是 `batch["random_camera"]`（u6-l2），其中携带随机相机当次采样的 `elevation`、`azimuth`、`camera_distances`（单位：度 / 度 / 世界坐标距离）。本讲的 `get_cond` 消费前两个，把它们换算成相对姿态。回顾 [configs/dreamcraft3d-coarse-nerf.yaml:L26-L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L26-L36)：粗阶段把相机距离钉死 3.8、fovy 钉死 20°（注释明言 `# Zero123 has fixed fovy`）、关闭全部扰动——就是为了让渲染相机的分布严格落在 Zero123 训练时的相机分布内。

### 2.4 术语表

| 术语 | 含义 |
| --- | --- |
| Zero123 / Stable Zero123 | 视图条件新视角合成扩散模型；Stable 版是 Stability AI 发布的改进版，修正了仰角歧义 |
| polar（极角） | Zero123 的仰角约定：polar = 90° − elevation，即与「正上方」的夹角 |
| cc_projection | CLIP-Camera Projection 的缩写，代码里的 `nn.Linear(772, 768)`：把 768 维 CLIP 嵌入与 4 维相机条件投影回 768 维（extern/zero123.py 中同类模块就叫 `CLIPCameraProjection`） |
| c_crossattn / c_concat | hybrid 条件的两把钥匙，见 `__conditioning_keys__` 映射表 |
| CFG 负向分支 | 本模型没有负向文本；uncond 分支 = 全零嵌入 + 全零参考潜码 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [threestudio/models/guidance/stable_zero123_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py) | 本讲主角：Stable Zero123 的 SDS 引导封装 | 全文件：加载工具 / configure / prepare_embeddings / get_img_embeds / get_cond / `__call__` / update_step |
| [extern/ldm_zero123/models/diffusion/ddpm.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py) | vendored 的 CompViz LDM 代码，被加载的模型本体 | `LatentDiffusion.__init__`（cc_projection）、`get_learned_conditioning`、`encode_first_stage`、`get_first_stage_encoding`、`apply_model`、`DiffusionWrapper` |
| [extern/ldm_zero123/modules/encoders/modules.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/modules/encoders/modules.py) | 条件编码器 | `FrozenCLIPImageEmbedder`：CLIP ViT-L/14 图像编码 |
| [extern/zero123.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/zero123.py) | 原版 Zero123 的 diffusers 推理管线（仅被 `zero123-unified-guidance` 导入，DreamCraft3D 四份配置都不用它） | 对照阅读：`CLIPCameraProjection`、`_encode_image` 的相机编码、`_get_latent_model_input` 的 concat |
| [extern/ldm_zero123/guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/guidance.py) | vendored 的分类器引导实验代码（Guider/GuideModel） | 确认它是仓库内无人导入的死代码，避免误读 |
| [load/zero123/sd-objaverse-finetune-c_concat-256.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/sd-objaverse-finetune-c_concat-256.yaml) | 模型结构蓝图 | `target` 反射链、`conditioning_key: hybrid`、`in_channels: 8`、`context_dim: 768` |
| [load/zero123/download.sh](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/download.sh) | 权重下载脚本 | stable_zero123.ckpt 来自 HuggingFace stabilityai/stable-zero123 |
| [threestudio/models/guidance/zero123_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/zero123_guidance.py) | 原版（非 Stable）Zero123 引导，与本讲主角逐行对照 | `get_cond` 的 T 第四维差异 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段配置 | `guidance_3d` 段、随机相机参数钉死 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 系统层 | `guidance_3d` 的调用点与 `3d_sd` 损失映射 |

## 4. 核心概念与源码讲解

本讲拆五个最小模块：加载链路 → 参考图双重嵌入 → 相机条件与 hybrid 注入 → SDS 复用与系统接线 → 边界与死代码辨析。

### 4.1 加载链路：load_model_from_config 与「target 反射实例化」

#### 4.1.1 概念说明

threestudio 自己的组件用注册机制实例化（u3-l1：`X_type` 的值查 `__modules__` 字典）。而 Zero123 这套来自 CompVis Stable Diffusion 训练框架的 vendored 代码用**另一套**实例化约定：yaml 里每个组件写 `target`（完整 Python 类路径）与 `params`（构造参数），加载时用 `importlib` 反射出类再调用。两套机制解决同一个问题——「配置即蓝图」——但查找方式不同：注册机制查字典，target 反射直接 import。读懂这条链路，是本讲实践任务的第一半。

#### 4.1.2 核心流程

```text
StableZero123Guidance.configure()
 ├─ OmegaConf.load("load/zero123/sd-objaverse-finetune-c_concat-256.yaml") → self.config
 └─ load_model_from_config(self.config, "load/zero123/stable_zero123.ckpt", device, vram_O=True)
     ├─ pl_sd = torch.load(ckpt, map_location="cpu")；sd = pl_sd["state_dict"]
     ├─ instantiate_from_config(config.model)
     │    └─ get_obj_from_str("extern.ldm_zero123.models.diffusion.ddpm.LatentDiffusion")
     │        → LatentDiffusion(**params)，构造时依次实例化：
     │            ├─ UNetModel（unet_config，in_channels=8）
     │            ├─ AutoencoderKL（first_stage_config，VAE）→ 立即冻结
     │            ├─ FrozenCLIPImageEmbedder（cond_stage_config，CLIP ViT-L/14）→ 立即冻结
     │            └─ cc_projection = nn.Linear(772, 768)（恒等初始化）
     ├─ model.load_state_dict(sd, strict=False)   # 缺键/多键只记录不报错
     ├─ use_ema=True：model_ema.copy_to(model) 后删除 EMA 副本
     ├─ vram_O=True：del model.first_stage_model.decoder   # 省 VRAM
     └─ model.eval().to(device)
随后 configure 继续：
 ├─ 全参数 requires_grad_(False)（冻结裁判）
 ├─ DDIMScheduler(1000, linear_start=0.00085, linear_end=0.0120, scaled_linear, ...)
 ├─ alphas = scheduler.alphas_cumprod → GPU
 └─ prepare_embeddings(cond_image_path)   # 预计算参考图嵌入（4.2）
```

#### 4.1.3 源码精读

target 反射的工具函数——注意它与注册机制毫无关系，纯 `importlib`：

- [threestudio/models/guidance/stable_zero123_guidance.py:L21-L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L21-L36)：`get_obj_from_str` 把 `"extern.ldm_zero123.models.diffusion.ddpm.LatentDiffusion"` 按最后一个点拆成模块路径与类名，`importlib.import_module` 后 `getattr` 取类；`instantiate_from_config` 检查 `target` 键存在后以 `params` 为关键字参数调用它。特殊字符串 `__is_first_stage__` / `__is_unconditional__` 返回 None，是 CompViz 惯用的占位符。

加载函数本体，注意最后三个工程细节：

- [threestudio/models/guidance/stable_zero123_guidance.py:L40-L71](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L40-L71)：`load_model_from_config` 完成「蓝图 → 空模型 → 灌权重」。`load_state_dict(sd, strict=False)`（L49）容忍键名不完全匹配；`model.use_ema` 为真时把 EMA 权重拷回正式权重并删除 EMA（L57-L61）——蒸馏用的是平滑后的 EMA 参数；`vram_O` 为真时直接 `del model.first_stage_model.decoder`（L63-L65），因为蒸馏只需要 VAE 编码器（渲染图 → 潜码），解码器（潜码 → 图）在这条路径上用不到。

configure 的入口与冻结：

- [threestudio/models/guidance/stable_zero123_guidance.py:L99-L113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L99-L113)：L102 加载结构 yaml，L103-L104 有一句 TODO——这套代码无法以 fp16 加载，故 `weights_dtype` 硬编码 `torch.float32`；L105-L110 调用 `load_model_from_config`；L112-L113 对**全部**参数 `requires_grad_(False)`，Zero123 是彻底冻结的裁判（与 u7-l2 的 DeepFloyd 相同；对比 u7-l4 将讲的 BSD，UNet 在那里成了运动员）。

结构蓝图——建议逐行读一遍这份只有 69 行有效内容的 yaml：

- [load/zero123/sd-objaverse-finetune-c_concat-256.yaml:L1-L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/sd-objaverse-finetune-c_concat-256.yaml#L1-L17)：`target: extern.ldm_zero123.models.diffusion.ddpm.LatentDiffusion` 是整条反射链的起点；`conditioning_key: hybrid` 决定 4.3 节的双通道注入；`timesteps: 1000` 与 `scale_factor: 0.18215`（VAE 潜码缩放系数）都在这里；`linear_start/linear_end` 供 DDIMScheduler 构造 β 序列。
- [load/zero123/sd-objaverse-finetune-c_concat-256.yaml:L28-L43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/sd-objaverse-finetune-c_concat-256.yaml#L28-L43)：UNet 配置——`in_channels: 8`（4 噪声潜码 + 4 参考图潜码，4.3 节的拼接落点）、`out_channels: 4`、`context_dim: 768`（交叉注意力维度，正好等于 CLIP ViT-L/14 图像嵌入维度）。
- [load/zero123/sd-objaverse-finetune-c_concat-256.yaml:L45-L69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/sd-objaverse-finetune-c_concat-256.yaml#L45-L69)：一级压缩（`first_stage_config` → AutoencoderKL）与条件编码（`cond_stage_config` → FrozenCLIPImageEmbedder）的 target。

被实例化出来的模型本体——`LatentDiffusion` 构造函数里藏着一个重要角色：

- [extern/ldm_zero123/models/diffusion/ddpm.py:L606-L664](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L606-L664)：`LatentDiffusion.__init__`。L648-L649 依次实例化 VAE 与 CLIP 编码器；L652-L656 构造 `self.cc_projection = nn.Linear(772, 768)`，并把权重前 768×768 块初始化为**单位阵**、偏置置零——即未训练时该层是「CLIP 分量直通、相机分量忽略」的恒等映射，随后 `load_state_dict` 会用检查点里训练好的值覆盖它。
- [extern/ldm_zero123/models/diffusion/ddpm.py:L721-L747](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L721-L747)：`instantiate_first_stage` / `instantiate_cond_stage` 把 VAE 与 CLIP 立即 `eval()` 并冻结——LDM 框架里这两个 stage 天生不训练。

权重的来源：

- [load/zero123/download.sh:L1-L4](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/download.sh#L1-L4)：脚本里被注释的两行是原版 zero123 权重，生效的一行 wget zero123-xl，末行注释指出 `stable_zero123.ckpt` 需从 HuggingFace `stabilityai/stable-zero123` 手动下载（u1-l2 讲过 load/ 的摆放规则）。

#### 4.1.4 代码实践

**实践：纸面追踪加载链路（不需要 GPU 与权重）。**

1. 实践目标：验证 yaml 的每个 `target` 都能解析到真实类，画出「yaml 键 → Python 对象」映射表。
2. 操作步骤：在仓库根目录运行下面的脚本（示例代码，只做类解析、不实例化模型，CPU 即可）：

   ```python
   # 示例代码：trace_zero123_load.py
   from omegaconf import OmegaConf
   from threestudio.models.guidance.stable_zero123_guidance import get_obj_from_str

   cfg = OmegaConf.load("load/zero123/sd-objaverse-finetune-c_concat-256.yaml")
   for name, sub in [
       ("model", cfg.model),
       ("unet_config", cfg.model.params.unet_config),
       ("first_stage_config", cfg.model.params.first_stage_config),
       ("cond_stage_config", cfg.model.params.cond_stage_config),
   ]:
       cls = get_obj_from_str(sub.target)   # 反射：importlib + getattr
       print(f"{name:20s} {sub.target}  ->  {cls}")
   print("in_channels =", cfg.model.params.unet_config.params.in_channels)
   print("context_dim =", cfg.model.params.unet_config.params.context_dim)
   print("conditioning_key =", cfg.model.params.conditioning_key)
   ```

3. 需要观察的现象：四行 target 全部成功解析（证明导入链完整）；打印出 `in_channels = 8`、`context_dim = 768`、`conditioning_key = hybrid`。
4. 预期结果：得到一张四行映射表，其中 `cond_stage_config` 解析到 `FrozenCLIPImageEmbedder`——这与 4.2 节的 CLIP 嵌入对上。若某个 target 报 `ModuleNotFoundError`，说明依赖未装全（如 `clip`、`kornia`）。运行结果**待本地验证**（依赖环境齐全时应全部通过）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_state_dict` 要 `strict=False`？

**答案**：[threestudio/models/guidance/stable_zero123_guidance.py:L49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L49)。yaml 蓝图构造出的模型与检查点保存时的模型在键名上允许少量出入（例如 EMA 相关键、训练期 logger 键）。strict=False 使缺键/多键只被收进 `m`/`u` 两个列表（verbose=False 时静默），加载不至于中断；代价是真正的权重错位也会被吞掉，所以 `m` 里不应出现大面积的 UNet 主干键。

**练习 2**：`vram_O=True` 删掉了 VAE 解码器，那 4.2 节将讲到的 `decode_latents` 方法还能用吗？

**答案**：不能。`decode_latents` 调用链是 `model.decode_first_stage → first_stage_model.decode`，而 decoder 已被 `del`。在本仓库的调用路径上 `decode_latents` 从未被调用（见 4.5 节死代码清单），所以删除是安全的；但若照抄这个类去写推理代码，一调用就会 `AttributeError`。

**练习 3**：`cc_projection` 初始化成「前 768 列单位阵、偏置零」有什么好处？

**答案**：见 [extern/ldm_zero123/models/diffusion/ddpm.py:L652-L656](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L652-L656)。这样初始化后该层输出 ≈ CLIP 嵌入本身，相机维度暂不产生任何影响——训练从「等价于没有相机条件」的状态平滑起步，而不是从随机投影开始破坏预训练 CLIP 嵌入的分布。对蒸馏路径而言它随后被检查点权重覆盖，这个初始化主要服务于「从 Zero123 权重继续微调相机条件」的训练场景。

### 4.2 参考图双重嵌入：prepare_embeddings / get_img_embeds

#### 4.2.1 概念说明

Zero123 对参考图（coarse 配置里 `${data.image_path}`，即 u2-l1 预处理产出的 RGBA 图）计算**两种互补的嵌入**：

1. **CLIP 语义嵌入**：ViT-L/14 图像编码器输出 (1,1,768)。它描述「这是什么物体」，分辨率信息已丢失，将来走交叉注意力通道，并作为相机条件的宿主（4.3 节拼接后一起投影）。
2. **VAE 像素潜码**：SD 的 VAE 编码器输出 (1,4,32,32)。它保留全部像素细节（纹理、轮廓、局部形状），将来直接拼进 UNet 输入通道。

参考图在训练全程不变，所以这两个嵌入在 `configure` 阶段**一次性预计算**（`prepare_embeddings` 只被调用一次），之后每步训练只是重复取用——这是免费的缓存优化。

#### 4.2.2 核心流程

```text
configure → prepare_embeddings(image_path)
 ├─ cv2 读 RGBA（BGRA→RGBA）
 ├─ resize 到 256×256，float 化到 [0,1]
 ├─ rgb = rgba_rgb * alpha + (1 - alpha)        # 白底 alpha 合成
 ├─ 存 self.rgb_256: (1,3,256,256)              # 供调试/可视化
 └─ get_img_embeds(rgb_256)
      ├─ img = img*2 - 1                        # [0,1] → [-1,1]（两个编码器都要求）
      ├─ c_crossattn = get_learned_conditioning(img)
      │     └─ FrozenCLIPImageEmbedder.encode → (1,1,768)
      └─ c_concat = encode_first_stage(img).mode()   → (1,4,32,32)
           # 注意：.mode() 取高斯后验均值，且【不乘 scale_factor】
```

#### 4.2.3 源码精读

预处理与双重嵌入的入口：

- [threestudio/models/guidance/stable_zero123_guidance.py:L146-L167](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L146-L167)：`prepare_embeddings`。L150-L151 以 `IMREAD_UNCHANGED` 读四通道图并做 BGRA→RGBA 换序（OpenCV 的历史包袱）；L153-L158 缩放到 256×256；L159 的 `rgb = rgba[..., :3] * rgba[..., 3:] + (1 - rgba[..., 3:])` 是白底合成——Zero123 训练数据没有透明通道，参考图必须落 到确定背景上；L167 调用 `get_img_embeds` 得到 `self.c_crossattn` 与 `self.c_concat` 两个成员，全程只此一次。

`get_img_embeds` 只有四行，但有一个容易看漏的不对称：

- [threestudio/models/guidance/stable_zero123_guidance.py:L169-L178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L169-L178)：`c_crossattn = self.model.get_learned_conditioning(img)`、`c_concat = self.model.encode_first_stage(img).mode()`。**条件潜码没有乘 `scale_factor`**。对照 4.4 节将看到的 `encode_images`（对渲染图）：它走 `get_first_stage_encoding` 会乘 0.18215。条件与目标两条编码路径刻意不对称，复刻的是 Zero123 训练时的行为——extern/zero123.py 里有原作者的 FIXME 注释直接说明了这一点（[extern/zero123.py:L431-L433](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/zero123.py#L431-L433)：encoded latents should be multiplied with scaling_factor, **but zero123 was not trained this way**）。

CLIP 编码器的真身：

- [extern/ldm_zero123/modules/encoders/modules.py:L457-L480](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/modules/encoders/modules.py#L457-L480)：`FrozenCLIPImageEmbedder.preprocess` 把 256×256 输入 bicubic 缩到 224×224（CLIP 原生分辨率）、[-1,1]→[0,1]、再按 CLIP 均值方差归一化；`forward` 调 `encode_image` 得 (B,768)；`encode` 再 `unsqueeze(1)` 成 (B,1,768)——这个「1」就是将来交叉注意力的 token 数：Zero123 的 context 只有**一个**图像 token（对比文本条件的 77 个 token）。

LDM 侧的两个包装：

- [extern/ldm_zero123/models/diffusion/ddpm.py:L777-L790](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L777-L790)：`get_learned_conditioning` 发现 cond_stage_model 有 `encode` 方法就调用之，返回 CLIP 嵌入。
- [extern/ldm_zero123/models/diffusion/ddpm.py:L1057-L1100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L1057-L1100)：`encode_first_stage` 调 `first_stage_model.encode(x)` 返回 `DiagonalGaussianDistribution` 后验对象（不是张量！所以调用侧要么 `.mode()` 要么 `.sample()`）。
- [extern/ldm_zero123/models/diffusion/ddpm.py:L766-L775](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L766-L775)：`get_first_stage_encoding` 对后验 `.sample()` 并乘 `self.scale_factor`（0.18215）——4.4 节 `encode_images` 用的就是它。

#### 4.2.4 代码实践

**实践：给两条编码路径做一张对比表。**

1. 实践目标：弄清「参考图条件编码」与「渲染图目标编码」的异同，理解那个刻意的 scale 不对称。
2. 操作步骤：对照阅读 [threestudio/models/guidance/stable_zero123_guidance.py:L169-L189](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L169-L189)，手工填写下表（答案已给出一半，补全另一半）：

   | 维度 | get_img_embeds（条件） | encode_images（目标） |
   | --- | --- | --- |
   | 输入 | 参考图 256×256 | 渲染图缩放到 256×256 |
   | 调用时机 | configure 一次 | 每个 guidance 步 |
   | 后验取值 | `.mode()` | `.sample()` |
   | 乘 scale_factor | 否 | 是（0.18215） |
   | 输出与去向 | （自己填：形状? 进入哪个通道?） | （自己填：形状? 进入加噪?） |
   | 梯度 | （自己填：有无?） | （自己填：有无?） |
3. 需要观察的现象：填表过程中会追到 `get_first_stage_encoding`（ddpm.py L766-L775）里那行 `self.scale_factor * z`，确认条件路径绕开了它。
4. 预期结果：条件 (1,4,32,32) 无梯度（no_grad 且不训练）、进 c_concat；目标 (B,4,32,32) 带梯度（SDS 要对它求导，经重参数化注入）、进加噪与 MSE。

#### 4.2.5 小练习与答案

**练习 1**：为什么条件潜码用 `.mode()` 而目标潜码用 `.sample()`？

**答案**：条件是「固定的锚」，取后验均值消除随机性，保证同一步里 cond/uncond 两次 UNet 前向的差异只来自条件本身；目标是「被优化的变量」，SDS 的噪声预测比较本就建立在随机采样与加噪之上，`.sample()` 与扩散训练时的用法一致。顺带一提，`.sample()` 在这里处于 no_grad 块外但 latents 本身由 `encode_images` 产生、梯度经重参数化的 `target` 注入（4.4 节），采样随机性不影响梯度通路。

**练习 2**：参考图为什么要缩到 256×256、白底合成，而渲染图（128 或 384 分辨率）也各自缩到 256？

**答案**：Zero123 的 VAE 与 UNet 都在 256×256（潜码 32×32）上训练，任何输入都必须归一到这个工作分辨率。渲染图在 `__call__` 里用 `F.interpolate` 缩放（4.4 节 L275-L277），与数据侧分辨率爬坡（128→384）解耦——先验模型看到的始终是它的原生分辨率。

### 4.3 相机条件构造与 hybrid 注入：get_cond → apply_model → DiffusionWrapper

#### 4.3.1 概念说明

这是本讲的核心模块：**相对相机姿态如何变成 UNet 能吃的条件**。

关键设计一：**条件是相对姿态而非绝对姿态**。Zero123 学的是「从参考相机移动 (Δpolar, Δazimuth) 后看到的图」，与物体在世界中的绝对朝向无关，这使得先验可以迁移到任何物体。

关键设计二：**方位角用 sin/cos 编码**。方位角差是周期量（−180° 与 +180° 是同一个方向），直接用线性值会让模型在边界处断裂；正余弦对把圆周嵌入平面，天然连续。

关键设计三：**Stable 版的第四维是参考视角的绝对极角**。对照同仓库的原版实现（[threestudio/models/guidance/zero123_guidance.py:L215-L225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/zero123_guidance.py#L215-L225)）可以看到精确差异：原版 T 的第四维是 `camera_distances - cond_camera_distance`（相对距离），Stable 版换成了 `deg2rad(90 - cond_elevation_deg)`（参考视角绝对极角）。原版 Zero123 只看相对姿态，无法分辨「从上往下看」还是「从下往上看」（仰角歧义）；Stable Zero123 把参考视角自己的绝对极角也告诉模型，从条件层面消除歧义——这正是 DreamCraft3D 选 Stable 版的原因之一（粗阶段 elevation_range 采样到 [−10°, 45°]，跨了水平线，必须有绝对仰角信息）。

#### 4.3.2 核心流程

T 四元组的构造（输入 elevation/azimuth 来自随机相机 batch，单位度）：

\[
T = \Big(\;
\underbrace{(90-\theta_t)-(90-\theta_c)}_{\text{相对极角}},\;
\underbrace{\sin(\phi_t-\phi_c)}_{\text{方位角差 sin}},\;
\underbrace{\cos(\phi_t-\phi_c)}_{\text{方位角差 cos}},\;
\underbrace{\tfrac{\pi}{180}(90-\theta_c)}_{\text{参考绝对极角}}
\;\Big)
\]

其中 \(\theta\) 为仰角（elevation）、\(\phi\) 为方位角（azimuth）、下标 t/c 表示目标/参考相机。极角 polar = 90° − elevation 是 Zero123 的约定。

```text
get_cond(elevation, azimuth, camera_distances)
 ├─ T = 上面的四元组，形状 (B,) → stack → (B,1,4)
 ├─ clip_emb = cc_projection( cat([c_crossattn.repeat(B,1,1)   # (B,1,768)
 │                                  , T], dim=-1) )             # (B,1,772) → (B,1,768)
 ├─ cond["c_crossattn"] = [ cat([zeros, clip_emb], dim=0) ]     # (2B,1,768)，uncond 在前
 └─ cond["c_concat"]    = [ cat([zeros, c_concat], dim=0) ]     # (2B,4,32,32)，uncond 在前

__call__ 内：
 x_in = cat([latents_noisy]*2)                                  # (2B,4,32,32)
 noise_pred = model.apply_model(x_in, t_in, cond)
   └─ 简单路径: x_recon = self.model(x_noisy, t, **cond)        # → DiffusionWrapper
        └─ hybrid 分支:
             xc = cat([x_noisy, c_concat], dim=1)                # (2B,8,32,32) ← 拼接位置①
             cc = cat(c_crossattn, dim=1)                        # (2B,1,768)
             out = diffusion_model(xc, t, context=cc)            # ← 拼接位置②（交叉注意力）
```

UNet 条件拼接位置示意图（本讲实践任务要求画的图）：

```text
                     ┌────────────────────────────────────────────┐
 参考图 ──CLIP──▶ 768│                                            │
            ╲        │   cat(dim=-1) → (B,1,772)                  │   交叉注意力
 相机 T ──────▶ 4 ───┼─▶ cc_projection (Linear 772→768) ──────────┼─▶ context (2B,1,768)
                     │                                            │   （②号拼接位置）
 目标相机姿态 ────────┘                                            |
 （elev/azimuth 减                                            UNet2DConditionModel
  去参考值而来）                                              ┌──────────────────────┐
                                                             │  输入 xc (2B,8,32,32) │
 渲染图 ─VAE─▶ latents ─加噪─▶ noisy (2B,4,32,32) ────────────▶│  ch 0-3: 带噪潜码      │
                                                             │  ch 4-7: 参考图潜码    │◀── ①号拼接位置
 参考图 ─VAE(.mode(),不缩放)─▶ (1,4,32,32) ─repeat+零uncond────┘  （c_concat 通道拼接）
                                                             │
 每个分辨率层的 CrossAttention 里:  Q=空间特征, K/V=context ────┘
```

#### 4.3.3 源码精读

T 四元组逐行：

- [threestudio/models/guidance/stable_zero123_guidance.py:L212-L224](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L212-L224)：第一项的行内注释 `# Zero123 polar is 90-elevation` 点明极角约定；`[:, None, :]` 把 (B,4) 变 (B,1,4)，与 CLIP 嵌入的 (B,1,768) 在最后一维拼接前对齐序列维。注意 `camera_distances` 出现在签名里（L207）但**从未参与 T 的构造**——Stable 版去掉了距离条件，这是从原版继承下来的形参残留（对照 [threestudio/models/guidance/zero123_guidance.py:L222](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/zero123_guidance.py#L222)，原版在这一行用 `camera_distances - self.cfg.cond_camera_distance`）。

cc_projection 融合与 CFG 双份条件：

- [threestudio/models/guidance/stable_zero123_guidance.py:L226-L236](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L226-L236)：`self.model.cc_projection` 把 CLIP 嵌入复制到 batch 维后与 T 拼接（(B,1,772)）投影回 (B,1,768)。相机条件不是独立的第二条注意力，而是**缝进 CLIP token 里**——这就是 4.1 节那个 `nn.Linear(772, 768)` 的用武之地。
- [threestudio/models/guidance/stable_zero123_guidance.py:L237-L252](https://github.com/deepseek-ai/DreamCraft336d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L237-L252)：两个条件各自在 batch 维前拼一份**全零**作为 uncond 分支：`c_crossattn` 是 `[zeros(2B 前半), clip_emb(后半)]`，`c_concat` 同理。Zero123 的 classifier-free guidance 负向定义 = 「没有图像、没有相机条件」——全零嵌入 + 全零参考潜码，而非文本模型里的空字符串。

apply_model 的分派：

- [extern/ldm_zero123/models/diffusion/ddpm.py:L1130-L1140](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L1130-L1140)：`apply_model` 开头，cond 已是 dict（hybrid 情形）则直接放行；本配置没有 `split_input_params`（分块外推的实验特性），走简单路径。
- [extern/ldm_zero123/models/diffusion/ddpm.py:L1260-L1266](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L1260-L1266)：`x_recon = self.model(x_noisy, t, **cond)`——`self.model` 是 `DiffusionWrapper`，cond dict 解包成 `c_concat=`/`c_crossattn=` 关键字参数。

DiffusionWrapper 的 hybrid 分支——**两个拼接位置的最终落点**：

- [extern/ldm_zero123/models/diffusion/ddpm.py:L1926-L1968](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/models/diffusion/ddpm.py#L1926-L1968)：`DiffusionWrapper.forward` 按 `conditioning_key` 分派；hybrid 分支（L1953-L1956）同时做两件事：`xc = torch.cat([x] + c_concat, dim=1)` 把 4+4 拼成 8 通道（对应 yaml 的 `in_channels: 8`），`cc = torch.cat(c_crossattn, 1)` 合并 context 后以关键字 `context=cc` 喂给 UNet——在 UNet 内部每个分辨率的交叉注意力层里作为 K/V。

原版对照（帮助理解 Stable 版改了什么）：

- [extern/zero123.py:L264-L272](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/zero123.py#L264-L272)：原版 Zero123 推理管线的相机编码 `[deg2rad(elevation), sin(azimuth), cos(azimuth), distance]`——绝对仰角 + 距离，没有「相对极角 + 参考绝对极角」的拆分。
- [extern/zero123.py:L41-L78](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/zero123.py#L41-L78)：`CLIPCameraProjection`——cc_projection 在 diffusers 世界里的同构体，同样 `Linear(768+4, 768)`，佐证「cc = CLIP-camera」的读法。
- [extern/zero123.py:L435-L444](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/zero123.py#L435-L444)：推理管线里 c_concat 的 CFG 拼法与本仓库 get_cond 相同（前半零、后半参考潜码），可互为印证。

#### 4.3.4 代码实践

**实践：手工数值推演 T 四元组（本讲实践任务「画拼接示意图」的数值版）。**

1. 实践目标：给定三个目标相机姿态，算出 T 的四个分量，验证对相对姿态编码的理解。
2. 操作步骤（纸面计算或用 Python 计算器）：设 `cond_elevation_deg=0`、`cond_azimuth_deg=0`（[configs/dreamcraft3d-coarse-nerf.yaml:L106-L107](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L106-L107) 的插值结果），对三组采样计算：
   - (elev, az) = (0°, 0°)：参考视角本身；
   - (elev, az) = (0°, 90°)：绕到正侧方；
   - (elev, az) = (45°, 180°)：俯视 + 背面。
3. 需要观察的现象：第一组 T ≈ (0, sin0, cos0, π/2) = (0, 0, 1, 1.5708)——相对姿态为零时 sin/cos 编码退化为 (0,1)，模型收到的信息是「没动」；第三组第一项 = (90−45)−(90−0) = −45° → 弧度 −0.785。
4. 预期结果：

   | (elev, az) | T1 相对极角(rad) | T2 | T3 | T4 参考极角(rad) |
   | --- | --- | --- | --- | --- |
   | (0°, 0°) | 0.0 | 0.000 | 1.000 | 1.5708 |
   | (0°, 90°) | 0.0 | 1.000 | 0.000 | 1.5708 |
   | (45°, 180°) | −0.785 | 0.000 | −1.000 | 1.5708 |

   注意 T4 恒定不变（它只描述参考相机），T1 的符号：仰角升高 → 相对极角为负（polar 变小）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 uncond 分支用「全零」而不是像文本模型那样用空提示词？

**答案**：Zero123 没有文本输入端。它的条件空间是「CLIP 嵌入 + 参考潜码」，classifier-free guidance 的无条件分支只能定义为该空间的原点：`torch.zeros_like(clip_emb)` 与全零 `c_concat`（[threestudio/models/guidance/stable_zero123_guidance.py:L237-L252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L237-L252)）。训练 Zero123 时就是这么-drop 条件的（cond dropout 置零），推理端必须复现同一种「无条件」。

**练习 2**：如果把方位角差直接用线性值 `az - cond_az`（弧度）而不用 sin/cos，会发生什么？

**答案**：方位角是周期量：+179° 与 −179° 只差 2°，线性编码却给出相差 358° 的两个截然不同的数，模型会在 ±180° 边界看到条件跳变，学不到连续的新视角过渡。sin/cos 对把圆周连续地嵌入 \(\mathbb{R}^2\)，边界自然粘合。粗阶段 `azimuth_range: [-180, 180]` 恰好覆盖全圆，这个编码方式是必需的。

**练习 3**：粗阶段把 `camera_distance_range` 钉在 3.8（与 `cond_camera_distance` 同值）还有意义吗？Stable 版不是不条件距离了吗？

**答案**：对 Zero123 条件本身确实不再必要（T 里没有距离项）。但钉死距离仍有两个作用：其一，渲染相机的视场覆盖（多大物体投影进 20° fovy 的画面）与 Zero123 训练数据的构图一致，避免先验见到「过近的特写/过远的小物」这类分布外输入；其二，与参考视角（`default_camera_distance: 3.8`）一致，使参考图本身落在同一相机分布内。这是保守但稳妥的工程选择。

### 4.4 SDS 复用与系统接线：__call__、update_step 与 guidance_3d 调用点

#### 4.4.1 概念说明

u7-l2 推导的 SDS 梯度 \(\nabla \propto w(t)(\hat\epsilon - \epsilon)\) 在这里**逐行复用**：加噪 → 双倍 batch 一次前向 → CFG 差分 → 乘 \(w(t)=1-\bar\alpha_t\) → `target = (latents - grad).detach()` 的 MSE 重参数化注入。差异只有三处：输入要先过 VAE 编码进潜空间；CFG 的 scale 默认 5（DeepFloyd 是 20）；uncond 的定义来自 4.3 节的全零条件。系统侧则要理解 guidance 主通道与 3D 通道的调用顺序、损失命名与时间步对齐。

#### 4.4.2 核心流程

```text
__call__(rgb=B H W C 渲染图, elevation, azimuth, camera_distances)
 ├─ rgb_BCHW = permute；interpolate 到 256×256
 ├─ latents = encode_images(rgb_BCHW_512)        # VAE，乘 scale_factor → (B,4,32,32)
 ├─ cond = get_cond(elevation, azimuth, ...)     # 4.3 节
 ├─ t ~ U[min_step, max_step]                    # 默认 [20, 980]，配置覆盖为四元组调度
 ├─ no_grad 内：
 │    noise ~ N(0,I)；latents_noisy = add_noise(latents, noise, t)
 │    x_in = cat([latents_noisy]*2)；noise_pred = apply_model(x_in, t_in, cond)
 ├─ (uncond, cond) = chunk(2)
 ├─ noise_pred = uncond + 5.0 * (cond - uncond)          # CFG
 ├─ w = (1 - alphas[t])；grad = w * (noise_pred - noise) # SDS 梯度
 ├─ target = (latents - grad).detach()                   # 重参数化注入
 └─ loss_sds = 0.5 * mse(latents, target, sum) / B
```

#### 4.4.3 源码精读

`__call__` 主体：

- [threestudio/models/guidance/stable_zero123_guidance.py:L255-L300](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L255-L300)：L275-L279 渲染图缩放到 256 后 `encode_images` 进潜空间（`rgb_as_latents` 分支是另一条无需 VAE 的捷径，DreamCraft3D 传 `False`）。注意 L267 的类型注解 `"B 4 64 64"` 是**过时的**（512 时代的遗留），256 输入的真实潜码是 32×32——读代码时不要被注解带偏。L284-L290 时间步从 `[min_step, max_step+1)` 均匀采样；L293-L300 双倍 batch 前向严格对应 4.3 节的 (2B,8,32,32) 输入。

CFG 与梯度（与 u7-l2 完全同构，仅 scale 不同）：

- [threestudio/models/guidance/stable_zero123_guidance.py:L302-L319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L302-L319)：`guidance_scale` 用配置值 5.0（[configs/dreamcraft3d-coarse-nerf.yaml:L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L109)）；`w = (1 - self.alphas[t])`；`target = (latents - grad).detach()` 的 MSE 重参数化注释与 DeepFloyd 版逐字相同——同一骨架的两份拷贝。
- [threestudio/models/guidance/stable_zero123_guidance.py:L321-L328](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L321-L328)：返回值只有 `loss_sd` / `grad_norm` / `min_step` / `max_step` 四项——没有 DeepFloyd 版的 `guidance_eval` 诊断图分支。

update_step 与时间步调度：

- [threestudio/models/guidance/stable_zero123_guidance.py:L330-L340](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L330-L340)：每批训练刷新 `min/max_step`（经 `C()` 四元组插值）与可选 `grad_clip`（默认 None，不裁剪）。
- [configs/dreamcraft3d-coarse-nerf.yaml:L110-L111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L110-L111)：`[0, 0.7, 0.2, 200]` / `[0, 0.85, 0.5, 200]`——与主通道（L98-L99）**逐字相同**。两个扩散先验在同一步加同样水平的噪声，两路梯度信号在噪声尺度上对齐（u7-l2 提过的「严格对齐」的落点就在这两行配置）。

系统侧调用点：

- [threestudio/systems/dreamcraft3d.py:L191-L223](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L191-L223)：guidance 子步内先调主通道 `self.guidance(...)`（geometry 阶段法向子步喂 `comp_normal`），随后 L209-L218 调 `self.guidance_3d(out["comp_rgb"], **batch, ...)`——**3D 通道永远吃 RGB**（Zero123 的先验建立在 RGB 视图上，不是法向图）。L219-L223 把返回项改名：`loss_sd` → `set_loss("3d_sd")`，权重查表 `lambda_3d_sd`（[configs/dreamcraft3d-coarse-nerf.yaml:L127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L127)，0.1，与 `lambda_sd` 同值——两个先验话语权相等）。

配置段全景：

- [configs/dreamcraft3d-coarse-nerf.yaml:L101-L111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L101-L111)：`guidance_3d_type: "stable-zero123-guidance"` 触发注册查找；`cond_image_path/elevation/azimuth/camera_distance` 四行全部用 `${data...}` 插值绑定到数据模块的参考相机参数——**参考相机在数据侧与先验侧必须是一个定义**，改动 `default_elevation_deg` 会同时改参考 batch 与 Zero123 的条件基准。

#### 4.4.4 代码实践

**实践：形状全链路推演 + 时间步对齐验证。**

1. 实践目标：不看运行结果，纯静态推演一次 guidance 步中所有张量形状；并验证双通道时间步对齐。
2. 操作步骤：
   - 按 coarse 阶段配置（batch_size 1、分辨率 128）从 `out["comp_rgb"]` 出发写下每步形状：`comp_rgb (1,128,128,3)` → permute → interpolate 256 → `encode_images` → `latents (1,4,32,32)` → `x_in (2,4,32,32)` → `apply_model` 输入 `xc (2,8,32,32)`、`context (2,1,768)` → `noise_pred (2,4,32,32)` → chunk 后各 `(1,4,32,32)`；
   - 用 Python 算 `[0, 0.7, 0.2, 200]` 在 step=0 与 step=200 处的 `C()` 值（u8-l1 讲过的线性插值：0 步时 min_percent=0.7，200 步后=0.2），换算成 timestep 区间 [700,850]→[200,500]；
   - 对照 [configs/dreamcraft3d-coarse-nerf.yaml:L98-L99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L98-L99) 确认两个通道完全一致。
3. 需要观察的现象：形状链条在 `xc (2,8,32,32)` 处翻倍且变 8 通道——这是 4.3 节两张拼图在数字上的体现。
4. 预期结果：得到一张 10 行左右的形状表；`C()` 计算与 u7-l2 第 4.4 节的结论一致（同一组四元组）。若想实际打印，可在 `get_cond` 返回前加一行日志（改动仅限本地实验，勿提交）：`threestudio.info(f"T={T.shape} clip={clip_emb.shape} concat={cond['c_concat'][0].shape}")`，运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Zero123 的 `guidance_scale` (5) 远小于 DeepFloyd 的 (20)？

**答案**：两个模型的 CFG 训练强度与条件可靠性不同。Zero123 的条件（参考图 + 相对姿态）信息量大、指向精确，小 scale 的差分已足够强；DeepFloyd 只有文本，需要更大 scale 才能把语义「压」进梯度。scale 过大会放大高频伪影（u7-l2 综合实践的同一结论在 3D 通道更明显，因为视图条件差分本身更尖锐）。实际权重还叠加了 `lambda_3d_sd: 0.1` 的损失端缩放，两级旋钮共同决定通道强度。

**练习 2**：geometry 阶段主通道可能吃 `comp_normal`，为什么 3D 通道不用改？

**答案**：[threestudio/systems/dreamcraft3d.py:L192-L195](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L192-L195) 的分支只作用于 `guidance_inp`；L213 写死 `self.guidance_3d(out["comp_rgb"], ...)`。Zero123 的先验是「RGB 视图的合理性」，法向图不在它的训练分布里，喂进去等于给裁判看它看不懂的卷子。

### 4.5 边界与死代码辨析：extern/ldm_zero123/guidance.py 与配置残留

#### 4.5.1 概念说明

vendored（整目录拷入的第三方代码）仓库的特点：大部分文件不在你的执行路径上，但都在你的工作区里。初学者最容易在这类目录里迷路，把无人调用的实验代码当成关键链路。本模块把本讲涉及的两块「边界代码」与四处配置残留一次性辨析清楚，避免后续阅读踩坑。

#### 4.5.2 核心流程

判别一段 vendored 代码是否在执行路径上的方法（u1-l3 讲过 grep + 导入链核对）：

```text
1. grep 它的模块名 → 找到导入者
2. 导入者是注册组件吗？→ 查 @threestudio.register
3. 注册名出现在任何 configs/*.yaml 吗？→ 都出现才是活代码
```

#### 4.5.3 源码精读

第一块边界代码：`extern/ldm_zero123/guidance.py`——仓库内**没有任何文件导入它**（grep `extern.ldm_zero123.guidance` 零命中）：

- [extern/ldm_zero123/guidance.py:L11-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/guidance.py#L11-L21)：`GuideModel` 抽象基类，定义 `preprocess` / `compute_loss` 两个接口。
- [extern/ldm_zero123/guidance.py:L59-L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/guidance.py#L59-L110)：`Guider.modify_score`——Zero123 原作者做「采样过程中途挂一个可微判别器（如图像分类器）做分类器引导」的实验代码：把噪声预测反解到 x0、VAE 解码成图、喂 guide_model 算损失、对 x_in 求梯度后按 `e_t - sqrt_1ma * correction` 修正噪声。DreamCraft3D 的 SDS 蒸馏路径完全不经过这里，它只是随 `ldm_zero123` 目录整体拷入的历史遗留。**读它唯一的价值**是对照理解「分类器引导（改噪声预测）」与「得分蒸馏（把差分当梯度回传）」是两种不同的先验注入方式。

第二块边界代码：`extern/zero123.py`——只被 [threestudio/models/guidance/zero123_unified_guidance.py:L24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/zero123_unified_guidance.py#L24) 导入（`from extern.zero123 import Zero123Pipeline`），而 `zero123-unified-guidance` 未出现在任何 DreamCraft3D 配置里。它在 4.3 节的价值是**原版对照**（CLIPCameraProjection 与绝对姿态编码），不是执行路径。

四处配置/形参残留（代码接受但不生效）：

| 残留 | 位置 | 说明 |
| --- | --- | --- |
| `cond_camera_distance` | [threestudio/models/guidance/stable_zero123_guidance.py:L85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L85)、[configs/dreamcraft3d-coarse-nerf.yaml:L108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L108) | T 里没有距离项；原版 zero123_guidance.py 才消费它 |
| `camera_distances` 形参 | [threestudio/models/guidance/stable_zero123_guidance.py:L207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L207) | get_cond 接收后未使用（`**kwargs` 兼容系统侧统一签名） |
| `half_precision_weights` | [threestudio/models/guidance/stable_zero123_guidance.py:L92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L92) | 定义后无引用；weights_dtype 已硬编码 float32 |
| `decode_latents` | [threestudio/models/guidance/stable_zero123_guidance.py:L191-L199](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L191-L199) | 无调用者；且 vram_O 已删 VAE decoder，调用必崩；注解 "B 3 512 512" 亦过时（32×32 潜码解码应为 256×256） |

另注：任务清单中出现的 `extern/ldm_zero128/` 目录在仓库中**不存在**（实际为 `extern/ldm_zero123/`），本文所有引用均以真实路径为准。

#### 4.5.4 代码实践

**实践：用 grep 三步法自查一块 vendored 代码的生死。**

1. 实践目标：亲手验证「`extern/ldm_zero123/guidance.py` 不在执行路径上」这一结论。
2. 操作步骤：
   - `grep -rn "ldm_zero123.guidance" --include="*.py"` → 期望零命中（无导入者）；
   - `grep -rn "zero123-unified-guidance" configs/` → 期望零命中（其唯一潜在消费者未被配置）；
   - `grep -rn "stable-zero123-guidance" configs/` → 命中 [configs/dreamcraft3d-coarse-nerf.yaml:L101](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L101) 等三份粗/几何阶段配置（texture 阶段 guidance_3d 留空，u2-l3）。
3. 需要观察的现象：三条命令的命中数分别为 0 / 0 / ≥3。
4. 预期结果：确认 stable-zero123-guidance 是活代码、ldm_zero123/guidance.py 是死代码、zero123-unified-guidance 是休眠代码（注册了但无配置使用）。

#### 4.5.5 小练习与答案

**练习 1**：`Guider.modify_score`（分类器引导）与 SDS（得分蒸馏）对扩散模型的使用方式有何本质区别？

**答案**：[extern/ldm_zero123/guidance.py:L59-L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/extern/ldm_zero123/guidance.py#L59-L110) 的分类器引导发生在**采样过程内部**：修改每一步的噪声预测 `e_t`，需要完整跑几十步去噪才能得到一张图，目的是「生成更好的图」。SDS 发生在**优化循环里**：每步只做一次前向拿到评分方向，不采样、不产图，目的是「把评分方向作为梯度改进三维场景」。前者改输出，后者改参数。

**练习 2**：如果想让 Stable Zero123 通道也支持 `grad_clip`，需要改几处？

**答案**：两处都不用改代码——[threestudio/models/guidance/stable_zero123_guidance.py:L89-L91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L89-L91) 的注释里就躺着现成的四元组示例 `[0, 2.0, 8.0, 1000]`，在配置里加 `system.guidance_3d.grad_clip=[0,2.0,8.0,1000]` 即可；`update_step`（L334-L335）会经 `C()` 每步刷新 `grad_clip_val`，`__call__`（L312-L313）已经写好了 clamp 分支。这是「代码存在 ≠ 配置启用」的又一例（u6-l4 的结论）。

## 5. 综合实践

**综合实践：完成规格任务——追踪 ckpt + yaml 的完整加载链路，并画出参考图嵌入与目标相机条件在 UNet 中的拼接位置示意图。**

本实践把五个模块串成一份可留存的笔记，产出两件东西：一张加载链路图、一张条件拼接示意图。

1. **加载链路追踪**。先运行 4.1.4 的 `trace_zero123_load.py` 得到四行 target 映射表；再补上人工部分——在纸上画出完整调用树：
   `launch.py --train`（u1-l4）→ `threestudio.find("stable-zero123-guidance")`（u3-l1 注册表）→ `BaseModule.__init__` → `configure`（[threestudio/models/guidance/stable_zero123_guidance.py:L99-L139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L99-L139)）→ `OmegaConf.load` 结构 yaml → `load_model_from_config` → `instantiate_from_config` 反射 `LatentDiffusion` →（内部再反射 UNetModel / AutoencoderKL / FrozenCLIPImageEmbedder）→ `load_state_dict(strict=False)` → EMA 拷贝 → 删 VAE decoder → `eval().to(device)` → 全参数冻结 → 预计算参考图嵌入。在树上标注三处易错点：`strict=False` 的意义、EMA 为什么拷贝后删除、删 decoder 与 `decode_latents` 的冲突。

2. **画条件拼接示意图**。以 4.3.2 的 ASCII 图为底稿，自己重画一张并补上数值（batch=1 时）：参考图两路（CLIP → (1,1,768)；VAE `.mode()` → (1,4,32,32)）、相机 T (1,1,4)、cc_projection (772→768)、CFG 双份复制后的 (2,1,768) 与 (2,4,32,32)、UNet 输入 (2,8,32,32) 与 context。两个拼接位置用①②标出：①输入通道维（对应 yaml `in_channels: 8`），②交叉注意力 K/V（对应 `context_dim: 768`）。

3. **对照源码自检**：图上每条边都要能在源码里指出行号（4.2.3 与 4.3.3 的引用清单就是评分标准）。特别注意别把 c_concat 画进交叉注意力、别把 CLIP 嵌入画进通道拼接——这是初学者最常见的两张拼图错位。

4. **（可选，需权重与 GPU，待本地验证）**形状实拍：加载真实的 `stable_zero123.ckpt`，在 `get_cond` 里临时打印 `T.shape / clip_emb.shape / cond["c_concat"][0].shape`，跑一个 batch=1 的假前向（随机 256×256 渲染图 + 随机 elevation/azimuth），核对与推演一致。

预期结果：两图 + 一张行号对照表。这张示意图同时是通往 u7-l4 的跳板——BSD 引导里 Stable Diffusion 的条件注入（文本 context 走交叉注意力、无 c_concat）是本图去掉①号拼接、把②号换成 77×768 文本嵌入后的退化版。

## 6. 本讲小结

- Stable Zero123 是**视图条件**的潜空间扩散先验：参考图的 CLIP 嵌入 (1,1,768) 与相机条件 T 四元组经 `cc_projection`（Linear 772→768，恒等初始化）缝成一个 token 走交叉注意力；参考图的 VAE 潜码 (1,4,32,32) 与带噪潜码拼成 8 通道 UNet 输入——hybrid 条件 = crossattn + concat 双通道。
- 相机条件是**相对姿态**：T =（相对极角， sin 方位角差， cos 方位角差， 参考绝对极角）。前三维与原版 Zero123 相同，第四维由原版的「相对距离」换成「参考视角绝对极角」，消除仰角歧义；`cond_camera_distance` 与 `camera_distances` 形参是残留，不再生效。
- 加载链路是第二套实例化机制：yaml 的 `target` 字符串经 `importlib` 反射出 `LatentDiffusion`（区别于 threestudio 的 `__modules__` 注册表），随后 `strict=False` 灌权重、EMA 拷贝、`vram_O` 删 VAE decoder、全参数冻结。
- 条件与目标的 VAE 编码**刻意不对称**：条件潜码 `.mode()` 不乘 scale_factor，目标潜码 `.sample()` 乘 0.18215——复刻 Zero123 训练行为（extern/zero123.py 的 FIXME 注释为证）。
- SDS 骨架从 u7-l2 原样复用（加噪 → 2B 前向 → CFG → \(w(t)\) → 重参数化注入），差异仅：输入需 VAE 编码、`guidance_scale` 5、uncond = 全零嵌入 + 全零参考潜码；系统侧 3D 通道永远吃 `comp_rgb`，损失名映射 `3d_sd`，时间步四元组与主通道逐字对齐。
- vendored 目录要辨生死：`extern/ldm_zero123/guidance.py`（分类器引导实验）无人导入；`extern/zero123.py` 仅被未启用的 zero123-unified-guidance 使用——它们是对照阅读材料，不是执行路径；`extern/ldm_zero128/` 在仓库中不存在。

## 7. 下一步学习建议

本讲补齐了粗阶段双引导的另一半，到此「冻结裁判」式的引导（DeepFloyd、Zero123）已全部讲完。下一讲 **u7-l4（BSD 引导（上）：三管线结构与 LoRA 装配）** 将第一次让扩散模型本身变成可训练对象：解读 `stable-diffusion-bsd-guidance` 的 `pipe / pipe_lora / pipe_fix` 三条 StableDiffusionPipeline、`train_unet / train_unet_lora` 两个可训练 UNet 与 `set_up_lora_layers` 的 LoRA 注入。阅读时建议带着本讲的两处对比去看：其一，BSD 的条件注入是本讲示意图的「退化版」（文本 context、无 c_concat 拼接）；其二，本讲的 `requires_grad_(False)` 在 BSD 里被系统地反转，哪些层保持冻结、哪些进 LoRA，正是下一讲的核心问题。

延伸阅读：Zero123 原论文（arXiv:2303.11328）第 3 节视图条件化的表述、Stable Zero123 的模型卡（HuggingFace stabilityai/stable-zero123，关于仰角歧义修正的说明）；代码层面可把 [threestudio/models/guidance/zero123_guidance.py:L215-L225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/zero123_guidance.py#L215-L225) 与本讲主角的 `get_cond` 并排 diff，两处差异（第四维、guidance 默认值）就是原版与 Stable 版的全部本质区别。
