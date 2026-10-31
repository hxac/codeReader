# u4-l4 UnifiedQueryDecoder：CLIP 词表分类与 mask pooling

## 1. 本讲目标

上一讲（u4-l3）我们读完了 `QueryDecoder` 的骨架：超点即查询、掩码注意力、迭代预测，并在结尾留下一个悬念——「分类改走球面分类路径，语义分支仅在最后一层计算」。本讲就把这个悬念拆开：

1. 理解 `sphere_cls`（球面分类）如何用**外部文本原型**替代传统线性分类头，以及这对开放词汇分割的意义。
2. 掌握 `mask_pool` 的 einsum 归一化池化：如何从「预测掩码内部的点特征」汇取出每个查询的分类特征。
3. 读懂 `forward_logit` 中 max-over-prototypes 与 `logit_scale` 的作用。
4. 区分实例分类与语义分类**两套投影头**（`cls_proj` / `cls_proj_sem`、`mask_pooling_proj` / `mask_pooling_proj_sem`），以及 `last_flag` 控制的语义分支时机。

## 2. 前置知识

### 2.1 线性分类头 vs 原型分类

传统分割模型的分类头是一个可学习矩阵：

\[ \text{logits} = W q + b, \quad W \in \mathbb{R}^{(K+1) \times d} \]

它有两个特点：**权重是训练学出来的**、**类别数 K 在建模时就钉死了**。想新增一个类别，就必须改网络、重新训练。

原型（prototype）分类换了一种思路：每个类别的「权重向量」不由训练得到，而是用**文本编码器**（这里是 OpenCLIP）对一个类别名的文本嵌入来充当。分类变成计算查询嵌入与各类原型的**余弦相似度**：

\[ \cos(e, c) = \frac{\langle e, c \rangle}{\|e\| \, \|c\|} \]

由于归一化后的向量都落在单位超球面上，这种分类常被称为**球面分类**（sphere classification）。它正是 CLIP 做零样本分类的方式——想换类别，只需换一组文本嵌入，网络结构不动。这就是 `sphere_cls=True` 的含义，也是 UniSeg3D 开放词汇能力（u6-l3）与文本-视觉对齐（u4-l5、u5-l6）的基石。

### 2.2 mask pooling（掩码池化）

分类不能只看查询向量本身——两个「椅子」查询和两个「桌子」查询的向量可能很接近，但它们**掩住的点**截然不同。mask pooling 的做法是：用预测掩码圈出点（这里是超点）特征，在掩码内做**平均池化**，得到「这个查询掩住了什么东西」的特征，再与查询向量相加后送去分类。这一思想来自 Mask2Former 等二阶段掩码分类模型。

### 2.3 需要的背景概念

- **buffer（`register_buffer`）**：PyTorch 中随模型保存/移动但不参与梯度更新的张量；`persistent=False` 表示**不写入 state_dict**（checkpoint）。
- **DDP 广播（`dist.broadcast`）**：多卡训练时把 rank 0 的张量广播到所有进程，保证各卡的常量完全一致。
- **einsum**：用下标字符串描述张量运算，如 `'nd,md->nm'` 表示两个矩阵相乘。
- 承接 u4-l3：解码器每层输出 `pred_mask = einsum('nd,md->nm', norm_query, mask_feats)`，本讲的池化与分类都建立在这份掩码之上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py) | 本讲主角。`UnifiedQueryDecoder`（L372 起）继承 `QueryDecoder`；含原型加载（L389-L432）、`forward_logit`（L433-L440）、改造版 `_forward_head`（L442-L502）。文件顶部 L11-L27 还有一份内嵌的 `mask_pool` |
| [uniseg3d/mask_pool.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_pool.py) | 独立的 `mask_pool` 函数（与 query_decoder.py 内嵌那份逐字相同），只依赖 torch，便于单独复用与测试 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | decoder 配置块 L47-L66：`sphere_cls=True`、原型文件路径、类别数；`stuff_classes=[0,1]`（L136）对应实例分类时被丢弃的前两类 |
| [uniseg3d/semantic_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py) | 语义损失如何消费 `sem_preds`（仅一句带过，细节在 u5-l1） |
| [README.md](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md) | L124-L126：`scannet_cls_embedding.pth` 的下载方式与来源说明 |

辅助背景：`data/scannet/scannet_cls_embedding.pth` 是**从官方 Release 下载**的类别原型文件，仓库内没有生成它的脚本（u1-l4 已确认）。

## 4. 核心概念与源码讲解

### 4.1 cls_embed：类别原型加载与 back_token 广播

#### 4.1.1 概念说明

球面分类需要一个「类别 → 原型向量」的查找表。UniSeg3D 把这张表做成了一个三维张量文件 `data/scannet/scannet_cls_embedding.pth`，其布局为：

\[ \text{cls\_embed} \in \mathbb{R}^{C \times P \times D} \]

- \( C \)：类别数。由代码与配置联立可推出 **C = 20**（20 个 ScanNet 语义类，详见 4.1.2）。
- \( P \)：每个类别的**原型个数**。一个类可以用多条文本模板（如 "a chair" / "a seat with legs"）各编码一个原型，推理时取最大相似度——代码不关心 P 具体是多少，直接 `size(1)` 读出（具体数值待本地验证）。
- \( D \)：CLIP 文本嵌入维度。原型与语言分支（u4-l5）用同一个 OpenCLIP 模型编码（README L126），`convnext_large_d_320` 的文本维是 768，但确切值以文件为准（待本地验证）。

