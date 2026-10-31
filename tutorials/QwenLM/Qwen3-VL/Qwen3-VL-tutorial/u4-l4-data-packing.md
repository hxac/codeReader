# 数据打包 pack_data.py 与长度分桶

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「为什么要打包（packing）」：变长多模态样本在常规 padding 训练下浪费了多少算力，packing 又是如何把浪费收回来的。
2. 读懂 [qwen-vl-finetune/tools/pack_data.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py) 的三段式主流程：**用真实处理器预估每条样本的 token 数 → 用 binpacking 装箱 → 输出两层列表的 packed 标注**。
3. 理解「pre-calculate（预先算好）长度」对训练效率的意义，以及 `_count.json` 缓存文件的作用。
4. 会用 [qwen-vl-finetune/tools/check_image.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py) 在打包前做媒体存在性、占位符配对、答案纯净性三类校验。
5. 亲手写一个 50 行以内的简化打包器，把自己的数据打包成不超过 8192 token 的桶。

本讲是单元四（微调数据准备）的收官：u4-l2 定义了单条样本的标注格式，u4-l3 讲了数据集注册与采样率，本讲把这些「散装样本」装进「长度均匀的桶」，为单元五的训练主流程（尤其是 `data_packing=True` 时拼批训练）做好准备。

## 2. 前置知识

### 2.1 变长样本与 padding 浪费

多模态训练样本天然变长：一张 1024×768 的图约 768 个视觉 token，一张小图可能只有 64 个；一轮短问答十几个文本 token，长文档问答几千个。常规训练要把一个 batch 内所有样本 **pad 到同一最大长度** 才能堆叠成矩形张量，于是每个样本平均浪费：

\[ \text{浪费 token 数} = \sum_{j=1}^{B} (L_{\max} - L_j) \]

其中 \( L_j \) 是第 \( j \) 条样本的真实长度，\( L_{\max} \) 是 batch 内最大长度。样本长度越参差，浪费越大——注意力与 FFN 的相当一部分计算花在 padding 位置上。

**packing（打包）** 的思路：把多条短样本拼进同一个「桶」，让桶与桶之间的长度尽量均匀、且每个桶都接近目标长度（如 4096），从而把 padding 浪费压到接近零。这本质上是经典的 **装箱问题（bin packing）**：给定每件物品的重量 \( w_i \) 和桶容量 \( V \)，用最少的桶装下所有物品：

\[ \text{minimize } B \quad \text{s.t.} \quad \sum_{i \in b_k} w_i \le V,\quad k=1,\dots,B,\quad \bigcup_{k} b_k = \{1,\dots,N\} \]

装箱问题是 NP 难的，工程上用贪心近似（如按重量降序逐个放入）就能得到很高的**填充率**：

\[ r = \frac{\sum_i w_i}{B \cdot V} \]

### 2.2 你需要回顾的三个既有结论

- **视觉 token 数**（u2-l2/u2-l3）：一张 H×W 的图约被压缩为 \( \frac{H}{32}\times\frac{W}{32} \) 个视觉 token（Qwen3-VL，patch 16、2×2 空间合并）；视频再除以 2（时间维 2× 压缩）。在处理器侧，这个数恰好等于 `image_grid_thw.prod() // 4`（网格数除以 4）。
- **标注格式**（u4-l2）：每条样本是 `{"image"/"video": 路径, "conversations": [{from, value}, ...]}`，`<image>`/`<video>` 占位符只出现在 human 轮。
- **注册与消费**（u4-l3）：`data/__init__.py` 的 `data_dict` 把代号映射为标注文件与媒体目录，`LazySupervisedDataset` 惰性加载。

### 2.3 一句话定位两个工具

- `pack_data.py`：训练**离线预处理**脚本——先算每条样本「真实会占多少 token」，再按 4096 的桶容量装箱，输出 `*_pack.json`。
- `check_image.py`：训练/打包**前置校验**脚本——把缺媒体文件、占位符数量不配、gpt 答案里混入媒体占位符的坏样本筛出来。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `qwen-vl-finetune/tools/pack_data.py` | 打包主脚本 | 长度预估 `calculate_tokens`、装箱 `pack_data`、`_count.json` 缓存、输出格式 |
| `qwen-vl-finetune/tools/check_image.py` | 数据完整性校验 | 三类校验规则、`*_valid.json` / `*_problems.json` 输出 |
| `qwen-vl-finetune/qwenvl/data/data_processor.py` | 训练侧消费 packed 数据 | 桶的识别（两层列表）、`_get_packed_item` 拼接、`pre_calculated_length` |
| `qwen-vl-finetune/qwenvl/train/argument.py` | 参数定义 | `data_packing` / `data_flatten` 两个开关 |
| `qwen-vl-finetune/README.md` | 文档 | packed 数据示例、`data_packing requires preprocess with tools/pack_data.py` |

注意两个环境事实（跑脚本前要知道）：

