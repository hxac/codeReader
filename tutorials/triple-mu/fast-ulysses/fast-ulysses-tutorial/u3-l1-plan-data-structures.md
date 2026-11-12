# A2ADims / CopyOp / A2APlan：寻址的纯主机表达

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `A2ADims`、`CopyOp`、`A2APlan` 三个数据结构各自的职责，以及它们为什么能做到「无 CUDA、无 torch、无通信库」。
2. 把 `CopyOp` 的十个字段逐一映射到 `cudaMemcpy2DAsync` / `cudaMemcpy3DAsync` 的参数，理解「一条 4D all-to-all 的发送任务 = 一组带源/目的步长的字节拷贝」。
3. 解释 `window_numel` 为什么取**所有 rank 中最大**的接收量，而不是本 rank 自己的接收量。
4. 找出 `validate()` 与 `checked_mul` / `checked_sum` / `check_byte_products` 各自防住了哪一类非法输入，理解「先于一切分配做校验」与「符号溢出是未定义行为」这两条防御原则。

本讲是 u3 单元的第一讲：我们只看**寻址的纯主机表达**——数据长什么样、怎么算出来；下一讲（u3-l2）才深入 `build_plan` 两种模式的完整发射逻辑，u3-l3 再看无 GPU 的重放测试。

## 2. 前置知识

### 2.1 步长拷贝（pitched copy）

普通 `memcpy` 只能搬运**一段连续**字节。如果要搬运的是二维「条纹」——每行取 `width` 字节、共 `rows` 行，但相邻行在内存里相隔 `pitch` 字节（中间夹着别人的数据）——就需要**步长拷贝**：

\[ \text{第 } i \text{ 行的目的起点} = \text{基址} + \text{offset} + i \times \text{pitch}, \quad i = 0, 1, \dots, \text{rows}-1 \]

CUDA 的 `cudaMemcpy2DAsync(dst, dpitch, src, spitch, width, height, ...)` 就是这个语义：源和目的各自有一个 pitch，每行搬 `width` 字节。理解了 pitched copy，本讲的 `CopyOp` 就只是一组整数。

### 2.2 前缀和（exclusive prefix sum）

给定每 rank 的分片长度表，比如 `seq_splits = [5, 3, 0, 7]`，**排他前缀和**给出每个 rank 的分片在全组总量里的起始偏移：

\[ \text{offset}_r = \sum_{k < r} \text{splits}[k] \]

即 `[0, 5, 8, 8]`。注意两个细节：不需要任何通信——每个 rank 手里都有**整张表**（u2-l3 讲过）；长度为 0 的分片（rank 2）会和它后面的 rank 共享同一个偏移（rank 2 和 rank 3 都从 8 开始）。

### 2.3 整数溢出与未定义行为

C++ 中**有符号整数溢出是未定义行为（UB）**：`a * b` 超过 \( 2^{63}-1 \) 时，编译器不需要给出任何特定结果，程序逻辑从此不可信。比「溢出后可能变成负数」更糟的是——它可能恰好变成一个**看起来合法的小正数**，绕过所有下游尺寸检查。所以本项目在算任何尺寸乘积之前先显式检查，宁可抛异常也不冒 UB 的险。

### 2.4 与前几讲的衔接

- u1-l4 已经建立分层地图：`a2a_plan.cc` 是**纯主机寻址层**，刻意不依赖 CUDA/torch/通信库，换来「无 GPU 也能测布局」与「Meta 内核复用」两个收益。
- u2-l2 讲过 `all_to_all_4d` 的形状契约：mode 0 把 `(b, s_local, n_global, d)` 变成 `(b, s_global, n_local, d)`，mode 1 是严格逆变换。
- u2-l3 讲过 `seq_splits` / `head_splits` 描述的是**组的切分几何**（每个 rank 的序列长与头数），全 rank 持有同一张表。

本讲回答的问题是：这份几何如何被翻译成一条条**具体到字节偏移**的拷贝指令。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [include/fast_ulysses/a2a_plan.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L1-L67) | 三个数据结构的定义 | 全部字段与注释里的设计理由 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L1-L218) | validate、前缀和、build_plan 的实现 | 校验顺序、溢出防护、ops 发射公式 |
| [src/plan_bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L1-L62) | `a2a_plan_debug` 算子：torch 与 plan 的唯一相遇点 | 实践任务用的调试入口 |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L24-L52) | `issue_copy`：CopyOp → cudaMemcpy2D/3DAsync | 字段到 memcpy 参数的映射（精读留到 u5-l1） |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L1-L333) | numpy 字节缓冲重放测试 | 校验断言与负控制注释（精读留到 u3-l3） |

## 4. 核心概念与源码讲解

### 4.1 A2ADims：一次交换的全部几何

#### 4.1.1 概念说明

`A2ADims` 回答一个问题：**这次 all-to-all 的「形状几何」是什么？** 它不看任何具体张量，只装七个数：批大小 `b`、头维 `d`、本 rank 编号、组大小，以及两张切分表。u2-l3 讲过切分表描述的是组几何而非单次收发量，所以 mode 0、mode 1、backward 共用同一份 `A2ADims`。

