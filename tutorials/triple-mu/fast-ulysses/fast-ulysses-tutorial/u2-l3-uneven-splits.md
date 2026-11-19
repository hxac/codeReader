# 不均匀分片：seq_splits 与 head_splits

## 1. 本讲目标

上一讲（u2-l2）我们背下了 `all_to_all_4d` 的形状契约：mode 0 把 `(b, s_local, n_global, d)` 变成 `(b, s_global, n_local, d)`，mode 1 是它的严格逆变换。但那张形状表隐含着一个假设——所有 rank 的 `s_local` 一样长、所有 rank 的 `n_local` 一样长。真实的序列不会这么体贴：wan-720p 的 75827 个 token 除以 8 个 rank，余 3。

本讲讲完之后，你应该能：

1. 给定全局长度 `s`、`n_global` 与 `ws`，自己算出每个 rank 的 `seq_splits` / `head_splits`；
2. 讲清楚 `src/group.cc` 里 `make_dims` 的「splits 与输入形状交叉校验」检查的是什么、为什么只检查本 rank 看得见的两根轴；
3. 解释不传 splits 时的偶均分特例，以及它为什么只要求「被散射轴」能被 world_size 整除；
4. 说明丢掉序列 padding 的动机与实测收益：为什么 baseline 为此付出 5–11%，而本路径只有 1.00–1.03×。

## 2. 前置知识

**序列分片与 padding。** 序列并行里，长度为 `s` 的序列被切成 `ws` 段，每 rank 拿一段。`torch.distributed` 的 `all_to_all_single` 在「等长 chunk」模式下最高效（一次 flat 交换），所以惯用做法是先把 `s` 向上凑成 `ws` 的倍数、在序列尾部补无意义的填充 token，让每 rank 等长。这些填充 token 不是免费的：它们会跟着真数据一起，**每一层、每一个 step** 地穿过注意力和两次集合通信。

**前缀和（prefix sum）。** 给定数组 \(a_0, a_1, \dots\)，它的（不含自身的）前缀和是 \(\text{off}[p] = \sum_{i<p} a_i\)。本讲里前缀和用来回答「rank p 的分片从全组的第几个位置开始」。

**被散射轴。** 承接 u2-l2 的术语：mode 0 把头轴散射出去（`n_global → n_local`）、把序列轴聚合回来（`s_local → s_global`）；mode 1 反向。「某 mode 的被散射轴」指该 mode 下输入里还是**全组长度**的那根轴——mode 0 是头轴（`x2 = n_global`），mode 1 是序列轴（`x1 = s_global`）。

**`all_to_all_single` 的 split sizes（对照用）。** `torch.distributed.all_to_all_single` 有 `split_sizes` 参数，语义是「**本次调用**我发给每个 peer 多少、从每个 peer 收多少」——它是 per-call 的收发量，方向一反两张表就要互换。fast-ulysses 的 `seq_splits` / `head_splits` **不是**这个语义，这是本讲 4.1 的核心区分点。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [docs/quickstart.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md) | 用户视角：「Dropping the sequence padding」一节的三行用法 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 契约文档：splits 段落与 Raises 清单 |
| [include/fast_ulysses/a2a_plan.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp) | 数据结构：`A2ADims` / `A2APlan`；「不均分是一般路径」的设计声明 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 纯主机寻址层：`validate`、前缀和、`window_numel`、拷贝发射（splits 的消费者） |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `make_dims`：splits 进入算子的入口与交叉校验（本讲主角） |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py) | 无 GPU 的布局验证：不均分、空分片用例（无 GPU 实践的依据） |

建议阅读顺序：先读两份 docs（知道「是什么、承诺了什么」），再读 `src/group.cc` 的 `make_dims`（知道「查什么」），最后浏览 `src/a2a_plan.cc` 的开头（知道「splits 被拿去算了什么」）。`build_plan` 的逐行精读属于 u3-l2，本讲只取所需。

## 4. 核心概念与源码讲解

### 4.1 splits 语义：一张描述全组几何的表

#### 4.1.1 概念说明

`seq_splits[p]` 是 **rank p 持有的序列分片长度**，`head_splits[p]` 是 **rank p 持有的头数**。两张表各有 `ws` 项，描述的不是「本次调用谁发给谁多少字节」，而是「**这个组当初是怎么切的**」——组的几何。

这带来三个直接推论：

1. **两张表是组属性，不是调用属性。** 同一对 splits 同时服务于 mode 0 和 mode 1：mode 0 发送时用 `head_splits` 决定每个 peer 收我的哪几列头、用 `seq_splits` 决定我落在 peer 窗口的哪几行；mode 1 沿同一几何反向走。这也是为什么（提前埋一个 u6-l3 的伏笔）backward 直接用「另一个 mode + **同一对** splits」，而 `all_to_all_single` 的 split sizes 在 backward 里必须交换——后者描述单次调用的收发量，前者描述组的形状。
2. **每个 rank 持有整张表，因此无需通信就能算出所有偏移。** 我给 peer p 发数据时，目的地址落在 p 的窗口里「我的序列分片所占据的行区间」，起点是 `seq_offset[我的rank]`——它是我**前面所有 rank** 的分片长度之和。不知道别人的长度，就写不出正确的目的偏移。
3. **表项可以为 0，不能为负。** 长度为 0 的分片（某 rank 一段序列都没分到）是合法输入；但全表为 0 会被分配入口拒绝（窗口无数据可搬）。