- `pack_data.py` 顶部 `import binpacking` 与 `from torchcodec.decoders import VideoDecoder`。版本清单 [qwen-vl-finetune/README.md:28-38](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L28-L38) 列出了 `torchcodec==0.2`，但**没有** `binpacking`——后者需自行 `pip install binpacking`。另注意 torchcodec 是**顶层硬 import**：即使只打包纯图像数据，装不上它脚本也起不来。
- `check_image.py` 的入口路径是硬编码在 `__main__` 里的，用前需要改文件底部两行（见 4.4.3）。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**长度预估 → binpacking 装箱 → packed 输出格式 → 图像完整性校验**。它们正好构成一条离线流水线：先校验、再估长、后装箱。

### 4.1 长度预估：MultimodalProcessor 与 calculate_tokens

#### 4.1.1 概念说明

装箱的前提是知道每件「物品」的重量。对多模态样本，重量 = **文本 token 数 + 视觉 token 数**：

\[ \text{num\_tokens} = \underbrace{21}_{\text{常数兜底}} + \underbrace{\sum_{\text{轮}} \text{len}(\text{template}(\text{轮}))}_{\text{文本}} + \underbrace{\sum_{\text{媒体}} \frac{\text{grid}_{t}\cdot\text{grid}_{h}\cdot\text{grid}_{w}}{4}}_{\text{视觉}} \]

关键点：**视觉长度不能靠字符数猜**，必须真的把图/视频按训练时的像素预算过一遍处理器，数出网格数。所以 `pack_data.py` 内置了一个迷你的 `MultimodalProcessor`，用与训练侧同源的 `Qwen2VLImageProcessor` 计算网格。

#### 4.1.2 核心流程

```
calculate_tokens(样本)
├── total = 21                              # 经验常数，兜底样本级固定开销
├── 逐轮 conversations:
│     用 apply_chat_template 单独渲染这一轮 → 累加 token 数
├── 若样本含 image 字段:
│     逐图 → process_image：
│       打开图片 → 按预算预处理 → image_grid_thw.prod() // 4
└── elif 含 video 字段:                     # 注意：elif，不是两个 if
      逐视频 → process_video：
        解码元信息 → 按 base_interval 采样帧数 → 预处理 → video_grid_thw.prod() // 4
```

`process_image` 内部流程：

```
拼路径(data_path + 相对路径) → 不存在则打印并返回 0
→ Image.open().convert('RGB')
→ deepcopy 处理器并注入 max_pixels/min_pixels 预算
→ preprocess(images=...) → image_grid_thw.prod() // 4
```

`process_video` 的帧数估计：`目标帧数 = round(视频时长 / base_interval)`，再夹进 `[video_min_frames, video_max_frames]`（默认 `[4, 8]`），用 `np.linspace` 在全片均匀取帧号。

#### 4.1.3 源码精读

**预算参数默认值**——注意这些数字里的 28 = 2 × patch_size(14)，是 Qwen2/2.5-VL 的换算基数；若你的模型是 Qwen3-VL（patch 16），预算要按 32 重新换算（与训练脚本里 `max_pixels 576*28*28` 的语义一致，详见 u5-l2）：

- [qwen-vl-finetune/tools/pack_data.py:L34-L43](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L34-L43) — `DataArguments` 定义图像/视频像素预算与视频采样参数：`max_pixels=2048*28*28`（单图上限）、`min_pixels=32*28*28`、`video_max_frame_pixels=576*28*28` / `video_min_frame_pixels=144*28*28`（单帧上下限）、`base_interval=4`（每 4 秒采 1 帧的期望节奏）、`video_min_frames=4` / `video_max_frames=8`、`data_path`（媒体基准目录，主循环里逐数据集覆写）。

**给处理器注入预算**：

- [qwen-vl-finetune/tools/pack_data.py:L52-L57](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L52-L57) — `_configure_processor` deepcopy 基础处理器后同时设置 `max_pixels`/`min_pixels` 属性和 `size = {'longest_edge': max, 'shortest_edge': min}` 字典。这与训练侧 `update_processor_pixels` 的注入方式同构（u4-l3 提过）：**打包时的像素预算必须与训练脚本一致，否则估出的长度会系统性偏差**。

**图像长度 = 网格数 ÷ 4**：

- [qwen-vl-finetune/tools/pack_data.py:L59-L67](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L59-L67) — `process_image` 打开图片过处理器，返回 `visual_processed['image_grid_thw'].prod() // 4`。`image_grid_thw` 是 (t, h, w) 网格，2×2 空间合并后每 4 个网格对应 1 个视觉 token（图像 t=1，视频 t=帧数/2，所以同一个 `// 4` 公式对两者都成立）。注意 L61-63：**文件不存在时只打印并返回 0**——坏数据不会让打包崩溃，但会让该样本长度被低估。这正是先跑 `check_image.py` 的理由（见 4.4）。

**视频长度的独立近似**：

