# 微批次切分与张量拼接 scatter/gather

## 1. 本讲目标

本讲深入 `dualpipe/utils.py` 中四个短小但关键的函数：`chunk_tensor`、`cat_tensor`、`scatter`、`gather`。它们是 DualPipe 把「一整批数据」与「流水线需要的微批次列表」相互转换的总开关。

学完本讲你应该能够：

- 说清 `scatter` 如何把一个（或多个）整张量沿指定维度切成 `chunks` 个微批次，并「转置」成按微批次组织的列表；
- 说清 `gather` 如何把各微批次的输出再拼接回完整输出；
- 理解 `None` 输入、单元素输出、空序列这三类边界情况分别由哪一行代码处理；
- 解释为什么底层切片用 `torch.tensor_split` 而不是 `torch.chunk`；
- 知道引擎在 `DualPipe.step` / `DualPipeV.step` 的哪几行调用了这两个函数。

---

## 2. 前置知识

本讲默认你已掌握 u2-l1 建立的两个概念：

1. **微批次（micro-batch，源码里叫 chunk）**：把一个 batch 沿 batch 维切成多片，依次灌入流水线，以降低「灌水/排水」阶段的相对气泡，气泡占比近似为 \((P-1)/(P-1+M)\)（\(P\) 为阶段数，\(M\) 为微批次数）。
2. **双向流水线**：数据从流水线两端相向喂入，每个 rank 持有一对镜像 stage；首 rank 喂 forward 方向、末 rank 喂 reverse 方向，因此首末 rank 各自只拿到「一半」输入。

此外你需要一点 PyTorch 张量操作基础：

- `torch.cat(tensors, dim)`：沿 `dim` 把多个张量拼起来（scatter 的逆操作）。
- `torch.chunk(t, n, dim)` 与 `torch.tensor_split(t, n, dim)`：都号称「切成 n 份」，但语义有差别（见 4.1.3）。
- Python 内置 `zip(*lists)`：对多个列表「按列取」，是 scatter 转置的核心技巧。

> 说明：本讲引用的源码行号基于当前 HEAD `030ce43`。本讲聚焦 `utils.py` 四个函数本身，引擎中 `step` 调用它们的上下文只点到为止，详细的八步调度留给 u3。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `dualpipe/utils.py` | 工具层 | `chunk_tensor` / `cat_tensor` / `scatter` / `gather` 四个函数 |
| `dualpipe/dualpipe.py` | DualPipe 引擎 | 在 `step` 开头 `scatter`、结尾 `gather`（行 345-346、434） |
| `dualpipe/dualpipev.py` | DualPipeV 引擎 | 同样的两处调用（行 326-327、405） |

这四个函数都是**内部工具**：它们没有被 `dualpipe/__init__.py` 导出（见 u1-l3），只在两个引擎里通过 `from dualpipe.utils import ... scatter, gather` 引入。`WeightGradStore` 虽然与它们同处一文件，但属于另一个主题（零气泡），由 u2-l4 专门讲解，本讲不展开。

---

## 4. 核心概念与源码讲解

本讲拆成两个对称的最小模块：**切**（`chunk_tensor` + `scatter`）与**拼**（`cat_tensor` + `gather`）。

### 4.1 切分原语：chunk_tensor 与 scatter

#### 4.1.1 概念说明

流水线并行的第一步，是把用户一次性给出的「整批输入」拆成流水线能逐片消化的「微批次列表」。这里有一个数据组织方式的转换：

- **用户视角**：输入是「按张量分组」——比如 `(x, y)` 表示两个张量，每个张量内部包含整批数据。
- **流水线视角**：需要「按微批次分组」——第 0 个微批次要同时拿到 `x` 的第 0 片和 `y` 的第 0 片，才能整体送进模型。

`scatter` 就是完成这个「行↔列」转置的函数；`chunk_tensor` 是它底层的「按维均分」工具，并顺带处理 `None` 这种「这个 rank 没有输入」的情况。

为什么需要处理 `None`？因为中间 rank 不喂数据（见 u2-l1），但它和首末 rank 跑的是**同一套** `step` 代码、同样要调用 `scatter`。工具函数必须对「没有数据」也给出一个结构正确（长度仍为 `chunks`）的结果，才能让下游循环对所有 rank 行为一致。