为什么要把几何单独抽成一个结构？因为寻址是**纯算术**：只要有了这张表，每个 rank 就能**免通信**地算出全组任何偏移——不需要 all_gather，不需要问任何 peer。这是整个「重排即步长」设计（u1-l1）的地基。

#### 4.1.2 核心流程

`A2ADims` 提供四个派生查询，全部是主机端纯函数：

```
seq_total()   = sum(seq_splits)              （带溢出检查）
head_total()  = sum(head_splits)             （带溢出检查）
seq_offsets() = 排他前缀和(seq_splits)
head_offsets()= 排他前缀和(head_splits)
validate()    = 抛 std::invalid_argument 指名道姓地报错
```

调用纪律写在头文件注释里：**在任何分配发生之前调用 `validate()`**。

#### 4.1.3 源码精读

结构定义与「为什么能纯主机测试」的注释（u1-l4 的分层决策就落在这几行）：

- [include/fast_ulysses/a2a_plan.hpp:L19-L34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L19-L34)：`A2ADims` 的七个字段与四个方法声明；注释强调「不均分是一般路径，偶均分只是 `seq_splits = [s/P]*P` 的特例，所以只有一条代码路径要写对」。
- [include/fast_ulysses/a2a_plan.hpp:L1-L6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L1-L6)：头文件开篇注释——无 CUDA、无 torch、无通信库，因此 `test/test_plan.py` 可以在 numpy 缓冲上单独重放计划。

两个 total 走 `checked_sum`，两个 offsets 走排他前缀和：

- [src/a2a_plan.cc:L93-L111](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L93-L111)：`seq_total()` / `head_total()` 委托给 `checked_sum`；`seq_offsets()` / `head_offsets()` 委托给 `exclusive_prefix_sum`。
- [src/a2a_plan.cc:L52-L61](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L52-L61)：`exclusive_prefix_sum` 的实现——`offsets[i]` 记录累加**之前**的运行值，正是「排他」的含义。

mode 枚举用注释直接写出两个方向的形状变换：

- [include/fast_ulysses/a2a_plan.hpp:L12-L17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L12-L17)：`kScatterHead = 0`（`[b, seq_splits[me], head_total, d] -> [b, seq_total, head_splits[me], d]`），`kGatherHead = 1` 是逆变换。

#### 4.1.4 代码实践

**目标**：用 Python 验证前缀和的「零分片共享偏移」现象，并触发两条 `validate` 报错。

**步骤**（本实践不需要 GPU，但需要已安装 fast-ulysses 扩展）：

```python
# 示例代码：实践脚本 dims_check.py
import torch
import fast_ulysses  # 仅 import 即可注册 torch.ops.fast_ulysses

# 1) 手算前缀和，观察零分片
seq_splits = [5, 3, 0, 7]
off, run = [], 0
for s in seq_splits:
    off.append(run); run += s
print("seq_offsets =", off)   # [0, 5, 8, 8]  <- rank2 与 rank3 共享偏移 8

# 2) 触发 validate 报错。C++ 侧抛 std::invalid_argument，跨过 torch 算子绑定后在
#    Python 侧以异常形式浮出（具体类型随 torch 版本可能不同，所以像 test_plan.py
#    一样用 Exception 兜底捕获）：
op = torch.ops.fast_ulysses.a2a_plan_debug
for bad in [
    dict(b=1, d=8, rank=0, world_size=4, seq=[5,3,0,7], head=[1,1], mode=0),   # head 长度 != ws
    dict(b=1, d=8, rank=4, world_size=4, seq=[5,3,0,7], head=[3,2,2,1], mode=0),  # rank 越界
]:
    try:
        op(bad["b"], bad["d"], bad["rank"], bad["world_size"],
           bad["seq"], bad["head"], bad["mode"], 2)
    except Exception as e:  # noqa: BLE001 —— 与 test/test_plan.py 的捕获方式一致
        print("OK ->", e)
```

**需要观察的现象**：前缀和输出 `[0, 5, 8, 8]`；两条报错分别含 `head_splits has 2 entries, expected 4` 与 `rank 4 out of range for world_size 4`。

**预期结果**：报错消息与 [src/a2a_plan.cc:L118-L131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L118-L131) 的字符串逐字对应——`validate` 指名道姓（哪个字段、错在哪）。实际运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`seq_splits = [5, 3, 0, 7]` 时，rank 2 的序列分片长为 0。它的 `seq_offsets` 条目和 rank 3 相同吗？为什么这不是 bug？

**答案**：相同，都是 8。排他前缀和 `offset_r = Σ_{k<r} splits[k]`，rank 2 贡献 0，所以 rank 3 的起点不因前面有个空分片而移动。零长度分片合法（[src/a2a_plan.cc:L132-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L132-L140) 的注释明确「零分片允许，负数不允许」），空分片只是让该 rank 不发射任何拷贝。

**练习 2**：`A2ADims` 里没有 `elem_size` 字段，它出现在哪、为什么不在 dims 里？

**答案**：`elem_size` 是 `build_plan(dims, mode, elem_size)` 的独立参数（[include/fast_ulysses/a2a_plan.hpp:L65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L65)）。dims 描述**几何**（元素个数层面），而 `elem_size` 把几何换算成**字节**；同一份几何配不同 dtype 的 elem_size 可以复用，且字节换算发生在换算处，语义更清楚。

