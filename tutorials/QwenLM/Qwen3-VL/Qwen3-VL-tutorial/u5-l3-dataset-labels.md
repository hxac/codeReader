# u5-l3 LazySupervisedDataset 与标签掩码构造

## 1. 本讲目标

上一讲（u5-l1）我们跟完了 `train_qwen.py` 的主流程，其中有一行 `data_module = make_supervised_data_module(processor, data_args=data_args)` 把数据侧整体打包交给了 Trainer。本讲就钻进这个「数据侧」，读完本讲你应该能够：

1. 说清一条 JSON 标注从磁盘到 `input_ids / labels / pixel_values / position_ids` 张量的完整加工链路。
2. 跟踪 `<image>` 占位符如何被替换成 messages content 里的图像元素（FIFO 配对）。
3. 理解 `preprocess_qwen_visual` 里两个魔法数字 `77091` 与 `151645` 的确切含义，以及标签区间 `[ans_start, ans_end+2)` 为什么恰好框住「assistant 回复 + 结束符」。
4. 掌握 `update_processor_pixels` 把命令行像素预算注入 processor 的双通道写法。
5. 理解 `__getitem__` 的三层容错重试，以及「坏样本被静默换成相邻样本」这个重要副作用。

## 2. 前置知识

本讲默认你已读过 u4-l2（标注格式）、u4-l3（数据集注册表）与 u5-l1（训练主流程）。再补三个概念：

- **labels 与 `-100`**：SFT（有监督微调）只希望模型学习「assistant 的回答」，而不是背诵用户的提问。PyTorch 的 `CrossEntropyLoss` 有个约定：标签值为 `ignore_index`（默认 `-100`）的位置不计损失。所以构造训练样本时，把「不想让模型学的位置」的标签填 `-100`，把「想让模型学的位置」的标签填真实 token id，即可实现选择性监督。本仓库把 `-100` 命名为 `IGNORE_INDEX`。
- **ChatML 格式**：Qwen 系列的对话模板。一轮 assistant 回答渲染成 token 后形如：
  \[ \langle|\text{im\_start}|\rangle \;\; \text{assistant} \;\; \backslash n \;\; \text{回复内容} \;\; \langle|\text{im\_end}|\rangle \;\; \backslash n \]
  其中 `<|im_start|>` 的 id 是 151644，`<|im_end|>` 的 id 是 151645，二者是 Qwen 词表里稳定的特殊 token；而 `assistant` 这个单词会被 BPE 分词器编码成一个**普通** token——id 77091（它不是特殊 token，所以源码只能硬编码数字）。这个 id 可以用一行 `tokenizer.decode([77091])` 本地验证，本讲实践环节会做。