约束「都传或都不传、全 rank 一致」由文档与校验共同维护，见 4.2。

#### 4.1.2 核心流程

一次带 splits 的调用，沿 u1-l4 建立的骨架走到本讲关注的这一步：

```text
group.all_to_all_4d(x, mode=0, seq_splits=..., head_splits=...)   # python/fast_ulysses/group.py
  └─ torch.ops.fast_ulysses.all_to_all_4d                          # src/bindings.cc
       └─ UlyssesGroup::plan()                                     # src/group.cc   按 (形状,mode,dtype,splits) 查缓存
            ├─ 缓存命中 → 直接返回 A2APlan
            └─ 缓存未命中 → make_dims()                            # 本讲主角：决定 splits 「是」什么
                             └─ build_plan(dims, mode, elem_size)  # src/a2a_plan.cc  消费 splits，产出 CopyOp 表
```

`make_dims` 的职责被刻意收窄到一件事——正如它上方的注释所说：

> The plan treats uneven as the general case, so the only decision here is what the splits ARE: the caller's, or the even ones the shape implies.
> （plan 把不均分当作一般情形，所以这里唯一的决定是：splits 用调用方给的，还是形状所隐含的偶均分。）

#### 4.1.3 源码精读

**设计声明：不均分是一般路径。** 这句话写在寻址层头文件的开头注释里，是理解本讲的钥匙：

[include/fast_ulysses/a2a_plan.hpp:1-6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L1-L6) —— 声明该文件是「无 CUDA、无 torch、无通信库」的纯主机寻址，并明确「不均分是一般情形，偶均分不过是 `seq_splits=[s/P]*P`、`head_splits=[n/P]*P`」——所以全库只有一条代码路径需要写对，没有「快路径/慢路径」之分。

**承载 splits 的结构体 `A2ADims`：**

[include/fast_ulysses/a2a_plan.hpp:19-34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L19-L34) —— `A2ADims` 携带 `b`、`d`、`rank`、`world_size` 加上两张 splits 表；`seq_total()` / `head_total()` 求和，`seq_offsets()` / `head_offsets()` 返回前缀和：

\[
\text{seq\_offset}[p] = \sum_{i=0}^{p-1} \text{seq\_splits}[i], \qquad \text{seq\_total} = \sum_{p} \text{seq\_splits}[p]
\]

**表项的合法性检查 `validate`：**

[src/a2a_plan.cc:113-141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L113-L141) —— 依次检查：`world_size > 0`、`rank` 在范围内、`b`/`d` 为正、两张表长度都恰好等于 `world_size`（L124-131），然后逐项查负数（L133-140）。L132 的注释给出了关键区分：**长度为 0 的分片合法（某个 rank 可能一段序列都分不到），负数不合法**。

**splits 如何变成别人的偏移——以 mode 0 为例：**

[src/a2a_plan.cc:175-193](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L193) —— 我给 peer p 发它名下的头列，发射的拷贝参数是：

```cpp
const int64_t row = dims.head_splits[p] * d_bytes;   // p 的头列宽（字节）
op.src_offset = head_offset[p] * d_bytes;            // 我的输入里 p 的头列从哪读
op.src_pitch  = head_total * d_bytes;                // 读步长 = 整条头轴
op.dst_offset = seq_offset[dims.rank] * row;         // 落在 p 的窗口里「我的行区间」
op.dst_pitch  = row;                                 // 写步长 = p 的窗口行是连续的全序列
op.rows       = s_me;                                // 我的序列分片长
```

用一个手工例子固化直觉（同 [test/test_plan.py:161-164](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L161-L164) 的 `test_uneven_sequence` 用例）：`seq_splits=[7,5,5,4]`，则 `seq_offset=[0,7,12,17]`。rank 2 给 peer 1 发数据时 `dst_offset = seq_offset[2] × row = 12 × row`——**目的偏移由发送方在表里的位置决定**。这解释了为什么两张表必须在每个 rank 上一字不差：任何一个 rank 的表不同，它写进别人窗口的位置就不同，数据静默错位。

**窗口尺寸也是从别人的表项里读出来的：**

[src/a2a_plan.cc:166-173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173) —— `window_numel` 取**所有 rank** 接收量的最大值。mode 0 下某 rank 的接收量是 `b × seq_total × head_splits[p] × d`，对 `p` 取 max：若头分片不均，头最少的 rank 也要按头最多的 rank 的份额开窗口。[include/fast_ulysses/a2a_plan.hpp:53-60](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L53-L60) 的注释给出原因：各 rank 的 peer 偏移只有在**大家开出同样大小**的窗口时才对齐，而因为每 rank 都持有全表，这个 max 可以不通信地各自算出。

