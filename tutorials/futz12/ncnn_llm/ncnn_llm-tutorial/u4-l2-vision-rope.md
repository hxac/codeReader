# 视觉位置编码 mRoPE 与 Hunyuan xdrope

## 1. 本讲目标

本讲是 RoPE 系列的第二讲，承接 u4-l1（纯文本单轴 RoPE），把视角转向**视觉语言模型（VLM / OCR）**里特有的**多轴位置编码**。读完本讲你应该能够：

1. 说清楚为什么图像不能直接套用纯文本的单轴位置编码，必须拆成「时间 / 高度 / 宽度」多个轴。
2. 看懂 Qwen 系 VLM 的 **mRoPE**：标准（连续分段）版本与 **interleaved（交错）** 版本的区别，以及 `mrope_section` 如何把维度切分给 t/h/w 三个轴。
3. 看懂 HunyuanOCR 的 **4 轴 xdrope**：`xdrope_section` 如何把维度切分给 linear/w/h/t 四个轴，以及 NTK-alpha 基如何扩展上下文。
4. 对一段「文本 + 图像」的混合 token 序列，画出每个 token 在各轴上使用的 position_id 分配示意。

本讲只讲**位置编码本身**（生成 cos/sin cache 的过程）；图像 patch 如何切分、视觉特征如何提取并注入 token 序列，分别在 u5-l2、u5-l3 讲解。

## 2. 前置知识

### 2.1 复习单轴 RoPE

u4-l1 已建立：RoPE 把每对相邻维度当作一个二维向量做旋转，旋转角

\[
\theta_{j}(\text{pos}) = \text{pos}\cdot \text{inv\_freq}[j],\qquad
\text{inv\_freq}[j] = \frac{1}{\theta_{\text{base}}^{\,2j/d}}
\]

其中 \(d\) 是 head_dim，pos 是该 token 的位置。预算一张 \((d/2,\;\text{seqlen})\) 的 `cos_cache` / `sin_cache`，推理时查表即可。关键性质：两个 token 的注意力只依赖它们的**相对位置** \(\Delta\text{pos}\)。

### 2.2 为什么纯文本的单轴位置对图像不够用

一段纯文本是一维序列，position_id 从 0 递增即可。但一张图像被切成 patch 网格后，本质是**二维**结构：

- 网格里 (行0,列3) 与 (行0,列5) 是「同一行、相邻两列」；
- 而 (行0,列3) 与 (行3,列3) 是「同一列、相邻两行」。

如果把 patch 按行优先展平成一维序列、用同一个递增 position_id，模型就只能看到「序列里的距离」，**完全分不清「水平相邻」和「垂直相邻」**。两个在原图里上下相邻的 patch，可能因为展平顺序而得到很大的序列距离，注意力就被错误压制。

多轴位置编码的解决思路：**把 head_dim 的维度分成几组，不同组分别携带不同轴（时间 t / 高度 h / 宽度 w）的位置**。这样每个轴组内部仍然满足「相对位置」性质，注意力就能同时感知「这两个 patch 同行」「那两个 patch 同列」这种二维几何关系。

### 2.3 多模态 token 序列的结构

一次图文推理的 token 序列大致长这样（细节见 u5-l3 的 `inject_image_embeds`）：

```
[ 文本token ... 文本token ] [ 图像token × image_embeds_size ] [ 文本token ... ]
       (图像之前)                   (image_pad 占位展开)             (图像之后)
```

其中 `image_pad_index` 是**第一个图像 token 在展平序列里的下标**，`image_embeds_size` 是图像 token 的总个数。视觉位置编码函数要回答的核心问题就是：**这三种区段里的每个 token，在每个轴组上各用哪个 position_id？**

### 2.4 spatial_merge_size（patch 合并）

Qwen-VL 系会把相邻 \(2\times2\) 个 patch 合并成一个 token（`spatial_merge_size=2`）。所以「合并后的网格」列数是 `num_patches_w / spatial_merge_size`，记作 `grid_w`；行数记作 `grid_h`。后续 mRoPE 计算高度/宽度下标时，用的都是**合并后**的网格坐标。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/utils/rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) | 声明三个解码器侧的视觉 RoPE 生成函数（标准 mRoPE、交错 mRoPE、Hunyuan xdrope），以及 `inject_image_embeds`。 |
| [src/utils/rope_embed.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp) | 上述函数的实现。本讲的主战场。 |
| [src/utils/vision_rope.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/vision_rope.h) | 声明 `generate_vision_rope_cache_2d`——这是**视觉编码器（ViT）内部**用的 2D RoPE，与本讲的解码器侧 mRoPE/xdrope 是两回事，放在这里做对比。 |
| src/ncnn_llm_gpt.cpp | VLM 的 prefill 在此处选择调用哪个视觉 RoPE 函数。 |
| src/ncnn_llm_ocr.cpp | GLM-OCR（标准 mRoPE）与 HunyuanOCR（xdrope）的调用点，以及 pos4 四轴的构造。 |

