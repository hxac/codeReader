# 2D 图像分支：Joint 与 3D-only 两种变体

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚一条 Qwen-VL 格式的 `image` 内容条目长什么样、`max_pixels` / `min_pixels` 约束了什么，以及它如何与点云 token 共存于同一条 message。
2. 解释 3D-only 推理为什么**不需要任何独立代码分支**：`image_dir=None` → image 条目变 `image=None` → `_prepare_messages_data` 统一过滤掉，序列里就没有视觉 token。
3. 逐字段对比 Joint 与 3D-only 两套评估配置，理解为什么差异恰好是三个字段（`lora_r`、`lora_alpha`、`image_dir`），以及 LoRA rank 失配会被静默吞掉的坑。
4. 会用 `tools/build_frame_images.py` / `tools/extract_frame.py` 从 infos pkl 出发，制备解码器期望的 `<image_dir>/<scene>/<frame>.jpg` 目录树。

本讲承接 u4-l5（推理分支 `infer_reason_eval` 的两阶段前向）。上一讲我们黑盒化了「message 里有个 image 条目」；本讲专门拆开这个盒子。

## 2. 前置知识

- **多模态 chat message（Qwen-VL 格式）**：Qwen-VL 系列模型不直接吃「一张图 + 一段话」，而是吃一组消息列表。每条消息是 `{'role': ..., 'content': [...]}`，`content` 是**内容条目列表**，条目可以是 `{'type': 'text', 'text': ...}` 或 `{'type': 'image', 'image': <路径>, ...}`。条目在列表中的顺序，就是视觉信息与文本在最终序列中的交错顺序。这套结构由 `qwen_vl_utils` 与 Qwen 处理器消费。
- **vision token（视觉词元）**：图像进入模型前会被切成小图块（patch），每个图块（或合并后的图块组）对应序列里的若干个视觉词元。图像越大，视觉词元越多，序列越长、显存越贵。因此需要一个「像素预算」来封顶。
- **智能缩放（smart resize）**：Qwen-VL 图像预处理的标准行为——保持长宽比，把图像总像素数钳制在 `[min_pixels, max_pixels]` 区间内：太大的等比缩小，太小的等比放大。本仓库统一用 `max_pixels = 448*448`、`min_pixels = 224*224`，即 \( 448 \times 448 = 200{,}704 \) 像素封顶、\( 224 \times 224 = 50{,}176 \) 像素保底。
- **ChatML 模板与 `process_vision_info`**：`processor.apply_chat_template(messages)` 把消息列表渲染成带 `<|im_start|>`/`<|im_end|>` 角色标记的纯文本；`process_vision_info(messages)`（来自 `qwen_vl_utils`）从消息里挑出所有图像条目、完成加载与缩放；随后 `processor(text=..., images=...)` 一次性产出 `input_ids`（含图像占位词元）和 `pixel_values`。
- **回顾 u4-l1 / u4-l3 / u4-l5**：超点特征经 `point2token` 投影成 `<|point|>` 嵌入，由前向预钩子 `masked_scatter` 注入；推理分支先 `generate` 生成回答，再做二次 teacher-forcing 前向取最后一层隐状态提取 `<SEG>` 查询。图像与点云 token 走的是**同一条序列**，只是注入机制不同（图像走处理器原生通道，点云走自定义钩子）。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器主体：image 条目构造、`image_dir` 路径查找、`_prepare_messages_data` 空图过滤 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 两套指令模板：纯点云版 `PART_GLAMM_INSTRUCTION` 与带图版 `PART_GLAMM_INSTRUCTION_IMG` |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | Joint 评估配置（`image_dir='data/scannet/frame_images'`，LoRA r=16） |
| [configs/ground3dlmm_eval_ground3d_scannet_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py) | 3D-only 评估配置（继承后仅覆盖三个字段） |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置：`Grounded_Decoder_Joint` 的双图像目录（ScanNet / ScanNet++） |
| [tools/build_frame_images.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_frame_images.py) | 批量抽帧：由 infos pkl 驱动，产出解码器期望的目录树 |
| [tools/extract_frame.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/extract_frame.py) | 单帧抽取：ScanNet 从 `color/` 复制，ScanNet++ 从 `rgb.mkv` 按索引解码 |
| [docs/DATA_IMAGES.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_IMAGES.md) | 图像数据获取与目录布局的官方文档 |

## 4. 核心概念与源码讲解

### 4.1 message 中的 image 条目与 max_pixels 约束

#### 4.1.1 概念说明

Joint 变体（论文主结果）在每个问题的输入里，除了点云 token，还额外喂给 Qwen3-VL **一帧 RGB 图像**——即生成该问题所依据的那个相机帧。实现上没有任何新组件：图像完全走 Qwen-VL 原生的多模态 message 通道，与点云 token（u4-l1 的钩子注入）并行不悖。

