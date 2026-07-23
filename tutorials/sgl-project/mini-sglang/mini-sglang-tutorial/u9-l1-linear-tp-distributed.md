# 张量并行 Linear 与分布式通信

## 1. 本讲目标

在 u8 我们已经搞清楚了一张卡上模型层是如何用 `BaseOP` 组合出来的。但当模型大到一张卡放不下、或者算力不够时，Mini-SGLang 会用**张量并行（Tensor Parallelism, TP）**把它拆到多张卡上一起算。本讲就聚焦「拆」与「合」这两件事在代码里是怎么落地的。

学完本讲你应该能够：

- 说清 **column-parallel（列并行，切输出）** 与 **row-parallel（行并行，切输入）** 在数学上切的是权重矩阵的哪一维、对应的集合通信（`all_reduce` / `all_gather`）发生在前向的哪一步。
- 识别 Mini-SGLang 的 **TP Linear 族**（`LinearReplicated` / `LinearColParallelMerged` / `LinearQKVMerged` / `LinearOProj` / `LinearRowParallel`）各自属于哪种切分、为何有的需要通信有的不需要。
- 解释 `LinearQKVMerged` 在 **GQA（kv_heads < tp_size）** 场景下为什么要把 KV 头「复制」而不是切分。
- 理解 `DistributedCommunicator` 用「插件栈」抽象通信后端，以及 `enable_pynccl_distributed` 如何在运行时把默认的 `torch.distributed`（NCCL）替换成自研的 **PyNCCL** 通道。

## 2. 前置知识

在进入源码前，先用一张纸一支笔的方式把「矩阵乘法怎么拆」想清楚。假设有一次线性变换：

\[ Y = X W \]

其中 \(X\) 是输入（形状 `(num_tokens, hidden)`），\(W\) 是权重。PyTorch 的 `F.linear` 把权重存成 `(out_features, in_features)`，即**行对应输出、列对应输入**。这一点非常关键，决定了我们下面说的「切行」其实是切输出维度。

张量并行的核心直觉是：**只要把矩阵乘法拆成「每个 rank 算一部分、最后再合」的形式，就能让多张卡并行算同一个矩阵乘法。** 拆法有两种，它们决定了通信发生在「算之前」还是「算之后」：

1. **列并行（column-parallel，切输出 / 切 dim0）**：把 \(W\) 沿**输出维**（行）切成 \(W_1, \dots, W_n\)，每个 rank 持有完整输入 \(X\) 和自己那份 \(W_i\)，算出 \(Y_i = X W_i\)（\(Y_i\) 是 \(Y\) 的一个输出块）。每个 rank 的 \(Y_i\) 是**最终输出的一部分**，彼此不重叠，因此**算完不需要任何通信**。

2. **行并行（row-parallel，切输入 / 切 dim1）**：把 \(W\) 沿**输入维**（列）切成 \(W_1, \dots, W_n\)，同时把输入也按列切成 \(X_1, \dots, X_n\)，每个 rank 只算 \(X_i W_i\)（这是一个**部分和**）。最终的 \(Y\) 是所有部分和相加：

\[ Y = \sum_{i=1}^{n} X_i W_i \]

所以行并行**算完必须做一次 `all_reduce`（求和）**，否则每个 rank 手里只有部分结果。

把这两种切法「串联」起来就能省通信：一个块如果前半段是列并行、后半段是行并行，那么**中间的激活值天然就是按卡切分的、不需要通信，整段只需要在行并行那一步做一次 `all_reduce`**。这正是 Transformer 里 MLP 和 Attention 的经典切法，也是本讲实践任务要手算的东西。

> 补充术语：
> - **rank / size**：多卡里每张卡的编号叫 rank（从 0 开始），卡的总数叫 size（即 `--tensor-parallel-size` / `--tp-size`）。
> - **集合通信（collective communication）**：多张卡之间同步数据的操作。本讲涉及 `all_reduce`（所有卡把各自张量求和，结果每张卡都有一份）和 `all_gather`（每张卡把自己的片段拼到一起，结果每张卡都有完整的一份）。
> - **GQA（Grouped-Query Attention）**：query 头多、key/value 头少的注意力结构。当 KV 头数少于卡数时，KV 头没法均匀切，需要复制。

## 3. 本讲源码地图

本讲围绕两组源码文件展开——一组定义「怎么切」，一组定义「怎么通信」：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/layers/linear.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py) | **TP Linear 族**：定义了 5 种带张量并行语义的线性层，决定权重的 local 形状以及前向时是否需要通信。 |
| [python/minisgl/distributed/info.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py) | **TP 身份登记**：用进程级单例 `DistributedInfo(rank, size)` 记录「我是第几张卡、一共几张卡」。 |
| [python/minisgl/distributed/impl.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py) | **通信后端抽象**：`DistributedCommunicator` 用插件栈把 `all_reduce` / `all_gather` 在 `torch.distributed` 与 `PyNCCL` 之间切换。 |
| [python/minisgl/distributed/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/__init__.py) | 把上述接口打包导出。 |
| [python/minisgl/layers/embedding.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py) | 两个使用通信算子的例子：`VocabParallelEmbedding`（用 `all_reduce`）与 `ParallelLMHead`（用 `all_gather`）。 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | `_init_communication` 决定启动 gloo / NCCL / PyNCCL 中的哪种组合。 |
| [python/minisgl/kernel/pynccl.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py) | PyNCCL 通信器的 JIT 构建与初始化（底层是 NCCL）。 |