> 注意区分两条线：`rope_embed.*` 里的函数服务**文本解码器**（决定 LLM 对图文 token 的注意力位置）；`vision_rope.*` 里的 `generate_vision_rope_cache_2d` 服务**视觉编码器自身**（ViT 内部对 patch 的注意力位置）。本讲以前者为主，后者在 4.4 末尾做简要对比。

---

## 4. 核心概念与源码讲解

### 4.1 多轴位置编码的动机与轴配置数据结构

#### 4.1.1 概念说明

无论是 mRoPE（3 轴 t/h/w）还是 xdrope（4 轴 linear/w/h/t），核心思想一致：

> **把 head_dim 的一半维度（即 \(d/2\) 个旋转对）按「轴配置」切成若干段，每段绑定一个位置轴；第 j 个旋转对使用的 position_id，由它所属的轴决定。**

这里有两个关键数据结构：

- `mrope_section`：长度为 3 的数组 \([s_t, s_h, s_w]\)，表示前 \(s_t\) 对给时间轴、接着 \(s_h\) 对给高度轴、最后 \(s_w\) 对给宽度轴，三者之和必须等于 \(d/2\)。
- `xdrope_section`：长度为 4 的数组 \([s_0, s_1, s_2, s_3]\)，分别对应 linear/w/h/t 四个轴，之和也必须等于 \(d/2\)。

之所以要等于 \(d/2\)，是因为 head_dim \(d\) 一共有 \(d/2\) 个旋转对，每个对都必须被分配给某一个轴，不能多也不能少。

#### 4.1.2 核心流程

轴配置的读取与校验发生在各运行时的构造函数里：

1. 从 `model.json` 的 `setting.rope` 块读出 `mrope_section`（或 `xdrope_section`）数组。
2. 校验长度（mRoPE 必须 3 个、xdrope 必须 4 个）与求和（必须等于 `rope_head_dim/2`）。
3. 失败则抛异常，被外层 catch 转成 `load model failed`（与 u1-l5 一致的尽早失败策略）。
4. 成员变量保存该数组，供 prefill/generate 时传给 RoPE 生成函数。

#### 4.1.3 源码精读

HunyuanOCR 读取并校验 `xdrope_section`：

[src/utils/rope_embed.cpp 之外，配置读取在 src/ncnn_llm_ocr.cpp:120-127](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L120-L127) —— 从 `rope_cfg["xdrope_section"]` 逐项取 int，累加求和，要求等于 `head_dim_/2`，否则抛 `"setting.rope.xdrope_section must sum to rope_head_dim/2"`。

GLM-OCR 读取并校验 `mrope_section`：

[src/ncnn_llm_ocr.cpp:172-185](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L172-L185) —— 从 `text_rope_cfg["mrope_section"]` 取值，并要求 `mrope_section_.size() == 3`，否则抛 `"setting.rope.mrope_section must have 3 entries"`。

VLM（ncnn_llm_gpt）侧则在 `setting.rope.type == "mRoPE"` 时读 `mrope_section`：

[src/ncnn_llm_gpt.cpp:236-239](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L236-L239) —— 注意这里靠 `type` 字段大小写敏感地分支（u4-l1 已强调过这点），匹配上 `"mRoPE"` 才走多轴路径。

三个生成函数的声明在头文件里集中给出，签名差异已经暗示了它们的分工：

[src/utils/rope_embed.h:51-85](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L51-L85) —— 标准 mRoPE 多了 `spatial_merge_size` 和 `mrope_section` 参数；交错版把这些硬编码（见 4.3）；xdrope 用 `pos4`（四轴 position 数组）+ `xdrope_section` + `alpha`。

#### 4.1.4 代码实践

**实践目标**：熟悉两个轴配置数组在构造期的来源与约束。

**操作步骤**：