一个 `image` 条目就是四行字典：

```python
{'type': 'image',
 'image': image_path,          # None（3D-only）或 '<image_dir>/<scene>/<frame>.jpg'
 'max_pixels': 448 * 448,      # 像素上限 200,704
 'min_pixels': 224 * 224}      # 像素下限 50,176
```

`max_pixels` 是**视觉 token 预算**：以 1296×968 的 ScanNet 彩色帧为例（约 125 万像素，示例数值），智能缩放会把它等比压到 20 万像素以内，视觉 token 数量随之被钳住。这与 `max_point_tokens=2000` 的点 token 预算属于同一类设计——两种模态的序列开销都被显式封顶。

#### 4.1.2 核心流程

一轮 QA 的 message 装配（推理、单轮）：

```text
scene_names[i] = "scene0019_00_frame_195"
        │ split('_frame_')
        ▼
scene_name="scene0019_00", frame_id="195"
        │ image_dir 非 None → os.path.join(image_dir, scene_name, frame_id + '.jpg')
        ▼
image_path = "data/scannet/frame_images/scene0019_00/195.jpg"
        │
        ▼ 构造 message（image 条目在前、text 指令在后）
content = [ {'type':'image', ...}, {'type':'text', 'text': 指令 + n_points 个 <|point|> + 问题} ]
```

三条规则贯穿所有代码路径：

1. **图像只注入首条 user 消息**：单轮 QA 就是唯一那条消息；多轮对话只有 turn 0 带图，后续轮纯文本。与点 token「一次注入、全程可见」的策略一致。
2. **image 条目排在 text 之前**：content 列表顺序决定视觉 token 与指令文本在最终序列中的相对位置。
3. **指令模板有两套**：带图版会明说「基于点云数据**和给定图像**」。

#### 4.1.3 源码精读

**两套指令模板**。[uniseg3d/prompt_template.py:36-43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43) 是纯点云版 `PART_GLAMM_INSTRUCTION`，措辞为 "using only the provided point-cloud information"；[uniseg3d/prompt_template.py:46-52](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L46-L52) 是带图版 `PART_GLAMM_INSTRUCTION_IMG`，措辞改为 "based on the point-cloud data **and the given image**"，且少了 `<sentence>` 触发器的说明。两者都保留 `{point}`（填入 n_points 个 `<|point|>`）与 `{question}` 两个槽位。

**图像路径从哪来**。[uniseg3d/query_decoder.py:2873-2875](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2873-L2875) 在 `infer_reason_eval` 主循环里把场景名按 `'_frame_'` 切开：`image_dir` 为 None 时 `image_path` 直接取 None，否则拼出 `<image_dir>/<scene>/<frame>.jpg`——这正是 3D-only 分支的入口。

**推理单轮 QA 的 message 构造**。[uniseg3d/query_decoder.py:2719-2728](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2719-L2728) 先用 `PART_GLAMM_INSTRUCTION` 渲染指令文本（2721 行），再把 image 条目放在 text 前面组成 content。注意一个忠实细节：**单轮路径即 Joint 模式也用纯点云措辞的模板**，图像照附——对模型而言真正起作用的是图像条目本身带来的视觉 token，提示词措辞没有随之切换。

**推理多轮 QA 的 turn 0**。[uniseg3d/query_decoder.py:2810-2822](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2810-L2822)：turn 0 用 `PART_GLAMM_INSTRUCTION_IMG` 且带 image 条目（2815-2819），后续轮只有一条 text（2820-2821）——LLaVA 式滚动历史，图像与点 token 都不重复注入。

**训练分支的 turn 0**。[uniseg3d/query_decoder.py:1392-1408](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1392-L1408)（`Grounded_Decoder_Joint.infer_reason_train`）与推理多轮同构：第 0 轮 IMG 模板 + image 条目，后续轮纯文本问题，assistant 消息放 GT 回复。

**图像消费入口**。[uniseg3d/query_decoder.py:23](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L23) 从 `qwen_vl_utils` 导入 `process_vision_info`——图像的加载与缩放全部委托给 Qwen 生态的标准工具，仓库自己不写图像 IO。

#### 4.1.4 代码实践（源码阅读型：盘点所有 image 条目）

1. **实践目标**：找出仓库中所有构造 image 条目的位置，弄清各自用哪套指令模板、注入在哪一轮。
2. **操作步骤**：
   - 在 `uniseg3d/` 下搜索字符串 `'type': 'image'`（或用编辑器的全局查找）。
   - 对每一处命中，记录：所在类 → 方法 → 行号 → 用的模板（`PART_GLAMM_INSTRUCTION` / `_IMG`）→ 注入轮次。