模型侧还要补一个**背景类**（back_token）：一个全零原型，充当「我不是任何东西」的第 \(C+1\) 类。这一步在 `__init__` 里完成，并把结果注册为 buffer。

#### 4.1.2 核心流程

```text
torch.load(pth)                          # (C, P, D)
读取 _dim=D、_prototypes=P
rank0 造全零 back_token(1, D)，其余 rank 造空张量
dist.broadcast 对齐所有卡                  # 保证背景原型各卡一致
back_token.repeat(P, 1)[None]            # (1, P, D)
cat 到类别维                             # (C+1, P, D)
permute(2, 0, 1)                         # (D, C+1, P) ← 方便 einsum
register_buffer('cls_embed', persistent=False)
```

两个关键推论（由 `forward_logit` 的切片反推，4.3 详述）：

- **语义分类**用全部 \(C+1\) 行 → logits 有 \(C+1 = 21\) 列 = `num_semantic_classes(20) + 1`，与配置 [configs/...py:L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L11) 一致；
- **实例分类**丢弃前 2 行（wall=0、floor=1 两个 stuff 类，对应配置 `stuff_classes=[0, 1]`）→ \(C-1 = 19\) 列 = `num_instance_classes(18) + 1`（18 个 thing 类 + 背景），与 [configs/...py:L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L10) 一致。

因此 **C 必为 20**——这正是「语义类去掉 2 个 stuff 类 = 实例类」的类别体系。

#### 4.1.3 源码精读

配置侧打开球面分类并指定原型文件（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L47-L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L47-L66)）：解码器为 `UnifiedQueryDecoder`，`sphere_cls = True`，`vocabulary_cls_embedding_path = 'data/scannet/scannet_cls_embedding.pth'`。

原型加载与 back_token 广播（[uniseg3d/query_decoder.py:L389-L406](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L389-L406)）：

```python
if self.sphere_cls:
    rank, world_size = get_dist_info()
    cls_embed = torch.load(vocabulary_cls_embedding_path)

    _dim = cls_embed.size(2)        # D
    _prototypes = cls_embed.size(1) # P

    if rank == 0:
        back_token = torch.zeros(1, _dim, dtype=torch.float32, device='cuda')
    else:
        back_token = torch.empty(1, _dim, dtype=torch.float32, device='cuda')
    if world_size > 1:
        dist.broadcast(back_token, src=0)
    back_token = back_token.to(device='cpu')
    cls_embed = torch.cat([
        cls_embed, back_token.repeat(_prototypes, 1)[None]
    ], dim=0)
    self.register_buffer('cls_embed', cls_embed.permute(2, 0, 1).contiguous(),
                         persistent=False)
```

这段代码做了三件事：

1. **加载原型**并读出 \(P\)、\(D\)；`back_token.repeat(_prototypes, 1)[None]` 把 1 个零向量复制成 (1, P, D)，即背景类也有 P 个（全零）原型，与其它类形状对齐。
2. **DDP 安全**：rank 0 造零、其余 rank 造未初始化内存、广播对齐——确保多卡训练时背景原型 bit 级一致。注意 `device='cuda'`：**在没有 GPU 的机器上无法实例化 `UnifiedQueryDecoder(sphere_cls=True)`**，做实验时要么用 GPU，要么像本讲实践那样手工复现流水线。
3. **`persistent=False`**：`cls_embed` 不进 checkpoint。好处是换词表不用改权重文件；代价是**任何构建模型的环境（包括纯推理）都必须存在这个 pth 文件**，否则 `torch.load` 直接报错。

两个线性分类头在球面模式下**根本不会被创建**（[uniseg3d/query_decoder.py:L210-L213](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L210-L213) 与 [L380-L387](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L380-L387)）：`out_cls` 与 `out_sem` 都被 `if not self.sphere_cls` 守卫。所以球面分类不是「并列选项」，而是**完全替换**了分类参数——模型里不存在 \(19 \times 256\) 或 \(21 \times 256\) 的可学习分类矩阵。

#### 4.1.4 代码实践

**实践目标**：亲手「体检」原型文件，验证 4.1.2 推出的三个维度与归一化假设。

**操作步骤**（在按 u1-l2 装好的环境中，项目根目录执行）：

```python
# 示例代码：inspect_prototypes.py
import torch
cls_embed = torch.load('data/scannet/scannet_cls_embedding.pth')
print('形状:', cls_embed.shape)          # 期望 (20, P, D)
print('dtype:', cls_embed.dtype)
print('每类原型的 L2 范数（按最后一维）:',
      cls_embed.norm(dim=2).mean().item())
```

**需要观察的现象**：

1. 第一维是否为 20；第三维是否为 768（与 `LangModule` 的 `input_channels=768` 对齐）。
2. 原型范数均值是否接近 1.0——`forward_logit` 只对查询侧做了 `F.normalize`，原型侧默认文件出厂时已归一化（README 称 "Processed ... embeddings"），范数 ≈ 1 即验证了这一点。

