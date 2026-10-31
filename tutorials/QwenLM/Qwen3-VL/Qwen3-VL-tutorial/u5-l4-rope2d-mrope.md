# rope2d.py：三代 MRoPE 位置编码与 Interleaved-MRoPE

## 1. 本讲目标

读完本讲，你应该能够：

- 说出 `position_ids` 三行（t/h/w）分别编码什么维度，以及为什么纯文本段三行完全相同。
- 读懂 `rope2d.py` 主循环的扫描骨架：如何用特殊 token 定位视觉段、如何交替生成文本段与视觉段的位置 id、段与段如何衔接。
- 对比三代实现的 `t_index` 差异：Qwen2-VL 的帧序号、Qwen2.5-VL 的 T-RoPE 绝对时间步进、Qwen3-VL 的时间戳方案（`t_index` 恒为 0）。
- 解释 Qwen3-VL 为什么敢于把时间维位置 id 全部置 0，以及这与 YaRN factor 只取 2~3 之间的因果关系。
- 亲手构造 `input_ids`，调用三个函数打印 3×L 的 `position_ids` 并验证自己的理解。

## 2. 前置知识

**RoPE（旋转位置编码）一句话直觉**：把每个 token 的查询/键向量按其"位置"旋转一个角度，两个 token 的注意力得分只依赖相对角度差，从而自然获得相对位置感知。标准 LLM 只有一个一维位置轴：第 i 个 token 的位置就是 i。

**MRoPE（多模态旋转位置编码）**：视觉 token 天然生活在三维网格里——时间 t（第几帧）、高 h（第几行 patch）、宽 w（第几列 patch）。如果仍用一维位置 0,1,2,…,1024，那么"同一帧的左右相邻 patch"和"跨帧的 patch"在位置轴上无法区分。MRoPE 的做法是把旋转维度切给三个轴，为每个视觉 token 同时给三个坐标：

\[ \text{position\_ids} = \begin{pmatrix} t_0 & t_1 & \cdots & t_{L-1} \\ h_0 & h_1 & \cdots & h_{L-1} \\ w_0 & w_1 & \cdots & w_{L-1} \end{pmatrix} \]

于是 `position_ids` 的形状是 `(3, batch_size, seq_len)`——三行分别是 t/h/w 坐标。**文本 token 没有空间结构，三行退化为同一个一维序列**，这就是"文本段三行相同"的原因。

**grid_thw 与 spatial_merge_size**：视觉编码器（ViT）输出的 patch 网格记作 `(t, h, w)`，即 `grid_thw`。进入 LLM 前，2×2 空间合并（spatial merge，本仓库恒为 2）会把相邻 4 个 patch 拼成 1 个 token，所以 LLM 侧网格是 `(t, h/2, w/2)`，一张图的视觉 token 数为：

\[ N_{vision} = t \times \frac{h}{2} \times \frac{w}{2} \]

例如 `grid_thw=[1,64,64]` 的图在 LLM 侧是 1×32×32 = 1024 个视觉 token。这与前几讲"一张 H×W 图约 (H/32)×(W/32) 个视觉 token"的说法完全一致。

**本讲登场的特殊 token id**（在 u5-l3 的 77091/151645 之外新增三个，均为 Qwen3-VL 词表中的固定 id）：

| id | 含义 | 在 rope2d.py 中的定义处 |
|---|---|---|
| 151652 | `<|vision_start|>`，视觉块开头 | 每个函数开头的 `vision_start_token_id` |
| 151655 | `<|image_pad|>`，图像占位符 | `image_token_id` |
| 151656 | `<|video_pad|>`，视频占位符 | `video_token_id` |

**时间维 2× 压缩**：视频采样时每 2 帧合成 1 个 temporal patch（u2-l3 讲过的 FRAME_FACTOR=2），所以 `grid_thw` 的 t 本身已经是"帧对"为单位。记住这一点，后面看 Qwen2.5-VL 的 `t_index` 公式时那个 ×2 就不神秘了。

**本讲在数据管线中的位置**：u5-l3 讲到 `preprocess_qwen_visual` 产出 `input_ids`、`labels`、`grid_thw` 后，还有一句"position_ids 与 collator 分别留给后两讲"——本讲就是补上 position_ids 这一环。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [qwen-vl-finetune/qwenvl/data/rope2d.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py) | 本讲主角。三个纯函数 `get_rope_index_3` / `get_rope_index_25` / `get_rope_index_2`，分别对应 Qwen3-VL / Qwen2.5-VL / Qwen2-VL 的位置 id 计算，只依赖 torch，无任何项目内依赖 |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 消费方。按 `model_type` 选择三个函数之一（L259-267），在 `_get_item` 中组装参数并调用（L416-424） |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 官方对 Interleaved-MRoPE、文本-时间戳对齐的表述（L50、L54），以及 YaRN factor 取 2~3 的原因说明（L792-795） |

