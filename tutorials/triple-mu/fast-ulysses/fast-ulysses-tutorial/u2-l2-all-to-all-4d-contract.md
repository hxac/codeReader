# u2-l2 · all_to_all_4d 的形状契约与错误检查

## 1. 本讲目标

学完本讲，你应该能够：

1. **由输入形状与 mode 推出输出形状**：给一个 `(b, s, n, d)` 和 `mode=0/1`（以及可选的 splits），不运行任何代码就能写出输出的四维形状。
2. **解释 dtype 白名单与 16 字节对齐规则**：知道哪 7 种 dtype 被接受，理解为什么规则写成 `d * elem_size` 必须是 16 的倍数，以及它如何随元素变窄而收紧（fp16 要 `d % 8`、fp32 要 `d % 4`、fp8/int8 要 `d % 16`）。
3. **把 docs/api.md 的 Raises 清单逐条落到源码行**：每条文档化的 `RuntimeError` 都能指到是哪个文件的哪个 `TORCH_CHECK` 抛的，并理解「校验必须全部发生在第一个屏障之前」这条工程纪律。
4. **复述集体调用契约五条**（形状序列、dtype 序列、集合性操作、backward 顺序、一缓冲一调用），并解释为什么违反契约的后果是**挂起而非报错**——这五种违规没有任何一条会被 `try/except` 捕到。

本讲承上启下：u1-l2 讲了「为什么要有两次 all-to-all」，u2-l1 讲了「组怎么建」；本讲回答「一次调用合法地长什么样」——这是后面 u3（布局计划）、u4（窗口管理）一切源码的前置契约。

## 2. 前置知识

- **张量形状记号**：`b` 批大小、`s` 序列长度、`n` 注意力头数、`d` 每头维度、`ws` 世界大小（rank 数）。`s_local` / `n_local` 是本 rank 持有的分片，`s_global = sum(seq_splits)`、`n_global = sum(head_splits)` 是全组总量。
- **SPMD 与集合操作**（u1-l2、u2-l1 已建立）：所有 rank 执行同一段程序；`all_gather`、对称内存 rendezvous 这类操作要求**每个 rank 都到达**才有人推进。一个 rank 提前抛错退出、别人还在等它，就是「挂起」。
- **mode 0 / mode 1**（u1-l2 已建立）：mode 0 用「序列分片 × 全部头」换「完整序列 × 头分片」，mode 1 是它的严格逆变换。本讲只关心**形状怎么对应**，不关心数据怎么搬（那是 u3/u5 的事）。
- **TORCH_CHECK 与 RuntimeError**：torch C++ 源码里 `TORCH_CHECK(条件, 消息)` 在条件不满足时抛 `c10::Error`，穿过 pybind11 到 Python 就是 `RuntimeError`。本讲看到的每条报错都是它。
- **16 字节对齐的直觉**：GPU 拷贝引擎搬数据时按「行」拷，一行是 `d * elem_size` 个字节。硬件对 16 字节整数倍的地址/宽度有天然偏好（这关系到能否走最快的拷贝路径）。本讲只需记住：**库里所有拷贝的起点、步长、宽度都是 `d * elem_size` 的倍数，所以只要这一个数对齐，全部对齐**——一条检查覆盖整个计划。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python 门面：构造、两次调用、输出分配 | `all_to_all_4d` 的签名与 `out=` 的 autograd 拒绝 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | torch 算子层：校验张量、取 plan 与窗口，然后屏障→传输→屏障→拷出 | `require_cuda`、`require_group_device`、`prepare` 的校验链与重叠检查 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | 有状态层：plan 缓存、窗口分配 | `make_dims`（4D、dtype、对齐、mode、splits 的全部检查）与 `allocate` 的空分片拒绝 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 纯主机寻址层（无 CUDA、无 torch） | `A2ADims::validate` 与 `build_plan` 里与 mode 相关的重复检查（供无 GPU 路径使用） |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 契约文档 | 集体契约五条（L6-L20）、mode 形状表（L44-L47）、dtype 与对齐（L49-L52）、Raises 全表（L93-L103） |

一次同步调用的骨架（u1-l4 已建立）：`group.py` 转发 → `prepare`（**本讲主角**：全部校验）→ 屏障 → 传输 → 屏障 → `copy_out`。记住这张图，4.3 节的一切都围绕「校验必须全部发生在第一个屏障之前」展开。

## 4. 核心概念与源码讲解

### 4.1 mode 形状表：由输入形状与 mode 推出输出形状

#### 4.1.1 概念说明

`all_to_all_4d` 是一个**纯重排**：它不计算任何东西，只把「序列轴上的分片」和「头轴上的分片」互换角色。所以输出形状完全由输入形状、mode、以及（可选的）splits 决定——不需要看数据，甚至不需要 GPU。这份「形状推导规则」就是本模块要掌握的**形状契约**。

不传 splits 时是偶均分特例；传了 splits，`seq_splits[p]` / `head_splits[p]` 表示「rank p 拥有的序列片长 / 头片数」，这是**组几何**的描述，与数据向哪个方向流动无关（这个性质在 u6-l3 讲 backward 时会再次出现：backward 不交换 splits）。

#### 4.1.2 核心流程

记 \( s_{\text{global}} = \sum_p \text{seq\_splits}[p] \)，\( n_{\text{global}} = \sum_p \text{head\_splits}[p] \)，\( n_{\text{local}} = \text{head\_splits}[\text{rank}] \)，\( s_{\text{local}} = \text{seq\_splits}[\text{rank}] \)。

不传 splits 时，由形状反推偶均分：

- mode 0 输入 `(b, s_local, n_global, d)` → 推得 `seq_splits = [s_local]*ws`、`head_splits = [n_global/ws]*ws`
- mode 1 输入 `(b, s_global, n_local, d)` → 推得 `seq_splits = [s_global/ws]*ws`、`head_splits = [n_local]*ws`

然后套用形状表：

| mode | 输入 | 输出 |
| --- | --- | --- |
| 0 — 散头、聚序列 | `(b, s_local, n_global, d)` | `(b, s_global, n_local, d)` |
| 1 — 逆变换 | `(b, s_global, n_local, d)` | `(b, s_local, n_global, d)` |

