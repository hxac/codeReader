# 实例精读：序列并行 DiT 注意力块

## 1. 本讲目标

前面几讲我们把 `all_to_all_4d` 的形状契约、错误检查与不均匀分片逐一拆开了。本讲换一个视角：**站在使用者一边**，逐段精读 [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L1-L137) ——一个完整的、可训练的序列并行注意力块，看这些契约如何拼成一个真实模块。

读完本讲，你应该能：

1. 读懂 `UlyssesAttention` 的每一层职责：它是一个**普通的 `nn.Module`**，没有 `autograd.Function` 包装，两次 all-to-all 包夹一次 SDPA。
2. 解释 qkv 打包进最后一维（`3 * head_dim`）的动机与正确性：为什么是「一次握手搬三份张量」，以及为什么这种折叠是安全的、而另一种看似等价的折叠是陷阱。
3. 复现示例的双校验方法学：与「无序列并行的单卡参照」比较输出**和**输入梯度，理解为什么用 `allclose` 而不是 `equal`、容差为什么是 `5e-2`。
4. 说出五个优化器步想证明什么、不证明什么。
5. 完成综合实践：把一次打包调用拆成对 q、k、v 的三次独立调用，验证输出等价，并用 CUDA 事件或 `_timed` 量化「字节相同、握手次数不同」带来的时间差。

## 2. 前置知识

本讲默认你已读过 u1-l2（Ulysses 原理）、u2-l1（Group 构造）、u2-l2（形状契约）。三个本讲会用到的结论，先摆出来：

| 结论 | 出处 | 一句话回顾 |
| --- | --- | --- |
| mode 0 把 \((b, s_{\text{local}}, n_{\text{global}}, d)\) 变成 \((b, s_{\text{global}}, n_{\text{local}}, d)\)，mode 1 是严格逆变换 | u1-l2 / u2-l2 | 两次交换都是纯字节搬运，往返位精确 |
| 一次同步调用的内部链路是 prepare → 屏障 → 传输 → 屏障 → copy_out | u1-l4 | **每次调用付两道与消息大小无关的握手屏障** |
| dtype 白名单 + \(d \times \text{elem\_size}\) 必须是 16 的倍数 | u2-l2 | bf16 的 elem_size 是 2 字节 |

再补两个本讲特有的术语：

- **SDPA**（scaled dot-product attention）：`torch.nn.functional.scaled_dot_product_attention`，计算 \(\text{softmax}(QK^\top/\sqrt{d_k})V\)。关键性质是**按头独立**——每个头在自己的 \((s, d)\) 子空间里做注意力，头与头之间不交换数据。整块「三明治」结构之所以成立，靠的就是它（u1-l2 讲过）。
- **DiT**（Diffusion Transformer）：视频/图像扩散模型用的 Transformer 变体，长序列训练是其典型场景，也是本库的主要目标负载。

## 3. 本讲源码地图

| 文件 | 本讲中的角色 |
| --- | --- |
| [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L1-L137) | 主角：137 行完整示例——一个模块类、一个单卡参照、双校验、五个优化器步 |
| [docs/migration.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L1-L243) | 从 DeepSpeed / sglang / yunchang 迁移的视角；本讲引用它的「结构性差异」「qkv 打包」「autograd 无需包装」三段 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L111-L119) | Python 门面：`all_to_all_4d` 签名（L111-L119）与 `_timed` 阶段计时（L251-L265），综合实践要用 |

## 4. 核心概念与源码讲解

### 4.1 模块结构：一个普通的 `nn.Module`

#### 4.1.1 概念说明

DeepSpeed、sglang、yunchang 都交付一个**注意力模块**（`DistributedAttention` / `UlyssesAttention` / `LongContextAttention`），由它持有 q/k/v 交换、注意力调用和输出交换。fast-ulysses 只交付**集合通信本身**——你用两次调用加上自己的注意力内核替换掉那个模块。这是迁移文档开门见山强调的结构性差异：[docs/migration.md:6-10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L6-L10)，而 `examples/dit_attention.py` 就是这种形态的完整样例（[docs/migration.md:157-163](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L157-L163)）。

这个模块最值得注意的「结构」是它**没有结构**：没有 `autograd.Function` 包装、没有手写 backward。因为 `all_to_all_4d` 在 C++ 里就是可微的，backward 是另一个 mode，包装器被「删除」而不是「翻译」过来（[docs/migration.md:149-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L149-L155)）。所以它长得和任何一个单卡注意力块一模一样——这正是示例 docstring 的第一条说明（[examples/dit_attention.py:5-8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L5-L8)）。

#### 4.1.2 核心流程

一次前向的数据流（形状以默认配置 ws=8 为例，常量在 [examples/dit_attention.py:29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L29)：`B=1, S_LOCAL=512, HEADS_PER_RANK=4, HEAD_DIM=128`，故 \(n_{\text{global}} = 4 \times 8 = 32\)，隐藏维 \(= 32 \times 128 = 4096\)）：