三者的关系一句话：`data_processor.py` 决定"用哪个函数、传什么参数"，`rope2d.py` 决定"每个 token 的三个坐标是什么"，README 提供"为什么这样设计"的官方口径。

## 4. 核心概念与源码讲解

### 4.1 模块一：纯文本路径——三行退化的 position_ids

#### 4.1.1 概念说明

三个函数都有同一个 `else` 分支：当 `input_ids` 为 None，或 `image_grid_thw`/`video_grid_thw` 都为 None（即序列里没有任何图像和视频）时，走纯文本路径。这条路径要回答的问题是：**没有视觉 token 时，MRoPE 如何退化为普通 RoPE？** 答案是三行填同样的 0,1,2,…,L-1。

#### 4.1.2 核心流程

```
输入：input_ids (B, L)，可选 attention_mask (B, L)
若有 attention_mask（batch 内有 padding）：
    position_ids = attention_mask 累加和 - 1        # 跳过 padding 计数
    padding 位置填 1                                 # 占位值，不参与注意力
    复制 3 份 → (3, B, L)
    delta = 每样本最大位置 id + 1 - 序列长度          # 此处恒为 0
若没有 attention_mask：
    position_ids = arange(L) 复制 3 份 → (3, B, L)
    delta = 全 0
```

关键点：`cumsum - 1` 保证左 padding（batch 推理的标准姿势，见 u3-l1）不计入位置，真实 token 仍从 0 开始连续编号。

#### 4.1.3 源码精读

以 `get_rope_index_2` 的 else 分支为例（三个函数的这一段逐字相同）：

[rope2d.py:L503-L526](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L503-L526)：纯文本 fallback。有 attention_mask 时用 `cumsum(-1) - 1` 跳过 padding 生成一维位置，再 `masked_fill_` 把 padding 位填 1，最后 `unsqueeze(0).expand(3, -1, -1)` 复制成三行；没有 attention_mask 时直接 `arange` 后展开三行。两条支路都返回 `(3, B, L)` 的张量与全零的 `mrope_position_deltas`。

对应的 Qwen3-VL 版本在 [rope2d.py:L103-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L103-L122)，逻辑同构。

#### 4.1.4 代码实践

1. **实践目标**：确认纯文本输入下三行 position_ids 完全相同、delta 恒为 0。
2. **操作步骤**：把下面脚本保存为 `text_path.py`，在装好 torch 的环境运行（`rope2d.py` 无项目内依赖，可直接把仓库路径加入 `sys.path`）。以下为示例代码：

   ```python
   import sys, torch
   sys.path.insert(0, "path/to/Qwen3-VL/qwen-vl-finetune")  # 让 qwenvl 可导入
   from qwenvl.data.rope2d import get_rope_index_2, get_rope_index_25, get_rope_index_3

   input_ids = torch.tensor([[100, 101, 102, 103, 104]])
   for name, fn in [("v2", get_rope_index_2), ("v2.5", get_rope_index_25), ("v3", get_rope_index_3)]:
       pos, delta = fn(2, input_ids=input_ids)
       print(name, pos.shape, pos.tolist(), delta.tolist())
   ```

3. **需要观察的现象**：三个函数输出的 `pos` 形状都是 `[3, 1, 5]`，且三行都是 `[[0, 1, 2, 3, 4]]`；`delta` 为 `[[0.]]`。
4. **预期结果**：三代实现纯文本行为完全一致——MRoPE 对纯文本就是普通 RoPE。
5. 数值为我按源码手推，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么纯文本路径要把一维位置复制成三行，而不是直接返回 `(1, B, L)`？
**答案**：模型的 MRoPE 模块按固定形状 `(3, B, L)` 读取三路坐标并分别切给 t/h/w 三段旋转维度，输入形状必须统一；三行填相同值即"三个轴坐标相等"，数学上等价于一维 RoPE。

**练习 2**：batch 推理时 attention_mask 左 padding 为 `[0,0,1,1,1]`，真实 token 的位置 id 是多少？
**答案**：`cumsum - 1` 得 `[-1,-1,0,1,2]`，padding 位再被 `masked_fill_` 改成 1，最终真实 token 位置为 `[0,1,2]`——padding 不挤占位置编号。

### 4.2 模块二：图像/视频混合扫描——主循环骨架与段间衔接

#### 4.2.1 概念说明

这是三代实现共享的"骨架"（代码几乎逐行相同），解决的问题是：**一条混排了文本与多个视觉块的序列，如何切成"文本段 / 视觉段"交替的片段，并为每段生成正确的三行坐标？** 三代差异只藏在视觉段 t 行的计算里（模块四详述），骨架本身自 Qwen2-VL 起一脉相承。

#### 4.2.2 核心流程