- **Dataset 与 Collator 的分工**：`Dataset.__getitem__(i)` 负责把第 i 条样本变成「单条张量」；`DataCollator` 负责把一个 batch 的单条张量拼成规整的矩形（padding 或拼接）。本讲只讲前者，collator 留给 u5-l5。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 本讲主文件：预算注入、占位符替换、标签掩码、容错重试、两种 collator |
| [qwen-vl-finetune/qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表 `data_dict` 与 `data_list` 解析（u4-l3 已讲，本讲是消费方） |
| [qwen-vl-finetune/qwenvl/data/rope2d.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py) | MRoPE 位置编码计算；本讲只到 `_get_item` 的调用点，内部留给 u5-l4 |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | `DataArguments` 中像素预算参数的默认值（28 口径遗产） |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 训练入口，`make_supervised_data_module` 的调用处 |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | 实践用的 demo 标注 |

先给一张总数据流图，本讲四个模块就是图中的四个工位：

```text
train_qwen.py:186  make_supervised_data_module
 └─ LazySupervisedDataset.__init__          ← ①update_processor_pixels 在这里注入预算
     （读注册表 + 标注 JSON，lazy：不分词不读图）
 └─ Trainer 取样本时调用 __getitem__(i)      ← ④三层容错重试
     └─ item_fn（默认 _get_item）
         └─ preprocess_qwen_visual
             ├─ _build_messages              ← ②<image>/<video> 占位符替换
             ├─ processor.apply_chat_template(tokenize=True)
             └─ labels 掩码扫描              ← ③71691/151645 标签区间
         └─ get_rope_index（position_ids，u5-l4）
 └─ DataCollator（u5-l5）
```

## 4. 核心概念与源码讲解

### 4.1 update_processor_pixels：把命令行像素预算写进 processor

#### 4.1.1 概念说明

u3-l1 讲过推理侧的「像素预算 → 缩放后尺寸 → 视觉 token 数 → 序列长度」因果链。训练侧同理，但预算来源不同：**不使用 processor 配置文件里的默认值，而是把 `DataArguments` 里的八个像素/帧数参数（u5-l2 讲过）强制写进 processor**。这样一条命令行就能控制全部样本的视觉开销，且训练与保存出去的 processor 配置保持一致（训练结束时 `processor.save_pretrained` 会把改过的值一并落盘，见 train_qwen.py:202）。

这个函数唯一的调用点在 `LazySupervisedDataset.__init__` 内（下文 4.4.3 详述），发生在任何样本被处理之前——先改预算，再开始干活。

#### 4.1.2 核心流程

```text
update_processor_pixels(processor, data_args):
    ip = processor.image_processor
    # 通道一：属性
    若 ip 有 min_pixels/max_pixels 属性:
        ip.min_pixels  ← data_args.min_pixels
        ip.max_pixels  ← data_args.max_pixels
    # 通道二：size 字典
    若 ip.size 是 dict:
        ip.size["shortest_edge"] ← data_args.min_pixels
        ip.size["longest_edge"]  ← data_args.max_pixels

    对 processor.video_processor 同样操作，额外再写:
        min_frames ← video_min_frames
        max_frames ← video_max_frames
        fps        ← video_fps
    return processor
```

要点：

- **双通道写入**：transformers 的图像处理器读取预算有两个入口——有的实现读 `min_pixels/max_pixels` 属性，有的读 `size` 字典。函数对两处都写（各有 `hasattr` 守卫），新旧实现通吃。
- **视频侧多三个旋钮**：`min_frames/max_frames` 限制抽帧数量，`fps` 决定采样率。训练路径把视频**文件**直接交给 processor，由 processor 内部抽帧（这点与 qwen-vl-utils 预采样后传 `do_sample_frames=False` 的推理路径不同，见 u2-l4）。
- **像素预算的换算口径**：`DataArguments` 的默认值是 \(28\times28\) 口径的遗产（Qwen2/2.5-VL patch=14，对齐因子 \(2\times14=28\)）。Qwen3-VL 的 patch=16，一个视觉 token 占 \( (2\times16)^2 = 1024 \) 像素。默认 `max_pixels = 28\times28\times576 = 451584\) 像素 ÷ 1024 ≈ **441 个视觉 token** 的上限；若你想要单图最多 1280 个视觉 token，应显式设 `max_pixels = 1280\times32\times32 = 1310720`。

#### 4.1.3 源码精读

图像侧：先打印改动前的值，再双通道写入。见 [data_processor.py:L44-L76](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L44-L76)——这段先取 `processor.image_processor`，把 `data_args.min_pixels/max_pixels` 同时写进属性（L56-L60）和 `size["shortest_edge"]/["longest_edge"]`（L62-L70），并用 `rank0_print` 输出 BEFORE/AFTER 对比，方便排查「预算到底改没改上」。

视频侧是同构套路再加三件套，见 [data_processor.py:L79-L137](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L79-L137)——先确认 processor 挂了 `video_processor`（L79），然后依次写 `min_pixels/max_pixels`（L92-L100）、`min_frames/max_frames`（L102-L110）、`fps`（L112-L114）和 `size` 字典（L116-L124）。

一个容易踩的坑在日志上：`rank0_print` 的开关是模块级变量 `local_rank`，见 [data_processor.py:L27-L32](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L27-L32)——`local_rank` 初始为 `None`，而 `train_qwen.py` 里虽然也有同名变量（train_qwen.py:100 会被赋值），但那是另一个模块命名空间，**不会**传播到 data_processor 模块。结果是默认情况下 `None == 0` 不成立，这些 BEFORE/AFTER 日志一条都不会打印。想在训练日志里看到预算注入过程，需要在构建数据集前手动执行 `qwenvl.data.data_processor.local_rank = 0`（实践环节会用到）。

参数默认值对照 [argument.py:L14-L26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L14-L26)：`max_pixels=28*28*576`、`min_pixels=28*28*16`、`video_max_pixels=1024*28*28` 等，全部是 28 口径；Qwen3-VL 训练脚本（如 sft_qwen3_4b.sh）都会显式覆盖为 32 口径。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到预算注入前后的 processor 状态差异。
2. **操作步骤**（示例代码，在仓库根目录执行）：

   ```python
   # peek_pixels.py —— 观察 update_processor_pixels 的注入效果
   import sys
   from types import SimpleNamespace
   sys.path.insert(0, "qwen-vl-finetune")

   import qwenvl.data.data_processor as dp          # 模块对象，待会要改它的 local_rank
   from transformers import AutoProcessor

   dp.local_rank = 0                                 # 打开 rank0_print，否则一条日志都没有

   processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
   data_args = SimpleNamespace(
       min_pixels=256 * 32 * 32, max_pixels=1280 * 32 * 32,
       video_min_pixels=256 * 32 * 32, video_max_pixels=768 * 32 * 32,
       video_min_frames=4, video_max_frames=8, video_fps=2,
   )
   processor = dp.update_processor_pixels(processor, data_args)
   print("最终 image size:", processor.image_processor.size)
   print("最终 video fps:", processor.video_processor.fps)
   ```

3. **需要观察的现象**：终端先输出 BEFORE 段（模型自带的默认预算），再输出 AFTER 段（你传入的 32 口径值），两者不同。
4. **预期结果**：`image size` 中 `shortest_edge=262144`（\(256\times1024\)）、`longest_edge=1310720`（\(1280\times1024\)）；`video fps: 2.0`。具体 BEFORE 值以本地运行为准（待本地验证）。
5. 若把 `dp.local_rank = 0` 那行注释掉再跑，应当观察到**什么日志都没有**——这正是 4.1.3 说的坑。

#### 4.1.5 小练习与答案

**练习 1**：默认 `max_pixels = 28*28*576`，对 Qwen3-VL 相当于单图多少个视觉 token 的上限？

答案：\(451584 \div 1024 = 441\) 个 token。注意实际尺寸还会被对齐到 32 的倍数（u2-l2 的 smart_resize 逻辑在 processor 内部同样生效），所以是「约 441」。

**练习 2**：为什么函数要同时写属性和 `size` 字典两个地方？

答案：transformers 不同版本/不同实现（慢速处理器 vs 快速处理器）读取像素预算的入口不一致，双写是一种兼容性防御；对应的代价是两处必须同步修改，否则预算口径会打架。

### 4.2 _build_messages：`<image>` 占位符到 messages content 的替换

#### 4.2.1 概念说明

u4-l2 讲过标注格式：每条样本是 `{image/video: 路径, conversations: [{from, value}]}` 的扁平 JSON。而 `processor.apply_chat_template` 吃的是 OpenAI 风格 messages——`content` 是 `{"type": "text"/"image"/"video"}` 元素组成的列表。`_build_messages` 就是两者之间的翻译官：**把 human 轮文本里的 `<image>`/`<video>` 占位符，原位替换成 media 池里的具体文件元素**。

#### 4.2.2 核心流程

```text
_build_messages(item, base_path):
    images ← item["image"] 归一化为 list（单字符串也包成 list）
    videos ← item["video"] 同上
    image_pool ← [{"type":"image","image": 绝对路径}, ...]
    video_pool ← [{"type":"video","video": 绝对路径}, ...]

    对每轮 turn:
        role ← "human"→user, 其余→assistant
        user 轮:
            text_parts ← re.split("(<image>|<video>)", text)   # 保留分隔符的切分
            逐段扫描:
                "<image>" → image_pool.pop(0) 进 content（池空则 ValueError）
                "<video>" → video_pool.pop(0) 进 content（池空则 ValueError）
                其他文本 → strip 后作为 {"type":"text"} 进 content
        assistant 轮:
            整段 value 原样作为纯文本 content

    收尾：image_pool / video_pool 若还有剩余 → ValueError（媒体没被占位符消耗）
```

关键设计：

- **FIFO 配对**：占位符与媒体文件按出现顺序一一配对（先进先出），与 u4-l2 讲的标注约定一致。两个方向的失配都会抛 `ValueError`：占位符多（池提前空）或媒体多（收尾有剩余）。
- **`re.split` 带捕获组**：`re.split(r"(<image>|<video>)", text)` 因为模式里有括号（捕获组），切分后分隔符本身也留在结果列表里，随后逐段判断类型——一段代码同时完成「切」和「识别」。
- **路径解析**：`(base_path / 文件).resolve()` 把标注里的相对路径拼上数据集 `data_path`（u4-l3 讲过注册表如何逐样本注入）转为绝对路径。`Path` 拼接绝对路径时会丢弃 base，所以标注里写绝对路径也安全；但**写 URL 不行**——`Path("base") / "http://..."` 会把 URL 压成一个畸形的本地路径，读取必然失败，随后被 4.4 的重试机制静默跳过。训练标注必须用本地路径。

#### 4.2.3 源码精读

媒体池构建见 [data_processor.py:L140-L156](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L140-L156)——先做单值/列表归一化（`isinstance(images, str)` 则包一层 list），再用 `_make_abs_paths` 把每个文件拼成绝对路径、打上 `type` 标签放入池子。

占位符切分与配对见 [data_processor.py:L159-L187](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L159-L187)——L160 把 `from == "human"` 映射为 user（其余一律 assistant）；L166 用捕获组切分；L168-L182 三分支处理 `<image>`/`<video>`/普通文本，其中 `pop(0)` 实现先进先出、池空即抛错；L187 表明 assistant 轮不做任何替换，value 原样作为文本。

未消耗媒体检查见 [data_processor.py:L189-L199](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L189-L199)——两个池只要非空就 `raise ValueError`，保证媒体与占位符严格等量配对。

一条真实 demo 标注（[single_images.json:L2-L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L2-L14)：human 问 "Is the value of Favorable 38 in 2015?\n<image>"，gpt 答 "Yes"）经本函数翻译后变成：

```python
[{"role": "user", "content": [
    {"type": "text", "text": "Is the value of Favorable 38 in 2015?"},
    {"type": "image", "image": "<绝对路径>/demo/images/10095.png"}]},
 {"role": "assistant", "content": [{"type": "text", "text": "Yes"}]}]
```

#### 4.2.4 代码实践

1. **实践目标**：亲手调用 `_build_messages`，看着占位符被替换、失配时抛错。
2. **操作步骤**（示例代码）：

   ```python
   # peek_build.py —— 单测 _build_messages
   import sys
   from pathlib import Path
   sys.path.insert(0, "qwen-vl-finetune")
   from qwenvl.data.data_processor import _build_messages

   ann = {
       "image": "demo/images/10095.png",
       "conversations": [
           {"from": "human", "value": "Is the value of Favorable 38 in 2015?\n<image>"},
           {"from": "gpt",   "value": "Yes"},
       ],
   }
   msgs = _build_messages(ann, base_path=Path("qwen-vl-finetune"))
   for m in msgs:
       print(m["role"], m["content"])

   # 失配实验：两个占位符、一张图
   bad = {"image": "demo/images/10095.png",
          "conversations": [{"from": "human", "value": "<image> and <image>"}]}
   try:
       _build_messages(bad, base_path=Path("qwen-vl-finetune"))
   except ValueError as e:
       print("ValueError:", e)
   ```

3. **需要观察的现象**：第一段打印中 user 轮 content 有 text 与 image 两个元素，且没有 `<image>` 字符串残留；第二段抛出「placeholders exceed the number of provided images」类 `ValueError`。
4. **预期结果**：与 4.2.3 末尾给出的 messages 结构一致；`ValueError` 的具体措辞见源码 L170-L173（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果标注里把 `<image>` 写到了 gpt 轮，会发生什么？

答案：不会报错，也不会被替换。gpt 轮走 L187 的纯文本分支，`<image>` 作为字面字符留在 assistant 答案里，之后会被当成「模型要学习的回复」进入标签区间（见 4.3），相当于教模型输出字符串 `<image>`。所以 u4-l2 强调占位符只应出现在 human 轮。

**练习 2**：一条标注有 2 张图但只有 1 个 `<image>` 占位符，错误在哪一步抛出？

答案：不在切分时，而在收尾检查（L190-L193）。切分循环只查「池是否被取空」，反向的「池有剩余」由函数末尾的 `if image_pool: raise ValueError` 兜住。

### 4.3 preprocess_qwen_visual：基于 token id 的 assistant 区间标签掩码

#### 4.3.1 概念说明

这是本讲的核心。函数做三件事：调 `_build_messages` 翻译标注 → 调 `apply_chat_template(tokenize=True)` 一步完成分词与视觉张量生成（u3-l1 讲过该模式）→ **在 token id 序列上做线性扫描，把 assistant 回复区间的 labels 从 `-100` 恢复为真实 token id**。

两个魔法数字的含义：

- `77091`：Qwen 分词器把单词 `assistant` 编码出的普通 BPE token id（Qwen2/2.5/3 系列共用同一套 151664 词表）。它出现在 ChatML 渲染结果中每个 assistant 轮的 `<|im_start|>` 之后，是「这里开始一段助手回答」的位置信号。
- `151645`：`<|im_end|>`，assistant 回答的结束符，特殊 token。

为什么用「扫 token id」而不是「扫字符串」？因为 `apply_chat_template` 的输出只剩 id，而 id 匹配比字符串匹配更快也更精确（不受分词边界影响）。

#### 4.3.2 核心流程

先看 ChatML 渲染后一轮 assistant 回答的 token 布局（位置从左到右）：

```text
...  <|im_start|>  assistant   \n    回复内容 C1..Cn   <|im_end|>   \n    ...
        151644      77091      198                      151645     198
                      ↑pos        ↑ans_start=pos+2        ↑ans_end   ↑ans_end+1
```

扫描算法：

```text
labels ← 与 input_ids 等长、全填 -100
pos ← 0
while pos < L:
    if input_ids[pos] == 77091:                # 命中 assistant 标记
        ans_start ← pos + 2                    # 跳过 77091 和它后面的换行 token
        ans_end ← 从 ans_start 向右线性扫，直到命中 151645（<|im_end|>）
        if ans_end < L:                        # 真的找到了结束符
            labels[ans_start : ans_end+2] ← input_ids 同区间
            pos ← ans_end                      # 跳到结束符，继续找下一轮
    pos ← pos + 1
```

监督区间 \([ans\_start, ans\_end+2)\) 恰好覆盖三段：**回复内容 + `<|im_end|>` + 结束符后紧跟的换行 token**。把结束符（及其换行）纳入监督是有意为之——模型不仅要学会「说什么」，还要学会「何时闭嘴」，否则生成永不停止。多轮对话时每个 assistant 轮都会各自命中一次 77091，产生多个监督区间；user 轮（包括展开后的 `<|image_pad|>` 视觉 token）保持 `-100` 永不监督。

两个防御性细节：

- 若某轮扫到序列末尾都没找到 151645（畸形样本），`if ans_end < L` 不成立，该轮 labels 保持全 `-100`——这一轮静默不监督，训练不中断。
- `labels` 与 `input_ids` 逐位对齐、不做位移：右移一位的操作（`logits[:, :-1]` 对 `labels[:, 1:]`）由模型 forward 内部的 loss 计算完成，这是 HF 的统一约定。

#### 4.3.3 源码精读

整体结构见 [data_processor.py:L202-L221](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L202-L221)——L206-L207 要求输入恰好一条源标注；L210-L211 取 `data_path` 并调 `_build_messages`；L213-L215 用 `tokenize=True, return_dict=True, return_tensors="pt"` 一步拿到 `input_ids/pixel_values/image_grid_thw` 等全部张量；L221 把 labels 初始化为全 `-100` 的模板。

标签扫描循环见 [data_processor.py:L223-L237](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L223-L237)——L227 命中 77091；L228 设 `ans_start = pos + 2`（跳过标记与换行两个 token，对应 4.3.2 布局图）；L230-L231 内层 while 线性寻找 151645；L232-L235 把区间 `[ans_start, ans_end+2)` 的 labels 恢复为真实 id（注意切片右端是 `ans_end + 2`，含结束符后一个 token）；L236 把 pos 挪到结束符处以便继续扫下一个 assistant 轮。

结果写回见 [data_processor.py:L239-L241](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L239-L241)——labels 与 input_ids 一起塞回 `full_result` 字典返回。

文件头部的常量 [data_processor.py:L21-L25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L21-L25) 定义了 `IGNORE_INDEX=-100` 与 `IMAGE_TOKEN_INDEX=151655`（`<|image_pad|>`）、`VIDEO_TOKEN_INDEX=151656`（`<|video_pad|>`）——后两个在本文件主要是语义记录，真正的逐 token 扫描发生在 rope2d.py（u5-l4）。

关于「77091 为什么一定是 `assistant` 而不含换行」：代码里 `ans_start = pos + 2` 要求 77091 与回复内容之间恰隔一个 token。ChatML 模板里 `<|im_start|>assistant\n内容` 中 `assistant` 与内容之间只有换行符一个 token，与 `+2` 偏移严格吻合；若 77091 是 `assistant\n` 的合并 token，内容会落在 `pos+1`，官方代码不会每轮丢掉回复的第一个字。用 `tokenizer.decode([77091])` 一行即可本地验证。

#### 4.3.4 代码实践

1. **实践目标**：验证监督区间解码后恰好是 assistant 回复（含结束符）。
2. **操作步骤**（示例代码，在仓库根目录执行；只需下载 processor，无需模型权重）：

   ```python
   # peek_labels.py —— 复现标签掩码并解码验证
   import sys
   from pathlib import Path
   sys.path.insert(0, "qwen-vl-finetune")
   from transformers import AutoProcessor
   from qwenvl.data.data_processor import preprocess_qwen_visual

   processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
   print("验证 77091 =", repr(processor.tokenizer.decode([77091])))   # 预期 'assistant'
   print("验证 151645 =", repr(processor.tokenizer.decode([151645]))) # 预期 '<|im_end|>'

   ann = {
       "image": "demo/images/10095.png",
       "conversations": [
           {"from": "human", "value": "Is the value of Favorable 38 in 2015?\n<image>"},
           {"from": "gpt",   "value": "Yes"},
       ],
       "data_path": "qwen-vl-finetune/demo",
   }
   out = preprocess_qwen_visual([ann], processor)

   ids, labs = out["input_ids"][0], out["labels"][0]
   print("input_ids:", ids.shape, " labels:", labs.shape)
   print("pixel_values:", out["pixel_values"].shape, " image_grid_thw:", out["image_grid_thw"])

   keep = (labs != -100).nonzero().squeeze(1)
   print("监督区间:", keep[0].item(), "~", keep[-1].item(), "共", keep.numel(), "个 token")
   print("监督内容:", repr(processor.tokenizer.decode(labs[keep], skip_special_tokens=False)))
   ```

3. **需要观察的现象**：77091 解码为 `'assistant'`；监督区间的解码结果只含 gpt 答案与结束符，不含任何提问内容。
4. **预期结果**：监督内容形如 `'Yes<|im_end|>\n'`（内容 + 结束符 + 换行）；监督区间应是**连续一段**。具体形状数值待本地验证（取决于图片经像素预算缩放后的网格）。
5. 加一步交叉检查：`processor.tokenizer.decode(ids[:keep[0].item()], skip_special_tokens=False)` 解码监督区间之前的部分，应当是 system/user 内容与展开的 `<|image_pad|>`。

#### 4.3.5 小练习与答案

**练习 1**：把 demo 标注改造成两轮问答（human/gpt/human/gpt），监督区间有几个？分别解码出什么？

答案：两个。每轮各命中一次 77091，各自从 `pos+2` 扫到本轮 `<|im_end|>`；解码结果依次是两轮 gpt 答案（各带结束符与换行），user 轮不受监督。

**练习 2**：为什么区间右端是 `ans_end + 2` 而不是 `ans_end + 1`？

答案：Python 切片 `[a : b]` 不含 b。写 `ans_end + 2` 实际覆盖到下标 `ans_end + 1`，即 `<|im_end|>`（ans_end 处）及其后一个换行 token（ans_end+1 处）。若写 `+1` 就漏掉结束符本身，模型学不会停止。

**练习 3**：如果标注的 gpt 答案文本里混入了字符串 `"<|im_end|>"`（u4-l2 明确禁止），监督会怎样？

答案：分词器会把它编码成真实的 151645，内层 while 在这里提前命中「结束符」，监督区间被提前截断，其后的答案文本失去监督。这就是 gpt 轮答案必须纯净的原因之一。

### 4.4 `__getitem__` 三层容错与 `_get_item` 张量组装

#### 4.4.1 概念说明

分布式训练动辄几万条样本、几百个小时，只要有**一条**样本让 `__getitem__` 抛异常，整个训练进程就会崩溃。坏样本的常见来源：媒体文件损坏、路径拼错、标注占位符不配对（前两个模块的 ValueError）、云盘瞬时不可读。`LazySupervisedDataset.__getitem__` 用三层重试把这些故障兜住，代价是**坏样本会被悄悄换成相邻样本**——这也解释了 u4-l2 为什么反复强调训练前先跑 `check_image.py` 清洗数据。

`__init__` 侧还有一个本讲必须交代的角色：它是 `update_processor_pixels` 的唯一调用点，并完成「读注册表 → 读标注 → 逐条注入 data_path → 选 rope 函数 → 选 item_fn」的全部初始化（lazy 模式：不读图、不分词）。

#### 4.4.2 核心流程

`__init__`（初始化一次）：

```text
dataset_use.split(",") → data_list()           # u4-l3 的注册表解析（含 %X 采样率）
按 model_type 绑定 get_rope_index（qwen3vl→_3, qwen2.5vl→_25, qwen2vl→_2）
逐数据集读 JSON/JSONL → random.sample 按采样率抽样 → 逐条(桶逐子)注入 data_path
update_processor_pixels(processor, data_args)   # 4.1 的预算注入发生在这里
item_fn ← data_packing ? _get_packed_item : _get_item
```

`__getitem__(i)`（每条样本）：

```text
第一层：原样本 i 重试 3 次，每次失败 sleep 1 秒     # 怀疑云盘/网络抖动
第二层：改取相邻样本 min(i+1, len-1) 重试 3 次，无 sleep  # 怀疑文件本身损坏
第三层：再试一次原样本 i，仍失败 → raise          # 确认失败，让训练停下
```

注意第一层与第二层的**诊断假设不同**：可恢复的环境抖动值得等待重试；文件损坏重试无意义，直接换样本。若相邻样本也坏，第二层每次都会推进一格吗？不会——`next_index` 恒为 `min(i+1, len-1)`，三次都试同一条相邻样本，仍失败则进入第三层对原样本做最后一次尝试后抛出。

`_get_item`（单样本张量组装）：

```text
preprocess_qwen_visual → input_ids/labels/pixel_values/grid_thw
image_grid_thw / video_grid_thw 归一化为 list
视频：second_per_grid_ts = temporal_patch_size / fps （每视频一份）
get_rope_index(merge_size, input_ids, grids, second_per_grid_ts) → 3×L 的 position_ids
attention_mask ← [seq_len]        # 注意：是长度列表，不是 0/1 掩码！
```

`attention_mask = [seq_len]` 是个反直觉设计：常规 collator 会忽略它、用 `input_ids.ne(pad)` 重建 0/1 掩码；而 Flattened collator（u5-l5）恰恰需要「每段序列的真实长度」，在 collator 里对 batch 内所有段做 `cumsum` 得到 varlen 注意力的分段边界。这里先埋下伏笔即可。

#### 4.4.3 源码精读

初始化主体见 [data_processor.py:L247-L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L247-L267)——L250-L251 解析 `dataset_use` 并查注册表；L260-L267 按 `data_args.model_type` 绑定对应代际的 rope 函数（该字段由 train_qwen.py 的模型路径嗅探写入，见 u5-l1），不认识的类型直接抛错。

标注加载与 data_path 注入见 [data_processor.py:L269-L293](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L269-L293)——按扩展名分流 JSON/JSONL（L272-L276）；`sampling_rate < 1.0` 时 `random.sample` 无种子抽样（L277-L282，u4-l3 讲过不可复现的坑）；L285-L290 对普通样本注入一次 `data_path`，对 packed 桶（`isinstance(ann, list)`）逐个子样本注入。

预算注入调用点与 item_fn 分派见 [data_processor.py:L296-L307](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L296-L307)——L297 就是 `update_processor_pixels(processor, data_args)` 的唯一调用处；L304-L307 按 `data_packing` 开关选择 `item_fn`，把「取普通样本还是取桶」的分派固化在初始化期，而不是每次取样本时判断。

三层重试见 [data_processor.py:L345-L387](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L345-L387)——L350-L360 第一层对原样本尝试 3 次、失败 `time.sleep(1)`（注释点明是针对云盘问题）；L362-L378 第二层固定取 `min(i+1, len-1)` 的相邻样本再试 3 次；L380-L387 第三层最后一次尝试原样本、失败 `raise`。顺带一提：L347 定义的 `num_final_retries = 30` 从未被使用，是个遗留变量——读代码时能识别「定义未用」也是基本功。

`_get_item` 组装见 [data_processor.py:L389-L440](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L389-L440)——L395 取序列长度；L397-L414 把 `image_grid_thw/video_grid_thw` 归一化为列表，并为每个视频计算 `second_per_grid_ts = temporal_patch_size / fps`（L408-L411，这是喂给 rope2d 的时间步长，u5-l4 展开）；L416-L424 调用按代际绑定的 `get_rope_index` 得到 `position_ids`；L427 写下 `attention_mask = [seq_len]` 这个长度列表。

段尾的 [data_processor.py:L429-L438](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L429-L438) 是一段**调试残留**：`text` 与 `label` 两个局部变量解码后从未返回、也未被使用。但它展示了一个极实用的调试技巧——把 labels 里的 `-100` 替换成 `pad_token_id` 再解码（L433-L438），肉眼即可检查掩码是否框对了区间。本讲 4.3.4 的实践正是这个技巧的复刻（我们用切片比替换更精确）。

最后看 packed 分支的一个隐患：[data_processor.py:L442-L448](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L442-L448) 处理「单条 dict」的分支里，L445 判断的是未定义变量 `source`（应为 `sources`），一旦 `data_packing=True` 而标注文件里混入了普通 dict 样本，就会触发 `NameError`，被 `__getitem__` 的重试机制吞掉、换成相邻样本。所以开 packing 时标注必须全部是 pack_data.py 产出的桶结构（u4-l4）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼观察「坏样本被静默换成相邻样本」。
2. **操作步骤**（示例代码；先复制一份标注再改，勿动源文件）：

   ```python
   # peek_retry.py —— 观察三层容错
   import json, sys, time
   from pathlib import Path
   from types import SimpleNamespace
   sys.path.insert(0, "qwen-vl-finetune")
   import qwenvl.data.data_processor as dp
   from qwenvl.data import data_dict
   from transformers import AutoProcessor

   root = Path("qwen-vl-finetune").resolve()
   anns = json.loads((root / "demo/single_images.json").read_text())
   anns[0]["image"] = "demo/images/NOT_EXIST.png"      # 人为制造坏样本
   Path("/tmp/bad_demo.json").write_text(json.dumps(anns))

   data_dict["bad_demo"] = {"annotation_path": "/tmp/bad_demo.json", "data_path": str(root)}
   data_args = SimpleNamespace(
       dataset_use="bad_demo", data_packing=False, model_type="qwen3vl",
       min_pixels=256*32*32, max_pixels=1280*32*32,
       video_min_pixels=256*32*32, video_max_pixels=768*32*32,
       video_min_frames=4, video_max_frames=8, video_fps=2,
   )
   ds = dp.LazySupervisedDataset(AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct"), data_args)
   t0 = time.time()
   sample = ds[0]                                       # 取坏样本
   print("耗时 %.1f 秒" % (time.time() - t0))
   keep = (sample["labels"][0] != -100).nonzero().squeeze(1)
   print("拿到的样本答案:", repr(ds.processor.tokenizer.decode(sample["labels"][0][keep])))
   ```

3. **需要观察的现象**：终端先打印 `[Try #0]`、`[Try #1]`、`[Try #2]` 三次失败日志（间隔约 1 秒，来自第一层的 sleep），随后 `[Try other #0]` 成功；返回的样本答案**不是**标注第 0 条的 "Yes"，而是第 1 条的 "6"。
4. **预期结果**：总耗时约 3 秒（三次 sleep）；`ds[0]` 实际返回了 `list_data_dict[1]` 的内容——「换样本」实锤。具体打印格式见源码 L359/L374（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：第一层重试为什么每次 `sleep(1)`，第二层却不 sleep？

答案：第一层怀疑的是云盘/网络等**瞬时**故障，等待后可能自愈；第二层怀疑的是文件本身损坏，等待无意义，直接换数据源。

**练习 2**：坏样本被换成相邻样本后，训练统计（比如一个 epoch 的实际样本数）还准吗？

答案：不准。`len(dataset)` 仍按原始标注数计算，实际消费的是「好样本可能被重复取、坏样本被跳过」的序列，epoch 内样本分布悄然改变。这正是要用 check_image.py 在训练前把坏样本清掉、而不是依赖运行时容错的理由。

**练习 3**：`_get_item` 返回的 `attention_mask` 是什么形状和语义？常规 collator 会用它吗？

答案：是仅含一个元素 `seq_len` 的 Python 列表，语义是「这条样本的真实长度」，不是 0/1 掩码。常规 `DataCollatorForSupervisedDataset` 忽略它并用 `input_ids.ne(pad_token_id)` 重建掩码；只有 Flattened collator 把 batch 内各段长度 cumsum 成 varlen 边界（u5-l5）。

## 5. 综合实践

把本讲四个模块串成一个「单步透视脚本」，对 demo 第 0 条样本完成从 JSON 到张量的全程检查（示例代码）：

```python
# inspect_sample.py —— 透视 LazySupervisedDataset 的完整加工链（仓库根目录执行）
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "qwen-vl-finetune")
import qwenvl.data.data_processor as dp
from qwenvl.data import data_dict
from transformers import AutoProcessor

# ① 注册 demo 数据集（u4-l3 的机制）：标注相对 data_path 解析
root = Path("qwen-vl-finetune").resolve()
data_dict["my_demo"] = {
    "annotation_path": str(root / "demo/single_images.json"),
    "data_path": str(root),                # 标注里的 demo/images/xxx.png 拼在此目录下
}

# ② 模拟 DataArguments（u5-l2），全部用 32 口径预算
data_args = SimpleNamespace(
    dataset_use="my_demo", data_packing=False, model_type="qwen3vl",
    min_pixels=256*32*32, max_pixels=1280*32*32,
    video_min_pixels=256*32*32, video_max_pixels=768*32*32,
    video_min_frames=4, video_max_frames=8, video_fps=2,
)

dp.local_rank = 0                          # 打开 rank0_print，观察 update_processor_pixels 日志
processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
ds = dp.LazySupervisedDataset(processor, data_args)
print("样本总数:", len(ds))

# ③ 单步观察：__getitem__ → _get_item → preprocess_qwen_visual → get_rope_index
sample = ds[0]
for k in ("input_ids", "labels", "position_ids"):
    print(f"{k:12s}", sample[k].shape)
print("pixel_values ", sample["pixel_values"].shape)
print("image_grid_thw", sample["image_grid_thw"].tolist(),
      "→ 视觉 token 数 =", (sample["image_grid_thw"].prod() // 4).item())
print("attention_mask", sample["attention_mask"], "← 是长度列表，不是 0/1 掩码")

# ④ 标签掩码验证：非 -100 区间应恰好解码为 gpt 答案 + 结束符
labs = sample["labels"][0]
keep = (labs != -100).nonzero().squeeze(1)
print("监督 token 数:", keep.numel(), " 区间:", keep[0].item(), "~", keep[-1].item())
print("监督内容:", repr(processor.tokenizer.decode(labs[keep], skip_special_tokens=False)))
print("标注中的 gpt 答案: 'Yes'（single_images.json 第 0 条）")
```

检查清单（预期现象）：

1. `update_processor_pixels` 打出 BEFORE/AFTER 两段日志，证明预算注入发生在 `__init__`。
2. `image_grid_thw` 形如 `[1, H, W]`，其乘积除以 4（\(2\times2\) 空间合并，u2-l1）即为视觉 token 数；`input_ids` 长度 ≈ 文本 token + 该值。
3. `position_ids` 形状为 `[3, 1, L]`——三行正是 MRoPE 的 t/h/w（u5-l4 深入）。
4. 监督内容解码为 `'Yes<|im_end|>\n'`，与标注 gpt 答案吻合，且只有一个连续监督区间。
5. 把 `data_args.max_pixels` 调大到 `1280*32*32*4` 再跑一遍，观察 `pixel_values` 变大、`input_ids` 变长——预算、网格、序列长度三者的联动在本脚本中一目了然。

运行前提：`transformers>=4.57.0`、能访问 Hugging Face 下载 2B 模型的 processor（无需权重）；各项具体数值待本地验证。

## 6. 本讲小结

- `LazySupervisedDataset.__init__` 完成注册表解析、标注加载（lazy 不分词不读图）、逐条注入 `data_path`、按 `model_type` 绑定 rope 函数，并在唯一调用点 [data_processor.py:L297](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L297) 用 `update_processor_pixels` 把命令行像素预算双通道（属性 + `size` 字典）写进 processor。
- `_build_messages` 用带捕获组的 `re.split` 把 human 轮文本按 `<image>/<video>` 切开，占位符与媒体池 `pop(0)` 先进先出配对，双向失配都抛 `ValueError`；gpt 轮纯文本直通；标注必须是本地路径。
- 标签掩码的核心是一次 token id 线性扫描：命中 `77091`（`assistant`）后从 `pos+2` 起步，向后扫到 `151645`（`<|im_end|>`），把区间 `[ans_start, ans_end+2)` 的 labels 从 `-100` 恢复为真实 id——恰好监督「回复内容 + 结束符 + 换行」，user 轮与视觉 token 永不监督，shift 交给模型内部 loss。
- `__getitem__` 三层容错（原样本×3 带 sleep → 相邻样本×3 → 原样本最后一次）保证单条坏样本不炸训练，副作用是静默换样本——训练前用 `check_image.py` 清洗才是正解。
- `_get_item` 尾部的 `attention_mask = [seq_len]` 是「长度列表」而非掩码，专为 u5-l5 的 Flattened collator cumsum 埋线；尾部的 decode 调试残留恰好是验证标签掩码的好技巧。
- 两个「读代码收获」：`rank0_print` 因模块级 `local_rank=None` 默认静默；`num_final_retries=30` 与 packed 分支的 `isinstance(source, ...)` 均为遗留问题。

## 7. 下一步学习建议

本讲到 `position_ids` 的产生为止——它来自 [data_processor.py:L416-L424](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L416-L424) 调用的 `get_rope_index_3`。下一讲 **u5-l4（rope2d.py：三代 MRoPE 位置编码与 Interleaved-MRoPE）** 将打开这个黑盒，对比三代实现的 t/h/w 计算，解释 Qwen3-VL 为何 `t_index` 恒为 0、时间信息如何改走文本时间戳。之后 **u5-l5（两种 DataCollator）** 会接住本讲埋下的两条线：`[seq_len]` 长度列表如何 cumsum 成 varlen 注意力边界，以及 packed 桶在 collator 阶段的拼接。建议你在进入下一讲前，先把本讲综合实践跑通——亲眼看过一个 `[3, 1, L]` 的 position_ids，下一讲的抽象公式就有了着力点。