**入口签名（Python 侧）：**

[python/fast_ulysses/group.py:111-128](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L111-L128) —— `all_to_all_4d(x, *, mode=0, out=None, seq_splits=None, head_splits=None)`，docstring 复述契约：「两者都传或都不传，每个 rank 一致，且与传入形状吻合」。`empty_output` / `all_to_all_4d_async` 接受同一对参数——组几何对所有入口统一。

#### 4.1.4 代码实践

**实践目标**：亲手为 wan-720p 的真实序列长度算出两张表，并核对每个量都能从表里复算出来。

**操作步骤**（纯 Python，无需 GPU，可在任何解释器里运行）：

```python
# 示例代码：手工计算两张 splits 表
s, n_global, ws = 75827, 40, 8

# 平衡公式：前 s % ws 个 rank 多拿 1 个 token（quickstart.md 的写法）
seq_splits = [s // ws + (1 if p < s % ws else 0) for p in range(ws)]
head_splits = [n_global // ws + (1 if p < n_global % ws else 0) for p in range(ws)]

print(seq_splits)    # 预测: [9479, 9479, 9479, 9478, 9478, 9478, 9478, 9478]
print(head_splits)   # 预测: [5, 5, 5, 5, 5, 5, 5, 5]   (40 恰好被 8 整除)

assert sum(seq_splits) == s
assert sum(head_splits) == n_global

# 前缀和与每 rank 的 mode 0 输入/输出形状
off, run = [], 0
for v in seq_splits:
    off.append(run); run += v
print(off)           # 预测: [0, 9479, 18958, 28437, 37915, 47393, 56871, 66349]
for r in range(ws):
    print(r, (1, seq_splits[r], 40, 384), "->", (1, s, head_splits[r], 384))
```

**需要观察的现象**：`seq_splits` 只含两种长度（9479 与 9478，相差恰 1）；`sum(seq_splits)` 精确等于 75827 而不是任何凑整的数。

**预期结果**：75827 = 8 × 9478 + 3，故前 3 个 rank 各 9479、后 5 个各 9478；每 rank mode 0 的输入 `(1, seq_splits[r], 40, 384)`、输出 `(1, 75827, 5, 384)`；mode 0 的窗口元素数 `1 × 75827 × 5 × 384 = 145,587,840`，按 bf16 计 291 MB——正好对上 benchmark 表里 wan-720p 的「MB/rank at ws=8 = 291」一行。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：h3-t2va-5s 的序列长度是 38051，`ws=8`。写出 `seq_splits`。
**答案**：38051 = 8 × 4756 + 3，所以 `seq_splits = [4757, 4757, 4757, 4756, 4756, 4756, 4756, 4756]`，检验 3×4757 + 5×4756 = 38051。

**练习 2**：`seq_splits` 里可以出现 0 吗？可以出现 −1 吗？全表为 0 会怎样？
**答案**：0 合法——`validate` 只拒绝负数（[src/a2a_plan.cc:132-140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L132-L140)），且 `build_plan` 对空分片直接跳过拷贝发射（[src/a2a_plan.cc:180-183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L183) 的 `row == 0 || s_me == 0` 分支），[test/test_plan.py:178-181](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L178-L181) 的 `test_empty_shard` 专门覆盖 `[3, 0, 5, 4]`。−1 不合法。全表为 0 时窗口尺寸为 0，分配入口以「this call moves no data」拒绝（[src/group.cc:209-214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L209-L214)）。

**练习 3**：为什么每个 rank 必须持有**整张** `seq_splits`，而不能只知道自己那一项？
**答案**：因为寻址需要别人的表项——mode 0 里我写给 peer p 的目的偏移是 `seq_offset[我的rank]`，它是我前面所有 rank 的长度之和；窗口尺寸要取 `head_splits` 的全表最大值；mode 1 里读偏移 `seq_offset[p]` 更是逐 peer 取前缀和（[src/a2a_plan.cc:175-193](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L193) 与 [src/a2a_plan.cc:103-111](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L103-L111)）。持有全表让每个 rank 无通信地算出全部寻址——这正是 SPMD 设计换来的免同步。

### 4.2 形状交叉校验：make_dims 只核对本 rank 看得见的那一半

#### 4.2.1 概念说明

调用方递进来的张量 `x` 只携带**本 rank 的视图**：mode 0 下 `x.sizes()[1]` 是我的序列分片长、`x.sizes()[2]` 是全组头数。于是能机器检查的一致性只有两条：

- **mode 0**：`x1 == seq_splits[rank]`（我的分片长对得上）且 `x2 == sum(head_splits)`（全组头数对得上）；
- **mode 1**（对称地）：`x1 == sum(seq_splits)` 且 `x2 == head_splits[rank]`。

查不了的有两类：