```
对 batch 中每条序列（用 attention_mask 剔除 padding）：
  ① 探测视觉块：找所有 151652 (<|vision_start|>) 的下标，
     取每个其后一个 token，是 151655 记 1 张图、是 151656 记 1 段视频
  ② st = 0；重复 图数+视频数 次：
     a. 从 st 起找下一个 151655 / 151656 的下标 ed_image / ed_video，
        取较小者决定当前处理的是图还是视频（按出现顺序消费 grid_thw 行）
     b. [st, ed) 是文本段：三行都是 arange(text_len) + st_idx
     c. 视觉段：llm_grid = (t, h//2, w//2)，按三维网格展开：
        t 行：帧号 i 重复 (llm_h × llm_w) 次
        h 行：行号 r 重复 llm_w 次
        w 行：列号 c 以 llm_w 为周期循环
        三行整体 + (text_len + st_idx)
     d. st = ed + llm_t × llm_h × llm_w     # 跳过占位符展开后的全部视觉 token
  ③ 若 st < 序列长度，剩余全部按文本段处理
  ④ 段间衔接规则：st_idx = 上一段三行最大值 + 1
```

h/w 两行的展开顺序值得注意：视觉 token 的排列是"逐帧、逐行、逐列"的行优先顺序，所以 h 行每隔 `llm_w` 个 token 加 1，w 行以 `llm_w` 为周期循环——这正是 `view(...).expand(...).flatten()` 三连做的事。

#### 4.2.3 源码精读

以 `get_rope_index_2`（骨架最干净的版本）为主线：

[rope2d.py:L406-L415](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L406-L415)：先剔除 padding，再用 `torch.argwhere(input_ids == vision_start_token_id)` 找到全部视觉块开头，`input_ids[vision_start_indices + 1]` 取出每个块内的第一个占位符 token，与 151655/151656 比对统计出图数和视频数。这就是"探测"步骤的原文。

[rope2d.py:L419-L436](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L419-L436)：主循环里用 `input_tokens.index(image_token_id, st)` 从当前起点向后找下一个图像占位符；图与视频谁先出现处理谁（`ed_image < ed_video` 分支），并从对应的 `grid_thw` 里按序取出一行 `(t, h, w)`。注意 `image_index`/`video_index` 是两个独立的递增指针——第 3 张图永远配 `image_grid_thw[2]`，与视频穿插无关。

[rope2d.py:L446-L458](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L446-L458)：`llm_grid_t/h/w = (t, h//spatial_merge_size, w//spatial_merge_size)` 完成 ViT 网格到 LLM 网格的换算；随后 `[st, ed)` 的文本段以 `st_idx` 为起点生成三行相同的一维坐标。`st_idx = llm_pos_ids_list[-1].max() + 1 if len(...) > 0 else 0` 就是段间衔接规则的原文——**新段起点 = 上一段三行最大值 + 1**。

[rope2d.py:L460-L481](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L460-L481)：视觉段三行的生成。`t_index` 把帧号沿 `llm_h*llm_w` 重复（一帧的所有 patch 共享帧号）；`h_index` 沿 w 维展开；`w_index` 沿 h 维展开。`torch.stack([t_index, h_index, w_index]) + text_len + st_idx` 把三行堆叠并平移。最后 `st = ed + llm_grid_t*llm_grid_h*llm_grid_w` 一次性跳过整块展开的视觉 token——**输入序列里视觉占位符必须已被 processor 展开成恰好这个数量**，否则段切分就错位了。

[rope2d.py:L483-L498](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L483-L498)：尾部剩余文本段补齐；`torch.cat` 拼回 `(3, L')` 并写回 `position_ids[..., i, attention_mask[i]==1]`。`mrope_position_deltas.append(llm_positions.max() + 1 - len(total_input_ids[i]))` 记录"最大位置 id + 1 与序列长度之差"——视觉段的 3D 坐标推进远慢于 token 数（1024 个 token 的图最多只把位置推到 32×32 网格的边缘），所以这个差通常是很大的负数，它供推理阶段生成新 token 时递增位置 id 使用；训练侧在 [data_processor.py:L416](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L416) 用 `position_ids, _ = ...` 直接丢弃。

`get_rope_index_2` 自己的 docstring 给了一个最小算例，值得背下来（t=3, h=2, w=2 的视频 + 5 个文本 token）：

[rope2d.py:L360-L368](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L360-L368)：12 个视觉 token 的 t 行是 `[0,0,0,0, 1,1,1,1, 2,2,2,2]`（帧号×4）、h 行是 `[0,0,1,1]` 循环、w 行是 `[0,1,0,1]` 循环；随后 5 个文本 token 从 `max(vision)+1 = 3` 起三行同走 `[3,4,5,6,7]`。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现 docstring 的算例，验证"段间衔接 = 前段最大值 + 1"。
2. **操作步骤**：运行以下示例代码（无需真实模型，token id 手工构造）：

   ```python
   import sys, torch
   sys.path.insert(0, "path/to/Qwen3-VL/qwen-vl-finetune")
   from qwenvl.data.rope2d import get_rope_index_2

   T, VS, VID = 100, 151652, 151656
   seq = [T, T] + [VS] + [VID] * (3*2*2) + [T, T]   # llm 网格 3×2×2 → 12 个视觉 token
   input_ids = torch.tensor([seq])
   pos, delta = get_rope_index_2(2, input_ids, video_grid_thw=torch.tensor([[3, 4, 4]]))
   print(pos[0, 0])       # 3×L
   print("delta =", delta)
   ```

