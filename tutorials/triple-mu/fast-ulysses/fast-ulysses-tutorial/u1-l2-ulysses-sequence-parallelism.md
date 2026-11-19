# 讲义 u1-l2：Ulysses 序列并行原理：两次 all-to-all 包夹注意力

## 1. 本讲目标

上一讲（u1-l1）我们站在项目外面看清了 fast-ulysses 要解决的问题：把 Ulysses 序列并行的两次 4D all-to-all 从关键路径上挪开。本讲我们打开这个问题本身——**这两次 all-to-all 到底交换了什么、为什么恰好是两次、为什么中间夹的正好是注意力**。

学完本讲你应当能：

1. 画出 mode 0 / mode 1 前后的张量形状：mode 0 把每个 rank 的 `(b, s_local, n_global, d)` 变成 `(b, s_global, n_local, d)`，mode 1 是它的逆。
2. 解释为什么长序列 DiT 训练里这两个集合通信成为关键路径（字节量随序列长度线性增长，且每层都要付两次）。
3. 理解 q/k/v 打包进最后一维（`3 * head_dim`）为什么能在字节量不变的前提下，把三次握手压成一次。
4. 说出 SPMD 调用契约：每个 rank 必须发出相同的形状序列，违反的后果是**挂起**而不是报错。

本讲全程不需要 GPU：所有实践都可以在单进程、纯 CPU 的 torch 上完成——这与项目自己的哲学一致（布局语义可以在主机上验证，见 [docs/design.md:37-39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L37-L39)）。

## 2. 前置知识

### 2.1 形状记号

全手册统一使用（与 [docs/api.md:3-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L3-L4) 一致）：

| 记号 | 含义 |
| --- | --- |
| `b` | batch 大小 |
| `d` | 每个注意力头的维度（head dim） |
| `n_global` | 注意力头总数；`n_local = head_splits[本rank]` 是本 rank 分到的头 |
| `s_global` | 全局序列长度；`s_local = seq_splits[本rank]` 是本 rank 分到的序列段 |
| `ws` | world size，即 rank 数 |

4D 张量 `(b, s, n, d)` 的轴含义是：batch、序列（token）、注意力头、头的通道。

### 2.2 集合通信与 all-to-all

- **集合通信（collective）**：所有 rank 一起参与的通信原语，如 broadcast、all-gather、all-reduce。
- **all-to-all**：每个 rank 都给其他每个 rank（包括自己）发一块数据，同时从每个 rank 收一块数据。它是"交换分片方式"的最自然原语：发出去的是自己多出来的那份，收进来的是自己缺的那份。

### 2.3 注意力按头独立

缩放点积注意力对每个头 \( h \) 独立计算：

\[
\mathrm{Attention}(Q_h, K_h, V_h) = \mathrm{softmax}\!\left(\frac{Q_h K_h^\top}{\sqrt{d}}\right) V_h
\]

关键性质：**头与头之间没有任何数据交换**。softmax 混合的只是同一头内部的 token（序列轴）。整个 Ulysses 方案成立的根基就是这一条——把头切开分给不同 rank，注意力计算本身不需要任何额外通信。

### 2.4 SPMD

Single Program Multiple Data：每个 rank 运行**同一个程序**，各自处理自己那份数据。torchrun 启动的训练脚本就是这种形态。这意味着代码里没有 `if rank == 0: 做A; else: 做B` 式的分工——所有 rank 执行同一条语句序列，只是数据不同。这一点直接决定了 4.4 节的调用契约。

### 2.5 承接上一讲

u1-l1 已点名的术语——拷贝引擎（CE）、对称内存、mode 0/1——本讲给出精确定义；CE 传输如何兑现这些形状变换则留给第 5 单元（u5-l1）。

## 3. 本讲源码地图

| 文件 | 本讲用它做什么 |
| --- | --- |
| [README.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L11-L13) | 项目对 Ulysses 的一段官方定义；快速开始里的形状示例 |
| [docs/quickstart.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L3-L5) | "两次调用包夹注意力"的最小代码；qkv 打包建议；"什么会让你挂起" |
| [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L1-L17) | 一个真实的序列并行注意力块：两次调用、qkv 打包、双路校验 |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L87) | mode 0/1 语义的**纯 numpy 参照实现**——本讲实践的对照答案 |
| [include/fast_ulysses/a2a_plan.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L12-L17) | mode 0/1 的 C++ 枚举定义，形状注释是全项目最浓缩的语义说明 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L111-L128) | Python 侧 `all_to_all_4d` 签名与 docstring 中的契约陈述 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L42-L47) | mode 与输入输出形状的官方对照表；集合契约清单 |

## 4. 核心概念与源码讲解

### 4.1 mode 0 / mode 1：一次交换的精确语义

#### 4.1.1 概念说明

长序列训练时，一张卡放不下整个序列的激活，于是把**序列切开**：rank r 拿到第 r 段（长度 `s_r = seq_splits[r]`），但拿**全部** `n_global` 个头。问题马上出现：注意力需要同一头看到**完整**序列才能算 `Q K^T`，而现在的每个 rank 只有片段。

Ulysses 的解法是一次"交换商品"：**用我的序列分片换你的头分片**。交换之后，rank 持有完整序列但只有 `1/ws` 的头——由 2.3 节的性质，注意力照样能算，而且每 rank 的注意力计算量降到原来的 `1/ws`。算完再换回来。

- **mode 0（scatter heads, gather sequence）**：序列分片 + 全部头 → 完整序列 + 头分片。注意力**之前**调用。
- **mode 1（gather heads, scatter sequence）**：mode 0 的严格逆变换。注意力**之后**调用。

