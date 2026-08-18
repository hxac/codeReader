# Prompt Processor：文本嵌入与视角相关提示

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 prompt processor 在 DreamCraft3D 训练链路中的位置：它在系统 `configure` 阶段一次性把文本编译成嵌入并缓存到磁盘，训练时只做查表。
2. 手推「方位角 → 方向（side/front/back/overhead）→ 文本模板 → 嵌入」的完整判定规则，包括阈值边界与 `shift_azimuth_deg` 的区间归一化。
3. 理解 view-dependent prompting（视角相关提示）如何缓解 Janus 多面问题，以及 perp-negative 三段式嵌入的插值数学。
4. 回答一个配置层面的关键问题：为什么 coarse 阶段必须用 `deep-floyd-prompt-processor`，而 texture 阶段换成 `stable-diffusion-prompt-processor`——答案是文本编码器必须与 guidance 模型的交叉注意力维度对齐。

本讲是单元七（扩散引导家族）的第一讲，先解决「扩散模型到底吃到了什么文本条件」这个问题，后续 DeepFloyd SDS、Zero123、BSD 各讲都建立在这个答案之上。

## 2. 前置知识

- **文本编码器与 tokenizer**：扩散模型（如 Stable Diffusion、DeepFloyd IF）不是直接读字符串，而是先把文本切成 token 序列，再由文本编码器（CLIP 或 T5）映射成一个张量，形状通常为 \(77 \times d\)（77 个 token 位置，\(d\) 是编码器隐层维度）。这个张量会在 UNet 的交叉注意力层里作为 key/value 注入，从而影响去噪过程。
- **CFG（classifier-free guidance）需要的两种嵌入**：条件嵌入（真实提示词）与无条件嵌入（通常是空字符串或负向提示词）。两者拼成一个 batch 过 UNet，再按 `guidance_scale` 外推，这就是 u7-l2 将精读的 SDS 加权差分的基础。
- **Janus 多面问题**（u1-l1 已引入）：纯文生图先验不知道「当前相机看到的是物体的哪个面」，容易在背面也生成正面。解法之一就是 view-dependent prompting——按相机方位角动态换提示词（"front view" / "back view"）。
- **本讲的承接点**（来自 u6-l1）：`dreamcraft3d-system` 在 `configure` 里用 `threestudio.find(prompt_processor_type)(prompt_processor)` 构造处理器，随即 `self.prompt_utils = self.prompt_processor()` 拿到输出对象；guidance 组件在每个训练步调用 `prompt_utils.get_text_embeddings(...)` 取嵌入。
- **注册机制**（u3-l1）：`X_type` 的值是注册名，`X` 段是构造参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/models/prompt_processors/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py) | 基类 `PromptProcessor`：方向模板、prompt 库、磁盘缓存流水线，以及输出类 `PromptProcessorOutput`（含视角判定与 perp-neg 插值） |
| [threestudio/models/prompt_processors/deepfloyd_prompt_processor.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/deepfloyd_prompt_processor.py) | `deep-floyd-prompt-processor`：用 T5 tokenizer/encoder（DeepFloyd IF）编码，coarse/geometry 阶段使用 |
| [threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py) | `stable-diffusion-prompt-processor`：用 CLIP tokenizer/encoder 编码，texture 阶段使用；文件尾部还有 DreamBooth 用的 `add_tokens_to_model` 工具函数 |
| [threestudio/models/prompt_processors/__init__.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/__init__.py) | 导入 5 个处理器文件，触发注册 |
| [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) | `shifted_expotional_decay`，perp-neg 负向权重函数 |

消费侧（本讲只看「怎么被调用」，深入留给后续讲义）：

- [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py)：configure 组装 + 每步取 `prompt_utils`。
- [threestudio/models/guidance/deep_floyd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py)：coarse 阶段的消费现场。
- [threestudio/models/guidance/stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py)：texture 阶段的消费现场。

配置侧：

- [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml)（coarse-neus/geometry 同款段落）：`deep-floyd-prompt-processor`。
- [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml)：`stable-diffusion-prompt-processor`。

## 4. 核心概念与源码讲解

### 4.1 输出契约：方位角如何变成「哪句话的嵌入」

#### 4.1.1 概念说明

prompt processor 的最终产物是一个 `PromptProcessorOutput` 对象（代码里叫 `prompt_utils`）。它持有：

- **一条全局嵌入**：`text_embeddings` / `uncond_text_embeddings`（不看相机，视角无关）；
- **四条视角嵌入**：`text_embeddings_vd` / `uncond_text_embeddings_vd`，分别对应 side / front / back / overhead 四个方向模板；
- **方向判定表**：`directions`（每个方向带一个条件函数）与 `direction2idx` 索引；
- **perp-neg 开关与系数**。

guidance 每个训练步拿着当前 batch 的相机参数（仰角、方位角、距离）来问它要嵌入。核心问题只有一个：**给定方位角，选哪条嵌入？** 这就是 view-dependent prompting 的运行时形态。

#### 4.1.2 核心流程

`get_text_embeddings(elevation, azimuth, camera_distances, view_dependent_prompting)` 的执行过程：