3. **需要观察的现象**：第 3 列（VS 属于文本段）起，前 3 个 token 三行为 `[0,1,2]`；12 个视觉 token 的 t 行 `[3]*4+[4]*4+[5]*4`、h 行 `[3,3,4,4]` 循环、w 行 `[3,4,3,4]` 循环；末尾 2 个文本 token 三行 `[6,7]`。
4. **预期结果**：与 docstring（L362-367）平移 3 位后完全一致；`delta = 8 - 17 = -9`。
5. **待本地验证**（以上数值为按源码手推）。

#### 4.2.5 小练习与答案

**练习 1**：一条序列里先出现视频 A 再出现图 B，`grid_thw` 该怎么传？
**答案**：分两个张量传——`video_grid_thw=[[tA,hA,wA]]`、`image_grid_thw=[[tB,hB,wB]]`。函数内部用 `ed_image < ed_video` 比较出现位置，各配各的指针 `image_index`/`video_index`，与传入张量的行序无关、与出现顺序有关。

**练习 2**：一张 `grid_thw=[1,64,64]` 的图占多少个输入 token？它的位置 id 最多推进到多少（相对段起点）？
**答案**：占 \(1 \times 32 \times 32 = 1024\) 个 token；但三行坐标的最大值只有 \(32-1=31\)（h、w 行的最大行/列号），t 行恒为段内偏移。这就是"视觉块只把位置计数推进 max(t,h,w)"的源码依据，也是 u3-l4 中 YaRN factor 只取 2~3 的根因。

**练习 3**：为什么 `mrope_position_deltas` 通常是负数？
**答案**：它等于"最大位置 id + 1 − 序列长度"。视觉 token 数是 \(t \cdot h \cdot w\)（merge 后），而位置推进只有 \(\max(t,h,w)\)，后者远小于前者，因此位置 id 的增长落后于序列长度，差值为负。

### 4.3 模块三：get_rope_index_3 的时间戳设计与 t_index 恒为 0

#### 4.3.1 概念说明

`get_rope_index_3` 是 Qwen3-VL 专属版本，函数 docstring 第一句就点明差异：[rope2d.py:L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L14) "Different from the original implementation, Qwen3VL use timestamps rather than absolute time position ids."（Qwen3-VL 用时间戳取代绝对时间位置 id）。

设计动机承接 u2-l4 的结论：Qwen2.5-VL 把时间编码进 t 行位置 id（T-RoPE），位置 id 是与真实秒数挂钩的"绝对时间轴"；Qwen3-VL 则把时间信息从位置 id 中**完全剥离**，改为在文本流里显式写入时间戳 token，视觉块内部 t 行恒为 0。这带来两个好处：位置 id 不再随视频时长膨胀（利于长视频与长上下文外推），时间语义从"隐式坐标"变成"模型可直接读到的文本"（利于秒级事件定位）。

#### 4.3.2 核心流程

```
入口预处理（get_rope_index_3 独有）：
  若 video_grid_thw 不为 None：
      video_grid_thw = repeat_interleave(video_grid_thw, t 列, dim=0)
      # 一行 [4, 64, 64] 被复制成 4 行 [4, 64, 64]
      video_grid_thw[:, 0] = 1
      # 4 行全部改成 [1, 64, 64] —— 每帧成为独立的单帧视觉块
之后与模块二骨架完全相同，唯一区别在视觉段 t 行：
  llm_grid_t 恒为 1 → t_index = arange(1) 展开 = 全 0
```

为什么入口要拆分？源码注释 [rope2d.py:L15](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L15) 给出了 Qwen3-VL 的输入版式：

```
<t1> <vision_start> <frame1 的 1024 个视觉 token> <vision_end>
<t2> <vision_start> <frame2 的 1024 个视觉 token> <vision_end> ...
```

每帧都被时间戳文本隔开、各自是一个带 `<|vision_start|>` 的独立视觉块。探测步骤按 `<|vision_start|>` 计数会得到"每帧一段"，所以 `video_grid_thw` 必须同步拆成每帧一行、t 置 1，指针才能一一对齐。

#### 4.3.3 源码精读

[rope2d.py:L16-L18](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L16-L18)：入口的两行拆分。`torch.repeat_interleave(video_grid_thw, video_grid_thw[:, 0], dim=0)` 按 t 列的值复制行（[4,64,64] → 4 行 [4,64,64]），随后 `video_grid_thw[:, 0] = 1` 把 t 维归一。拆分只针对视频，图像天然 t=1 不需要处理。