```text
x                    (1, 512, 4096) = (b, s_local, n_global·head_dim)   ← 调用方已有的序列分片
 ├─ Linear qkv + view → (1, 512, 32, 384)      打包：最后一维 = 3·head_dim
 ├─ all_to_all_4d(mode=0) → (1, 4096, 4, 384)  交换：序列分片 ⇄ 头分片
 ├─ chunk(3, dim=-1) → q/k/v 各 (1, 4096, 4, 128)
 ├─ SDPA（按头独立，全序列本地算）→ (1, 4096, 4, 128)
 ├─ all_to_all_4d(mode=1) → (1, 512, 32, 128)  换回：头分片 ⇄ 序列分片
 └─ Linear proj → (1, 512, 4096)               输出仍是「本 rank 的序列分片、全部头」
```

输入输出契约是对称的：进出都是调用方自己那段序列分片、全部的头，模块内部（两次集合通信之间）才看到全序列＋头分片——类 docstring 写明了这一点（[examples/dit_attention.py:33-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L33-L37)）。

#### 4.1.3 源码精读

构造函数只做三件事：记下 group、把两个投影层建成普通 `nn.Linear`（[examples/dit_attention.py:39-43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L39-L43)）。注意 `qkv` 这一个 Linear 的输出宽度是 \(3 \cdot n_{\text{global}} \cdot \text{head\_dim}\)——打包在权重层面就定好了，后面的 `view` 只是 reshape，不搬运数据（[examples/dit_attention.py:50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L50)）。

forward 主体在 [examples/dit_attention.py:45-63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L45-L63)，每一步旁边的注释就是文档：

- mode 0 调用带注释「Differentiable; its backward is mode 1」（[L52-L53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L52-L53)）；
- SDPA 需要 \((b, \text{heads}, s, d)\) 布局，所以前后各一次 `transpose(1, 2)`（[L56-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L56-L59)）；
- mode 1 之前显式写了 `.contiguous()`（[L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L62)）——其实算子内部本来就会做连续化，自己写一遍不改变任何事（[docs/migration.md:140-142](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L140-L142)），这里写出来是为了让 L63 的 `reshape` 读起来显然合法。

另外注意 `main()` 里构造 group 用了 `require_nvlink=False`（[examples/dit_attention.py:85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L85)）。u2-l1 讲过该参数的语义；migration.md 也说明这个开关「是为测量那种拓扑准备的，不是为在其中运行」（[docs/migration.md:219-221](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L219-L221)）。示例没有注释这一选择，一个合理的解读是：演示脚本希望在任何单机拓扑上都能跑起来，不被 NVLink 检查拦住；生产训练应保持默认 `True`。

#### 4.1.4 代码实践：形状追踪

1. **实践目标**：把 4.1.2 的形状表在真实运行里逐行对上号。
2. **操作步骤**：把 `examples/dit_attention.py` 拷贝一份（例如 `my_dit.py`，不要改动仓库原文件），在 `forward` 里插入三行打印（示例代码）：

   ```python
   # 示例代码：加在 forward 内部的形状追踪
   r = dist.get_rank()
   print(f"[r{r}] x        {tuple(x.shape)}")
   print(f"[r{r}] qkv pre  {tuple(qkv.shape)}")   # mode 0 之前
   print(f"[r{r}] qkv post {tuple(qkv.shape)}")   # mode 0 之后（qkv 被重新赋值过）
   ```

   然后用任意进程数运行（ws 不必是 8，任何 \(N \ge 1\) 都行，`n_global` 会自动变为 \(4N\)）：

   ```bash
   torchrun --nproc_per_node=2 my_dit.py
   ```

3. **需要观察的现象**：ws=2 时应看到 `qkv pre (1, 512, 8, 384)`、`qkv post (1, 1024, 4, 384)`——序列轴从 512 变 1024、头轴从 8 变 4，两轴相乘不变。
4. **预期结果**：形状表逐行吻合；`x` 与最终返回值形状相同。无 GPU 时无法用 nccl 后端运行，此实践**待本地验证**。

#### 4.1.5 小练习与答案

1. **问**：为什么模块的输出是 `(b, s_local, n_global·head_dim)`，而不是含全序列的形状？
   **答**：mode 1 已经把头分片换回了序列分片；模块的输入输出契约是对称的——调用方自始至终只持有自己的序列分片和全部的头，整条序列只在两次集合通信**之间**存在（[examples/dit_attention.py:33-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L33-L37)）。
2. **问**：一个 DeepSpeed 用户要把 `DistributedAttention(local_attn, sp_group)` 迁移过来，模块层面要补什么、删什么？
   **答**：补——对 `local_attn`（自己的注意力内核）的显式调用和两次 `all_to_all_4d`；删——`_SeqAllToAll` 那个 `autograd.Function` 包装，`all_to_all_4d` 本身可微，包装器直接删除（[docs/migration.md:48-56](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L48-L56)）。
