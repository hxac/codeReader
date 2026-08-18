# u6-l1 dreamcraft3d-system 的 configure 与双引导组装

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `ImageConditionDreamFusion` 的 `Config` 中 `stage`、`freq`、`guidance_3d_type/guidance_3d`、`control_guidance_type/control_guidance` 等新增字段的含义与默认值。
2. 讲清楚 `configure()` 在父类 `BaseLift3DSystem` 组装的四件套（geometry/material/background/renderer）之外，又追加了哪些组件：`guidance`、可选的 `guidance_3d`、`prompt_processor/prompt_utils`、`perceptual_loss`、可选的 `control_guidance` 一族。
3. 理解「双引导通道」设计：`guidance`（2D 文本扩散先验）与 `guidance_3d`（视图条件 3D 先验）各自在 `training_substep` 的哪里被消费、损失如何汇入总损失。
4. 理解 `stage` 字段如何像开关一样驱动 `forward`、RGB 损失形式、正则块、渲染类型与可视化采样等多处行为分支。
5. 画出 dreamcraft3d-system 的完整模块依赖图，并对照 coarse-nerf 与 texture 两份配置标出哪些分支被激活。

## 2. 前置知识

本讲是「读系统总装」的第一讲，需要以下已建立的认知（来自前置讲义）：

- **注册机制**（u3-l1）：`X_type` 的值是注册名，兄弟段 `X` 是参数 dict，`threestudio.find(X_type)(cfg.X)` 完成实例化。
- **BaseSystem / BaseLift3DSystem**（u3-l3）：`BaseSystem.__init__` 在构造时调用 `configure()`；`BaseLift3DSystem.configure` 组装 geometry、material、background、renderer 四件套，renderer 通过构造参数持有前三者的引用。`geometry_convert_from` 的跨阶段衔接也在其中。
- **材质与背景**（u5-l5）：四阶段全部用 `no-material`（几何特征直接输出 RGB）与 `solid-color-background`，背景经 over 合成进 `comp_rgb`，但参考图损失用 `comp_rgb_bg` 免疫背景监督。

本讲还要用到的两个直觉概念：

- **得分蒸馏引导（guidance）是什么**：扩散模型不直接生成图片，而是对「当前 3D 场景渲染出的图」打分，给出一个让渲染图「更像真图」的梯度。本讲不推导公式（u7-l2 精讲），只需记住调用形态：`guidance(render_out["comp_rgb"], prompt_utils, **batch)` 返回一个 dict，其中 `loss_` 开头的项会被系统收集加权。粗略地说，SDS 类梯度形如 \( w(t)\cdot(\epsilon_{\text{cond}}-\epsilon_{\text{uncond}}) \)，即条件噪声预测与无条件噪声预测的加权差。
- **为什么需要两个引导**：单一文生图先验不知道「背面长什么样」，容易训出多张脸的 Janus 问题。DreamCraft3D 粗阶段同时挂两个引导——`guidance`（DeepFloyd，文本先验）负责语义，`guidance_3d`（Stable Zero123，视图条件先验）负责「从参考图这个角度看应该是这样」的多面一致性。这就是**双通道（dual guidance）**。

术语提示：`guidance_3d` 里的 "3d" 不是指三维卷积，而是指「视图条件的多视角一致性先验」（以相对相机姿态为条件）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 本讲主角。`ImageConditionDreamFusion` 系统：Config、configure、training_step/training_substep、各 stage 分支 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `BaseSystem`（构造顺序、true_global_step）与 `BaseLift3DSystem.configure`（四件套组装，本讲的「上半段」） |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段配置：DeepFloyd + Zero123 双引导全开 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | 纹理阶段配置：BSD 引导，guidance_3d 与 control_guidance 均被注释 |
| [threestudio/utils/perceptual/perceptual.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py) | `perceptual-loss` 注册名所在，configure 中被硬编码查找 |
| [threestudio/models/prompt_processors/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py) | `PromptProcessor.__call__` 返回 `prompt_utils` 的结构 |
| [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py) | BSD 引导，其 Config 含 `only_pretrain_step` 等字段，会反向影响系统的调度 |

## 4. 核心概念与源码讲解

本讲把 `dreamcraft3d.py` 拆成 5 个最小模块：Config 数据类、configure 组装流水线、双引导通道的运行时消费、control_guidance 可选正则、stage 驱动的行为分支。

### 4.1 Config 数据类：系统级开关的总账本

#### 4.1.1 概念说明

`ImageConditionDreamFusion` 的 `Config` 继承自 `BaseLift3DSystem.Config`。继承意味着：yaml 里 `system:` 段既能写父类的字段（geometry_type、material_type、renderer_type、guidance_type、prompt_processor_type……），也能写本类新增的字段。这个数据类就是「这台训练机器有哪些旋钮」的权威清单——**读任何一个 threestudio 系统，先读它的 Config**。

新增字段回答四个问题：

- 现在是哪个阶段？（`stage`）
- 参考图监督和扩散引导怎么轮流来？（`freq`）
- 要不要第二路 3D 先验？（`guidance_3d_type` + `guidance_3d`）
- 要不要 ControlNet 编辑正则？（`control_guidance_type` + `control_guidance` + `control_prompt_processor_type` + `control_prompt_processor`）

#### 4.1.2 核心流程