3. **需要观察的现象**：命中应集中在 `query_decoder.py`，且模板选择并非「Joint 一律用 IMG 版」。
4. **预期结果**（静态阅读即可核对）：

| 位置 | 类 / 方法 | 模板 | 轮次 |
|---|---|---|---|
| query_decoder.py:1401-1408 | `Grounded_Decoder_Joint.infer_reason_train` | `_IMG` | 训练第 0 轮 |
| query_decoder.py:2724-2728 | `Grounded_Decoder_Eval._process_single_turn_qa` | 纯点云版（但附图） | 单轮唯一消息 |
| query_decoder.py:2817-2819 | `Grounded_Decoder_Eval._process_multi_turn_qa` | `_IMG` | 推理 turn 0 |
| query_decoder.py:3246-3250 | `Grounded_Decoder_Eval_MultiImg.infer_reason_eval` | 条目列表由上层构造 | 单轮（多图） |
| query_decoder.py:3300 附近 | `Grounded_Decoder_Eval_Text` | 纯点云版，**无** image 条目 | 单轮（纯文本） |

5. 若无法确定运行结果，明确写「待本地验证」——本实践为纯静态阅读，无需运行。

#### 4.1.5 小练习与答案

1. **`max_pixels` 和 `min_pixels` 各自约束什么？为什么下限也要设？**
   答：两者把图像总像素数钳制在 \([50{,}176,\ 200{,}704]\) 区间（Qwen-VL 处理器的智能缩放会保持长宽比落进区间）。上限控制视觉 token 预算、防序列爆炸；下限防止图像被过度缩小而丢失细节。
2. **image 条目为什么放在 text 条目前面？**
   答：content 列表的顺序决定多模态序列中视觉 token 与文本 token 的交错位置；放最前使视觉信息先于指令文本出现，与 ChatML 模板共同决定最终序列布局。
3. **单轮推理（Joint 模式）用的指令模板提到图像了吗？这意味着什么？**
   答：没有——`_process_single_turn_qa` 用的是纯点云措辞的 `PART_GLAMM_INSTRUCTION`，但 image 条目照附。对模型真正生效的是图像条目产生的视觉 token；提示词与模态在这里没有严格对齐，属于源码中的真实不一致，读码时应注意而不是想当然。

### 4.2 `_prepare_messages_data` 的空图过滤：无图推理如何成立

#### 4.2.1 概念说明

3D-only 变体（checkpoint `Ground3D-LMM-4B-3D`）不消费任何 RGB 帧。最直觉的实现是到处写 `if 有图: ... else: ...`，但仓库选择了更干净的方式：**让 image 条目永远出现在 message 里，只是当 `image_dir=None` 时它的 `image` 字段是 None；在统一入口处把 `image=None` 的条目丢掉**。于是 Joint 与 3D-only 共享 100% 的解码与生成代码，唯一开关在配置层。这与 u1-l3 讲过的「_3d 配置仅覆盖三个字段」互为表里：配置差异极小，正因为代码路径只有一条。

#### 4.2.2 核心流程

`_prepare_messages_data(messages, point_embeds)` 是所有推理 message 的必经之路：

```text
messages（可能含 image=None 的条目）
   │ ① 遍历每条消息的 content 列表，
   │    删除 (type=='image' 且 image is None) 的条目     ← 3D-only 在此"变成"纯文本消息
   ▼
   │ ② apply_chat_template(messages) → ChatML 文本
   │ ③ process_vision_info(messages) → images（此时 3D-only 为空列表）、videos、kwargs
   │ ④ processor(text, images, videos) → input_ids / pixel_values / attention_mask
   │ ⑤ 附加 point_embeds；point_mask = (input_ids == point_token_id)
   ▼
data（一个类字典批次，交给 generate / 二次前向）
```

关键因果链：image 条目被过滤 → `process_vision_info` 拿不到任何图像 → 处理器不产生任何视觉 token → 序列里只剩点 token 与文本。**无图推理不是「跳过图像分支」，而是「图像模态整体缺席」**。

#### 4.2.3 源码精读

**过滤逻辑本体**。[uniseg3d/query_decoder.py:2655-2662](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2655-L2662)：对每条消息，若 `content` 是列表，就用一个列表推导式剔除「`type` 为 `image` 且 `image` 为 `None`」的条目。注释明确写着这是为 3D-only checkpoint（`model.decoder.image_dir=None`）服务的——目标形态是「点 token + 文本、零视觉 token」。

**后续装配**。[uniseg3d/query_decoder.py:2663-2667](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2663-L2667)：过滤后立即套 ChatML 模板、跑 `process_vision_info`、过处理器，然后把 `point_embeds` 挂进批次、用 `input_ids == point_token_id` 重算 `point_mask`（u4-l1 讲过的数量契约入口）。图像与点云两条注入通道在此汇合成同一个批次。