#### 4.1.2 核心流程

`scatter(inputs, chunks, dim)` 的执行过程可以用伪代码描述：

```
输入校验：
    inputs 必须是 Tensor / tuple / list（不能是裸 None）
    每个元素必须是 None 或 Tensor
归一化：
    若 inputs 是单个 Tensor，包成 (inputs,)
逐元素切片：
    对每个元素 x，调用 chunk_tensor(x, chunks, dim)
    得到「每个输入 → 它的 chunks 个切片」
转置：
    zip(*inputs) 把「按输入分组」翻成「按微批次分组」
兜底：
    若转置结果为空（即 inputs 本身是空序列），补成 chunks 个空 tuple
返回：
    长度为 chunks 的列表，第 k 项是第 k 个微批次（一个 tuple）
```

把「两个输入 × 四个微批次」的转置画出来就是：

```
切片后（按输入分组）：        转置后（按微批次分组）：
  inputs[0] = (x0, x1, x2, x3)      microbatches[0] = (x0, y0)
  inputs[1] = (y0, y1, y2, y3)      microbatches[1] = (x1, y1)
                                     microbatches[2] = (x2, y2)
                                     microbatches[3] = (x3, y3)
```

关键性质：当 `chunks=4` 时，无论输入是一个张量、两个张量、还是「这个 rank 没数据（元素为 None）」，`scatter` **总是返回恰好 4 个微批次**。这个「数量恒定」的保证是流水线调度的前提。

#### 4.1.3 源码精读

先看底层切片函数 `chunk_tensor`：

[dualpipe/utils.py:46-49](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L46-L49) —— `chunk_tensor` 做两件事：若 `x is None`，返回一个含 `chunks` 个 `None` 的列表（让「无输入」的 rank 也能拿到正确长度的结果）；否则用 `x.tensor_split(chunks, dim=dim)` 做均分。

```python
def chunk_tensor(x, chunks, dim):
    if x is None:
        return [None for _ in range(chunks)]
    return x.tensor_split(chunks, dim=dim)
```

> **为什么是 `tensor_split` 而不是 `torch.chunk`？**
> 这是本讲最容易被忽略、却最关键的细节。`torch.chunk` 在维度不能被整除时**可能返回比请求数更少的块**；而 `torch.tensor_split` **总是返回恰好 `chunks` 个切片**（不能整除时把余数分给靠前的几块，行为与 `numpy.array_split` 一致）。流水线调度（u3 的八步循环）严格假设「微批次数量 = num_chunks」，多一个少一个都会错位。`scatter` 选用 `tensor_split` 正是为了锁定这个数量保证。类型上，`tensor_split` 返回的是一个 tuple。

再看 `scatter` 主体：

[dualpipe/utils.py:62-71](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L62-L71) —— `scatter`：先断言 `inputs` 是 Tensor/tuple/list（**不接受裸 `None`**）；若是单张量则包成单元素 tuple；逐元素 `chunk_tensor` 切片后用 `zip(*inputs)` 转置成「按微批次分组」；若转置为空（`inputs` 是空序列）则补成 `chunks` 个空 tuple。

```python
def scatter(inputs, chunks, dim):
    assert isinstance(inputs, (torch.Tensor, tuple, list))
    if isinstance(inputs, torch.Tensor):
        inputs = (inputs,)
    assert all(x is None or isinstance(x, torch.Tensor) for x in inputs)
    inputs = [chunk_tensor(x, chunks, dim) for x in inputs]
    microbatches = [microbatch for microbatch in zip(*inputs)]
    if len(microbatches) == 0:
        microbatches = [() for _ in range(chunks)]
    return microbatches
```

逐行拆解：