"scatter"指把本来聚在一起的轴（所有头）打散到各 rank，"gather"指把本来分散的轴（各 rank 的序列段）聚成完整序列。mode 0 同时做两件事：scatter 头、gather 序列。

#### 4.1.2 核心流程

设头偏移 \( H_p = \sum_{j<p} h_j \)，序列偏移 \( S_p = \sum_{j<p} s_j \)（\( h_j, s_j \) 分别是 `head_splits[j]`、`seq_splits[j]`；\( H_0 = S_0 = 0 \)）。

**mode 0**：rank \( r \) 持有 \( x_r \in \mathbb{R}^{b \times s_r \times n \times d} \)（\( n = n_{\text{global}} \)）。它发给 peer \( p \) 的是自己张量的一个**头切片**：

\[
x_r[:,\; :,\; H_p{:}H_{p+1},\; :] \;\in\; \mathbb{R}^{b \times s_r \times h_p \times d}
\]

rank \( p \) 把来自所有 rank 的块**沿序列轴（dim 1）按 rank 序**拼接，得到输出：

\[
y_p = \bigl\Vert_{r=0}^{ws-1} x_r[:, :, H_p{:}H_{p+1}, :] \bigr\vert_{\dim 1} \;\in\; \mathbb{R}^{b \times s_{\text{global}} \times h_p \times d}
\]

**mode 1**：rank \( r \) 持有 \( y_r \in \mathbb{R}^{b \times s_{\text{global}} \times h_r \times d} \)，发给 peer \( p \) 的是一个**序列切片** \( y_r[:, S_p{:}S_{p+1}, :, :] \)，rank \( p \) 沿**头轴（dim 2）**拼接：

\[
z_p = \bigl\Vert_{r=0}^{ws-1} y_r[:, S_p{:}S_{p+1}, :, :] \bigr\vert_{\dim 2} \;\in\; \mathbb{R}^{b \times s_p \times n \times d}
\]

**往返正确性**（为什么两次调用恰好复原，且是位精确的）：

\( z_p \) 沿 dim 2 的第 \( r \) 段 = \( y_r \) 沿 dim 1 的第 \( p \) 段 = \( x_p[:, :, H_r{:}H_{r+1}, :] \)。于是

\[
z_p = \bigl\Vert_{r} x_p[:, :, H_r{:}H_{r+1}, :] \bigr\vert_{\dim 2} = x_p[:, :, H_0{:}H_{ws}, :] = x_p
\]

整个过程中只有切片和搬运，没有任何算术，所以往返是**位精确**（bit-exact）的——这是 [test/test_plan.py:236-237](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L237) 敢用 `assert_array_equal` 的原因。

**通信量记账**（偶均分时）：mode 0 中每个 rank 发出 \( b \cdot s_{\text{local}} \cdot n_{\text{global}} \cdot d \) 个元素，收到 \( b \cdot s_{\text{global}} \cdot n_{\text{local}} \cdot d \) 个元素；由于 \( s_{\text{global}} = ws \cdot s_{\text{local}} \)、\( n_{\text{local}} = n_{\text{global}} / ws \)，两者相等——发多少收多少，正好是全量张量的 \( 1/ws \)。

**不均匀分片是一般路径**：上面的公式从未要求 \( s_r \) 或 \( h_p \) 相等。偶均分只是 \( s_j = s/P,\ h_j = n/P \) 的特例（[include/fast_ulysses/a2a_plan.hpp:5-6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L5-L6) 的注释原话）。允许某个 \( s_p = 0 \) 也是合法的（空序列段不发拷贝，见 [test/test_plan.py:179-181](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L179-L181)）。丢掉 padding 的收益在 u2-l3 展开。

#### 4.1.3 源码精读

**① mode 的权威定义——C++ 枚举。** 全项目对 mode 0/1 最浓缩的语义说明只有两行注释：

[include/fast_ulysses/a2a_plan.hpp:12-17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L12-L17)

```cpp
enum A2AMode : int {
    // [b, seq_splits[me], head_total, d] -> [b, seq_total, head_splits[me], d]
    kScatterHead = 0,
    // the inverse: [b, seq_total, head_splits[me], d] -> [b, seq_splits[me], head_total, d]
    kGatherHead = 1,
};
```

这两行注释就是 mode 的完整契约：`kScatterHead = 0` 把"我的序列段 × 全部头"变成"全部序列 × 我的头段"，`kGatherHead = 1` 是逆变换。Python 侧的 `mode=0` / `mode=1` 直接映射到这个枚举。

**② 官方形状对照表。**

[docs/api.md:44-47](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L44-L47)

| mode | input | output |
| --- | --- | --- |
| 0 — scatter heads, gather sequence | `(b, s_local, n_global, d)` | `(b, s_global, n_local, d)` |
| 1 — the inverse | `(b, s_global, n_local, d)` | `(b, s_local, n_global, d)` |

**③ README 快速开始中的具体数字。**

[README.md:63-68](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L63-L68)

```python
b, s_local, heads, d = 1, 4096, 8, 128
ws = dist.get_world_size()
x = torch.randn(b, s_local, heads * ws, d, dtype=torch.bfloat16, device="cuda")

y = group.all_to_all_4d(x, mode=0)        # (b, s_local * ws, heads, d)
z = group.all_to_all_4d(y, mode=1)        # back to x's shape
```

注意这里的 `heads=8` 是**每 rank** 的头数（即 `n_local`），所以输入的头轴是 `heads * ws = 64 = n_global`。以 `ws=8` 代入：`x` 是 `(1, 4096, 64, 128)`，mode 0 之后 `y` 是 `(1, 32768, 8, 128)`——序列轴变长 8 倍，头轴缩短 8 倍，元素总数不变。mode 1 再换回 `(1, 4096, 64, 128)`。