- [qwen-vl-finetune/tools/pack_data.py:L69-L83](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L69-L83) — `process_video` 用 torchcodec 的 `VideoDecoder` 读元信息，按 `round(时长/interval)` 估帧数、夹进 `[4, 8]`、`np.linspace` 均匀取帧、整批解码后过处理器，返回 `video_grid_thw.prod() // 4`。要点：这里的采样规则（base_interval=4、最多 8 帧）是**打包脚本自己的近似**，与训练侧 `video_fps`/`nframes` 参数并不逐字对齐——估长允许近似，只要不系统性偏小。

**长度累加的三个细节**：

- [qwen-vl-finetune/tools/pack_data.py:L86-L103](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L86-L103) — `calculate_tokens`：① L87 从 21 起步（无注释的经验常数，兜底样本级固定开销，如系统提示类包装，使估算略偏保守）；② L89-94 逐轮用 `apply_chat_template` 单独渲染 `{'role': ..., 'content': text}` 再数长度——每轮各自计入 `<|im_start|>`/`<|im_end|>` 等模板包装；③ L95-102 用 `if 'image' ... elif 'video'`——**同时含图和视频的样本只统计 image 分支**，视频长度被忽略（又一个偏保守/偏小的近似，注册混合样本时要心里有数）。单值与列表两种媒体写法都在这里被归一化成列表逐个处理（承接 u4-l2 的两种写法）。

#### 4.1.4 代码实践（估算一条真实样本的长度）

1. **实践目标**：不依赖模型下载，手算一条 demo 样本的 num_tokens 组成，建立「一条样本 ≈ 视觉 token + 文本 token」的直觉。
2. **操作步骤**：
   - 打开 `qwen-vl-finetune/demo/single_images.json` 的第一条样本，用 PIL 读取 `demo/images/10095.png` 的宽高；
   - 按 Qwen3-VL 规则对齐到 32 的倍数（`w32 = round(w/32)*32`，参照 u2-l2 的就近对齐），计算视觉 token 数 \( \frac{w_{32}}{32}\times\frac{h_{32}}{32} \)；
   - 文本部分用 `len(问题 + 答案) // 2` 粗估（或加载本地 tokenizer 精确计算），三者相加得到估算值。
3. **需要观察的现象**：视觉 token 通常占该样本总长度的 90% 以上——多模态样本的长度主导项是媒体而不是文字。
4. **预期结果**：能得到一个与 `pack_data.py` 的 `process_image` 同数量级的数字。**待本地验证**（需要读取本地图片尺寸；本讲不假设已运行）。

#### 4.1.5 小练习与答案

**练习 1**：一张 1024×768 的图在 Qwen3-VL 的预算下大约产生多少视觉 token？
**答**：1024 与 768 都恰好是 32 的倍数（32×32、32×24），故 \( 32 \times 24 = 768 \) 个视觉 token。这也解释了为什么像素预算常写成「token 数 × 32 × 32」的形式（u3-l1）。

**练习 2**：为什么 `process_image` 在文件缺失时返回 0 而不是抛异常？
**答**：打包脚本希望「尽量跑完」，坏样本交由 `check_image.py` 事先筛除、训练时由 `__getitem__` 的重试机制跳过（u4-l2）。代价是缺文件的样本长度被低估，所以**校验必须前置**。

**练习 3**：`calculate_tokens` 里 `if 'image' ... elif 'video'` 意味着什么？
**答**：一条同时带图和视频的样本只会累计 image 的视觉 token，视频部分按 0 计——长度被低估，桶可能实际超出预算。混合模态样本要么拆开，要么接受近似。

### 4.2 binpacking 装箱：pack_data 函数与主流程

#### 4.2.1 概念说明

有了每条样本的 `num_tokens`，下一步是把它们装进容量 `pack_length`（脚本里固定为 4096，与训练脚本 `--model_max_length 4096` 对齐，见 [qwen-vl-finetune/README.md:273](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L273)）的桶。脚本用的是第三方库 `binpacking` 的 `to_constant_volume`（按重量降序排序后贪心装箱，best-fit-decreasing 风格）。**pre-calculate（预估长度）的意义就在这一步兑现**：

- 装箱质量完全取决于重量估计是否准确——用真实处理器估长，才能保证「桶内 token 总和 ≤ 4096」这件事在训练时真的成立（训练时不重新数长度）；
- 估长是最贵的一步（要解码图片/视频），结果缓存到 `_count.json`，**重跑打包不再碰媒体文件**；
- 长度均匀的桶让训练时每个 batch 的序列长度相近，显存占用稳定、padding 接近零。

#### 4.2.2 核心流程

```
主流程（对每个 dataset）
├── 读标注 json/jsonl
├── 若存在 <标注名>_count.json:
│     直接加载预计算长度（秒级完成）
├── 否则:
│     线程池并发跑 calculate_tokens → 写出 _count.json
├── pack_length = 4096, batch_size = 256
├── 每 256 条一批调用 pack_data:
│     lengths = [d.num_tokens ...]
│     to_constant_volume(list(enumerate(lengths)), 4096, weight_pos=1)
│     → 每组还原样本并 pop 掉 num_tokens
└── 汇总所有批的桶 → 写出 <标注名>_pack.json
```