---

### 4.2 CopyOp：把一条拷贝写成十个整数

#### 4.2.1 概念说明

`CopyOp` 是整个项目的原子操作：**一条从本 rank 输入张量到某个 peer 对称窗口的步长拷贝**。它只有十个整数，没有指针——偏移都是**相对各自缓冲区基址的字节数**。这个设计带来一个关键好处（头文件注释原话）：**同一个结构既描述一条 `cudaMemcpy2D/3DAsync`，也描述测试里的一次主机重放**——设备路径和 numpy 重放路径执行的是同一份算术，所以无 GPU 的测试能证明布局正确。

`depth` / `src_slice` / `dst_slice` 三个字段是「批维度折叠」：\( b \) 次相同的 rows×width 拷贝、按固定步长排布，可以合成**一次** 3D 拷贝。

#### 4.2.2 核心流程

字段 → CUDA memcpy 参数的映射表（对照 [src/transfer.cu:L26-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L26-L52) 的 `issue_copy`）：

| CopyOp 字段 | 含义 | `cudaMemcpy2DAsync` | `cudaMemcpy3DAsync` |
| --- | --- | --- | --- |
| `peer` | 发给哪个 rank | 决定目的基址 `peer_ptrs[p]` | 同左 |
| `src_offset` | 源基址偏移（字节） | 调用方加到 src 上 | 同左 |
| `dst_offset` | 目的基址偏移（字节） | 调用方加到 dst 上 | 同左 |
| `src_pitch` | 源相邻行间距 | `spitch` 参数 | `srcPtr.pitch` |
| `dst_pitch` | 目的相邻行间距 | `dpitch` 参数 | `dstPtr.pitch` |
| `width` | 每行字节数 | `width` 参数 | `extent.width` |
| `rows` | 行数 | `height` 参数 | `extent.height` |
| `depth` | 折叠的批元素个数 | （不出现，=1 走 2D） | `extent.depth` |
| `src_slice` / `dst_slice` | 批元素间距 | （不出现） | 推导 `xsize`：`slice / pitch` |

注意基址相加发生在**发射时**（[src/transfer.cu:L92](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L92)）：`peer_ptrs[p] + op.dst_offset`、`src_bytes + op.src_offset`——CopyOp 自己永远只存相对偏移。

mode 0（scatter head）下，本 rank \( r \) 发给 peer \( p \) 的拷贝参数由这组公式给出（记 \( e \) 为 `elem_size`，\( d_b = d \cdot e \) 为每头字节数，\( N \) 为 `head_total`，\( S_r \) 为 `seq_offset[r]`，\( s_r \) 为 `seq_splits[r]`）：

\[ \text{row}_p = h_p \cdot d_b \]
\[ \text{src\_offset} = H_p \cdot d_b, \qquad \text{src\_pitch} = N \cdot d_b \]
\[ \text{dst\_offset} = S_r \cdot \text{row}_p, \qquad \text{dst\_pitch} = \text{width} = \text{row}_p, \qquad \text{rows} = s_r \]

直觉：输入张量里「peer \( p \) 的头列」每隔 \( N \cdot d_b \) 字节出现一段 \( h_p \cdot d_b \) 字节（**读跨步**）；而 \( p \) 的窗口里序列维是连续的，本 rank 的贡献恰好落在自己序列分片占的那几行（**写连续**）。批间距：源侧一个批元素整张 \( s_r \cdot N \cdot d_b \)，目的侧一整窗 \( \text{seq\_total} \cdot \text{row}_p \)。

批折叠的判定条件（`push_batched`）：

\[ \text{worth\_fusing} \iff \text{depth} > 1 \,\wedge\, \text{rows} > 1 \]
\[ \text{expressible} \iff \text{src\_slice} \bmod \text{src\_pitch} = 0 \,\wedge\, \text{dst\_slice} \bmod \text{dst\_pitch} = 0 \,\wedge\, \frac{\text{src\_slice}}{\text{src\_pitch}} \ge \text{rows} \,\wedge\, \frac{\text{dst\_slice}}{\text{dst\_pitch}} \ge \text{rows} \]

#### 4.2.3 源码精读

- [include/fast_ulysses/a2a_plan.hpp:L36-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L36-L51)：`CopyOp` 全部十个字段。注释说明偏移相对缓冲基址、`depth` 如何把批维度折进一条 3D 拷贝。
- [src/a2a_plan.cc:L63-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L63-L89)：`push_batched`——先判 `worth_fusing`（只有**多行**拷贝才融合：单行拷贝本来就是平坦 memcpy，折成 3D 反而把它推上跨步路径），再判 `expressible`（`cudaMemcpy3DParms` 不接受 slice 步长，它自己按 `pitch * ysize` 推导，所以步长必须能整除 pitch 且装得下行数）；两者都满足才合成一条，否则退回 `depth` 条独立 2D 拷贝。这两条约束的出处注释在 [src/a2a_plan.cc:L63-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L63-L69)。
- [src/a2a_plan.cc:L176-L193](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L176-L193)：mode 0 的发射公式源码，与上面公式一一对应；`if (row == 0 || s_me == 0) continue` 处理零头数 peer 与零序列本 rank。
- [src/transfer.cu:L26-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L26-L52)：`issue_copy`——`depth <= 1` 走 `cudaMemcpy2DAsync`，否则构造 `cudaMemcpy3DParms`：`make_cudaPitchedPtr(base, pitch, width, slice/pitch)` 与 `make_cudaExtent(width, rows, depth)`。注释点明「slice 计数是推导出来的，因为 3D 参数结构不接受 slice 步长」。