**④ 语义的测试参照实现——本讲实践的"标准答案"。**

[test/test_plan.py:72-87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L87)

```python
def reference_scatter_head(inputs, head_splits):
    """[b, s_r, n_total, d] per rank -> [b, s_total, n_p, d] per rank.
    Rank p's output stacks, over source ranks r in order, the head columns
    p owns from rank r's local sequence shard."""
    head_off = np.cumsum([0] + list(head_splits))
    return [
        np.concatenate([x[:, :, head_off[p] : head_off[p + 1], :] for x in inputs], axis=1)
        for p in range(len(inputs))
    ]
```

这段 numpy 代码逐字实现了 4.1.2 的公式：rank p 的输出 = 对每个来源 rank r 取其头切片 `head_off[p]:head_off[p+1]`，沿 `axis=1`（序列轴）拼接。紧随其后的 [reference_gather_head（test/test_plan.py:89-98）](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L89-L98) 是 mode 1 的对应实现（沿 `axis=2` 拼接序列切片）。测试还把它们锚定到 torch 自己的 `all_to_all_single` 语义上（[test/test_plan.py:260-266](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L260-L266)），说明这是所有 Ulysses 实现共同遵守的语义。

#### 4.1.4 代码实践

> 说明：本讲义写作环境无法执行 Python，以下脚本均**未实际运行**；"预期现象"是从上述源码语义推导的，请本地验证（纯 CPU、无需安装 fast-ulysses，只需 torch）。

**实践目标**：在单进程内用 torch 的切片 + `cat` 复现 mode 0 / mode 1 的语义，并验证两者互逆。

**操作步骤**：保存为 `u1l2_practice.py`，`python u1l2_practice.py` 运行。

```python
# 示例代码：单进程模拟 Ulysses 的两次 all-to-all（纯 CPU）
import torch

def scatter_head(inputs, head_splits):
    """mode 0: 每个元素是 [b, s_r, n_total, d]；返回每个 rank 的 [b, s_total, n_p, d]"""
    off = [0]
    for h in head_splits:
        off.append(off[-1] + h)
    return [
        torch.cat([x[:, :, off[p]:off[p + 1], :] for x in inputs], dim=1)
        for p in range(len(inputs))
    ]

def gather_head(inputs, seq_splits):
    """mode 1: 每个元素是 [b, s_total, n_r, d]；返回每个 rank 的 [b, s_p, n_total, d]"""
    off = [0]
    for s in seq_splits:
        off.append(off[-1] + s)
    return [
        torch.cat([x[:, off[p]:off[p + 1], :, :] for x in inputs], dim=2)
        for p in range(len(inputs))
    ]

torch.manual_seed(0)
seq_splits, head_splits = [5, 3, 4, 2], [2, 3, 1, 2]   # 故意不均匀；与 test_round_trip 同一组
b, d, ws = 2, 8, len(seq_splits)

inputs = [torch.randn(b, s, sum(head_splits), d) for s in seq_splits]

mid = scatter_head(inputs, head_splits)                 # 模拟所有 rank 的 mode 0
back = gather_head(mid, seq_splits)                     # 模拟所有 rank 的 mode 1

print([tuple(m.shape) for m in mid])
print(all(torch.equal(z, x) for z, x in zip(back, inputs)))   # 往返应位精确复原
```

**需要观察的现象**：
1. `mid` 中每个张量的形状——序列轴都变成 `sum(seq_splits)=14`，头轴各自是 `head_splits[p]`。
2. 最后一行打印 `True`。

**预期结果**：`mid` 的形状依次为 `(2,14,2,8) (2,14,3,8) (2,14,1,8) (2,14,2,8)`；往返比较打印 `True`（位精确，因为只有切片和拼接）。**待本地验证**。

**对照检查**：把你的 `scatter_head` 与 [test/test_plan.py:72-87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L87) 的 `reference_scatter_head` 逐行对比——切片区间、拼接轴、拼接顺序（沿来源 rank 序）三者都应一致。`splits` 采用了与 [test_round_trip（test/test_plan.py:238-239）](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L238-L239) 相同的不均匀参数，因此你的脚本实质上在 CPU 上复刻了那个测试。

**进阶（可选）**：仿照 [test/test_plan.py:277-285](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L277-L285)，用 `permute(2,0,1,3)` + `chunk(ws)` + `cat` 再实现一遍偶均分下的 mode 0，验证与你的 `scatter_head` 输出位相等。注意这条 all_to_all_single 风格的路子要求均分——`chunk` 只能切出等大的块，这正是本库用显式 `splits` 列表作一般接口的一个理由。

#### 4.1.5 小练习与答案

**练习 1**：`ws=2`，`seq_splits=[4,6]`，`head_splits=[3,1]`，`b=1`，`d=16`。写出两个 rank 在 mode 0 前后的形状。

**答案**：rank 0：`(1,4,4,16) → (1,10,3,16)`；rank 1：`(1,6,4,16) → (1,10,1,16)`。序列轴统一为全局长度 10，头轴各自取自己的份。

**练习 2**：mode 0 中 rank `r` 发给 peer `p` 的数据块形状是什么？rank `p` 总共从多少个 rank 收数据？

**答案**：块形状是 `(b, s_r, h_p, d)`——"我的序列段 × 你的头份"。rank `p` 从全部 `ws` 个 rank（含它自己）各收一块，沿 dim 1 拼成 `(b, s_global, h_p, d)`。本 rank 自己的那份也走同一条交换路径，这是后面窗口设计的一个伏笔（见 [include/fast_ulysses/a2a_plan.hpp:62-64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L64) 的注释）。