`pack_data` 单批装箱细节：

```
输入: data_list（每条含 num_tokens）
1. lengths = [d["num_tokens"] for d in data_list]
2. to_constant_volume(list(enumerate(lengths)), V, weight_pos=1)
   # enumerate 把 (原始下标, 重量) 绑在一起，weight_pos=1 指明重量在元组第 2 位
3. 对每个桶: 按下标取回样本 → copy() → pop("num_tokens") → 追加到该桶列表
输出: [[样本, 样本, ...], [样本, ...], ...]
```

#### 4.2.3 源码精读

**装箱函数本体**：

- [qwen-vl-finetune/tools/pack_data.py:L106-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L106-L122) — `pack_data` 是纯函数：L108 取重量；L109-113 调 `binpacking.to_constant_volume`，`enumerate(lengths)` 让每个物品携带 `(原始下标, 重量)`，`weight_pos=1` 告诉库重量取元组第 2 位；L115-121 每个桶按下标取回样本、`copy()` 后 `pop("num_tokens")`——**打包产物里不再携带长度字段**（4.3 会讲 `_count.json` 里保留的版本另有用途）。由于库内部先按重量降序排序再贪心放入，**桶内样本顺序通常不是原始顺序**（综合实践里会抽查验证这一点）。

**配置区**：

- [qwen-vl-finetune/tools/pack_data.py:L125-L139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L125-L139) — 使用前要改的占位配置：L125-130 `datasets` 字典（结构与训练侧 `data_dict` 一致，只有 `data_path` + `annotation_path` 两键，此处是 `dummy_dataset` 占位）；L133-139 加载 `AutoTokenizer`、L135 **硬编码覆盖**了一个极简 ChatML 模板（只有 im_start/im_end 包装，没有系统提示与 Qwen3-VL 默认模板的额外内容），L136 用 `Qwen2VLImageProcessor.from_pretrained` 加载基础图像处理器（Qwen 系 VL 处理器共享同一套 `preprocess(images/videos) → *_grid_thw` 接口，patch_size 等差异由模型目录的配置携带），L139 组装 `MultimodalProcessor`。模板被简化意味着**文本长度也是近似值**。

**预计算与缓存**：

- [qwen-vl-finetune/tools/pack_data.py:L153-L167](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L153-L167) — 缓存机制：L153 把 `xxx.json(l)` 换成 `xxx_count.json`；L154-156 命中缓存则直接读；否则 L158-163 用 `ThreadPoolExecutor` 并发执行 `calculate_and_update`（把 `num_tokens` 写回样本字典），tqdm 显示进度；L166-167 写出缓存。**第二次运行会跳过全部解码，秒级进入装箱阶段**。

**分批装箱与输出**：

- [qwen-vl-finetune/tools/pack_data.py:L169-L192](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L169-L192) — L170 `pack_length = 4096`；L172 `batch_size = 256`——**装箱按 256 条一批局部进行**，不是全数据集全局装箱（批内贪心，批间互不知晓，牺牲少量填充率换取速度与内存）；L177-180 逐批调用 `pack_data` 并 extend 汇总；L186 打印装箱耗时；L189-192 把结果写成 `xxx_pack.json`（`indent=2`）。

#### 4.2.4 代码实践（观察缓存与分批行为）

1. **实践目标**：验证「预计算只算一次」与「分批装箱」两个行为。
2. **操作步骤**：把 `datasets` 字典指向一个几百条的小标注（可先用 `demo/single_images.json` 复制扩充），`data_path` 指向仓库根目录，运行 `python tools/pack_data.py`；观察首次运行的 tqdm 进度条与耗时；**立刻再次运行**，对比耗时；然后打开产物目录看多出的 `*_count.json` 与 `*_pack.json` 两个文件。
3. **需要观察的现象**：第二次运行没有「Processing ... data」进度条，直接打印 `Found pre - calculated token counts...`，总耗时从分钟级降到秒级；`_count.json` 里每条样本多一个 `num_tokens` 字段，而 `_pack.json` 里没有。
4. **预期结果**：如上。**待本地验证**（需要本地 Python 环境与 `binpacking`/`torchcodec` 依赖）。

#### 4.2.5 小练习与答案

**练习 1**：把 `batch_size` 从 256 调大到全数据集大小，装箱结果会怎么变？有什么代价？
**答**：批内可见的样本越多，贪心装箱的全局信息越充分，填充率通常越高（桶数更少）；代价是 `to_constant_volume` 需要对整批排序与更多桶的维护，装箱阶段耗时与内存上升。对几万条规模的数据，256 与全局差别一般不大。

**练习 2**：`pack_length=4096` 比训练脚本的 `model_max_length=4096` 恰好相等，能不能把 `pack_length` 设成 5000？
**答**：不建议。桶内 token 总和若超过 `model_max_length`，训练时会发生截断或报错（截断会把桶尾样本腰斩，监督信号损坏）。**打包容量必须 ≤ 训练最大长度**，并给估算误差留余量。