**训练侧没有这个容错**。[uniseg3d/query_decoder.py:1350-1358](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1350-L1358)：`Grounded_Decoder_Joint`（训练用解码器）先按场景名是否含 `'scene'` 在 `scannet_image_dir` / `scannetpp_image_dir` 之间二选一，然后**无条件** `os.path.join(image_dir, ...)`——若目录为 None 会直接抛 `TypeError`。也就是说 Joint 训练恒有图，缺目录属配置错误，应当尽早失败；「无图模式」本质是推理侧概念。顺带一提：`Grounded_Decoder_Eval` 里也保留了一份 `infer_reason_train` 拷贝（2502 行起），其推理入口同样做了 None 判断（2530-2531），但训练分支未过滤 None 条目——发布的训练配置用的是 `Grounded_Decoder_Joint`，这份拷贝不必深究。

#### 4.2.4 代码实践（可运行：亲手复现过滤逻辑）

1. **实践目标**：用一个零依赖的纯 Python 片段，逐字复现 `_prepare_messages_data` 的过滤行为，直观看到「3D-only 的 message 长什么样」。
2. **操作步骤**：把下面的片段存成 `filter_demo.py`（**示例代码**，逻辑取自 query_decoder.py:2659-2662），运行 `python3 filter_demo.py`：

   ```python
   # 示例代码：复现 _prepare_messages_data 的空图过滤（零依赖）
   messages = [
       dict(role='user', content=[
           dict(type='image', image=None, max_pixels=448*448, min_pixels=224*224),  # 3D-only
           dict(type='text', text='<|point|>*N + question'),
       ]),
       dict(role='user', content=[
           dict(type='image', image='data/scannet/frame_images/scene0019_00/195.jpg',
                max_pixels=448*448, min_pixels=224*224),                            # Joint
           dict(type='text', text='<|point|>*N + question'),
       ]),
   ]
   for _m in messages:
       if isinstance(_m.get('content'), list):
           _m['content'] = [_c for _c in _m['content'] if not (
               isinstance(_c, dict) and _c.get('type') == 'image' and _c.get('image') is None)]
   for i, _m in enumerate(messages):
       print('message[%d]:' % i, [c.get('type') for c in _m['content']])
   ```

3. **需要观察的现象**：第一条消息只剩 `text`（3D-only 形态），第二条仍是 `image` + `text`（Joint 形态）。
4. **预期结果**：

   ```text
   message[0]: ['text']
   message[1]: ['image', 'text']
   ```

5. 进阶（**待本地验证**，需完整环境）：在 Joint 配置上追加 `--cfg-options model.decoder.image_dir=None` 干跑一次推理，确认模型照常出答案（只是没了视觉输入），体会「同一权重、同一路径、仅数据缺席」。

#### 4.2.5 小练习与答案

1. **如果不过滤 `image=None` 的条目，最可能发生什么？**
   答：`None` 不是合法图像来源，`process_vision_info` / 处理器在图像加载或编码阶段就会抛异常，推理根本走不到生成步；即使不抛错也绝不可能得到正确的无图序列。（具体报错形式取决于 `qwen_vl_utils` 版本，待本地验证。）
2. **为什么把过滤放在统一入口 `_prepare_messages_data`，而不是在构造 message 的地方写 if/else？**
   答：保证 Joint 与 3D-only 共享同一条装配与解码路径，`image_dir` 是否为 None 成为唯一开关；代码不随变体数翻倍，配置继承（u1-l3）才能做到只覆盖三个字段。
3. **训练解码器为什么可以不做这个容错？**
   答：Joint 训练恒有图：训练配置显式给出 ScanNet / ScanNet++ 两个图像目录（见 4.3.3），缺目录会在 `os.path.join(None, ...)` 处立刻 TypeError——配置错误就该尽早暴露，而不是静默降级。

### 4.3 Joint 与 3D-only：配置的三字段差异

#### 4.3.1 概念说明

两个发布变体对应两个 checkpoint 与两族配置：`Ground3D-LMM-4B-Joint`（3D + RGB，论文主结果，LoRA r=16/α=32）与 `Ground3D-LMM-4B-3D`（纯点云，r=32/α=64）。得益于 4.2 的单路径设计，`_3d` 配置只需要继承 Joint 配置后**覆盖三个字段**：`lora_r`、`lora_alpha`、`image_dir`。前两个不是「调参」，而是**权重形状匹配问题**：LoRA 旁路的秩写死在 checkpoint 里，配置里的 r 与 checkpoint 不一致时，`strict=False` 加载会**静默忽略**不匹配的 adapter，模型无声退回基座 Qwen——结果看起来能跑、指标却莫名崩掉。这是本项目最阴险的坑之一（u4-l2 与 u6-l2 的 `probe_lora_rank` 均为此而生）。