1. 若开启视角相关提示：对 4 个方向逐一执行 `d.condition(ele, azi, dis)`，得到 batch 内每个相机的方向下标 `direction_idx`（默认 0 = side，未命中任何条件就是 side）。
2. 用 `direction_idx` 从 `text_embeddings_vd` / `uncond_text_embeddings_vd` 里「按行取嵌入」（`embedding[indices]` 式的高级索引）。
3. 若关闭视角相关提示：直接把全局单条嵌入 `expand` 到 batch 大小。
4. 返回 `torch.cat([cond, uncond])`——**条件在前、无条件在后**，共 \(2B\) 行，供 CFG 双批次前向。

方位角先经区间归一化：

\[ \text{azi}_{\text{norm}} = ((\text{azi} + 180) \bmod 360) - 180 \in (-180, 180] \]

然后按阈值判定方向（默认 `front_threshold=45`、`back_threshold=45`、`overhead_threshold=60`）：

| 方向 | 判定条件（均为严格不等式） | 模板（`view_dependent_prompt_front=False`，默认） |
| --- | --- | --- |
| side（默认） | 恒真（兜底） | `{prompt}, side view` |
| front | \( -45 < \text{azi}_{norm} < 45 \) | `{prompt}, front view` |
| back | \( \text{azi}_{norm} > 135 \) 或 \( \text{azi}_{norm} < -135 \) | `{prompt}, back view` |
| overhead | \( \text{ele} > 60 \) | `{prompt}, overhead view` |

注意 `directions` 列表的顺序固定为 side(0)、front(1)、back(2)、overhead(3)，perp-neg 逻辑硬编码依赖这个顺序。

#### 4.1.3 源码精读

输出数据类与两个关键张量字段（视角相关的四条嵌入与方向表）定义在此：

[threestudio/models/prompt_processors/base.py:37-49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L37-L49)
这段定义了 `PromptProcessorOutput`：`text_embeddings_vd` 形状为 `Nv N Nf`（4 个方向 × 77 token × 编码维度），并携带 `directions`、`direction2idx` 与 perp-neg 配置。

方向选择与嵌入查表的完整实现：

[threestudio/models/prompt_processors/base.py:51-78](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L51-L78)
`get_text_embeddings` 先用每个方向的 `condition` 掩码回填 `direction_idx`，再做高级索引取视角嵌入；最后 `torch.cat([text_embeddings, uncond_text_embeddings])` 返回，注意源码里 `# IMPORTANT` 注释明确指出这里的 (cond, uncond) 顺序与其他实现相反——读 guidance 源码时切不可搞反。

方位角归一化函数：

[threestudio/models/prompt_processors/base.py:168-170](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L168-L170)
`shift_azimuth_deg` 把任意度数方位角折到 \([-180, 180)\)，使 front/back 判定不依赖采样时方位角的表示范围（u4-l1 讲过相机方位角可来自不同区间）。

消费现场之一（coarse 阶段，DeepFloyd guidance 的非 perp-neg 分支）：

[threestudio/models/guidance/deep_floyd_guidance.py:249-253](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L249-L253)
DeepFloyd guidance 在每个 guidance 步调用 `prompt_utils.get_text_embeddings(elevation, azimuth, camera_distances, self.cfg.view_dependent_prompting)`，把返回的 \(2B\) 行嵌入与同样复制两份的加噪隐变量一起送进 UNet（即 CFG 双批次）。

消费现场之二（texture 阶段，BSD guidance 的采样路径用视角相关、训练路径用视角无关）：

[threestudio/models/guidance/stable_diffusion_bsd_guidance.py:435-440](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L435-L440)
BSD 的采样方法按 `self.cfg.view_dependent_prompting` 取视角相关嵌入。

[threestudio/models/guidance/stable_diffusion_bsd_guidance.py:505-507](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L505-L507)
而 LoRA 相关的训练路径显式传 `view_dependent_prompting=False`，用全局单条嵌入——DreamBooth 式个性化学的是「这个物体」本身，不希望提示词随视角抖动（u7-l5 展开）。

#### 4.1.4 代码实践：不下载模型，推演 -180°→180° 的方向判定

**实践目标**：验证自己对阈值判定规则的理解，尤其边界行为。

**操作步骤**（示例代码，无需 GPU、无需联网）：

```python
# 示例代码：direction_quiz.py —— 手工复刻 base.get_text_embeddings 的方向判定循环
import torch

def shift_azimuth_deg(az):  # 与 base.py:168-170 相同
    return (az + 180) % 360 - 180

FRONT, BACK, OVERHEAD = 30.0, 30.0, 60.0  # 用 texture 阶段的阈值
templates = ["{p}, side view", "{p}, front view", "{p}, back view", "{p}, overhead view"]

elevation = torch.zeros(9)
azimuth = torch.linspace(-180, 180, 9)
distance = torch.full((9,), 2.7)

idx = torch.zeros_like(elevation, dtype=torch.long)          # 默认 side
a = shift_azimuth_deg(azimuth)
idx[(a > -FRONT) & (a < FRONT)] = 1                          # front
idx[(a > 180 - BACK) | (a < -180 + BACK)] = 2                # back
idx[elevation > OVERHEAD] = 3                                # overhead

for az, i in zip(azimuth.tolist(), idx.tolist()):
    print(f"azimuth={az:7.1f} -> {['side','front','back','overhead'][i]:8s} '{templates[i].format(p='a hamburger')}'")
```