**练习 3**：为什么往返校验可以用 `torch.equal`（位精确），而 4.2 节例子里的注意力对照只能用 `allclose`？

**答案**：两次 all-to-all 是纯数据搬运（切片 + 拼接），没有任何浮点运算，位精确复原；注意力含浮点归约（softmax、matmul），两条执行路径的归约顺序不同，结果只能近似相等。

### 4.2 包夹注意力：为什么两次通信中间正好放注意力

#### 4.2.1 概念说明

序列并行要解决的核心矛盾：**切序列**省下了显存与计算，但**注意力需要完整序列**。Ulysses 的回答是把通信放在注意力的两头，形成一个"三明治"：

```
mode 0（交换） → 注意力（本地计算） → mode 1（交换回来）
```

mode 0 之后每个 rank 持有 `(b, s_global, n_local, d)`：**完整序列 × 我的头段**。由 2.3 节"头与头不通信"的性质，注意力在这一布局下完全本地——每个 rank 对自己的 `n_local` 个头、对着完整序列算 `softmax(QK^T)V`，期间零通信。mode 1 把布局换回"我的序列段 × 全部头"，让层的其余部分（LayerNorm、MLP 等逐 token 操作）继续按序列并行运转。

反过来看这个三明治的**记账**：

- 计算量：每 rank 注意力浮点量为 \( O(b \cdot n_{\text{local}} \cdot s_{\text{global}}^2 \cdot d) \)，是单卡全量 \( O(b \cdot n_{\text{global}} \cdot s_{\text{global}}^2 \cdot d) \) 的 \( 1/ws \)——序列并行让平方项的计算也被摊薄。
- 通信量：两次交换各搬全量张量的 \( 1/ws \)（见 4.1.2 的记账），与序列长度**线性**相关。

于是有了对"关键路径"的定量直觉：注意力的计算量按 \( s^2 \) 增长但被摊到 `ws` 张卡上；通信按 \( s \) 线性增长且每层固定付两次、无法摊薄。序列越长，通信在每层耗时中的占比越高，最终成为瓶颈。用 README 快速开始的形状估算一笔账：`(1, 4096, 64, 128)` bf16 每个 rank 约 67 MB，每个注意力层两次交换 ≈ 每 rank 搬 134 MB；几十层的 DiT 一步就是每 rank 数 GB 的纯搬运。[README.md:11-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L11-L13) 说这两次集合通信是"the critical communication"，量级上就是这个意思；README 性能表（[README.md:34-39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L34-L39)）里 base 路径在 H200 上花 1.569 ms、优化后仍要 0.860 ms 的，正是这两次交换。

#### 4.2.2 核心流程

一个序列并行的 DiT 注意力块，前向伪代码：

```
输入 x: (b, s_local, n_global, head_dim)          # 我持有的序列段，全部头
1. qkv = 投影(x) → view 成 (b, s_local, n_global, 3*head_dim)   # 4.3 节的打包
2. qkv = all_to_all_4d(qkv, mode=0)               # → (b, s_global, n_local, 3*head_dim)
3. q, k, v = chunk(qkv, 3, dim=-1)
   attn = SDPA(q, k, v)                            # 完整序列 × 我的头段，零通信
   # → (b, s_global, n_local, head_dim)
4. out = all_to_all_4d(attn, mode=1)               # → (b, s_local, n_global, head_dim)
5. 返回 投影(out)
```

反向传播时两个mode自动互换角色：mode 0 的 vjp 是 mode 1、mode 1 的 vjp 是 mode 0（排列的 vjp 是逆排列），细节在 u6-l3。

#### 4.2.3 源码精读

**① quickstart 的最小三明治。**

[docs/quickstart.md:3-5](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L3-L5) 一句话点题："The two calls bracket attention: mode 0 trades the sequence shard for a head shard, mode 1 trades it back." 对应代码：

[docs/quickstart.md:21-25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L21-L25)

```python
def attention_block(qkv):       # (b, s_local, n_global, 3 * head_dim)
    qkv = group.all_to_all_4d(qkv, mode=0)      # -> (b, s_global, n_local, 3 * head_dim)
    out = attention(qkv)                        # each rank now holds whole sequences
    return group.all_to_all_4d(out, mode=1)     # -> (b, s_local, n_global, head_dim)
```

**② 完整实例：examples/dit_attention.py 的 forward。**

[examples/dit_attention.py:45-63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L45-L63)

```python
def forward(self, x: torch.Tensor) -> torch.Tensor:
    b, s_local, _ = x.shape
    qkv = self.qkv(x).view(b, s_local, self.n_global, 3 * self.head_dim)
    qkv = self.group.all_to_all_4d(qkv, mode=0)  # (b, s_global, n_local, 3 * head_dim)
    q, k, v = qkv.chunk(3, dim=-1)
    attn = F.scaled_dot_product_attention(
        q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
    ).transpose(1, 2)  # (b, s_global, n_local, head_dim)
    attn = self.group.all_to_all_4d(attn.contiguous(), mode=1)
    return self.proj(attn.reshape(b, s_local, self.n_global * self.head_dim))
```

三段各对应伪代码的一步：第 50 行的 `view` 是打包（4.3 节）；第 53 行 mode 0；第 55-59 行在 `(b, s_global, n_local, 3*head_dim)` 布局上 `chunk` 出 q/k/v 并做 SDPA（`transpose(1,2)` 只是把头轴换到 SDPA 要求的位置，不搬数据语义）；第 62 行 mode 1 换回。模块的输入输出都是 `(b, s_local, n_global * head_dim)`——对调用者来说它就是一个普通 `nn.Module`。

