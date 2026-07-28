# RoPE 基础与长上下文变体

## 1. 本讲目标

本讲是「位置编码 RoPE」专题的第一讲（承接 [u2-l3 prefill 文本预填充流程](u2-l3-prefill-flow.md)）。在 prefill 流程里，`generate_rope_embed_cache` 作为一个「黑盒」出现过：它吃进 token 数量、`rope_head_dim`、`position_id`，吐出两张 `ncnn::Mat`（`cos_cache`/`sin_cache`），再喂给 decoder。本讲要打开这个黑盒。

学完后你应当掌握：

1. **基础 RoPE**：理解 `inv_freq`、cos/sin cache 的生成方式，以及「低维转得快、高维转得慢」的频率分布直觉。
2. **三种长上下文变体**：NTK、YaRN、LongRoPE 各自如何修改 `inv_freq`（或缩放 cos/sin），以及它们为什么能把模型的可用上下文从几千拉到几万、几十万。
3. **配置→代码的接线**：构造函数如何读取 `model.json` 的 `setting.rope` 块，根据 `type` 字段在四个生成函数里四选一。

本讲只覆盖**纯文本、单轴位置**的 RoPE。视觉场景的多轴 mRoPE / Hunyuan xdrope 留给 [u4-l2 视觉位置编码 mRoPE 与 Hunyuan xdrope](u4-l2-vision-rope.md)。

---

## 2. 前置知识

### 2.1 为什么需要位置编码

Transformer 的注意力机制本身是「位置无关」的：把一句话的词打乱顺序，自注意力的矩阵运算结果只是行重排，模型分辨不出先后。所以必须额外注入位置信息。位置编码就是给每个 token 的向量叠上一个「随位置变化」的成分，让模型知道「谁在前、谁在后」。

### 2.2 RoPE 的核心直觉

RoPE（Rotary Position Embedding，旋转位置编码）的做法是：把每个 token 向量切成若干个二维小片（一对相邻维度看作一个二维平面上的向量），然后对每一对维度施加一个**旋转**。旋转的角度由两件事决定：

- **位置 m**：token 在序列里的序号，越往后角度越大。
- **维度对的下标 i**：不同维度对用不同的「转速」。低维对转得快（高频），高维对转得慢（低频）。

具体地，第 i 对维度对位置 m 的旋转角是：

\[
\theta_{m,i} = m \cdot \text{inv\_freq}_i, \qquad \text{inv\_freq}_i = \frac{1}{\theta_{\text{base}}^{\,2i/d}}
\]

其中 \(d\) 是 `rope_head_dim`（参与旋转的维数），\(\theta_{\text{base}}\) 就是配置里的 `rope_theta`（如 100000、1000000）。

把角度预算成 cos/sin 两张表，旋转本身在 ncnn 算子里完成。本讲的四个函数，职责就是生成这两张表：

\[
\texttt{cos\_cache}[m][i] = \cos(\theta_{m,i}), \qquad \texttt{sin\_cache}[m][i] = \sin(\theta_{m,i})
\]

> **关键性质**：由于是「旋转」，两个 token 的点积只依赖它们的**相对位置** \(m-n\)，而不是绝对位置。这正是 RoPE 相对绝对位置编码的优势。

### 2.3 长上下文的痛点

随着位置 m 不断增大，\(\theta_{m,i} = m \cdot \text{inv\_freq}_i\) 会越来越大。对**高频维度对**（小的 i、大的 inv_freq）来说，角度很快就会绕好几圈——cos 值剧烈振荡，模型在这些维度上几乎无法区分「m=40000」和「m=40001」。模型在训练时没见过这么大的角度，**外推（extrapolation）就崩了**。

长上下文变体的共同目标：在不重新训练（或只轻微微调）的前提下，调整 `inv_freq`，让长位置下的角度分布仍接近训练时见过的范围。下面三种变体是三种不同的「调整配方」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) | 声明 `RopeScalingParams` 结构体与全部 RoPE 生成函数（含本讲的四个 + 视觉用函数）。 |
| [src/utils/rope_embed.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp) | 四个变体的实现：`generate_rope_embed_cache` / `generate_ntk_rope_embed_cache` / `generate_yarn_rope_embed_cache` / `generate_rope_embed_cache_LongRoPE`。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `RoPE_Type` 枚举（四选一）与 `rope_head_dim`/`rope_theta`/`short_factor`/`long_factor`/`ntk_scaling_params` 等成员。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 构造函数读取 `setting.rope` 配置；prefill/generate 里按 `rope_type` 四分支调用对应生成函数。 |