[rope2d.py:L86-L90](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L86-L90)：视觉段三行的生成，注释写得很直白——"t_index is always 0 because llm_grid_t is always 1 (we use timestamps to encode the temporal information for videos)"。`torch.arange(llm_grid_t)` 在 `llm_grid_t=1` 时只产出 0，展开后整行全 0；h/w 两行与模块二完全一致。时间信息不在坐标里，而在帧与帧之间那些 `<t1>`、`<t2>` 文本 token 里——它们走普通文本段路径，三行同走一维递增坐标，与前后文自然衔接。

调用侧的参数组装在 [data_processor.py:L404-L424](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L404-L424)：从 `preprocess_qwen_visual` 的产物里取出 `image_grid_thw`/`video_grid_thw`，为每个视频构造 `second_per_grid_ts = temporal_patch_size / fps`（这个参数只有 Qwen2.5-VL 路径真正消费，传给 `_3` 也会被忽略），再调 `self.get_rope_index(...)` 算出 `position_ids` 存进样本字典——这正是 u5-l3 留白处的完整现场。函数选择逻辑在 [data_processor.py:L259-L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L259-L267)：`model_type` 为 `qwen3vl`/`qwen2.5vl`/`qwen2vl` 时分别绑定 `_3`/`_25`/`_2`，其余值直接抛 `ValueError`。`model_type` 由 train_qwen.py 按模型路径嗅探写入（见 u5-l1），所以**选错模型路径名会让训练用错一代位置编码**。

#### 4.3.4 代码实践

1. **实践目标**：观察"视频帧未按时间戳分段"时 `get_rope_index_3` 的行为，理解入口拆分与输入版式的隐式契约。
2. **操作步骤**：用模块二实践里同一条"4 帧连续占位符"的序列，改调 `get_rope_index_3`（示例代码）：

   ```python
   pos3, delta3 = get_rope_index_3(
       2, input_ids, video_grid_thw=torch.tensor([[3, 4, 4]]))
   ```

   对照再构造"正确分段"的序列：`[T, VS] + [VID]*4 + [T, VS] + [VID]*4 + [T, VS] + [VID]*4 + [T]`（每帧一个独立视觉块，中间的 T 模拟时间戳 token），传同样的 `video_grid_thw=[[3,4,4]]`。
3. **需要观察的现象**：未分段时，探测只发现 1 个视频块，只消费拆分后的第 1 行 `[1,4,4]`，`st` 跳过 4 个 token 后剩下的 8 个视频占位符被当成"尾部文本段"按一维编号——位置 id 语义错乱但不报错；分段后每个视觉块各自消费一行，t 行全 0，时间戳 T token 处三行为一维递增。
4. **预期结果**：`get_rope_index_3` 要求输入序列与"每帧独立视觉块"的版式配套；训练时这份版式由 processor 渲染 chat template 天然保证，手工调用时必须自己构造。
5. **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Qwen3-VL 中一段 8 帧视频（每帧 `grid=[1,64,64]`）的视觉 token，t 行坐标是什么？帧间时间信息在哪？
**答案**：每帧块的 t 行全 0（段内偏移之下），h/w 行各自 0..31 展开；时间信息不在任何位置 id 里，而在帧与帧之间的 `<t1>…<t8>` 时间戳文本 token 中，由文本段的一维坐标自然编码进上下文。

**练习 2**：`second_per_grid_ts` 传给 `get_rope_index_3` 会生效吗？
**答案**：不会。`_3` 的签名里保留了这个参数（[rope2d.py:L10](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L10)）但函数体从不读取它——时间已经不进位置 id，这个参数只为与 `_25` 保持接口一致而存在。调用侧 [data_processor.py:L408-L411](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L408-L411) 无论哪代都照算不误，是否消费由函数决定。

### 4.4 模块四：三代实现差异对比与 Interleaved-MRoPE

#### 4.4.1 概念说明

把三个函数并排放好，**全部差异收敛于一处：视频视觉段的 t 行公式**。图像路径三代逐字相同（`_25` 里 `second_per_grid_t` 只在视频分支被赋值、图像分支置 0）。同时要分清两个容易混淆的名词：

- **MRoPE 坐标**（rope2d.py 的职责）：每个 token 的 (t,h,w) 三个整数坐标。
- **Interleaved-MRoPE**（模型内部 RoPE 模块的职责）：如何把注意力头的旋转维度在 t/h/w 三个坐标之间分配频率。传统 MRoPE 按 `mrope_section`（如 16/24/24）把维度切成连续三段，t 轴只分到其中一段、拿不到全频段；Interleaved-MRoPE 把三轴频率交错铺满全频段，官方表述见 [README.md:L50](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L50) "Full‑frequency allocation over time, width, and height"。本讲的 rope2d.py 只管坐标，频率分配发生在 transformers 的模型代码里，由 config 的 `mrope_section` 与 `mrope_interleaved` 控制。