**③ 双路校验的方法论。** 这个例子不只跑通，还自我检查：与"单 rank、无序列并行"的同一块（[examples/dit_attention.py:66-74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L66-L74) 的 `reference`）同时比较**输出**和**输入梯度**（[examples/dit_attention.py:102-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L102-L116)）：把各 rank 的分片 `all_gather` 起来拼成全局结果，与参照做 `allclose`。文件 docstring 道破动机（[examples/dit_attention.py:11-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L11-L13)）：一个集合通信在单元测试里可以位精确，却仍被接错到真实模块的错误的轴上——所以要拿完整模块对完整模块比。

#### 4.2.4 代码实践

**实践目标**：在单进程里亲手验证"包夹"的正确性——被夹住的算子只要**不跨头混合**，三明治结果就与"不切分直接算"一致。

**操作步骤**（示例代码，CPU 可运行；接 4.1.4 的两个函数）：

```python
import torch.nn.functional as F

def ulysses_sandwich(x_list, seq_splits, head_splits):
    """x_list[r]: (b, s_r, n_global, 3*head_dim)。中间夹一个本地注意力。"""
    y_list = scatter_head(x_list, head_splits)          # mode 0
    out = []
    for y in y_list:                                     # 每"rank"：完整序列 × 自己的头段
        q, k, v = y.chunk(3, dim=-1)
        a = F.scaled_dot_product_attention(
            q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
        ).transpose(1, 2)                                # (b, s_global, n_local, head_dim)
        out.append(a.contiguous())
    return gather_head(out, seq_splits)                  # mode 1

torch.manual_seed(1)
b, d, ws = 1, 16, 4
seq_splits, head_splits = [5, 5, 5, 5], [2, 2, 2, 2]
n_global = sum(head_splits)
# 构造全局张量再切开，保证两条路径看到同一份数据（与 example 的做法一致）
full = torch.randn(b, sum(seq_splits), n_global, 3 * d)
x_list = [full[:, sum(seq_splits[:r]):sum(seq_splits[:r+1])] for r in range(ws)]

local_out = ulysses_sandwich(x_list, seq_splits, head_splits)
sandwiched = torch.cat(local_out, dim=1)                # 按 rank 序拼回全局

q, k, v = full.chunk(3, dim=-1)                         # 参照：不切分，直接算
ref = F.scaled_dot_product_attention(
    q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
).transpose(1, 2)

print(torch.allclose(sandwiched, ref, atol=1e-5))
```

**需要观察的现象**：最后打印 `True`；若把 `local_out` 拼接时的 rank 顺序打乱（比如逆序 `cat`），应当 `False`——全局序列顺序必须是 rank 序。

**预期结果**：float32/CPU 下两条路径的数值差远小于 `atol=1e-5`，打印 `True`。**待本地验证**。（用 `allclose` 而非 `equal` 的理由见练习 4.1-3。）

#### 4.2.5 小练习与答案

**练习 1**：如果把三明治中间的 SDPA 换成一个**跨头混合**的算子（比如把头轴 `reshape` 进 batch 轴后做一次混合所有头的线性层），正确性会怎样？

**答案**：会悄悄出错。mode 0 之后每个 rank 只有 `n_local` 个头，任何跨头混合都只能在本地这份头上算，缺少其他 rank 的头。三明治的正确性前提恰是"被夹住的算子按头独立"——这正是真实注意力满足、而随意一个层未必满足的性质。

**练习 2**：为什么例子在 mode 1 之前调用 `attn.contiguous()`（[examples/dit_attention.py:62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L62)）？

**答案**：SDPA 输出经过 `transpose(1, 2)` 后是非连续视图，而 `all_to_all_4d` 要求连续内存（库内部会 `.contiguous()`，见 [docs/api.md:49-50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L49-L50)）。显式写出使数据流清晰，也避免依赖库的隐式整理。

**练习 3**：mode 0 之后每 rank 的显存里激活是变大了还是变小了？

**答案**：形状从 `(b, s_local, n_global, 3d)` 变为 `(b, s_global, n_local, 3d)`，元素总数相同（偶均分时每 rank 元素数不变；不均匀时各 rank 略有差异）。所以三明治本身不省注意力输入的激活显存——它省的是**计算**（每头摊到 `ws` 份里）并保证通信量最小（每 rank 只搬 `1/ws` 全量）。省激活显存靠的是序列切分本身（每 rank 只需为 `s_local` 段保存层间激活）。

### 4.3 qkv 打包：一次握手搬完三份张量

#### 4.3.1 概念说明

注意力需要 q、k、v 三份张量，朴素做法是各做一次交换——**六次**集合调用（每份 mode 0 + mode 1）。但观察三份张量的形状：都是 `(b, s_local, n_global, d)`。把它们沿最后一维拼成 `(b, s_local, n_global, 3d)`，两次交换就能搬完全部字节。

为什么"字节相同却更快"？因为一次集合调用的开销不只是带宽：库的每次调用都包含两道屏障内核与 copy-out（[docs/design.md:28-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L28-L29)），屏障是所有 rank 的会合点，吃的是**延迟**而非带宽。三次独立交换 = 每层 6 次调用；打包后 = 2 次调用。同样的字节，握手次数降到 1/3，且更大的单次消息对带宽利用更友好。quickstart 的原话（[docs/quickstart.md:27-28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L27-L28)）："one call moves the same bytes with one handshake instead of three"。