#### 4.2.4 代码实践

**目标**：打印一条真实 CopyOp 并逐字段手工核对。

**步骤**：取 `b=2, d=8, elem_size=2`（fp16，故 \( d_b = 16 \) 字节）、`seq_splits=[5,3,0,7]`、`head_splits=[3,2,2,1]`、mode 0、rank 1。rank 1 发给 peer 2 的那条 op 按公式应为：

| 字段 | 公式 | 手算 | 值 |
| --- | --- | --- | --- |
| `peer` | — | — | 2 |
| `src_offset` | \( H_2 \cdot d_b \) | \( 5 \times 16 \) | 80 |
| `src_pitch` | \( N \cdot d_b \) | \( 8 \times 16 \) | 128 |
| `dst_offset` | \( S_1 \cdot \text{row}_2 \) | \( 5 \times 32 \) | 160 |
| `dst_pitch` = `width` | \( h_2 \cdot d_b \) | \( 2 \times 16 \) | 32 |
| `rows` | \( s_1 \) | — | 3 |
| `depth` | \( b \)（融合成立时） | — | 2 |
| `src_slice` | \( s_1 N d_b \) | \( 3 \times 8 \times 16 \) | 384 |
| `dst_slice` | \( \text{seq\_total} \cdot \text{row}_2 \) | \( 15 \times 32 \) | 480 |

融合自检：\( 384 \bmod 128 = 3 \ge 3 \)、\( 480 \bmod 32 = 15 \ge 3 \)，且 `rows=3 > 1`、`depth=2 > 1` → 可融合。

```python
# 示例代码：copyop_check.py
import torch, fast_ulysses
shape, window, ops = torch.ops.fast_ulysses.a2a_plan_debug(
    2, 8, 1, 4, [5, 3, 0, 7], [3, 2, 2, 1], 0, 2)
cols = ["peer","src_off","dst_off","src_pitch","dst_pitch",
        "width","rows","depth","src_slice","dst_slice"]
for row in ops.tolist():
    print(dict(zip(cols, row)))
# 期望其中一行（peer==2）为：
# peer=2, src_off=80, dst_off=160, src_pitch=128, dst_pitch=32,
# width=32, rows=3, depth=2, src_slice=384, dst_slice=480
```

**需要观察的现象**：`ops` 是形状 `[n_ops, 10]` 的 int64 张量，列序与 [src/plan_bindings.cc:L14-L15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L14-L15) 的注释一致；rank 1 一共 4 条 op（每个 peer 一条，全部融合为 depth=2）。

**预期结果**：`peer==2` 那行与上表完全一致。本表由源码公式手算而来，实际运行输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `push_batched` 拒绝融合单行（`rows == 1`）拷贝？

**答案**：单行拷贝本身就是一次平坦 memcpy（`width == pitch` 时 CE 直接线性搬）；折成 3D 拷贝反而把它推上跨步路径。而多行拷贝本来就在跨步路径上，融合省掉 \( b-1 \) 次发射是纯赚。见 [src/a2a_plan.cc:L66-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L66-L69) 的注释，以及 [test/test_plan.py:L196-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L196-L203) 的 `test_batch_fusion_rule`（`rows=1` 时断言每 peer 发 `b` 条独立 2D 拷贝）。

**练习 2**：`CopyOp` 里为什么没有指针、只有偏移？

**答案**：偏移相对「本 rank 输入张量基址」与「peer 窗口基址」。指针在发射时才由传输层加上（[src/transfer.cu:L92](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L92)），且 peer 窗口地址要靠对称内存 rendezvous 才知道（u4-l1）。保持无指针让同一份结构可以被 numpy 在字节缓冲上原样重放——这是布局能脱离 GPU 测试的前提。

---

### 4.3 A2APlan：输出形状、窗口尺寸与发送清单

#### 4.3.1 概念说明

`A2APlan` 是 `build_plan` 的返回值，三个字段回答三个问题：

1. `output_shape`——**本 rank** 收到的输出长什么样（从窗口基址起是稠密的）。
2. `window_numel`——对称窗口必须装下多少元素。关键：这是**全组最大**那个 rank 的输出量，不是本 rank 的。
3. `ops`——本 rank 要**发**的拷贝清单，`ops[i].peer` 说明每条发去哪。

`window_numel` 取最大值的原因有两层（头文件注释给出）：**peer 的写入偏移只有当每个 rank 分配同样大小的窗口时才能对齐**——本 rank 给 peer \( p \) 算的 `dst_offset` 是按「\( p \) 的窗口按统一布局排布」假设写的；同时，每个 rank **无需通信**就能算出这个最大值，因为切分表描述的是整个组。