1. 打开 `src/ncnn_llm_ocr.cpp:120-127`，记录 `xdrope_section` 的求和约束公式。
2. 打开 `src/ncnn_llm_ocr.cpp:172-185`，记录 `mrope_section` 的长度约束。
3. 假设某模型 `rope_head_dim = 128`，自己写两组**合法**的配置：一组 `mrope_section`（3 项，和为 64）、一组 `xdrope_section`（4 项，和为 64），再写两组**非法**配置（和不等于 64 / 长度不对）。

**需要观察的现象**：对照源码里的 `throw` 分支，确认非法配置会触发哪条报错。

**预期结果**：和不为 64 的 `xdrope_section` 触发 `"must sum to rope_head_dim/2"`；长度非 3 的 `mrope_section` 触发 `"must have 3 entries"`。这些是运行期才会触发的校验，**待本地验证**具体报错文案。

#### 4.1.5 小练习与答案

**练习 1**：为什么轴配置各段之和必须严格等于 `rope_head_dim/2`，多了少了都不行？

**答案**：`rope_head_dim/2` 是旋转对的总数，每个旋转对都必须明确归属某个轴才能算出 cos/sin。多出说明配置与 head_dim 不匹配（模型导出错了），少出则有旋转对未被赋值（位置信息缺失），两者都会导致位置编码错误，所以构造期直接抛异常。

**练习 2**：`mrope_section` 的三个元素分别对应 t/h/w，它们的**相对大小**会影响什么？

**答案**：影响「模型把多少注意力分辨率分给每个轴」。某轴分到的维度对越多，该轴能表达的相对位置频率越丰富、越精细。例如给高度、宽度都分较多维度，模型对图像内部二维几何的感知就更强。

---

### 4.2 标准 mRoPE：generate_rope_embed_cache_vision_mrope

#### 4.2.1 概念说明

这是 Qwen2-VL 风格的 mRoPE。它把 \(d/2\) 个旋转对**连续**切成三段：\([0, s_t)\) 给时间轴、\([s_t, s_t+s_h)\) 给高度轴、\([s_t+s_h, d/2)\) 给宽度轴。每个 token 在每段里使用的 position_id 由它处于序列的哪个区段决定：

- **图像之前的文本**：三个轴都用同一个递增的线性位置（行为退化成普通 1D RoPE）。
- **图像 token**：时间轴恒定（一整张图视为同一时刻）、高度轴用「行号」、宽度轴用「列号」。
- **图像之后的文本**：线性位置接着图像的宽度跨度继续递增。

#### 4.2.2 核心流程

设 `grid_w = num_patches_w / spatial_merge_size`（合并后网格列数），`grid_h` 为行数。对序列里第 \(i\) 个 token、第 \(j\) 个旋转对：

1. 先按标准公式算 \(\text{inv\_freq}[j] = 1/\theta_{\text{base}}^{2j/d}\)（与 u4-l1 完全相同）。
2. 判断 \(i\) 落在哪个区段，算出本 token 在当前轴上的 `pos`：

\[
\text{pos} =
\begin{cases}
\text{position\_id} + i, & i < \text{image\_pad\_index} \quad(\text{图像前文本，三轴同值})\\
\text{position\_id} + \text{image\_pad\_index}, & \text{图像 token, 落在时间轴段}\\
\text{position\_id} + \text{image\_pad\_index} + \text{row}(i), & \text{图像 token, 落在高度轴段}\\
\text{position\_id} + \text{image\_pad\_index} + \text{col}(i), & \text{图像 token, 落在宽度轴段}\\
\text{position\_id} + i - \text{image\_embeds\_size} + \text{grid\_w}, & i \ge \text{image\_pad\_index} + \text{image\_embeds\_size} \quad(\text{图像后文本})
\end{cases}
\]

其中图像 token 的行、列坐标（在合并后网格里，行优先排列）：

\[
\text{row}(i) = \left\lfloor\frac{i - \text{image\_pad\_index}}{\text{grid\_w}}\right\rfloor,\qquad
\text{col}(i) = (i - \text{image\_pad\_index}) \bmod \text{grid\_w}
\]

3. \(\cos_{ij} = \cos(\text{pos}\cdot\text{inv\_freq}[j])\)，\(\sin_{ij}\) 同理，写入 cache 的第 \(i\) 行第 \(j\) 列。