1. **第 63 行断言**：`inputs` 必须是 Tensor/tuple/list。注意它**拒绝裸 `None`**——`scatter(None, ...)` 会直接 `AssertionError`。那为什么引擎里中间 rank 传 `None` 却不报错？答案在引擎签名上：`step` 的第一个参数是 `*inputs`（可变位置参数），见 [dualpipe/dualpipe.py:296](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L296)。调用 `step(x)` 时，即便 `x is None`，也会被 Python 自动包成 `inputs=(None,)` 再传给 `scatter`。也就是说，**`*inputs` 这一招把「裸 None」变成了「含一个 None 的 tuple」**，从而既满足了 `scatter` 的断言，又让 `chunk_tensor` 的 `None` 分支得以生效。这是工具层与引擎层之间一个精巧的契约。
2. **第 64-65 行**：单张量包成 `(inputs,)`，保证后面「逐元素」逻辑统一。
3. **第 66 行断言**：每个元素要么是 `None`，要么是 Tensor。
4. **第 67 行**：对每个输入调用 `chunk_tensor`，得到「每个输入 → 它的 chunks 片」。
5. **第 68 行 `zip(*inputs)`**：核心转置——「每个输入的第 k 片」聚成「第 k 个微批次」。
6. **第 69-70 行兜底**：只有当 `inputs` 是**空序列** `()`/`[]` 时，`zip(*inputs)` 才为空，此时补成 `chunks` 个空 tuple，保证返回长度仍是 `chunks`。

最后看看引擎里这两行的真实用法：

[dualpipe/dualpipe.py:345-346](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L345-L346) —— `DualPipe.step` 在开头把 `inputs` 和 `labels` 都 `scatter` 成 `half_num_chunks` 个微批次。注意切的是**一半**（`half_num_chunks = num_chunks // 2`），因为另一半微批次走反方向（见 u2-l1 双向设计）。

```python
inputs = scatter(inputs, half_num_chunks, self.batch_dim)
labels = scatter(labels, half_num_chunks, self.batch_dim)
```

DualPipeV 则只在首 rank 喂数据，且切的是**全部** `num_chunks`（没有「另一半」）：

[dualpipe/dualpipev.py:326-327](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L326-L327) —— DualPipeV 只在 `is_first_rank` 时 scatter，且用完整的 `num_chunks`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `scatter` 的「转置 + 数量恒定」性质，并测出 `None` 输入的行为。

**操作步骤**（需已安装 PyTorch 与 `dualpipe`，可用 `pip install -e .` 安装；若无法运行，按下方「预期结果」阅读理解即可）：

```python
# 示例代码（非项目原有，仅用于演示 scatter 行为）
import torch
from dualpipe.utils import scatter

x = torch.randn(24, 8)            # 整批输入：(24, 8)
micro = scatter(x, chunks=4, dim=0)

# 1) 数量恒定：始终 4 个微批次
print(len(micro))                 # 4
# 2) 每个微批次是一个 tuple，其第 0 个元素是 (6, 8) 的切片
print(type(micro[0]), micro[0][0].shape)   # <class 'tuple'> torch.Size([6, 8])

# 3) None 输入：必须包在序列里，不能传裸 None
m_none = scatter((None,), chunks=4, dim=0)
print(len(m_none), m_none[0])     # 4 (None,)
# scatter(None, 4, 0)  # 这一行会 AssertionError，可自行取消注释验证
```

**需要观察的现象**：

- `len(micro) == 4`，每个微批次是 tuple，其张量形状为 `(6, 8)`（24÷4）。
- `scatter((None,), 4, 0)` 返回 `[(None,), (None,), (None,), (None,)]`——长度仍是 4，内容是 `None`。
- `scatter(None, 4, 0)` 抛 `AssertionError`。

**预期结果**：上述输出与注释一致。若你把 `chunks` 改成不能整除 24 的值（比如 `chunks=5`），由于用的是 `tensor_split`，仍会得到**恰好 5 个**微批次（前 4 个 5 行、最后 1 个 4 行），而非更少——这正是 `tensor_split` 相对 `chunk` 的优势。