#### 4.4.2 核心流程：三代 t_index 公式

设视频在 LLM 侧网格为 `(t, h', w')`，帧号 \(i = 0, 1, \ldots, t-1\)，`second_per_grid_t = temporal_patch_size / fps`（默认 2/2 = 1.0）：

\[ \text{Qwen2-VL:} \quad t\_index_i = i \]

\[ \text{Qwen2.5-VL:} \quad t\_index_i = i \times \text{second\_per\_grid\_t} \times 2 \]

\[ \text{Qwen3-VL:} \quad \text{入口将网格拆为 } t \text{ 个单帧块，每块 } t\_index \equiv 0 \text{（时间改由文本时间戳携带）} \]

Qwen2.5-VL 的 ×2 正是 u2-l3 讲过的时间维 2× 压缩（FRAME_FACTOR=2，每 2 帧合 1 个 temporal patch），`second_per_grid_t` 则把"patch 步数"换算成"真实秒数"——默认参数下步进为 2。它的 docstring 用 `fps=1, tokens_per_second=25, temporal_patch_size=2` 的例子算出 interval=50。

#### 4.4.3 源码精读

**Qwen2-VL 的 t 行**——[rope2d.py:L460-L465](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L460-L465)：`torch.arange(llm_grid_t).view(-1, 1).expand(-1, llm_grid_h * llm_grid_w).flatten()`，帧号原样进坐标，一帧的所有 token 共享同一个 t 值。位置 id 与"第几帧"一一对应，但与真实时间无关——1 fps 采样和 4 fps 采样的同一视频会得到相同的 t 行。

**Qwen2.5-VL 的 t 行（T-RoPE）**——[rope2d.py:L244-L247](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L244-L247)：进入视频分支时读取 `second_per_grid_ts[video_index]`（未传则默认 1.0），图像分支显式置 0；[rope2d.py:L265-L271](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L265-L271)：`time_tensor = expanded_range * second_per_grid_t * 2` 后取整——帧号先乘真实秒数再乘 2，位置 id 变成与视频时长成比例的"绝对时间轴"，采样率不同的同一视频时间坐标一致。docstring 的完整算例在 [rope2d.py:L149-L163](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L149-L163)：3 个 temporal patch、fps=1、25 tokens/s 时 t 行为 `[0,0,0,0, 50,50,50,50, 100,100,100,100]`。

**Qwen3-VL 的时间戳方案**——[rope2d.py:L86-L87](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L86-L87)：t 行恒 0（模块三已精读）。官方动机见 [README.md:L54](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L54)："Text–Timestamp Alignment: Moves beyond T‑RoPE to precise, timestamp‑grounded event localization"——超越 T-RoPE，用精确的时间戳定位事件。配套的数据侧机制就是 u2-l4 精读的 `return_video_metadata=True`：`fps`/`frames_indices`/`total_num_frames` 让调用方有能力算出每帧的秒级时间戳并写进提示文本。

**位置 id 增长与 YaRN factor 的因果链**——[README.md:L792-L795](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L792-L795)：官方说明因为 Interleaved-MRoPE 的位置 id 增长慢于 vanilla RoPE，256K 扩 1M 上下文时 factor 取 2 或 3 而非 4。本模块的源码刚好给出证明：一条含 \(N_v\) 个视觉 token 的序列，文本段贡献约 \(N_{text}\) 的位置推进，而每个视觉块只推进 \(\max(t,h,w)\)；Qwen3-VL 里 t 行归零后每个视觉块更是只推进 \(\max(h',w')\)，位置 id 的最大值远小于序列长度（模块二练习 3 的负 delta 就是量化证据）。

**三代差异速查表**：

| 维度 | get_rope_index_2 (Qwen2-VL) | get_rope_index_25 (Qwen2.5-VL) | get_rope_index_3 (Qwen3-VL) |
|---|---|---|---|
| 定义位置 | [L336-L526](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L336-L526) | [L125-L333](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L125-L333) | [L5-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L5-L122) |
| 视频视觉段 t 行 | 帧序号 \(i\) | \(i \times\) second\_per\_grid\_t \(\times 2\)（绝对时间） | 恒为 0 |
| 时间信息载体 | 位置 id（帧序号，与真实时间脱钩） | 位置 id（T-RoPE 绝对时间轴） | 文本流中的时间戳 token |
| 入口特殊处理 | 无 | 无 | `video_grid_thw` 按 t 拆分、t 置 1（[L16-L18](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L16-L18)） |
| `second_per_grid_ts` | 不消费 | 消费（视频分支） | 不消费（仅保留签名） |
| 输入版式要求 | 视觉块整体展开 | 视觉块整体展开 | 每帧独立视觉块、时间戳穿插 |
| 纯文本/图像路径 | 三代逐字相同 | 三代逐字相同 | 三代逐字相同 |