1. **「只传一张表」没有意义**——plan 需要的是全组几何，而缺失的另一半无法从一个**已经分片了的**形状推断：从 `(b, s_local, n_global, d)` 里你不知道别的 rank 的 `s_local` 是多少。所以这是「都传或都不传」，而不是「缺省补全」。
2. **「两个 rank 的表彼此一致」查不了**——每个 rank 只能对自己的说法负责。本地交叉校验能挡住「本 rank 的表与自己的张量矛盾」，但 rank A 与 rank B 的表不同属于 u2-l2 讲过的集体契约，违反的后果是挂起或静默错位，不是报错。

这套校验的动机写在检查上方的注释里：让切错分片的调用方**在这里拿到报错，而不是一个悄然错误的结果**。

#### 4.2.2 核心流程

`make_dims` 中 splits 分支的判定顺序（伪代码）：

```text
若 seq_splits 与 head_splits 至少给了一个:
    若只给了一个            → 报错 "pass both seq_splits and head_splits, or neither"
    填入 dims; dims.validate()   # 表长 == ws、无负数（先于一切按 rank 的索引）
    expect_x1 = mode==0 ? seq_splits[rank] : sum(seq_splits)
    expect_x2 = mode==0 ? sum(head_splits) : head_splits[rank]
    若 (x1, x2) != (expect_x1, expect_x2)
        → 报错 "input is [...] but the splits imply [...]"   # 打印两个完整形状
否则:
    走偶均分特例（见 4.3）
```

注意校验顺序的讲究：`validate()` 在**任何按 `rank` 索引表项的动作之前**运行——先确认表长恰好是 `ws` 且无负数，`seq_splits[rank]` 才是安全的。

#### 4.2.3 源码精读

**分支入口与「都传或都不传」：**

[src/group.cc:80-87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L80-L87) —— 只要任一 splits 有值就进入此分支；L81-83 的注释给出「缺一半无意义」的完整论证（plan 需要全组几何、缺失的一半无法从已分片的形状推断），L83-84 的 `TORCH_CHECK` 拒绝只传一张表的调用；L85-87 把两张表填入 `dims` 并先跑 `validate()`。

**交叉校验本体：**

[src/group.cc:88-110](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L88-L110) —— L90-91 按 mode 算出 `expect_x1` / `expect_x2`；L92-109 的 `TORCH_CHECK` 把**实际形状**与**splits 隐含形状**两个四元组并排打印，形如 `input is [1, 9480, 40, 384] but the splits imply [1, 9479, 40, 384]`——`b` 与 `d` 也一并列出，哪根轴错了、错成多少一目了然。L88-89 注释点明动机：mis-shard 的调用方在此得到报错，而不是「silently corrupt result」。

**校验运行的时机——缓存未命中时、第一个屏障之前：**

[src/group.cc:167-196](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L167-L196) —— `plan()` 用 `(sizes, seq, head, mode, dtype)` 做缓存键（L173-180），`make_dims` 只在未命中时运行（L191）。开头 L39-41 的注释解释了为什么这样是安全的：`make_dims` 检查的每个量都在缓存键里，缓存命中不可能跳过一个本会失败的检查。承接 u2-l2 的纪律：这些检查全部发生在调用的**第一个屏障之前**，所以被拒的 rank 不会把已经入队的同伴晾在集合点上。

**文档对应：**

[docs/api.md:88-91](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L88-L91) —— 契约复述：`seq_splits[p]` / `head_splits[p]` 是 rank p 的分片，「都传或都不传、全 rank 一致、与传入形状吻合」；不传表示偶均分且被散射轴必须整除；都传则分片可任意不均——这正是能丢掉 padding 的机制。[docs/api.md:93-103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93-L103) 的 Raises 清单里，「one of the splits without the other, or splits contradicting x's shape」逐条对应上面两个 `TORCH_CHECK`。

#### 4.2.4 代码实践

**实践目标**：先在纸面预测报错消息，再实际触发它，确认消息与 `make_dims` 的 `TORCH_CHECK` 一一对应。

**操作步骤**（需要已构建的扩展与一张 GPU；完整脚本见第 5 节，此处为最小片段，`ws=1` 即可）：

```python
# 示例代码：接在第 5 节脚本之后运行
bad = torch.randn(1, seq_splits[rank] + 1, n_global, 384,
                  dtype=torch.bfloat16, device="cuda")   # 序列长度比 splits 声称的多 1
try:
    group.all_to_all_4d(bad, mode=0, seq_splits=seq_splits, head_splits=head_splits)
    print("没有报错 —— 与文档矛盾，请检查环境")
except RuntimeError as e:
    print(f"[rank {rank}] {e}")
```

**需要观察的现象**：抛出的是 `RuntimeError`，消息把两个形状并排打印；**程序不挂起**——即使在 `ws=8` 下各 rank 的 `bad` 形状彼此不同（9480 与 9479），每个 rank 也都在本地、于任何屏障之前被拒绝。

**预期结果**：`ws=1` 时消息为 `input is [1, 75828, 40, 384] but the splits imply [1, 75827, 40, 384]`；`ws=8` 时 rank 0 看到 `input is [1, 9480, 40, 384] but the splits imply [1, 9479, 40, 384]`，而 rank 3-7 的 implied 一侧是 9478。确切文本待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：mode 1 下的 `expect_x1` / `expect_x2` 各是什么？
**答案**：`expect_x1 = sum(seq_splits)`（mode 1 输入的序列轴是全组长度），`expect_x2 = head_splits[rank]`（头轴是我的分片）。见 [src/group.cc:90-91](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L90-L91) 的三元表达式。