**待本地验证**：上述命令的精确输出需在装好 PyTorch 的环境运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scatter` 第一行要拒绝裸 `None`，而 `chunk_tensor` 又专门处理 `None`？这两者矛盾吗？

**参考答案**：不矛盾，它们分工不同。`scatter` 的断言约束的是**整体入参的类型**——它要求调用方传一个「序列」（Tensor/tuple/list），避免把「无输入」和「非法入参」混淆。`chunk_tensor` 处理的 `None` 是**序列内部的某个元素**为 `None`（代表「这一路输入在这个 rank 上没有」）。引擎用 `step(*inputs)` 的可变参数把中间 rank 的裸 `None` 包成 `(None,)`，正好让两层各司其职：`scatter` 收到合法的 tuple，`chunk_tensor` 负责把里面的 `None` 展开成正确长度。

**练习 2**：若把 `chunk_tensor` 里的 `tensor_split` 换成 `torch.chunk`，在什么输入下会出 bug？

**参考答案**：当 batch 维度不能被 `chunks` 整除时。例如 `x` 第 0 维为 6、`chunks=4` 时，`torch.chunk(x, 4, 0)` 只会返回 3 个块（每块 2 行），于是 `scatter` 返回 3 个微批次而非 4 个，下游按 `num_chunks` 设计的八步循环会错位。`tensor_split` 在这种情况下仍返回恰好 4 个块（2,2,1,1），保住了「数量恒定」的不变量。

---

### 4.2 聚合原语：cat_tensor 与 gather

#### 4.2.1 概念说明

`gather` 是 `scatter` 的逆过程：流水线把每个微批次各自算出一小段输出，最后需要把这些小段**按原顺序拼回**一整批输出。`cat_tensor` 是它底层的「按维拼接」工具，并处理三类边界：

- 每个微批次只产出**一个**张量时，不要无谓地包一层；
- 某路输出全程为 `None`（比如纯前向推理时不返回输出）时，拼出来仍是 `None`；
- 多路输出（一个微批次产出多个张量）时，要按「第几路」分别聚合。

#### 4.2.2 核心流程

`gather(micro_outputs, dim)` 的过程：

```
输入校验：
    micro_outputs[0] 必须是 Tensor / tuple / list
归一化：
    若每个微批次产出的是单个 Tensor，把它们各自包成 (x,)
分组转置：
    zip(*micro_outputs) 按「第几路输出」分组
逐路拼接：
    对每组调用 cat_tensor(..., dim) 拼回
返回：
    一个 tuple，第 k 项是第 k 路输出的聚合结果
```

与 `scatter` 对称的图示（每微批次单路输出，4 个微批次）：

```
micro_outputs = [y0, y1, y2, y3]   （4 个微批次的输出张量）
归一化：        [(y0,), (y1,), (y2,), (y3,)]
转置：          [(y0, y1, y2, y3)]   （1 路，含 4 段）
拼接：          (torch.cat([y0,y1,y2,y3], dim),)
返回：          长度 1 的 tuple
```

注意 `gather` **总是返回一个 tuple**（哪怕只有一路输出，也是一个 1-tuple）。引擎拿到后会判断长度是否为 1 并解包（见 4.2.3）。

#### 4.2.3 源码精读

先看底层 `cat_tensor`：

[dualpipe/utils.py:52-59](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L52-L59) —— `cat_tensor` 处理三种情况：序列长度为 1 时直接取出唯一元素（不必 cat）；序列首元素为 `None` 时断言全部为 `None` 并返回 `None`；其余情况用 `torch.cat` 沿 `dim` 拼接。

```python
def cat_tensor(x, dim):
    if (isinstance(x, tuple) or isinstance(x, list)):
        if len(x) == 1:
            return x[0]
        elif x[0] is None:
            assert all(y is None for y in x)
            return None
    return torch.cat(x, dim=dim)
```

三个分支的用途：

1. **`len(x) == 1`**：这组只有一段，无需拼接，直接返回它本身（即便它本身是 `None` 也安全——单元素 `None` 会从这里返回 `None`）。
2. **`x[0] is None`**：用 `assert all(y is None for y in x)` 确保整组都是 `None`（防止「半 None 半张量」这种不一致状态），然后返回 `None`。对应「这路输出本就不存在」的场景。
3. **`torch.cat(x, dim=dim)`**：正常的多段拼接。

再看 `gather`：

[dualpipe/utils.py:74-80](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L74-L80) —— `gather`：断言首个微批次产出的是 Tensor/tuple/list；若是单 Tensor 则把每个微批次包成单元素 tuple；用 `zip(*micro_outputs)` 按「第几路输出」分组；逐组 `cat_tensor` 拼回；返回一个 tuple。

```python
def gather(micro_outputs, dim):
    assert isinstance(micro_outputs[0], (torch.Tensor, tuple, list))
    if isinstance(micro_outputs[0], torch.Tensor):
        micro_outputs = [(x,) for x in micro_outputs]
    outputs = [x for x in zip(*micro_outputs)]
    outputs = tuple(cat_tensor(x, dim=dim) for x in outputs)
    return outputs