#### 4.3.2 核心流程

```text
ground3dlmm_eval_ground3d_scannet.py          （Joint 基准配置）
    lora_r=16, lora_alpha=32, image_dir='data/scannet/frame_images'
        │ _base_ 继承 + 递归覆盖
        ▼
ground3dlmm_eval_ground3d_scannet_3d.py       （3D-only）
    lora_r=32, lora_alpha=64, image_dir=None
    （其余 200 余行配置全部原样继承）
```

运行时差异传导：`image_dir=None` → 4.2 的过滤生效（无视觉 token）；`lora_r=32` → 与 3D-only checkpoint 的 adapter 形状吻合 → LoRA 真正参与推理。

#### 4.3.3 源码精读

**Joint 配置的 decoder 块**。[configs/ground3dlmm_eval_ground3d_scannet.py:65-74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L65-L74)：`lora_r=16`、`lora_alpha=32`（68-69 行），`image_dir='data/scannet/frame_images'`（74 行）——图像目录作为解码器的一个普通构造参数进入 `Grounded_Decoder_Eval.__init__`（[uniseg3d/query_decoder.py:1985-2006](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1985-L2006)，签名第 1998 行、存为属性第 2006 行）。

**3D-only 配置全文**。[configs/ground3dlmm_eval_ground3d_scannet_3d.py:19-27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L19-L27)：`_base_` 指向 Joint 配置，随后只覆盖 `lora_r=32`、`lora_alpha=64`、`image_dir=None` 三个字段。文件头部的 docstring（[configs/ground3dlmm_eval_ground3d_scannet_3d.py:1-17](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L1-L17)）把「rank 失配 → `strict=False` 静默忽略 → 退回基座 Qwen」的因果链写得非常清楚，值得整段读一遍。