**练习 2**：构造一个「每个 rank 的本地校验都通过、但组必然出事」的 splits 用法。
**答案**：让两个 rank 传**彼此不同**的表，且各自与自己的 `x` 吻合。例如 `ws=2`、rank 0 传 `seq_splits=[4,4]` 并持有 `x1=4`，rank 1 传 `seq_splits=[4,5]` 并持有 `x1=5`：两 rank 的 `x1==seq_splits[rank]` 都成立，但两张表不同 → 目的偏移与窗口尺寸各算各的，peer 写错位置。这类错误机器查不了，只能靠「全 rank 一致」的契约。

**练习 3**：为什么报错要打印两个**完整形状**，而不是只说「长度不匹配」？
**答案**：因为出错的不一定是长度轴——`b`、`d` 一并列出后，`input is` 与 `splits imply` 逐位对照能直接指认是哪根轴、差多少；同时 `implied` 形状是**由表重建**的，等于把「调用方声称的几何」原样回显给调用方（[src/group.cc:92-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L92-L109)）。

### 4.3 偶均分特例：不传 splits 时，形状自己说话

#### 4.3.1 概念说明

不传 splits 时，`make_dims` 用输入形状反推偶均分：

- **mode 0**（散射头轴）：要求 `n_global % ws == 0`，然后 `seq_splits = [s_local] * ws`、`head_splits = [n_global/ws] * ws`；
- **mode 1**（散射序列轴）：要求 `s_global % ws == 0`，然后 `seq_splits = [s_global/ws] * ws`、`head_splits = [n_local] * ws`。

注意两件容易被忽略的事：

1. **只有被散射轴要求整除。** 被散射轴在输入里还是全组长度，要均分成 `ws` 份，必须能整除；另一根轴进来时**已经是本 rank 的分片**，根本不存在「再切」的问题，自然无整除要求。
2. **反推出的 `seq_splits = [s_local] * ws` 隐含了集体契约。** 「每个 rank 的分片长都等于我这个 rank 的 `x1`」只有在所有 rank 发出相同形状时才成立——又一次，本地无法判定，交给契约。

特例不是另一条代码路径：反推出的两张表和调用方显式传入的偶均分表**完全相同**，下游 `build_plan` 一视同仁。这正是 4.1「一般路径」设计的回报。

#### 4.3.2 核心流程

```text
未传 splits:
    mode 0: 若 n_global % ws != 0 → 报错(提示改传 splits)
            否则 seq_splits=[s_local]*ws, head_splits=[n_global/ws]*ws
    mode 1: 若 s_global % ws != 0 → 报错(提示改传 splits)
            否则 seq_splits=[s_global/ws]*ws, head_splits=[n_local]*ws
两种来源的表汇入同一条 validate → build_plan 路径
```

#### 4.3.3 源码精读

**特例分支本体：**

[src/group.cc:113-135](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L113-L135) —— L114 注释点明「偶均分这一特例只有在被散射轴整除时才能由形状单独确定；另一根轴进来时已分片，从不需要整除」。mode 0 分支（L115-124）检查 `x2 % ws == 0` 后 `assign(ws, x1)` / `assign(ws, x2 / ws)`；mode 1 分支（L125-134）对称。两条报错消息都在末尾给出出路：`-- or pass seq_splits and head_splits`。

一个阅读提示：消息原文是 `n_global (41) must divide world_size (8)`，按数学惯用语字面读是反的；实际检查是 `x2 % ws == 0`，即 **n_global 能被 world_size 整除**。读源码时以代码为准。

**「不传」与「空列表」是两回事：**

[src/group.cc:173-180](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L173-L180) —— plan 缓存键里 splits 保持 optional，注释说明若把「不传」与「空表」折叠成一个键，一次偶均分调用就能为后续的空表调用焐热缓存，而空表会在 `validate` 的长度检查处被拒。所以 `seq_splits=None`（偶均分）与 `seq_splits=[]`（非法，表长 0 ≠ ws）语义截然不同。

#### 4.3.4 代码实践

**实践目标**：观察偶均分特例的整除性边界，并学会在头轴不整除时构造平衡的不均匀 `head_splits`。

**操作步骤**（纯纸面 + 可选单卡运行）：

1. 纸面预测：`ws=8`、`n_global=41`、`mode=0`、**不传** splits 会发生什么？
2. 若确需 `ws=8` 个 rank 分 41 个头，用平衡公式 `head_splits = [41//8 + (1 if p < 41%8 else 0) for p in range(8)]` 算出表，并写出每 rank 的输入形状。
3. （可选）在单卡上把第 1 步跑一遍：