```

要点：

- **第 75 行断言**：要求 `micro_outputs` 非空，且第一个元素类型合法。
- **第 76-77 行归一化**：和 `scatter` 对称——若每个微批次产出单个 Tensor，包成单元素 tuple，统一后续逻辑。
- **第 78 行 `zip(*micro_outputs)`**：按「第几路输出」分组。若每个微批次产出 2 个张量，这里会得到 2 组，分别聚合。
- **返回 tuple**：`gather` 永远返回 tuple，长度 = 每个微批次产出的张量数。

引擎里的用法（DualPipe 结尾聚合输出）：

[dualpipe/dualpipe.py:434-436](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L434-L436) —— `gather` 把首/末 rank 缓存的各微批次输出拼回，并用 `len(outputs) == 1` 解包成单个张量（否则保持 tuple）。

```python
outputs = gather(self.output_chunks[self.is_first_rank], self.batch_dim)
if len(outputs) == 1:
    outputs = outputs[0]
```

> 这里 `self.is_first_rank` 是个布尔值当索引用（首 rank 取 `True=1`、末 rank 取 `False=0`），对应双向设计中首末 rank 各自「有效输出」落在 `output_chunks` 的不同槽位，细节留给 u3。

DualPipeV 的对称写法在 [dualpipe/dualpipev.py:405](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L405) —— `outputs = gather(self.output_chunks[1], self.batch_dim)`。

#### 4.2.4 代码实践

**实践目标**：验证 `scatter → gather` 的往返一致性，并观察 `None` 输出的聚合结果。

**操作步骤**：

```python
# 示例代码（非项目原有，仅用于演示 gather 行为）
import torch
from dualpipe.utils import scatter, gather

x = torch.randn(24, 8)
micro = scatter(x, chunks=4, dim=0)          # 4 个微批次，每个是 (t,) 的 tuple

# 取出每个微批次的张量，模拟「各微批次各算一路输出」
micro_outputs = [mb[0] for mb in micro]
out = gather(micro_outputs, dim=0)

print(type(out), len(out))                    # <class 'tuple'> 1
print(torch.equal(out[0], x))                 # True —— 往返一致

# None 输出的聚合
none_micro = scatter((None,), chunks=4, dim=0)        # [(None,)] * 4
none_out = gather([mb[0] for mb in none_micro], dim=0)
print(none_out)                               # (None,)
```

**需要观察的现象**：

- `out` 是长度为 1 的 tuple，`out[0]` 与原张量 `x` 逐元素相等。
- 全 `None` 输入经过 `gather` 后得到 `(None,)`（1-tuple，元素为 `None`），与 4.1.4 中 `scatter((None,), ...)` 的结果正好配对。

**预期结果**：往返 `torch.equal` 为 `True`；`none_out == (None,)`。

**待本地验证**：精确输出需在装好 PyTorch 的环境运行确认。

#### 4.2.5 小练习与答案

**练习 1**：`gather` 为什么总是返回 tuple，而不是「单路输出时直接返回张量」？

**参考答案**：为了让接口语义统一——调用方总能按「第几路输出」去索引。真正的「解包成单张量」交由引擎在外层用 `if len(outputs) == 1: outputs = outputs[0]`（dualpipe.py:435-436）显式完成。这样 `gather` 自身不必猜测调用方想要 tuple 还是裸张量，职责更单一。

**练习 2**：`cat_tensor` 里 `assert all(y is None for y in x)` 这一句去掉会怎样？

**参考答案**：它会变成「半静默」——若某组里既有 `None` 又有张量（一种不该出现的不一致状态），`torch.cat` 会因为混入 `None` 而报错，但错误信息会指向 `torch.cat` 而非真正的数据不一致。保留断言能把这种「数据装配错误」在最早处、用清晰的语义暴露出来，属于防御式编程。

---

## 5. 综合实践

**任务**：模拟「双路输入 + 双路输出」的微批次往返，完整跑通 `scatter → 逐微批次处理 → gather`，并验证往返一致性。

设想一个 stage 接收两个张量 `(x, w)`，各产出两个张量 `(y, z)`。我们要把它切成 4 个微批次分别处理，再拼回。

```python
# 示例代码（非项目原有，综合演示 scatter/gather 的双路往返）
import torch
from dualpipe.utils import scatter, gather

