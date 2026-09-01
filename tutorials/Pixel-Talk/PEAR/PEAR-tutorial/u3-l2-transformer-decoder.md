# TransformerDecoder：零 token 查询与 cross-attention

## 1. 本讲目标

上一讲（u3-l1）我们读完了 ViT 骨干：它把 256×192 的人体 patch 变成 `(B, 1280, 16, 12)` 的特征图。本讲沿着数据流往下走一步，回答一个问题：

**这张特征图里的信息，是怎么被"压缩"成一组人体参数的？**

答案是本讲的主角 `TransformerDecoder`：它用**一个零初始化的查询 token**，通过 6 层 cross-attention 不断从 192 个图像 token 中"抽取"信息，最终把这一个 token 交给下一讲的 9 个线性解码器，翻译成 SMPL-X 与 FLAME 参数。

学完本讲你应当能够：

1. 说清 **零 token 作为 Query、图像 token 作为 Key/Value** 的 cross-attention 设计，以及为什么单 token 的自注意力是"退化"的。
2. 画出 `PreNorm → Attention / CrossAttention / FeedForward` 的残差堆叠结构（`TransformerCrossAttn`）。
3. 区分 `DropTokenDropout`（整列丢弃 token）与 `ZeroTokenDropout`（原位置零）两种 dropout 变体的行为差异。
4. 用 HEAD 配置独立实例化 `TransformerDecoder`，跑通 forward 并解释每一步张量形状。

## 2. 前置知识

### 2.1 Query / Key / Value：一场"提问—检索"游戏

注意力机制可以类比图书馆检索：

- **Query（Q，查询）**：你想问的问题；
- **Key（K，索引）**：每本书的标签；
- **Value（V，内容）**：每本书的正文。

计算流程是：用 Q 和每个 K 算相似度，softmax 归一化成权重，再按权重对 V 加权求和：

\[ \mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left( \frac{Q K^{\top}}{\sqrt{d_k}} \right) V \]

其中 \( d_k \) 是每个头的维度，除以 \( \sqrt{d_k} \)（即代码里的 `scale`）是为了防止点积随维度增大而过大、导致 softmax 饱和。

### 2.2 自注意力 vs 交叉注意力

- **自注意力（self-attention）**：Q、K、V 都来自**同一个序列**，序列内部的 token 互相交换信息。u3-l1 里 ViT 骨干的 32 层 Block 用的就是它。
- **交叉注意力（cross-attention）**：Q 来自序列 A，K、V 来自**另一个序列 B**。信息从 B 流向 A。本讲里 A 是 1 个零 token，B 是 192 个图像 token——相当于"拿着一个空篮子去图像特征里取货"。

### 2.3 多头与维度约定

把 1024 维的 token 拆成 `heads=8` 个 64 维的子空间（头）分别做注意力，再拼回去。本讲涉及两个维度，务必分清：

| 名称 | 代码字段 | PEAR 中的值 | 含义 |
| --- | --- | --- | --- |
| 模型宽度 | `dim` | 1024 | token 在层间流动时的维度 |
| 图像特征维度 | `context_dim` | 1280 | 骨干输出的通道数（u3-l1） |

注意力内部还会先把两者投影到 `inner_dim = dim_head × heads = 64 × 8 = 512`，最后再投影回 1024。

### 2.4 与前面讲义的衔接

- u2-l5 已给出全景：head 把 `(1,1280,16,12)` 特征重排成 192 个 context token，一个零 token 经 6 层 cross-attention 得 `(1,1024)`。本讲把这段黑盒彻底打开。
- u3-l1 讲过骨干 `mlp_ratio=4`（FFN 隐藏层是 4 倍宽）。注意本讲的 `mlp_dim=1024` **等于** `dim`，并没有 4 倍扩张——这是解码头与骨干的一个显著差异。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [models/smplx/pose_transformer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py) | 本讲主文件：`PreNorm`、`Attention`、`CrossAttention`、`TransformerCrossAttn`、`DropTokenDropout`、`ZeroTokenDropout`、`TransformerDecoder` 全部在此 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | HEAD 段给出解码器的全部超参数 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | `SMPLXTransformerDecoderHead`：构造 `TransformerDecoder` 并喂数据的调用方（参数解码器本身留到 u3-l3） |
| [models/smplx/t_cond_mlp.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/t_cond_mlp.py) | `normalization_layer` 工厂与 `AdaptiveLayerNorm1D`（条件 LayerNorm，PEAR 未启用） |
| [utils/graphics.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics.py) | `overlay_attention_on_image`：把注意力权重画回输入图，综合实践会用到 |

另外两处只需知道存在：[models/smplx/pose_transformer.py:132-162](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L132-L162) 的 `Transformer` 与 [L246-L303](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L246-L303) 的 `TransformerEncoder` 是纯自注意力版本，PEAR 推理链路没有用到它们（属"入口不可达"的备用件），但它们与 `TransformerCrossAttn`/`TransformerDecoder` 共享同一套零件。

## 4. 核心概念与源码讲解

### 4.1 Attention 与 CrossAttention：一对孪生注意力

#### 4.1.1 概念说明

`Attention` 与 `CrossAttention` 是同一套注意力公式的两种接线方式：

- `Attention`：Q、K、V 全部由同一个输入 `x` 经**一个** `to_qkv` 线性层一次性算出——这是自注意力。
- `CrossAttention`：Q 由 `x` 经 `to_q` 算出，K、V 由**另一个张量** `context` 经 `to_kv` 算出——这是交叉注意力。`to_q` 和 `to_kv` 的输入维度可以不同（1024 vs 1280），这正是"两个来源"在代码上的体现。