**练习 3**：为什么装箱用 `enumerate(lengths)` 而不是直接传 `lengths`？
**答**：`to_constant_volume` 返回的是「装进每个桶的物品」，若只传重量，装箱后就无法找回对应样本。`enumerate` 把原始下标绑进物品元组（`weight_pos=1` 指明重量位置），装箱后按下标取回原样本。

### 4.3 packed 输出格式：两层列表与训练侧消费

#### 4.3.1 概念说明

`_pack.json` 的顶层是**桶列表**，每个桶是**样本列表**——两层嵌套：

```json
[
  [ {样本1}, {样本2}, {样本3} ],   ← 桶 0：三条样本拼成约 4096 token
  [ {样本4} ],                     ← 桶 1：一条长样本自己一桶
  ...
]
```

训练时开 `--data_packing True`，数据集把**每个桶当作一个训练单元**（`__len__` 数的是桶数），取样本时逐条处理桶内样本再在张量维度拼接。README 里「Packed Data Example」展示的是桶内的样本形态，**没有画出外层嵌套**（[qwen-vl-finetune/README.md:L123-L153](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L123-L153)）；以代码产出为准——若照 README 手写一个扁平的两样本列表，训练时会被当成「两个各含一条样本的桶」，退化成不打包。

#### 4.3.2 核心流程

训练侧消费一个桶：

```
LazySupervisedDataset.__init__（data_packing=True 时）
├── 读标注: 元素是 list → 桶，逐个子样本注入 data_path
├── item_fn = _get_packed_item
__getitem__(i)                      # i 是桶下标
├── 桶 = list_data_dict[i]（若单样本 dict 先包成 [dict]）
└── _get_packed_item(桶):
      对桶内每个样本分别跑 _get_item
      （分词、标签掩码、视觉张量、position_ids）
      → input_ids 按 dim=1 拼接、labels 按 dim=1 拼接
      → position_ids 按 dim=2 拼接、attention_mask 变为各段长度的列表
```

#### 4.3.3 源码精读

**桶的识别与 data_path 注入**：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:L285-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L285-L291) — 加载标注时逐元素判断：`isinstance(ann, list)` 说明是桶，**给桶里每个子样本注入 `data_path`**（承接 u4-l3：媒体路径相对 data_path 解析）；普通 dict 样本走 else 分支单点注入。

**data_packing 开关**：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:L304-L307](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L304-L307) — `data_args.data_packing` 为真时 `item_fn = _get_packed_item`，否则 `_get_item`。开关定义在 [qwen-vl-finetune/qwenvl/train/argument.py:L16-L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L16-L17)（`data_flatten` / `data_packing` 默认均为 False），README 对应说明在 [qwen-vl-finetune/README.md:L311-L312](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L311-L312)：`data_packing requires preprocess with tools/pack_data.py`——**开关与离线预处理必须配套**。

**桶内拼接**：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:L450-L472](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L450-L472) — `_get_packed_item` 对桶内每个样本调用 `_get_item`（[L389-L395](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L389-L395)，内部走 `preprocess_qwen_visual` 完成分词与标签掩码，u5-l3 精读），然后 L461-466 把各段的 `input_ids`/`labels` 沿序列维 `torch.cat`、`position_ids` 沿第 3 维拼接，`attention_mask` 收集成「各段长度列表」——拼接序列如何防止样本间互相注意，由 u5-l5 的 Flattened collator 与 u5-l6 的 varlen 注意力接棒，本讲只需知道：**桶在这里被物理拼成了一条序列**。顺带一个读码彩蛋：L444-448 的 dict 分支引用了尚未定义的 `source` 变量，若真被走到会抛 `NameError`；不过 `__getitem__`（[L352-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L352-L356)）已统一把 dict 包成 `[dict]`，该分支实际是死代码。

**num_tokens 的另一个去向**：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:L337-L343](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L337-L343) — `pre_calculated_length` 属性：若样本带 `num_tokens`（即直接用 `_count.json` 当标注），直接返回长度数组，无需逐样本分词估算。在本仓库当前代码中没有其他调用点（已用全局搜索确认），可视为暴露给「按长度分组采样」等用途的接口——这也解释了为什么 `_count.json` 要保留 `num_tokens` 而 `_pack.json` 把它 pop 掉：前者是「带长度的原始数据」，后者是「训练直接消费的桶」。

#### 4.3.4 代码实践（源码阅读型：追踪一个桶的一生）

1. **实践目标**：从 `_pack.json` 的一个桶出发，跟踪它在训练侧被加载、注入、拼接的完整路径。
2. **操作步骤**：
   - 打开任一 `_pack.json`（或综合实践的产物），确认顶层元素是 list；
   - 在 `data_processor.py` 中依次定位三处代码：L286-288（桶元素逐子注入 `data_path`）、L304-307（`data_packing` 决定 `item_fn`）、L450-466（拼接四个张量字段）；
   - 画出数据流：`_pack.json → list_data_dict[i]（桶）→ _get_packed_item → 桶内逐样本 _get_item → cat 拼接 → Flattened collator`。