torch.manual_seed(0)
x = torch.randn(24, 8)
w = torch.randn(24, 4)

# 1) 双路输入切成 4 个微批次：每个微批次是 (x_k, w_k)
micro = scatter((x, w), chunks=4, dim=0)
print(len(micro), len(micro[0]))          # 4 2 —— 4 个微批次，每个含 2 路

# 2) 逐微批次处理：模拟 stage 产出双路输出 (y=x_k*2, z=w_k+1)
micro_outputs = []
for (xk, wk) in micro:
    micro_outputs.append((xk * 2, wk + 1))   # 每个 micro_output 是 2-tuple

# 3) 把双路输出分别聚合回整批
out = gather(micro_outputs, dim=0)
print(type(out), len(out))                # <class 'tuple'> 2 —— 2 路
y_full, z_full = out
print(torch.equal(y_full, x * 2))         # True
print(torch.equal(z_full, w + 1))         # True
```

**思考要点**：

1. `scatter((x, w), 4, 0)` 返回的每个微批次是 2-tuple，对应两路输入的第 k 片——验证了「按微批次分组」。
2. `gather` 收到的每个微批次产出是 2-tuple，最终返回 2-tuple `(y_full, z_full)`——验证了「按输出路数分组」。
3. 两个 `torch.equal` 都为 `True`，说明 scatter/gather 是**无损往返**：信息没有丢失或错位。

**待本地验证**：精确的 `True/True` 需在装好 PyTorch 的环境运行确认；若只做源码阅读，可对照 4.1.2 与 4.2.2 的转置图手算每一步的形状。

把这道题走通，你就真正理解了「整批 ↔ 微批次列表」这层抽象在 DualPipe 里是如何无损地来回切换的。

---

## 6. 本讲小结

- `chunk_tensor` / `cat_tensor` 是底层原语：前者用 `torch.tensor_split` 按维均分（并处理 `None`），后者用 `torch.cat` 拼接（并处理单元素、全 `None` 边界）。
- `scatter` 把整批输入「转置」成 `chunks` 个微批次列表；`gather` 把各微批次输出按路聚合回整批，二者互逆且无损。
- `scatter` 选用 `tensor_split` 而非 `torch.chunk`，是为了在维度不可整除时**仍保证恰好 `chunks` 个微批次**——这是流水线调度「数量恒定」的前提。
- `None` 边界由两层分担：`scatter` 拒绝裸 `None`（要求序列），`chunk_tensor` 处理序列内的 `None` 元素；引擎 `step(*inputs)` 的可变参数把中间 rank 的裸 `None` 自动包成 `(None,)`，把两层衔接起来。
- `gather` 总返回 tuple（哪怕单路），解包由引擎外层 `if len(outputs) == 1` 显式完成。
- 引擎中 `DualPipe.step` 在开头 scatter（切一半）、结尾 gather；`DualPipeV.step` 在首 rank scatter（切全部）、结尾 gather。

---

## 7. 下一步学习建议

- **u2-l4 零气泡机制 WeightGradStore**：回到同一个 `utils.py`，看 `WeightGradStore` 与 `run_backward` 如何把权重梯度计算延后，这是另一条贯穿引擎的公共机制。
- **u3-l2 状态管理与计算原语**：本讲看到 `scatter` 产出的微批次列表会被存进引擎的 `input_chunks` / `labels` / `output_chunks` 等缓冲，下一单元会展开这些缓冲的二维结构 `[phase][chunk_id]`。
- **u3-l5 八步调度引擎 step()**：本讲只点到 `step` 的 scatter/gather 两处调用，完整的「灌水—重叠—排水」八步循环在那里详解，届时你会看到「微批次数恒定」这个不变量是如何被调度依赖的。