推导顺序（伪代码）：

```text
resolve_splits(x_shape, mode, ws, splits?):
    if splits 缺失:
        mode 0: 要求 n_global % ws == 0；seq=[x1]*ws, head=[x2/ws]*ws
        mode 1: 要求 s_global % ws == 0；seq=[x1/ws]*ws, head=[x2]*ws
    else:
        两组都必须给，且与 x 的形状交叉吻合
    if mode == 0: return (b, sum(seq), head[rank], d)
    else:         return (b, seq[rank],  sum(head), d)
```

注意 `ws = 1` 的特例：`s_local == s_global`、`n_local == n_global`，两个 mode 的形状都退化成恒等——这就是为什么 u1-l3 说「world_size=1 是合法一等公民」，可以用它做单卡冒烟测试。

#### 4.1.3 源码精读

形状表的文档版在 [docs/api.md:L44-L47](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L44-L47)。

真正的形状推导在 `src/group.cc` 的 `make_dims`。先看偶均分特例——mode 0 检查被散射的轴（头轴）能整除，然后直接从形状造出两组 splits：

[src/group.cc:L115-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L115-L124)
```cpp
if (mode == 0) {
    TORCH_CHECK(x2 % ws == 0,
                "mode 0 scatters the head axis, so n_global (",
                x2, ") must divide world_size (", ws,
                ") -- or pass seq_splits and head_splits");
    dims.seq_splits.assign(ws, x1);
    dims.head_splits.assign(ws, x2 / ws);
}
```
这段在说：mode 0 下 `x2` 是全部头数 `n_global`，它必须能被 `ws` 整除（报错消息里写的是 `n_global must divide world_size`，语义是 `n_global % ws == 0`）；`x1` 就是本 rank 的序列片长，所以 `seq_splits` 全填 `x1`。mode 1 的对称分支在 [src/group.cc:L126-L134](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L126-L134)。

再看显式 splits 的交叉校验——splits 描述的形状必须和手里的张量吻合：

[src/group.cc:L90-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L90-L109)
```cpp
const int64_t expect_x1 = (mode == 0) ? dims.seq_splits[rank] : dims.seq_total();
const int64_t expect_x2 = (mode == 0) ? dims.head_total() : dims.head_splits[rank];
TORCH_CHECK(x1 == expect_x1 && x2 == expect_x2,
            "input is [", dims.b, ", ", x1, ", ", x2, ", ", d,
            "] but the splits imply [", ... "]");
```
这段是形状表的可执行形式：mode 0 时输入的第二维必须是 `seq_splits[rank]`（本 rank 的序列片）、第三维必须是 `head_total()`（全部头）；mode 1 正好反过来。**分片错了不产生错数据，而是直接报错**——因为排列错误意味着拷贝偏移全错，静默给出损坏的结果是最坏结局。

最终输出形状在 `build_plan` 里拼出来：

[src/a2a_plan.cc:L163-L164](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L163-L164)
```cpp
plan.output_shape = (mode == kScatterHead) ? std::vector<int64_t>{dims.b, seq_total, n_me, dims.d} :
                                             std::vector<int64_t>{dims.b, s_me, head_total, dims.d};
```
和形状表逐字对应。Python 侧只是薄薄一层转发——`out is None` 时直接调 functional 算子：[python/fast_ulysses/group.py:L129-L132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L132)。

#### 4.1.4 代码实践

**实践目标**：不看文档、手写形状推导函数，再用真实调用验证。

1. 阅读目标：合上文档，写出 `output_shape(x_shape, mode, ws, rank, seq_splits=None, head_splits=None)`。
2. 操作步骤（示例代码，纯主机可运行，无需 GPU）：

```python
# 示例代码：形状契约的纯 Python 表达，可对照 docs/api.md 的表格逐行核对
def output_shape(x_shape, mode, ws, rank=0, seq_splits=None, head_splits=None):
    b, x1, x2, d = x_shape
    if seq_splits is None:
        assert head_splits is None, "两组 splits 要么都传要么都不传"
        if mode == 0:
            seq_splits, head_splits = [x1] * ws, [x2 // ws] * ws
        else:
            seq_splits, head_splits = [x1 // ws] * ws, [x2] * ws
    if mode == 0:
        return (b, sum(seq_splits), head_splits[rank], d)
    return (b, seq_splits[rank], sum(head_splits), d)

# 自检 1：偶均分 mode 0，ws=4
assert output_shape((2, 16, 40, 128), 0, 4, rank=1) == (2, 64, 10, 128)
# 自检 2：不均匀 splits（s=75827 的 8 路切分，头 40 路切分）
seq = [9479, 9478, 9478, 9478, 9478, 9478, 9478, 9480]
head = [5, 5, 5, 5, 5, 5, 5, 5]
assert sum(seq) == 75827 and sum(head) == 40
assert output_shape((1, seq[3], 40, 128), 0, 8, rank=3, seq_splits=seq, head_splits=head) == (1, 75827, 5, 128)
```

3. 需要观察的现象：三个 assert 全部无声通过。
4. 预期结果：如果哪个 assert 失败，说明你对形状表的理解和实现不一致——回到 4.1.2 的表格找差异。有 GPU 时可追加一步真实对照：`torchrun --nproc_per_node=1` 下构造 `UlyssesGroup`，调用一次并打印 `y.shape`（待本地验证；无 GPU 时以文档表格为参照即可，本函数不触碰设备）。

#### 4.1.5 小练习与答案

**练习 1**：`ws=8`，输入 `(1, 4096, 40, 384)`，`mode=0`，不传 splits。会发生什么？

**答案**：mode 0 要求头轴整除：`n_global=40`，`40 % 8 == 0`，合法。推得 `seq_splits=[4096]*8`、`head_splits=[5]*8`，输出 `(1, 32768, 5, 384)`。若头数换成 44（`44 % 8 != 0`），则在 `make_dims` 抛 `RuntimeError`，消息以 `mode 0 scatters the head axis` 开头。

**练习 2**：mode 1 下输入 `(2, 128, 6, 64)`、`ws=4`、不传 splits，输出是什么？反过来把输出喂给 mode 0 会得到什么？