```python
# 示例代码：整除性边界（ws=1 时改用 x2=41 仍可观察 dtype/形状检查的正常通过路径，
# 整除性报错本身需要 ws>1 才能触发，纸面推演即可）
x = torch.randn(1, 1024, 41, 128, dtype=torch.bfloat16, device="cuda")
group.all_to_all_4d(x, mode=0)   # ws=8 时：41 % 8 != 0 → RuntimeError
```

**需要观察的现象**（多卡时）：报错消息以 `mode 0 scatters the head axis` 开头、给出 `n_global (41)` 与 `world_size (8)` 的具体数值、并以 `-- or pass seq_splits and head_splits` 收尾——与 [src/group.cc:116-121](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L116-L121) 逐字对应。

**预期结果**：41 = 8×5 + 1，平衡公式给出 `head_splits = [6,5,5,5,5,5,5,5]`（前 41 % 8 = 1 个 rank 多拿 1 个头），检验 `sum(head_splits) = 6 + 5×7 = 41` ✓。顺手养成一个习惯：每张手算的表都用 `sum(...) == 全局长度` 收尾自查——这类口算最常见的就是把余数摊错 rank 数。整除性报错的确切文本待本地验证（`ws=1` 时不会触发，需多卡或纸面推演）。

#### 4.3.5 小练习与答案

**练习 1**：`s_global=75827`、`ws=8`、`mode=1`、不传 splits，会发生什么？
**答案**：报错。mode 1 的被散射轴是序列轴，75827 % 8 = 3 ≠ 0，命中 [src/group.cc:126-131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L126-L131)，消息以 `mode 1 scatters the sequence axis` 开头并建议改传 splits。

**练习 2**：写出偶均分下 mode 0 的 `window_numel`，并说明它为什么在所有 rank 上相同。
**答案**：`window_numel = b × s_total × (n_global/ws) × d`，其中 `s_total = ws × s_local`。对 `p` 取 max 的那一项 `head_splits[p]` 此时是常数 `n_global/ws`，max 退化成该常数，所以每 rank 相同——偶均分是「每 rank 输出形状都一样」的特例（[test/test_plan.py:151-158](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L151-L158) 的 `test_even` 专门断言了这一点）。

**练习 3**：同一 rank 上先用偶均分调用一次、再用显式 `seq_splits=[s_local]*ws`（数值等价）调用一次，会共享 plan 缓存吗？
**答案**：不会。缓存键里的 splits 是 optional，「不传」与「传入等值表」是两个不同的键（[src/group.cc:177-180](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L177-L180)），所以各算一次 plan——结果相同，只是各缓存一份。

### 4.4 去 padding：动机与实测收益

#### 4.4.1 概念说明

把三层因果摊开：

1. **padding 为什么存在。** baseline（`torch.distributed` 路径）走 `permute → all_to_all_single → permute`，其中 `all_to_all_single` 的等长 chunk 模式要求每个 rank 的分片一样长，于是 `s` 被向上凑到 `ws` 的倍数、尾部补填充 token。
2. **padding 的代价。** 填充 token 与真数据一起，每层每步穿过注意力计算与两次集合通信——纯浪费的计算与显存。填充量上界是 \(ws-1\) 个 token，所以相对开销以 \( \frac{s+\text{pad}}{s} < 1 + \frac{ws}{s} \) 为界：序列越短、余数越大，浪费越可观。
3. **baseline 丢掉 padding 为什么贵。** 一旦分片长度相差哪怕 1 个 token，`all_to_all_single` 就不能再走 flat 等长路径，必须改用 split sizes 模式，外加每 peer 的 reshape 和事后一次 `cat`——多出来的重排正是 baseline 的 5–11%。而本路径**从一开始就把不均分当一般路径**（4.1 的设计声明）：寻址本来就是带步长的字节拷贝，分片等不等长不改变发射的拷贝形态，所以丢 padding 几乎免费。

#### 4.4.2 实测数字怎么说

[docs/benchmark.md:166-191](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L166-L191) —— 「Removing the sequence padding」一节。表列含义：`base padded` / `base unpadded` 是 baseline 在「凑整+填充」与「真实长度+split sizes」两种写法下的耗时，`base cost` 是两者之比；`ours` 同理。摘录三行（毫秒，ws=8）：

| GPU | shape | base cost | ours cost |
| --- | --- | --- | --- |
| H200 | wan-720p | 1.06× | **1.00×** |
| B200 | h3-t2va-5s | 1.05× | **1.01×** |
| H100 | wan-720p | 1.10× | **1.00×** |

判读：baseline 去 padding 要付 1.05–1.11×（H100 最贵，重排占比最高），本路径只要 1.00–1.03×——注意这里的对照是「同一个实现 padded vs unpadded」，所以差的不是 padding 本身那几个字节，而是**每种实现为了处理不均分要走哪条代码路径**。benchmark 的原话：同一根 all_to_all_single，一旦分片不等长就得带上 split sizes、per-peer reshape 和一次 `cat`。

#### 4.4.3 源码精读

**用户视角的完整用法：**