> 「图像后文本」位置之所以是 `+ grid_w`：图像 token 在宽度轴上的最大位置是 `image_pad_index + grid_w - 1`，紧随其后的文本若要与之连续，就应从 `image_pad_index + grid_w` 开始。把线性下标里的 `image_embeds_size` 个图像 token 扣掉、再补上 `grid_w`，正好衔接。

#### 4.2.3 源码精读

完整实现：

[src/utils/rope_embed.cpp:337-405](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L337-L405) —— 标准 mRoPE 主体。

关键的三段判断与轴分配：

[src/utils/rope_embed.cpp:379-395](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L379-L395) —— `j < mrope_section[0]` 走时间轴（pos 直接用 `image_pad_index`，恒定）；`j < mrope_section[0]+mrope_section[1]` 走高度轴（`hid`）；否则走宽度轴（`wid`）。`hid` 用整除、`wid` 用取模，正是行优先网格里的行列坐标。

三段区段的入口判断：

[src/utils/rope_embed.cpp:368-375](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L368-L375) —— 图像前用 `pos += i`，图像后用 `pos += i - image_embeds_size + (num_patches_w / spatial_merge_size)`，与上面公式一致。

调用点（GLM-OCR 文本解码器）：

[src/ncnn_llm_ocr.cpp:435-440](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L435-L440) —— `image_token_positions[0]` 即图像 token 起点下标，`num_vision_tokens` 即 `image_embeds_size`；并据此推出下一轮的 `next_position_id`。

调用点（VLM prefill 的非交错分支）：

[src/ncnn_llm_gpt.cpp:470-473](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L470-L473) —— 注意 prefill 末尾把 `position_id` 推进 `token_ids.size() - image_embeds_size + grid_w`，与「图像后文本」的位置公式同源，保证多轮对话里后续 token 的位置连续。

#### 4.2.4 代码实践

**实践目标**：手算一个小例子，验证你对三段区段与 t/h/w 分配的理解。

**操作步骤**：取一组迷你参数：

- `mrope_section = [2, 2, 2]`（即 `embed_dim/2 = 6`，`embed_dim = 12`）
- `position_id = 0`，`image_pad_index = 3`，`image_embeds_size = 4`
- `num_patches_w = 4`，`spatial_merge_size = 2` → `grid_w = 2`，`grid_h = 2`（4 个图像 token 正好排成 \(2\times2\) 合并网格）

注入图像后的序列共 8 个 token：下标 `0,1,2`（图像前文本）、`3,4,5,6`（图像）、`7`（图像后文本）。

请按 4.2.2 的公式，逐个 token 算出它在时间轴段、高度轴段、宽度轴段三组维度上各自的 `pos`，填成一张表。

**需要观察的现象**：图像 4 个 token 的高度轴 pos 是否只取两个值（行号）、宽度轴 pos 是否只取两个值（列号）、时间轴 pos 是否全部相同。

**预期结果**（参考答案见 4.2.5）：图像 token 的 (行,列) 依次为 (0,0)(0,1)(1,0)(1,1)；时间轴 pos 全为 3；图像后文本 token 7 的 pos 应为 5。

#### 4.2.5 小练习与答案

**练习 1**：在上述迷你例子中，图像 token 的宽度轴 pos 各是多少？

**答案**：`grid_w=2`，列号 `wid = (i-3) % 2`：i=3→0、i=4→1、i=5→0、i=6→1；加上锚点 `image_pad_index=3`，得 pos = 3,4,3,4。可见同一列的两个 patch（i=3 与 i=5）宽度 pos 相同，mRoPE 因此能识别「同列」。

**练习 2**：如果把 `spatial_merge_size` 改成 1（不合并），`grid_w` 会变成多少，对位置编码有什么影响？

**答案**：`grid_w = num_patches_w / 1 = 4`，网格变成 \(4\times?\) 更细。同一张图占用更多 token、每个 token 的行列坐标更精细，模型对图像内部几何的分辨率更高，但序列更长、计算量更大。

**练习 3**：为什么图像 token 的时间轴 pos 是常数，而不是随 i 递增？

**答案**：一张静态图像里所有 patch 处于「同一时刻」，没有时间先后，所以时间轴给同一个值；真正的空间结构由 h/w 两轴承担。这正是多轴编码相对单轴编码的核心收益。

---

### 4.3 交错 mRoPE：generate_rope_embed_cache_vision_mrope_interleaved

#### 4.3.1 概念说明

Qwen3.5-VL 用的是 mRoPE 的**交错（interleaved）** 变体。区别在于维度到轴的映射方式：