**需要观察的现象**：9 个方位角各自命中的方向与最终文本模板。

**预期结果**（`-180 -135 -90 -45 0 45 90 135 180`）：

```
-180 -> back   'a hamburger, back view'
-135 -> side   'a hamburger, side view'
 -90 -> side   'a hamburger, side view'
 -45 -> side   'a hamburger, side view'   # 45° > 30° 阈值，落回 side
   0 -> front  'a hamburger, front view'
  45 -> side   'a hamburger, side view'
  90 -> side   'a hamburger, side view'
 135 -> side   'a hamburger, side view'
 180 -> back   'a hamburger, back view'
```

若把阈值换回默认 45°，注意 `-45°` 与 `+45°` 因**严格不等式**（`a < FRONT` 不含等号）仍归 side，而 `0°` 归 front。这是读配置时容易踩的边界细节；`4.5` 综合实践会用真实类验证同一结论。

#### 4.1.5 小练习与答案

**练习 1**：相机仰角 70°、方位角 10° 时命中哪个方向？
答：overhead。方向回填是「后写覆盖先写」——`direction_idx` 初始化为 side，循环中 front 条件先把它改成 1，随后 overhead 条件（`ele > 60` 成立）又改成 3，最终 overhead 胜出。源码循环见 base.py:62-66。

**练习 2**：为什么 `get_text_embeddings` 返回的批次是 \(2B\) 行而不是 \(B\) 行？
答：CFG 需要条件与无条件两条前向，实现上把 cond 与 uncond 沿 batch 维拼接、隐变量也复制两份，一次 UNet 调用同时得到两个噪声预测，再按 guidance_scale 外推。顺序是 (cond, uncond)，源码注释特别强调与其他实现相反。

**练习 3**：`view_dependent_prompting=False` 时四条 `*_vd` 嵌入还有用吗？
答：`get_text_embeddings` 不会用它们（走 `expand` 分支），但 perp-neg 路径强制要求视角相关（有 `assert`），且配置里仍会计算并缓存它们；BSD guidance 正是两种模式混用（采样视角相关、LoRA 训练视角无关）。

### 4.2 构造流水线：configure 的方向模板、prompt 库与子进程缓存

#### 4.2.1 概念说明

`PromptProcessor.configure` 负责把一个字符串 prompt 编译成上面那些嵌入张量。它解决三个工程问题：

1. **模板化**：把 prompt 变成 4 条视角文本 + 4 条负向文本；
2. **复用**：同一「模型 + 文本」的嵌入算一次就够，落盘缓存，重跑实验不再加载文本编码器；
3. **省显存**：文本编码器很大（T5-XL 约 4.5B 参数），用一个独立子进程算完 embedding 就退出，训练进程里从不驻留。

#### 4.2.2 核心流程

```
configure()
 ├─ 设缓存目录 .threestudio_cache/text_embeddings
 ├─ 构建 directions（4 个 DirectionConfig：模板 + 负向模板 + 条件函数）
 ├─ 读 load/prompt_library.json，预处理 prompt（"lib:" 前缀查库）
 ├─ prompts_vd = [手动覆盖 prompt_front/... 或 模板(prompt)]
 ├─ negative_prompts_vd = [负向模板(负向 prompt)]   # 恒等变换，四方向相同
 ├─ prepare_text_embeddings()   # rank 0 only
 │    ├─ 汇总 10 条文本：prompt、negative、4×vd、4×neg_vd
 │    ├─ 对每条算 hash(模型名, 文本)，已缓存则跳过
 │    └─ spawn 子进程批量编码并 torch.save 到缓存目录
 └─ load_text_embeddings()      # barrier 同步后，所有 rank 从磁盘读回
```

缓存键是 `md5(f"{model}-{prompt}")`，**模型名参与哈希**，因此同一句 prompt 在 T5 与 CLIP 下的缓存互不覆盖——这也是「换 guidance 模型必须换 processor」在工程上的体现。

#### 4.2.3 源码精读

缓存键函数：

[threestudio/models/prompt_processors/base.py:19-23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L19-L23)
`hash_prompt` 把「模型路径 + prompt」做 MD5 作为缓存文件名，天然隔离不同文本编码器的缓存。

方向配置数据类：

[threestudio/models/prompt_processors/base.py:26-34](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L26-L34)
每个 `DirectionConfig` 打包三件事：正向模板（`str -> str`）、负向模板、以及 `(elevation, azimuth, camera_distances) -> bool 掩码` 的条件函数。数据类把「文本怎么变」与「什么时候用」绑在一起。

configure 主体（方向表构建 + 模板展开 + 缓存流水线启动）：

[threestudio/models/prompt_processors/base.py:222-337](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L222-L337)
这段先按 `view_dependent_prompt_front` 选择「`{s}, side view` 后缀」或「`side view of {s}` 前缀（DreamFusion 原文风格）」两套模板构建 4 个方向（227-292 行），然后从 `load/prompt_library.json` 读 prompt 库、展开 `prompts_vd`（319-322 行，`self.cfg.get(f"prompt_{d.name}", None) or d.prompt(self.prompt)` 允许用 `prompt_front` 等键手动覆盖某个方向的文案——`self.cfg` 经 parse_structured 后是 OmegaConf 对象，支持 `.get`），最后调用 `prepare_text_embeddings` + `load_text_embeddings`。