**答案**：mode 1 输入形状是 `(b, s_global, n_local, d)`，检查 `128 % 4 == 0`，推得 `seq_splits=[32]*4`、`head_splits=[6]*4`，输出 `(2, 32, 24, 64)`。把 `(2, 32, 24, 64)` 喂给 mode 0：检查 `24 % 4 == 0`，`seq_splits=[32]*4`、`head_splits=[6]*4`，输出 `(2, 128, 6, 64)`——还原成最初输入。两次调用互逆，这正是 u2-l4 示例里「包夹注意力」成立的前提。

**练习 3**：为什么 `ws=1` 时任何头数都不会触发整除检查的报错？

**答案**：任何整数对 1 取模都是 0。`make_dims` 的两个整除分支（`x2 % ws`、`x1 % ws`）在 `ws=1` 下恒真，所以该检查在单 rank 下「不可达」——这也是本讲综合实践中我们无法在 `ws=1` 下测整除报错的原因（test/distributed/validation.py:L123 对此有同样的备注）。

### 4.2 dtype 白名单与 16 字节对齐

#### 4.2.1 概念说明

传输层搬运的是**字节**，不理解数值含义——`build_plan` 只接收一个 `elem_size`，窗口和 plan 缓存的键里带 dtype，仅此而已。既然如此，为什么要限制 dtype？

因为「接受哪些类型」是一个**产品决策而非技术约束**：源码注释明确说这是「listed, not opened up」（列出来的，不是推导出来的）。放行 `kBool`、复数、量化类型意味着要为这些没人推理过的类型背书。于是白名单收下训练里实际会用的 7 种：`float16 / bfloat16 / float32 / float8_e4m3fn / float8_e5m2 / int8 / uint8`。

对齐规则 `d * elem_size` 必须是 16 的倍数，来自传输的物理形态：拷贝引擎按「一行 `d * elem_size` 字节」做二维/三维拷贝（u3、u5 展开），所有偏移、步长、宽度都是这个数的倍数。**检查一行，对齐全局**——这是把一条约束放在最便宜位置（纯主机、进 plan 缓存键）的典型设计。

注意规则的**不对称收紧**：约束的是字节数不是维数，所以元素越窄，对 `d` 的要求越苛刻。fp16 要 `d % 8 == 0`，fp32 只需 `d % 4 == 0`，fp8/int8 则要 `d % 16 == 0`。

#### 4.2.2 核心流程

被接受的 dtype 与对应的对齐要求：

| dtype | elem_size | 对 `d` 的要求 | 例：`d=128` | 例：`d=8` |
| --- | --- | --- | --- | --- |
| float16 | 2 B | \( d \times 2 \equiv 0 \pmod{16} \Rightarrow d \bmod 8 = 0 \) | ✅ 256 B | ✅ 16 B |
| bfloat16 | 2 B | `d % 8 == 0` | ✅ | ✅ |
| float32 | 4 B | `d % 4 == 0` | ✅ | ✅ |
| float8_e4m3fn / float8_e5m2 | 1 B | `d % 16 == 0` | ✅ | ❌ 8 B |
| int8 / uint8 | 1 B | `d % 16 == 0` | ✅ | ❌ 8 B |

检查流程（`make_dims` 内，进入 splits 逻辑之前）：

```text
取 d = sizes[3]，elem = elementSize(dtype)
检查 (d * elem) % 16 == 0
    失败 → "the head dim must be 16-byte aligned: d=.. x .. B is .. B"
```

一个直接推论：`d=8` 在 bfloat16 下合法、转成 float8_e4m3fn 后非法——同一个形状在不同 dtype 下对齐结论不同，test/distributed/validation.py:L103-L108 专门测了这一条。

#### 4.2.3 源码精读

dtype 白名单与「这是决策不是约束」的注释：

[src/group.cc:L59-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L59-L62)
```cpp
TORCH_CHECK(dtype == at::kHalf || dtype == at::kBFloat16 || dtype == at::kFloat || dtype == at::kFloat8_e4m3fn
                || dtype == at::kFloat8_e5m2 || dtype == at::kChar || dtype == at::kByte,
            "dtype must be float16, bfloat16, float32, float8_e4m3fn, float8_e5m2, int8 or uint8, got ",
            dtype);
```
七个类型逐一列出；其上方 [src/group.cc:L54-L58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L54-L58) 的注释解释了为什么不全放行（`kBool`、复数、量化类型「nothing here has reasoned about」）。

对齐检查及其「一条检查覆盖整个计划」的论证：

[src/group.cc:L67-L71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L67-L71)
```cpp
// One check covers the whole plan: every byte quantity build_plan emits -- src_offset,
// dst_offset, width, both pitches, the batch strides -- is a multiple of d * elem. Note the
// rule TIGHTENS as the element shrinks: d % 8 at fp16, d % 4 at fp32, d % 16 at fp8 and int8.
TORCH_CHECK(
    (d * elem) % 16 == 0, "the head dim must be 16-byte aligned: d=", d, " x ", elem, " B is ", d * elem, " B");
```
报错消息把违规的算式直接打出来（`d=4 x 2 B is 8 B`），调用者不用反推。文档版同一条规则在 [docs/api.md:L49-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L49-L52)，并补了一句「检查之下没有任何东西是 dtype 相关的——传输搬的是字节」。

注意位置：这两条检查都在 `make_dims` 里，而 `make_dims` 只在 **plan 缓存未命中**时运行（见 [src/group.cc:L182-L195](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L182-L195)）。这不会有漏洞，因为「这里检查的每一样东西都是 plan 缓存键的一部分」——同一组 `(sizes, mode, dtype, splits)` 上次通过了检查，缓存命中直接复用是安全的（注释在 [src/group.cc:L39-L44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L39-L44)）。

#### 4.2.4 代码实践

**实践目标**：把对齐表内化成可运行的判定器。

1. 实践目标：写一个 `head_dims_ok(d, dtype)`，对 `d ∈ {4, 6, 8, 12, 16, 96, 128}` × 全部 7 种 dtype 打一张 ✅/❌ 表。
2. 操作步骤（示例代码，纯主机）：