- 标准 mRoPE：三段**连续**排列（先全部时间、再全部高度、再全部宽度）。
- 交错 mRoPE：旋转对按 `(t, h, w)` 三元组**循环**排列，即每相邻 3 个对分别属于 t、h、w。

此外交错版还有两个特点：`mrope` 段宽 `{11,11,10}` 与合并大小 `2` 被**硬编码**进函数（专供 Qwen3.5-VL 的固定配置），且只旋转 embedding 的一半（`partial_rotary_factor = 0.5`），另一半 cos=1、sin=0（不旋转）。

#### 4.3.2 核心流程

对第 \(i\) 个 token、第 \(j\) 个旋转对（\(j < \text{embed\_dim}/2\)）：

1. 计算 `rotate_dim = embed_dim * 0.5`，仅当 \(j < \text{rotate\_dim}/2\) 时真正旋转；否则该对 cos=1、sin=0。
2. `inv_freq[j] = 1/\theta_{\text{base}}^{2j/\text{rotate\_dim}}`（注意分母是 `rotate_dim` 而非 `embed_dim`）。
3. 对真正旋转的对，按 `j % 3` 选轴：

\[
\text{axis}(j) =
\begin{cases}
\text{高度}, & j \bmod 3 = 1 \;\text{且}\; j < 11\times3\\
\text{宽度}, & j \bmod 3 = 2 \;\text{且}\; j < 10\times3\\
\text{时间}, & \text{否则}
\end{cases}
\]

4. 选定轴后，`pos` 的计算与 4.2 标准版**完全相同**（图像前线性、图像内按行/列、图像后接 `grid_w`，只是 `grid_w = num_patches_w / 2`）。

> 当 `rope_head_dim = 128` 时，`rotate_dim/2 = 32`，正好等于 `11+11+10`；于是真正旋转的 32 个对里有 11 个时间、11 个高度、10 个宽度，与硬编码的 `mrope={11,11,10}` 自洽。

#### 4.3.3 源码精读

完整实现与硬编码常量：

[src/utils/rope_embed.cpp:161-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L161-L245) —— 注意 `merge_size=2`、`mrope[3]={11,11,10}`、`partial_rotary_factor=0.5` 都是局部常量，函数签名里**没有** `mrope_section` / `spatial_merge_size` 参数。

按 `j % 3` 选轴：

[src/utils/rope_embed.cpp:207-229](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L207-L229) —— `which_pos=1` 表示高度（`hid`），`which_pos=2` 表示宽度（`wid`），否则时间。`hid`/`wid` 仍用 `num_patches_w / merge_size` 作除数/模数。

不旋转的另一半：

[src/utils/rope_embed.cpp:238-242](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L238-L242) —— `j >= rotate_dim/2` 时直接写 cos=1.0、sin=0.0，相当于对该维度对不做任何旋转。

调用点（VLM prefill 的 Qwen3.5-VL 分支）：

[src/ncnn_llm_gpt.cpp:468-469](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L468-L469) —— 当 `vision_type == VISION_QWEN3_5_VL` 时走交错版，其余 VLM 走标准版（4.2）。

#### 4.3.4 代码实践

**实践目标**：对比「连续分段」与「交错三元组」两种维度布局。

**操作步骤**：

1. 假设 `rotate_dim/2 = 12`（即把 {11,11,10} 等比缩小到 {4,4,4} 便于手算），列出两种布局下，第 \(j=0\ldots11\) 个旋转对分别属于哪个轴。
   - 标准（连续）版按 4.2 的 `[s_t,s_h,s_w]=[4,4,4]` 切分。
   - 交错版按 `j%3` 与上界规则切分。
2. 把两种结果并排画成一行「T/H/W」字母序列。

**需要观察的现象**：连续版呈 `TTTT HHHH WWWW` 三块；交错版呈 `T H W T H W …` 交替。

**预期结果**：交错版前 12 对为 `T,H,W,T,H,W,T,H,W,T,H,W`（4 个完整三元组）。两种布局对模型表达能力的影响取决于权重训练时的约定，本仓库两种都保留以适配不同模型族，**待本地验证**具体模型该用哪一种（由 `vision_type` 决定）。

#### 4.3.5 小练习与答案

**练习 1**：为什么交错版要把 `merge_size` 和 `mrope` 硬编码，而标准版把它们做成参数？