```text
yaml 的 system: 段
   │ (parse_structured 严格校验)
   ▼
ImageConditionDreamFusion.Config 实例 self.cfg
   ├── stage: 'coarse' | 'geometry' | 'texture'   → 驱动多处行为分支（见 4.5）
   ├── freq: dict                                   → 训练调度参数包（见 u6-l2）
   ├── guidance_3d_type/guidance_3d                 → 空字符串 = 不组装第二路引导
   ├── use_mixed_camera_config: bool                → 多卡混合内参实验开关
   ├── control_guidance_type 等 4 个字段            → 空字符串 = 不组装正则引导
   └── visualize_samples: bool                      → 验证时是否采样扩散模型输出
```

注意一个模式：**可选组件用「类型字符串是否为空」表达**。`guidance_3d_type` 与 `control_guidance_type` 默认都是 `""`，configure 里据此决定组装还是置 `None`/跳过。这与 `guidance_type`（必填）形成对照——主引导是必需品，另外两路是可选件。

#### 4.1.3 源码精读

类的注册与 Config 定义（注释还说明了论文中 coarse 与 geometry 合称 geometry-sculpting）：

- [threestudio/systems/dreamcraft3d.py:21-36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L21-L36)：`@threestudio.register("dreamcraft3d-system")` 注册类；`Config` 继承 `BaseLift3DSystem.Config` 并新增 9 个字段。`stage` 默认 `"coarse"`，`freq` 默认空 dict。

对照两份配置看这些字段的实际取值：

- [configs/dreamcraft3d-coarse-nerf.yaml:41-43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L41-L43)：`system_type: "dreamcraft3d-system"`、`stage: coarse`。四份阶段配置共用同一个系统注册名，只换插件与 `stage` 值。
- [configs/dreamcraft3d-texture.yaml:42-43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L42-L43)：`stage: texture`，且 `use_mixed_camera_config` 从 data 段插值继承（默认 false）。

`freq` 在两份配置中的内容（本讲只看它有哪些键，调度语义留给 u6-l2）：

- [configs/dreamcraft3d-coarse-nerf.yaml:113-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L113-L118)：`n_ref: 2`、`ref_only_steps: 0`、`ref_or_guidance: "alternate"`、`no_diff_steps: 0`、`guidance_eval: 0`。
- [configs/dreamcraft3d-texture.yaml:111-116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L111-L116)：同样的 `alternate` 与 `n_ref: 2`，但 `no_diff_steps: -1`（配合 `true_global_step > no_diff_steps` 的判断，-1 意味着从第 0 步起扩散引导即可生效；coarse 的 0 则挡住第 0 步）。

#### 4.1.4 代码实践

1. **实践目标**：不用下载任何模型权重，验证 Config 字段清单与两份 yaml 的对应关系。
2. **操作步骤**：在仓库根目录运行以下「示例代码」（只需 `pip install -e .` 级别的依赖，不需要 GPU）：

```python
# 文件名建议：inspect_system_cfg.py（示例代码）
import dataclasses
import threestudio  # 触发全部注册
from threestudio.systems.dreamcraft3d import ImageConditionDreamFusion

for f in dataclasses.fields(ImageConditionDreamFusion.Config):
    print(f"{f.name:35s} default={f.default!r}")
```

3. **需要观察的现象**：打印出的字段分为两批——先父类（geometry_type、guidance_type、prompt_processor_type、optimizer……）后本类新增（stage、freq、guidance_3d_type……）。
4. **预期结果**：共约 30 个字段；`guidance_3d_type`、`control_guidance_type`、`control_prompt_processor_type` 默认均为 `""`。再打开 texture yaml 对照：其中 `guidance_3d` 段整段被注释（[configs/dreamcraft3d-texture.yaml:88-98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L88-L98)），所以回落到默认 `""`，第二路引导不会组装。
5. 本脚本只做反射，不实例化模型，CPU 即可运行（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果某配置漏写 `stage`，会发生什么？
**答案**：不会报错，`stage` 取默认值 `"coarse"`（[threestudio/systems/dreamcraft3d.py:27](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L27)）。训练会按 coarse 分支走：体渲染正则生效、RGB 损失用 MSE。这也是为什么四份阶段配置都必须显式写 `stage`。

**练习 2**：`guidance_3d_type` 与 `guidance_3d` 两个字段为什么必须成对出现？
**答案**：前者是注册名字符串（决定用哪个类），后者是构造参数 dict。configure 中 `threestudio.find(self.cfg.guidance_3d_type)(self.cfg.guidance_3d)` 两者同时消费；只写参数不写类型，参数会被静默忽略（类型为空 → 组装为 `None`）。

**练习 3**：`freq` 为什么声明为 `dict` 而不是像其他字段那样的强类型 dataclass？
**答案**：`freq: dict = field(default_factory=dict)`（[threestudio/systems/dreamcraft3d.py:28](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L28)）是弱类型容器，`parse_structured` 不会校验其中的键。各阶段按需放键（geometry 阶段才需要 `n_rgb`），代价是键名拼错只会在运行时以 KeyError 暴露。

### 4.2 configure()：六类组件的组装流水线

#### 4.2.1 概念说明

`configure()` 是系统的「总装车间」。它分两段：第一段 `super().configure()` 完成渲染四件套；第二段是本类追加的扩散引导侧组件。**整条流水线不加载任何数据、不前向任何张量，只负责把对象树建起来**——真正的运行逻辑在 training_step（u6-l2 详讲）。

追加的组件可分三类：