3. **需要观察的现象**：`attention_mask` 在 `_get_item` 里是单段长度 `[seq_len]`（[L427](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L427)），到 `_get_packed_item` 变成多段长度列表——它记录了「拼接序列里每段的真实边界」。
4. **预期结果**：能独立说出「桶数 = 训练样本数」以及「一条桶序列内各样本边界靠 attention_mask 列表区分」这两个结论。

#### 4.3.5 小练习与答案

**练习 1**：packed 标注里某条顶层元素是 dict 而不是 list，训练会崩吗？
**答**：不会。`__getitem__` 会把它包成 `[dict]`，`_get_packed_item` 按单元素桶处理——等价于「这条样本自己占一个桶」，只是失去了拼批收益。

**练习 2**：`_pack.json` 与 `_count.json` 的区别是什么？
**答**：`_count.json` = 原始样本 + `num_tokens` 字段（长度缓存，可直接当标注用并支持 `pre_calculated_length`）；`_pack.json` = 两层列表的桶（`num_tokens` 已被 pop，训练在 `data_packing=True` 时消费）。

**练习 3**：`data_flatten` 与 `data_packing` 都会把序列拼起来（[data_processor.py:L681](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L681) 二者任一为真都选 Flattened collator），区别在哪？
**答**：`data_flatten` 在 **collator 阶段** 把 batch 里现成的样本拼接（样本仍逐条存取）；`data_packing` 在 **离线数据阶段** 就把样本装进桶，数据集的一条记录即一个桶（需要 `pack_data.py` 预处理）。前者不动数据文件，后者动。二者都依赖 varlen 注意力防止样本间互相注意（u5-l6）。

### 4.4 图像完整性校验：check_image.py

#### 4.4.1 概念说明

坏样本在流水线里的破坏是**静默**的：缺图让打包长度低估（4.1）、占位符数量不配让训练时 `_build_messages` 抛错、gpt 答案里混入 `<image>` 会把占位符文本当监督信号。`check_image.py` 在最前面把坏样本筛出来，产出「干净子集 + 问题报告」，三类校验：

1. **媒体存在性**：`image`/`video`（及复数形式）字段指向的文件是否存在；
2. **占位符配对**：human 轮里 `<image>`/`<video>` 的出现次数是否等于媒体文件数（u4-l2 的配对规则）；
3. **答案纯净性**：gpt 轮里不允许出现 `<image>`/`<video>`。

#### 4.4.2 核心流程

```
validate_data(json 路径, 媒体根目录)
├── 用 datasets.load_dataset("json", ...) 读标注
├── 逐条:
│   ├── 归一化 media 字段（单值→列表，兼容 image/images、video/videos）
│   ├── 逐文件 os.path.exists(媒体根目录/相对路径) → 记 missing_files
│   ├── 统计 human 轮 <image>/<video> 次数 vs 媒体数 → 记 token_mismatch
│   └── gpt 轮含媒体占位符 → 记 gpt_media_token
│   └── 无问题 → valid；有问题 → 附 validation_problems 字段进 problem
├── 写出 <base>_valid.json 与 <base>_problems.json
└── 打印汇总（总数/合格率/媒体类型分布/各类问题计数/缺失文件样例）
```

#### 4.4.3 源码精读

**字段归一化与类型统计**：

- [qwen-vl-finetune/tools/check_image.py:L52-L72](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L52-L72) — L54-55 同时兼容 `image`/`images`、`video`/`videos` 四种键名（缺省空列表）；L59-63 把单值字符串包成列表；L66-72 统计「纯图 / 纯视频 / 混合」三类样本数。

**三类校验规则**：

- [qwen-vl-finetune/tools/check_image.py:L74-L89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L74-L89) — 媒体存在性：`os.path.join(media_folder_path, media_file)` 后 `os.path.exists`，缺失则记入 `missing_files` 并给该条样本打 `missing_files` 问题标签。
- [qwen-vl-finetune/tools/check_image.py:L91-L110](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L91-L110) — 占位符计数：L104-107 只统计 **human 轮** 的 `<image>`/`<video>` 次数（与 u4-l2「占位符只允许出现在 human 轮」一致）；L108-110 一旦 gpt 轮出现媒体占位符就打标记。
- [qwen-vl-finetune/tools/check_image.py:L112-L130](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L112-L130) — 数量比对：`actual_counts != expected_counts` 记 `token_mismatch`（一条样本对每种媒体类型各比一次，`break` 保证每条至多记一次该类问题）；gpt 轮含占位符单独记 `gpt_media_token`。

**结果分流与汇总**：