[docs/quickstart.md:64-78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L64-L78) —— 「Dropping the sequence padding」一节。四行代码做完所有事：真实长度 `s = 75827`（不整除）、平衡公式算 `seq_splits`、`head_splits = [n_global // ws] * ws`（该例中头轴恰好整除）、随调用传入。末尾一句是本讲的立场陈述：**「Uneven is the general path here, not a slow one.」**（不均分在这里是一般路径，不是慢路径。）

注意 quickstart 的 `head_splits = [n_global // ws] * ws` 只在头轴整除时成立；不整除时用 4.3.4 的平衡公式（本讲实践里统一用平衡公式，两种情形都覆盖）。

#### 4.4.4 代码实践

**实践目标**：量化 wan-720p 工作点上的 padding 开销，并外推它在短序列下的增长。

**操作步骤**（纸面计算）：

1. `s=75827`、`ws=8`：凑整到哪个长度？pad 几个 token？填充占比多少？
2. 每个 rank 的输入张量在 padded 与 unpadded 下各多大（`b=1`、`n_global=40`、`3·head_dim=384`、bf16）？
3. 若 `s=1000`、`ws=8`：最坏情况填充占比是多少？

**需要观察的现象 / 预期结果**：

1. 75827 → 凑整到 75832（= 9479 × 8），pad = 5 个 token，占比 5/75827 ≈ 0.007%——在这个工作点上填充本身微不足道；benchmark 表里 5–11% 的差价来自 baseline **处理不均分的代码路径**，而非填充字节。但填充 token 穿过注意力与通信的浪费是每层每步累积的，且 benchmark 的「What is not measured」一节明说：去 pad 节省的注意力计算与显存是微基准看不见的。
2. unpadded：`9478 × 40 × 384 × 2 B ≈ 291.0 MB`（这就是 benchmark 表的「291 MB/rank」）；padded：每 rank 9479 行 ≈ 291.03 MB——字节几乎不变。
3. `s=1000`、`ws=8`：1000 恰整除，取 `s=1001` 则凑到 1008、pad=7，占比 7/1001 ≈ 0.7%；一般地填充占比上界 \( (ws-1)/s \)，`s` 只有几百时可达百分之几。

**待本地验证**项：无——本实践全部是算术，鼓励把结果与 benchmark 表并排抄写一遍加深记忆。

#### 4.4.5 小练习与答案

**练习 1**：`s=16384`、`ws=8` 与 `s=16385`、`ws=8`，填充各是多少？
**答案**：16384 = 8 × 2048，pad = 0（恰好整除，连 splits 都不用传）；16385 凑到 16392，pad = 7，占比 7/16385 ≈ 0.04%。

**练习 2**：用自己的话说清「ours cost 1.00–1.03×」里的两个数各自是怎么测出来的。
**答案**：同一二进制、同一形状，跑两次：一次把序列凑整传偶均分（padded），一次传真实长度的 `seq_splits`（unpadded），`cost = unpadded 耗时 / padded 耗时`。≈1.00 说明对本路径而言「处理不均分」不构成额外工作——拷贝本来就是带步长的。

**练习 3**：既然 wan-720p 的填充占比只有 0.007%，为什么作者仍然把「丢 padding」列为特性？
**答案**：三个理由。(a) baseline 侧的对比是真实的：想去 padding 的人用 baseline 要付 5–11%，用本路径免费——选择权本身有价值；(b) 填充占比上界是 \((ws-1)/s\)，短序列或大余数时显著；(c) 填充 token 穿过**每层每步**的注意力与通信，节省的算力和显存在端到端训练里累积，而微基准（benchmark.md「What is not measured」）恰恰测不到这部分。

## 5. 综合实践

把本讲四个模块串成一个可运行的脚本：算表 → 构造 → mode 0 → 验形状 → mode 1 往返 → 触发交叉校验报错。脚本对任意 `--nproc_per_node` 成立（含 1），与 u1-l3 的结论一致：`world_size=1` 是合法一等公民，屏障被跳过，但寻址、校验、往返逻辑全部照常运行。

**实践目标**：一次运行覆盖 4.1 的表计算、4.2 的交叉校验、4.3 的形状推导、4.4 的真实工作点形状。

**操作步骤**：将下面脚本存为 `uneven_demo.py`（示例代码，放在仓库外的任意目录均可），然后：

```bash
torchrun --nproc_per_node=<N> uneven_demo.py      # N=1 亦可；多卡时按 LOCAL_RANK 绑卡已由脚本处理
```