#### 4.4.4 代码实践

1. **实践目标**：用同一段视频网格，直观看到三代 t 行的三种取值。
2. **操作步骤**：运行以下示例代码（对比脚本的核心）：

   ```python
   grids = torch.tensor([[4, 4, 4]])          # 4 个 temporal patch × 2×2 llm 网格
   seq = [T, T] + [VS] + [VID] * (4*2*2) + [T]   # v2/v2.5 版式：整段展开
   ids = torch.tensor([seq])
   p2, _  = get_rope_index_2(2, ids, video_grid_thw=grids)
   p25, _ = get_rope_index_25(2, ids, video_grid_thw=grids,
                              second_per_grid_ts=[1.0])
   # v3 版式：每帧独立视觉块，中间穿插模拟时间戳的文本 token
   seq3 = [T, VS] + [VID]*4 + [T, VS] + [VID]*4 + [T, VS] + [VID]*4 + [T, VS] + [VID]*4 + [T]
   ids3 = torch.tensor([seq3])
   p3, _ = get_rope_index_3(2, ids3, video_grid_thw=grids)
   for tag, p in [("v2", p2), ("v2.5", p25), ("v3", p3)]:
       print(tag, "t 行 =", p[0, 0][0].tolist())
   ```

3. **需要观察的现象**：`v2` 的视觉段 t 行为 `[0]*4+[1]*4+[2]*4+[3]*4` 加段偏移；`v2.5` 为 `[0]*4+[2]*4+[4]*4+[6]*4` 加段偏移（步进 ×2）；`v3` 每个单帧块内 t 行全 0 加各块段偏移，且相邻块之间被时间戳文本 token 的递增坐标隔开。
4. **预期结果**：帧序号 → 绝对时间 → 恒 0 三种形态并列呈现；若给 `v2.5` 换 `second_per_grid_ts=[0.5]`（等价 fps=4），t 行步进变为 1——证明 T-RoPE 坐标与采样率解耦。
5. **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：同样 8 个 temporal patch 的视频，`fps=2` 与 `fps=4` 两种采样下，Qwen2-VL 与 Qwen2.5-VL 的 t 行有何不同？
**答案**：Qwen2-VL 两者 t 行都是 0..7（帧序号，与 fps 无关）；Qwen2.5-VL 的 `second_per_grid_t = 2/fps`，fps=2 时步进 2（0,2,…,14），fps=4 时步进 1（0,1,…,7）——t 行正比于真实秒数，采样率越高坐标越密但时间轴一致。

**练习 2**：为什么说"Qwen3-VL 的 t_index 恒为 0"反而增强了时间能力？
**答案**：位置 id 里省下的时间维度信息没有丢，而是以时间戳文本的形式显式进入上下文——模型像读普通文字一样读到"这是第 3.5 秒的帧"，可以直接在生成时引用秒数实现事件定位；同时位置 id 不再随视频时长线性膨胀，长视频序列的位置外推压力更小（与 YaRN factor 2~3 的结论互为表里）。

**练习 3**：Interleaved-MRoPE 的"interleaved"改的是 rope2d.py 算出的坐标吗？
**答案**：不是。rope2d.py 仍输出 (t,h,w) 三个整数坐标；interleaved 指模型内部把旋转维度的频率在三个坐标轴间交错分配（对照 `mrope_section` 的连续切段分配），由 transformers 模型代码与 config 的 `mrope_interleaved` 实现，本仓库只提供坐标。

## 5. 综合实践

把本讲四个模块串成一个对照实验（即本讲规格指定的实践任务）：

**任务**：构造一条含 1 张图（`grid_thw=[1,64,64]`）和若干文本 token 的 `input_ids`，分别调用三个 `get_rope_index_*` 打印 3×L 的 `position_ids`；标出视觉段 t/h/w 三行取值，并回答"Qwen3-VL 中 t_index 恒为 0 的原因"。

**步骤**（以下为示例代码，需 torch 环境，`sys.path` 指向本仓库的 `qwen-vl-finetune`）：

```python
import sys, torch
sys.path.insert(0, "path/to/Qwen3-VL/qwen-vl-finetune")
from qwenvl.data.rope2d import get_rope_index_2, get_rope_index_25, get_rope_index_3

T, VS, IMG = 100, 151652, 151655
# grid [1,64,64] → llm 网格 1×32×32 → 1024 个视觉 token
seq = [T]*5 + [VS] + [IMG]*1024 + [T]*5
input_ids = torch.tensor([seq])
image_grid = torch.tensor([[1, 64, 64]])

for name, fn in [("v2", get_rope_index_2),
                 ("v2.5", get_rope_index_25),
                 ("v3", get_rope_index_3)]:
    pos, delta = fn(2, input_ids, image_grid_thw=image_grid)
    p = pos[0, 0]                      # (3, L)
    print(f"== {name} ==  L={p.shape[1]}  delta={delta.item()}")
    print("头部文本段(0:6) 三行:", p[:, :6].tolist())
    print("视觉段前 4 列  t/h/w:", p[0, 6:10].tolist(),
          p[1, 6:10].tolist(), p[2, 6:10].tolist())
    print("h 行第 32 列跳变处:", p[1, 6+30:6+34].tolist())
    print("尾部文本段:", p[:, -5:].tolist(), " 全局最大位置 id:", p.max().item())
```