> 提醒：`generate_rope_embed_cache_full`、`generate_rope_embed_cache_vision_mrope*`、`generate_hunyuan_xdrope_cos_sin` 不在本讲范围，分别属于嵌入运行时和视觉 RoPE。

---

## 4. 核心概念与源码讲解

### 4.1 基础 RoPE：`generate_rope_embed_cache`

#### 4.1.1 概念说明

这是最朴素的 RoPE：直接用上面的公式生成 cos/sin 表，不做任何缩放。它对应 `model.json` 里 `"type": "RoPE"`（这也是 [readme.md:L252-L256](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L252-L256) 给出的默认例子）。模型能稳定处理的上下文长度，基本由训练时见过的最大位置决定。

#### 4.1.2 核心流程

1. 按 `embed_dim/2` 个维度对，逐个算 `inv_freq[i] = 1/θ^(2i/d)`。
2. 为 `seqlen` 个位置、每个位置 `embed_dim/2` 个维度对，算 `t = (position_id + i) * inv_freq[j]`，再 `cosf(t)`/`sinf(t)`。
3. 写入 `cos_cache`/`sin_cache`，形状都是 `(embed_dim/2, seqlen)`。

伪代码：

```text
for j in 0..d/2:  inv_freq[j] = 1 / theta^(2j/d)
for i in 0..seqlen:
    pos = position_id + i          # 支持从某个偏移开始（多轮对话续写）
    for j in 0..d/2:
        t  = pos * inv_freq[j]
        cos_cache[i][j] = cos(t)
        sin_cache[i][j] = sin(t)
```

注意 `position_id` 参数：首轮 prefill 传 `0`，续写 decode 时传当前 token 总数，使位置连续递增（见 u2-l5）。

#### 4.1.3 源码精读

`inv_freq` 的构造与 cos/sin 双重循环：

[src/utils/rope_embed.cpp:L102-L128](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L102-L128) —— 这段先用一个循环算 `inv_freq`（L104-L108），再 `cos_cache.create(embed_dim/2, seqlen)` 分配 `ncnn::Mat`（L110-L111），最后双重循环按行写入 cos/sin（L113-L127）。`cos_cache.row(i)` 取第 i 行（对应第 i 个位置），逐维度对填值。

形状 `(embed_dim/2, seqlen)`：第一维是「维度对数」，第二维是「序列长度」。每个维度对只需一个角度（一对二维向量共用一个旋转角），所以只需 `d/2` 列而非 `d` 列。

#### 4.1.4 代码实践