另一个关键设计：**本 rank 自己的份额也走窗口**（`ops` 覆盖全部 `world_size` 个目的地，包括 `peer == rank`）。这正是零拷贝路径需要的——窗口本身就是调用者的输出；也让 copying 路径的 copy-out 成为一次平坦拷贝（整窗一起搬出），见 [include/fast_ulysses/a2a_plan.hpp:L62-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L64) 的注释。

#### 4.3.2 核心流程

```
build_plan(dims, mode, elem_size):
  1. dims.validate(); elem_size、mode 合法性检查
  2. 算 seq_total / head_total / 两组 offsets / s_me / n_me
  3. check_byte_products —— 给所有字节量一个总上界（见 4.4）
  4. output_shape:
       mode 0 -> (b, seq_total, n_me, d)
       mode 1 -> (b, s_me, head_total, d)
  5. window_numel = max over p of  b * rows(p) * heads(p) * d
       mode 0: rows(p) = seq_total, heads(p) = head_splits[p]
       mode 1: rows(p) = seq_splits[p],  heads(p) = head_total
     （每次乘法都过 checked_mul）
  6. 对每个 peer p 发射一条 CopyOp（公式见 4.2；mode 1 是镜像：
     读连续、写跨步，因为窗口就是返回张量，没有本地整理的机会）
```

mode 0 的窗口尺寸公式：

\[ \text{window\_numel} = \max_p \; b \cdot \text{seq\_total} \cdot h_p \cdot d \]

#### 4.3.3 源码精读

- [include/fast_ulysses/a2a_plan.hpp:L53-L60](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L53-L60)：`A2APlan` 三字段；注释解释 window 取最大的两个理由（偏移对齐需要同尺寸；splits 全表在手机就能算最大值，无需通信）。
- [src/a2a_plan.cc:L162-L164](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L162-L164)：`output_shape` 按 mode 二选一。
- [src/a2a_plan.cc:L166-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173)：窗口尺寸循环——对每个 rank 算「假如它是接收方需要多少」，三层 `checked_mul` 嵌套，取 max。注释指向头文件里 window_numel 的说明。
- [src/a2a_plan.cc:L175-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L214)：发射循环。mode 0 分支（L176-L193）读跨步写连续；mode 1 分支（L194-L213）是逆操作，注释说明为什么 mode 1 必须写跨步——窗口即返回张量，没有「先落到每发送者一段、再本地交错」的余地。
- [test/test_plan.py:L141-L147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L141-L147)：测试对窗口的两条断言——**所有 rank 的 window_numel 必须相同**（各自独立计算却要裁出同样的偏移空间），且等于各 rank 输出量的最大值。这是本节论断的可执行版本。

#### 4.3.4 代码实践

**目标**：先预测 `window_numel`，再与 `a2a_plan_debug` 的返回值比对——体会「每个 rank 独立算出同一个最大值」。

**步骤**：沿用 4.2 的配置（`b=2, d=8, e=2`，`seq_splits=[5,3,0,7]`，`head_splits=[3,2,2,1]`，mode 0）。先手算：

\[ \text{window\_numel} = \max_p \; 2 \times 15 \times h_p \times 8 = \max_p \; 240 \, h_p = 240 \times 3 = 720 \]

对照各 rank 自身输出量：rank 0 是 \( 2 \times 15 \times 3 \times 8 = 720 \)（恰好最大），rank 1/2 是 480，rank 3 是 240——**多数 rank 的窗口都比自己的输出大**。

```python
# 示例代码：window_check.py
import torch, fast_ulysses
for r in range(4):
    shape, window, ops = torch.ops.fast_ulysses.a2a_plan_debug(
        2, 8, r, 4, [5, 3, 0, 7], [3, 2, 2, 1], 0, 2)
    print(f"rank {r}: shape={list(shape)} window={int(window)} n_ops={ops.shape[0]}")
# 预期（手算）：
# rank 0: shape=[2,15,3,8] window=720 n_ops=4
# rank 1: shape=[2,15,2,8] window=720 n_ops=4
# rank 2: shape=[2,15,2,8] window=720 n_ops=0   <- s_me=0，不发射
# rank 3: shape=[2,15,1,8] window=720 n_ops=4
```

**需要观察的现象**：四个 rank 的 `window` 全部是 720（同一个最大值）；rank 2 因 `seq_splits[2]=0` 一条 op 都不发，但它的 `output_shape` 仍然合法（`n_me=2`）。

**预期结果**：如上表。手算值由 [src/a2a_plan.cc:L166-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173) 公式推出，实际运行输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果 `window_numel` 取的是「本 rank 自己的输出量」而不是全组最大，会发生什么？

**答案**：不同 rank 会分配出**不同大小**的窗口，而本 rank 为 peer \( p \) 计算的 `dst_offset` 是按统一布局写的——比如 4.2 的例子里 rank 1 给 peer 0 写的偏移按「rank 0 的窗口能装下 720 个元素」假设，若 rank 0 只按自己的 480 分配（此例恰好相等，换一组 splits 就不等了），写入就会越界或错位。这正是测试断言「全组 window_numel 必须相同」的原因（[test/test_plan.py:L145-L146](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L145-L146)）。