3. **问**：为什么 `UlyssesAttention` 在构造时保存 group，而不是每次 forward 新建一个？
   **答**：group 持有对称内存窗口、plan 缓存和 comm 流，构造又是集合操作；正确纪律是建一次、长期持有（[docs/migration.md:233-238](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L233-L238)）。

### 4.2 qkv 打包：一次握手搬三份张量

#### 4.2.1 概念说明

注意力需要 q、k、v 三份张量走同一趟交换。最直接的写法是对三者各调一次集合通信；本示例（以及 migration.md 推荐的形态）把三份沿最后一维拼成一个 `3 * head_dim` 的最后轴，**一次调用搬完**。示例 docstring 的收尾段专门强调这一点（[examples/dit_attention.py:15-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L15-L16)）：one handshake instead of three, for the same bytes。迁移表里对应的一行也是同一个意思：DeepSpeed 的三次 `_SeqAllToAll.apply`（q、k、v 各一次）在这里变成 `3 * head_dim` 最后轴上的一次调用（[docs/migration.md:52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L52)）。

为什么「字节不变却更快」？因为成本分两类：

- **按字节计费的带宽项**：传输 \(b \cdot s_{\text{local}} \cdot n_{\text{global}} \cdot 3d \cdot \text{elem}\) 字节——拼与不拼总量相同（ws=8 默认配置下是 \(1 \times 512 \times 32 \times 384 \times 2 = 12{,}582{,}912\) 字节 ≈ 12.6 MB）。
- **按次数计费的延迟项**：每次调用两道握手屏障 + 一次 copy_out，与消息大小基本无关。

打包省的是第二类。粗略模型：

\[
T_{\text{调用}} \approx 2\,T_{\text{屏障}} + T_{\text{传输}}(\text{字节}) + T_{\text{copy\_out}}
\]

输入侧打包 1 次调用付 \(2\) 道屏障，拆开 3 次付 \(6\) 道；输出侧两种写法都只有一份注意力输出，都是 1 次调用。整层合计：**2 次调用 / 4 道屏障 vs 4 次调用 / 8 道屏障**，字节完全一样。综合实践（第 5 节）会实测这个差。

正确性依赖一个关键事实：**`all_to_all_4d` 只重排 s 轴与 n 轴，从不混合最后一维**。因此「先交换后按最后一维 chunk」与「先按最后一维拆开再各自交换」取出的是同一组值——最后一维的任何切分都与交换可交换。这保证了 chunk 出的 q/k/v 与三次独立交换的结果逐位相同。

折叠的位置也讲究。把 \((b, s, 3, n, d)\)（yunchang 的 5D 形态）免费 reshape 成 \((b, s, 3n, d)\) 看起来同样「打包了三份」，但 migration.md 明确标记它是陷阱：偶均分会沿合并后的 \(3n\) 轴切出**连续块**，每个 rank 收到的是「q 的某几个头 + k 的某几个头 + v 的某几个头」的混合物，而不是「同一批头的完整 q/k/v」（[docs/migration.md:143-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L143-L145)）。折叠进**最后一维**则不动 \((s, n)\) 两轴，rank 收到的仍是完整的头分片。示例因此让投影层直接以 \(3 \cdot n \cdot d\) 的输出顺序「上游就地发射」这个布局（[docs/migration.md:138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L138)）。

对齐也顺手核对：u2-l2 的规则是 \(d \times \text{elem\_size}\) 须为 16 的倍数；这里参与交换的最后一维 \(d = 3 \times 128 = 384\)，bf16 下 \(384 \times 2 = 768\)，\(768 \bmod 16 = 0\)，合法。

#### 4.2.2 核心流程

```text
Linear 输出 (b, s_local, n_global·3·head_dim)          ← 权重层面已经按 [头][3·head_dim] 排好
  └─ view → (b, s_local, n_global, 3·head_dim)          ← 纯 reshape，零拷贝
  └─ all_to_all_4d(mode=0)                              ← 一次握手搬三份
  └─ chunk(3, dim=-1) → q/k/v                           ← 交换缓冲上的跨步视图，零拷贝
```

#### 4.2.3 源码精读

- 打包与交换：[examples/dit_attention.py:48-53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L48-L53)——注释直说「q, k, v packed into the last axis so the two collectives below stay two collectives」（两次集合通信保持为两次），L50 的 `view` 完成打包，L53 一次 mode 0 搬完。
- 交换后拆包：[examples/dit_attention.py:55](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L55)——`chunk(3, dim=-1)` 拿到三个跨步视图，不复制数据。
- migration.md 推荐形态的等价片段：[docs/migration.md:32-42](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L32-L42)——与示例逐行对应，注释同样点出「two collectives stay two collectives」。

#### 4.2.4 代码实践：验证「最后一维切分与交换可交换」

这个实践**不需要 GPU、不需要本库**，纯 torch 在 CPU 上就能完成，同时复用你在 u1-l2 实践里写过的 mode 0 模拟。