子进程编码与缓存写入：

[threestudio/models/prompt_processors/base.py:343-390](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L343-L390)
`prepare_text_embeddings` 装饰了 `@rank_zero_only`（只有 0 号进程编码），跳过已缓存文本后，用 `mp.get_context("spawn")` 起子进程执行 `spawn_func`（编码器加载、前向、保存都在子进程里，退出即释放显存）；`spawn=False` 时在本进程直接执行。

读回与容错：

[threestudio/models/prompt_processors/base.py:392-416](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L392-L416)
`load_text_embeddings` 先 `barrier()` 等所有 rank 到齐（确保 rank 0 已写完缓存），再分别加载全局单条嵌入与 4 条视角嵌入并 `torch.stack`；`load_from_cache` 在文件缺失时抛 `FileNotFoundError`。

prompt 库检索：

[threestudio/models/prompt_processors/base.py:418-437](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L418-L437)
`preprocess_prompt` 支持 `lib:keywords` 前缀：以下划线分隔的关键词去 `load/prompt_library.json` 的 dreamfusion 列表里做「全部关键词都是子串」的匹配，命中唯一一条则替换，多条或零条都报错。这就是为什么 `configure` 必须在仓库根目录运行（相对路径 `load/prompt_library.json`）。

系统侧的组装（承接 u6-l1）：

[threestudio/systems/dreamcraft3d.py:50-53](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L50-L53)
`configure` 里 `find(prompt_processor_type)(cfg)` 构造处理器，`self.prompt_utils = self.prompt_processor()` 调用 `__call__` 得到输出对象；训练时每个 substep 还会再调一次 `self.prompt_processor()`（dreamcraft3d.py:125），由于只是打包已加载的张量，开销可忽略。

顺带一提，配置里还有「prompt 去偏」实验分支 `get_debiased_prompt`（[base.py:444-502](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L444-L502)），用 BERT 掩码语言模型逐词检测并删除 prompt 中「带视角偏置」的词（比如 "smiling" 只在正面出现），默认 `use_prompt_debiasing=False` 关闭，DreamCraft3D 四份配置均未启用。

#### 4.2.4 代码实践：观察缓存目录与哈希命名

**实践目标**：亲眼确认「模型 + 文本 → 哈希文件名」的缓存机制。

**操作步骤**：

1. 若跑过任何训练/预处理，查看 `ls .threestudio_cache/text_embeddings/ | head`（目录不存在也正常，说明还没触发过编码）。
2. 用示例代码验证哈希：`python -c "from threestudio.models.prompt_processors.base import hash_prompt as h; print(h('DeepFloyd/IF-I-XL-v1.0','a hamburger, side view')); print(h('stabilityai/stable-diffusion-2-1-base','a hamburger, side view'))"`（需在仓库根目录、环境可用时运行）。
3. 打开 [configs/dreamcraft3d-coarse-nerf.yaml:88-92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L92)，注意 `prompt_processor` 与 `guidance` 两段的 `pretrained_model_name_or_path` 完全一致。

**需要观察的现象**：两个不同模型名对同一句 prompt 产生**不同**的哈希（即不同缓存文件）。

**预期结果**：两串 32 位十六进制 MD5 不同；coarse 配置里 processor 与 guidance 共用 `DeepFloyd/IF-I-XL-v1.0`。步骤 1-3 的具体输出待本地验证（依赖环境与是否跑过训练）。

#### 4.2.5 小练习与答案

**练习 1**：为什么同一句 "a hamburger, side view" 在 coarse 和 texture 阶段会被编码两次、存成两个缓存文件？
答：缓存键包含模型名（base.py:19-23）。coarse 用 T5（DeepFloyd），texture 用 CLIP（SD2.1-base），两者的 tokenizer 词表和嵌入空间完全不同，即使文本相同，嵌入也不同，必须分开缓存。

**练习 2**：如果只想让「正面」的文案与其它面不同，配置怎么写？
答：在 `system.prompt_processor` 段加 `prompt_front: "a hamburger with sesame seeds visible, front view"` 之类的键。configure 的 319-322 行会优先取 `prompt_{direction}` 覆盖，未设置的方向仍走默认模板。注意覆盖文案自己负责写清楚视角后缀，框架不会再补。

**练习 3**：为什么 `prepare_text_embeddings` 用 spawn 子进程而不是直接在本进程编码？
答：文本编码器体积巨大（T5-XL 8bit 也要数 GB 显存），编码是一次性任务；放到子进程里算完即退出，训练主进程的显存完全留给 NeRF/DMTet 与扩散 UNet。`spawn` 上下文还避免了 CUDA fork 的坑。

### 4.3 Perp-Negative：三段式负向嵌入与视角插值

#### 4.3.1 概念说明

Perp-Neg（Perpendicular Negative，出自 Armandruption 等人的工作）是对「负向提示」的精细化：不用一条固定的负向嵌入，而是**把 front/side/back 三个方向的嵌入按当前方位角插值出正向条件，同时把相邻两个方向的嵌入作为两条带权负向条件**，三条一起过 UNet（外加 uncond，共 4 份）。直觉是：相机转到 45° 时，模型不该被「纯正面」或「纯侧面」的语义拉扯，而是被显式告知「往两边都不许偏」。