```python
# 示例代码：对齐规则的判定器，直接对照 docs/api.md 的规则
import torch

OK_DTYPES = [torch.float16, torch.bfloat16, torch.float32,
             torch.float8_e4m3fn, torch.float8_e5m2, torch.int8, torch.uint8]

def aligned(d, dtype):
    return (d * torch.empty((), dtype=dtype).element_size()) % 16 == 0

for d in (4, 6, 8, 12, 16, 96, 128):
    row = {str(dt).replace("torch.", ""): "OK" if aligned(d, dt) else "no" for dt in OK_DTYPES}
    print(d, row)
```

3. 需要观察的现象：`d=4` 一行只有 float32 是 OK；`d=8` 一行 float16/bfloat16/float32 OK、两种 fp8 和 int8/uint8 是 no；`d=16` 起全 OK。
4. 预期结果：表格与 4.2.2 的公式一致。挑 `d=4`（bf16）做一次真实调用，应抛出消息含 `16-byte aligned` 的 `RuntimeError`（待本地验证，见综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`d=6` 的 bfloat16 张量（`6×2=12` B）会被接受吗？`d=6` 的 float32（`6×4=24` B）呢？

**答案**：bfloat16：12 不是 16 的倍数，拒绝；float32：24 同样不是 16 的倍数，也拒绝。容易错以为是「`d` 是不是 2 的幂」的问题——判据始终是**字节数** `d * elem_size` 对 16 取模。

**练习 2**：为什么检查放在 `make_dims`（plan 缓存键的一部分）而不是每次调用都查的 `prepare` 里？

**答案**：因为对齐与否只依赖 `(sizes, dtype)`，而这两者连同 mode、splits 一起构成 plan 缓存键：同一组值一旦通过检查并缓存，后续命中不可能携带未检查的新值。相反，依赖**张量本身**而非形状的检查（是否 CUDA、是否连续、与窗口是否重叠）无法进缓存键，必须留在 `prepare` 每次调用执行——这正是 [src/group.cc:L39-L44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L39-L44) 注释划分的两类。

**练习 3**：一个 `d=8` 的 bfloat16 张量 `.to(torch.float8_e4m3fn)` 后再调用，两次的对齐结论分别是什么？

**答案**：bfloat16 下 `8×2=16` B，合法；转 float8 后 `8×1=8` B，在 `make_dims` 抛 `RuntimeError`（消息含 `16-byte aligned`）。dtype 变了、形状没变，但规则跟着**字节**走——validation.py 把这条作为独立用例（"a head dim that is aligned at bfloat16 but not at float8"）。

### 4.3 Raises 清单：每条拒绝对应哪里，为何都在第一个屏障之前

#### 4.3.1 概念说明

docs/api.md 的 Raises 一节列出了所有文档化的 `RuntimeError`。本模块做三件事：**逐条落到源码**、**理解排序原则**、**认清它的边界**。

排序原则是本讲最重要的一条工程纪律：**全部校验必须发生在调用的第一个屏障之前**。原因：这是一次集合调用。如果 rank 0 的参数非法、rank 1 的合法，而非法问题在「rank 0 已经进入第一个屏障、开始等所有人」之后才被发现——rank 0 抛错退出，rank 1 永远等下去。所以 `prepare` 的第一句注释就是「Everything here runs BEFORE the call's first barrier, so a rejected argument leaves no rank waiting on peers that did not reject it」（被拒的参数不会留下任何 rank 干等没拒它的同伴）。

边界：这份清单只覆盖**本 rank 单方面可判定**的非法性。需要跨 rank 一致性才能判定的（形状序列、dtype 序列……）属于 4.4 的契约，那里**没有**报错，只有挂起。两节合起来是同一枚硬币的两面：能本地判定的 → 提前抛；只能靠约定的 → 写进契约。

#### 4.3.2 核心流程

`prepare` 的校验链（严格顺序，箭头后是被拒的后果）：

```text
check_alive()            → "this UlyssesGroup has been destroyed"
prune_owned()            （回收缓冲，不抛）
require_cuda(input)      → "input must be a CUDA tensor, got one on cpu"
require_group_device()   → "input is on cuda:X but this UlyssesGroup was built for cuda:Y"
input.contiguous()       （接受非连续输入，内部拷平）
group->plan(...)         → make_dims 的五连：
                             4D 检查 / dtype 白名单 / 16B 对齐 / mode ∈ {0,1} / splits 检查
out 分支（若有 out）      → contiguous CUDA / 设备 / dtype / 形状 四连
窗口获取 window()         → allocate()："this call moves no data"（全 rank 分片为空）
重叠检查                 → "input overlaps the window" / "out overlaps the window"
────────── 以上全部完成，才发出第一个屏障 fast_barrier ──────────
```

#### 4.3.3 源码精读

「先于第一个屏障」的总纲，写在 `prepare` 的开头：

[src/bindings.cc:L87-L88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L87-L88)
```cpp
// Everything here runs BEFORE the call's first barrier, so a rejected argument leaves no rank
// waiting on peers that did not reject it.
```
第一个屏障在 `transfer_on_stream` 的 [src/bindings.cc:L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L203)（`fast_barrier(stream, call.win->flag_ptrs, rank)`），而 `prepare` 在 `run` 里先于它执行：[src/bindings.cc:L235-L236](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L235-L236)。

CUDA 检查的放置理由——为什么在 `CUDAGuard` 之前：

[src/bindings.cc:L28-L33](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L28-L33)
```cpp
// Before any CUDAGuard: constructing one from a CPU tensor aborts inside c10 with a message that
// names neither this library nor the argument. docs/api.md promises this wording.
void require_cuda(const at::Tensor& input)
{
    TORCH_CHECK(input.is_cuda(), "input must be a CUDA tensor, got one on ", input.device());
}
```
不只是拒绝 CPU 张量，还保证了报错**署名**：不检查的话，用户看到的会是 c10 内部一条既不含库名也不含参数名的 abort。

splits 的「都传或都不传」与交叉校验（4.1.3 已精读消息本身），检查入口在：