还有两个被反复调用的辅助：

- [python/minisgl/utils/misc.py:20-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L20-L26) 的 `div_even`：负责「把一个维度按卡数均分」，是所有切分计算的算盘珠子。
- [python/minisgl/models/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py) 的 `GatedMLP` / `RopeAttn`：把 TP Linear 族拼成真实模型层的胶水代码。

## 4. 核心概念与源码讲解

### 4.1 TP 身份与切分计算

#### 4.1.1 概念说明

任何一个想做张量并行的层，在构造时都需要回答两个问题：

1. 「我是第几张卡、一共几张卡？」——决定该切哪一份、该不该复制。
2. 「把这个维度按卡数均分会得到多少？」——决定 local 权重矩阵的形状。

这两个问题分别由 `distributed/info.py` 和 `utils/misc.py` 的 `div_even` 回答。它们是整个 TP 体系的基石：**所有 TP Linear 在 `__init__` 里第一件事就是读 TP 身份、用 `div_even` 算 local 形状。**

#### 4.1.2 核心流程

TP 身份是一个**进程级单例**：

- Engine 启动时（每个 GPU 进程内）调一次 `set_tp_info(rank, size)` 登记。
- 此后任何层的构造函数用 `get_tp_info()` 取出 `(rank, size)`。
- `set_tp_info` 只能调一次，重复调会抛 `RuntimeError`，避免某张卡中途换身份导致切分错乱。

切分计算靠 `div_even(a, b)`：

```text
普通模式 (allow_replicate=False):
    要求 a % b == 0，返回 a // b
    （切分：每个 rank 拿到 a/b 个）

复制模式 (allow_replicate=True):
    若 b > a 且 b % a == 0，返回 1
    （复制：每个 rank 拿到 1 个，但总数 a < b，必然有 rank 拿到重复的）
```

#### 4.1.3 源码精读

`DistributedInfo` 是一个不可变数据类，断言 `0 <= rank < size`，并提供 `is_primary()` 判断「我是不是 rank 0」（rank 0 在多 rank 广播里是「队长」，见 u4-l2）：

```python
@dataclass(frozen=True)
class DistributedInfo:
    rank: int
    size: int
    def __post_init__(self):
        assert 0 <= self.rank < self.size
    def is_primary(self) -> bool:
        return self.rank == 0
```