1. **实践目标**：证明「先交换后 chunk」与「先拆开后交换」取出同一组值——这正是三次独立调用能替换打包调用的正确性依据。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：纯 CPU 验证最后一维切分与 mode 0 交换可交换
   import torch

   def mode0_shards(x, ws):
       """返回每个 rank 的 mode 0 输出：全序列 × 该 rank 的头分片。"""
       b, s, n, d = x.shape
       s_l, n_l = s // ws, n // ws
       outs = []
       for r in range(ws):
           seq_shards = [
               x[:, rr * s_l:(rr + 1) * s_l, r * n_l:(r + 1) * n_l, :]
               for rr in range(ws)
           ]                                   # 源 rank rr 的序列分片 × rank r 的头分片
           outs.append(torch.cat(seq_shards, dim=1))
       return outs

   ws, b, s, n, hd = 4, 1, 32, 8, 16
   x = torch.randn(b, s, n, 3 * hd)            # 打包形态：最后一维 3·hd

   a = mode0_shards(x, ws)                     # 路径 A：先交换
   a_q = [t.chunk(3, dim=-1)[0] for t in a]    #        后按最后一维取 q

   q, k, v = x.view(b, s, n, 3, hd).unbind(dim=3)
   b_q = mode0_shards(q.contiguous(), ws)      # 路径 B：先拆出 q，再交换

   assert all(torch.equal(p, c) for p, c in zip(a_q, b_q))
   print("chunk 与交换可交换：成立")
   ```

3. **需要观察的现象**：断言通过；两条路径对每个 rank 的 q 逐位相等。
4. **预期结果**：必然通过——交换是纯置换，且只作用于 s、n 两轴。这个「参照重放」的验证思路与 `test/test_plan.py` 用主机端参照核对 plan 的做法同源（u3-l3 会精读）。

#### 4.2.5 小练习与答案

1. **问**：若 `head_dim=100`、dtype 为 bf16，打包后的调用合法吗？不打包呢？
   **答**：都不合法。对齐约束作用于参与交换的最后一维：打包 \(3 \times 100 = 300\)，\(300 \times 2 = 600\)，\(600 \bmod 16 = 8 \ne 0\)；不打包 \(100 \times 2 = 200\)，\(200 \bmod 16 = 8 \ne 0\)。两者都会被 u2-l2 讲过的对齐检查以 `RuntimeError` 拒绝——打包与否不改变约束本身，只决定要检查几次。
2. **问**：`(b, s, 3, n, d) → (b, s, 3n, d)` 的免费 reshape 为什么是陷阱，而 `(…, 3·head_dim)` 为什么安全？
   **答**：前者把 3 折进了会被均分切开的 n 轴，每个 rank 收到的连续块里混着 q/k/v 各自的一部分头；后者只加宽不被切分的最后一维，rank 收到的仍是「同一批头 × 完整的 q/k/v 三段」（[docs/migration.md:143-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L143-L145)）。
3. **问**：ws=8 时，打包与不打包两种写法的整层调用次数、屏障次数各是多少？
   **答**：打包 = 输入侧 1 次 + 输出侧 1 次 = 2 次调用、4 道屏障；q/k/v 拆开 = 3 + 1 = 4 次调用、8 道屏障（每次调用 barrier_in + barrier_out 两道，见 [python/fast_ulysses/group.py:264](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L264) 的阶段名）。与 ws 无关。

### 4.3 参照对照：输出与梯度双校验

#### 4.3.1 概念说明

「交换是字节级置换」在单元测试里可以位精确，但一个真实的注意力块仍可能把张量接错轴——比如 mode 0 之后没 chunk 对位置、或转置转错轴，数据照样被搬运、测试照样「通过」。所以示例的校验不测集合通信本身，而是**测整个模块的行为**，并且测两样东西：前向输出和**输入梯度**。对照物是一个无序列并行的参照——同一个模块实例、同一份权重，一个 rank 上拿全序列算一遍（[examples/dit_attention.py:66-74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L66-L74) 的 `reference` 函数，与 `forward` 唯一的区别就是没有两次 all_to_all）。docstring 把这层用意说得很直白（[examples/dit_attention.py:10-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L10-L13)）。

双校验的三个方法学要点：

1. **同一份数据**：全局输入在各 rank 上用同一份随机数构造再切片，参照算的就是「并行路径实际消费的那份数据」（[examples/dit_attention.py:88-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L88-L94)）。
2. **同一份权重**：各 rank 同种子初始化保证权重跨 rank 一致（[L82](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L82)）；参照直接复用 `block` 实例，且比较发生在任何 `opt.step()` 之前，权重逐位相同。
3. **allclose 而非 equal**：两条路径喂给 SDPA 的批布局不同（参照一次算 \(n_{\text{global}}\) 个头，并行路径每 rank 算 \(n_{\text{local}}\) 个头），且并行路径的 q/k/v 是交换缓冲的跨步视图、参照的是密集张量，浮点归约顺序不同必有微小差异。代码注释原话："attention is not a permutation, and the two paths reduce in different orders"（[examples/dit_attention.py:99-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L99-L101)）。

#### 4.3.2 核心流程

`main()` 中校验段落的顺序（顺序本身有讲究）：

1. 并行路径前向 + 反向：`y_local = block(x_local)`、`y_local.sum().backward()`（[L96-L97](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L96-L97)），得到分片输出与 `x_local.grad`。
2. `dist.all_gather` 收集全部分片输出与全部分片梯度（[L102-L105](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L102-L105)）——用的是普通 `torch.distributed` 集合通信，校验基础设施不必也是被测物。
3. 参照路径前向 + 反向：`reference(block, x_global)`、`.sum().backward()`（[L107-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L107-L108)），得到 `y_ref` 与 `x_global.grad`。
4. rank 0 上沿序列轴 `cat` 后做两个最大绝对误差：输出误差与梯度误差，先 `.float()` 再相减，阈值为 `tol = 5e-2`（[L110-L116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L110-L116)）。

#### 4.3.3 源码精读

- 参照函数：[examples/dit_attention.py:66-74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L66-L74)——与 `forward` 逐行同构，只少两次 `all_to_all_4d`；`module.qkv`/`module.proj` 复用的是同一个模块的权重。
- 数据构造：[examples/dit_attention.py:88-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L88-L94)——`x_global` 先 `requires_grad_(True)`，再 `detach()` 切片后给 `x_local` 也加 `requires_grad_(True)`：两套叶子各自攒各自的梯度，互不干扰。
- 比较与容差：[examples/dit_attention.py:110-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L110-L116)——`.float()` 把 bf16 升到 float32 再求差；`tol = 5e-2` 旁的注释给出量纲依据："bfloat16 through attention plus two projections"（bf16 精度 + 注意力 + 两次投影的合理上限）。

#### 4.3.4 代码实践：ws=1 的退化运行

1. **实践目标**：在最小环境下跑通校验，并解释 ws=1 与 ws=8 两个误差数字的差别来自哪里。
2. **操作步骤**：单卡即可运行（nccl 后端仍需一块 GPU）：

   ```bash
   torchrun --nproc_per_node=1 examples/dit_attention.py
   ```

3. **需要观察的现象**：头部打印 `ws=1 s_global=512 n_global=4`；两行误差均应显示 `0.0000 OK`。
4. **预期结果**：ws=1 时两条路径执行的是**同一串算子、同一布局**（mode 0 在 ws=1 不改变形状，参照与并行路径的 chunk 都是同一缓冲上的同跨步视图），因此误差应精确为 0；ws≥2 时误差变为「非零但小于 5e-2」。两个数字对比正好把 4.3.1 的第 3 点（差异来自注意力归约顺序，而非交换本身）钉死。**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：为什么相减前要把两边都 `.float()`？
   **答**：bf16 只有约 3 位十进制有效数字，在 bf16 里做减法本身就会把误差舍入掉；升到 float32 再取最大绝对误差，得到的才是有意义的误差度量（[examples/dit_attention.py:111-112](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L111-L112)）。
2. **问**：参照为什么复用 `block` 实例，而不是同种子再 new 一个？
   **答**：比较的前提是权重逐位相同。复用实例（且比较先于任何 `opt.step()`）把这一点变成结构性保证，而不是靠「种子相同」这个间接条件；种子相同在 L82 解决的是另一个问题——各 rank 之间权重一致。
3. **问**：把判据改成 `torch.equal` 会怎样？
   **答**：几乎必然失败。交换本身位精确，但两条路径的 SDPA 头数批布局不同、q/k/v 一个跨步一个密集，浮点归约顺序不同产生微小差异——这正是注释选 allclose 的原因（[examples/dit_attention.py:99-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L99-L101)）。

### 4.4 训练循环：五个优化器步

#### 4.4.1 概念说明

校验回答的是「一次前向 + 一次反向对不对」；训练循环回答的是另一个问题——**「能训练吗」**。一个集合通信可以在单测里位精确，却仍被接进错误的轴，导致梯度流断掉或炸掉，只有真实的优化器步能暴露这类问题。docstring 的最后一句话就是这个意思（[examples/dit_attention.py:12-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L12-L13)）。

循环本身平平无奇：AdamW、固定随机目标、5 步 `mse_loss`。两个不平凡的点：

- **梯度自动穿过两次集合通信**。`loss.backward()` 触发 mode 1 的 backward（= mode 0）和 mode 0 的 backward（= mode 1），两次反向交换由 C++ 算子的 autograd 公式自动完成——这正是 4.1 说「没有包装器」的另一面。数学细节（为什么 vjp 是逆排列、splits 为何不交换）留到 u6-l3。
- **loss 是 rank 本地的**。每个 rank 的 `mse_loss` 算的是自己序列分片对同一份随机目标的误差；各 rank 数字不同、也不应相同。这段循环证明「模块在训练」，不产生任何全局指标（[examples/dit_attention.py:126-129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L126-L129) 的注释明说 ranks are not meant to agree）。

#### 4.4.2 核心流程

```text
构建 AdamW(block.parameters(), lr=1e-3) 与固定 target
循环 5 次:
    zero_grad(set_to_none=True)
    loss = mse_loss(block(x_local), target)   ← 两次正向交换
    loss.backward()                            ← 两次反向交换（另一个 mode）
    opt.step()
    rank 0 打印本 rank 分片的 loss