[src/group.cc:L80-L87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L80-L87)
```cpp
if (seq_splits.has_value() || head_splits.has_value()) {
    // One without the other has no meaning: the plan needs the WHOLE group's geometry, and the
    // missing half cannot be inferred from a shape that is itself already sharded.
    TORCH_CHECK(seq_splits.has_value() && head_splits.has_value(),
                "pass both seq_splits and head_splits, or neither");
```
「缺的那一半无法从形状推断」——形状本身已经是分片后的，你看到 `(2, 16, 40, 128)` 时无从知道 40 是不是全组头数，所以必须由调用者给出完整几何。

mode 检查在 torch 路径与纯主机路径各有一份（语义相同、措辞不同）：

- torch 路径：[src/group.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L72) — `"mode must be 0 or 1, got ", mode`
- 纯主机路径：[src/a2a_plan.cc:L149-L151](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L149-L151)，经由 `fail()`（[src/a2a_plan.cc:L12-L15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L12-L15)）抛 `std::invalid_argument`——这一份服务于不经过 torch 的 `a2a_plan_debug` 路径（u3-l3 展开），从 Python torch 调用进来的一定先命中 `make_dims` 那份，即 `RuntimeError`。

「全空分片」的拒绝发生在窗口分配处，仍在第一个屏障之前：

[src/group.cc:L210-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L210-L214)
```cpp
// Every rank computes the same window size (it is the max over all ranks), so this throws on
// all of them together and cannot leave anyone waiting. A zero-sized symmetric allocation has
// a null data_ptr, which rendezvous refuses with a null handle.
TORCH_CHECK(numel > 0,
            "this call moves no data: every rank's shard is empty. ...");
```
窗口大小取全 rank 最大值，所以「全空」是**每个 rank 同时算出 0**、同时抛——连这个最晚的检查也保持着「集体一致地失败」的性质。

`out` 的四连检查与重叠检查（消息见 4.3.2 流程图；源码在 [src/bindings.cc:L112-L139](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L112-L139) 与 [src/bindings.cc:L158-L172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L158-L172)）。重叠检查用**字节区间**而非指针相等，因为部分重叠可以落在窗口基址之后；`out` **本身就是**窗口（来自 `empty_output` 的零拷贝路径）是唯一合法的重合。

文档全表在 [docs/api.md:L93-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93-L103)，开头一句就是本模块的排序原则：「from validation that runs **before the call's first barrier**」。

#### 4.3.4 代码实践

**实践目标**：做一张「文档拒绝 × 源码位置」对照表——这是本讲综合实践（写 pytest 断言这些消息）的侦察工作。

1. 实践目标：把 docs/api.md Raises 一节的每一个子句映射到 `文件:行号`。
2. 操作步骤：逐条读 [docs/api.md:L95-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L95-L103)，在 `src/bindings.cc`、`src/group.cc`、`src/a2a_plan.cc` 里找到对应的 `TORCH_CHECK`。参考答案（本讲已逐一核对）：

| docs/api.md 的拒绝子句 | 源码位置 | 消息特征片段 |
| --- | --- | --- |
| the group already `destroy()`ed | [src/bindings.cc:L102](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L102) → [src/group.cc:L162-L165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L162-L165) | `has been destroyed` |
| `x` not 4D | [src/group.cc:L53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L53) | `must be 4D` |
| not CUDA | [src/bindings.cc:L30-L33](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L30-L33) | `must be a CUDA tensor` |
| another device | [src/bindings.cc:L38-L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L38-L48) | `one group serves exactly one device` |
| dtype not in the list | [src/group.cc:L59-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L59-L62) | `dtype must be` |
| `d * elem_size` not 16 B-aligned | [src/group.cc:L70-L71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L70-L71) | `16-byte aligned` |
| `mode` not 0 or 1 | [src/group.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L72) | `mode must be 0 or 1` |
| one of the splits without the other | [src/group.cc:L83-L84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L83-L84) | `both seq_splits and head_splits` |
| splits contradicting `x`'s shape | [src/group.cc:L90-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L90-L109) | `but the splits imply` |
| no splits and the scattered axis does not divide | [src/group.cc:L115-L134](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L115-L134) | `must divide world_size` |
| every rank's shard empty | [src/group.cc:L212-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L212-L214) | `moves no data` |
| `out` not contiguous CUDA / other device / dtype / shape | [src/bindings.cc:L114-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L114-L129) | `contiguous CUDA tensor` / `out is on cuda:` / `out has dtype` / `out has shape` |
| `x` / `out` overlapping the window | [src/bindings.cc:L164-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L164-L171) | `overlaps the window` |
| `world_size` outside `[1, 8]` | 构造函数：[src/group.cc:L151-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L151-L153) | `world_size must be in [1, 8]` |

3. 需要观察的现象：每条文档子句都能找到唯一（或明确分工的两处）源码出处，没有一条是「文档写了、代码没查」或反之。
4. 预期结果：这张表就是综合实践中 pytest `match=` 片段的直接来源。

#### 4.3.5 小练习与答案

**练习 1**：`x` 不是 4D 的检查（group.cc:53）和 `x` 在别的设备上的检查（bindings.cc:38-48），谁先执行？如果传入一个「3D 且在错误 GPU 上」的张量，报哪条？

**答案**：`prepare` 的顺序是 `require_cuda` → `require_group_device` → `contiguous` → `plan()`（内部才查 4D）。所以「3D 且错设备」先报设备（`one group serves exactly one device`）；只有设备正确时才轮到 `must be 4D`。想在综合实践里测 4D 报错，输入必须放在正确的设备上——pytest 用例里 3D 张量要用 `device=dev` 构造。

**练习 2**：为什么「全 rank 分片为空」的检查（`moves no data`）放在 `allocate` 而不是 `make_dims`？

**答案**：这个条件等价于 `window_numel == 0`，而 `window_numel` 是 `build_plan` 对全 rank 取最大值算出来的——make_dims 阶段还没有这个数。更关键的是拒绝的**理由**：0 大小的对称分配 `data_ptr` 为空，rendezvous 会拒绝空句柄，那是 torch 内部的报错，不如在分配入口给出署名的、解释性的消息。且窗口大小全 rank 一致地算出，这个失败天然是集体一致的。