注意"打包进最后一维"这个位置选择的正确性：mode 0/1 只重排前三维（batch、序列、头），最后一维 `d` 整体随行移动——所以 `3d` 与 `d` 在交换语义下没有区别，`chunk(3, dim=-1)` 在交换前后都合法。

#### 4.3.2 核心流程

```
投影层输出 (b, s_local, n_global * 3d)
   └─ view → (b, s_local, n_global, 3d)     # 纯视图，零拷贝
        └─ all_to_all_4d(mode=0) → (b, s_global, n_local, 3d)
             └─ chunk(3, dim=-1) → q, k, v 各 (b, s_global, n_local, d)
                  └─ SDPA → (b, s_global, n_local, d)
                       └─ all_to_all_4d(mode=1) → (b, s_local, n_global, d)
```

对比朴素方案：

| 方案 | 每层集合调用数 | 每层搬运字节（每 rank） |
| --- | --- | --- |
| q、k、v 各自交换 | 6 | \( 6 \cdot b\, s_{\text{local}}\, n_{\text{global}}\, d\, e \) |
| 打包成 `3d` | 2 | \( 2 \cdot b\, s_{\text{local}}\, n_{\text{global}}\, (3d)\, e \)（相同） |

#### 4.3.3 源码精读

**① example 的投影与打包。**

[examples/dit_attention.py:48-50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L48-L50)

```python
# (b, s_local, n_global, 3 * head_dim) -- q, k and v packed into the last axis so the two
# collectives below stay two collectives.
qkv = self.qkv(x).view(b, s_local, self.n_global, 3 * self.head_dim)
```

投影层直接输出 `3 * n_global * head_dim` 个通道（构造见 [examples/dit_attention.py:42](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L42)），`view` 把它重排成 `(b, s_local, n_global, 3*head_dim)`——一次 `view`，零拷贝。文件 docstring 强调这"比看起来更重要"（[examples/dit_attention.py:15-17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L15-L17)）。

**② 交换后拆包。**

[examples/dit_attention.py:55-59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L55-L59)：`q, k, v = qkv.chunk(3, dim=-1)` 把最后一维三等分。`chunk` 产生视图，随后 SDPA 的 `transpose` 也是视图——拆包本身同样零拷贝。

**③ quickstart 的通用建议。**

[docs/quickstart.md:27-28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L27-L28)："Pack q, k and v into one collective when you can: the last dim is just `3 * head_dim`, and one call moves the same bytes with one handshake instead of three."——"when you can"是提醒：能否打包取决于你的投影结构是否允许三份共享一次交换（本例共享同一个输入投影，天然可以）。

#### 4.3.4 代码实践

**实践目标**：用计数器量化"打包减少的是调用次数、不是字节数"。

**操作步骤**（示例代码，接 4.1.4 的函数与数据）：

```python
calls, moved = 0, 0
def traced_scatter(inputs, head_splits):
    """inputs: 每个 rank 一份同形状张量。统计调用次数与该次交换搬动的元素数。"""
    global calls, moved
    calls += 1
    moved += sum(x.numel() for x in inputs)
    return scatter_head(inputs, head_splits)

n_global = sum(head_splits)
q_per_rank = [torch.randn(b, s, n_global, d) for s in seq_splits]   # 一次 q 的交换要 ws 份
k_per_rank = [torch.randn(b, s, n_global, d) for s in seq_splits]
v_per_rank = [torch.randn(b, s, n_global, d) for s in seq_splits]

# 方案 A：q、k、v 各自交换 —— 三次调用
calls = moved = 0
traced_scatter(q_per_rank, head_splits)
traced_scatter(k_per_rank, head_splits)
traced_scatter(v_per_rank, head_splits)
print("独立交换:", calls, "次调用,", moved, "元素")

# 方案 B：沿最后一维打包成 3d，一次交换 —— 一次调用
packed_per_rank = [torch.cat([q, k, v], dim=-1) for q, k, v in zip(q_per_rank, k_per_rank, v_per_rank)]
calls = moved = 0
traced_scatter(packed_per_rank, head_splits)
print("打包交换:", calls, "次调用,", moved, "元素")
```

**需要观察的现象**：两种方案的 `moved` 元素数完全相等（`torch.cat` 沿最后一维打包不增不减字节）；调用次数是 3 对 1。换算到真实库：每层 mode 0 + mode 1 共 2 次调用（打包）对 6 次（不打包），每次调用含两道屏障（[docs/design.md:28-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L28-L29)），屏障次数从 12 降到 4。

**预期结果**：打印显示元素数相等、调用次数 3:1。**待本地验证**。（有多卡环境时，更真实的量化是把 [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L50-L53) 的打包 `view` 改成三次独立 `all_to_all_4d` 用 CUDA 事件计时对比——那是 u2-l4 的实践任务。）

#### 4.3.5 小练习与答案

**练习 1**：为什么把 q/k/v 拼在**最后一维**，而不是拼在序列维或头维？

**答案**：mode 0/1 的重排只作用于 batch、序列、头三维，最后一维 `d` 是每行整体搬运的最小单元（库的对齐检查也是按 `d * elem_size` 计，见 [README.md:107-108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L107-L108)）。拼在最后一维，交换前后都能用零拷贝视图（`view` / `chunk`）打包拆包；拼在序列维或头维则会改变那些轴的分片含义，破坏 mode 的形状契约。

**练习 2**：三次独立交换与一次打包交换，带宽层面的字节数相同，那打包省下的开销具体是什么？