先讲一个本讲最重要的洞察，后面反复用到：

> **当序列里只有 1 个 token 时，自注意力是退化的。** 单 token 意味着 K 也只有 1 个，softmax 作用在单个数上恒等于 1，于是输出就是该 token 自己的 V 投影：\( \mathrm{softmax}(z) \equiv 1 \Rightarrow \mathrm{out} = V \)。它退化成"一个逐点非线性变换 + 残差"，**不发生任何 token 间的信息交换**。PEAR 的 `num_tokens=1`，所以解码头里真正的信息通道只有 cross-attention——图像信息全部经由 K/V 这条侧路流入零 token。

#### 4.1.2 核心流程

以真实配置（`dim=1024`、`context_dim=1280`、`heads=8`、`dim_head=64`、`inner_dim=512`）为例，`CrossAttention.forward(x, context)` 的流程：

```
x: (B, n_q, 1024)            # PEAR 中 n_q = 1（零 token）
context: (B, n_k, 1280)      # PEAR 中 n_k = 192（图像 token）

q = to_q(x)                  # (B, n_q, 512)
k, v = to_kv(context).chunk  # 各 (B, n_k, 512)
分头 rearrange               # (B, 8, n_q, 64) / (B, 8, n_k, 64)

dots = q @ kᵀ × scale        # (B, 8, n_q, n_k)   scale = 64^(-1/2) = 1/8
attn = softmax(dots, dim=-1) # 沿 n_k 归一化，每行和为 1
out = attn @ v               # (B, 8, n_q, 64)
拼回头 → to_out              # (B, n_q, 1024)
```

直观解读：attn 的每一行是这 1 个查询 token 对 192 个图像 token 的"关注度分布"，输出就是 192 个图像 Value 的加权平均——零 token 完成了一次对整张特征图的信息汇聚。

两个基础件先扫一眼：

- `PreNorm`（Pre-Norm 残差包装）：先 LayerNorm 再进子层，配合外部 `+ x` 残差。这是与 ViT 骨干（u3-l1）一致的现代写法，优点是深层梯度稳定。
- `FeedForward`：`Linear(1024,1024) → GELU → Dropout → Linear(1024,1024) → Dropout`。注意隐藏维等于输入维（`mlp_dim=1024`），不是骨干那种 4 倍扩张。

#### 4.1.3 源码精读

**PreNorm：先归一化再进子层**。[models/smplx/pose_transformer.py:24-34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L24-L34) 把"选哪种归一化"交给工厂函数，并在 `AdaptiveLayerNorm1D` 时把额外条件参数 `*args` 一并传进去（条件归一化需要时间步/条件向量 t，PEAR 用 `norm='layer'` 走不到这条分支）：

```python
class PreNorm(nn.Module):
    def __init__(self, dim: int, fn: Callable, norm: str = "layer", norm_cond_dim: int = -1):
        self.norm = normalization_layer(norm, dim, norm_cond_dim)
        self.fn = fn
    def forward(self, x, *args, **kwargs):
        ...
        return self.fn(self.norm(x), **kwargs)
```