```python
# 示例代码：uneven_demo.py —— 不均匀分片全流程演示
import os

import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
rank, ws = dist.get_rank(), dist.get_world_size()

# ---- 4.1/4.4: wan-720p 的真实工作点，平衡公式（头轴不整除时同样成立）----
s, n_global, b, d = 75827, 40, 1, 384
seq_splits = [s // ws + (1 if p < s % ws else 0) for p in range(ws)]
head_splits = [n_global // ws + (1 if p < n_global % ws else 0) for p in range(ws)]
assert sum(seq_splits) == s and sum(head_splits) == n_global

group = UlyssesGroup()

# ---- 4.3: 由表推出每 rank 的输入形状，构造张量 ----
x = torch.randn(b, seq_splits[rank], n_global, d, dtype=torch.bfloat16, device="cuda")

# ---- mode 0: 验证输出形状 = (b, sum(seq_splits), head_splits[rank], d) ----
y = group.all_to_all_4d(x, mode=0, seq_splits=seq_splits, head_splits=head_splits)
expected = (b, sum(seq_splits), head_splits[rank], d)
assert tuple(y.shape) == expected, (tuple(y.shape), expected)
print(f"[rank {rank}] mode 0: {tuple(x.shape)} -> {tuple(y.shape)}")

# ---- mode 1: 同一对 splits 是严格逆变换，往返位精确 ----
back = group.all_to_all_4d(y, mode=1, seq_splits=seq_splits, head_splits=head_splits)
assert tuple(back.shape) == tuple(x.shape) and torch.equal(back, x)
print(f"[rank {rank}] round trip bit-exact")

# ---- 4.2: 与 splits 矛盾的输入（长度 +1），本地即拒、不挂起 ----
bad = torch.randn(b, seq_splits[rank] + 1, n_global, d, dtype=torch.bfloat16, device="cuda")
try:
    group.all_to_all_4d(bad, mode=0, seq_splits=seq_splits, head_splits=head_splits)
    raise AssertionError("应当报错而没有报错")
except RuntimeError as e:
    print(f"[rank {rank}] rejected: {e}")

group.destroy()
dist.destroy_process_group()
```

**需要观察的现象**：

1. mode 0 输出形状在所有 rank 上都是 `(1, 75827, head_splits[rank], 384)`——序列轴变全组、头轴变分片；
2. 往返 `torch.equal(back, x)` 通过（纯数据搬运，位精确）；
3. `bad` 调用抛 `RuntimeError` 且**程序继续走到 destroy**——校验先于第一个屏障，被拒不拖累同伴。

**预期结果**：`ws=8` 时 rank 0 打印 `mode 0: (1, 9479, 40, 384) -> (1, 75827, 5, 384)`，rank 3-7 的输入序列长为 9478；rejected 消息形如 `input is [1, 9480, 40, 384] but the splits imply [1, 9479, 40, 384]`（rank 3-7 的 implied 侧为 9478）。`ws=1` 时输入输出同形 `(1, 75827, 40, 384)`，rejected 消息为 `... [1, 75828, ...] ... [1, 75827, ...]`。具体输出待本地验证。

**无 GPU 替代实践**（源码阅读型，对应 u1-l4「布局可在主机上验证」的分层收益）：

```bash
python -m pytest test/test_plan.py -k "uneven or empty_shard" -v    # 无需 GPU 与进程组
```

随后精读 [test/test_plan.py:72-98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L72-L98) 的两个参照函数 `reference_scatter_head` / `reference_gather_head`——它们用切片+拼接陈述了不均分语义的「标准答案」，是第 5 节脚本在 GPU 上验证之事的纯主机镜像。

## 6. 本讲小结

- `seq_splits` / `head_splits` 描述**组的切分几何**（每个 rank 的序列长与头数），不是单次调用的收发量——所以 mode 0 / mode 1（以及 backward）用的是**同一对**表，这与 `all_to_all_single` 需要交换的 split sizes 有本质区别。
- 规则：**都传或都不传**（缺的一半无法从已分片的形状推断）、**全 rank 一字不差**（目的偏移由发送方在表中的位置决定）、**表长 = ws、可为 0、不可为负**。
- `make_dims` 的交叉校验只核对本 rank 看得见的两根轴：mode 0 查 `x1 == seq_splits[rank]` 且 `x2 == sum(head_splits)`，mode 1 对称；检查全部先于第一个屏障，被拒不挂起；跨 rank 的表一致性属于集体契约，机器不查。
- 不传 splits 是偶均分特例，由形状反推，只要求**被散射轴**能被 `ws` 整除；反推出的表与显式传入的偶均分表完全等价，下游只有一条代码路径。
- 去 padding：baseline 一旦分片不等长就得离开 flat 路径（5–11%），本路径把不均分当一般路径（1.00–1.03×）；填充浪费的上界是 \((ws-1)/s\)，且每层每步累积。

## 7. 下一步学习建议

- **u2-l4（实例精读：序列并行 DiT 注意力块）**：看不均匀分片放进完整的「mode 0 → SDPA → mode 1」训练循环里长什么样，包括 qkv 打包与梯度双校验。
- **u3-l1 / u3-l2（布局计划）**：本讲只看了 `build_plan` 的 mode 0 发射片段；这两讲会拆 `CopyOp` 的每个字段、mode 1 的跨步写、以及 `push_batched` 何时把 batch 折叠成一次 3D 拷贝——splits 的全部下游消费都在那里。
- **顺手一读**：[test/test_plan.py:121-148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L121-L148) 的 `check`——它在无 GPU 环境下用 numpy 重放 CopyOp 并断言「所有 rank 的 `window_numel` 相同、且等于最大输出的元素数」，是本讲 4.1「窗口取全表最大」的直接测试证据。