1. **必装**：`guidance`（主引导）、`prompt_processor`（文本编码器封装）与 `prompt_utils`（其输出）、`perceptual_loss`（VGG 感知损失，硬编码查找）。
2. **选装**：`guidance_3d`（视图条件先验）、`control_guidance` 一族（ControlNet 编辑正则 + 配套提示词处理器）。
3. **父类已装**：geometry、material、background、renderer。

#### 4.2.2 核心流程

```text
BaseSystem.__init__（构造即调用 configure，见 base.py:45）
   ▼
ImageConditionDreamFusion.configure()
   ├─ ① super().configure()
   │     ├─ （可选）geometry_convert_from 跨阶段转换
   │     ├─ self.geometry  = find(geometry_type)(...)
   │     ├─ self.material  = find(material_type)(...)
   │     ├─ self.background= find(background_type)(...)
   │     └─ self.renderer  = find(renderer_type)(..., geometry=, material=, background=)
   ├─ ② self.guidance       = find(guidance_type)(guidance)          # 主引导（必装）
   ├─ ③ self.guidance_3d    = find(guidance_3d_type)(guidance_3d) 或 None
   ├─ ④ self.prompt_processor = find(prompt_processor_type)(...)
   │    self.prompt_utils     = self.prompt_processor()               # 缓存的嵌入输出
   ├─ ⑤ self.perceptual_loss = find("perceptual-loss")({})           # 硬编码注册名
   └─ ⑥ 若 control_guidance_type != ""：
         self.control_guidance / control_prompt_processor / control_prompt_utils
```

#### 4.2.3 源码精读

父类四件套（本讲的「上半段」，细节在 u3-l3 已讲）：

- [threestudio/systems/base.py:243-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L297)：`BaseLift3DSystem.configure`。geometry 的 `geometry_convert_from` 分支与常规分支之后，L288-L297 依次实例化 material、background、renderer，并把前三者作为构造参数注入 renderer。
- [threestudio/systems/base.py:35-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L35-L48)：`BaseSystem.__init__` 中 L45 调用 `self.configure()`——这就是「new 系统对象时组装就已完成」的原因。

本类的追加组装（本讲核心代码，建议逐行读完）：

- [threestudio/systems/dreamcraft3d.py:40-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L40-L63)：`configure()` 全文。逐段说明：
  - L42 `super().configure()`：先建四件套（此时 self.geometry 等已可用）。
  - L43 主引导：`find(guidance_type)(guidance)`。coarse 阶段是 `deep-floyd-guidance`，texture 阶段是 `stable-diffusion-bsd-guidance`。
  - L44-49 第二路引导：类型串非空才 find，否则 `self.guidance_3d = None`——texture 阶段走的就是 None 分支。
  - L50-53 提示词处理器：`prompt_processor` 负责把文本变成嵌入；紧接着 `self.prompt_utils = self.prompt_processor()` 把缓存好的嵌入输出取出来存成属性。
  - L55-56 感知损失：注意这里是**硬编码**注册名 `"perceptual-loss"`、传空配置 `{}`——不像其他组件由 yaml 决定，这个组件没有配置入口。
  - L58-63 ControlNet 正则一族：仅当 `control_guidance_type` 非空才组装，且引导与其提示词处理器**同时**创建。

`prompt_utils` 是什么：

- [threestudio/models/prompt_processors/base.py:504-516](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L504-L516)：`PromptProcessor.__call__` 返回 `PromptProcessorOutput`，含正向/负向嵌入、视角相关嵌入（`text_embeddings_vd`）与 perp-neg 参数。它是个普通可调用对象，调用一次拿同一份缓存结果。

感知损失组件本体：