**练习 3**：`seq_splits=[]`（空列表）和不传 `seq_splits` 是同一件事吗？

**答案**：不是。plan 缓存键刻意保留 optional 语义（[src/group.cc:L177-L180](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L177-L180)）。「不传」走偶均分推导；「空列表」进入显式 splits 分支，随后被 `A2ADims::validate` 以 `seq_splits has 0 entries, expected ws` 拒绝（[src/a2a_plan.cc:L124-L127](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L124-L127)）。把两者折叠成同一个缓存键，会让一次偶均分调用为之后的空列表调用「预热」出错误的 plan——validation.py:L269-L278 回归的正是这个坑。

### 4.4 集体调用契约：五条规则与「挂起而非报错」

#### 4.4.1 概念说明

4.3 的 Raises 清单保护的是「单 rank 可判定」的非法性。但一次集合调用的正确性还依赖**所有 rank 互相配合**：大家发同样形状的序列、同样 dtype 的序列、在同样的位置做集合操作。这类违规，任何一个 rank **单看自己的参数都发现不了**——rank 0 无法知道 rank 1 传了什么形状。既然查不出来，库的选择是不查：把要求写成契约，违反的后果是挂起。

[docs/api.md:L8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L8) 一句话定调：**「Violating these hangs the group. Nothing raises and nothing times out.」**（违反这些会把组挂死。没有任何报错，也没有超时。）

为什么是挂起？以「形状序列不一致」为例：rank 0 先发 `(2,16,40,128)`，rank 1 先发 `(2,16,48,128)`。新形状触发窗口分配，分配需要 rendezvous——每个 rank 都要带着同样大小的窗口到场。rank 0 在等 rank 1 的句柄，rank 1 在等 rank 0 的，谁也等不到，GPU 上的自旋屏障永远转不出去，进程不退出、不抛错、不超时。这不是 bug，是集合操作的本质：**约定无法被单方面验证时，违反约定的代价由死锁承担**。

#### 4.4.2 核心流程

契约五条（[docs/api.md:L10-L20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L10-L20)），按「谁依赖它」理解：

```text
① 同形状序列   —— 首次遇新形状会触发集合性窗口分配（rendezvous）
② 集合性操作   —— 构造、empty_output()、destroy() 全组到齐才推进
③ 同 dtype 序列 —— 窗口按 (role, dtype) 缓存：dtype 序列不同 = 分配序列不同
④ backward 顺序 —— 反向节点里也要发集合通信，autograd 引擎到达顺序必须全组一致
⑤ 一缓冲一调用 —— 窗口是单缓冲：一次调用会覆盖它写过的缓冲
```

违反的后果统一：

```text
某 rank 单方面偏离契约
  → 它与同伴在某个集合点（rendezvous / 屏障 / all_gather）上失配
  → 对方永远等不到它，或它永远等不到对方
  → 进程挂起：无异常、无超时、无堆栈
```

判别准则（和 4.3 的分界线）：

- 违规**只涉及本 rank 的参数与本地状态**（形状不是 4D、dtype 不在白名单、组已 destroy……）→ Raises 清单，屏障前抛 `RuntimeError`。
- 违规**只有对比其他 rank 才能确认**（形状序列、dtype 序列、调用次数、释放时机……）→ 契约，挂起。

#### 4.4.3 源码精读

契约原文：[docs/api.md:L6-L20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L6-L20)。

①③ 的根源——窗口按 `(role, dtype)` 缓存、按需增长，增长即集合分配：

[src/group.cc:L255-L267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L255-L267)
```cpp
const Window& UlyssesGroup::window(WindowRole role, at::ScalarType dtype, int64_t numel)
{
    check_alive();
    const auto key = std::make_pair(static_cast<int64_t>(role), dtype);
    auto       it  = windows_.find(key);
    if (it != windows_.end() && it->second.numel >= numel) {
        return it->second;
    }
    ...
    return windows_.emplace(key, allocate(numel, dtype)).first->second;
}
```
窗口装不下时调用 `allocate`，而 `allocate` 内部是 `empty_strided_p2p` + `rendezvous`（[src/group.cc:L218-L227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L218-L227)）——rendezvous 要求每个 rank 带同样大小的分配到场。所以「同形状序列」「同 dtype 序列」不是风格建议，是 rendezvous 的物理前提（u4-l1、u4-l2 展开）。

⑤ 的根源——窗口是单缓冲，写者要先等读者：

[src/bindings.cc:L198-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L198-L203)
```cpp
// WRITERS WAIT FOR READERS, before writing anything. The window is single-buffered, so this
// call is about to overwrite what the previous one produced, which a peer may still be
// reading; the closing barrier below proves everyone's WRITES landed and nothing about their
// READS. ...
fast_barrier(stream, call.win->flag_ptrs, rank);
```
一次调用会覆盖上一次写进窗口的数据。同一个 `empty_output` 缓冲被两个并发的调用同时用作 `out=`，两边都在写同一块内存——屏障只保证「写都落地」，不保证「没人还在读」。所以要两条缓冲（或用 `lend=True` 让库自己轮转，u4-l4 展开）。

④ 的根源——backward 里真的有一次集合通信：

[src/bindings.cc:L549-L559](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L549-L559)
```cpp
// The copying form, never `out` from empty_output(): that is a collective allocation, and
// putting one inside backward would hand the rendezvous position to the autograd engine's
// node order rather than to the caller's own program. ...
return {at::Tensor(),
        A2AFunction::apply(group, grads[0].contiguous(), 1 - mode, seq, head),
        ...};
```
反向传播执行到 `A2AFunction::backward` 时会再发一次 `all_to_all_4d`（`1 - mode`，同一组 splits——u6-l3 展开）。如果各 rank 的 autograd 引擎以不同顺序到达这个节点，等于发出不同顺序的集合调用——回到契约①③的挂起。注释还说明了另一层：这也是 backward **永不走 `out=`** 的原因（把集合分配塞进 backward，rendezvous 的位置就交给了引擎的节点顺序）。

② 集合性操作：`empty_output` 走 `make_output` → `allocate`（rendezvous），`destroy` 在 [python/fast_ulysses/group.py:L267-L277](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L267-L277) 先排空 comm 流再 `dist.barrier`——全组到齐才能释放窗口。