**预期结果**：形状 (20, P, 768)、范数均值 ≈ 1.0。**文件未下载或数值待本地验证**（P 的具体值无法从源码推出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cls_embed` 要 `permute(2, 0, 1)` 存成 (D, C+1, P)，而不是保持 (C+1, P, D)？

**答案**：为了与 `forward_logit` 里的 `einsum('nd,dkp->nkp', ...)` 下标约定对齐——einsum 要求参与运算的维度按下标出现，把嵌入维 D 放到 dim0 后，查询嵌入 (n, d) 可以直接与原型 (d, k, p) 相乘，不需要额外转置；同时实例分类的「丢掉前两类」变成一个直观的 `[:, 2:, :]` 切片。

**练习 2**：删掉 `data/scannet/scannet_cls_embedding.pth` 后加载一个训练好的 checkpoint 做推理，会发生什么？为什么？

**答案**：会在**构建模型阶段**就报错（`torch.load` 文件不存在），根本轮不到加载 checkpoint。因为 `persistent=False`，原型不随 checkpoint 保存，每次实例化 `UnifiedQueryDecoder` 都要重新读 pth——推理环境同样依赖该文件。

**练习 3**：back_token 为什么要在 rank 0 上 `zeros`、其它 rank 上 `empty` 再广播，而不是每张卡各自 `zeros`？

**答案**：这是多进程训练中的防御性写法。虽然 `zeros` 本身是确定性的，但该模式能确保「所有 rank 上的 buffer 来自同一次初始化」，杜绝任何 rank 间数值不一致的可能（这类写法常见于需要随机初始化再对齐的常量场景）；广播后统一搬回 CPU 参与后续 `torch.cat`。

### 4.2 mask_pool 与两个池化投影头

#### 4.2.1 概念说明

`mask_pool` 回答的问题是：**「第 n 个查询掩住的区域长什么样？」**

输入是点（超点）特征矩阵 \(X \in \mathbb{R}^{D_{in} \times M}\)（\(D_{in}=256=d\_model\)，\(M\) 为该场景超点数）和掩码 logits \(M_{logit} \in \mathbb{R}^{N \times M}\)（\(N\) 为查询数）。它先把 logits 二值化（>0 即视为掩内），再做**归一化平均**：

\[ x_n^{pool} = \frac{\sum_{m} \mathbb{1}[M_{logit,n,m} > 0] \cdot X_{:,m}}{\sum_{m} \mathbb{1}[M_{logit,n,m} > 0] + 10^{-8}} \in \mathbb{R}^{D_{in}} \]

分母加 `1e-8` 防止「掩码全空」的查询除零。两个工程细节值得注意：

- **掩码侧梯度被切断**：`with torch.no_grad()` + `mask.detach()`，池化只把掩码当「区域选择器」，梯度只回流到点特征 \(X\)（即 `mask_feats`）上。掩码 logits 自己的监督来自掩码损失（u5）。
- **二值化阈值是 0（logit）而非 0.5（概率）**：等价于对 sigmoid 概率取 0.5 阈值，与解码器里生成注意力掩码的阈值一致（u4-l3）。

池化结果随后经**两套对称的投影头**加工：

| 头 | 结构 | 用途 |
| --- | --- | --- |
| `mask_pooling_proj` | LayerNorm + Linear(256→256) | 实例分类分支的池化投影 |
| `mask_pooling_proj_sem` | LayerNorm + Linear(256→256) | 语义分类分支的池化投影（独立参数） |
| `cls_proj` | Linear-ReLU ×2 + Linear(256→D) | 池化特征+查询 → 实例分类嵌入 |
| `cls_proj_sem` | Linear-ReLU ×2 + Linear(256→D) | 池化特征+查询 → 语义分类嵌入 |

实例与语义**各自一套完整参数**，互不共享——因为两个任务的类别空间与判别习惯不同（实例要区分「哪把椅子」，语义只需「是椅子」）。

#### 4.2.2 核心流程

```text
mask_feats[i]                # (M, 256) 点(超点)特征
        │ .T
        ▼