**答案**：交错版专门服务 Qwen3.5-VL 这一个模型族，其合并大小（2）与轴宽（11/11/10）是固定的，硬编码可省去配置、也避免传错；标准版要兼容 Qwen2-VL 等多个可配置模型，故做成参数从 `model.json` 读入。

**练习 2**：`partial_rotary_factor = 0.5` 意味着什么？

**答案**：只对 embedding 的一半维度施加旋转，另一半保持原样（cos=1,sin=0）。这是一种降低 RoPE 计算量、并让部分维度携带「与位置无关」信息的常见做法，是否使用取决于模型权重训练时的设计。

---

### 4.4 Hunyuan xdrope：4 轴与 NTK-alpha 基

#### 4.4.1 概念说明

HunyuanOCR 用的是更激进的 **xdrope**：轴数从 3 增到 **4**（linear/w/h/t），并且基频率不做朴素 RoPE，而是用 **NTK-alpha 基**来扩展上下文长度。

- **4 轴**：`pos4 = { pos_lin, pos_w, pos_h, pos_t }`，每个都是长度为 `seq_len` 的 position 数组。`xdrope_section` 把 \(d/2\) 个对切成 4 段，分别绑定这 4 个轴。
- **NTK-alpha 基**：把基频率从 \(\theta_{\text{base}}\) 放大为

\[
\theta_{\text{ntk}} = \theta_{\text{base}}\cdot \alpha^{\,d/(d-2)}
\]

再用它算 `inv_freq`。这与 u4-l1 讲过的 NTK 思路一致（高频不动、低频周期拉长），只是这里用单一 alpha 参数控制拉伸强度（HunyuanOCR 默认 `alpha=1000`）。

#### 4.4.2 核心流程

1. 算 NTK-alpha 基：\(\theta_{\text{ntk}} = \theta_{\text{base}}\cdot \alpha^{d/(d-2)}\)。
2. 算 `inv_freq[j] = 1/\theta_{\text{ntk}}^{2j/d}`，\(j = 0\ldots d/2-1\)。
3. 用 `xdrope_section` 的前缀和，把每个 \(j\) 映射到一个轴编号 `a`（详见下方源码），默认值是「最后一个轴」。
4. 对第 \(i\) 个 token、第 \(j\) 个对：取 `pos = pos4[a][i]`，\(\cos_{ij}=\cos(\text{pos}\cdot\text{inv\_freq}[j])\)，\(\sin\) 同理。

pos4 的构造（prefill 阶段）：默认四个轴都初始化为线性下标 \(i\)；然后**仅图像网格 token** 把宽度轴改成列号 `c`、高度轴改成行号 `r`、时间轴改成 0，linear 轴保持 \(i\)。生成阶段（decode）则四个轴都用同一个线性 `position_id`。

#### 4.4.3 源码精读

完整实现：

[src/utils/rope_embed.cpp:406-450](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L406-L450) —— Hunyuan xdrope 主体。

NTK-alpha 基计算：

[src/utils/rope_embed.cpp:418-426](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L418-L426) —— `ntk_theta = rope_theta * alpha^(rope_head_dim/(rope_head_dim-2))`，再据此生成 `inv_freq`。头文件注释里有同样公式：

[src/utils/rope_embed.h:73-77](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L73-L77) —— 注明 `pos4 = { linear, w, h, t }`、NTK-alpha 基公式与 `xdrope_section` 前缀映射规则。

维度到轴的前缀映射：

[src/utils/rope_embed.cpp:428-437](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L428-L437) —— 先把 `axis_of_dim` 全部初始化为「最后一个轴」，再按 `xdrope_section` 的宽度逐段填充轴编号。例如 `xdrope_section=[16,16,16,16]` 时，`j/16` 即得轴号；段宽不均时也能正确分配。这段代码兼容任意段宽，是「前缀划分」的通用实现。

取轴并算 cos/sin：

[src/utils/rope_embed.cpp:442-448](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L442-L448) —— `a = axis_of_dim[j]`，`pos = pos4[a][i]`。

pos4 在 prefill 中的构造（HunyuanOCR）：

[src/ncnn_llm_ocr.cpp:648-668](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L648-L668) —— 四个数组先全部初始化为线性 `i`；然后对图像网格 token（行优先，列数为 `patch_w+1`）把 `pos_w[idx]=c`、`pos_h[idx]=r`、`pos_t[idx]=0`；最后组装成 `pos4[4] = {pos_lin, pos_w, pos_h, pos_t}` 传给生成函数。