见 [python/minisgl/distributed/info.py:6-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py#L6-L15)。`_TP_INFO` 是模块级全局变量，`set_tp_info` 写、`get_tp_info` 读，`try_get_tp_info` 在可能还没初始化时返回 `None`：[python/minisgl/distributed/info.py:18-35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py#L18-L35)。

`div_even` 的「复制模式」是 GQA 的命脉：

```python
def div_even(a: int, b: int, allow_replicate: bool = False) -> int:
    if allow_replicate and b > a:
        assert b % a == 0, f"{b = } must be divisible by {a} for KV head replication"
        return 1
    assert a % b == 0, f"{a = } must be divisible by {b = }"
    return a // b
```

见 [python/minisgl/utils/misc.py:20-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L20-L26)。注意参数顺序是 `div_even(被除数, 除数)`，例如 `div_even(num_kv_heads, tp_size, allow_replicate=True)` 表示「把 KV 头数按卡数分，允许复制」。

`set_tp_info` 的真正调用点在 Engine 初始化的第二行：[python/minisgl/engine/engine.py:32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L32)，在创建模型之前就登记好，确保后续每一层的 `__init__` 都能取到正确的 TP 身份。

#### 4.1.4 代码实践

**实践目标**：验证 `div_even` 在 GQA 复制场景下的行为，并理解它为何不会报错。

**操作步骤**：

1. 在安装好 `minisgl` 的环境里打开 Python（CPU 即可，不需要 GPU）。
2. 直接导入并调用：

```python
from minisgl.utils import div_even

# 普通 q 头：8 个 q 头分给 2 张卡
print(div_even(8, 2))                       # 4，每卡 4 个 q 头

# GQA：8 个 q 头、2 个 kv 头、2 张卡
print(div_even(8, 2), div_even(2, 2, allow_replicate=True))   # 4 1

# 极端 GQA：8 个 q 头、4 个 kv 头、8 张卡
print(div_even(8, 8), div_even(4, 8, allow_replicate=True))   # 1 1
# kv 头 4 < 卡数 8 且 8 % 4 == 0，每卡 1 个，但只有 4 个 kv 头 → 复制
```

3. 再故意制造一个会报错的情况，观察断言信息：

```python
div_even(3, 2)              # 3 不能被 2 整除，普通模式抛 AssertionError
div_even(3, 8, allow_replicate=True)  # 8 % 3 != 0，复制模式抛 AssertionError
```

**需要观察的现象**：复制模式只有当「卡数是头数的整数倍」时才成立；否则既切不开也复制不了，只能报错。

**预期结果**：上面四行依次打印 `4`、`4 1`、`1 1`，最后两行抛出 `AssertionError`。若本地无 `minisgl` 安装，可对照源码逻辑口算，结果一致即「待本地验证」通过。

#### 4.1.5 小练习与答案

**练习 1**：一个模型有 32 个 query 头、8 个 kv 头，在 `tp_size=4` 下，每个 rank 持有几个 q 头、几个 kv 头？

答案：q 头 `div_even(32, 4)=8`；kv 头 `div_even(8, 4, allow_replicate=True)=2`。每卡 8 个 q 头、2 个 kv 头，无需复制。

**练习 2**：为什么 `set_tp_info` 设计成「只能调一次」？

答案：TP 身份决定了所有层的权重切分形状。若允许中途修改，已经建好的层（权重已按旧 rank 切好）会与新身份不一致，导致 `all_reduce` 维度对不上。一次性写入等于给整个进程锁死了切分契约。

### 4.2 TP Linear 族

#### 4.2.1 概念说明

Mini-SGLang 没有用 PyTorch 的 `nn.Linear`，而是自造了一族「天生支持张量并行」的 Linear（继承自 u8 讲过的 `BaseOP`）。它们都建立在一个公共基类 `_LinearTPImpl` 上，区别只在于**构造时如何计算 local 权重形状**以及**前向时是否调用集合通信**。这一族共 5 个成员：

| 类名 | 切分方式 | 前向是否通信 | 典型用途 |
| --- | --- | --- | --- |
| `LinearReplicated` | 不切（每卡完整） | 否 | MoE 的 router（`gate`） |
| `LinearColParallelMerged` | 列并行（可合并多个输出） | 否 | MLP 的 `gate_up_proj` |
| `LinearQKVMerged` | 列并行（q/k/v 合并 + GQA 复制） | 否 | Attention 的 `qkv_proj` |
| `LinearOProj` | 行并行 | `all_reduce` | Attention 的 `o_proj` |
| `LinearRowParallel` | 行并行 | `all_reduce` | MLP 的 `down_proj` |

记忆口诀：**「列并行不通信、行并行要 all_reduce」**。原因就是 §2 讲的——列并行每卡拿到的是最终输出的一块（天然分卡），行并行每卡拿到的只是部分和（必须求和）。

#### 4.2.2 核心流程

所有 TP Linear 的构造都遵循同一套节奏：

```text
1. get_tp_info()          # 读 (rank, size)
2. div_even(...)          # 算 local_input_size / local_output_size
3. super().__init__(...)  # 把 full / local 两套尺寸交给 _LinearTPImpl
                          # _LinearTPImpl 用 local 尺寸开权重张量
4.（仅行并行）持有一个 DistributedCommunicator，前向时调用 all_reduce
```

注意 `_LinearTPImpl` 同时保存了 `full_*`（完整尺寸，做参考）和 `local_*`（本卡实际尺寸，决定权重形状）：

```python
self.weight = torch.empty(local_osize, local_isize)
```

形状是 `(local_output_size, local_input_size)`，与 PyTorch `F.linear` 的权重约定一致。前向就是一次普通的矩阵乘加：

```python
def forward(self, x):
    return F.linear(x, self.weight, self.bias)
```

#### 4.2.3 源码精读

公共基类 `_LinearTPImpl` 见 [python/minisgl/layers/linear.py:13-32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L13-L32)。它把 full/local 两套尺寸都记下来，但只开 local 大小的权重。

`LinearReplicated` 最简单——full 和 local 完全相等，不切分：[python/minisgl/layers/linear.py:35-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L35-L53)。

`LinearColParallelMerged` 是「合并多个列并行输出」的版本，典型场景是 MLP 把 gate 和 up 两个同形矩阵合成一个大矩阵：

```python
class LinearColParallelMerged(_LinearTPImpl):
    def __init__(self, input_size, output_sizes, has_bias):
        tp_info = get_tp_info()
        tp_output_sizes = [div_even(size, tp_info.size) for size in output_sizes]
        output_size = sum(output_sizes)
        tp_output_size = sum(tp_output_sizes)
        super().__init__(input_size, output_size, input_size, tp_output_size, has_bias)
```

见 [python/minisgl/layers/linear.py:56-68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L56-L68)。注意 `input_size` 不切（列并行每卡持有完整输入），只把每个输出按卡数均分再求和得到 `tp_output_size`。

`LinearQKVMerged` 是最复杂的一个，因为它要同时处理 q/k/v 三段、还要处理 GQA 复制：

```python
class LinearQKVMerged(_LinearTPImpl):
    def __init__(self, hidden_size, head_dim, num_qo_heads, num_kv_heads, has_bias):
        tp_info = get_tp_info()
        local_num_qo = div_even(num_qo_heads, tp_info.size)
        local_num_kv = div_even(num_kv_heads, tp_info.size, allow_replicate=True)
        full_osize  = (num_qo_heads + 2 * num_kv_heads) * head_dim      # q + k + v
        local_osize = (local_num_qo + 2 * local_num_kv) * head_dim
        super().__init__(hidden_size, full_osize, hidden_size, local_osize, has_bias)
```

见 [python/minisgl/layers/linear.py:71-88](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L71-L88)。三件事：
- q 头用普通 `div_even`（query 头通常多于卡数，能切）。
- kv 头用 `allow_replicate=True`（GQA 下 kv 头少，不够切就复制）。
- `2 * num_kv_heads` 是因为 k 和 v 各占一份。`local_osize` 里 `2 * local_num_kv` 同理。

剩下的两个是行并行，它们在前向里调用 `all_reduce`：

```python
class LinearOProj(_LinearTPImpl):
    def __init__(self, input_size, output_size, has_bias):
        tp_info = get_tp_info()
        local_isize = div_even(input_size, tp_info.size)   # 切输入维
        local_osize = output_size                           # 输出不切
        self._comm = DistributedCommunicator()              # 持有通信器
        self._tp_size = tp_info.size
        ...
    def forward(self, x):
        y = F.linear(x, self.weight, self.bias)
        if self._tp_size > 1:            # 单卡时跳过通信
            y = self._comm.all_reduce(y)
        return y
```

见 [python/minisgl/layers/linear.py:91-106](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L91-L106)。`LinearRowParallel` 的代码几乎一模一样（[python/minisgl/layers/linear.py:109-127](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L109-L127)），区别只是语义命名：`OProj` 专门给注意力输出投影用，`RowParallel` 给 MLP 的 `down_proj` 用。

两个行并行层都做了同一个优化：`if self._tp_size > 1` 时才通信。这样单卡（`tp_size==1`）部署时完全不碰分布式代码，零开销。

这族 Linear 在模型层里的装配方式，看 `RopeAttn`（[python/minisgl/models/utils.py:79-123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L123)）和 `GatedMLP`（[python/minisgl/models/utils.py:25-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L25-L50)）：一个 attention 块是 `qkv_proj`（列并行）→ attention → `o_proj`（行并行 all_reduce）；一个 MLP 块是 `gate_up_proj`（列并行）→ 激活 → `down_proj`（行并行 all_reduce）。两者都是「先列后行」的经典组合。

#### 4.2.4 代码实践

**实践目标**：确认 `LinearQKVMerged` 在 GQA 复制场景下算出的 local 输出维度，与「q 切分 + kv 复制」的预期一致。

**操作步骤**：

1. 读 [python/minisgl/layers/linear.py:71-88](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L71-L88)。
2. 取一个真实模型的数字，例如 Qwen3-0.6B：`num_qo_heads=16`，`num_kv_heads=8`，`head_dim=128`，`hidden_size=1024`。
3. 假设 `tp_size=2`，手算：`local_num_qo = 16/2 = 8`，`local_num_kv = 8/2 = 4`（不复制），`local_osize = (8 + 2*4)*128 = 2048`。
4. 再取一个需要复制的例子：`num_qo_heads=16`，`num_kv_heads=4`，`tp_size=8`。手算：`local_num_qo = 16/8 = 2`，`local_num_kv = div_even(4, 8, replicate) = 1`（4 个 kv 头分给 8 张卡，复制），`local_osize = (2 + 2*1)*128 = 512`。

**需要观察的现象**：复制模式下，每个 rank 的 `local_num_kv=1`，但 8 张卡共有 8 份、其中只有 4 种不同的 kv 头，有 2 张卡持有完全相同的 kv 权重。

**预期结果**：上面两例的 `local_osize` 分别是 2048 和 512。可在本地用 `div_even` 直接验证中间量。

#### 4.2.5 小练习与答案

**练习 1**：`LinearColParallelMerged` 的 `output_sizes=[3072, 3072]`、`tp_size=2`，求 `tp_output_size`。

答案：`tp_output_sizes=[1536, 1536]`，`tp_output_size = 1536+1536 = 3072`。即每卡输出维是 3072（合并后的 gate_up 一半）。

**练习 2**：为什么 `LinearOProj` 和 `LinearRowParallel` 代码几乎相同，却要分成两个类？

答案：纯粹是语义可读性。`LinearOProj` 告诉读者「这是注意力输出投影」，`LinearRowParallel` 告诉读者「这是 MLP 的 down 投影」。两者数学行为一致（行并行 + all_reduce），分开命名让模型装配代码（`RopeAttn` / `GatedMLP`）更易读。

### 4.3 通信算子 all_reduce / all_gather 在哪里被调用

#### 4.3.1 概念说明

上节我们看到行并行 Linear 在前向里调 `all_reduce`。本节把这个通信算子放到整个模型的语境里看：**Mini-SGLang 只有两种集合通信被实际用到——`all_reduce`（求和）与 `all_gather`（拼接）**，而且各有固定的出现位置。

- `all_reduce`：出现在「每个 rank 持有部分和、需要凑成完整结果」的地方。典型是行并行的 `o_proj` / `down_proj`，以及词表并行的 embedding 查表。
- `all_gather`：出现在「每个 rank 持有结果的一段、需要拼成完整序列」的地方。典型是 `ParallelLMHead` 的 logits 拼接（每卡只算了部分词表的 logits，要拼成全词表）。

理解这一点能帮你快速定位多卡推理时通信开销在哪里——基本上每个 decoder layer 有 2 次 `all_reduce`（attention 一次、mlp 一次），模型末端有 1 次 `all_gather`（lm_head）。

#### 4.3.2 核心流程

词表并行 embedding（`VocabParallelEmbedding`）用了一个很巧的 `all_reduce` 技巧：

```text
1. 每卡只持有 [start, start+vocab_local) 这段词表的 embedding 权重
2. 输入 token id 落在别卡区间时，本卡查到的是 0（由 indexing kernel + vocab_range 屏蔽）
3. all_reduce 把所有卡的查表结果求和
   → 落在本卡区间的 token 得到正确 embedding，落在别卡的也得 0+正确值=正确值
```

这样不需要 `all_gather` 把权重拼起来，只要一次求和就能复原完整 embedding。

`ParallelLMHead` 则相反——它需要返回完整词表的 logits 给采样器，所以必须 `all_gather` 把各卡的 logits 片段拼起来。拼接后还要做一次维度重排（`permute` + `reshape`），因为 `all_gather` 默认是「先 rank0 的全部、再 rank1 的全部」排布，而词表逻辑顺序是「按词表 id 交错」的。

#### 4.3.3 源码精读

`VocabParallelEmbedding.forward` 用 `all_reduce` 把分卡查表结果求和：

```python
def forward(self, x):
    from minisgl.kernel import indexing
    y = indexing(weights=self.weight, indices=x,
                 vocab_range=self.vocab_range if self.tp_size > 1 else None)
    return self._comm.all_reduce(y) if self.tp_size > 1 else y
```

见 [python/minisgl/layers/embedding.py:32-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L32-L42)。`vocab_range` 让落在区间外的 token 查表得 0，再靠求和补回正确值。构造期算 `vocab_range` 的代码见 [python/minisgl/layers/embedding.py:14-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L14-L30)，用 `div_ceil`（向上取整）切词表，每卡的 `start_idx = vocab_local * rank`。

`ParallelLMHead.forward` 用 `all_gather` 拼 logits，并做维度重排：

```python
def forward(self, x):
    ...
    logits = F.linear(x, module.weight, self.bias)
    if self.tp_size == 1:
        return logits
    input_shape = logits.shape
    output_tensor = self._comm.all_gather(logits)             # (tp_size*bs, vocab_local)
    if bs == 1:
        return output_tensor.view(1, -1)[:, : self.num_embeddings]
    output_tensor = output_tensor.view((self.tp_size,) + input_shape)   # (tp_size, bs, vocab_local)
    output_tensor = output_tensor.permute(1, 0, 2).contiguous()         # (bs, tp_size, vocab_local)
    output_tensor = output_tensor.reshape(input_shape[:1] + (self.tp_size * input_shape[1],))
    return output_tensor[:, : self.num_embeddings]
```

见 [python/minisgl/layers/embedding.py:87-110](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L87-L110)。要点：
- `all_gather` 后第一维变成 `tp_size * bs`（rank 顺序排布）。
- `view + permute(1,0,2)` 把它重排成 `(bs, tp_size, vocab_local)`，即「先按 token、再按 rank」——这才是词表逻辑顺序。
- 最后 `[:, :num_embeddings]` 截断，因为 `div_ceil` 可能让总词表略大于真实 `vocab_size`（见 u8-l2 的形状契约）。
- `bs==1`（decode 常见）走快路径，直接 `view(1,-1)` 省去 permute 开销。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：画出 `tp=2` 时一个 MLP（`gate_up` → `down`）各权重的切分维度，并标出 `all_reduce` 发生的位置，解释为何只在 `down_proj` 后通信。

**操作步骤**：

1. 假设模型 `hidden_size=1024`，`intermediate_size=3072`，`tp_size=2`。
2. 对照 [python/minisgl/models/utils.py:25-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L25-L50) 的 `GatedMLP`，画一张表：

| 层 | 类型 | full 权重形状 | 每卡权重形状 | 前向通信 |
| --- | --- | --- | --- | --- |
| `gate_up_proj` | `LinearColParallelMerged([3072,3072])` | `(6144, 1024)` | `(3072, 1024)` | 无 |
| `down_proj` | `LinearRowParallel(3072→1024)` | `(1024, 3072)` | `(1024, 1536)` | `all_reduce` |

3. 在纸上画出激活值的流动：
   - 输入 `x` 形状 `(num_tokens, 1024)`，**两卡都持有完整 x**。
   - `gate_up = gate_up_proj(x)`：每卡算出 `(num_tokens, 3072)`，是完整 gate_up 的一半，**两卡手里的内容不同**，但谁都不需要对方的数据，不通信。
   - 激活 `y = silu_and_mul(gate_up)`：每卡 `(num_tokens, 1536)`，仍是各算各的。
   - `out = down_proj(y)`：每卡算出 `(num_tokens, 1024)`，但这是**部分和**（只用了自己那 1536 维输入），必须 `all_reduce` 求和才能得到完整结果。

**需要观察的现象**：整个 MLP 只在最后一步有一次 `all_reduce`；中间 `gate_up_proj` 和激活完全本地。

**预期结果**：表格与上图。**为何只在 `down_proj` 后通信**——因为 `gate_up_proj` 是列并行，每卡算出的是最终 gate_up 的一个**不重叠的输出块**，下游 `down_proj` 按行并行只需要自己这块输入就能算出一个部分和；只有这个部分和需要跨卡求和（行并行的数学定义 \(Y=\sum X_i W_i\)）。把通信从「每层都做」压成「每段只做一次」，是张量并行省通信的核心。

> 这个结论对 attention 同样成立：`qkv_proj`（列并行）+ attention + `o_proj`（行并行）也只在 `o_proj` 后做一次 `all_reduce`。所以每个 decoder layer 正好 2 次 `all_reduce`。

#### 4.3.5 小练习与答案

**练习 1**：`VocabParallelEmbedding` 为什么用 `all_reduce` 而不是 `all_gather`？

答案：因为每卡查表时对「非自己区间」的 token 返回 0，正确值恰好由持有该区间的那张卡返回。所有卡的结果**求和**即可复原，等价于「每个 token 的 embedding 只有一张卡贡献了非零值」。`all_gather` 会把所有卡的片段都拼起来（数据量翻 tp_size 倍），反而冗余。

**练习 2**：`ParallelLMHead` 的 `all_gather` 之后为什么还要 `permute(1,0,2)`？

答案：`all_gather` 按 rank 顺序拼接，得到 `(tp_size*bs, vocab_local)`，即「rank0 的所有 token、再 rank1 的所有 token」。但词表逻辑顺序需要「每个 token 的所有 vocab 片段挨在一起」，即 `(bs, tp_size, vocab_local)`。`view((tp_size,bs,vocab_local))` 拆出 rank 维后，`permute(1,0,2)` 把 token 维提到最前，正是为了恢复这个顺序。

### 4.4 DistributedCommunicator 与 PyNCCL 挂载

#### 4.4.1 概念说明

前面所有 `all_reduce` / `all_gather` 都通过 `DistributedCommunicator` 这一个对象发出。它本身不做通信，而是把通信**委托**给底层「插件」：

- 默认插件 `TorchDistributedImpl`：直接用 `torch.distributed`（底层是 NCCL 或 gloo）。
- 可选插件 `PyNCCLDistributedImpl`：用 Mini-SGLang 自研的 PyNCCL 通道（底层仍是 NCCL 库，但绕过 `torch.distributed` 的 Python 封装，便于做 CUDA Graph 友好的细粒度控制）。

切换由「插件栈」实现：`DistributedCommunicator` 维护一个列表，`all_reduce` / `all_gather` 永远调用**栈顶（最后一个）**插件。Engine 启动时如果启用 PyNCCL，就把 `PyNCCLDistributedImpl` 追加到栈顶，之后所有调用自动走 PyNCCL。这是一种很干净的「后缀优先」策略——不动现有代码、只追加一个插件就能改全局行为。

> 为什么需要 PyNCCL？因为 `torch.distributed` 的 `all_reduce` 在 CUDA Graph 捕获/回放场景下不够灵活（u5-l3 讲过 decode 用 CUDA Graph 反复回放）。PyNCCL 把 NCCL 句柄直接握在手里，可以放进图里回放，是 overlap scheduling + CUDA Graph 的重要拼图。

#### 4.4.2 核心流程

通信后端的三层结构：

```text
DistributedCommunicator         # 对外的门面，只有 all_reduce / all_gather 两个方法
        │  调用 plugins[-1]（栈顶）
        ▼
DistributedImpl（抽象基类）      # 定义 all_reduce / all_gather 接口
   ├── TorchDistributedImpl     # torch.distributed 实现（默认）
   └── PyNCCLDistributedImpl    # PyNCCL 实现（启用后追加到栈顶）
        │
        ▼
PyNCCLCommunicator（kernel 层） # tvm-ffi JIT 构建的 NCCL 封装
```

启用流程在 Engine 里：

```text
_init_communication(config):
    if tp_size == 1 or use_pynccl:        # 单卡 或 默认开启 pynccl
        init_process_group(backend="gloo")         # 只建 CPU 组（控制信令）
        enable_pynccl_distributed(...)             # 追加 PyNCCL 到栈顶（GPU 数据）
    else:                                  # --disable-pynccl
        init_process_group(backend="nccl")         # 用 torch.distributed 的 NCCL
        new_group(backend="gloo")                  # 另建 CPU 组做控制信令
```

注意单卡时也建 gloo 组——因为 u4-l2 讲过多 rank 广播需要 CPU 进程组做 `barrier` 和消息条数 `broadcast`，但单卡下这些其实不触发；这里主要是保持代码路径统一。

#### 4.4.3 源码精读

抽象基类 `DistributedImpl` 只定义两个抽象方法：[python/minisgl/distributed/impl.py:15-21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L15-L21)。

默认实现 `TorchDistributedImpl` 直接调 `torch.distributed`，且 `tp_size==1` 时短路返回：

```python
class TorchDistributedImpl(DistributedImpl):
    def all_reduce(self, x):
        tp_size = dist.get_world_size()
        if tp_size == 1:
            return x
        dist.all_reduce(x, op=dist.ReduceOp.SUM)
        return x
    def all_gather(self, x):
        tp_size = dist.get_world_size()
        if tp_size == 1:
            return x
        shape = list(x.shape)
        shape[0] = shape[0] * tp_size
        out = torch.empty(shape, dtype=x.dtype, device=x.device)
        dist.all_gather_into_tensor(out, x)
        return out
```

见 [python/minisgl/distributed/impl.py:24-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L24-L41)。注意 `all_gather` 沿 dim0 拼接（`shape[0] *= tp_size`），与 embedding 里 `permute` 的依据一致。

门面 `DistributedCommunicator` 的全部秘密就是「调用栈顶」：

```python
class DistributedCommunicator:
    plugins: List[DistributedImpl] = [TorchDistributedImpl()]
    def all_reduce(self, x):
        return self.plugins[-1].all_reduce(x)
    def all_gather(self, x):
        return self.plugins[-1].all_gather(x)
```

见 [python/minisgl/distributed/impl.py:63-70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L63-L70)。`plugins` 是**类变量**，全进程共享一个列表。

启用 PyNCCL 就是往这个列表追加一个插件：

```python
def enable_pynccl_distributed(tp_info, tp_cpu_group, max_bytes):
    if tp_info.size == 1:
        return
    from minisgl.kernel import init_pynccl
    comm = init_pynccl(tp_rank=tp_info.rank, tp_size=tp_info.size,
                       tp_cpu_group=tp_cpu_group, max_size_bytes=max_bytes)
    DistributedCommunicator.plugins.append(PyNCCLDistributedImpl(comm))
```

见 [python/minisgl/distributed/impl.py:73-90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L73-L90)。`PyNCCLDistributedImpl`（[python/minisgl/distributed/impl.py:44-60](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L44-L60)）把调用转给持有的 `PyNCCLCommunicator`。`max_bytes` 决定 PyNCCL 预分配的最大通信缓冲（Engine 里按 `max_forward_len * hidden_size * itemsize` 估算，见 [python/minisgl/engine/engine.py:123-126](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L123-L126)）。

Engine 的 `_init_communication` 决定建哪种进程组：[python/minisgl/engine/engine.py:112-137](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L112-L137)。`use_pynccl` 默认 `True`（[python/minisgl/engine/config.py:29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L29)），CLI 上用 `--disable-pynccl` 关闭（[python/minisgl/server/args.py:124-130](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L124-L130)）。

最底层的 `PyNCCLCommunicator` 由 `init_pynccl` 用 tvm-ffi JIT 构建，它的关键步骤是用 gloo CPU 组广播 NCCL unique ID，让所有 rank 拿到同一个通信句柄：

```python
def init_pynccl(*, tp_rank, tp_size, tp_cpu_group, max_size_bytes=0):
    ...
    if tp_rank == 0:
        id_list = [module.create_nccl_uid()]
    else:
        id_list = [None]
    torch.distributed.broadcast_object_list(id_list, src=0, group=tp_cpu_group)
    nccl_id = id_list[0]
    return cls(tp_rank, tp_size, max_size_bytes, nccl_id)
```

见 [python/minisgl/kernel/pynccl.py:45-78](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L45-L78)。`PyNCCLCommunicator` 的接口签名（`all_reduce` / `all_gather` / `get_buffer`）见 [python/minisgl/kernel/pynccl.py:16-25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L16-L25)。这里又一次印证 u4-l2 的结论：**gloo CPU 组管控制信令（广播 UID），PyNCCL/NCCL 管 GPU 数据**，两者职责解耦。

进程退出时 `destroy_distributed` 把插件栈清空：[python/minisgl/distributed/impl.py:93-97](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L93-L97)。

#### 4.4.4 代码实践

**实践目标**：通过 `tests/kernel/test_comm.py` 理解 PyNCCL `all_reduce` / `all_gather` 的正确性如何被验证，并观察它的多进程测试骨架。

**操作步骤**：

1. 读 [tests/kernel/test_comm.py:14-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L14-L41) 的 `run(tp_size, tp_rank)`：它先 `set_tp_info`、建 gloo 组、再 `init_pynccl` 建通信器——这正是 Engine `_init_communication` 的简化版。
2. 读 [tests/kernel/test_comm.py:96-135](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L96-L135) 的 `test_correctness`：它用一个全 1 张量反复 `all_reduce(x, "sum")` 共 \(N\) 次，期望结果是 `pow(tp_size, N)`——因为每次 all_reduce 都把所有卡的值求和后写回，\(N\) 次累乘正是 `tp_size ** N`。
3. 读 [tests/kernel/test_comm.py:141-149](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L141-L149) 的 all_gather 测试：每卡填入自己的 `rank` 值，gather 后期望 `0,0,...,0,1,1,...,1,...`（`repeat_interleave`），正好印证「all_gather 按 rank 顺序拼接」。
4. 看 [tests/kernel/test_comm.py:153-172](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L153-L172) 的 `__main__`：用 `multiprocessing` + `spawn` 起 `tp_size=4` 个进程，每张卡跑一个 `run`，这是多卡测试的标准骨架。

**需要观察的现象**：`test_correctness` 里有一段 `if tp_rank == 0: time.sleep(1)`——故意让 rank 0 慢一拍，验证 NCCL 的 `all_reduce` 会等齐所有 rank（同步语义），不会因为某个 rank 慢就丢数据。

**预期结果**：在 4 卡机器上 `python tests/kernel/test_comm.py` 应打印 4 个 rank 各自的 `Correctness check for rank X passed` 和带宽统计。单卡环境无法运行多进程 NCCL，此为「待本地验证（需多卡）」；可仅做源码阅读，重点理解 `pow(tp_size, N)` 这个断言如何同时验证正确性与同步性。

#### 4.4.5 小练习与答案

**练习 1**：`enable_pynccl_distributed` 在 `tp_info.size == 1` 时直接 `return`，为什么？

答案：单卡根本没有跨卡通信需求，`DistributedCommunicator.plugins` 里默认的 `TorchDistributedImpl.all_reduce` 也会在 `tp_size==1` 时短路返回原张量。追加 PyNCCL 插件既无意义（没有对端）也建不起来（NCCL 需要至少 2 个 rank 协商 UID），所以直接跳过。

**练习 2**：假设有人误调了两次 `enable_pynccl_distributed`，会发生什么？

答案：`plugins` 会变成 `[Torch, PyNCCL_1, PyNCCL_2]`，`plugins[-1]` 指向第二个 PyNCCL。功能上仍正确（两个 PyNCCL 行为一致），但浪费了一份 NCCL 句柄与缓冲显存。代码没有显式防重入，靠 Engine 只调一次的调用约定保证。

## 5. 综合实践

把本讲四个模块串起来，完成一个「端到端追踪一次 TP 前向的通信」任务。

**任务**：以 `tp_size=2`、模型为 dense Llama（`hidden=1024`, `intermediate=3072`, `num_qo=16`, `num_kv=8`, `head_dim=128`, `vocab=32000`）为设定，回答下列问题并把答案整理成一张「通信地图」：

1. **身份层**：Engine 启动时在哪一行登记 TP 身份？rank 0 和 rank 1 的 `DistributedInfo` 分别是什么？（提示：[engine.py:32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L32)）
2. **切分层**：一个 decoder layer 里，`qkv_proj` / `o_proj` / `gate_up_proj` / `down_proj` 各自每卡权重形状是多少？分别属于哪种切分？
3. **通信层**：这个 layer 前向时，一共发生几次 `all_reduce`？分别在哪些类里触发？走的是 `TorchDistributedImpl` 还是 `PyNCCLDistributedImpl`（默认配置下）？
4. **首尾层**：模型开头的 `embed_tokens` 用 `all_reduce` 还是 `all_gather`？末尾的 `lm_head` 呢？为什么两者不同？
5. **切换层**：若用户加 `--disable-pynccl`，`_init_communication` 会走哪个分支？此时 `all_reduce` 底层变成什么？

**参考答案要点**：

1. `engine.py:32` 的 `set_tp_info(rank=config.tp_info.rank, size=config.tp_info.size)`；rank 0 是 `DistributedInfo(0,2)`、rank 1 是 `DistributedInfo(1,2)`。
2. `qkv_proj`（列并行）每卡 `((8+2*4)*128, 1024)=(2048,1024)`；`o_proj`（行并行）每卡 `(1024, 8*128)=(1024,1024)`；`gate_up_proj`（列并行合并）每卡 `(3072,1024)`；`down_proj`（行并行）每卡 `(1024,1536)`。
3. 一个 layer 发生 **2 次** `all_reduce`：`LinearOProj.forward`（[linear.py:102-106](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L102-L106)）和 `LinearRowParallel.forward`（[linear.py:123-127](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L123-L127)）。默认 `use_pynccl=True`，栈顶是 `PyNCCLDistributedImpl`。
4. `embed_tokens` 用 `all_reduce`（屏蔽 + 求和复原），`lm_head` 用 `all_gather`（需要完整词表 logits 给采样器）。不同是因为 embedding 只需「复原每条 token 的向量」（求和即可），而 lm_head 必须把所有词表的分数都给采样器挑（必须拼接全词表）。
5. 走 `else` 分支（[engine.py:128-135](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L128-L135)）：建 NCCL 进程组，不追加 PyNCCL 插件，`all_reduce` 底层变成 `torch.distributed` 的 NCCL（即 `dist.all_reduce`）。

## 6. 本讲小结

- 张量并行的两种切分：**列并行切输出（dim0）、不需要通信**；**行并行切输入（dim1）、前向后必须 `all_reduce` 求和**。
- TP Linear 族 5 个成员都建在 `_LinearTPImpl` 上，区别只在构造时算 local 形状的方式和前向是否通信；`LinearQKVMerged` 用 `div_even(..., allow_replicate=True)` 处理 GQA 下 kv 头少于卡数的复制场景。
- 「先列并行后行并行」的组合（MLP 的 gate_up→down、Attention 的 qkv→o_proj）能把每段通信压成一次 `all_reduce`，每个 decoder layer 正好 2 次。
- `all_reduce` 还用于词表并行 embedding（屏蔽+求和复原），`all_gather` 用于 `lm_head` 拼接全词表 logits，后者需要 `permute` 重排 rank 顺序。
- `DistributedCommunicator` 用「插件栈」抽象通信后端，`plugins[-1]` 永远生效；`enable_pynccl_distributed` 追加 `PyNCCLDistributedImpl` 到栈顶即完成从 `torch.distributed` 到 PyNCCL 的切换，默认开启。
- PyNCCL 底层仍是 NCCL 库，靠 gloo CPU 组广播 UID 协商句柄；它比 `torch.distributed` 更适合 CUDA Graph 回放场景，是 overlap scheduling 的通信拼图。

## 7. 下一步学习建议

本讲把「模型层怎么切、怎么通信」讲完了，接下来有两个自然方向：

- **向上一层（u9-l2 Embedding/Norm/RoPE/Attention）**：看 `AttentionLayer` 如何用 `LinearQKVMerged` 算出的 qkv 切分维度来 split q/k/v、做 RoPE 并调用注意力后端，把本讲的切分结果接到注意力计算上。
- **向下一层（u10-l2 自定义 Kernel）**：本讲提到的 `PyNCCLCommunicator`、`indexing`（词表并行查表）、`all_reduce` 的底层都是 tvm-ffi JIT 构建的 CUDA kernel，u10-l2 会展开这些 kernel 如何被构建与调用。
- **横向对照（u4-l2 多 rank 广播）**：本讲的 `all_reduce`/`all_gather` 是「GPU 数据通信」，u4-l2 的 ZMQ pub/sub + gloo broadcast 是「CPU 控制信令」，两者共同构成多卡协同，值得对照阅读以区分「数据面」与「控制面」。