mask_pool(x=(256, M),        mask=pred_mask.detach() (N, M)
        │  内部: logits>0 二值化 → 行归一化 → einsum('dm,nm->nd')
        ▼
maskpool_ (N, 256)           # 每个查询「掩住区域」的平均特征
        ├── mask_pooling_proj     ──┐
        └── mask_pooling_proj_sem ──┤（同一份 maskpool_ 喂两个头）
                                    ▼
              各自 + norm_query → cls_proj / cls_proj_sem → 球面分类
```

注意一个容易被忽略的点：**语义分支复用的是同一份 `maskpool_`**（来自实例掩码的池化），只是后续投影不同。

#### 4.2.3 源码精读

`mask_pool` 函数（[uniseg3d/mask_pool.py:L5-L21](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_pool.py#L5-L21)）：

```python
def mask_pool(x, mask):
    """
    Args:
        x: [D, M]
        mask: [N, M]
    """
    with torch.no_grad():
        mask = mask.detach()
        mask = (mask > 0).to(mask.dtype)
        denorm = mask.sum(dim=(-1), keepdim=True) + 1e-8

    mask_pooled_x = torch.einsum(
        "dm,nm->nd",
        x,
        mask / denorm,
    )
    return mask_pooled_x
```

`no_grad` 块只包住掩码处理；`einsum` 在块外，因此梯度经 `x` 正常回传、经 `mask` 断流。`mask / denorm` 让每行权重和为 1（掩内点的平均）。

四个投影头与池化函数的组装（[uniseg3d/query_decoder.py:L408-L432](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L408-L432)）：

```python
cls_embed_dim = self.cls_embed.size(0)          # = D，原型嵌入维
self.cls_proj = nn.Sequential(                  # 实例分类嵌入
    nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
    nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
    nn.Linear(d_model, cls_embed_dim))
self.cls_proj_sem = nn.Sequential(...)          # 同构，语义版

self.mask_pooling = mask_pool                   # 函数别名
self.mask_pooling_proj = nn.Sequential(         # 实例池化投影
    nn.LayerNorm(d_model), nn.Linear(d_model, d_model))
self.mask_pooling_proj_sem = nn.Sequential(...) # 语义池化投影
```

两点说明：

1. `cls_proj` 的输出维是 `cls_embed_dim = self.cls_embed.size(0)`，即 permute 后的 dim0 = \(D\)。**投影头把 256 维查询空间升维到 CLIP 文本空间**，与原型做相似度——这正是「把视觉查询翻译到语言空间」的接口。
2. `self.mask_pooling = mask_pool` 里的 `mask_pool` 是 **query_decoder.py 自己内嵌的那份**（[uniseg3d/query_decoder.py:L11-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L11-L27)），与 mask_pool.py 逐字相同但互不引用；`uniseg3d/__init__.py` 也不导入 mask_pool.py，所以它是一份「独立工具副本」，恰好方便我们单独测试。

#### 4.2.4 代码实践

**实践目标**：脱离整个模型，单独验证 `mask_pool` 的数值行为与梯度断流。

**操作步骤**（只需 torch，不必装 mmdet3d；示例代码）：

```python
# mp_test.py —— 从文件直载，避免触发 uniseg3d/__init__.py 的重型依赖
import importlib.util, torch
spec = importlib.util.spec_from_file_location('mp', 'uniseg3d/mask_pool.py')
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)

torch.manual_seed(0)
x = torch.randn(4, 6, requires_grad=True)   # D=4, M=6 点特征
mask = torch.randn(2, 6, requires_grad=True)  # N=2 查询的掩码 logits
out = mp.mask_pool(x, mask)                 # (2, 4)

# 手工对照第 0 行
inside = (mask[0] > 0)
manual = x[:, inside].mean(dim=1)
print(out[0], '\n', manual)                 # 应一致（误差 < 1e-6）
print('inside 点数:', inside.sum().item(),
      '全空掩码行是否有限:', torch.isfinite(mp.mask_pool(
          x, torch.full((1, 6), -10.0))).all().item())
```

**需要观察的现象**：`out[0]` 与手工按掩内下标求均值完全一致；全负 logits（掩码空）的输出为有限值（全 0，来自 `1e-8` 分母）。

**预期结果**：数值一致、无 NaN/Inf。此脚本只需 torch，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mask_pool` 要对 mask 做 `detach`，而不让分类损失也顺便监督掩码？

**答案**：让分类梯度直接改掩码会造成训练不稳定——分类头会「教」掩码去圈住容易分类的区域而非真实物体边界，与掩码损失（BCE/Dice，u5）的目标冲突。detach 后掩码只作二值区域选择器，分类损失监督的是「如何在给定区域内聚合特征」（mask_feats 与投影头），各损失职责清晰。

**练习 2**：`mask_pooling_proj` 里为什么先 `LayerNorm` 再 `Linear`，而不是像 `cls_proj` 那样用 ReLU？

**答案**：池化输出是掩内特征的**加权平均**，量纲由 mask_feats 决定且随掩码大小波动；LayerNorm 先把它拉回稳定分布再仿射，稳住后续与 `norm_query`（本身经过 `out_norm`）相加时的尺度。`cls_proj` 处理的输入已是归一化向量之和，用 ReLU 增加非线性即可。（这是合理的工程解读；原文未附注释，属源码阅读推断。）

**练习 3**：一个查询的掩码 logits 全为负时，`mask_pool` 返回什么？这个结果送进 `cls_proj` 后会发生什么？

**答案**：二值掩码全 0，`einsum` 结果为全零向量（分母 1e-8）。全零池化特征 + `norm_query` 后仍可分类，即「这个查询没掩住任何点」时分类完全退化为只看查询向量本身——深监督下这类查询通常会被背景原型（相似度 0）压制成低置信度。

### 4.3 forward_logit：相似度分类（max-over-prototypes 与 logit_scale）

#### 4.3.1 概念说明

`forward_logit` 是球面分类的「最后一步」：把投影到 CLIP 空间的查询嵌入 \(e_n \in \mathbb{R}^{D}\) 与原型张量 \(\text{cls\_embed} \in \mathbb{R}^{D \times (C+1) \times P}\) 做相似度，输出每个查询对每个类的 logit：

\[ \text{logit}_{n,k} = \tau \cdot \max_{p=1..P} \; \langle \hat{e}_n,\; c_{k,p} \rangle, \qquad \hat{e}_n = \frac{e_n}{\|e_n\|_2}, \quad \tau = e^{4.6052} \approx 100 \]

三个成分各司其职：