pos4 在 decode 中的构造：

[src/ncnn_llm_ocr.cpp:715-721](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L715-L721) —— 生成的文本 token 四个轴都用当前 `position_id`（`pos4 = {p1,p1,p1,p1}`），再 `position_id++`，与纯文本的逐 token 推进一致。

#### 4.4.4 代码实践

**实践目标**：理解「前缀划分」如何把维度映射到 4 个轴。

**操作步骤**：取 `xdrope_section = [2, 2, 2, 2]`（即 `rope_head_dim/2 = 8`，`rope_head_dim = 16`），手工模拟 [rope_embed.cpp:428-437](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L428-L437) 的 `axis_of_dim` 填充过程，列出 `j=0..7` 各自对应的轴编号（0=linear, 1=w, 2=h, 3=t）。

**需要观察的现象**：每连续 2 个维度归同一个轴，4 段正好覆盖 8 个维度。

**预期结果**：`axis_of_dim = [0,0,1,1,2,2,3,3]`，即 j∈{0,1}→linear、{2,3}→w、{4,5}→h、{6,7}→t。若把段宽改成 `[3,3,1,1]`，则应为 `[0,0,0,1,1,1,2,3]`，体现「段宽不必相等」。

#### 4.4.5 小练习与答案

**练习 1**：xdrope 比 mRoPE 多了一个 linear 轴，这个轴在 prefill 时对所有 token 都等于线性下标 \(i\)，它的作用是什么？

**答案**：linear 轴携带「序列整体顺序」信息，保证即便图像 token 的 w/h/t 被改成网格坐标，模型仍能通过 linear 段感知「这个 token 在整句话里的先后位置」。它相当于在多轴编码里保留了一条与传统 1D RoPE 等价的通道，对图文交错的顺序理解很重要。

**练习 2**：把 `alpha` 调大（如从 1000 调到 10000），NTK 基如何变化，对上下文长度有何影响？

**答案**：\(\theta_{\text{ntk}}=\theta_{\text{base}}\cdot\alpha^{d/(d-2)}\) 随 alpha 增大而增大，`inv_freq` 整体变小、低频维度的周期被拉得更长，模型能外推到更长的上下文而不崩溃；代价是短距离的相对位置区分度可能略降。这与 u4-l1 讲的 NTK 思路完全一致。

**练习 3**：为什么 decode 阶段四个轴都用同一个 `position_id`，而 prefill 阶段图像 token 要分别用 w/h/t？

**答案**：decode 生成的是纯文本 token，没有二维空间结构，四个轴退化为同一个线性位置即可（等价于普通 RoPE）；prefill 阶段图像 token 才有网格坐标，需要用 w/h 携带二维几何、t 表示「同处一帧」。区分 prefill 与 decode 的轴取值，正是 xdrope 兼顾「图像二维」与「文本一维」的关键。

---