**需要逐项核对的观察点**（按源码手推，**待本地验证**）：

1. 头部 6 个 token（5 个文本 + VS）属同一段文本段，三行均为 `[0,1,2,3,4,5]`。
2. 视觉段从偏移 6 开始：t 行 `[6,6,6,6,...]` 全 6；h 行从 6 起、每 32 个 token 加 1（`[6]*32, [7]*32, …, [37]*32`）；w 行从 6 起、以 32 为周期循环（`6,7,…,37, 6,7,…`）。
3. 尾部 5 个文本 token 三行均为 `[38,39,40,41,42]`（= 视觉段最大值 37 + 1 起）。
4. 三个函数输出**完全一致**——图像路径三代同构，差异只在视频。
5. `L = 5+1+1024+5 = 1035`，而最大位置 id 仅 42，`delta = 43 − 1035 = −992`：1024 个视觉 token 只把位置推进了 32。
6. 书面作答：Qwen3-VL 中 `t_index` 恒为 0，是因为时间信息不再编码进位置 id（`llm_grid_t` 被入口拆分归一为 1，`arange(1)=0`），改由帧间的时间戳文本 token 携带——模型直接"读到"时间，而位置轴彻底卸下时间负担。

**进阶**（可选）：把 `image_grid` 换成 `video_grid_thw=[[4,64,64]]`，按模块四 4.4.4 的两种版式分别构造序列，观察三代 t 行的三种形态。

## 6. 本讲小结

- `position_ids` 形状 `(3, B, L)`：三行分别是 t/h/w 坐标；纯文本段没有空间结构，三行退化为同一个一维序列，MRoPE 自动退化为普通 RoPE。
- 主循环骨架三代共享：用 `<|vision_start|>`(151652) 探测视觉块、按占位符首次出现位置交替切段；段间衔接规则是"新段起点 = 上一段三行最大值 + 1"；h 行每 `llm_w` 个 token 加 1、w 行以 `llm_w` 为周期循环。
- 视觉 token 数是 \(t \cdot \frac{h}{2} \cdot \frac{w}{2}\)，但位置 id 只推进 \(\max(t,h,w)\)，因此 `mrope_position_deltas` 通常是大负数——这也是 README 说 Interleaved-MRoPE 位置 id 增长慢于 vanilla RoPE、YaRN factor 取 2~3 的源码级依据。
- 三代差异全部收敛于视频段 t 行：Qwen2-VL 用帧序号（与真实时间脱钩）→ Qwen2.5-VL 用 T-RoPE 绝对时间（帧号 × `temporal_patch_size/fps` × 2）→ Qwen3-VL 恒 0，时间改由文本时间戳携带，且入口把视频网格按帧拆分以匹配"每帧独立视觉块"的输入版式。
- Interleaved-MRoPE 的"交错"发生在模型内部的旋转维度频率分配（config `mrope_section`/`mrope_interleaved`），rope2d.py 只负责产出 (t,h,w) 坐标，两者是"坐标"与"频率分配"的分工。
- 调用契约：`data_processor.py` 按 `model_type` 选函数（选错模型路径名 = 用错一代位置编码）；手工调 `get_rope_index_3` 时序列必须已按时间戳分段，否则拆分后的网格行与视觉块对不上、位置 id 静默错乱。

## 7. 下一步学习建议

- **下一讲 u5-l5**：position_ids 由本讲算出后，正是交给两种 DataCollator 与 batch 内其他样本拼合/填充的——去看 `DataCollatorForSupervisedDataset` 如何在 batch 维堆叠本讲的 `(3, 1, L)`，以及 `FlattenedDataCollatorForSupervisedDataset` 拼接多条序列时如何重新衔接三行坐标。
- **回看 transformers 侧实现**：本仓库的 rope2d.py 是训练侧复刻；推理与模型内部版本在 transformers 的 `modeling_qwen2_5_vl.py`/`modeling_qwen3_vl.py` 的 `get_rope_index` 与 RoPE 模块中，可对照 `mrope_section`、`mrope_interleaved` 字段理解频率分配。
- **串联时间戳全链路**：把 u2-l4 的 `video_metadata`（fps/frames_indices/total_num_frames）→ 时间戳文本 → 本讲 t 行归零连成一条线，就完整理解了 Qwen3-VL"文本-时间戳对齐"从数据到位置编码的落地路径。