工厂函数在 [models/smplx/t_cond_mlp.py:48-59](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/t_cond_mlp.py#L48-L59)：`"layer"` 返回 `nn.LayerNorm(dim)`，`"ada"` 返回 `AdaptiveLayerNorm1D`（[L7-L33](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/t_cond_mlp.py#L7-L33)，用条件向量调制 scale/shift）。PEAR 只用 LayerNorm，`ada` 是留而未用的扩展点。

**FeedForward：等宽两层 MLP**。[models/smplx/pose_transformer.py:37-49](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L37-L49) 就是 `Linear → GELU → Dropout → Linear → Dropout`，因配置 `mlp_dim=1024` 而等宽。

**Attention：单输入三合一投影**。[models/smplx/pose_transformer.py:72-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L72-L83) 一层 `to_qkv` 同时算出 Q/K/V 再 `chunk(3, dim=-1)` 切开，省了两次矩阵乘的调用开销：

```python
def forward(self, x):
    qkv = self.to_qkv(x).chunk(3, dim=-1)
    q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=self.heads), qkv)
    dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale   # scale = dim_head ** -0.5
    attn = self.attend(dots)                                   # softmax(dim=-1)
    out = torch.matmul(attn, v)
    ...
```

**CrossAttention：Q 与 K/V 分家**。[models/smplx/pose_transformer.py:98-113](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L98-L113) 是本模块的关键：`to_kv` 的输入维度是 `context_dim`（1280），`to_q` 的输入维度是 `dim`（1024），二者都被投到 `inner_dim=512`：

```python
context_dim = default(context_dim, dim)
self.to_kv = nn.Linear(context_dim, inner_dim * 2, bias=False)  # 1280 -> 1024
self.to_q  = nn.Linear(dim, inner_dim, bias=False)              # 1024 -> 512
```

forward 里 [L109-L129](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L109-L129) 的注意力主体与 `Attention` 完全同构，区别只在 K/V 的来源：`context = default(context, x)`——不传 context 时退化为自注意力。[L120-L125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L120-L125) 有一段被注释掉的可视化调试代码（配合文件顶部 [L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L12) 导入的 `overlay_attention_on_image`），说明作者开发时曾在指定层把注意力热图叠回输入图检查——我们在综合实践里会重新启用这个思路。

顺带一提文件顶部的 [exists/default 辅助函数（L14-L21）](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L14-L21)和 `einops.rearrange` 的分头写法，都是社区开源 Transformer 实现的常见惯例；[L11](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L11) 被注释掉的 `# from .vit import Attention, FeedForward` 和 [smplx_head.py:104](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L104) 的 docstring「Cross-attention based SKEL Transformer decoder」都暗示这份文件是从既有项目（SKEL 系）改造而来。

#### 4.1.4 代码实践

**实践一：手工复算 CrossAttention，验证你真的懂了公式。**

1. 实践目标：不借助任何封装，手工按公式算一遍 cross-attention，确认与模块输出一致；并验证单 token softmax 恒为 1。
2. 操作步骤：在仓库根目录、u1-l2 搭好的 `pear` 环境中运行下面脚本（导入 `pose_transformer` 会连带导入 `utils.graphics`，因此需要 pytorch3d/cv2/matplotlib 已装；纯 CPU 即可）。以下为示例代码：

   ```python
   # practice_u3l2_attn.py（示例代码，仓库根目录运行：python practice_u3l2_attn.py）
   import torch
   from einops import rearrange
   from models.smplx.pose_transformer import Attention, CrossAttention

   torch.manual_seed(0)
   ca = CrossAttention(dim=1024, context_dim=1280, heads=8, dim_head=64).eval()
   tok = torch.randn(1, 1, 1024)     # 1 个查询 token
   ctx = torch.randn(1, 128, 1280)   # 128 个假图像 token（真实是 192 个）

   out = ca(tok, context=ctx)

   # —— 手工按公式复算 ——
   q = rearrange(ca.to_q(tok), 'b n (h d) -> b h n d', h=8)
   k, v = ca.to_kv(ctx).chunk(2, dim=-1)
   k = rearrange(k, 'b n (h d) -> b h n d', h=8)
   v = rearrange(v, 'b n (h d) -> b h n d', h=8)
   dots = torch.matmul(q, k.transpose(-1, -2)) * ca.scale       # (1,8,1,128)
   attn = dots.softmax(dim=-1)
   manual = ca.to_out(rearrange(torch.matmul(attn, v), 'b h n d -> b n (h d)'))
   print('手工复算与模块输出一致：', torch.allclose(out, manual, atol=1e-6))
   print('attn 形状 =', attn.shape, ' 每行权重和 =', attn.sum(-1))

   # —— 单 token 自注意力退化验证 ——
   sa = Attention(dim=1024, heads=8, dim_head=64).eval()
   x1 = torch.randn(1, 1, 1024)
   _, k1, v1 = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=8),
                   sa.to_qkv(x1).chunk(3, dim=-1))
   print('单 token softmax =', torch.softmax(torch.zeros(1), dim=-1).item())
   print('自注意力输出 == to_out(v)：',
         torch.allclose(sa(x1), sa.to_out(rearrange(v1, 'b h n d -> b n (h d)'))))
   ```

3. 需要观察的现象：`attn` 形状为 `(1, 8, 1, 128)`；`attn.sum(-1)` 每个（头, 查询）位置的权重和都恒等于 1。
4. 预期结果：两个 `allclose` 均打印 `True`；单 token softmax 打印 `1.0`。（逐位运算顺序一致，理论上严格成立；具体到 `atol` 的数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CrossAttention` 需要 `to_q` 和 `to_kv` 两个输入维度不同的线性层，而 `Attention` 只需一个 `to_qkv`？

**答案**：`Attention` 的 Q/K/V 同源，输入都是 `dim`，可一次投影三份；`CrossAttention` 的 Q 来自 token 序列（`dim=1024`），K/V 来自图像 context（`context_dim=1280`），两个来源维度不同，必须各配一条投影，把它们送到公共的 `inner_dim=512` 空间后才能做点积。

**练习 2**：PEAR 中 `num_tokens=1`，这层自注意力每层的"注意力权重"是多少？它还有信息混合作用吗？

**答案**：softmax 作用在单个 logit 上恒为 1，注意力矩阵是 1×1 的 `[1.0]`，输出就是 `to_out(v)`。没有信息混合作用，只是一次逐点变换加残差；信息混合完全由 cross-attention 承担。

**练习 3**：[pose_transformer.py:120-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L120-L125) 被注释的代码和 `forward` 里无人使用的 `idx` 参数是做什么的？

**答案**：调试可视化钩子。在指定层（`idx == 2`）把注意力权重 `attn` 通过 `utils/graphics.py` 的 `overlay_attention_on_image` 叠回输入图保存成 png，用来人工检查模型"看"了哪里。生产推理不启用，因此 `idx` 目前只是遗留形参。

### 4.2 TransformerCrossAttn：六层「自注意 → 交叉注意 → FFN」堆叠

#### 4.2.1 概念说明

`TransformerCrossAttn` 是解码器的"层堆叠器"：把若干个 `[自注意力, 交叉注意力, 前馈]` 三件套按 Pre-Norm 残差方式串起来。PEAR 配置 `depth=6`，即 6 层。

它解决的问题是**信息抽取的迭代加深**：一次 cross-attention 只能做一次"加权平均"式的汇聚；堆 6 层、每层都重新从图像 token 里取一次信息并经 FFN 加工，零 token 就能逐层提炼出从低级特征到高层语义的表征——类似 DETR 系列中 object query 反复读图像特征的过程，只不过这里只有 1 个 query，且最终目的是回归而非检测。

值得注意的是每层三件套里**自注意力排在最前**。结合 4.1 的结论：单 token 时它退化为逐点变换，但如果未来 `num_tokens>1`（比如多人或多假设），它就会承担 token 之间的协商。这套结构对两种用法都兼容。

#### 4.2.2 核心流程

```
输入 x: (B, n_q, 1024)          # PEAR: n_q = 1
context: (B, 192, 1280)

repeat depth=6 次:
    x = x + SelfAttn(LN(x))         # 单 token 时退化为逐点变换
    x = x + CrossAttn(LN(x), K/V=LN 前的 context)   # 图像信息注入 ← 唯一信息入口
    x = x + FFN(LN(x))              # 逐点非线性加工

输出 x: (B, n_q, 1024)
```

用公式表达每层：

\[ x \leftarrow x + \mathrm{Attn}(\mathrm{LN}(x)), \qquad x \leftarrow x + \mathrm{CrossAttn}(\mathrm{LN}(x),\ \mathrm{ctx}), \qquad x \leftarrow x + \mathrm{FFN}(\mathrm{LN}(x)) \]

注意残差连接让每层都是"增量修正"：初始零 token 的信息一路上只增不减。

`forward` 还支持 `context_list`：给每一层配不同的 context（例如不同尺度、不同来源的特征），长度必须等于层数，否则抛 `ValueError`。

#### 4.2.3 源码精读

**构造：每层三件套、各自套 PreNorm**。[models/smplx/pose_transformer.py:179-194](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L179-L194)：

```python
for _ in range(depth):
    sa = Attention(dim, heads=heads, dim_head=dim_head, dropout=dropout)
    ca = CrossAttention(dim, context_dim=context_dim, heads=heads, dim_head=dim_head, dropout=dropout)
    ff = FeedForward(dim, mlp_dim, dropout=dropout)
    self.layers.append(nn.ModuleList([PreNorm(dim, sa, ...), PreNorm(dim, ca, ...), PreNorm(dim, ff, ...)]))
```

**前向：三段残差 + 每层 context 分发**。[models/smplx/pose_transformer.py:196-206](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L196-L206)：

```python
def forward(self, x, *args, context=None, context_list=None):
    if context_list is None:
        context_list = [context] * len(self.layers)   # 单一 context 广播给所有层
    if len(context_list) != len(self.layers):
        raise ValueError(...)
    for i, (self_attn, cross_attn, ff) in enumerate(self.layers):  # 6 层 transformer
        x = self_attn(x, *args) + x
        x = cross_attn(x, *args, context=context_list[i], idx=i) + x
        x = ff(x, *args) + x
    return x
```

这段做了三件事：把外部传入的单个 context 复制成每层一份（[L197-L198](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L197-L198)）；校验 `context_list` 长度（[L199-L200](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L199-L200)）；按 自注意→交叉注意→FFN 三段残差推进（[L202-L205](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L202-L205)，行内注释「6 层 transformer」正对应配置 `depth: 6`）。源代码里源注释的 `# 6 层 transformer` 与 `configs/infer.yaml` 的 `depth: 6` 一一对应。

**调用点：head 怎么喂 context**。[models/smplx/smplx_head.py:257-267](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L257-L267)：

```python
x = einops.rearrange(x, 'b c h w -> b (h w) c')   # (B,1280,16,12) -> (B,192,1280)
token = x.new_zeros(B, 1, 1)                       # 零 token
token_out = self.transformer(token, context=x)     # (B,1,1024)
token_out = token_out.squeeze(1)                   # (B,1024)
```

特征图被摊平成 192 个 context token；零 token 经 6 层抽取后 `squeeze` 成向量，交给参数解码器（u3-l3 的主题）。

**参数账本**（按真实配置推算，可在实践中用 `sum(p.numel())` 核对）：

| 子模块 | 每层参数量 |
| --- | --- |
| `Attention`（to_qkv 1024→1536 + to_out 512→1024） | 2,098,176 |
| `CrossAttention`（to_kv 1280→1024、to_q 1024→512、to_out 512→1024） | 2,360,320 |
| `FeedForward`（两个 1024→1024） | 2,099,200 |
| 3 个 LayerNorm | 6,144 |
| **每层合计** | **6,563,840** |

6 层共约 3938 万，加上 token/位置嵌入约 3.9×10⁷——只有 ViT-Huge 骨干（u3-l1，约 6.3×10⁸）的 6% 左右。"重骨干、轻解码"正是这类回归式架构的典型配比。

#### 4.2.4 代码实践

**实践二：体验 `context_list` 的每层分发机制。**

1. 实践目标：验证 (a) 单一 context 会被广播到所有层；(b) 每层可用序列长度不同的 context；(c) 长度不匹配时报错。
2. 操作步骤（示例代码）：

   ```python
   # practice_u3l2_tca.py（示例代码）
   import torch
   from models.smplx.pose_transformer import TransformerCrossAttn

   tca = TransformerCrossAttn(dim=1024, depth=3, heads=8, dim_head=64,
                              mlp_dim=1024, context_dim=1280).eval()
   toks = torch.randn(1, 4, 1024)   # 4 个 token，便于观察自注意力不再退化

   # (a) 单一 context 广播到 3 层
   ctx = torch.randn(1, 128, 1280)
   print(tca(toks, context=ctx).shape)              # 预期 (1, 4, 1024)

   # (b) 每层不同 context，且序列长度可以不同
   ctx_list = [torch.randn(1, n, 1280) for n in (128, 96, 160)]
   print(tca(toks, context_list=ctx_list).shape)    # 预期 (1, 4, 1024)

   # (c) context_list 长度 != depth
   try:
       tca(toks, context_list=ctx_list[:2])
   except ValueError as e:
       print('[预期报错]', e)
   ```

3. 需要观察的现象：前两次调用都输出 `(1, 4, 1024)`——输出形状只由 token 数与 `dim` 决定，context 只影响数值不影响形状；第三次抛出 `ValueError`，报错信息就是 [L200](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L200) 拼出的 `len(context_list) != len(self.layers) (2 != 3)`。
4. 预期结果：如上；`4` 个 token 时自注意力是 4×4 的注意力矩阵，不再退化（可与实践一的单 token 输出对照）。具体报错文案待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`TransformerCrossAttn` 每层按什么顺序执行哪三个子层？哪个子层是图像信息进入 token 的唯一通道？

**答案**：PreNorm 自注意力残差 → PreNorm 交叉注意力残差（以图像 token 为 K/V）→ PreNorm FFN 残差。交叉注意力是唯一通道：自注意力只在 token 内部混合（且单 token 时退化），FFN 是逐点变换，都不接触图像特征。

**练习 2**：`context_list` 机制适合什么场景？PEAR 用了吗？

**答案**：适合每层看不同特征的做法，例如多尺度特征金字塔（浅层看高分辨率、深层看语义层）或图像+文本多源条件。PEAR 只调用 `self.transformer(token, context=x)` 传单一 context，由 [L198](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L198) 广播成 6 份相同的 context，属于"备而未用"的扩展点。

**练习 3**：解码头 FFN 的隐藏层是模型宽度的几倍？和骨干相比说明了什么？

**答案**：`mlp_dim=1024 = dim`，1 倍；骨干是 `mlp_ratio=4`（4 倍）。解码头参数量远小于骨干，信息加工的重心放在了骨干特征提取上，解码端只做轻量的逐点加工与参数回归。

### 4.3 TransformerDecoder.forward：零 token 嵌入、位置嵌入与 dropout 变体

#### 4.3.1 概念说明

`TransformerDecoder` = **入层三件套 + TransformerCrossAttn**：把输入 token 投影到模型宽度、（可选）做 embedding dropout、加位置编码，然后送进 4.2 的堆叠器。

三个概念逐一说明：

1. **零 token 嵌入**：输入是全零张量 `(B, 1, 1)`，经 `to_token_embedding = nn.Linear(1, 1024)` 投影。关键在于：线性层作用在 0 上，输出恒等于偏置向量 \( b \)——**与输入无关**。所以"零 token 的嵌入"实际上是一个完全可学习的常数向量（`bias + pos_embedding`），写"零输入"只是让它表现得像个参数。下一讲的 `set_smpl_init` 会看到，均值参数回归的思想在这里也有呼应：先把"默认人"作为出发点。
2. **位置嵌入**：`pos_embedding` 形状 `(1, num_tokens, dim)`，按 token 序号切片加到嵌入上。单 token 时它只是又一个可学习常数；多 token 时它为不同 token 提供身份区分。
3. **dropout 变体**：这里不是普通的 `nn.Dropout`（随机置零个别数值），而是两种**token 级**正则：
   - `DropTokenDropout`（"drop"）：以概率 p **整列删除** token，序列长度变短；
   - `ZeroTokenDropout`（"zero"）：以概率 p 把某些 token 的嵌入**整体置零**，序列长度不变。

为什么需要 token 级 dropout？对多 token 输入（如 `TransformerEncoder` 处理一组关节 token），drop 掉整个 token 可以防止模型过度依赖个别输入位置，类似特征级的 DropBlock。而对只有 1 个 token 的 PEAR，删掉唯一 token 会让序列清空，因此更安全的做法是置零——不过 PEAR 实际配置 `emb_dropout: 0.0`，两种都没启用（见 4.3.5 练习 3）。

#### 4.3.2 核心流程

```
inp: (B, num_tokens, token_dim=1)     # PEAR: 全零, num_tokens=1
  │ to_token_embedding: Linear(1, 1024)
  ▼
x: (B, n, 1024)                       # 零输入 ⇒ 恒等于 bias 向量
  │ dropout(x)                        # token 级 dropout（p=0 时恒等）
  ▼
x = x + pos_embedding[:, :n]          # 按实际 n 切片加位置编码
  │ TransformerCrossAttn(x, context)  # 4.2 的 6 层堆叠
  ▼
out: (B, n, 1024)
```

注意 [L359](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L359) 的 `pos_embedding[:, :n]`：运行时的 n 可以**小于**构造时的 `num_tokens`（只用前 n 个位置编码），但不能更大（广播失败）。这给了同一模块处理变长 token 序列的灵活性。

#### 4.3.3 源码精读

**构造参数从哪来**。[models/smplx/smplx_head.py:115-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L115-L125) 先写死三个参数，再用 HEAD 配置覆盖：

```python
self.input_is_mean_shape = False
transformer_args = {
    'num_tokens': 1,
    'token_dim': (n_poses + n_betas + n_cam + n_expression) if self.input_is_mean_shape else 1,
    'dim': 1024,
}
transformer_args.update(OmegaConf.to_container(self.cfg, resolve=True))  # cfg = cfg.HEAD
self.transformer = TransformerDecoder(**transformer_args)
```

由于 `input_is_mean_shape=False`，`token_dim=1`；`configs/infer.yaml` 的 [HEAD 段（L23-L31）](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L23-L31) 提供 `context_dim=1280、depth=6、dim_head=64、dropout=0.0、emb_dropout=0.0、heads=8、mlp_dim=1024、norm='layer'`，且**不含** `dim`，所以 `dim=1024` 保留。最终生效参数：`num_tokens=1, token_dim=1, dim=1024, context_dim=1280, depth=6, heads=8, dim_head=64, mlp_dim=1024, dropout=0, emb_dropout=0, emb_dropout_type='drop'`。

**TransformerDecoder 构造**。[models/smplx/pose_transformer.py:325-352](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L325-L352)：

```python
if not skip_token_embedding:
    self.to_token_embedding = nn.Linear(token_dim, dim)     # Linear(1, 1024)
self.pos_embedding = nn.Parameter(torch.randn(1, num_tokens, dim))
if emb_dropout_type == "drop":
    self.dropout = DropTokenDropout(emb_dropout)
elif emb_dropout_type == "zero":
    self.dropout = ZeroTokenDropout(emb_dropout)
elif emb_dropout_type == "normal":
    self.dropout = nn.Dropout(emb_dropout)
self.transformer = TransformerCrossAttn(dim, depth, heads, dim_head, mlp_dim, dropout,
                                        norm=norm, norm_cond_dim=norm_cond_dim,
                                        context_dim=context_dim)
```

**forward：入层四步**。[models/smplx/pose_transformer.py:354-362](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L354-L362)：

```python
def forward(self, inp, *args, context=None, context_list=None):
    x = self.to_token_embedding(inp)      # 零输入 ⇒ 输出恒为 bias
    b, n, _ = x.shape
    x = self.dropout(x)
    x += self.pos_embedding[:, :n]        # [1,1,1024]
    x = self.transformer(x, *args, context=context, context_list=context_list)
    return x
```

行尾源码注释 `# [1,1,1024]` 印证了 PEAR 的实际形状。

**DropTokenDropout：整列删除、batch 共享掩码**。[models/smplx/pose_transformer.py:218-225](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L218-L225)：

```python
if self.training and self.p > 0:
    zero_mask = torch.full_like(x[0, :, 0], self.p).bernoulli().bool()  # 形状 (n,)
    if zero_mask.any():
        x = x[:, ~zero_mask, :]           # 序列变短
```

掩码由 `x[0, :, 0]` 生成，形状只含 token 维，**整个 batch 共享同一张丢弃掩码**；[L222](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L222) 的 TODO 注释表明作者想改成逐样本独立掩码但未做。

**ZeroTokenDropout：原位置零、逐样本掩码、原地赋值**。[models/smplx/pose_transformer.py:237-243](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L237-L243)：

```python
if self.training and self.p > 0:
    zero_mask = torch.full_like(x[:, :, 0], self.p).bernoulli().bool()  # 形状 (b, n)
    x[zero_mask, :] = 0                   # 序列长度不变；原地修改
```

掩码形状是 `(b, n)`，逐样本独立；`x[mask, :] = 0` 是**原地写**——在 `TransformerDecoder` 里它作用在 `to_token_embedding` 刚产出的新张量上，是安全的；但在 `TransformerEncoder` 的 `emb_dropout_loc == "input"` 分支（[L291-L292](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L291-L292)）它会直接改写调用方传入的 `inp`，是一个值得警惕的副作用（练习 3 会用到）。

**零 token 在哪诞生**：[models/smplx/smplx_head.py:260-262](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L260-L262) 的 `token = x.new_zeros(B, 1, 1)`，随后 [L266](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L266) 以图像特征为 context 调用本模块。

#### 4.3.4 代码实践

**实践三（本讲主实践）：用 HEAD 配置实例化 TransformerDecoder，跑通 forward，再改 `num_tokens`。**

1. 实践目标：
   - 用真实 HEAD 配置构建 `TransformerDecoder`，跑通一次 forward；
   - 亲身体验 `context_dim=1280` 这个硬约束（先踩坑再修正）；
   - 观察 `num_tokens=4` 时位置嵌入与输出形状的变化；
   - 数一数解码器参数量，与骨干对比。

2. 操作步骤（示例代码，仓库根目录运行，CPU 即可）：

   ```python
   # practice_u3l2_decoder.py（示例代码）
   import torch
   from omegaconf import OmegaConf
   from utils.general_utils import ConfigDict, add_extra_cfgs
   from models.smplx.pose_transformer import TransformerDecoder

   # 1) 读配置（沿用 u2-l1 的方式）
   meta_cfg = ConfigDict(model_config_path='configs/infer.yaml')
   meta_cfg = add_extra_cfgs(meta_cfg)

   # 2) 复刻 smplx_head.py L117-L125 的构造方式
   transformer_args = {'num_tokens': 1, 'token_dim': 1, 'dim': 1024}
   transformer_args.update(OmegaConf.to_container(meta_cfg.HEAD, resolve=True))
   print('构造参数 =', transformer_args)
   dec = TransformerDecoder(**transformer_args).eval()
   print('参数量 =', sum(p.numel() for p in dec.parameters()))

   # 3) 踩坑：context 最后一维给 1024 行吗？
   ctx_wrong = torch.randn(1, 128, 1024)
   try:
       dec(torch.zeros(1, 1, 1), context=ctx_wrong)
   except RuntimeError as e:
       print('[预期报错] to_kv 的 Linear(1280,1024) 吃到 1024 维输入：', e)

   # 4) 正确形状：最后一维必须是 context_dim=1280
   ctx = torch.randn(1, 128, 1280)          # 真实推理时是 (1,192,1280)
   out = dec(torch.zeros(1, 1, 1), context=ctx)
   print('num_tokens=1 输出 =', out.shape)   # 预期 (1, 1, 1024)

   # 5) 零 token 的"初始向量"是可学习常数：bias + pos_embedding
   init_vec = dec.to_token_embedding(torch.zeros(1, 1, 1)) + dec.pos_embedding
   print('初始查询向量范数 =', init_vec.norm().item())

   # 6) num_tokens=4：位置嵌入与输出同步变化
   dec4 = TransformerDecoder(**{**transformer_args, 'num_tokens': 4}).eval()
   print('pos_embedding =', tuple(dec4.pos_embedding.shape))     # 预期 (1, 4, 1024)
   out4 = dec4(torch.zeros(1, 4, 1), context=ctx)
   print('num_tokens=4 输出 =', out4.shape)                      # 预期 (1, 4, 1024)

   # 7) 变长输入：n 可以小于 num_tokens（切片加位置编码）
   out2 = dec4(torch.zeros(1, 2, 1), context=ctx)
   print('n=2 输入输出 =', out2.shape)                           # 预期 (1, 2, 1024)
   ```

3. 需要观察的现象：
   - 第 3 步抛 `RuntimeError`——`to_kv` 是按 `context_dim=1280` 建的 `nn.Linear(1280, 1024, bias=False)`，输入最后一维 1024 无法与之相乘（u2-l5 已知骨干输出 1280 通道，这里的形状约束正来自下游 192 个 1280 维图像 token）；
   - 第 4、6 步输出形状分别为 `(1,1,1024)`、`(1,4,1024)`——输出 token 数恒等于输入 token 数；
   - `pos_embedding` 随 `num_tokens` 从 `(1,1,1024)` 变为 `(1,4,1024)`；
   - 第 7 步 `n=2 < num_tokens=4` 仍能运行（只用前 2 个位置编码）。
4. 预期结果：如上；`sum(p.numel())` 应约为 39,386,112（4.2.3 的账本），与骨干约 6.3×10⁸ 对比悬殊。报错的具体文案、参数量精确值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：输入恒为全零时，`nn.Linear(1, 1024)` 的输出是什么？"零 token 的嵌入"究竟是什么东西？

**答案**：线性层输出 \( W \cdot 0 + b = b \)，恒等于偏置向量，与输入无关。所以零 token 的嵌入实际是可学习常数 `to_token_embedding.bias + pos_embedding[0]`，"零输入"只是让它表现得像一个 `nn.Parameter`。第 5 步打印的初始查询向量正是这两者之和。

**练习 2**：`DropTokenDropout` 和 `ZeroTokenDropout` 在"序列长度、掩码粒度、实现方式"三方面有何区别？

**答案**：① 长度：drop 会删掉整列 token 使 \( n \) 变小，zero 长度不变；② 掩码：drop 的掩码形状是 `(n,)`、整个 batch 共享（且有 TODO 想改成逐样本），zero 的掩码形状是 `(b, n)`、逐样本独立；③ 实现：drop 用布尔索引取子集（非原地），zero 用 `x[mask, :] = 0` 原地写回。

**练习 3**：PEAR 推理时这两种 dropout 会生效吗？如果训练时把 `emb_dropout` 设成 0.5 且用默认的 `emb_dropout_type='drop'`，会发生什么？

**答案**：不会生效——配置 `emb_dropout: 0.0`，两个变体的第一道判断都是 `self.training and self.p > 0`，p=0 直接恒等（顺带一提，推理脚本也没有显式 `.eval()`，但因 p=0、`dropout=0.0`，训练/评估模式在这里等价）。若 p=0.5 且用 drop：唯一的 token 有 50% 概率被整列删除，序列长度变 0；按 [smplx_head.py:267](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L267) 的 `token_out.squeeze(1)`（长度 0 不会被 squeeze）与后续解码器推断，链路将得到空张量而无法训练——单 token 场景应选 `emb_dropout_type='zero'`（此为机制推导，具体报错位置待本地验证）。

## 5. 综合实践

**任务：可视化零 token 在 6 层 cross-attention 中分别"看"了图像的哪里。**

前三个实践用的是假 context；现在把真模型、真图片接上，把 6 层的注意力权重取出来，叠回输入图。作者留在 [pose_transformer.py:120-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L120-L125) 的注释代码用的正是同一个工具函数。

前置条件：u1-l2 的 `pear` 环境、u1-l2 的模型资产、可联网下载权重、一块 GPU（u2-l5 已说明前向依赖 CUDA）。

1. 实践目标：把"零 token → 6 层 cross-attention → 192 个图像 token"这条抽象链路变成 6 张肉眼可见的热图。

2. 操作步骤（示例代码）：

   ```python
   # practice_u3l2_attnmap.py（示例代码，仓库根目录运行）
   import torch, cv2
   from huggingface_hub import hf_hub_download
   from models.pipeline.ehm_pipeline import Ehm_Pipeline
   from utils.general_utils import ConfigDict, add_extra_cfgs
   from utils.pipeline_utils import to_tensor
   from utils.graphics import overlay_attention_on_image
   from inference_wo_detect import pad_and_resize   # 复用现成的预处理

   meta_cfg = ConfigDict(model_config_path='configs/infer.yaml')
   meta_cfg = add_extra_cfgs(meta_cfg)

   ckpt = hf_hub_download(repo_id='BestWJH/PEAR_models', filename='pear_model.pt', repo_type='model')
   ehm_model = Ehm_Pipeline(meta_cfg)
   state = torch.load(ckpt, map_location='cpu', weights_only=True)
   ehm_model.backbone.load_state_dict(state['backbone'], strict=False)
   ehm_model.head.load_state_dict(state['head'], strict=False)
   ehm_model = ehm_model.cuda().eval()

   # 在 6 层 CrossAttention 的 softmax 上挂钩子，抓注意力权重
   attn_bank = {}
   for i in range(6):   # head.transformer=TransformerDecoder, .transformer=TransformerCrossAttn
       sm = ehm_model.head.transformer.transformer.layers[i][1].fn.attend
       sm.register_forward_hook(
           lambda mod, inp, out, i=i: attn_bank.setdefault(i, out.detach().cpu()))

   img = cv2.imread('example/images/000000.jpg')       # 任取 example 下一张图
   resized = pad_and_resize(img, target_size=256)
   img_patch = torch.permute(to_tensor(resized, 'cuda:0') / 255, (2, 0, 1)).unsqueeze(0)

   # 把网络真正看到的 256×192 裁剪区存盘（BGR 顺序往返，颜色保持一致）
   crop = img_patch[0, :, :, 32:-32]
   cv2.imwrite('input_crop.jpg',
               (crop.permute(1, 2, 0).cpu().numpy() * 255).astype('uint8'))

   with torch.no_grad():
       ehm_model(img_patch)                            # 只需前向，无需 EHM/渲染器

   for i, attn in attn_bank.items():                   # attn: (1, 8, 1, 192)
       print(f'layer {i}: attn {tuple(attn.shape)}, 行和={attn.sum(-1).mean():.4f}')
       overlay_attention_on_image('input_crop.jpg', attn,
                                  save_path=f'attn_layer{i}.png')
   ```

3. 需要观察的现象：每层 `attn` 形状为 `(1, 8, 1, 192)`——1 个查询、8 个头、192 个图像 token，softmax 行和恒为 1；`overlay_attention_on_image`（[utils/graphics.py:21-90](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics.py#L21-L90)）默认 `patch_h=16, patch_w=12`，恰好把 192 个权重还原成 ViT 的 patch 网格再上采样叠图。
4. 预期结果：得到 `input_crop.jpg` 与 6 张 `attn_layer*.png`；对比各层热图，观察注意力集中在人体哪些部位、随层数加深如何收窄或转移（具体分布待本地验证——这正是原作者注释掉的调试代码想回答的问题）。
5. 思考延伸：热图是 8 个头平均后的结果（工具函数第 46-47 行 `attn[0,:,0,:].mean(dim=0)`）。若想看单头差异，可把 `attn[0, h, 0, :]` 单独传入并自行 `.view(16, 12)`。

## 6. 本讲小结

- **零 token 是根查询向量**：输入恒为零使 `Linear(1,1024)` 退化为偏置，"零 token 的嵌入"实为可学习常数 `bias + pos_embedding`；它经 6 层迭代从图像 token 中抽取信息。
- **cross-attention 是唯一信息入口**：单 token 的自注意力 softmax 恒为 1、退化为逐点变换；图像特征只经 `to_kv`（1280→512）这条侧路流入 token（`to_q`：1024→512）。
- **层结构**：`TransformerCrossAttn` 每层做 PreNorm 自注意力 → PreNorm 交叉注意力 → PreNorm FFN 三段残差，`depth=6`；FFN 等宽（`mlp_dim=dim=1024`），与骨干的 4 倍扩张形成对比。
- **形状契约**：输出 token 数恒等于输入 token 数；context 最后一维必须等于 `context_dim=1280`，token 数（192）则可变；`pos_embedding[:, :n]` 支持 n 小于构造时的 `num_tokens`。
- **两种 token 级 dropout**：`DropTokenDropout` 整列删 token、batch 共享掩码；`ZeroTokenDropout` 原位置零、逐样本掩码、原地赋值。PEAR `emb_dropout=0.0` 两者皆未启用。
- **体量对比**：解码器约 3.9×10⁷ 参数（每层约 656 万），仅约为 ViT-Huge 骨干的 6%——重骨干、轻解码。

## 7. 下一步学习建议

token 到此已就绪：`(B, 1024)` 的向量正等待被翻译成人体参数。下一讲 **u3-l3 SMPLXTransformerDecoderHead：参数解码器全家桶** 将精读 [smplx_head.py:130-147](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L130-L147) 的 9 个线性解码器（`smplx_poses_decoder` 312 维、`flame_shape_decoder` 300 维、`cam_decoder` 3 维等）、`rot6d_to_rotmat` 六维旋转表示，以及 `set_smpl_init` 的均值参数初始化——那正是本讲"零 token 常数起点"思想在参数空间的呼应。

建议提前浏览的源码：[smplx_head.py:255-319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L255-L319)（forward 后半段），并留意本讲输出 `token_out.squeeze(1)` 在其中被每个解码器重复使用的方式。