最后看 Python 门面上一条「主动报错」而非挂起的边界案例——`out=` 遇到需要梯度的输入：

[python/fast_ulysses/group.py:L138-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143)
```python
if torch.is_grad_enabled() and x.requires_grad:
    raise RuntimeError(
        "all_to_all_4d(out=...) does not support autograd: the out-variant is a mutating "
        "op with no backward, so the gradient would be dropped without an error. Drop "
        "out=, or run under torch.no_grad()."
    )
```
这条违规本可以静默（梯度悄悄丢失），库选择报错——「拒绝而非静默出错」是贯穿整个 API 的另一条价值观：**可检测的静默错误都升格为报错；不可检测的跨 rank 失配才留给契约**。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——为三个违规场景指出违反的契约条目，并解释为什么不会有报错。

1. 实践目标：把「症状 → 契约条目 → 挂起机制」连起来。
2. 操作步骤：阅读 [docs/api.md:L6-L20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L6-L20) 与 4.4.3 的源码，回答下面三个场景（先自己写，再对答案）：
   - 场景 A：`ws=2`，rank 0 循环里发 `(2, 16, 40, 128)`，rank 1 发 `(2, 16, 48, 128)`。
   - 场景 B：训练循环里，两个 rank 的 loss 里把两个用到 `all_to_all_4d` 的分支按不同顺序写了（rank 0 先分支甲后分支乙，rank 1 相反），前向无异常。
   - 场景 C：`buf = group.empty_output(x, mode=0)`，同一个循环迭代里把它同时作为两次不同 `all_to_all_4d(..., out=buf)` 的输出。
3. 需要观察的现象（纸面推演；**不要**真的在多卡上跑场景 A/B——那会挂死你的 GPU 进程，需要手工 kill）。
4. 预期结果：见答案。有 GPU 想安全体验「挂起」时，只可用 `--nproc_per_node=1` 验证「合法调用不挂」，跨 rank 失配在单卡上无法复现（这正是它危险的原因）。

#### 4.4.5 小练习与答案

**练习 1（对应 4.4.4 场景 A）**：违反哪条？为什么挂的是 rendezvous？

**答案**：违反①（同形状序列）。两个形状都没问题、dtype 也合法——每个 rank 单看都「合法」，所以 4.3 的检查全放行。第一个新形状触发窗口分配：rank 0 以 `window_numel(40 头方案)` 分配，rank 1 以 `window_numel(48 头方案)` 分配，rendezvous 上互相等对方的句柄，永远等不到。无异常、无超时。

**练习 2（对应场景 B）**：违反哪条？为什么前向没事、反向才挂？

**答案**：违反④（backward 顺序）。前向两次调用的**集合**在两个 rank 上次序一致（各自程序里都发了甲、乙两次，顺序无关紧要——只要形状序列与 dtype 序列相同）。挂起发生在反向：`A2AFunction::backward` 里各有一次集合通信（`1-mode` 调用），rank 0 的引擎先到甲的节点、rank 1 先到乙的节点，反向集合的到达顺序失配，屏障上互等。docs/api.md:L15-L17 特别提醒「这是全新的挂法」，且计算图持有 group 的引用——图活得比 `destroy()` 长还会再抛错。

**练习 3（对应场景 C）**：违反哪条？为什么可能「看起来还能跑」而更危险？

**答案**：违反⑤（一缓冲一调用）。`buf` 是同一块对称内存，两次调用都在让 peers 直写它。屏障保证的是「本次调用的写全部落地」，不保证「上一次的读者已经读完」；两个写者交错覆盖，结果是**损坏的数据而非崩溃**——最坏的一类失败，因为它可能安静地污染训练。修复：两个 `empty_output` 缓冲各配一次调用，或改用 `all_to_all_4d_async(lend=True)` 让库自己轮转四个窗口（u4-l4）。

## 5. 综合实践

**任务**：写一组可运行在 `world_size=1` 的 pytest 用例，对六类非法参数逐一断言抛出 `RuntimeError`，并把断言的消息片段与 docs/api.md 的 Raises 一节逐条对照——亲手验证「文档承诺的每条拒绝都真的拒绝，且理由正确」。

这正是本讲的验收：形状表（4.1）、dtype 与对齐（4.2）、Raises 映射（4.3）全部浓缩进可执行的断言里。项目自己的 [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py) 在多卡上做同样的全覆盖（其 docstring 明确说「每个拒绝在每个 rank 上都检查，然后 ranks 在 barrier 会合」）；我们的 `ws=1` 版本是它的子集——单 rank 下每个 rank 自然「全体一致」。

**操作步骤**：

1. 把下面的测试保存为 `fast-ulysses-tutorial/test_u2l2_contract.py`（示例代码；本讲义只内嵌，不替你建仓）：