**答案**：省的是每调用的固定成本——两道屏障（全体 rank 的会合延迟）、调用的校验与发射开销；同时单条消息变大 3 倍，对带宽的利用率更好（大消息更容易打满链路，这也是本项目"面向每 rank 数百 MB 大消息"定位的一部分，见 u1-l1）。

**练习 3**：注意力输出 `attn` 是 `(b, s_global, n_local, d)`，为什么 mode 1 之前不把它和别的张量打包？

**答案**：可以打包的对象得是"同一时刻、同方向、同形状"的交换。输出侧只有注意力结果一份（q/k/v 已在输入侧消费掉），没有可拼的同伴；而且 mode 1 之后它要进输出投影，拼了还得拆。打包的收益出现在"多份张量共享同一次交换"的场合——这里是输入侧的 q/k/v。

### 4.4 SPMD 调用契约：所有 rank 发出相同的形状序列

#### 4.4.1 概念说明

SPMD 意味着每个 rank 跑同一段程序。集合通信进一步要求：**会合点必须对齐**——每个 rank 到达某次集合调用时，其他 rank 也得到达同一次调用。对 fast-ulysses 来说，这条纪律具体化为"**每个 rank 必须发出相同的形状（和 dtype）序列**"。

这条契约的来源在库的内部设计里：第一次遇到新形状的调用会分配一块对称内存窗口，而这个分配是集合操作——所有 rank 必须一起到齐才能完成（[docs/quickstart.md:92-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L92-L94)）。若某个 rank 走了别人没走的分支（比如 `if rank == 0` 里多发了一次调用），它会在集合点等永远不来的同伴——**挂起，不报错，不超时**（[docs/api.md:8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L8) 原话："Violating these hangs the group. Nothing raises and nothing times out."）。

好消息是：这**不是新纪律**。`torch.distributed` 的任何集合通信本来就要求同样的对齐，quickstart 特别指出"这是 `torch.distributed` 集合通信已经要求的同一种纪律"（[docs/quickstart.md:94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L94)）。在 Ulysses 的典型用法里它自动满足：所有 rank 跑同一个模型同一层，自然发出相同的形状序列。

#### 4.4.2 核心流程

契约清单（摘自 [docs/api.md:6-20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L6-L20)，本讲关注第 1 条，其余在后续讲义展开）：

1. **每个 rank 必须发出相同的形状序列**——新形状的首次调用触发集合的窗口分配。
2. 构造、`empty_output()`、`destroy()` 都是集合操作。
3. 每个 rank 必须发出相同的 **dtype 序列**（窗口按 `(role, dtype)` 分配，u4-l2）。
4. autograd 下，各 rank 的引擎必须以相同顺序到达 backward 节点（u6-l3）。
5. 一个缓冲同一时刻只服务一个调用（u4-l3）。

```
健康路径：                             病理路径：
rank0: a2a(S1) a2a(S2) a2a(S1)         rank0: a2a(S1) a2a(S1) a2a(S2)
rank1: a2a(S1) a2a(S2) a2a(S1)         rank1: a2a(S1) ------  a2a(S2)
                                       rank1 的第 2 次调用等 rank0 的第 2 次调用
                                       ——双方在窗口分配处互相等待 → 挂起
```

#### 4.4.3 源码精读

**① Python 侧的契约陈述。**

[python/fast_ulysses/group.py:35-41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L35-L41)

```python
class UlyssesGroup:
    """Ulysses all-to-all over NVLink, moved by the copy engines.

    Construction is collective over ``process_group``. Windows are allocated on the first call that
    needs one and cached, so every rank must issue the same sequence of shapes. Contract:
    docs/api.md.
    """
```

类 docstring 一句话讲清因果链：窗口在首次需要时分配并缓存 → 所以每个 rank 必须发出相同的形状序列。

**② 方法 docstring 的重复强调。**

[python/fast_ulysses/group.py:120-127](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L120-L127)：`all_to_all_4d` 的 docstring 先复述 mode 语义，末尾再次以 "Collective: every rank must issue the same sequence of shapes." 收尾——这是 Python 层唯一能看到的行为契约，参数校验之外的集合纪律由文档与使用者共同维护。

**③ README 的提醒位置。** 快速开始代码块的最后一句就是它（[README.md:74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L74)）："Every rank must issue the same sequence of shapes."——放在读者第一次接触 API 的地方，可见其分量。

**④ "什么会让你挂起"一节。**

[docs/quickstart.md:90-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L90-L94)

> Every rank must issue the same sequence of shapes. The first call with a new shape allocates a window, and that allocation is collective — if one rank takes a branch the others do not, it waits there.

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——把契约从三处文档锚点串起来，并对三个"病理分支"做纸面推演。

**操作步骤**：

1. 依次打开上面 4.4.3 的三处引用（`group.py:35-41`、`group.py:120-127`、`api.md:6-20`），确认三处措辞一致。
2. 对下面三段代码，各自回答：哪个 rank 在哪里等谁？会不会有报错？

```python
# 场景 A：rank 间分支不同
if dist.get_rank() == 0:
    group.all_to_all_4d(x, mode=0)          # 只有 rank 0 发出

# 场景 B：形状序列不同（rank 0 多换了一个更大的形状）
group.all_to_all_4d(x_small, mode=0)
if dist.get_rank() == 0:
    group.all_to_all_4d(x_big, mode=0)      # 触发一次新的窗口分配

# 场景 C：所有 rank 都发，但 dtype 不同
group.all_to_all_4d(x_fp16 if dist.get_rank() % 2 else x_fp32, mode=0)
```