最后: block.group.destroy() → dist.destroy_process_group()   ← 顺序不可换
```

#### 4.4.3 源码精读

- 循环本体：[examples/dit_attention.py:118-129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L118-L129)——`target = torch.randn_like(y_local)` 在循环**外**创建并固定，模块拟合的是这一个确定目标，loss 才应该单调下降。
- 收尾顺序：[examples/dit_attention.py:131-132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L131-L132)——`destroy()` 是集合操作（u2-l1：先同步 comm 流、再组内 barrier），必须先于 `dist.destroy_process_group()`，否则在 group 还活着时拆掉它脚下的进程组。

#### 4.4.4 代码实践：确认梯度在流动

1. **实践目标**：亲眼确认反向传播确实穿过了两次集合通信。
2. **操作步骤**：在你拷贝的 `my_dit.py` 里，把 `y_local.sum().backward()` 之后加两行（示例代码）：

   ```python
   # 示例代码：检查输入梯度穿过了两次集合通信
   assert x_local.grad is not None, "梯度没有穿过 all_to_all"
   print(f"[r{rank}] |x_local.grad| = {x_local.grad.norm().item():.4f}")
   ```

   运行 `torchrun --nproc_per_node=<N> my_dit.py`。
3. **需要观察的现象**：断言不触发；5 步 loss 大体下降（模块在拟合固定目标）；各 rank 的梯度范数不同。
4. **预期结果**：若把两次 `all_to_all_4d` 误接到错误的轴上，最典型的症状就是 `x_local.grad` 为 `None` 或全 0——这个断言是最便宜的烟雾测试。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：`loss.backward()` 时两次 `all_to_all_4d` 各自的反向发生了什么？
   **答**：mode 0 的 backward 是 mode 1、mode 1 的 backward 是 mode 0，splits 原样传递不交换——由算子的 autograd 公式自动执行，示例里的两行注释（[L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L52)、[L61](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L61)）就是全部需要手写的文档（另见 [docs/migration.md:149-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L149-L155)；推导在 u6-l3）。
2. **问**：rank 0 打印的 loss 各 rank 相同吗？应该相同吗？
   **答**：不同、也不应相同。loss 是本 rank 序列分片对固定目标的 MSE，分片内容各 rank 不同；这段循环的目的是证明「能训练」，不是给出全局损失（[examples/dit_attention.py:126-129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L126-L129)）。
3. **问**：把 `block.group.destroy()` 挪到 `dist.destroy_process_group()` 之后会怎样？
   **答**：`destroy()` 内部要做组内 barrier 并清理窗口与 comm 流，此时底层进程组已被拆掉，轻则报错重则挂起；u2-l1 讲过它是集合操作，顺序不可换（[examples/dit_attention.py:131-132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L131-L132)）。

## 5. 综合实践

把第 4 节的四条线串起来：**将示例的打包调用改成三次独立调用，先证等价、再量化代价、最后解释原因**。

### 5.1 实践目标

1. 验证：q/k/v 三次独立 `all_to_all_4d` 与一次打包调用，输出逐位（或 allclose 意义上）一致。
2. 量化：同样字节下，4 次调用对 2 次调用多付的时间，并用 `_timed` 的阶段分解指出时间去了哪。
3. 解释：写出「为什么字节相同但时间不同」的注释。

### 5.2 操作步骤

将下面的完整脚本（示例代码）保存为仓库根目录的 `dit_attention_3calls.py`（新文件，不改仓库源码），运行：

```bash
torchrun --nproc_per_node=<N> dit_attention_3calls.py    # 计时请在 ws>=2 下进行
```

```python
# 示例代码：三次独立调用 vs 一次打包调用
from __future__ import annotations