> ### 补充：视觉编码器侧的 2D RoPE（generate_vision_rope_cache_2d）
>
> 本讲重点在「文本解码器」侧的 mRoPE/xdrope。但 [src/utils/vision_rope.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/vision_rope.h) 与 [src/utils/vision_rope.cpp:14-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/vision_rope.cpp#L14-L84) 还提供了一个服务 **ViT 自身** 的 `generate_vision_rope_cache_2d`：它取 `rope_section=[h_dim, w_dim]`（2 项），按 patch 网格的 (current_h, current_w) 直接给 h 段、w 段算 cos/sin，并可选 `duplicate_sections` 把结果复制一份。它与本讲的 mRoPE 互不调用、各管一段（一个管 ViT 内部注意力，一个管 LLM 对图文 token 的注意力），不要混淆。

---

## 5. 综合实践

**任务**：画出一整段「文本 + 图像 + 文本」序列在 mRoPE 下的轴分配示意，并把 xdrope 的轴分配也对照画一遍。

沿用 4.2.4 的迷你参数：`mrope_section=[2,2,2]`、`position_id=0`、`image_pad_index=3`、`image_embeds_size=4`、`num_patches_w=4`、`spatial_merge_size=2`（`grid_w=2`）。注入图像后序列为 8 个 token：`0,1,2`（文本前）、`3,4,5,6`（图像）、`7`（文本后）。

**第一步（mRoPE 标准版）**：按下表逐格填写每个 token 在三个轴段上的 `pos`：

| token i | 区段 | 时间轴段 pos (j∈[0,2)) | 高度轴段 pos (j∈[2,4)) | 宽度轴段 pos (j∈[4,6)) |
|---------|------|------------------------|------------------------|------------------------|
| 0 | 图像前 | 0 | 0 | 0 |
| 1 | 图像前 | 1 | 1 | 1 |
| 2 | 图像前 | 2 | 2 | 2 |
| 3 | 图像(0,0) | 3 | 3 | 3 |
| 4 | 图像(0,1) | 3 | 3 | 4 |
| 5 | 图像(1,0) | 3 | 4 | 3 |
| 6 | 图像(1,1) | 3 | 4 | 4 |
| 7 | 图像后 | 5 | 5 | 5 |

> 推导要点：图像前文本三轴同值 = `position_id+i`；图像 token 时间轴恒为 `0+3=3`，高度轴 = `3+行号`，宽度轴 = `3+列号`；图像后文本 = `0 + 7 - 4 + 2 = 5`（`grid_w=2`）。注意 token 7 在三轴上都是 5，与图像的最大宽度 pos（`3+(2-1)=4`）正好衔接。

**第二步（xdrope 对照）**：若改用 xdrope（`xdrope_section=[2,2,2,2]`，4 轴 linear/w/h/t），同样 8 个 token。非图像 token 四轴都用线性 `i`；图像 token 的 linear 轴仍用 `i`，w 轴用列号、h 轴用行号、t 轴用 0（参照 [ncnn_llm_ocr.cpp:648-662](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L648-L662) 的构造规则）。请自行补全下表的 linear/w/h/t 四列，并对比 mRoPE 表，体会「xdrope 额外保留 linear 轴」带来的差异。

**第三步（验证）**：在源码里把两张表的关键格子（如图像 token 4 的宽度 pos、token 7 的图像后 pos）与 [rope_embed.cpp:368-395](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L368-L395) 的分支逐一对照，确认手算与代码一致。

> 说明：本实践为源码阅读 + 手算型，无需运行模型；若要跑真实模型观察 cos/sin 数值，需先按 u1-l2/u1-l5 准备好模型目录与 `model.json`，结果**待本地验证**。

## 6. 本讲小结

- 视觉位置编码之所以要「多轴」，是因为图像 patch 是二维网格，单轴递增 position_id 无法区分「水平相邻」与「垂直相邻」。
- **标准 mRoPE**（`generate_rope_embed_cache_vision_mrope`）把 \(d/2\) 个旋转对连续切成 t/h/w 三段（由 `mrope_section` 决定段宽）；图像 token 在 t 轴恒定、h 轴用行号、w 轴用列号，文本 token 三轴同用线性位置。
- **交错 mRoPE**（`..._interleaved`）专供 Qwen3.5-VL，按 `(t,h,w)` 三元组循环排列维度，硬编码 `{11,11,10}` 与 `merge_size=2`，且只旋转一半维度。
- **Hunyuan xdrope**（`generate_hunyuan_xdrope_cos_sin`）扩到 4 轴（linear/w/h/t），由 `xdrope_section` 前缀划分；基频率用 NTK-alpha 放大（\(\theta_{\text{ntk}}=\theta_{\text{base}}\cdot\alpha^{d/(d-2)}\)）以扩展上下文。
- 三种函数产出的都是 \((d/2,\;\text{seqlen})\) 形状的 cos/sin cache，区别只在「每个 token 每个维度对各用哪个 position_id」。
- 解码器侧的 mRoPE/xdrope 与视觉编码器侧的 `generate_vision_rope_cache_2d`（ViT 内部 2D RoPE）是两条独立的线，不要混淆。

## 7. 下一步学习建议

- 下一讲 u5（视觉语言模型）会把本讲的位置编码接进完整链路：u5-l2 讲图像如何切成 patch、u5-l3 讲 `inject_image_embeds` 如何把视觉特征塞进 `image_pad` 占位，届时你会看到本讲的 `image_pad_index` / `image_embeds_size` 是怎么来的。
- 若想横向对比长上下文技术，可回看 u4-l1 的 NTK/YaRN/LongRoPE，本讲 xdrope 的 NTK-alpha 基与之同源。
- 进阶读者可尝试：为一个新 VLM 在 `model.json` 里配置 `setting.rope.type="mRoPE"` 与合适的 `mrope_section`，并在 u8-l6（接入新模型）里把本讲的轴配置纳入改动清单。