**需要观察/推演的现象**：三个场景都是挂起而非报错；挂起的位置不同——A 中 rank 0 在集合调用的屏障处等待其他 rank 进入**同一次**调用；B 中 rank 0 在新窗口的集合分配处等待；C 中窗口按 `(role, dtype)` 分配（`group.py:39` 所述"形状序列"的 dtype 对应物），两个阵营各自等待对方。

**预期结果**：纸面推演即可完成；如有多卡环境可实测 A（**注意**：实测会真挂起，需 `Ctrl-C` 或 kill 清理残留的自旋内核进程，这正是 u9-l1 要讲的 torchrun 超时清理问题的由来）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：下面的用法违反契约吗？`for x in batch: group.all_to_all_4d(x, mode=0)`（每个 rank 的 `batch` 长度相同）。

**答案**：不违反。所有 rank 发出相同**序列**的调用——循环长度相同、每步形状相同即可。契约要求的是序列对齐，不是"只能调用一次"。

**练习 2**：所有 rank 用**相同的** `seq_splits` / `head_splits` 列表（而不是各自只传自己那份），这和 SPMD 契约有什么关系？

**答案**：splits 描述的是**整个组的几何**（哪个 rank 拿哪段），不是本 rank 的私有参数，因此必须全 rank 一致（[docs/api.md:88-90](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L88-L90)：pass both or neither, identical on every rank）。这既是正确性要求（每个 rank 都要为所有 peer 计算寻址），也和 SPMD 同源：同一份程序当然读同一份组几何配置。

**练习 3**：为什么库不干脆把"形状序列不一致"检测出来报个错？

**答案**：检测本身需要通信（rank 间对账），而对账又是一次需要全体参与的集合操作——用它来守护其他集合操作，鸡生蛋问题。挂起是集合通信的通用失败模式，`torch.distributed` 亦然，所以项目选择把它写成文档纪律（api.md 的 Collective contract 一节）而非运行时检查。理解这一点对阅读第 9 单元的对抗性测试（故意制造竞态与挂起）很重要。

## 5. 综合实践

**任务：在单进程里写一个"迷你 Ulysses"，把本讲四个模块串起来。**

要求（示例代码骨架，CPU 可运行）：

1. **基础件**（4.1）：实现 `scatter_head` / `gather_head`（4.1.4 已给出），断言 `gather_head(scatter_head(xs))` 位精确还原——这就是 mode 0/1 互逆。
2. **三明治**（4.2）：仿照 [examples/dit_attention.py:45-63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L45-L63) 写 `ulysses_attention(x_list, ...)`：mode 0 → 每 rank 本地 SDPA → mode 1；用 4.2.4 的方法与"单进程全量"参照做 `allclose` 双校验（输出**和**输入梯度——给 `x_list` 设 `requires_grad`，两侧 `sum().backward()` 后比较梯度，完整镜像 [examples/dit_attention.py:96-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L96-L116) 的两条比较）。
3. **打包**（4.3）：输入用 `3 * head_dim` 最后一维的打包形式（q/k/v 在投影后 `view` 进来），确认集合调用次数是 2 而不是 6。
4. **契约反思**（4.4）：在代码注释里回答——你的单进程模拟里，"所有 rank 发出相同形状序列"这条契约对应什么？（答案：你给列表里每个元素发的调用序列完全相同；真实多进程下这点由 SPMD 程序结构保证，而任何 `if rank == ...` 的条件调用都会破坏它。）

**验收标准**：往返位精确 `True`；三明治输出与梯度 `allclose` 通过（float32/CPU 建议先试 `atol=1e-5`）；调用计数 2。全部**待本地验证**。通过后，建议在装好库的环境里跑一次真实版：`torchrun --nproc_per_node=<N> examples/dit_attention.py`，把它打印的 `output max |diff|` / `d/dinput max |diff|` 与你单进程模拟的误差量级对照（bf16 下容差是 `5e-2`，见 [examples/dit_attention.py:113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L113)）。

## 6. 本讲小结

- **mode 0 / mode 1 是一对互逆的形状变换**：mode 0 把 `(b, s_local, n_global, d)` 换成 `(b, s_global, n_local, d)`——每个 rank 发出头切片、沿序列轴收回；mode 1 反向。往返是纯搬运，位精确。
- **三明治结构**：mode 0 → 注意力 → mode 1。注意力按头独立的性质让"完整序列 × 头分片"布局下的计算完全本地，计算量摊薄 `ws` 倍，通信量每 rank 只有全量的 `1/ws`。
- **长序列下通信是关键路径**：通信字节随 `s` 线性增长且每层固定付两次，不被 `ws` 摊薄；这是整个项目存在的理由。
- **qkv 打包**：三份 `(b, s, n, d)` 拼成 `(b, s, n, 3d)`，字节不变、握手次数从每层 6 次降到 2 次（屏障从 12 道降到 4 道）。
- **SPMD 契约**：每个 rank 必须发出相同的形状/dtype 序列，根源是新形状首次调用触发的窗口分配是集合操作；违反的后果是挂起而非报错——这与 `torch.distributed` 的既有纪律同源。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：安装、构建与第一次运行——把本讲读过的 `examples/dit_attention.py` 在真实多卡上跑起来，亲眼看它打印的双路校验输出。
- **u2 单元**：`UlyssesGroup` 的构造细节、`all_to_all_4d` 的完整形状契约与错误检查（本讲只用了契约的一条）、不均匀分片的实战（u2-l3 会解释为什么"丢掉 padding"在基线里值 5-11% 的耗时）。
- **源码预读**：有兴趣可以提前看 [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L260-L296) 的 `test_matches_torch_all_to_all_single`——它把本讲的语义锚定到 `all_to_all_single` 上，是通往 u3（布局计划如何变成带步长拷贝）的桥梁。