见本讲 [第 5 节综合实践](#5-综合实践) 里的独立对比程序——其中 `rope_basic` 函数复刻了这里的公式，可单独打印基础 RoPE 在大位置下的 cos 值。

#### 4.1.5 小练习与答案

**练习**：当 `rope_theta` 从 10000 调到 1000000，最高频维度对（i=0）和最低频维度对（i=d/2-1）的 `inv_freq` 分别怎么变？对旋转角有什么影响？

**答案**：i=0 时 `inv_freq = 1/θ^0 = 1`，与 θ 无关，不变；i=d/2-1 时 `inv_freq ≈ 1/θ`，θ 增大 100 倍则 inv_freq 缩小约 100 倍。效果：θ 越大，低速维度对的旋转角越小（转得更慢），整体频率分布被「压扁」，使模型在更大位置范围内角度都不会绕太多圈——这就是现代模型把 `rope_theta` 从 10000 调到 100000 甚至 1000000 的原因。

---

### 4.2 NTK-RoPE：`generate_ntk_rope_embed_cache`

#### 4.2.1 概念说明

NTK（Neural Tangent Kernel-aware）插值的思路是：**不要对所有维度一刀切**。高频维度对（小 i）负责局部细节，外推能力本来就还行，应尽量保持原样；低频维度对（大 i）负责长程关系，正是外推崩坏的重灾区，需要把它们的周期拉长。

NTK 通过**只放大 base θ** 来实现「差异化拉伸」：令

\[
\theta_{\text{ntk}} = \theta \cdot \alpha^{\,d/(d-2)}, \qquad \text{inv\_freq}_i = \frac{1}{\theta_{\text{ntk}}^{\,2i/d}}
\]

比较它与基础版的比值：

\[
\frac{\text{inv\_freq}_i^{\text{ntk}}}{\text{inv\_freq}_i^{\text{base}}} = \alpha^{-2i/(d-2)}
\]

- i=0（最高频）：比值为 1，**完全不动**。
- i=d/2（最低频）：比值 ≈ \(\alpha^{-1}\)，inv_freq 缩小到 1/α，**周期拉长 α 倍**。

于是低频维度对被大幅拉伸、高频维度对几乎不变，恰好对症下药。`α` 来自 `model.json` 的 `rope_scaling.alpha`。

#### 4.2.2 核心流程

1. 算 `base_correction = α^(d/(d-2))`，得 `ntk_theta = θ · base_correction`。
2. 用 `ntk_theta` 代替 θ 算 `inv_freq`。
3. cos/sin 双重循环与基础版完全相同（无额外缩放）。

#### 4.2.3 源码精读

[src/utils/rope_embed.cpp:L62-L100](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L62-L100) —— L71-L75 先算 `base_correction` 与 `ntk_theta`（注意用的是 `scaling_params.alpha`，L72）；L77-L81 用 `ntk_theta` 算 inv_freq；L83-L99 的 cos/sin 循环与基础版逐行一致，**没有任何 mscale 或额外乘法**。

#### 4.2.4 代码实践

见 [第 5 节](#5-综合实践) 程序中的 `rope_ntk` 函数：固定一个较大的 `position_id`（如 40000），对比基础版与 NTK 版在第 0 维与最后一维的 cos 值，你会看到最后一维的基础 cos 已剧烈振荡、NTK cos 被压回接近 1（周期被拉长）。

#### 4.2.5 小练习与答案

**练习**：为什么 NTK 选 `α^(d/(d-2))` 这种带 `d/(d-2)` 的指数，而不是简单地 `ntk_theta = θ·α`？

**答案**：若直接 `θ·α`，则所有维度对的 inv_freq 都被均匀乘以 `α^{-2i/d}`，连高频维度对也被拉伸，会破坏模型对局部细节的刻画。用 `α^(d/(d-2))` 代入后，比值变成 `α^{-2i/(d-2)}`：分母是 `(d-2)` 而非 `d`，使得 i=0 时比值精确为 1（高频不动），低频才显著拉伸。这是 NTK 论文里从 NT K 核视角推出的「温和」插值。

---

### 4.3 YaRN：`generate_yarn_rope_embed_cache`

#### 4.3.1 概念说明

YaRN（Yet another RoPE extensioN）在 NTK 基础上再加两件事：按波长分段的「坡道插值」、以及对注意力分数的整体缩放（mscale）。本仓库里的实现有一个**重要事实**需要先点出（见 4.3.3）：经典的波长坡道（`yarn_ramp`）在这份代码里被**计算了但并未实际作用于 inv_freq**，真正生效的差异只有「mscale 乘到 cos/sin 上」。换句话说，**本实现里 YaRN ≈ NTK（同样的 `α^(d/(d-2))` 公式）+ mscale 注意力缩放**。

mscale 的作用：长上下文下注意力会被稀释，YaRN 用一个 >1 的乘子放大 cos/sin（等价于放大旋转后的注意力对比度），缓解稀释。

#### 4.3.2 核心流程

1. `scale = scaling_params.alpha`，与 NTK 用同样的 `base_correction = scale^(d/(d-2))` 算 inv_freq。
2. cos/sin 循环里，若 `mscale != 1`，把每个 cos/sin 乘以 `mscale`。

#### 4.3.3 源码精读

[src/utils/rope_embed.cpp:L7-L11](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L7-L11) —— `yarn_ramp` 是经典 YaRN 的分段线性函数（低于 low 返回 0、高于 high 返回 1、中间线性过渡）。

[src/utils/rope_embed.cpp:L13-L60](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L13-L60) —— 仔细看 L31-L40：`freq`、`r`、`ramp` 三个局部变量**都被计算了却没有出现在 inv_freq 的表达式里**（L39 的 inv_freq 与 NTK 的 L80 完全相同，只是把 `alpha` 换名叫 `scale`）。真正与 NTK 不同的只有 L50-L57：当 `mscale != 1` 时给 cos/sin 乘 `mscale`。

> 这是一处容易误读的地方：看到 `yarn_ramp` 会以为完整的 YaRN 波长混合已经接入，但本实现中它属于遗留/未启用代码。以源码为准。

#### 4.3.4 代码实践

见 [第 5 节](#5-综合实践) 程序中的 `rope_yarn`：把 `mscale` 设为 1 时，它的 cos 与 NTK 完全相同；设为 1.4 时整体被放大——你可以直观看到 mscale 的「对比度增强」效果。

#### 4.3.5 小练习与答案

**练习**：给定同样的 `alpha`，YaRN（mscale=1）与 NTK 的 cos_cache 数值应有什么关系？把 mscale 调大会改变「相对位置」信息吗？

**答案**：mscale=1 时两者数值**完全相等**（inv_freq 公式一致、无额外缩放）。mscale>1 会把整层 cos/sin 等比放大，相当于放大旋转角、进而放大注意力 logit 的对比度，但**每一对维度对的相对关系不变**，所以相对位置编码的性质保留——它调的是「注意力有多尖锐」，不是「谁在谁前面」。

---

### 4.4 LongRoPE：`generate_rope_embed_cache_LongRoPE`

#### 4.4.1 概念说明

LongRoPE（Microsoft，用于 Phi-3 等长上下文模型）走得更极端：**每个维度对都用一个独立、人工搜索出来的插值因子**。NTK/YaRN 只有一个标量 α 控制全局拉伸曲线，而 LongRoPE 给 `d/2` 个维度对各配一个浮点因子，存成两张数组：

- `SHORT_FACTOR[d/2]`：序列长度 ≤ 训练长度时用。
- `LONG_FACTOR[d/2]`：序列长度 > `original_max_position_embeddings` 时用。

这些因子是用进化算法在目标长上下文上搜出来的，比单参数公式精细得多，因而能把上下文扩到 128k 甚至更高。

#### 4.4.2 核心流程

1. 用**基础**公式算 inv_freq（`1/θ^(2i/d)`，注意这里 base 不变）。
2. 选插值数组：`seqlen > ORIGINAL_MAX_POSITION_EMBEDDINGS` 用 `LONG_FACTOR`，否则 `SHORT_FACTOR`。
3. 对每个维度对 j，把频率除以因子：`freq = t / ext_factor[j]`，再 `cos(freq)·scaling_factor`。
4. `scaling_factor` 由 `compute_scaling_factor` 算出（一个 sqrt-log 公式）。

公式：

\[
f_{m,j} = \frac{m \cdot \text{inv\_freq}_j}{\text{ext\_factor}_j}, \qquad \texttt{cos}[m][j] = \cos(f_{m,j}) \cdot s
\]

#### 4.4.3 源码精读

[src/utils/rope_embed.cpp:L247-L250](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L247-L250) —— `compute_scaling_factor` 的定义。注意它被调用时（L287）传入的 `max_position_embeddings` 就是 `ORIGINAL_MAX_POSITION_EMBEDDINGS` 本身，而函数第二个参数默认是 `32768`。所以：当模型的 `original_max_position_embeddings` 正好等于 32768（Phi-3 默认）时，`scale = 1`、`scaling_factor = sqrt(1)=1`，**整体缩放退化为不缩放**；只有 OMPE ≠ 32768 时 `scaling_factor` 才偏离 1。

[src/utils/rope_embed.cpp:L252-L300](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L252-L300) —— L286 按 seqlen 与 OMPE 的大小关系选 `ext_factor`；L289-L299 双重循环里 `freq = (t * inv_freq[j]) / ext_factor[j]`（L295），cos/sin 都乘 `scaling_factor`（L296-L297）。注意 L277-L278 用 `channel(0)` 取一维缓冲、L291-L292 用 `i*half_dim` 手动算行偏移，与基础版用 `row(i)` 的写法不同（布局相同，访问方式不同）。

#### 4.4.4 代码实践

见 [第 5 节](#5-综合实践) 程序中的 `rope_longrope`：填入两组示例 `SHORT_FACTOR`/`LONG_FACTOR`（**示例数据，非真实模型权重**），观察长序列下选用 LONG_FACTOR 后高频维度对的 cos 被压回平稳。

#### 4.4.5 小练习与答案

**练习**：LongRoPE 为什么需要 SHORT/LONG 两套因子，而不是一套？

**答案**：短序列时模型仍在训练分布内，应尽量保持原始频率结构（SHORT_FACTOR 接近 1）；只有当序列越过训练长度、需要外推时才切换到大幅插值的 LONG_FACTOR。若短序列也用 LONG_FACTOR，会过度拉伸、损害正常长度下的表现。代码里用 `seqlen > ORIGINAL_MAX_POSITION_EMBEDDINGS` 作为切换阈值。

---

### 4.5 构造函数的 `rope.type` 选择：把四者串起来

#### 4.5.1 概念说明

四个生成函数是平行的工具，模型到底用哪个，由 `model.json` 的 `setting.rope.type` 决定。构造函数把字符串映射成枚举 `RoPE_Type`，并把变体专用的参数（NTK/YaRN 的 scaling、LongRoPE 的 factor 数组）存进成员变量。prefill 与 generate 在真正需要 cos/sin 时，再按枚举四选一调用。

#### 4.5.2 核心流程

1. 读 `setting.rope.rope_head_dim` → 成员 `rope_head_dim`（默认 64）。
2. 按 `setting.rope.type` 字符串设 `rope_type` 枚举：
   - `"RoPE"` → `RoPE`
   - `"NTKRoPE"` → `NTK_RoPE`
   - `"YaRNRoPE"` → `YARN_RoPE`
   - `"LongRoPE"` → `LongRoPE`（同时读 `short_factor`/`long_factor`/`original_max_position_embeddings`）。
3. 若有 `rope_scaling` 子块，填充 `ntk_scaling_params`（alpha/beta_fast/beta_slow/factor/mscale/mscale_all_dim）。
4. 读 `rope_theta`。
5. 运行时（prefill 主体段、末位 token、generate 每步）用 `if/else if` 链调用对应函数。

#### 4.5.3 源码精读

枚举与成员声明：[src/ncnn_llm_gpt.h:L112-L126](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L112-L126) —— `rope_head_dim` 默认 64、`rope_theta` 默认 100000、`RoPE_Type` 四值枚举、`ntk_scaling_params`、LongRoPE 专用的两个 factor 向量与 `original_max_position_embeddings`。

构造函数配置读取：[src/ncnn_llm_gpt.cpp:L116-L146](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L116-L146) —— L121-L133 是 type 字符串到枚举的映射，注意字符串必须精确匹配（`"NTKRoPE"`、`"YaRNRoPE"`，大小写敏感）；L135-L143 读取 `rope_scaling` 子块填 `ntk_scaling_params`；L145 读 `rope_theta`。整块被 `if (config["setting"].contains("rope"))` 守护，没有 rope 块时全部走默认值。

prefill 里的四分支调用（主体段）：[src/ncnn_llm_gpt.cpp:L256-L266](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L256-L266) —— 这是 u2-l3 里 RoPE 黑盒的「真身」。同一个 `if/else if` 模式在文件里**重复出现多次**（末位 token 段 [L333-L342](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L333-L342)、多轮 prefill [L649-L657](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L649-L657)、generate 每步 [L746-L754](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L746-L754)、generate 内部 [L895-L903](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L895-L903)），只是 `seqlen`/`position_id` 不同。规律：`seqlen=token_ids.size()`、`position_id` 首轮为 0、续写时为当前 token 总数。

#### 4.5.4 代码实践（配置型）

打开任意一个文本模型的 `assets/<模型目录>/model.json`，找到 `setting.rope` 块。对照 [src/ncnn_llm_gpt.cpp:L116-L146](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L116-L146)，逐字段标注：每个 JSON 键被构造函数的哪一行读取、写入哪个成员变量。若该模型用的是 `LongRoPE`，确认它的 `short_factor`/`long_factor` 数组长度是否等于 `rope_head_dim/2`。

#### 4.5.5 小练习与答案

**练习**：如果 `model.json` 写了 `"type": "yarnrope"`（全小写），会发生什么？

**答案**：构造函数的字符串比较是大小写敏感的（L131 是 `rope_cfg["type"] == "YaRNRoPE"`），`"yarnrope"` 不匹配任何分支，`rope_type` 保持默认枚举值 `RoPE`（=0），于是运行时走基础 `generate_rope_embed_cache`，而不是 YaRN。模型仍能跑，但用的是错误的（未缩放的）位置编码，长上下文效果会退化。这类静默 fallback 是排查「长上下文变笨」问题时值得首先核对的点。

---

## 5. 综合实践

**目标**：用一个独立小程序，亲自生成基础 RoPE 与三种变体的 cos 值，在同一 `position_id` 下对比，直观看到长上下文变体如何「压平」高频维度对的剧烈振荡。

下面的程序是**示例代码**——它复刻了 `rope_embed.cpp` 里四个函数的数学公式（纯 `<cmath>`，不依赖 ncnn），所以你可以用任何 `g++` 直接编译运行，便于快速实验。真实推理用的是项目里那四个 `ncnn::Mat` 版本的函数，公式与此完全一致。

```cpp
// 示例代码：rope_compare.cpp —— 复刻 rope_embed.cpp 四个变体的 inv_freq / cos 公式
// 编译： g++ -O2 -std=c++17 rope_compare.cpp -o rope_compare
#include <cstdio>
#include <cmath>
#include <vector>

int d = 64;            // rope_head_dim
float theta = 100000;  // rope_theta

// 1) 基础 RoPE：对应 generate_rope_embed_cache
float rope_basic(int pos, int j) {
    float inv_freq = 1.0f / std::pow(theta, (float)(j * 2) / d);
    return std::cos(pos * inv_freq);
}

// 2) NTK-RoPE：对应 generate_ntk_rope_embed_cache
float rope_ntk(int pos, int j, float alpha) {
    float base_correction = std::pow(alpha, (float)d / (d - 2.0f));
    float ntk_theta = theta * base_correction;
    float inv_freq = 1.0f / std::pow(ntk_theta, (float)(j * 2) / d);
    return std::cos(pos * inv_freq);
}

// 3) YaRN：inv_freq 同 NTK（scale=alpha），再乘 mscale
float rope_yarn(int pos, int j, float alpha, float mscale) {
    float base_correction = std::pow(alpha, (float)d / (d - 2.0f));
    float ntk_theta = theta * base_correction;
    float inv_freq = 1.0f / std::pow(ntk_theta, (float)(j * 2) / d);
    return std::cos(pos * inv_freq) * mscale;
}

// 4) LongRoPE：inv_freq 用基础公式，频率除以逐维因子，再乘 scaling_factor
float compute_scaling_factor(int ompe, int ref = 32768) {
    float scale = (float)ompe / (float)ref;
    return std::sqrt(1.0f + std::log(scale) / std::log((float)ref));
}
float rope_longrope(int pos, int j, const std::vector<float>& ext, int ompe) {
    float inv_freq = 1.0f / std::pow(theta, (float)(j * 2) / d);
    float freq = (pos * inv_freq) / ext[j];
    return std::cos(freq) * compute_scaling_factor(ompe);  // ompe=32768 时 ≈1
}

int main() {
    int pos = 40000;              // 一个远超常见训练长度的位置
    float alpha = 4.0f;           // NTK/YaRN 缩放
    float mscale = 1.4f;
    int ompe = 32768;
    int half = d / 2;
    // 示例因子（非真实权重）：低维(高频)接近1，高维(低频)显著>1
    std::vector<float> long_factor(half, 1.0f);
    for (int j = 0; j < half; j++) long_factor[j] = 1.0f + 7.0f * (float)j / (half - 1);

    printf("pos=%d, d=%d, theta=%.0f, alpha=%.1f\n", pos, d, theta, alpha);
    printf("%4s %12s %12s %12s %12s\n", "j", "basic", "ntk", "yarn(ms)", "longrope");
    for (int j : {0, half / 4, half / 2, half - 1}) {
        printf("%4d %12.5f %12.5f %12.5f %12.5f\n", j,
               rope_basic(pos, j),
               rope_ntk(pos, j, alpha),
               rope_yarn(pos, j, alpha, mscale),
               rope_longrope(pos, j, long_factor, ompe));
    }
    return 0;
}
```

**操作步骤**

1. 把上面的程序存为 `rope_compare.cpp`（放在仓库外任意目录即可，不要污染源码树）。
2. 编译运行：`g++ -O2 -std=c++17 rope_compare.cpp -o rope_compare && ./rope_compare`。
3. 把 `pos` 从 `40000` 改成 `100`、再改成 `200000`，各跑一次，对比同一行（尤其 `j = half-1` 最低频维度对）的数值变化。

**需要观察的现象与预期结果**

- **基础 RoPE**：在 `pos=40000` 时，低 j（高频）维度的 cos 值会落到 -1~1 之间的任意位置、且 pos 稍变就剧烈跳变——这正是外推崩坏的信号。
- **NTK / YaRN(mscale=1)**：最低频维度对（`j=half-1`）的 cos 被显著「拉回」接近 1（因为周期被放大 α 倍）；最高频维度对（`j=0`）的 cos 与基础版几乎相同（NTK 不动高频）——验证 4.2 的差异化拉伸。
- **YaRN(mscale=1.4)**：在 NTK 基础上整体放大 1.4 倍（注意 cos 可能 >1，因为 mscale 是对旋转后注意力对比度的放大，这里只是演示数值趋势）。
- **LongRoPE**：低 j 维度因子接近 1、cos 与基础版接近；高 j 维度因子大、cos 被压回平稳——但因为是**逐维**因子，曲线形状与 NTK 的平滑曲线不同。
- 把 `pos` 调小到 `100`：所有变体数值都接近基础版（短序列下插值几乎不起作用），对应 4.4.5 里「短序列不该过度插值」的结论。

> 若你本地无法编译，可改为**源码阅读型实践**：打开 [src/utils/rope_embed.cpp:L102-L300](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L102-L300)，在纸上对 `pos=40000, j=31, d=64, theta=100000` 手算基础版与 NTK 版（α=4）的 inv_freq 与 cos，得出同样结论——这一步**待本地验证**。

---

## 6. 本讲小结

- RoPE 把每对维度看作二维向量做旋转，角度 \(\theta_{m,i}=m\cdot\text{inv\_freq}_i\)，`inv_freq` 由 `rope_head_dim` 与 `rope_theta` 决定；四个生成函数负责预算 cos/sin 两张表。
- **基础 `generate_rope_embed_cache`** 直接套公式，长位置下高频维度对角度过大、外推崩坏。
- **NTK** 用 `ntk_theta = θ·α^(d/(d-2))` 差异化拉伸：高频不动、低频周期放大 α 倍，对症长程外推。
- **YaRN** 在本仓库实现里 = NTK 的 inv_freq 公式 + `mscale` 注意力缩放；经典 `yarn_ramp` 波长混合在此是计算但未生效的遗留代码，以源码为准。
- **LongRoPE** 给每个维度对一个独立、搜索出的因子（SHORT/LONG 两套），按 `seqlen` 是否超过 `original_max_position_embeddings` 切换，精度最高、可扩到 128k+。
- 构造函数用 `setting.rope.type`（大小写敏感）选枚举，prefill/generate 里同一套 `if/else if` 四分支重复出现，仅 `seqlen`/`position_id` 不同。

---

## 7. 下一步学习建议

1. 继续 [u4-l2 视觉位置编码 mRoPE 与 Hunyuan xdrope](u4-l2-vision-rope.md)：本讲的 `position_id` 是单轴标量，视觉模型要把图像 token 拆到 t/h/w 多轴，看 `generate_rope_embed_cache_vision_mrope` 与 `generate_hunyuan_xdrope_cos_sin` 如何复用同一套 `inv_freq` 思想。
2. 回到 [u2-l3 prefill](u2-l3-prefill-flow.md) 与 [u2-l4 generate](u2-l4-generate-loop.md)，结合本讲重新审视 cos/sin cache 如何作为 `in2`/`in3` 喂进 decoder，以及 `position_id` 跨轮连续递增的语义。
3. 想深入原理，可阅读 RoPE（Su et al.）、NTK-aware scaling、YaRN、LongRoPE（Microsoft）四篇论文，对照本讲公式逐行验证。