- [qwen-vl-finetune/tools/check_image.py:L132-L146](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L132-L146) — 无问题的进 `valid_data`；有问题的 `item.copy()` 后追加 `validation_problems` 字段（结构化的问题清单，方便程序化修复）进 `problem_data`；分别写出 `<base>_valid.json` 与 `<base>_problems.json`。
- [qwen-vl-finetune/tools/check_image.py:L149-L163](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L149-L163) — 汇总打印：总数、合格率、媒体类型分布、缺媒体/占位符不配/gpt 含占位符三类计数、最多 5 个缺失文件样例。
- [qwen-vl-finetune/tools/check_image.py:L166-L169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L166-L169) — 入口：`json_file_path` 与 `media_folder_path` **硬编码**为 `"example.json"` 与 `"media"`，使用前需改成自己的路径（或改成 `argparse`/`sys.argv`）。

#### 4.4.4 代码实践（跑一次真实校验）

1. **实践目标**：对 u4-l2 制作的 `my_dataset.json`（或 `demo/single_images.json`）跑完整校验并读懂报告。
2. **操作步骤**：
   - 把 `check_image.py` 底部 L167-168 的两个路径改为你的标注与媒体目录（`media_folder_path` 填标注里相对路径的基准目录，仓库 demo 场景下填 `qwen-vl-finetune/`）；
   - 运行 `python tools/check_image.py`；
   - 故意制造一个坏样本：复制一条记录、把 `image` 改成不存在的路径，再跑一次，对比两次的 Validation Summary。
3. **需要观察的现象**：合格率从 100% 下降；`Entries with missing media` 加 1；`Sample missing files` 列出你伪造的路径；`*_problems.json` 中该样本带 `{"type": "missing_files", ...}` 的 `validation_problems`。
4. **预期结果**：`*_valid.json` 条数 + `*_problems.json` 条数 = 总条数。**待本地验证**（需本地 Python 环境；依赖 `datasets`、`tqdm`）。

#### 4.4.5 小练习与答案

**练习 1**：一个样本有 2 张图，human 轮写了 3 个 `<image>`，会命中哪类问题？
**答**：`token_mismatch`（expected=2, actual=3）。训练时这类样本会在占位符配对时抛错、被 `__getitem__` 重试机制静默跳过（u4-l2），所以要在离线阶段筛掉。

**练习 2**：为什么 gpt 轮出现 `<image>` 要单独作为一类问题，而不是算进数量不配？
**答**：占位符替换只发生在 human 轮；gpt 轮的 `<image>` 是**字面文本**，会成为监督信号的一部分，教模型「回答时输出占位符」。它是语义污染，不是数量问题，故单独标记为 `gpt_media_token`。

**练习 3**：校验脚本的输出如何接入本讲的打包流水线？
**答**：用 `*_valid.json`（而非原始标注）作为 `pack_data.py` 里 `datasets` 字典的 `annotation_path` 输入，保证打包估长的每条样本媒体齐全、配对正确。

## 5. 综合实践

**任务：写一个 50 行以内的简化打包器，把 u4-l2 制作的 `my_dataset.json` 打包成不超过 8192 token 的桶，并抽查桶内样本顺序。**

这个实践把四个最小模块串成一条流水线（估长 → 装箱 → 输出 → 抽查），完成后你会得到一份能被 `data_packing=True` 训练直接消费的 packed 标注。

### 5.1 准备

- 输入：u4-l2 实践产出的 `my_dataset.json`（5 条图文样本；没有的话先用 `demo/single_images.json` 代替，3 条也可）。
- 依赖：`pip install binpacking pillow`（不需要下载模型——视觉 token 用尺寸估算，文本 token 用近似公式；若本地有 tokenizer 可替换得更精确）。

### 5.2 参考实现（示例代码，约 45 行）

```python
# mini_pack.py —— 简化版打包器（示例代码）
# 用法: python mini_pack.py <输入.json> <输出_pack.json> [桶容量，默认8192]
import json, os, sys
from PIL import Image
import binpacking

SRC, DST = sys.argv[1], sys.argv[2]
BUCKET = int(sys.argv[3]) if len(sys.argv) > 3 else 8192
ALIGN = 32  # Qwen3-VL: 2×patch_size(16)；Qwen2/2.5-VL 换成 28

def text_tokens(s):                    # 近似：精确版用 AutoTokenizer 统计
    return max(1, len(s) // 2)

def image_tokens(path):                # 近似 smart_resize：就近对齐后 (W/32)*(H/32)
    try:
        w, h = Image.open(path).size
    except Exception:
        return 0                       # 缺文件按 0 计，呼应 4.1 的保守行为
    gw, gh = max(round(w / ALIGN), 1), max(round(h / ALIGN), 1)
    return gw * gh                     # 每个对齐格 = 1 个视觉 token

def num_tokens(item, base):
    n = 21                             # 与 pack_data.py 相同的常数兜底
    for turn in item["conversations"]:
        n += text_tokens(turn["value"])
    imgs = item.get("image", [])
    for f in ([imgs] if isinstance(imgs, str) else imgs):
        n += image_tokens(os.path.join(base, f))
    return n                           # 视频长度从略：混合样本请参考 4.1 的近似

base = os.path.dirname(os.path.abspath(SRC))
data = json.load(open(SRC))
for item in data:
    item["num_tokens"] = num_tokens(item, base)

groups = binpacking.to_constant_volume(
    list(enumerate(d["num_tokens"] for d in data)), BUCKET, weight_pos=1)
packed = [
    [{k: v for k, v in data[i].items() if k != "num_tokens"} for i, _ in g]
    for g in groups
]
json.dump(packed, open(DST, "w"), ensure_ascii=False, indent=2)
print(f"{len(data)} samples -> {len(packed)} buckets, "
      f"fill rate {sum(d['num_tokens'] for d in data) / (len(packed) * BUCKET):.1%}")
```