**练习 2**：mode 1 下 `window_numel` 的公式是什么？用同一组 splits 算一遍。

**答案**：mode 1 下每个 rank 接收 `(b, s_p, head_total, d)`，所以 \[ \text{window\_numel} = \max_p \; b \cdot s_p \cdot \text{head\_total} \cdot d = 2 \times 8 \times 8 \times \max_p s_p = 128 \times 7 = 896 \]。注意两个 mode 的窗口大小**不相等**（720 vs 896），但同一 mode 内全组一致——u4-l2 会讲到窗口按角色缓存，sync 路径里两个 mode 各自触发自己的增长。

**练习 3**：为什么说「本 rank 份额也走窗口」让 copy-out 变成一次平坦拷贝？

**答案**：所有发送方（包括本 rank 自己）都把数据写进窗口的稠密前缀，窗口内容从基址起就是完整的输出张量字节。copy-out 因此不需要任何重排，只需把整段字节从窗口搬进调用者的输出张量——一次 `cudaMemcpyAsync`。若自己的份额直接留在输入里，copy-out 就得处理「一部分在窗口、一部分在本地」的拼接。

---

### 4.4 validate 与溢出防护：先于一切分配的防御

#### 4.4.1 概念说明

`build_plan` 的开头是三道闸门：`dims.validate()`、`elem_size` 检查、`mode` 检查（[src/a2a_plan.cc:L143-L151](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L143-L151)）。纪律是：**校验必须先于任何分配**（头文件注释：Call before anything allocates）。这与 u2-l2 讲过的「全部校验先于第一个屏障」一脉相承——被拒的参数不该留下任何副作用。

溢出防护是本模块的主角。要害在于：上层入口 `make_dims`（src/group.cc，u2-l3 讲过）只拿**本 rank 的张量**和 splits 交叉校验，**peer 的表项不受任何张量约束**。如果 `seq_splits = [1, 2^62]`，`window_numel` 的乘积会溢出 int64——注释原话：**一个回绕的乘积给出负的 window_numel，而每个下游尺寸检查都会把它读成「够小」**。防护分三层：

1. `checked_mul(a, b)`：乘之前检查 \( b > \lfloor \text{max}/a \rfloor \)。
2. `checked_sum(values)`：累加时检查运行和。
3. `check_byte_products`：给**所有字节量**（offset、pitch、width、slice 都是五个因子的部分乘积）一个总上界——因为 window_numel 按元素计，管不住按字节计的量；而有符号溢出是 UB，连「变成负数被下游抓住」都不可指望。

#### 4.4.2 核心流程

```
validate():
  world_size > 0
  0 <= rank < world_size
  b > 0 且 d > 0
  len(seq_splits)  == world_size
  len(head_splits) == world_size
  每个表项 >= 0      （== 0 合法：零分片；< 0 拒绝）
  任一失败 -> throw std::invalid_argument("fast_ulysses: <指名字段的问题>")

checked_mul(a, b):
  若 a != 0 且 b > INT64_MAX / a -> fail（"overflows int64"）
  否则返回 a * b

check_byte_products(dims, seq_total, head_total, elem_size):
  bound = ∏ max(factor, 1)  for factor in {b, seq_total, head_total, d, elem_size}
  每步乘法走 checked_mul          （空轴按 1 计：它只是让拷贝不发射，不会让别的因子变小）
```

#### 4.4.3 源码精读

- [src/a2a_plan.cc:L113-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L113-L141)：`validate` 全文。六组检查、每条消息指名字段与实际值（例如 `seq_splits has 2 entries, expected 4`）；L132 的注释划清边界——零分片合法、负数不合法。
- [src/a2a_plan.cc:L19-L25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L19-L25)：`checked_mul`。注意 `a != 0` 守卫避免除零；注释解释为什么必须有它（peer 表项无界 → 乘积回绕 → 负 window_numel 被下游当「够小」）。
- [src/a2a_plan.cc:L27-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L27-L37)：`checked_sum`——对每个元素检查 `v > max - total` 再累加，防求和本身溢出。
- [src/a2a_plan.cc:L39-L50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L39-L50)：`check_byte_products`。注释（L39-L43）给出三点理由：window_numel 按元素计、字节量是五个因子的部分乘积所以一个上界全覆盖、有符号溢出是 UB 而非「可被后续检查抓住的负数」；空轴按 1 计。
- [src/a2a_plan.cc:L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L159)：`check_byte_products` 在 `build_plan` 里的调用位置——在窗口循环和发射循环**之前**，即任何字节量被算出来之前。
- [test/test_plan.py:L317-L333](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L317-L333)：溢出用例与**负控制**注释——测试文件明确写着：把 `checked_mul`/`checked_sum` 拆掉重编译，这两个用例就会「成功」返回一个带负 window_numel 的计划而不是抛异常。负控制的存在说明作者知道这类检查最容易被「顺手简化」掉。

#### 4.4.4 代码实践

**目标**：亲手触发两类溢出，并对照负控制注释理解「如果不检查会怎样」。

**步骤**：