DreamCraft3D 的 coarse-nerf 配置开启了它（`use_perp_neg: true`），texture 阶段默认关闭。

#### 4.3.2 核心流程

对 batch 中每个相机 \((\text{ele}, \text{azi}, \text{dis})\)：

1. overhead 视角：正向 = overhead 嵌入，负向用两条 uncond 占位、权重 0。
2. 前半圈（\(|\text{azi}| < 90\)）：设 \(r = 1 - |\text{azi}|/90 \in (0, 1]\)（0=纯侧，1=纯正），则
   \[ \text{pos} = r \cdot \text{front} + (1-r) \cdot \text{side} \]
   负向为 front、side 两条，权重 \(-f_{fs}(r)\) 与 \(-f_{sf}(1-r)\)。
3. 后半圈（\(|\text{azi}| \ge 90\)）：设 \(r = 2 - |\text{azi}|/90 \in [0, 1)\)（0=纯背，1=纯侧），则
   \[ \text{pos} = r \cdot \text{side} + (1-r) \cdot \text{back} \]
   负向为 side、front 两条，权重 \(-f_{sb}(r)\) 与 \(-f_{fsb}(r)\)。

权重函数族是移位指数衰减：

\[ f(r) = a e^{-br} + c \]

默认系数（base.py:197-204）满足源码注释里的约束 \(a e^{-b} + c = 0\)（如 \(f_{sb}(1) = e^{-0.5} - 0.606 \approx 0\)）与 \(f_{fs}(1)=0,\ a,b>0\)，保证**插值到端点时负向引导权重衰减为 0**——相机正对某个方向时，不再需要「别偏向邻居」的惩罚。具体数值曲线在 4.3.4 实践里画。

#### 4.3.3 源码精读

perp-neg 主实现：

[threestudio/models/prompt_processors/base.py:80-165](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L80-L165)
`get_text_embeddings_perp_neg` 开头 `assert view_dependent_prompting`（perp-neg 依赖方向嵌入）；108-111 行按下标 0/1/2/3 硬编码取出 side/front/back/overhead 四条嵌入；113-152 行按方位角分前半圈/后半圈做正向线性插值、收集两条负向嵌入与权重；154-165 行把 (pos, uncond, neg×2) 沿 batch 拼成 \(4B\) 行返回，权重整形成 `B×2`。

权重函数：

[threestudio/utils/ops.py:432-433](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L432-L433)
`shifted_expotional_decay(a, b, c, r) = a·exp(-b·r) + c`，一行实现，全部魔法在系数选择上。

系数定义：