**训练侧是「双目录」而非单目录**。[configs/ground3dlmm_train.py:25-26](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L25-L26) 定义 ScanNet 与 ScanNet++ 两个图像目录，[configs/ground3dlmm_train.py:47-78](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L78) 把它们作为 `scannet_image_dir` / `scannetpp_image_dir` 传给 `Grounded_Decoder_Joint`——因为训练数据是双数据源 `ConcatDataset` 混训，须按场景名分流（4.2.3 的 1352-1355 行）；而评估配置按数据集一对一（ScanNet++ 的 Joint 配置在 [configs/ground3dlmm_eval_ground3d_scannetpp.py:74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp.py#L74) 把 `image_dir` 指向 `data/scannetpp/frame_images`，其 `_3d` 版同样只覆盖三字段），所以评估解码器只需单个 `image_dir`。两个 `__init__` 签名对照：训练解码器 [uniseg3d/query_decoder.py:884-906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L884-L906)（双目录参数）。

**统一启动器的自动选择**：`eval_ground3d.sh` 会探测 checkpoint 的 LoRA rank（r=16 → joint，r=32 → 3d-only）自动选配置，见 [README.md:261-262](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L261-L262)，细节留待 u6-l2。

**变体家族预告**（点到为止，u7-l1 展开）：`Grounded_Decoder_Eval_MultiImg`（[uniseg3d/query_decoder.py:3152-3161](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3152-L3161)）一次喂某场景目录下的全部帧，服务 ScanRefer/Reason3D 的多视图输入；`Grounded_Decoder_Eval_Text`（[uniseg3d/query_decoder.py:3273-3283](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3273-L3283)）则完全不给图。可见「给几张图」本身就是一条设计轴。

#### 4.3.4 代码实践（配置对照 + 合并验证）

1. **实践目标**：亲眼确认两配置合并后**恰好**只差三个字段，并理解 rank 失配的后果。
2. **操作步骤**：
   - 第一步（纯文本，必做）：并排通读 `configs/ground3dlmm_eval_ground3d_scannet.py` 与 `..._3d.py`——后者全文不到 30 行。
   - 第二步（需 mmengine，**待本地验证**）：递归合并后对比字段：

   ```python
   # 示例代码：验证 _3d 配置与 Joint 配置合并后仅差三字段（需已安装 mmengine）
   from mmengine.config import Config
   a = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet.py')
   b = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet_3d.py')
   for k in ('lora_r', 'lora_alpha', 'image_dir'):
       print(k, ':', a.model.decoder[k], '->', b.model.decoder[k])
   ```

3. **需要观察的现象**：三个字段的取值变化方向——LoRA 变宽、图像关闭。
4. **预期结果**（待本地验证）：

   ```text
   lora_r : 16 -> 32
   lora_alpha : 32 -> 64
   image_dir : data/scannet/frame_images -> None
   ```

5. **思考核对**：如果把 3D-only checkpoint 喂给 Joint 配置，哪两个字段失配？后果是什么？（答案见 `_3d` 配置 docstring 1-17 行：adapter 被 `strict=False` 静默忽略，模型退回基座 Qwen，输出正常但指标崩坏。）

#### 4.3.5 小练习与答案

1. **三字段各为什么必须覆盖？**
   答：`lora_r`/`lora_alpha` 决定 LoRA 旁路形状，必须与 checkpoint 一致，否则 adapter 被静默丢弃；`image_dir=None` 关闭图像模态，是 3D-only 的语义本身。
2. **`eval_ground3d.sh` 如何在两个变体间自动挑选配置？**
   答：读取 checkpoint 里 LoRA 的实际 rank，r=16 选 joint 配置、r=32 选 `_3d` 配置（README 261-262 行），也可用 `--variant` 显式指定。
3. **为什么评估解码器用一个 `image_dir`，训练解码器却要两个？**
   答：评估按数据集一对一发配置（scannet / scannetpp 各自的 `image_dir`）；训练是 ScanNet + ScanNet++ 双源 `ConcatDataset` 混训，同一进程内须按场景名是否匹配 `scene\d{4}_\d{2}` 在两目录间分流（query_decoder.py:1350-1355）。

### 4.4 帧图像抽取工具：从 infos pkl 到 frame_images 目录树

#### 4.4.1 概念说明

Joint 推理的图像输入有个现实障碍：ScanNet / ScanNet++ 的影像受数据集许可约束**不能**随 Ground3D HF 数据集分发，用户必须自行从官方源获取，再抽出模型需要的那一小部分帧。工具链解决两个问题：**(a) 抽哪些帧**——由 infos pkl 决定（每个样本的场景名就编码了帧号）；**(b) 帧从哪来**——ScanNet 从 SensReader 解包出的 `color/` 里复制，ScanNet++ 从 `rgb.mkv` 按帧索引解码。最终落成目录树：

```text
<image_dir>/                # 即 model.decoder.image_dir
└── scene0019_00/
    ├── 195.jpg             # ← scene_names 'scene0019_00_frame_195' 查找的正是这个文件
    └── ...
```

注意：**推理只需要 RGB 图像本身**，位姿、内参、深度都不是模型输入（它们只是数据集元数据）。

#### 4.4.2 核心流程

`tools/build_frame_images.py` 的批处理逻辑：

```text
读 part3d_glamm_infos_{train,val}.pkl → data_list
  对每个样本：
    basename(lidar_path) 去扩展名 → 'scene0019_00_frame_195'
    不含 '_frame_' → 跳过
    split('_frame_') → (scene, frame)
    (scene, frame) 已见 → 跳过（去重）
    输出已存在 → 跳过（幂等，可断点续跑）
    detect_dataset(scene)：匹配 scene\d{4}_\d{2} → scannet，否则 scannetpp
    scannet  → extract_scannet：复制 scannet/scans/<scene>/color/<frame>.jpg
    scannetpp→ extract_scannetpp：cv2 按 CAP_PROP_POS_FRAMES 定位解码 rgb.mkv
               （cv2 不可用回退 ffmpeg；再回退预解码 png）
  汇总打印 ok / miss / 唯一 (scene, frame) 数
```

两端路径契约：脚本写出的 `os.path.join(out, scene, f'{frame}.jpg')` 与解码器查找的 `os.path.join(self.image_dir, scene_name, frame_id + '.jpg')`（query_decoder.py:2874-2875）逐字符对齐——工具与模型之间没有任何注册表或中间层，靠的就是这条字符串约定。

#### 4.4.3 源码精读

**目录布局与 3D-only 豁免**。[docs/DATA_IMAGES.md:6-15](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_IMAGES.md#L6-L15)：官方写明 `<image_dir>/<scene>/<frame>.jpg` 布局、样本名到帧文件的映射规则、以及「3D-only checkpoint 无需任何图像，`image_dir=None` 即可」；[docs/DATA_IMAGES.md:17-19](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_IMAGES.md#L17-L19) 解释了许可限制导致的「自行获取 + 本地抽取」流程。

**pkl 驱动的遍历与去重**。[tools/build_frame_images.py:33-47](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_frame_images.py#L33-L47)：加载 pkl（兼容 dict 与裸 list 两种形态），对每个样本取 `lidar_points.lidar_path` 的 basename 去掉扩展名得到 `<scene>_frame_<N>`，切出 `(scene, frame)`，用 `seen` 集合去重，目标路径 `os.path.join(a.out, scene, f'{frame}.jpg')`；已存在的文件直接计数跳过——这让脚本天然可重复运行、增量补抽。

**抽取分发与缺失统计**。[tools/build_frame_images.py:49-57](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_frame_images.py#L49-L57)：`detect_dataset` 判断数据集后分发到 `extract_scannet` / `extract_scannetpp`；`extract_frame.py` 在原始文件缺失时以 `SystemExit` 退出，这里捕获后记为 MISS 而不中断整批，最后统一汇报（59-60 行）。

**数据集判别**。[tools/extract_frame.py:25-27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/extract_frame.py#L25-L27)：正则 `scene\d{4}_\d{2}` 命中即 ScanNet，否则（哈希式 id）视为 ScanNet++。与训练解码器按 `'scene' in scene_name` 分流（query_decoder.py:1352）是同一判别逻辑的两种写法。

**两种抽取实现**。[tools/extract_frame.py:62-73](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/extract_frame.py#L62-L73) ScanNet 路径：前提是先用官方 SensReader 把 `<scene>.sens` 解包出 `color/`，这里只做文件复制，缺失时给出带修复指引的报错；[tools/extract_frame.py:76-117](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/extract_frame.py#L76-L117) ScanNet++ 路径：QA 里的 `frame_num` 是 `rgb.mkv` 的 **0-based 帧索引**，主路径用 OpenCV `CAP_PROP_POS_FRAMES` 定位解码——注释强调这与数据集生成脚本的做法逐字一致（保证抽出的帧就是当年生成问题时看的那帧）；cv2 缺失回退 ffmpeg `select` 滤镜，再回退预解码的 `rgb/frame_XXXXXX.png`。

#### 4.4.4 代码实践（工具链「规划模式」：先算抽哪些帧，不动真数据）

1. **实践目标**：不下载任何 ScanNet 原始数据，先精确列出 `build_frame_images.py` 将要抽取的帧清单与目标路径，核对与解码器查找路径的契约。
2. **操作步骤**：
   - 第一步：阅读批量用法（[docs/DATA_IMAGES.md:56-68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_IMAGES.md#L56-L68)），三个参数 `--pkl` / `--raw_root` / `--out` 的含义在 [tools/build_frame_images.py:27-30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_frame_images.py#L27-L30)。有环境时可先跑 `python tools/build_frame_images.py --help` 核对。
   - 第二步（需本地已有 pkl，**待本地验证**）：用下面的「规划模式」脚本只打印、不抽取——遍历逻辑逐行取自 build_frame_images.py:33-47：

   ```python
   # 示例代码：build_frame_images.py 的"干跑"版——只列出将抽取的帧，不读任何图像
   import os, pickle
   d = pickle.load(open('data/scannet/part3d_glamm_infos_val.pkl', 'rb'))
   data_list = d['data_list'] if isinstance(d, dict) and 'data_list' in d else d
   seen = set()
   for s in data_list:
       name = os.path.basename(s['lidar_points']['lidar_path']).rsplit('.', 1)[0]
       if '_frame_' not in name:
           continue
       scene, frame = name.split('_frame_')
       if (scene, frame) in seen:
           continue
       seen.add((scene, frame))
       print(os.path.join('data/scannet/frame_images', scene, frame + '.jpg'))
   print('unique (scene, frame):', len(seen))
   ```

   - 第三步（可选，需原始数据，**待本地验证**）：抽单帧试水——`python tools/extract_frame.py --dataset scannet --root $GROUND3D_DATA --scene scene0019_00 --frame 195 --out /tmp/f.jpg`，再按 DATA_IMAGES.md 3.A 跑全量。
3. **需要观察的现象**：打印出的每个路径都应是 `<out>/<scene>/<N>.jpg` 形态；同一 `(scene, frame)` 只出现一次（多样本共享一帧被去重）。
4. **预期结果**（待本地验证）：例如出现 `data/scannet/frame_images/scene0019_00/195.jpg`，且末行 `unique (scene, frame):` 给出唯一帧数（应远小于样本总数——一个场景的多个 QA 样本共享同一帧）。
5. 把任一打印路径与 query_decoder.py:2874-2875 的拼接式对照，确认两端契约一致。

#### 4.4.5 小练习与答案

1. **帧号 195 对 ScanNet 与 ScanNet++ 分别指什么？**
   答：ScanNet 指 SensReader 解包出的 `color/195.jpg` 文件名；ScanNet++ 指 `rgb.mkv` 里第 195 帧（0-based 索引），用 `cv2.CAP_PROP_POS_FRAMES` 定位——与数据集生成问题时读帧的方式一致（extract_frame.py:79-83 注释）。
2. **`build_frame_images.py` 为什么对已存在的输出文件直接跳过？**
   答：幂等与断点续跑——mkv 逐帧解码很贵，重跑不应重复劳动；配合 `seen` 集合对 `(scene, frame)` 去重（同一帧被多个 QA 样本引用），全量成本只与唯一帧数相关。
3. **为什么说这套工具链与解码器之间「没有接口，只有字符串契约」？**
   答：工具只负责落盘 `<out>/<scene>/<frame>.jpg`，模型只负责拼 `image_dir/<scene>/<frame>.jpg`；二者没有共享常量或注册机制，对齐完全靠命名约定——因此自制数据集时最怕破坏 `<scene>_frame_<N>` 命名（呼应 u2-l2 的裸文件名契约）。

## 5. 综合实践

**任务：一次「Joint vs 3D-only」对照推理，把本讲四个模块串起来。**

对同一个场景、同一个子任务（如 `scale_estimation`），跑三种设定并对比预测 JSON：

| 设定 | checkpoint | 配置 | 图像输入 |
|---|---|---|---|
| A | Ground3D-LMM-4B-Joint | `ground3dlmm_eval_ground3d_scannet.py` | 有（frame_images） |
| B | Ground3D-LMM-4B-Joint | 同上 + `--cfg-options model.decoder.image_dir=None` | 无（同权重消融） |
| C | Ground3D-LMM-4B-3D | `ground3dlmm_eval_ground3d_scannet_3d.py` | 无 |

步骤：

1. 按README 快速上手下载两个 checkpoint；用 4.4 的工具为 val 集中**一个场景**准备好 `frame_images/<scene>/<N>.jpg`（或至少抽一帧手工放置）。
2. 跑设定 A：`tools/test.py` + Joint 配置，`--cfg-options` 按需收窄子任务（回顾 u1-l4 的三层旋钮），开启 `save_pred_qa_dir`。
3. 跑设定 B：同一 checkpoint、同一配置，仅追加 `model.decoder.image_dir=None`——验证 4.2 的单路径设计：不报错、照常出答案。
4. 跑设定 C：换 3D-only checkpoint 与 `_3d` 配置——体会三字段差异的实际含义。
5. 对比三份预测 JSON：A 与 B 的 `pred_answer` 有何差异（B 失去视觉线索后，涉及外观/纹理的回答是否退化？接地标记 `<p>…</p><SEG>` 是否仍出现？）；C 与 B 同为无图，差异又来自何处（权重不同）。
6. 填写观察表并存入你的学习笔记。

**待本地验证**：本实践需要 GPU、checkpoint 与真实数据，无法在纯阅读环境完成；步骤 2-4 的命令模板见 README 快速上手与 u1-l4。

## 6. 本讲小结

- Joint 变体的图像输入零新组件：就是 Qwen-VL 原生 message 里的一个 `image` 条目，`max_pixels/min_pixels` 把像素总数钳在 \([50{,}176,\ 200{,}704]\)，与 `max_point_tokens` 同属「模态 token 预算」设计。
- 图像与点 token 策略一致：只在首条 user 消息注入（单轮唯一消息 / 多轮 turn 0），一次注入、全程可见。
- 3D-only 不是独立代码路径：`image_dir=None` → image 条目变 `image=None` → `_prepare_messages_data` 统一过滤 → 序列零视觉 token；Joint 与 3D-only 共享全部解码代码。
- 两变体配置恰好差三字段：`lora_r`/`lora_alpha`（形状匹配，失配会被 `strict=False` 静默吞掉、退回基座 Qwen）与 `image_dir`（模态开关）。
- 训练侧 `Grounded_Decoder_Joint` 恒有图：ScanNet / ScanNet++ 双目录按场景名分流，无 None 容错——缺目录尽早崩溃是正确行为。
- `build_frame_images.py` 由 infos pkl 驱动产出 `<out>/<scene>/<frame>.jpg`，与解码器的路径拼接构成纯字符串契约；ScanNet++ 的帧号是 `rgb.mkv` 的 0-based 索引，抽取方式与数据集生成逐字对齐。

## 7. 下一步学习建议

- **u5-l1（Mask2Former 式查询解码器）**：本讲产出的 `<SEG>` 分割查询接下来如何与超点特征逐层交互、输出逐点 mask——顺着 `forward_iter_pred` 读下去。
- **u6-l2（评估启动器与配置族）**：`eval_ground3d.sh` 如何探测 LoRA rank 自动选 joint/`_3d` 配置，正是本讲三字段差异的工程化兜底。
- **延伸阅读**：`uniseg3d/query_decoder.py` 末尾的两个子类 `Grounded_Decoder_Eval_MultiImg`（3152-3269 行，一次喂整个场景的多帧）与 `Grounded_Decoder_Eval_Text`（3273-3382 行，纯文本无图），配合 `docs/DATA_SCANREFER_REASON3D.md`，预习 u7-l1 的 ScanRefer/Reason3D 迁移——「给几张图」在这条支线上被扩展成了多视图。