import os

import torch
import torch.distributed as dist
import torch.nn.functional as F

from fast_ulysses import UlyssesGroup

B, S_LOCAL, HEADS_PER_RANK, HEAD_DIM = 1, 512, 4, 128


class PackedAttention(torch.nn.Module):
    """与 examples/dit_attention.py 的 UlyssesAttention 相同：输入侧一次打包调用。"""

    def __init__(self, group, n_global, head_dim):
        super().__init__()
        self.group, self.n_global, self.head_dim = group, n_global, head_dim
        self.qkv = torch.nn.Linear(n_global * head_dim, 3 * n_global * head_dim, bias=False)
        self.proj = torch.nn.Linear(n_global * head_dim, n_global * head_dim, bias=False)

    def forward(self, x):
        b, s_local, _ = x.shape
        qkv = self.qkv(x).view(b, s_local, self.n_global, 3 * self.head_dim)
        qkv = self.group.all_to_all_4d(qkv, mode=0)                 # 握手 1/2（输入侧仅此一次）
        q, k, v = qkv.chunk(3, dim=-1)
        attn = F.scaled_dot_product_attention(
            q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
        ).transpose(1, 2)
        attn = self.group.all_to_all_4d(attn.contiguous(), mode=1)  # 握手 2/2
        return self.proj(attn.reshape(b, s_local, self.n_global * self.head_dim))