### 5.3 操作步骤

1. 保存脚本，运行 `python mini_pack.py my_dataset.json my_dataset_pack.json 8192`。
2. 记录打印的桶数与填充率。
3. 抽查第一个桶：写三行 Python 打印桶内每条样本的 `num_tokens` 估算值与 human 问题前 20 个字符（估长那步可以把中间结果先存一份，或临时在脚本里打印）。

### 4. 需要观察的现象与预期结果

- **现象 1**：每个桶的 token 总和 ≤ 8192（`to_constant_volume` 的容量约束）。
- **现象 2**：桶内样本顺序**大概率不是** `my_dataset.json` 里的原始顺序——`to_constant_volume` 先按重量降序排序再贪心装箱，桶内通常呈现「重在前、轻在后」的排布。这就是规格里「抽查一个桶内的样本顺序」要验证的点。
- **现象 3**：3~5 条小样本（每条几百 token）会被装进**同一个桶**，填充率远低于 4096 场景（样本太少、桶太大）；把桶容量改成 512 再跑一次，会看到桶数变多、单桶更满——直观感受容量与填充率的权衡。
- 以上均为**待本地验证**：具体排布与填充率取决于你的数据分布。

### 5.4 进阶（可选）

把 `my_dataset_pack.json` 注册进 `qwenvl/data/__init__.py` 的 `data_dict`（沿用 u4-l3 的方法，`annotation_path` 指向 packed 文件），训练脚本加 `--data_packing True`，用 `--num_train_epochs 0.01` 做一次冒烟训练（2B 模型即可），观察日志里 `Total training samples` 打印的是**桶数**而不是样本数。完整训练流程属于 u5-l1 的内容。

## 6. 本讲小结

- **长度预估**：`calculate_tokens` = 常数 21 + 逐轮 chat template 渲染的文本 token + `*_grid_thw.prod() // 4` 的视觉 token；视觉长度必须用与训练一致的像素预算过真实处理器才能算准，`process_image` 对缺文件返回 0（静默低估）。
- **binpacking 装箱**：`pack_data` 用 `to_constant_volume`（降序贪心）按 4096 容量装箱，每 256 条一批局部进行；估长结果缓存到 `_count.json`，重跑免解码秒级完成——「pre-calculate 长度」是 packing 收益的前提。
- **packed 输出格式**：`_pack.json` 是「桶列表的列表」，`num_tokens` 在打包时被 pop；训练侧 `data_packing=True` 时每个桶是一个训练单元，`_get_packed_item` 把桶内样本的张量沿序列维拼接、`attention_mask` 变为各段长度列表；README 的示例没有体现外层嵌套，以代码为准。
- **图像完整性校验**：`check_image.py` 做媒体存在性、human 轮占位符配对、gpt 轮纯净性三类校验，产出 `*_valid.json` / `*_problems.json`，应当作为打包前的第一道工序。
- **两个易踩的坑**：`binpacking` 不在 README 依赖清单里需自行安装（torchcodec 已列出但是顶层硬 import）；打包容量必须 ≤ 训练的 `model_max_length` 并留误差余量。

## 7. 下一步学习建议

- **u5-l1（train_qwen.py 训练主流程）**：看 `data_packing=True` 如何一路传到 `make_supervised_data_module`，以及打包数据如何进入 Trainer。
- **u5-l3（LazySupervisedDataset 与标签掩码）**：本讲只借用了 `_get_item` 的入口，标签掩码（-100 区间）的构造细节在那里精读。
- **u5-l5（两种 DataCollator）**：`FlattenedDataCollatorForSupervisedDataset` 如何把本讲的桶（或 `data_flatten` 的 batch）拼成最终训练序列，`attention_mask` 如何变成累计长度向量。
- **u5-l6（FlashAttention varlen 替换）**：拼接训练的最后一环——`cu_seqlens` 如何由各段长度生成、varlen 注意力如何保证桶内样本互不干扰。
- 延伸阅读：第三方库 `binpacking` 的源码（`to_constant_volume` 的排序与贪心细节），以及 HF Trainer 的 `group_by_length` 机制，与本讲 `pre_calculated_length` 属性的潜在用途对照。