1. **余弦相似度**：查询侧 `F.normalize`；原型侧默认文件已归一化（4.1.4 验证）。取值域 \([-1, 1]\)。
2. **max-over-prototypes**：一个类有 P 个原型（多模板），取最大相似度——「与该类任一说法最像就算像」，比平均更符合多模板语义，也让 P 的大小不改变语义。
3. **logit_scale = 4.6052**：`exp(4.6052) ≈ 100`，这是 CLIP 温度参数的初始值。余弦取值太「扁」（好样本 0.3、坏样本 0.2 就差很多），乘 100 拉开差距，使后续 softmax/交叉熵有足够陡的分布。它是 buffer（不训练）。

**实例/语义两条路径的差异只有一处切片**：

- 语义（`sem=True`）：用全部原型 → \(C+1 = 21\) 列（20 类 + 背景列，背景列随后被语义损失切掉，见下）。
- 实例（`sem=False`）：`cls_embed[:, 2:, :]` 丢掉 wall/floor 两个 stuff 类 → \(C-1 = 19\) 列（18 个 thing 类 + 背景）。

**背景列恒为 0 的推论**：back_token 是全零原型，与任何归一化向量的内积为 0，max 后仍是 0，乘 \(\tau\) 还是 0。于是实例 logits 中背景列**恒等于 0**：当某查询与所有 thing 类原型的余弦都 ≤ 0 时，argmax 落在背景列——一个简洁的「无对象」机制。

#### 4.3.2 核心流程

```text
cls_embd (N, D)                    # 来自 cls_proj / cls_proj_sem
    │ F.normalize(dim=-1)
    ▼
einsum('nd,dkp->nkp')              # (N, 21, P) 或切片后 (N, 19, P)
    │ max(-1)                      # 对 P 个原型取最大
    ▼
(N, 21) / (N, 19)
    │ × logit_scale.exp() ≈ ×100
    ▼
sem_logits (N,21) / inst_logits (N,19)
```

下游消费方式（一句话带过，细节在 u5）：