[threestudio/models/prompt_processors/base.py:193-204](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/base.py#L193-L204)
`Config` 里的 `perp_neg_f_sb/fsb/fs/sf` 四组 `(a, b, c)` 三元组，注释写明约束 `a*e(-b)+c = 0`、`f_fs(1)=0`、`a, b > 0`。名字含义：fs=front↔side 场景下 front 的负权重，sb=side↔back 场景下 side 的负权重，fsb=side↔back 场景下 front 的负权重，sf=front↔side 场景下 side 的负权重。

消费现场（coarse 阶段 DeepFloyd guidance）：

[threestudio/models/guidance/deep_floyd_guidance.py:212-224](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L212-L224)
guidance 检查 `prompt_utils.use_perp_neg`，是则取三段式嵌入与负权重，把隐变量复制 4 份（pos/uncond/neg×2）一次前向。梯度如何用负权重合成 SDS 更新，属于 u7-l2 的内容。

配置开关：

[configs/dreamcraft3d-coarse-nerf.yaml:88-92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L92)
coarse-nerf 的 `prompt_processor` 段：`use_perp_neg: true`（texture 配置无此键，默认 False）。

#### 4.3.4 代码实践：画 perp-neg 负权重的插值曲线

**实践目标**：把 4.3.2 的公式变成肉眼可见的曲线，理解「端点归零」。

**操作步骤**（示例代码，纯 CPU）：

```python
# 示例代码：perp_neg_curve.py
import torch
from threestudio.utils.ops import shifted_expotional_decay as f

r = torch.linspace(0, 1, 101)
for name, abc in [("f_fs(front侧惩罚)", (4, 0.5, -2.426)),
                  ("f_sb(side侧惩罚)",  (1, 0.5, -0.606)),
                  ("f_fsb(front侧惩罚)", (1, 0.5, +0.967))]:
    w = -f(*abc, r)          # 源码里权重带负号：base.py:138-151
    print(f"{name}: r=0 -> {w[0]:.3f}, r=0.5 -> {w[50]:.3f}, r=1 -> {w[100]:.3f}")
```

**需要观察的现象**：三个函数在 \(r=1\) 处的值。

**预期结果**（手算即可验证）：

| 函数 | r=0 | r=0.5 | r=1 |
| --- | --- | --- | --- |
| \(-f_{fs}\) | -1.574 | -0.689 | ≈0 |
| \(-f_{sb}\) | -0.394 | -0.203 | ≈0 |
| \(-f_{fsb}\) | -1.967 | -1.772 | -1.574 |

即相机越接近插值端点（正对某方向），相邻方向的负惩罚越趋近 0；`f_fsb` 恒不为 0——后半圈时「别长得像正面」始终是强约束，这正是对抗 Janus（背面长出正脸）的关键。

#### 4.3.5 小练习与答案

**练习 1**：方位角 60° 时正向嵌入是哪两类的混合、比例多少？
答：\(|60| < 90\) 走前半圈，\(r = 1 - 60/90 = 1/3\)，\(\text{pos} = \frac{1}{3}\text{front} + \frac{2}{3}\text{side}\），负向为 front（权 \(-f_{fs}(1/3)\)）与 side（权 \(-f_{sf}(2/3)\)）。

**练习 2**：为什么 perp-neg 要 `assert view_dependent_prompting`？
答：它的全部构造都依赖 side/front/back 三条方向嵌入的几何意义；关掉视角相关提示后这些嵌入不会被选用，插值也就没有意义（base.py:87-89）。

**练习 3**：perp-neg 返回的嵌入 batch 是多少行？
答：\(4B\)：每相机 1 条正向 + 1 条 uncond + 2 条负向，按 (pos 全体, uncond 全体, neg 全体) 三块拼接；负权重单独以 `B×2` 张量返回（base.py:154-165）。

### 4.4 两个实现与「词表对齐」：deep-floyd（T5）vs stable-diffusion（CLIP）

#### 4.4.1 概念说明

基类把「怎么编码」留给子类：`spawn_func` 是唯一的必填抽象点（configure_text_encoder / get_text_embeddings 那组方法标注了 "unused, kept for debugging"）。两个子类的差别只在**用哪个文本编码器**：

| | deep-floyd-prompt-processor | stable-diffusion-prompt-processor |
| --- | --- | --- |
| 注册名 | `deep-floyd-prompt-processor` | `stable-diffusion-prompt-processor` |
| 默认模型 | `DeepFloyd/IF-I-XL-v1.0` | `runwayml/stable-diffusion-v1-5`（继承自基类） |
| tokenizer/encoder | T5Tokenizer + T5EncoderModel | AutoTokenizer(CLIP) + CLIPTextModel |
| 嵌入形状 | 77 × 4096（8bit 加载） | 77 × 768（SD1.5；SD2.1-base 为 1024） |
| 使用阶段 | coarse-nerf / coarse-neus / geometry | texture（BSD） |

**为什么必须配对使用**：guidance 的 UNet 交叉注意力层有一个固定的 key/value 输入维度。DeepFloyd IF 的 UNet 期待 T5 的 4096 维序列嵌入，Stable Diffusion 的 UNet 期待 CLIP 的 768/1024 维。如果 coarse 阶段误配 `stable-diffusion-prompt-processor`，嵌入维度与 UNet 权重形状不匹配，前向直接报形状错误；即使维度碰巧兼容，两套 tokenizer 的词表与语义空间也完全不同，等于给模型喂乱码。所以配置里 `prompt_processor.pretrained_model_name_or_path` 必须与 `guidance.pretrained_model_name_or_path` 一致——这就是讲义标题里「与 guidance 模型词表对齐」的含义。

另一个现实原因：DeepFloyd IF 是**像素空间**扩散模型且以强文本理解著称（DreamFusion 的选择），coarse 阶段需要它把文本语义「雕刻」进几何；texture 阶段的 BSD 需要 HuggingFace diffusers 生态里可插拔 LoRA 的 Stable Diffusion，故整套换成 CLIP 体系。

#### 4.4.2 核心流程

两个 `spawn_func` 的流程完全同构（都由基类 `prepare_text_embeddings` 以相同签名调用）：

```
spawn_func(model_path, prompts, cache_dir, device)
 ├─ 加载 tokenizer（subfolder="tokenizer"）
 ├─ 加载文本编码器（subfolder="text_encoder"）
 ├─ tokenize(padding=max_length, truncation=True)
 ├─ with torch.no_grad(): encoder(input_ids) 取 last_hidden_state
 └─ 逐条 torch.save(embedding, cache_dir/hash(model,prompt).pt)
```

差别只在加载哪个类：T5 vs CLIP，以及 DeepFloyd 侧额外的 `load_in_8bit=True, variant="8bit", device_map="auto"`。

#### 4.4.3 源码精读

DeepFloyd 处理器注册与配置：

[threestudio/models/prompt_processors/deepfloyd_prompt_processor.py:16-22](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/deepfloyd_prompt_processor.py#L16-L22)
注册名 `deep-floyd-prompt-processor`，`Config` 仅把默认模型覆盖为 `DeepFloyd/IF-I-XL-v1.0`，其余行为全部继承基类。

DeepFloyd 的编码实现：

[threestudio/models/prompt_processors/deepfloyd_prompt_processor.py:55-96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/deepfloyd_prompt_processor.py#L55-L96)
`spawn_func` 用 T5Tokenizer（max_length 硬编码 77）+ 8bit T5EncoderModel 编码，逐条落盘；45-51 行还保留了一个调试用 `get_text_embeddings`（走 IFPipeline 的 `encode_prompt`），类型标注 `B 77 4096` 明示了 T5 嵌入维度。

Stable Diffusion 处理器注册与编码实现：

[threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py:72-103](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py#L72-L103)
`spawn_func` 用 AutoTokenizer + CLIPTextModel（`device_map="auto"`），同样 max_length padding 到 `tokenizer.model_max_length`（SD 系为 77）后编码落盘。41-68 行的调试版 `get_text_embeddings` 类型标注 `B 77 768`，对应 SD1.5 的 CLIP 隐层维度。

texture 阶段的配置（含阈值收紧）：

[configs/dreamcraft3d-texture.yaml:71-76](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L71-L76)
texture 用 `stable-diffusion-prompt-processor` + `stabilityai/stable-diffusion-2-1-base`，且 `front_threshold/back_threshold` 从默认 45 收紧到 30——纹理阶段几何已冻结，front/back 提示的收益下降，缩小其覆盖区可减少视角文案对纹理的扰动。

同文件尾部的 DreamBooth 工具（与 u8-l2 呼应）：

[threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py:106-133](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py#L106-L133)
`add_tokens_to_model` 把 DreamBooth 学到的「新概念 token 嵌入」注入 tokenizer/CLIP 编码器（`add_tokens` + `resize_token_embeddings` + 覆写对应行）。在本仓库 grep 只有定义处、无调用点，属为下游个性化实验预留的工具函数（用途待后续验证）。

包导入与注册触发：

[threestudio/models/prompt_processors/__init__.py:1-6](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/__init__.py#L1-L6)
导入 5 个处理器模块（含 dummy 与 clip 变体），`import threestudio` 时即完成注册——u3-l1 讲过的「导入即注册」。

#### 4.4.4 代码实践：写出「为什么 coarse 用 deep-floyd-prompt-processor」的论据链

**实践目标**：把 4.4.1 的结论落到可指认的源码行号上。

**操作步骤**：

1. 对照读两处形状标注：[deepfloyd_prompt_processor.py:45-51](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/deepfloyd_prompt_processor.py#L45-L51)（`B 77 4096`）与 [stable_diffusion_prompt_processor.py:41-43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/prompt_processors/stable_diffusion_prompt_processor.py#L41-L43)（`B 77 768`）。
2. 打开 [configs/dreamcraft3d-coarse-nerf.yaml:88-99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88-L99)，确认 `prompt_processor_type: deep-floyd-prompt-processor` 与 `guidance_type: deep-floyd-guidance` 共用同一 `pretrained_model_name_or_path`。
3. 写 3-5 句话回答：若把 coarse 配置的 `prompt_processor_type` 改成 `stable-diffusion-prompt-processor`（其余不动），训练会在哪里、以什么方式失败？

**需要观察的现象**：配置中 processor 与 guidance 的模型路径逐字一致；两实现的嵌入维度标注不同。

**预期结果**：失败点在 DeepFloyd guidance 的 UNet 前向（u7-l2 将读到的 `forward_unet`）：交叉注意力拿到的 `encoder_hidden_states` 是 77×768 的 CLIP 嵌入，而 UNet 权重期待 77×4096，抛出形状不匹配错误；即便无形状检查，语义空间也完全错位。本步骤为源码阅读型实践，报错信息待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：基类 `PromptProcessor` 里哪些方法是子类必须实现的？
答：严格说只有 `spawn_func`（基类 raise NotImplementedError，且被 `prepare_text_embeddings` 实际调用）；`configure_text_encoder`/`destroy_text_encoder`/`get_text_embeddings` 在两个子类里都标注 "unused, kept for debugging"。基类的 configure/缓存/方向判定/perp-neg 全部直接复用。

**练习 2**：SD2.1-base 的 CLIP 输出是 1024 维，代码里 `B 77 768` 的标注还成立吗？
答：不成立，该标注按默认的 SD1.5 写。维度由 `pretrained_model_name_or_path` 决定；要点是 77 个 token 位置固定、维度必须与 guidance UNet 的交叉注意力维度一致。

**练习 3**：perp-neg 在 texture 阶段为什么不开启？
答：texture 配置未设 `use_perp_neg`（默认 False），BSD 的纹理蒸馏以参考图个性化为主、几何已冻结，Janus 风险在 coarse 阶段已被双先验压制；且 BSD 的 LoRA 训练路径本身就用视角无关嵌入（stable_diffusion_bsd_guidance.py:505-507）。

## 5. 综合实践

**任务**：写一个脚本，用**真实的** `StableDiffusionPromptProcessor` 打印 -180° 到 180° 每隔 45° 的 view-dependent 文本与负向文本，并核对你对方向判定的预测。

分两档执行：

**A 档（无 GPU / 无网络，跑逻辑不跑编码）**——用一个假编码器顶替 CLIP，让 `configure` 全流程离线走通：

```python
# 示例代码：demo_vd_prompts.py —— 在仓库根目录运行
import os, torch, threestudio
from threestudio.models.prompt_processors.stable_diffusion_prompt_processor import (
    StableDiffusionPromptProcessor,
)
from threestudio.models.prompt_processors.base import hash_prompt

class DemoProcessor(StableDiffusionPromptProcessor):
    @staticmethod
    def spawn_func(pretrained_model_name_or_path, prompts, cache_dir, device):
        os.makedirs(cache_dir, exist_ok=True)
        for p in prompts:  # 用假嵌入顶替 CLIP：只关心文本选择逻辑
            torch.save(torch.zeros(77, 1024),
                       os.path.join(cache_dir, f"{hash_prompt(pretrained_model_name_or_path, p)}.pt"))

pp = threestudio.find("stable-diffusion-prompt-processor")({
    "prompt": "a delicious hamburger",
    "pretrained_model_name_or_path": "demo/cpu-only",  # 假模型名：缓存永不与真实运行冲突
    "front_threshold": 30., "back_threshold": 30.,     # 与 texture 阶段一致
    "spawn": False,
})
utils = pp()

elevation = torch.zeros(9)
azimuth = torch.linspace(-180, 180, 9)
distance = torch.full((9,), 2.7)

emb = utils.get_text_embeddings(elevation, azimuth, distance, True)
idx = torch.zeros_like(elevation, dtype=torch.long)
for d in utils.directions:
    idx[d.condition(elevation, azimuth, distance)] = utils.direction2idx[d.name]

for a, i in zip(azimuth.tolist(), idx.tolist()):
    print(f"azi={a:7.1f} -> {utils.directions[i].name:8s} "
          f"cond='{pp.prompts_vd[i]}'  neg='{pp.negative_prompts_vd[i]}'")
print("嵌入批次形状:", tuple(emb.shape))       # 期望 (18, 77, 1024)：cond 在前 uncond 在后
w = utils.get_text_embeddings_perp_neg(elevation, azimuth, distance)[1]
print("perp-neg 负权重形状:", tuple(w.shape))   # 期望 (9, 2)
```

**B 档（有网络与 GPU）**：把 `pretrained_model_name_or_path` 改回 `stabilityai/stable-diffusion-2-1-base`、删除自定义子类，直接 `find("stable-diffusion-prompt-processor")({...})`。首次运行会自动从 HuggingFace 下载 CLIP 文本编码器并在 `.threestudio_cache/text_embeddings/` 生成真实缓存。

**需要观察的现象**：

1. 9 个方位角命中的方向与文本（与 4.1.4 的手推表对照，应完全一致）；
2. 负向文本四方向相同（负向模板是恒等函数，默认负向 prompt 为空串，即 uncond 是空文本嵌入）；
3. 嵌入批次形状为 \((2 \times 9, 77, d)\)，cond 块在前；
4. perp-neg 正常返回且要求 view_dependent_prompting=True。

**预期结果**：方向序列为 `back, side, side, side, front, side, side, side, back`（30° 阈值、严格不等式、边界 ±45° 落 side）；A 档嵌入为全零（假编码器），B 档为真实 CLIP 嵌入。B 档与下载、显存相关，待本地验证。

**收尾解释（对应任务第二问）**：coarse 阶段用 `deep-floyd-prompt-processor`，是因为 `deep-floyd-guidance` 的 UNet 交叉注意力只认 T5 的 77×4096 序列嵌入（deepfloyd_prompt_processor.py:45-51 的形状标注与 coarse-nerf.yaml:88-99 中两段共用的模型路径是直接证据）；换成 CLIP 嵌入要么形状报错、要么语义错位，且两者缓存键不同（base.py:19-23）保证了混用不会静默发生。

## 6. 本讲小结

- prompt processor 是「文本 → 嵌入」的一次性编译器：`configure` 构建 side/front/back/overhead 四方向模板，子进程编码 10 条文本，按 `md5(模型名+文本)` 缓存到 `.threestudio_cache/text_embeddings/`，训练期只查表。
- view-dependent prompting 的运行时形态是 `PromptProcessorOutput.get_text_embeddings`：方位角经 `shift_azimuth_deg` 归一到 \([-180,180)\) 后按阈值（front/back/overhead 默认 45/45/60，texture 收紧为 30/30，严格不等式）选方向嵌入，返回 (cond, uncond) 拼接的 \(2B\) 行——顺序与其他实现相反，源码有显式注释。
- perp-neg 把负向条件做成三段式：正向嵌入按 \(r\) 在 front/side/back 间线性插值，两条负向嵌入配 \(-a e^{-br} - c\) 型衰减权重，端点处权重归零；coarse 开启、texture 关闭。
- `deep-floyd-prompt-processor`（T5，77×4096）与 `stable-diffusion-prompt-processor`（CLIP，77×768/1024）的分工由 guidance 的 UNet 交叉注意力维度决定，配置上体现为 processor 与 guidance 共用 `pretrained_model_name_or_path`。
- BSD 是两种模式的混合消费者：采样路径视角相关、LoRA 训练路径视角无关，为 u7-l4/u7-l5 的三管线结构埋下伏笔。

## 7. 下一步学习建议

下一讲（u7-l2）进入 [threestudio/models/guidance/deep_floyd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py)，看本讲产出的嵌入如何进入 `forward_unet` 与 `get_noise_pred`：CFG 加权差分如何变成 SDS 梯度、`min/max_step_percent` 如何调度时间步、以及 `use_perp_neg=True` 时本讲的三段式嵌入与负权重如何参与梯度合成。阅读建议：先把本讲 4.1.3 引用的 deep_floyd_guidance.py:212-253 上下文通读一遍，记住「cond 在前」的返回顺序，再去看噪声预测的切片方式。