- [threestudio/utils/perceptual/perceptual.py:15-30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/perceptual/perceptual.py#L15-L30)：`perceptual-loss` 的注册与实现——内部包一个 VGG 风格的 `PerceptualLoss`，`__call__(x, y)` 返回两张图的感知距离（裁剪版 LPIPS）。

两份配置组装结果的直接对照（引号内即注册名）：

| 组件属性 | coarse-nerf | texture |
| --- | --- | --- |
| geometry | `implicit-volume` | `tetrahedra-sdf-grid`（经 geometry_convert_from 转换初始化） |
| material / background | `no-material` / `solid-color-background` | 同左 |
| renderer | `nerf-volume-renderer` | `nvdiff-rasterizer` |
| guidance | `deep-floyd-guidance`（[yaml:94-99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L94-L99)） | `stable-diffusion-bsd-guidance`（[yaml:78-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L78-L86)） |
| guidance_3d | `stable-zero123-guidance`（[yaml:101-111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L101-L111)） | `None`（整段被注释） |
| prompt_processor | `deep-floyd-prompt-processor` | `stable-diffusion-prompt-processor` |
| perceptual_loss | 装了但粗阶段不用 | 装了，texture 正则分支可用 |
| control_guidance 一族 | 不装 | 不装（注释掉，[yaml:100-109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L100-L109)） |

注意 prompt_processor 与 guidance 的**词表必须配套**：coarse 两者的 `pretrained_model_name_or_path` 都是 `DeepFloyd/IF-I-XL-v1.0`（[yaml:88-96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L96)），texture 都是 `stabilityai/stable-diffusion-2-1-base`（[yaml:71-81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L81)）。文本嵌入要喂给对应的 UNet，词表/维度不匹配会直接报错（详见 u7-l1）。

#### 4.2.4 代码实践

1. **实践目标**：不加载大模型，打印两份配置在 `configure()` 各分支上的「装配清单」，验证上表。
2. **操作步骤**：运行以下「示例代码」（在仓库根目录；依赖 torch/omegaconf，无需 GPU 与权重下载）：

```python
# 文件名建议：dryrun_assemble.py（示例代码）
import threestudio
from threestudio.utils.config import load_config

CASES = {
    "coarse-nerf": "configs/dreamcraft3d-coarse-nerf.yaml",
    "texture":     "configs/dreamcraft3d-texture.yaml",
}
for name, path in CASES.items():
    cfg = load_config(path, [
        "system.prompt_processor.prompt=a hamburger",   # 必填 ???
        "system.geometry_convert_from=outputs/x/ckpts/last.ckpt",  # texture 必填 ???
    ])
    s = cfg.system  # ExperimentConfig.system 是 dict
    print(f"== {name} | stage={s['stage']}")
    for key in ["geometry_type", "renderer_type", "guidance_type",
                "guidance_3d_type", "prompt_processor_type",
                "control_guidance_type"]:
        print(f"   {key:25s} -> {s[key]!r}")
```

3. **需要观察的现象**：`load_config` 内部会执行 `OmegaConf.resolve` 与 `__post_init__`，因此会顺带在 `outputs/` 下创建试验目录（副作用，u2-l2 讲过）；终端打出两份配置的分支取值。
4. **预期结果**：coarse-nerf 的 `guidance_3d_type` 为 `'stable-zero123-guidance'`；texture 的为 `''`、`control_guidance_type` 为 `''`。**不要**在此脚本里调用 `threestudio.find(s['guidance_type'])(s['guidance'])`——那会触发 DeepFloyd/SD 权重下载（数 GB）。
5. 若本机未装依赖，此脚本为「待本地验证」；配置取值可先靠读 yaml 确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `perceptual_loss` 不像 guidance 那样由 yaml 配置？
**答案**：它是内部实现细节（VGG 感知距离），没有可调项暴露给用户的价值；代码用固定注册名 `"perceptual-loss"` 加空配置 `{}` 直接组装（[dreamcraft3d.py:55-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L55-L56)）。这是「注册机制」与「硬编码」的折中：仍走 find（可被 custom_import 覆盖），但不占配置面。

**练习 2**：`configure()` 里 `self.prompt_utils = self.prompt_processor()` 之后，`training_substep` 里又写了一次 `prompt_utils = self.prompt_processor()`（L125）。重复了吗？
**答案**：没重复出错误——`PromptProcessor` 构造时（`configure` 阶段）已算好嵌入并缓存，`__call__` 只是取缓存输出（[prompt_processors/base.py:504-516](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L504-L516)），两次调用返回等价结果。configure 里的属性赋值更多是「留一份全局引用」的习惯写法。

**练习 3**：若把 texture yaml 的 `guidance_3d` 注释段取消注释但 `guidance_3d_type` 仍注释着，组装结果？
**答案**：`guidance_3d_type` 为默认 `""` → `self.guidance_3d = None`，参数段被完全忽略；训练时 `if self.guidance_3d is not None` 分支不进，不会报错但也没有 3D 先验梯度。这是「静默失效」，排查配置问题时要警惕。

### 4.3 双引导通道：guidance 与 guidance_3d 的运行时消费

#### 4.3.1 概念说明

组装只是把两个扩散模型挂到系统上；真正「用」它们发生在 `training_substep(guidance="guidance")` 子步里。两个通道的分工：

- **`guidance`（2D 文本通道）**：吃渲染 RGB（geometry 阶段也吃渲染法向）+ 文本嵌入，产出语义先验梯度。coarse 用 DeepFloyd SDS，texture 用 BSD（BSD 还会反过来更新扩散模型自身，见 u7-l5）。
- **`guidance_3d`（视图条件通道）**：吃渲染 RGB + **batch 里的相机参数**（不需要 prompt），以「参考图 + 相对位姿」为条件给出多视角一致性梯度。它只在 `guidance` 子步内被调用——**两通道是叠加关系，不是并列的两个子步**。

#### 4.3.2 核心流程

```text
training_substep(..., guidance="guidance")
   ├── (门槛) true_global_step > freq.no_diff_steps ?   否 → 跳过全部扩散引导
   ├── guidance_inp = comp_rgb（geometry 阶段且 render_type=="normal" 时用 comp_normal）
   ├── out_2d = self.guidance(guidance_inp, prompt_utils, **batch, ...)
   │      └── 返回 dict：loss_sd 等以 "loss_" 开头的项 → set_loss("sd", ...)
   └── 若 guidance_3d 非 None：
          （可选门槛）未启用混合相机配置，或 get_rank()%2==0
          out_3d = self.guidance_3d(comp_rgb, **batch, ...)
          └── "loss_" 项 → set_loss("3d_" + 名字, ...)   # 与 2D 通道靠前缀区分
最终：loss = Σ loss_terms[λ 对应的 C() 权重 × 各项]
```

#### 4.3.3 源码精读

- [threestudio/systems/dreamcraft3d.py:191-207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L191-L207)：2D 通道消费点。L191 的条件 `guidance == "guidance" and self.true_global_step > self.cfg.freq.no_diff_steps` 是扩散引导的总闸门；L192-195 说明 geometry 阶段 normal 渲染步会把 `comp_normal` 当引导输入；L196-203 调用主引导并把返回 dict 中 `loss_` 开头的项收进损失表（L204-207）。
- [threestudio/systems/dreamcraft3d.py:209-224](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L209-L224)：3D 通道消费点。L209 判空（texture 阶段为 None 直接跳过）；L211-212 的 FIXME 注释揭示 `use_mixed_camera_config` 是按 rank 混合相机的实验分支（默认 false，恒走左支）；L213-218 调用 `guidance_3d`——注意参数里没有 `prompt_utils`，只有 `**batch`（相机位姿条件）；L219-223 收集损失时加 `"3d_"` 前缀并 log 为 `train/{name}_3d`。
- [configs/dreamcraft3d-coarse-nerf.yaml:101-111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L101-L111)：Zero123 引导的配置。`cond_image_path`、`cond_elevation_deg` 等条件相机参数直接插值自 data 段的参考视角参数——u4-l1 讲过随机相机距离/fovy 被钉死，正是为了让采样相机与这里的条件相机严格对应。
- [configs/dreamcraft3d-coarse-nerf.yaml:125-127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L125-L127)：两通道的损失权重 `lambda_sd: 0.1`、`lambda_3d_sd: 0.1`——1:1 叠加。对照 [configs/dreamcraft3d-texture.yaml:124-127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L124-L127)：texture 阶段 `lambda_3d_sd: 0.0` 且 BSD 新增了 `lambda_lora/lambda_pretrain` 两个权重（为 u7-l5 的交替优化准备）。
- [threestudio/systems/dreamcraft3d.py:321-334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L334)：损失的最终汇总：遍历 `loss_terms`，用 `self.C(cfg.loss[lambda_名字])` 取步数感知权重（C() 机制见 u2-l2 / u8-l1）加权求和。`set_loss` 的名字 → `lambda_` + 名字的映射就发生在这里（L325-327），所以 3D 通道的 `3d_sd` 对应 `lambda_3d_sd`。

一个容易忽略的工程细节——引导权重不入检查点：

- [threestudio/systems/dreamcraft3d.py:596-608](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L596-L608)：`on_save_checkpoint` 把 state_dict 中以 `"guidance."` 开头的键全部弹出；`on_load_checkpoint` 在检查点本就不含这些键时，把当前（重新初始化的）guidance 权重注入，避免加载报错。也就是说数 GB 的 DeepFloyd/SD 主引导不持久化，恢复训练时从预训练初始状态重来。注意前缀判断是 `"guidance."` 带点号——`"guidance_3d.xxx"` **不**匹配，所以 Zero123 的权重是正常保存/加载的（这也合理：Zero123 始终冻结，存了也只是冗余；而 BSD 微调中的 UNet 不入 ckpt 是当前实现的行为，做长训练续跑实验时要意识到这一点）。

#### 4.3.4 代码实践

1. **实践目标**：用纸笔（或 10 行脚本）推演 coarse 阶段前 8 步双通道的执行情况，把「配置 → 行为」的映射走通。
2. **操作步骤**：
   - 读 [dreamcraft3d.py:344-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L344-L357) 的 `do_ref` 判定（alternate 模式：`true_global_step % n_ref == 0` 为 ref 步，否则 guidance 步；coarse 阶段无 `only_pretrain_step` 字段，L354 的 hasattr 不成立）。
   - 对 `true_global_step = 0..7`、`n_ref=2`、`no_diff_steps=0` 制表：每步 `do_ref/do_guidance` 哪个为真、`training_substep` 里 2D/3D 通道是否执行。
3. **需要观察的现象**：偶数步走 ref 子步（两个引导都不执行）；奇数步走 guidance 子步，且因 `step > 0` 成立，2D 与 3D 通道**同时**执行。
4. **预期结果**：步 0 → ref；步 1 → guidance（DeepFloyd + Zero123）；步 2 → ref；步 3 → guidance……即扩散先验以 1/2 频率、双通道叠加方式介入。若把 `n_ref` 改为 4，guidance 子步占比仍按 alternate 逻辑变化（详见 u6-l2 的完整调度分析）。
5. 判定逻辑纯 Python，可写模拟脚本验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`guidance_3d` 为什么不需要 `prompt_utils` 参数？
**答案**：Zero123 是图生图模型——条件是参考图嵌入（配置里的 `cond_image_path`，在 guidance 内部编码）加相对相机姿态（来自 `**batch` 的 elevation/azimuth 等），文本提示不参与。文本条件专属于 2D 通道。

**练习 2**：3D 通道的损失名为什么统一加 `3d_` 前缀？
**答案**：两个通道返回的 dict 键名可能撞车（都叫 `loss_sd`）。L223 `set_loss("3d_"+name.split("_")[-1], value)` 用前缀区分，最终才能各自映射到 `lambda_3d_sd` 与 `lambda_sd` 两个独立权重（L325-327 的反查机制）。

**练习 3**：texture 阶段如果把 `guidance_3d` 打开（取消注释），代码上通不通？
**答案**：configure 与调用侧都兼容（判空逻辑本来就在），但 nvdiff 渲染器输出的 batch 键与 Zero123 期望的相机条件是否匹配需要核对；且 texture 配置把 `lambda_3d_sd` 设为 0.0（[texture.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L127)），即使通道执行，梯度权重也为零。默认关闭是论文设计：纹理阶段靠 BSD 个性化先验，不再需要通用 3D 先验。

### 4.4 control_guidance 可选正则与 perceptual_loss

#### 4.4.1 概念说明

texture 阶段有一个「实验性」的正则分支：用 ControlNet img2img 对当前渲染图做一次**编辑**，再要求渲染图与编辑图在感知距离上接近（`lambda_reg`）。直觉是：BSD 在持续改写扩散模型，渲染外观可能逐渐漂离参考图；编辑正则提供一条「拉回参考外观」的软约束。它默认关闭（配置注释 + 权重为 0），但代码链路完整，是学习「如何往系统里挂可选监督」的范本。

#### 4.4.2 核心流程

```text
（texture 子步，guidance=="guidance"）
若 C(lambda_reg) > 0 且 true_global_step % 5 == 0：
   rgb 渲染图 → 双线性缩放到 512×512
   control_prompt_utils = control_prompt_processor()
   with no_grad：
       control_dict = control_guidance(rgb=…, cond_rgb=…, prompt_utils=…, mask=…)
       edit_images = control_dict["edit_images"]      # ControlNet 编辑结果
       （顺手存 .threestudio_cache/control_debug.jpg 便于人工检查）
   loss_reg = (H/8)*(W/8) * perceptual_loss(edit_images, rgb).mean()
   → set_loss("reg", loss_reg)                        # 对应 lambda_reg 权重
```

三个门槛共同决定该分支是否执行：`lambda_reg > 0`（C() 步数感知）、子步类型为 guidance、步数是 5 的倍数（控制频率，兼省算力）。

#### 4.4.3 源码精读

- [threestudio/systems/dreamcraft3d.py:58-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L58-L63)：configure 侧的组装——只有 `control_guidance_type` 非空才创建 `control_guidance` 与**配套的** `control_prompt_processor`（编辑用的提示词可以与主提示词不同模型）。
- [threestudio/systems/dreamcraft3d.py:298-317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L298-L317)：texture 分支的 reg 损失全文。L299 三重门槛；L302 缩放到 512；L304-314 `no_grad` 内做编辑并落盘调试图；L316 用 4.2 装好的 `self.perceptual_loss` 计算感知距离，乘 `(H/8)*(W/8)` 做尺度补偿（VGG 特征图分辨率相关的归一）。
- [configs/dreamcraft3d-texture.yaml:100-109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L100-L109)：被注释的启用样例——`stable-diffusion-controlnet-reg-guidance` + 一个写实风格的 SD1.5 系模型作提示词编码器，prompt 直接插值主提示词。
- [configs/dreamcraft3d-texture.yaml:137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L137)：`lambda_reg: 0.0`——即使取消注释组件段，权重为零时 L299 第一道门槛就拦下分支，`control_guidance` 只占显存不产生梯度。

一个重要的联动陷阱：**组件与权重必须同时开**。只把 `lambda_reg` 调大而不配 `control_guidance_type`，L305 会因 `self.control_guidance` 属性不存在直接 AttributeError；只配组件不调权重则静默无效。

#### 4.4.4 代码实践

1. **实践目标**：搞清「正确启用编辑正则」需要改动的最小集合，并理解每处遗漏的后果。
2. **操作步骤**（源码阅读 + 配置改写，不实际训练）：
   - 复制 texture yaml 为 `my-texture.yaml`（放在 configs/ 下自行实验，勿改原文件）。
   - 取消 L100-109 注释；把 `system.loss.lambda_reg` 从 0.0 改为如 0.1。
   - 写出三份「错误配置」：只开组件不开权重；只开权重不开组件；开了组件但 `control_prompt_processor_type` 留空。
3. **需要观察的现象**：对照 [dreamcraft3d.py:58-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L58-L63) 与 L299、L303、L305 三处引用，逐一推断三种错误配置分别在第一波触发什么。
4. **预期结果**：错误 1 → 分支永不进（静默）；错误 2 → 首次进入分支时 `AttributeError: 'ImageConditionDreamFusion' object has no attribute 'control_guidance'`；错误 3 → configure 里 `find("")(…)` 抛 KeyError（find 空注册名查不到）。真正跑通需下载对应 ControlNet/SD 权重并在 texture 阶段 ckpt 上续训（待本地验证）。
5. 若有环境，跑数百步后查看 `.threestudio_cache/control_debug.jpg`（L314 写盘）确认编辑链路活着。

#### 4.4.5 小练习与答案

**练习 1**：为什么编辑过程包在 `torch.no_grad()` 里？
**答案**：正则的方向是「渲染图向编辑图靠拢」——梯度只需经 `perceptual_loss(edit_images, rgb)` 流回渲染管线；编辑图本身是目标（target），不需要也不应回传到 ControlNet（L304 的 no_grad 同时避免把 ControlNet 的激活整棵挂在计算图上，省显存）。

**练习 2**：`perceptual_loss` 为什么在所有阶段都组装、却只有 texture 用？
**答案**：configure 不感知阶段差异（stage 分支都在运行期），统一组装最简单；coarse/geometry 的正则走体渲染特有的 orient/sparsity 等（见 4.5），不需要感知距离。组件在粗阶段只多占少量 VGG 显存。

**练习 3**：`true_global_step % 5 == 0` 这个门槛与 `C()` 调度有何不同？
**答案**：`%5` 是**离散开关**（执行/不执行），`C()` 是**连续插值**（权重从起值渐变到终值）。前者控制「算不算」，后者控制「算多重」。两者叠加实现「每 5 步算一次、权重随训练渐变」的复合调度。

### 4.5 stage 字段驱动的行为分支地图

#### 4.5.1 概念说明

`stage` 是单字段多路开关：同一个类，靠它在运行期切换成「粗几何机器」「精几何机器」或「纹理机器」。这解释了 u2-l3 的结论——四份 yaml 共用 `dreamcraft3d-system`，差异一半来自换插件（`*_type`），另一半就来自换 `stage` 值。

#### 4.5.2 核心流程

`stage` 在本文件中的全部分支点：

```text
stage = "coarse"   "geometry"          "texture"
  │         │            │                  │
  ├─ forward：render_mask 仅 texture 开（供 BSD 可见性 mask）
  ├─ ref RGB 损失：MSE（coarse/geometry） │ L1+grow_mask（texture）
  ├─ 正则块三选一：体渲染正则 │ 网格正则 │ control/perceptual 正则
  ├─ 渲染类型：固定 rgb   │ rgb/normal 按 n_rgb 交替 │ 固定 rgb
  ├─ 验证采样：无        │ 无                │ visualize_samples 时 sample/sample_lora
  └─ 其他值 → raise ValueError
```

#### 4.5.3 源码精读

- [threestudio/systems/dreamcraft3d.py:65-72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L65-L72)：`forward`——texture 阶段给 renderer 传 `render_mask=True`。nvdiff 渲染器据此输出参考视角不可见区域的 mask（u5-l4 讲过），供 BSD 引导把蒸馏限制在可见区域。
- [threestudio/systems/dreamcraft3d.py:135-143](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L135-L143)：参考 RGB 损失的形式切换——coarse/geometry 用 MSE；texture 用 L1 且带 9×9 max_pool 膨胀的 `grow_mask`（对物体边缘像素降权，容忍抠图边缘误差）。
- [threestudio/systems/dreamcraft3d.py:252-319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L252-L319)：正则块的三分支结构。`if stage == "coarse"`（orient/sparsity/opaque/eikonal/z_variance 等体渲染正则）→ `elif stage == "geometry"`（normal_consistency/laplacian 网格正则）→ `elif stage == "texture"`（4.4 讲的 reg 分支）→ L318-319 其他值直接 `raise ValueError`——写错 stage 会在第一个训练步立刻崩，而不是静默跑偏。
- [threestudio/systems/dreamcraft3d.py:359-362](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L359-L362)：渲染类型切换——geometry 阶段按 `freq.n_rgb` 在 rgb 与 normal 渲染间交替（对应 [configs/dreamcraft3d-geometry.yaml:93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L93) 的 `n_rgb: 4`，即每 4 步一次 rgb、其余 normal），coarse/texture 恒为 rgb。normal 渲染步配合 L192-195 把 `comp_normal` 喂给 DeepFloyd，用「法向图当图片」的技巧雕几何。
- [threestudio/systems/dreamcraft3d.py:452-471](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L452-L471)：验证期的 texture 专属分支——`visualize_samples` 为真时，调用 `self.guidance.sample(...)` 与 `sample_lora(...)` 各生成一张图存档，用于人工观察 BSD 中两套 UNet 的生成差异（u7-l4/u7-l5 展开其含义）。
- 另一处跨模块联动：[threestudio/systems/dreamcraft3d.py:354-357](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L354-L357) 用 `hasattr(self.guidance.cfg, "only_pretrain_step")` 探测当前引导是不是 BSD（只有它有该字段，见 [stable_diffusion_bsd_guidance.py:72-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L72-L73)），是则强制插入预训练专用步。系统与引导之间靠「配置字段探测」耦合——这是阅读大型插件系统时值得留意的模式（细节归 u6-l2/u7-l5）。

#### 4.5.4 代码实践

1. **实践目标**：把 `stage` 的全部分支点整理成可检索的表格，作为后续读 training_substep 的「地图」。
2. **操作步骤**：在 `dreamcraft3d.py` 中搜索 `cfg.stage`（共 7 处左右），对每处记录：行号、所在方法、三分支各做什么；再搜索 `freq.`（n_ref/no_diff_steps/n_rgb/guidance_eval）补充调度列。
3. **需要观察的现象**：所有 stage 判断都集中在 `forward` 与 `training_step/training_substep` 两个方法里；没有任何 stage 判断出现在 configure 中（组装是阶段无关的）。
4. **预期结果**：得到一张与 4.5.2 流程图对应的表；并能回答「为什么不干脆写三个 System 类」——因为四阶段 90% 代码相同（参考图监督、双通道引导、损失汇总、验证可视化全是共享的），stage 分支只是差异点的最小表达。
5. 纯阅读任务，无需运行（可直接完成）。

#### 4.5.5 小练习与答案

**练习 1**：`stage: "coarse"` 拼错成 `"coarce"` 何时报错？
**答案**：configure 与前几步都不报（stage 不参与组装）；第一个训练步走到正则块时落入 `else` 分支 `raise ValueError(f"Unknown stage ...")`（L318-319）。失败被推迟到训练开始，代价是浪费了模型加载时间。

**练习 2**：texture 阶段的 `render_mask=True` 如果也用在 coarse 阶段会怎样？
**答案**：nerf-volume-renderer 的 `forward` 不接受/不处理 `render_mask` 参数语义（它是 nvdiff 光栅化器的功能），轻则无效、重则 TypeError；这正是「渲染器插件与 stage 配套」的体现——换 stage 通常同时换 renderer。

**练习 3**：geometry 阶段为什么需要 rgb/normal 交替，而 coarse 不需要？
**答案**：coarse 的 NeRF 密度场靠 RGB SDS 就能起形状；geometry 阶段目标是把 SDF 表面雕准，法向图对表面朝向极其敏感，DeepFloyd 对「图片」的先验能直接约束法向图的整体合理性（L192-195 的 normal 输入分支）。交替而非全程 normal，是为了保住纹理/外观信息不退化。

## 5. 综合实践

**任务：绘制 dreamcraft3d-system 的模块依赖图，并标注两份配置下的激活状态。**

步骤：

1. **画图**：以 `ImageConditionDreamFusion` 为根，画出全部子组件。参考骨架（请补全每个节点的注册名与「被谁持有」关系）：

```text
ImageConditionDreamFusion (dreamcraft3d-system)
 ├── geometry* ─────────── coarse: implicit-volume │ texture: tetrahedra-sdf-grid(由 geometry_convert_from 转换)
 ├── material* ─────────── no-material（两配置相同）
 ├── background* ───────── solid-color-background（两配置相同）
 ├── renderer* ─────────── coarse: nerf-volume-renderer │ texture: nvdiff-rasterizer
 │      └── (构造参数注入 geometry/material/background 的引用)
 ├── guidance ──────────── coarse: deep-floyd-guidance │ texture: stable-diffusion-bsd-guidance
 ├── guidance_3d ───────── coarse: stable-zero123-guidance │ texture: None（未组装）
 ├── prompt_processor ──── coarse: deep-floyd-prompt-processor │ texture: stable-diffusion-prompt-processor
 ├── prompt_utils ──────── PromptProcessorOutput（prompt_processor() 的返回值）
 ├── perceptual_loss ───── perceptual-loss（硬编码注册名，两配置都装）
 ├── [control_guidance、control_prompt_processor、control_prompt_utils] ── 两配置均未组装
 └── (运行期) pearson: PearsonCorrCoef —— on_fit_start 才创建
     （* 号四件套来自 super().configure()，即 BaseLift3DSystem）
```

2. **标注激活分支**：在图旁列两张清单，分别对 coarse-nerf 与 texture 写明：双通道里 3D 通道是否执行、control 正则是否可能执行、forward 是否 render_mask、ref RGB 损失形式、正则块走哪一支、渲染类型是否交替。
3. **脚本验证**（可选，接 4.2.4 的 dry-run）：把 dry-run 脚本的打印项扩展成上面清单的自动化版本，让脚本直接输出「装配清单 + 分支预测」。
4. **自查**：用以下问题检验成图质量——texture 阶段 `guidance_3d` 是「没装」还是「装了没权重」？（没装，None）；`perceptual_loss` 在 coarse 阶段装了吗？（装了，不用）；renderer 到 geometry/material/background 的箭头方向说明了什么？（renderer 是消费者，四件套不感知 renderer）。

预期产出：一张依赖图 + 一张两列对照表，恰好覆盖本讲全部最小模块。

## 6. 本讲小结

- `ImageConditionDreamFusion.Config` 在父类四件套字段之外新增 `stage/freq/guidance_3d 族/control_guidance 族/use_mixed_camera_config/visualize_samples`；可选组件用「类型字符串是否为空」表达，空则不组装。
- `configure()` = `super().configure()`（geometry/material/background/renderer）+ 追加 guidance（必装）、guidance_3d（可选，None 兜底）、prompt_processor/prompt_utils、perceptual_loss（硬编码 `perceptual-loss`）、control 一族（可选，引导与提示词处理器成对创建）。
- 双引导是**叠加**关系：guidance 子步里先调 2D 通道（DeepFloyd/BSD，带 prompt_utils），再在 `guidance_3d is not None` 时调 3D 通道（Zero123，带相机条件），损失靠 `3d_` 前缀与 `lambda_3d_sd` 权重区分。
- 主引导权重不入检查点（`on_save_checkpoint` 弹出 `guidance.*` 键），恢复训练时扩散模型回到预训练初始状态；`guidance_3d.*` 因前缀带下划线不被弹出。
- control_guidance 编辑正则需要「组件段 + `lambda_reg>0`」同时开启；只开一边要么静默无效要么 AttributeError。
- `stage` 是运行期开关（组装阶段无关），驱动 render_mask、RGB 损失形式（MSE vs L1+grow_mask）、正则三分支、rgb/normal 交替与验证采样。

## 7. 下一步学习建议

- 下一讲 **u6-l2 training_step：参考监督与扩散引导的交替调度** 将深入 `freq`（n_ref/ref_only_steps/no_diff_steps/ref_or_guidance）与 BSD `only_pretrain_step` 的联动——本讲 4.3.4 的推演练习正是它的热身。
- 之后 **u6-l3 / u6-l4** 逐项读参考图监督损失与各阶段正则，把 `loss_terms` 表的每一项落到实处。
- 若想先攻引导侧，可跳读 **u7-l1（prompt processor）** 与 **u7-l2（DeepFloyd SDS）**，再回看本讲 4.3 的消费点会更有体感。
- 建议随手重读 [threestudio/systems/dreamcraft3d.py:40-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L40-L63) 与 [threestudio/systems/base.py:243-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L297)，把「父类装四件套、子类装引导族」的两段式结构印在脑子里——这是读一切 threestudio 衍生系统的模板。