- 语义：[uniseg3d/semantic_criterion.py:L35-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py#L35-L41) 先 `pred_mask[:, :-1]` 切掉最后的背景列，再做交叉熵（`ignore_index=20`）。
- 实例：u5-l3 的 `OVInstanceCriterion` 对 19 列做监督，最后一列即「非物体」通道。
- 推理：[uniseg3d/uniseg3d.py:L110-L115](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L110-L115) 用 `F.softmax(cls_preds, dim=-1)[:, :-1]` 得到实例置信度（同样切掉背景列），可再乘 IoU 预测分数（u5-l4）。

#### 4.3.3 源码精读

[uniseg3d/query_decoder.py:L433-L440](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L433-L440)：

```python
def forward_logit(self, cls_embd, sem=False):
    if sem:
        cls_pred = torch.einsum('nd,dkp->nkp',
            F.normalize(cls_embd, dim=-1), self.cls_embed)
    else:
        cls_pred = torch.einsum('nd,dkp->nkp',
            F.normalize(cls_embd, dim=-1), self.cls_embed[:, 2:, :])
    cls_pred = cls_pred.max(-1).values
    cls_pred = self.logit_scale.exp() * cls_pred
    return cls_pred
```

配上 `logit_scale` 的注册（[uniseg3d/query_decoder.py:L419-L420](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L419-L420)）：

```python
logit_scale = torch.tensor(4.6052, dtype=torch.float32)
self.register_buffer('logit_scale', logit_scale, persistent=False)
```

阅读提示：

- `self.cls_embed[:, 2:, :]` 的 `2` 与配置里 `stuff_classes=[0, 1]`（[configs/...py:L136](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L136)）、评估器 `stuff_class_inds=[0,1]`（[L286](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L286)）是**同一个语义类别体系的三处呼应**：wall/floor 只参与语义/全景，不参与实例分类。
- 整个函数没有任何可学习参数——分类的全部「知识」都在原型文件与投影头里，这就是球面分类的可替换性所在。

#### 4.3.4 代码实践

**实践目标**：用小张量验证 max-over-prototypes、温度缩放与「背景列恒 0」。

**操作步骤**（示例代码）：

```python
import torch, torch.nn.functional as F
torch.manual_seed(0)
D, C, P, N = 8, 6, 3, 4                    # 缩小规模便于观察
prototypes = F.normalize(torch.randn(D, C, P), dim=0)   # 已归一化原型
prototypes = torch.cat([prototypes,
    torch.zeros(D, 1, P)], dim=1)          # 补背景类 → (D, 7, P)

cls_embd = torch.randn(N, D)
sim = torch.einsum('nd,dkp->nkp', F.normalize(cls_embd, dim=-1), prototypes)
logits = sim.max(-1).values * torch.tensor(4.6052).exp()

print('形状:', logits.shape)               # (4, 7)
print('背景列（最后一列）:', logits[:, -1])  # 应全为 0
# 手工对照一个元素
n, k, p = 1, 2, sim[1, 2].argmax()
manual = torch.dot(F.normalize(cls_embd, dim=-1)[1], prototypes[:, 2, p]) * 100
print('手工值:', manual.item(), 'vs', logits[1, 2].item())
```

**需要观察的现象**：形状 (4, 7)；背景列全 0；手工值与 einsum 结果一致（原型已归一化时内积即余弦）。

**预期结果**：以上三点全部成立；把 `* 100` 去掉后 softmax 会明显变平（可自行打印 `logits.softmax(-1)` 对比）。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `max` 而不是 `mean` 聚合一个类的 P 个原型？

**答案**：多模板原型刻画「同一类的多种说法」，一个查询只要与其中一种说法强匹配就应属于该类；`mean` 会被不相关模板稀释（比如 "a seat with legs" 与点云不太像，但 "a chair" 很像，mean 会拉低分数）。max 还保证增删模板不改变已匹配查询的分数语义。

**练习 2**：如果删除 `logit_scale`（τ=1），训练会发生什么？

**答案**：余弦取值域只有 [-1,1]，19 类的 softmax 会接近均匀分布，交叉熵梯度极小、损失曲线平缓，模型难以收敛到尖锐的类别判断。τ≈100 是 CLIP 论文中温度参数的初始值，用于把余弦差距放大到适合 softmax 的量级。

**练习 3**：实例路径 `cls_embed[:, 2:, :]` 切掉的是哪两类？如果换成 Matterport3D 这类别集不同的数据集，需要改动哪里？

**答案**：切掉的是语义类别体系中索引 0、1 的 wall 和 floor（stuff 类，不构成实例）。换数据集需要：用新数据集类别名重新生成 `cls_embedding.pth`（P 个模板 × 新类别数 C）、更新配置中 `num_semantic_classes`/`num_instance_classes` 与 stuff 类索引，解码器代码本身（`[:, 2:, :]`）只在 stuff 类仍恰为前 2 个时才不用改。

### 4.4 `_forward_head` 的 last_flag 语义分支与两套投影头

#### 4.4.1 概念说明

`UnifiedQueryDecoder` 重写了父类的 `_forward_head`，把「每个查询同时做实例分类与语义分类」接进迭代预测循环。关键设计是 `last_flag` 参数：

- **实例分类（cls_preds）每一层都算**：6 层解码 + 初始一次 = 7 份预测，前 6 份进 `aux_outputs` 深监督（承接 u4-l3）；
- **语义分类（sem_preds）只在最后一层算**（`last_flag=True`）：语义分割本质是「每个超点属于哪类」的逐点判断，由最终掩码 + 每查询的语义标签合成即可，中间层的语义预测既贵又欠成熟，算一次就够。

于是同一份 `norm_query`、同一份 `pred_mask`、同一份 `maskpool_`，被两套投影头分别加工：实例头（`mask_pooling_proj` + `cls_proj` + 切片原型）与语义头（`mask_pooling_proj_sem` + `cls_proj_sem` + 全量原型）。另注意配置 `objectness_flag=False`（[configs/...py:L64](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L64)），所以 `scores` 恒为 None——实例置信度完全来自 `cls_preds`（推理侧 softmax，u6-l1）。

#### 4.4.2 核心流程

```text
_forward_head(queries, mask_feats, last_flag):        # 每个场景 i 循环
  norm_query = out_norm(queries[i])                    # (N, 256)
  pred_mask = einsum('nd,md->nm', norm_query, mask_feats[i])   # (N, M)
  ── sphere_cls 分支 ──
  maskpool_ = mask_pool(mask_feats[i].T, pred_mask.detach())  # (N, 256)
  # 实例路（每层都走）
  cls_embd = cls_proj( mask_pooling_proj(maskpool_) + norm_query )
  cls_preds.append(forward_logit(cls_embd))            # (N, 19)
  # 语义路（仅 last_flag）
  if last_flag:
      cls_embd_sem = cls_proj_sem( mask_pooling_proj_sem(maskpool_) + norm_query )
      sem_preds.append(forward_logit(cls_embd_sem, sem=True))  # (N, 21)
  ── 注意力掩码（attn_mask=True）──
  attn_mask 由 pred_mask.sigmoid() < 0.5 派生（u4-l3 已讲）

forward_iter_pred 调用节奏：
  初始 _forward_head(last_flag=False)          # 第 1 份 cls_preds
  for i in 0..5:
      cross_attn → self_attn → ffn
      _forward_head(last_flag=(i == 5))         # 第 2..7 份；仅 i=5 带 sem
  返回末层 cls/sem/masks + aux_outputs（前 6 份，sem 均为 None）
```

#### 4.4.3 源码精读

球面分支主体（[uniseg3d/query_decoder.py:L477-L491](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L477-L491)）：

```python
pred_mask = torch.einsum('nd,md->nm', norm_query, mask_feats[i])

if self.sphere_cls:
    maskpool_ = self.mask_pooling(x=mask_feats[i].T, mask=pred_mask.detach())

    maskpool_embd = self.mask_pooling_proj(maskpool_)
    cls_embd = self.cls_proj(maskpool_embd + norm_query)
    cls_pred = self.forward_logit(cls_embd)
    cls_preds.append(cls_pred)
    if last_flag:
        norm_query_sem = norm_query.clone()
        maskpool_embd_sem = self.mask_pooling_proj_sem(maskpool_)
        cls_embd_sem = self.cls_proj_sem(maskpool_embd_sem + norm_query_sem)
        cls_pred_sem = self.forward_logit(cls_embd_sem, sem=True)
        sem_preds.append(cls_pred_sem)
```

注意三处细节：

1. `x=mask_feats[i].T`：把 (M, 256) 转成 (256, M) 以符合 `mask_pool` 的 `[D, M]` 约定。
2. **相加融合**：`maskpool_embd + norm_query`——分类特征 = 「我掩住了什么」+「我是谁」，两个 256 维向量直接相加后各自投影到 CLIP 空间。
3. `norm_query_sem = norm_query.clone()` 是防御性克隆（相加本身非原地，克隆保证两分支互不干扰）。

`last_flag` 对返回值的门控（[uniseg3d/query_decoder.py:L500-L502](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L500-L502)）：非末层时 `sem_preds` 直接置 `None`。

迭代循环中的调用节奏（[uniseg3d/query_decoder.py:L548-L567](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L548-L567)）：初始一次 `last_flag=False`，随后每层 `last_flag = i == len(self.cross_attn_layers) - 1`；同时每轮收集 `contras_embeds`（供 u5-l6 跨任务蒸馏）。最终返回（[L579-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L579-L585)）：末层 `cls_preds / sem_preds / masks / scores(None) / contras_embeds` + 6 份 `aux_outputs`。

`pred_scores` 的门控（[uniseg3d/query_decoder.py:L474-L476](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L474-L476)）：`objectness_flag=False` 时为 None，确认「实例置信度唯一来源是球面分类 logits」。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：不跑模型，靠通读代码回答「一次前向到底产生几份分类预测、几份语义预测」。

**操作步骤**：

1. 打开 [uniseg3d/query_decoder.py:L531-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L531-L585)，数一数 `_forward_head` 的调用点：L548（初始）与 L561（循环内）。
2. 结合配置 `num_layers=6`（[configs/...py:L49](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L49)），列出 `cls_preds` / `sem_preds` / `pred_masks` / `contras_embeds` 四个列表各自的长度与哪些元素是 None。
3. 核对 L569-L578 的 `aux_outputs` zip：它取每个列表的 `[:-1]`，`sem_preds[:-1]` 里是否全为 None？

**需要观察的现象**：`cls_preds` 7 份（1 初始 + 6 层）、`sem_preds` 仅末层 1 份非 None、`aux_outputs` 6 份且其 `sem_preds` 全为 None。

**预期结果**：与上述一致；据此可推断语义损失每步只算 1 次、实例损失算 7 次（深监督），这与 u5-l1 将看到的损失调用次数吻合（待在 u5 验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么语义分支只在最后一层计算，而实例分支每层都算？

**答案**：实例分类与掩码耦合紧（匹配 + 深监督能逐层校正「查询↔实例」的对应），中间层监督收益大；语义预测只是给最终掩码贴逐点标签的「颜色笔」，中间层掩码本身还不准，语义预测既无监督必要也浪费算力（21 列 × 每查询），只在最终掩码定型后算一次即可。

**练习 2**：`_forward_head` 里实例路与语义路共用同一份 `maskpool_`，语义分类会不会因此「被实例掩码带偏」？

**答案**：池化区域确实来自实例掩码，但语义头有独立的 `mask_pooling_proj_sem` 与 `cls_proj_sem`，可以在同一区域特征上学出与实例头不同的判别方向；且语义分支的掩码本来就该是「该查询对应的物体区域」，用实例掩码圈区域在语义上是自洽的（每个查询就是一个潜在物体/区域）。真正的解耦体现在两套参数与两套原型切片（19 vs 21 列）上。

**练习 3**：配置里 `objectness_flag=False`，那推理时实例的置信度分数从哪来？

**答案**：从球面分类 logits 来。[uniseg3d/uniseg3d.py:L110-L115](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L110-L115) 对 `cls_preds` 做 `softmax` 后切掉最后一列（背景）得到每类置信度，作为实例分数；若启用了 IoU 预测分支（u5-l4）还会乘上 `preds_iou`。

## 5. 综合实践

**任务**：完整复现「mask_pool → 池化投影 → cls_proj → forward_logit」流水线，核对实例/语义 logits 的类别维数。

**第 1 步：体检原型文件**（见 4.1.4）。若尚未下载 `scannet_cls_embedding.pth`，可先用合成原型跑通第 2 步，下载后再回来核对真实维度。

**第 2 步：8 查询 × 50 点的最小复现**（示例代码，只需 torch；在项目根目录执行）：

```python
# sphere_cls_repro.py
import importlib.util, torch, torch.nn as nn, torch.nn.functional as F

# —— 可切换：真实文件 or 合成原型 ——
try:
    proto_raw = torch.load('data/scannet/scannet_cls_embedding.pth')  # (C,P,D)
except FileNotFoundError:
    C, P, D = 20, 3, 768                       # 合成替代（数值待本地验证）
    proto_raw = F.normalize(torch.randn(C, P, D), dim=2)
C, P, D = proto_raw.shape
print('原型:', proto_raw.shape)                # 期望 (20, P, 768)

# 补背景类 → (C+1, P, D) → permute → (D, C+1, P)
prototypes = torch.cat(
    [proto_raw, torch.zeros(1, P, D)], 0).permute(2, 0, 1).contiguous()

# 直载 mask_pool，避免触发 uniseg3d/__init__.py
spec = importlib.util.spec_from_file_location('mp', 'uniseg3d/mask_pool.py')
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)

# —— 模拟解码器末层 ——
torch.manual_seed(0)
N, M, d_model = 8, 50, 256                     # 8 个查询、50 个超点
norm_query  = F.normalize(torch.randn(N, d_model), dim=-1)   # out_norm 后的查询
mask_feats  = torch.randn(M, d_model)          # x_mask 输出
pred_mask   = norm_query @ mask_feats.T        # (N, M) 掩码 logits

maskpool_     = mp.mask_pool(x=mask_feats.T, mask=pred_mask.detach())  # (N,256)
pool_proj     = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_model))
pool_proj_sem = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_model))
cls_proj      = nn.Sequential(nn.Linear(d_model, d_model), nn.ReLU(),
    nn.Linear(d_model, d_model), nn.ReLU(), nn.Linear(d_model, D))
cls_proj_sem  = nn.Sequential(nn.Linear(d_model, d_model), nn.ReLU(),
    nn.Linear(d_model, d_model), nn.ReLU(), nn.Linear(d_model, D))

def forward_logit(embd, sem=False):
    p = prototypes if sem else prototypes[:, 2:, :]
    return (torch.einsum('nd,dkp->nkp', F.normalize(embd, dim=-1), p)
            .max(-1).values) * 100.0

cls_embd     = cls_proj(pool_proj(maskpool_) + norm_query)
cls_embd_sem = cls_proj_sem(pool_proj_sem(maskpool_) + norm_query)
inst_logits, sem_logits = forward_logit(cls_embd), forward_logit(cls_embd_sem, True)
print('实例 logits:', inst_logits.shape)       # 期望 (8, 19)
print('语义 logits:', sem_logits.shape)        # 期望 (8, 21)
print('实例背景列恒 0:', bool((inst_logits[:, -1] == 0).all()))
print('argmax 类别（实例）:', inst_logits.argmax(-1))
```

**第 3 步：核对与观察**：

1. `maskpool_` 形状 (8, 256)；实例 logits **(8, 19)**、语义 logits **(8, 21)**（以 C=20 为前提，用真实文件时请复核）。
2. 实例 logits 最后一列（背景）全 0；语义 logits 最后一列是背景列，会被语义损失切掉。
3. 把 `norm_query` 置零再跑一次：分类完全由 `maskpool_`（掩码区域特征）决定——感受「查询身份」与「掩码内容」两路信息的相加融合。

**预期结果**：形状与零背景列全部成立。脚本中合成原型的 P=3、D=768 为占位假设，**真实数值待本地验证**。

## 6. 本讲小结

- **球面分类 = 用外部 CLIP 文本原型替换线性分类头**：`out_cls`/`out_sem` 在 `sphere_cls=True` 下不创建，分类知识全部来自 `scannet_cls_embedding.pth`（(C=20, P, D)，补全零 back_token 后 (D, 21, P) 存为非持久 buffer），换词表不必改网络。
- **mask_pool 是「掩码区域特征提取器」**：logits>0 二值化 + 行归一化 + einsum 平均；掩码侧 detach 断梯度，梯度只流向点特征；全空掩码靠 1e-8 兜底。
- **forward_logit 三步曲**：查询 L2 归一化 → 与原型余弦相似度并对 P 个原型取 max → 乘 `exp(4.6052)≈100` 的温度；实例路径 `[:, 2:, :]` 丢弃 wall/floor 两个 stuff 类，得 19 列，背景列因全零原型恒为 0。
- **实例与语义是两套完全独立的头**：`mask_pooling_proj`/`cls_proj`（每层都算，19 列）与 `mask_pooling_proj_sem`/`cls_proj_sem`（仅 `last_flag` 末层算，21 列），共用同一份 `maskpool_` 但参数不共享。
- **一次前向共 7 份实例分类、1 份语义分类**：初始 + 6 层的 `cls_preds` 中前 6 份进 `aux_outputs` 深监督；`objectness_flag=False` 使 `scores` 恒 None，实例置信度唯一来源是球面分类 logits（推理侧 softmax 后切掉背景列）。
- **工程提醒**：`persistent=False` 意味着推理环境也必须有原型文件；`back_token` 建在 `device='cuda'` 上，纯 CPU 机器无法直接实例化解码器，做实验可用本讲的手工复现脚本绕开。

## 7. 下一步学习建议

本讲补完了解码器的「分类侧」，至此模型四大件（骨干 u4-l2、解码器 u4-l3/本讲、语言分支）只剩语言分支未读：

1. **下一讲 u4-l5（LangModule 与 OpenCLIP 文本编码）**：本讲的「CLIP 空间」到底从哪来——`convnext_large_d_320` 文本塔如何把描述编码成 768 维嵌入再投影到 32 维查询空间；读完即可理解原型文件与语言分支为何必须是同一个编码器。
2. **再往后 u4-l6（提示查询注入）**：看文本嵌入如何作为文本提示查询拼进解码器，与本讲的 `type_embedding` 汇合。
3. **对照阅读损失侧（u5）**：本讲产出的 `cls_preds`（19 列）与 `sem_preds`（21 列）分别被 `OVInstanceCriterion`（u5-l3）与 `ScanNetSemanticCriterion`（u5-l1，`[:, :-1]` 切背景列后交叉熵）消费，建议带着「19/21 列的列布局」这个问题去读。
4. **动手延伸**：在综合实践脚本里把合成原型换成真实文件，再尝试把某个类的原型换成同义句子的 OpenCLIP 嵌入，观察 argmax 的变化——这就是「开放词汇」的最小实验雏形（系统做法见 u6-l3）。