```python
# 示例代码：u2-l2 综合实践 —— all_to_all_4d 的契约用例（world_size=1 可跑）
from __future__ import annotations

import os

import pytest

torch = pytest.importorskip("torch")
dist = pytest.importorskip("torch.distributed")
pytest.importorskip("fast_ulysses", reason="fast-ulysses._C extension not built")
from fast_ulysses import UlyssesGroup  # noqa: E402


@pytest.fixture(scope="session")
def dev():
    dist.init_process_group("nccl")
    torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
    yield torch.device("cuda", torch.cuda.current_device())
    dist.destroy_process_group()


@pytest.fixture(scope="session")
def group(dev):
    g = UlyssesGroup(device=dev, require_nvlink=False)  # 单卡：NVLink 检查无话可说，关掉
    yield g
    g.destroy()


def bf16(shape, device):
    return torch.randn(*shape, dtype=torch.bfloat16, device=device)


GOOD = (2, 16, 4, 128)  # ws=1：mode 0 下 4 % 1 == 0，天然合法


def test_3d_input(group, dev):
    # 对照 api.md: "x not 4D"；检查在 make_dims，故张量须在正确设备上（见 4.3.5 练习 1）
    with pytest.raises(RuntimeError, match="must be 4D"):
        group.all_to_all_4d(bf16((2, 16, 128), dev))


def test_cpu_input(group, dev):
    # 对照 api.md: "not CUDA"；require_cuda 先于一切 CUDA 侧构造
    x = torch.randn(*GOOD, dtype=torch.bfloat16, device="cpu")
    with pytest.raises(RuntimeError, match="must be a CUDA tensor"):
        group.all_to_all_4d(x)


def test_bad_dtype(group, dev):
    # 对照 api.md: "dtype not in the dtype list"；只匹配前缀，流式打印的枚举名不写死
    with pytest.raises(RuntimeError, match="dtype must be"):
        group.all_to_all_4d(bf16(GOOD, dev).to(torch.int32))


def test_unaligned_head_dim(group, dev):
    # 对照 api.md: "d * elem_size not 16 B-aligned"；d=4 的 bf16 是 8 B
    with pytest.raises(RuntimeError, match="16-byte aligned"):
        group.all_to_all_4d(bf16((2, 16, 4, 4), dev))


def test_mode_2(group, dev):
    # 对照 api.md: "mode not 0 or 1"；torch 路径命中的是 make_dims 那份检查
    with pytest.raises(RuntimeError, match="mode must be 0 or 1"):
        group.all_to_all_4d(bf16(GOOD, dev), mode=2)


def test_seq_splits_without_head_splits(group, dev):
    # 对照 api.md: "one of the splits without the other"
    with pytest.raises(RuntimeError, match="both seq_splits and head_splits"):
        group.all_to_all_4d(bf16(GOOD, dev), seq_splits=[16])


def test_splits_contradict_shape(group, dev):
    # 对照 api.md: "splits contradicting x's shape"；ws=1 期望输入 (2, 99, 4, 128)
    with pytest.raises(RuntimeError, match="but the splits imply"):
        group.all_to_all_4d(bf16(GOOD, dev), seq_splits=[99], head_splits=[4])


def test_rejection_leaves_nothing_broken(group, dev):
    # validation.py 的 accepts 思路：一串拒绝之后，合法调用必须仍然工作
    y = group.all_to_all_4d(bf16(GOOD, dev), mode=0)
    assert y.shape == (2, 16, 4, 128)  # ws=1 下 mode 0 是形状恒等（4.1.2）
```

2. 运行（需要已构建扩展与一块 GPU；参照 u1-l3 的安装讲义）：

```bash
torchrun --nproc_per_node=1 -m pytest fast-ulysses-tutorial/test_u2l2_contract.py -v
```

3. **对照**：打开 [docs/api.md:L93-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L93-L103)，逐条核对每个 `match=` 片段都出现在 Raises 一节的对应子句里，并核对我们能**测不到**的两条：
   - "no splits and the scattered axis does not divide"——`ws=1` 下一切整除，不可达（4.1.5 练习 3）；
   - 跨设备的两条（`one group serves exactly one device` / `out is on cuda:`）——单卡机器上造不出第二块卡（多卡机器可仿照 [test/distributed/validation.py:L286-L310](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L286-L310) 补上）。
4. **需要观察的现象**：8 个用例全部 PASSED；每个拒绝的完整消息里都含断言片段（可临时把 `pytest.raises` 换成 `try/except` 打印完整消息对照 4.3.4 的表格）。
5. **预期结果**：全绿，且 `test_rejection_leaves_nothing_broken` 证明一串拒绝没有给组留下任何坏状态——这正是「校验先于第一个屏障」承诺的可见收益。
6. 无 GPU 环境时：本实践的纯主机部分（4.1.4 形状推导、4.2.4 对齐表、4.3.4 映射表）全部可完成；pytest 部分标注**待本地验证**。

## 6. 本讲小结

- **形状契约**：mode 0 把 `(b, s_local, n_global, d)` 变成 `(b, s_global, n_local, d)`，mode 1 是严格逆变换；不传 splits 时由被散射轴的整除性反推偶均分。输出形状可纯手推，`ws=1` 时退化为恒等。
- **dtype 与对齐**：7 种 dtype 的白名单是产品决策（传输只搬字节）；对齐约束是 `d * elem_size` 为 16 的倍数——一行检查覆盖 plan 里全部字节量，并随元素变窄而收紧（`d%8`/`d%4`/`d%16`）。
- **Raises 的排序纪律**：所有校验（销毁、设备、4D、dtype、对齐、mode、splits、out、重叠、空分片）都发生在第一个屏障之前——被拒的参数不会留下任何 rank 干等同伴；且「查形状的」进 plan 缓存键、「查张量的」留在每次调用。
- **契约五条与挂起**：形状序列、dtype 序列、集合性操作、backward 顺序、一缓冲一调用——跨 rank 才能判定的违规不设检查，违反即挂起（无异常、无超时）；能单方面判定的静默错误（如 `out=` 丢梯度）则升格为显式报错。
- **分界线**：`RuntimeError` 清单保护「本 rank 可判定」的世界；契约保护「全组一致」的世界。写分布式代码时的自查顺序永远是：先对照 Raises 排本地非法，再对照五条排跨 rank 失配。

## 7. 下一步学习建议

下一讲 **u2-l3（不均匀分片：seq_splits 与 head_splits）** 深入本讲只是「校验对象」的 splits：`s=75827` 这类真实序列长度如何切、`make_dims` 的交叉校验如何防住错误分片、以及为什么不均匀分片是让调用方丢掉序列 padding 的关键（性能动机）。

更远的路标：

- 想知道形状表背后的**数据怎么搬**：u3-l1/u3-l2 精读 `A2ADims / CopyOp / A2APlan` 与 `build_plan`——本讲 4.1.3 里那句「所有字节量都是 `d*elem` 的倍数」将在那里变成具体的 `src_pitch / dst_pitch`。
- 想知道契约①③的 rendezvous 到底怎么 collective：u4-l1（对称内存窗口分配）与 u4-l2（按 `(role, dtype)` 的窗口生命周期）。
- 想看契约④的完整推导（backward 为何是 `1-mode` 且 splits 不交换）：u6-l3 精读 `A2AFunction`。
- 想看这些报错在多卡上如何被系统化验证：u9-l2 精读 [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py)——本讲综合实践是它的单卡缩影。