class ThreeCallAttention(torch.nn.Module):
    """q、k、v 各走一次 mode 0：同样字节，多两次握手。权重与 PackedAttention 共享。"""

    def __init__(self, group, n_global, head_dim):
        super().__init__()
        self.group, self.n_global, self.head_dim = group, n_global, head_dim
        self.qkv = torch.nn.Linear(n_global * head_dim, 3 * n_global * head_dim, bias=False)
        self.proj = torch.nn.Linear(n_global * head_dim, n_global * head_dim, bias=False)

    def forward(self, x):
        b, s_local, _ = x.shape
        # 同一个 Linear 的输出，只是把「打包」推迟：view 成 (…, n, 3, head_dim) 再 unbind，
        # 取到的三组值与原实现「先交换后 chunk(3, dim=-1)」完全相同——
        # 因为交换只重排 s 轴与 n 轴，从不混合最后一维（见 4.2.4 的纯 torch 验证）。
        qkv = self.qkv(x).view(b, s_local, self.n_global, 3, self.head_dim)
        q, k, v = qkv.unbind(dim=3)                  # 各 (b, s_local, n_global, head_dim)
        q = self.group.all_to_all_4d(q, mode=0)      # 握手 1/4
        k = self.group.all_to_all_4d(k, mode=0)      # 握手 2/4（同流串行，窗口复用是安全模式）
        v = self.group.all_to_all_4d(v, mode=0)      # 握手 3/4
        attn = F.scaled_dot_product_attention(
            q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
        ).transpose(1, 2)                            # (b, s_global, n_local, head_dim)
        attn = self.group.all_to_all_4d(attn.contiguous(), mode=1)  # 握手 4/4（输出只有一份张量）
        return self.proj(attn.reshape(b, s_local, self.n_global * self.head_dim))


def bench(module, x, iters=20):
    for _ in range(5):          # 预热：触发新形状的集合性窗口分配与 SDPA 内核选择
        module(x)
    torch.cuda.synchronize()
    start, end = torch.cuda.Event(True), torch.cuda.Event(True)
    start.record()
    for _ in range(iters):
        module(x)
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters     # 毫秒


def main():
    dist.init_process_group("nccl")
    rank, ws = dist.get_rank(), dist.get_world_size()
    torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", rank)))
    dev = torch.device("cuda", torch.cuda.current_device())
    torch.manual_seed(0)

    n_global = HEADS_PER_RANK * ws
    group = UlyssesGroup(require_nvlink=False)
    packed = PackedAttention(group, n_global, HEAD_DIM).to(dev, dtype=torch.bfloat16)
    three = ThreeCallAttention(group, n_global, HEAD_DIM).to(dev, dtype=torch.bfloat16)
    three.load_state_dict(packed.state_dict())   # 权重逐位相同：差异只能来自路径本身

    s_global = S_LOCAL * ws
    x = torch.randn(B, s_global, n_global * HEAD_DIM, dtype=torch.bfloat16, device=dev)
    x_local = x[:, rank * S_LOCAL:(rank + 1) * S_LOCAL]

    # 1) 等价性：同样权重、同样字节，输出应一致
    with torch.no_grad():
        err = (packed(x_local).float() - three(x_local).float()).abs().max().item()
        t_packed, t_three = bench(packed, x_local), bench(three, x_local)

        # 2) 阶段分解：_timed 把一次调用切成 barrier_in/transfer/barrier_out/copy_out
        big = torch.randn(B, S_LOCAL, n_global, 3 * HEAD_DIM, dtype=torch.bfloat16, device=dev)
        small = torch.randn(B, S_LOCAL, n_global, HEAD_DIM, dtype=torch.bfloat16, device=dev)
        for t in (big, small):                   # 先预热：新形状首调会触发集合性窗口分配
            group.all_to_all_4d(t, mode=0)
        _, st_big = group._timed(big, mode=0)    # 打包形态 d=384 的一次调用
        _, st_small = group._timed(small, mode=0)  # 拆开形态 d=128 的一次调用

    if rank == 0:
        print(f"ws={ws}  max |packed - three| = {err:.6f}")
        print(f"packed : {t_packed:.3f} ms/iter")
        print(f"three  : {t_three:.3f} ms/iter  ({t_three / t_packed:.2f}x)")
        print(f"packed d=384 stages (ms): { {k: round(v, 3) for k, v in st_big.items()} }")
        print(f"single d=128 stages (ms): { {k: round(v, 3) for k, v in st_small.items()} }")

    # 为什么字节相同、时间不同：
    # 输入侧要搬运的总字节都是 b*s_local*n_global*3*head_dim*elem_size，但每次
    # all_to_all_4d 都要付两道与消息大小基本无关的标志位屏障（barrier_in/barrier_out）
    # 和一次 copy_out——这些是按「次数」计费的延迟项；传输本身才是按字节计费的带宽项。
    # 打包把输入侧 3 次调用（6 道屏障、3 次 copy_out）合并为 1 次（2 道屏障、1 次
    # copy_out），省下的是 4 道屏障加 2 次 copy_out 的固定延迟，带宽项一分未变。
    # 注意：ws=1 时屏障被整体跳过（u1-l3），这项差异归零——计时请在 ws>=2 下做。
    group.destroy()
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