```python
# 示例代码：overflow_check.py
import torch, fast_ulysses
op = torch.ops.fast_ulysses.a2a_plan_debug

cases = [
    ([1, 2**62], [1, 1], "乘积溢出：b*seq_total*heads*d*e 超过 int64"),
    ([2**62, 2**62], [1, 1], "求和溢出：seq_total 本身就超过 int64"),
]
for seq, head, why in cases:
    try:
        op(1, 8, 0, 2, seq, head, 0, 2)
        print("NOT REJECTED!", why)
    except Exception as e:  # noqa: BLE001 —— 与 test/test_plan.py 的捕获方式一致
        print("OK ->", e, "   #", why)
```

**需要观察的现象**：两条都被拒，消息都含 `overflows int64`。第一条实际由 `check_byte_products` 触发（消息形如 `the exchange in bytes overflows int64 (...)`，它先于窗口循环执行）；第二条由 `checked_sum`（经 `seq_total()`）触发（消息形如 `seq_splits total overflows int64`）。

**预期结果**：与 [test/test_plan.py:L324-L333](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L324-L333) 的断言一致（`match="overflows int64"`）。实际运行输出待本地验证。

然后做一个**思想实验**（对应负控制）：如果把 `checked_mul` 换成裸的 `*`，第一个用例里 \( 1 \times (1 + 2^{62}) \times 1 \times 8 \times 2 \) 回绕后的值按补码可能得到负数，而窗口分配处的 `if (need > window.numel())` 之类的比较会把负数当「够小」放行——于是得到一个远小于所需的窗口。写出这条因果链（三行以内）即可。

#### 4.4.5 小练习与答案

**练习 1**：`validate` 为什么允许 `seq_splits` 里出现 0，却不允许负数？

**答案**：0 是真实会发生的几何——不均分时某个 rank 可能恰好分不到序列（[test/test_plan.py:L179-L181](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L179-L181) 的 `test_empty_shard` 专门覆盖 `[3,0,5,4]`），此时该 rank 只是不发射拷贝；负数没有任何合法语义，且会让前缀和产生重叠区间，所以直接拒绝（[src/a2a_plan.cc:L132-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L132-L140)）。

**练习 2**：`check_byte_products` 为什么把空轴按 1 计，而不是跳过？

**答案**：一个为 0 的轴（比如 `b == 0`）确实让拷贝不再发射，但它**不会让其他因子变小**——发射公式里剩下的因子（seq_total、head_total、d、elem_size）的乘积仍然会被算出来。若按 0 跳过，上界就漏掉了这些量。按 1 计让上界覆盖「即使什么都没发，中间乘积也可能溢出」的情形（[src/a2a_plan.cc:L42-L43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L42-L43) 注释）。

**练习 3**：为什么 `checked_mul` 的条件是 `a != 0 && b > max / a`，而不是写完乘法再检查结果为负？

**答案**：两个原因。其一，有符号溢出在 C++ 里是 UB——写完再查根本不保证能观察到「负数」，编译器可以假设溢出不发生而删掉检查。其二，正溢出的结果未必是负数（可能回绕成小正数），事后检查连「恰好变小的窗口」都抓不住。事前用整数除法比较是唯一可靠的做法；`a != 0` 守卫避免除零（[src/a2a_plan.cc:L19-L25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L19-L25)）。

## 5. 综合实践

**任务**：完整执行本讲规格中的调试流程——用 `torch.ops.fast_ulysses.a2a_plan_debug` 打印 `ws=4`、`seq_splits=[5,3,0,7]` 下**每个 rank** 的 mode 0 ops 表，手工核对其中一行的全部字段，并**先预测再比对** `window_numel`。

配置约定：`b=2, d=8, elem_size=2`（fp16），`head_splits=[3,2,2,1]`（刻意取不均匀，让窗口最大值落在 rank 0 而非「人人相等」）。

```python
# 示例代码：practice_u3l1.py —— 本实践不需要 GPU，只需已构建的 fast_ulysses 扩展
import torch
import fast_ulysses  # import 即注册 torch.ops.fast_ulysses（见 test_plan.py:19-21）

WS, SEQ, HEAD = 4, [5, 3, 0, 7], [3, 2, 2, 1]
B, D, ELEM, MODE = 2, 8, 2, 0
COLS = ["peer", "src_off", "dst_off", "src_pitch", "dst_pitch",
        "width", "rows", "depth", "src_slice", "dst_slice"]

# ---- 第 0 步：先在纸上预测（不许先跑代码） ----
# seq_offsets = [0,5,8,8], head_offsets = [0,3,5,7], seq_total=15, head_total=8, d_bytes=16
# window_numel = max_p 2*15*head[p]*8 = 240*3 = 720
PREDICT_WINDOW = 720

plans = []
for r in range(WS):
    shape, window, ops = torch.ops.fast_ulysses.a2a_plan_debug(
        B, D, r, WS, SEQ, HEAD, MODE, ELEM)
    plans.append({"shape": list(shape), "window": int(window),
                  "ops": [dict(zip(COLS, row)) for row in ops.tolist()]})

# ---- 第 1 步：窗口预测比对 ----
assert {p["window"] for p in plans} == {PREDICT_WINDOW}, "窗口预测失败"
print("window_numel 预测正确:", PREDICT_WINDOW)

# ---- 第 2 步：手工核对 rank 1 -> peer 2 那一行 ----
# 公式（mode 0，r=1，p=2）：
#   row      = head[2]*d_bytes      = 2*16 = 32
#   src_off  = head_off[2]*d_bytes  = 5*16 = 80
#   src_pitch= head_total*d_bytes   = 8*16 = 128
#   dst_off  = seq_off[1]*row       = 5*32 = 160
#   dst_pitch=width= row            = 32 ;  rows = seq[1] = 3
#   融合：src_slice = 3*8*16 = 384 (384/128=3 >= 3)
#         dst_slice = 15*32  = 480 (480/32 =15 >= 3) -> depth = b = 2
HAND = {"peer": 2, "src_off": 80, "dst_off": 160, "src_pitch": 128,
        "dst_pitch": 32, "width": 32, "rows": 3, "depth": 2,
        "src_slice": 384, "dst_slice": 480}
got = next(op for op in plans[1]["ops"] if op["peer"] == 2)
assert got == HAND, f"手工核对失败: {got} != {HAND}"
print("rank1->peer2 手工核对通过:", got)

# ---- 第 3 步：打印全部 ops 表，观察结构性事实 ----
for r, p in enumerate(plans):
    print(f"\nrank {r}: shape={p['shape']} window={p['window']}")
    for op in p["ops"]:
        print("   ", op)
# 结构性预期（手算）：
#   rank 0/1/3 各 4 条 op（每个 peer 一条，全部融合 depth=2）
#   rank 2   0 条 op（seq[2]=0），但 shape/window 仍然正常
#   每条 op 的 dst_off 都等于 seq_off[r] * (head[p]*16)
```

**操作步骤**：

1. 在已安装 fast-ulysses（u1-l3）的环境里运行 `python practice_u3l1.py`。
2. 跑之前先把 `PREDICT_WINDOW` 和 `HAND` 两处换成**你自己**的手算结果，再让脚本判定。
3. 观察输出的结构性事实：rank 2 零 op、所有 rank 窗口同为 720、每条 op 的 `dst_off` 与 `seq_off[r]` 的关系。

**预期结果**：三个断言全部通过；ops 表与第 3 步注释里的结构性预期一致。所有期望值均由 4.2/4.3 节的源码公式手算得出；实际运行输出待本地验证。

**延伸思考**（为下一讲热身）：把 `MODE` 改成 1 再跑——你会看到同样的 splits 发射出的却是「读连续、写跨步」的镜像计划，且窗口变成 896。为什么两个 mode 用**同一对** splits 而不交换？带着这个问题进入 u3-l2。

## 6. 本讲小结

- `A2ADims` 把一次交换的全部几何（b、d、rank、world_size、两张切分表）装进纯主机结构；全 rank 持同一张表，前缀和免通信地给出全组偏移——这是「不均分是一般路径、偶均分是特例」的单一代码路径。
- `CopyOp` 用十个整数描述一条步长拷贝，偏移相对缓冲基址、无指针，因此同一结构既是 `cudaMemcpy2D/3DAsync` 的参数源，也是 numpy 重放的脚本——布局正确性可以脱离 GPU 验证。
- `depth`/`src_slice`/`dst_slice` 把批维度折叠成一条 3D 拷贝，但只对多行拷贝且步长可整除 pitch 时融合（`push_batched` 的两条判定）。
- `A2APlan` 的 `window_numel` 取全组最大接收量：peer 的写入偏移只有在所有 rank 分配同尺寸窗口时才对齐；每个 rank 凭全表无需通信即可算出同一个最大值。本 rank 自己的份额也走窗口，这让零拷贝路径成为可能、copy_out 成为一次平坦拷贝。
- `validate` 六组检查全部先于分配、指名字段报错；`checked_mul`/`checked_sum`/`check_byte_products` 三层防护拦住「peer 表项无界 → 乘积回绕 → 负窗口被下游当够小」这条路径，因为有符号溢出是 UB，事后检查不可靠。

## 7. 下一步学习建议

下一讲 **u3-l2（build_plan 精读：两种模式的拷贝发射）** 将把本讲 的字段公式扩展成完整的发射逻辑：mode 0 读跨步写连续、mode 1 读连续写跨步的镜像关系、「本 rank 份额也走窗口」这一决定在两条路径上分别兑现成什么，以及 `push_batched` 的融合判定在真实形状下的得与失。建议先完成本讲综合实践，并顺手把 `MODE` 改成 1 跑一遍，带着「同一对 splits 为什么不交换」的疑问去读。

之后再进入 **u3-l3（无 GPU 的布局测试）**：看 `plan_bindings.cc` 如何把纯主机 plan 暴露成 `a2a_plan_debug` 算子、`test_plan.py` 如何用 numpy 字节缓冲重放 CopyOp 并对照 `all_to_all_single + permute` 的参照——你今天手算的每一条公式，那里都有可执行的断言。窗口如何被分配成真实显存，则要等 **u4-l1（torch 对称内存与窗口分配）**。