### 5.3 需要观察的现象

- 等价性一行：`max |packed - three|` 应为 0 或极小（两条路径喂给 SDPA 的 q/k/v 一个是交换缓冲的跨步视图、一个是密集张量，SDPA 可能选不同内核，故用与示例一致的宽容差判断；建议同时打印 `torch.equal(...)` 看是否碰巧逐位相等）。
- 计时两行：`three` 的每次迭代应慢于 `packed`，比值大于 1。
- 阶段分解两行：`d=384` 与 `d=128` 两次调用的 `transfer` 大致与字节成比例（约为 3 倍关系），而 `barrier_in`/`barrier_out` 两次都接近同一个「与大小无关」的小常数——这就是延迟项与带宽项的分界。`_timed` 的阶段名与定义见 [python/fast_ulysses/group.py:251-265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L251-L265)（阶段数值为毫秒，与 [benchmark/bench_a2a.py:198](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L198) 的用法同口径；注意 docstring 标明它仅供基准、读事件会同步）。

### 5.4 预期结果

- 输出等价成立（4.2.4 已从「交换不碰最后一维」给出结构性论证）。
- 慢的幅度由 4 道额外屏障 + 2 次额外 copy_out 的固定延迟决定；消息越小（本例仅约 12.6 MB），延迟项占比越大，比值越难看。具体数字**待本地验证**——请在 ws≥2、空闲的 GPU 上运行并记录。
- 若把 `--nproc_per_node=1` 跑一遍：等价性仍成立，但两个版本耗时几乎相同——因为 ws=1 时屏障被整体跳过，延迟项消失。这个「负对照」本身就是对「时间差来自握手」的交叉验证。

## 6. 本讲小结

- `examples/dit_attention.py` 是本库推荐形态的完整样例：fast-ulysses 只交付集合通信，注意力内核与两次调用的编排归调用方；模块因此是普通 `nn.Module`，没有 `autograd.Function` 包装。
- qkv 沿最后一维打包成 \(3 \cdot \text{head\_dim}\)：字节不变、输入侧握手由 3 次并为 1 次；正确性依赖「交换只重排 s/n 轴、从不混合最后一维」，故与最后一维的任何切分可交换；折叠进 n 轴的 5D 变体则是混头的陷阱。
- 校验方法学：与同权重、同数据的单卡参照比较输出**和**输入梯度；`allclose` 而非 `equal`（差异来自 SDPA 的归约顺序，不是交换本身）；先 `.float()` 再求差，容差 `5e-2` 对应 bf16 穿过注意力加两次投影的量纲。
- 五个优化器步证明的是「能训练」而非「一次正确」；梯度自动穿过两次交换（backward = 另一个 mode），loss 是 rank 本地量。
- 成本模型：\(T_{\text{调用}} \approx 2T_{\text{屏障}} + T_{\text{传输}}(\text{字节}) + T_{\text{copy\_out}}\)——打包优化的是按次数计费的延迟项，不是带宽项。

## 7. 下一步学习建议

下一讲进入 u3 单元：**u3-l1（A2ADims / CopyOp / A2APlan：寻址的纯主机表达）**。本讲你一直把 `all_to_all_4d` 当黑盒用；u3 将打开 [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L1-L1) 所在的纯主机层，看「序列⇄头重排」如何被编译成一组带源/目的步长的字节拷贝——其中 mode 0 的发射逻辑正是本讲那次打包调用的真身。

顺路可读：

- [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L1-L1) 的 Raises 全表——把本讲用到的契约再对一遍；
- u6-l3 将给出「backward 是另一个 mode」的完整推导，回答本讲 4.4 遗留的为什么；
- [benchmark/bench_a2a.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L1-L1) 的 `stages` 模式（u9-l4 精读）——本讲综合实践里 `_timed` 的工业版用法。
