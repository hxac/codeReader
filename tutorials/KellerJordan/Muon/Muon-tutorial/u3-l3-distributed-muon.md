# 分布式 Muon：Muon.step 的跨卡参数分片与 all_gather 同步

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清分布式 `Muon` 的设计思路：**数据不动、梯度已一致，把「参数更新」这个计算任务按参数轮流分摊到各张卡上**，再用 `all_gather` 把更新后的参数广播给所有人。
2. 逐行读懂 [muon.py:L71-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L71-L96) 中最难的一段：参数按尺寸降序排序、用空张量补齐到 `world_size` 的倍数、每个 rank 只计算自己分到的参数、`all_gather` 原地写回。
3. 解释两个「不起眼但致命」的细节为什么长这样：构造函数为什么必须显式列表并排序；`p.grad` 为 `None` 时为什么填零而不是跳过。
4. 学会用 `torchrun` + `gloo` 后端在**纯 CPU、无 GPU** 的环境下验证分布式优化器的正确性。

## 2. 前置知识

本讲是整个学习手册里第一个「分布式」讲义，先补齐几个概念。你已经在前几讲见过 Muon 的算法本体（动量 + Newton-Schulz 正交化），本讲**没有新算法，只有新的并行编排**。

### 2.1 rank 与 world_size：W 个工人的编号制度

用 `torchrun` 启动训练时，同一个脚本会被启动成 \( W \) 个独立进程（`W = world_size`，通常每张 GPU 一个）。每个进程拿到一个编号 \( r \in \{0, 1, \dots, W-1\} \)，即 `rank`。

- `dist.get_world_size()`：返回 \( W \)，队伍总共多少人。
- `dist.get_rank()`：返回 \( r \)，我自己的工号。

关键认知：**每个 rank 执行的是同一份代码**，但 `if dist.get_rank() == 0: ...` 这类分支让不同进程做不同的事。分布式代码的全部难点，就是保证 W 份执行流在恰当的时机「对表」。

### 2.2 集合通信三兄弟

集合通信（collective）是让 W 个进程同步交换数据的原语。本讲只用到 `all_gather`，先用一张表建立直觉（以 \( W = 3 \) 为例）：

| 原语 | 语义 | 调用后每个 rank 拥有 |
|---|---|---|
| `broadcast` | rank 0 把自己的张量发给大家 | 同一份 rank 0 的数据 |
| `all_reduce` | 各 rank 的张量逐元素求和（或求最大值等）后人人可得 | 同一份聚合结果（DDP 用它平均梯度）|
| `all_gather` | 每个 rank 贡献一个张量，**人人都拿到完整列表** | 长度为 W 的张量列表：第 j 个位置是 rank j 的贡献 |

`all_gather` 的「按位置对齐」契约是本讲的核心新知识，展开讲：

> 调用 `dist.all_gather(tensor_list, mine)` 时，`mine` 是本 rank 的贡献，`tensor_list` 是长度为 W 的输出列表。调用结束后，**每个 rank** 的 `tensor_list[j]` 都被原地写入 rank j 的贡献。因此契约是：**位置 j 上，所有 rank 的缓冲区形状必须一致**（都要装得下 rank j 的数据）。但不同位置之间、以及各 rank 自己贡献的 `mine`，形状可以互不相同。

用一段伪流程表示（W=3，三个 rank 分别持有 A、B、C，形状各异）：

```text
调用前:  rank0: mine=A(4,2)   rank1: mine=B(2,6)   rank2: mine=C(3,3)
三个 rank 都传入同一个形状模板列表 [ (4,2), (2,6), (3,3) ]
调用后:  每个 rank 的列表都是 [ A, B, C ]
         —— 位置 0 的缓冲区在任何 rank 上都是 (4,2)，位置 1 都是 (2,6)……
```

`Muon.step` 正是依赖这条契约，才能在**一次** `all_gather` 里同时交换 W 个形状各异的参数。

### 2.3 DDP 数据并行回顾，以及本讲的新视角

标准 DDP（DistributedDataParallel）数据并行中：每张卡持有**完整模型副本**，各自吃不同的小批量数据，反向传播后 DDP 用 `all_reduce` 把梯度平均——于是每张卡上的梯度逐位一致，各卡独立执行相同的优化器 `step()`，参数自然保持一致。

在这个范式下，优化器步骤是「人人重复做同样的计算」。而 Muon 的 `step()` 计算很贵：Newton-Schulz 迭代是 5 轮、每轮 3 次矩阵乘法（见 u2-l2）。于是分布式 Muon 换了一个视角——**优化器步骤本身也可以并行**：

- 梯度既然已经一致，任何一张卡算出的更新都一样；
- 那就让 rank \( r \) 只负责一部分参数的更新，其余参数「坐等」别人算完广播过来；
- 这就是「按参数分片」：W 张卡分摊 P 个参数的 Newton-Schulz 计算，每卡只需处理约 \( P/W \) 个。

注意一个重要前提：`Muon` 类**自己不做梯度同步**，它假设梯度已经一致（典型由 DDP 保证）。本讲综合实践会用「相同种子的数据」在 CPU 上模拟这个前提，从而让 Muon 内部的 `all_gather` 成为唯一通信，隔离出要验证的机制。

### 2.4 gloo 后端：没有 GPU 也能学分布式

`torch.distributed` 的集合通信由后端实现：GPU 上用 NCCL，CPU 上用 gloo。`dist.init_process_group("gloo")` 即可在普通笔记本/服务器上跑双进程分布式实验——这正是本讲所有实践的验证方式。

### 2.5 记号约定

后文统一使用：\( W \) = world_size，\( r \) = 本进程 rank，\( P \) = 参数个数（`len(params)`），\( \lceil \cdot \rceil \) 为向上取整。

## 3. 本讲源码地图

本讲只涉及一个源码文件 `muon.py`（约 290 行），但会对照它的几个区域，并借三个 git 提交还原设计演变：

| 位置 | 内容 | 在本讲中的角色 |
|---|---|---|
| [muon.py:L2](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L2) | `import torch.distributed as dist` | 通信 API 入口 |
| [muon.py:L44-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L64) | `Muon` 类定义与 docstring | 分布式纯 Muon 优化器 |
| [muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69) | 构造函数：列表断言 + 按尺寸排序 | 模块 4.2 精读 |
| [muon.py:L71-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L71-L96) | `Muon.step`：分片循环 + `all_gather` | 模块 4.3 / 4.4 精读 |
| [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | `SingleDeviceMuon.step`（u2-l5 已精读） | 对照组：去掉通信后的骨架 |
| [muon.py:L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169)、[muon.py:L193-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193-L208) | `MuonWithAuxAdam` 中 `use_muon` 组的排序与分片 | 同一机制的复用（u3-l2 承接） |
| [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223) | `use_muon=False` 的 adam 分支：零集合通信 | 一致性机制的对照 |
| git 提交 `f0d3600` | 修复补齐公式 | 模块 4.2 的历史佐证 |
| git 提交 `3b04428` → `5bc6993` | `p.grad is None` 从 `continue` 改为填零 | 模块 4.4 的历史佐证 |

## 4. 核心概念与源码讲解

### 4.1 模块：dist.get_world_size / get_rank 与集合通信入门

#### 4.1.1 概念说明

`world_size` 和 `rank` 是分布式训练里的「身份系统」。`Muon.step` 对这两个函数有四种不同的用法，各自服务一个目的：

1. **算补齐数量**：把参数列表补长到 W 的倍数（见模块 4.2）。
2. **算轮转步长**：`range(P)[::W]`，每 W 个参数为一「轮」（见模块 4.3）。
3. **选自己的参数**：`base_i + rank` 用工号决定本 rank 在每轮负责第几个参数。
4. **选自己的发送槽位**：`all_gather` 时本 rank 的贡献放在列表的第 `rank` 个位置。

注意一个容易踩的坑：`Muon` 类内部无条件调用 `dist.get_world_size()`（[muon.py:L81](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81)）。如果进程组尚未初始化（没调用过 `dist.init_process_group`）就直接用 `Muon`，会在 `step()` 时报「默认进程组未初始化」之类的错误。这正是单机场景应该用 `SingleDeviceMuon` 的原因（u1-l3 的选型矩阵）。

#### 4.1.2 核心流程

`all_gather` 的执行流程（W = 2 时）：

```text
rank 0: mine = params_pad[base_i + 0] ┐
rank 1: mine = params_pad[base_i + 1] ┘ 同时进入 all_gather（集合通信，全员到齐才返回）
返回后：
  两个 rank 的列表都是 [ rank0 的贡献, rank1 的贡献 ]
  且写入是「原地」的 —— 直接写进列表里那两个张量对象的存储区
```

「全员到齐才返回」意味着：**每个 rank 执行集合通信的次数和顺序必须完全一致**，否则先到的进程会一直等待——这就是后文 `p.grad` 填零设计的出发点（模块 4.4）。

#### 4.1.3 源码精读

`torch.distributed` 在文件顶部一次性导入，四个类共用：

[muon.py:L2](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L2) —— 导入分布式通信模块，别名 `dist`，本文件中所有集合通信都经它发出。

`Muon.step` 中四个 rank/world_size 用法集中出现在三行（先混个眼熟，逐行精读在模块 4.3）：

```python
params_pad = params + [torch.empty_like(params[-1])] * (dist.get_world_size() - len(params) % dist.get_world_size())
for base_i in range(len(params))[::dist.get_world_size()]:
    if base_i + dist.get_rank() < len(params):
        p = params[base_i + dist.get_rank()]
```

（[muon.py:L81-L84](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81-L84)）

- 第 81 行：`get_world_size()` 用来计算「补几个空张量」。
- 第 82 行：`[::W]` 让循环变量每轮跳 W 个参数，一轮处理一批。
- 第 83-84 行：`get_rank()` 决定本 rank 在这一轮认领哪个参数。

最后，[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94) 中 `get_rank()` 第四次出现：本 rank 的贡献槽位是列表切片的第 `rank` 个位置。

#### 4.1.4 代码实践

**实践：跑起来第一个 gloo 双进程通信。** 目标是亲眼确认 rank/world_size 的取值和 `all_gather` 的位置语义。

1. 实践目标：验证 `torchrun` 启动两个进程、`all_gather` 后人人持有按 rank 排列的完整列表。
2. 操作步骤：把下面的脚本存为 `hello_dist.py`（示例代码，建议放在仓库根目录），运行 `torchrun --nproc_per_node=2 hello_dist.py`：

```python
# hello_dist.py（示例代码）
import torch
import torch.distributed as dist

dist.init_process_group("gloo")   # torchrun 已通过环境变量注入 rank/world_size/地址
rank, W = dist.get_rank(), dist.get_world_size()
print(f"我是 rank {rank} / 共 {W} 个进程")

t = torch.full((2, 2), float(rank + 1))          # rank0 全是 1.0，rank1 全是 2.0
out = [torch.empty_like(t) for _ in range(W)]
dist.all_gather(out, t)
if rank == 0:
    print("gathered:", out)
dist.destroy_process_group()
```

3. 需要观察的现象：终端会打印两条「我是 rank …」；rank 0 打印的 `gathered` 列表应该是 `[全是 1.0 的矩阵, 全是 2.0 的矩阵]`——第 0 个位置装 rank 0 的贡献，第 1 个位置装 rank 1 的贡献。
4. 预期结果：两个进程各自报告 0/1 编号；`all_gather` 输出按 rank 位置排列（具体打印格式随 PyTorch 版本略有差异，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：设 \( W = 4 \)、\( P = 10 \)。`step()` 会执行几轮 `all_gather`？每轮列表里有几个张量？rank 2 认领哪些参数下标？

答案：轮数为 \( \lceil 10/4 \rceil = 3 \)（`range(10)[::4]` 产生 0、4、8）；每轮恰好 4 个张量（最后一轮不足处用填充张量补齐）；rank 2 认领下标 \{2, 6\}（10 已越界）。各 rank 认领数最多相差 1。

**练习 2**：`all_gather` 和 W 次 `broadcast` 有什么区别？

答案：`broadcast` 只有一个源，把一份张量发给大家；`all_gather` 是「每人贡献一个、人人得到完整列表」，语义上等于 W 次不同源的 `broadcast` 一次完成。分布式 Muon 每轮有 W 个不同 owner，用 `all_gather` 一条调用完成全部交换，代码也无需按源分支。

**练习 3**：为什么 `all_gather` 被称为「集合通信」而不是「点对点通信」？

答案：集合通信要求**所有**参与进程同时进入同一个调用，缺少任何一个都会挂起等待；点对点通信（`send`/`recv`）只涉及两个进程。这解释了为什么分布式 Muon 必须保证各 rank 的通信序列逐调用对齐。

### 4.2 模块：params 按尺寸排序与补齐

#### 4.2.1 概念说明

分片的前提是「所有 rank 对参数列表的顺序达成一致」，且列表长度能被 W 整除。这两件事都在进入 `step()` 之前解决：

1. **排序**：构造函数把参数按形状降序排序。排序的**主要收益是负载均衡**——把计算量大的矩阵轮流分给不同 rank（最大的给 rank 0、次大的给 rank 1……），避免某张卡独占所有大矩阵。它同时给出一个只由形状决定、与用户传参顺序无关的规范顺序；但跨 rank 顺序一致的真正保证，来自「各 rank 用同一份模型代码生成参数列表」。
2. **补齐**：若 \( P \bmod W \neq 0 \)，最后一轮凑不齐 W 个参数，就在列表尾部补「空张量」凑数。填充张量只是让 `all_gather` 的列表长度凑满 W 的占位符，**内容是未初始化的内存垃圾，从不被读取**。

为什么必须凑满 W？回到 4.1 的契约：`all_gather` 的输出列表长度必须等于 `world_size`，且每个 rank 在同一位置提供的形状要一致——所以每轮的切片 `params_pad[base_i : base_i+W]` 必须恒有 W 个元素。

#### 4.2.2 核心流程

补齐数量的推导。设 \( P = \) 参数个数，需要 \( k \) 个填充张量使得 \( P + k \equiv 0 \pmod W \)：

\[ k = W - (P \bmod W) \quad (P \bmod W \neq 0) \]

源码直接写死 \( k = W - P \bmod W \)。当 \( P \bmod W = 0 \) 时它算出 \( k = W \)——多补了 W 个，但循环只遍历到下标 \( P-1 \)（见模块 4.3），填充张量根本不会被触碰，只是多分配几个空张量，**无害但冗余**。

一个细节：`[torch.empty_like(params[-1])] * k` 按照Python 语义生成的是 **k 个指向同一张量对象的引用**。由于没人读取填充张量，实践中无碍；但如果你要在实验里检查填充槽位的内容，要知道它们可能是同一个对象。

排序键也有个值得注意的细节：`x.size()` 返回 `torch.Size`（元组子类），比较是**字典序**而非元素个数。例如形状 (512, 512)（约 26 万元素）会排在 (256, 4096)（约 100 万元素）**前面**。所以这个排序是「按形状字典序从大到小」的负载均衡**启发式**，并不严格等价于按计算量排序。

#### 4.2.3 源码精读

构造函数 [muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69)：

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    assert isinstance(params, list) and len(params) >= 1 and isinstance(params[0], torch.nn.Parameter)
    params = sorted(params, key=lambda x: x.size(), reverse=True)
    super().__init__(params, defaults)
```

- [muon.py:L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67) —— 三重断言：`params` 必须是**显式列表**、非空、首元素是 `nn.Parameter`。原因：下一行要 `sorted(...)`，第 81 行要 `params[-1]` 和下标切片——生成器或 `model.parameters()` 直接传入都会让后续代码崩溃，断言把错误前移到构造期（这是 u2-l5 提过的「分布式版必须显式列表」的出处）。
- [muon.py:L68](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L68) —— 按形状字典序**降序**排序。降序 + 轮转分配 ≈ 经典的「先大后小轮流发牌」负载均衡；同时保证 `params[-1]` 是字典序最小的参数，填充张量 `empty_like(params[-1])` 取到了列表里最小的形状。
- [muon.py:L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L69) —— 把排好序的列表交给 `torch.optim.Optimizer` 基类，建立 `param_groups` 与 `state`（u2-l4 的知识）。

补齐发生在 `step()` 的第一行（[muon.py:L81](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81)）：

```python
params_pad = params + [torch.empty_like(params[-1])] * (dist.get_world_size() - len(params) % dist.get_world_size())
```

这行做三件事：取出本组的参数列表；计算 \( k = W - P \bmod W \)；用 k 个「形状同 `params[-1]`、内容未初始化」的空张量拼出 `params_pad`。`MuonWithAuxAdam` 在 `use_muon=True` 的组里原样复用这两招：排序在 [muon.py:L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169)，补齐在 [muon.py:L195](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L195)。

**一段真实的历史**：这个补齐公式曾经写错过。提交 `f0d3600`（2025-06-10，"fixed params padding in distributed optimizers"）把它从

```python
[torch.empty_like(params[-1])] * (len(params) % dist.get_world_size())      # 修复前：补 P % W 个
```

改成现在的 `W - P % W`。旧公式在 \( W = 2 \) 时碰巧总对（\( P \bmod 2 = 1 \) 时两者都等于 1），但在 \( W \geq 3 \) 且 \( P \bmod W \geq 2 \) 时补齐不足，最后一轮切片 `params_pad[base_i : base_i+W]` 长度不足 W，`all_gather` 直接异常。可用 \( P = 5, W = 4 \) 验证：旧公式补 \( 5 \bmod 4 = 1 \) 个，总长 6，不是 4 的倍数。

#### 4.2.4 代码实践

**实践：用纯 Python 演练排序与补齐数学**（不需要 PyTorch、不需要多进程，几秒钟出结果）。

1. 实践目标：确信修复后的公式恒能整除，并亲眼看到旧公式在哪些组合下崩坏。
2. 操作步骤：运行下面的示例代码：

```python
# pad_math.py（示例代码，纯 Python）
def pads_old(P, W): return P % W          # f0d3600 修复前
def pads_new(P, W): return W - P % W      # f0d3600 修复后

print(f"{'P':>3} {'W':>3} {'旧补':>4} {'新补':>4}  旧公式是否崩溃")
for W in range(2, 9):
    for P in range(1, 17):
        old, new = pads_old(P, W), pads_new(P, W)
        assert (P + new) % W == 0                     # 新公式恒成立
        broken = (P + old) % W != 0                   # 旧公式补不齐 → 最后一轮切片长度不足 W
        if broken:
            print(f"{P:>3} {W:>3} {old:>4} {new:>4}  ← {P}+{old}={P+old} 不是 {W} 的倍数")
```

3. 需要观察的现象：输出一张崩溃清单，例如 `P=2, W=3`（2+2=4 不是 3 的倍数）、`P=5, W=4`（5+1=6 不是 4 的倍数）等行；同时 `assert` 从不触发。
4. 预期结果：新公式对全部组合满足整除；旧公式只在 \( P \bmod W \in \{0, 1, W/2\} \) 的组合上侥幸存活，其余组合都出现在崩溃清单里。这些输出是纯算术，可以直接推算核对。

#### 4.2.5 小练习与答案

**练习 1**：\( P = 7 \)、\( W = 4 \)。需要补几个张量？`params_pad` 总长多少？最后一轮（`base_i = 4`）的切片里有哪些张量？

答案：补 \( 4 - 7 \bmod 4 = 1 \) 个；总长 8；切片为 `[params[4], params[5], params[6], 填充]`，其中填充张量占据位置 3，由没有认领任务的 rank 3 贡献。

**练习 2**：形状 (256, 4096) 和 (512, 512) 谁排在前面？谁的元素多？这说明排序键的什么性质？

答案：(512, 512) 字典序更大（512 > 256），排前面；但 256×4096 ≈ 105 万元素，远多于 512×512 ≈ 26 万。说明排序键是形状元组的字典序，只是负载均衡的近似启发式，不严格按计算量（元素数）排序。

**练习 3**：为什么填充张量用 `torch.empty_like(params[-1])` 而不是 `zeros_like`？不初始化内容安全吗？

答案：安全。填充张量参与 `all_gather` 只是为了凑满列表长度与位置形状，它们接收到的「垃圾数据」只被写进其他 rank 的填充槽位，而填充槽位此后从不被读取。用 `empty_like` 省掉无意义的清零开销；顺带一提，排完序后 `params[-1]` 是字典序最小的参数，填充张量的形状代价也最小。

### 4.3 模块：Muon.step 分片循环

#### 4.3.1 概念说明

分片循环要解决的问题：把 P 个参数的更新计算公平地摊到 W 个 rank 上，并且每一轮都能用一次 `all_gather` 完成结果交换。方案是**按参数下标轮转（round-robin）认领**：

\[ \text{rank } r \text{ 认领下标集合} = \{ r,\ r+W,\ r+2W,\ \dots \} \cap [0, P) \]

即每个 rank 只计算下标与自己的工号同余的参数，认领数量为 \( \lceil (P - r)/W \rceil \) 个，各 rank 之间至多差 1 个。循环变量 `base_i`（取值 0、W、2W…）标记「轮」的起点，每轮恰好发生一次 `all_gather`，共 \( \lceil P/W \rceil \) 次。

为什么按参数分片而不是像 DDP 那样按数据分片？因为梯度已经一致（DDP 已完成 `all_reduce`），重复计算更新是纯浪费；而 Newton-Schulz 的开销与参数形状成正比，把参数摊开算、再把结果广播回来，能让优化器步骤的时间近似除以 W。

#### 4.3.2 核心流程

`step()` 对一个参数组的完整流程（以综合实践中的 8 个权重、\( W = 2 \) 为例；形状按字典序降序排列后为 (256,128), (160,96), (128,256), (128,64), (96,128), (80,160), (48,80), (32,48)）：

```text
构造 params_pad：8 % 2 == 0 → 公式补 2 个填充张量（循环用不到，无害）

base_i = 0（第 1 轮）:
    rank 0 计算 params[0]=(256,128) 的更新并写回自己的副本
    rank 1 计算 params[1]=(160,96)  的更新并写回自己的副本
    all_gather → 两个 rank 的 params[0]、params[1] 都变成 owner 的最新值
base_i = 2（第 2 轮）: 同上，处理 params[2]、params[3]
……
base_i = 6（第 4 轮）: 处理 params[6]、params[7]，最后一次 all_gather
```

每轮内部的伪代码：

```text
for base_i in 0, W, 2W, ...:
    j = base_i + rank                      # 我的认领下标
    if j < P:                              # 最后一轮可能有 rank 认领不到
        p = params[j]
        （本行开始的优化器骨架与 SingleDeviceMuon 完全一致，见 u2-l5）
        grad 为 None → 填零；惰性建 momentum_buffer；算 muon_update；两行原地更新 p
    all_gather(params_pad[base_i : base_i+W], params_pad[base_i + rank])   # 无条件执行
```

注意 `all_gather` 在 `if j < P` 的**外面**：即使本 rank 这一轮没有认领到参数，也必须参加集合通信，把别人的结果收回来。

#### 4.3.3 源码精读

逐段读 [muon.py:L79-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L94)：

```python
for group in self.param_groups:
    params = group["params"]
```

（[muon.py:L79-L80](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L79-L80)）遍历参数组；`params` 已在构造期排好序。

```python
params_pad = params + [torch.empty_like(params[-1])] * (dist.get_world_size() - len(params) % dist.get_world_size())
for base_i in range(len(params))[::dist.get_world_size()]:
```

（[muon.py:L81-L82](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81-L82)）补齐（模块 4.2），然后按步长 W 遍历轮起点。注意循环上界是 `len(params)` 而不是 `len(params_pad)`——这就是「多补的 W 个填充张量永远不被使用」的原因。

```python
    if base_i + dist.get_rank() < len(params):
        p = params[base_i + dist.get_rank()]
```

（[muon.py:L83-L84](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L83-L84)）越界保护：最后一轮参数不够分时，工号靠后的 rank 认领不到任务，跳过计算分支（但下面的 `all_gather` 照常参加）。

```python
        if p.grad is None:
            # continue
            p.grad = torch.zeros_like(p)  # Force synchronization
        state = self.state[p]
        if len(state) == 0:
            state["momentum_buffer"] = torch.zeros_like(p)
        update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
        p.mul_(1 - group["lr"] * group["weight_decay"])
        p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

（[muon.py:L85-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L85-L93)）这段与 [muon.py:L117-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L125) 的 `SingleDeviceMuon` 完全一致（u2-l5 已逐行精读）：填零梯度、惰性建动量缓冲、算 `muon_update`、解耦权重衰减 + 施加更新两行原地操作。唯一的新知识点在**状态的分布**上——见下面的关键观察。

```python
    dist.all_gather(params_pad[base_i:base_i + dist.get_world_size()], params_pad[base_i + dist.get_rank()])
```

（[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94)）每轮一次的全员同步，机制细节在模块 4.4 展开。

**关键观察——优化器状态也被分片了**：`self.state[p]` 的初始化只发生在认领到 `p` 的 rank 上。也就是说 rank \( r \) 的 `state` 字典里**只有**下标与 \( r \) 同余的那些参数的 `momentum_buffer`。动量缓冲的显存/内存占用随 W 线性下降，这是「按参数分片」附带的内存红利。代价是 `optimizer.state_dict()` 在单卡上不再完整——恢复训练时需要每个 rank 各存各的。

**MuonWithAuxAdam 的复用**：同样的分片循环原样出现在 [muon.py:L193-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193-L208)（`use_muon=True` 分支），逐行对照会发现只有缩进和组键的差别；而紧随其后的 adam 分支（[muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223)）一次集合通信都没有——为什么它不需要？答案在模块 4.4。

#### 4.3.4 代码实践

**实践：单进程模拟轮转分配表，检查负载均衡**（不需要分布式环境）。

1. 实践目标：不跑 torchrun 也能算出「哪个 rank 拿到哪些参数」，并量化排序的均衡效果。
2. 操作步骤：运行下面的示例代码（形状取自综合实践的 8 层 MLP）：

```python
# assign_table.py（示例代码）
shapes = [(128, 64), (256, 128), (128, 256), (96, 128),
          (160, 96), (80, 160), (48, 80), (32, 48)]          # 模型顺序
order = sorted(shapes, key=lambda s: s, reverse=True)         # 构造函数的排序（字典序降序）
P, W = len(order), 2

for r in range(W):
    mine = [base_i + r for base_i in range(0, P, W) if base_i + r < P]
    load = sum(order[i][0] * order[i][1] for i in mine)
    print(f"rank {r}: 下标 {mine}，元素量合计 {load}")
```

3. 需要观察的现象：rank 0 得到下标 [0, 2, 4, 6]（形状 (256,128)、(128,256)、(96,128)、(48,80)，合计 81664 个元素），rank 1 得到 [1, 3, 5, 7]（合计 37888 个元素）。
4. 预期结果：两侧各有 4 个参数（数量均衡），但元素量差距约 2.2 倍——印证 4.2 的结论：字典序排序是近似启发式，本例的大矩阵恰好都落在偶数位。对真实 transformer（大量近方形权重）这种错位会小得多。读者可以把 `W` 改成 3、4，观察均衡度变化（纯算术，可手算核对）。

#### 4.3.5 小练习与答案

**练习 1**：构造函数为什么断言 `isinstance(params, list)`，而 `SingleDeviceMuon` 不需要？

答案：`Muon.__init__` 随后要对 `params` 做 `sorted(...)`、取 `params[-1]`，`step()` 还要做下标切片——这些操作要求可随机访问的显式列表。`model.parameters()` 返回生成器，直接传入会在构造期就出问题；断言把错误前移到最易排查的位置。`SingleDeviceMuon` 只是 `for p in group["params"]` 迭代，生成器经父类处理后也能工作，故无此断言。

**练习 2**：\( W = 8 \)、\( P = 100 \)。每个 rank 认领多少个参数？一共发生多少次 `all_gather`？

答案：认领数为 \( \lceil (100 - r)/8 \rceil \)，rank 0~3 各 13 个、rank 4~7 各 12 个；`all_gather` 共 \( \lceil 100/8 \rceil = 13 \) 次（100 % 8 = 4，最后一轮有 4 个填充槽位）。

**练习 3**：对比 [muon.py:L117-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L125)（单机版）与 [muon.py:L85-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L85-L93)（分布式版）的逐参数处理，算法层面有区别吗？

答案：没有。梯度填零、动量惰性初始化、`muon_update` 调用、两行原地更新完全一致。区别全在编排：分布式版每步只处理 \( 1/W \) 的参数（下标同余），再靠 `all_gather` 补齐其余参数。算法单卡/多卡完全一致，是多卡正确性的基石。

### 4.4 模块：all_gather 通信原语与跨 rank 一致性

#### 4.4.1 概念说明

剩下的问题是：一行 `all_gather` 凭什么能让所有 rank 的参数保持逐位一致？答案由三块拼成：

1. **切片别名的原地写回**：`params_pad[base_i:base_i+W]` 切片产生新列表，但列表元素仍是（指向）真正的 `Parameter` 张量对象。`all_gather` 把结果**原地**写进列表每个位置的张量存储区——对非填充槽位而言，这等于把 owner 更新后的参数直接写进本 rank 模型的权重里。
2. **梯度一致 + 计算确定性**：DDP 已保证各 rank 梯度逐位相同；`muon_update` 是确定性函数（无随机数、同输入同输出）。于是 owner 算出的更新与其他任何 rank「假如自己算」的结果完全相同——广播 owner 的结果没有信息损失。
3. **通信序列对齐**：每个 rank 每轮都执行恰好一次 `all_gather`，从不因「没认领到参数」或「梯度为 None」而跳过。序列错位会导致集合通信互相错配（一个 rank 在等第 k 轮、另一个已经进到第 k+1 轮），轻则挂死、重则静默错乱。

三者合起来构成一个闭环：梯度一致 → 确定性更新 → owner 结果即全体结果 → `all_gather` 原地写回 → 参数逐位一致 → 下一步梯度继续一致。

#### 4.4.2 核心流程

一轮 `all_gather` 的数据流（W = 2，round 处理 params[base_i]、params[base_i+1]）：

```text
           计算阶段（各算各的，无通信）
rank 0: 更新自己的 params[base_i+0]        rank 1: 更新自己的 params[base_i+1]
                    └──────────┬──────────────┘
              all_gather（每 rank 贡献自己刚更新的那个张量）
                              ↓
rank 0 的 params[base_i+1] ← rank 1 的最新值    （跨 rank 写回）
rank 1 的 params[base_i+0] ← rank 0 的最新值    （跨 rank 写回）
各自「自己 → 自己」的槽位等于自写自，无变化
```

`p.grad` 填零在闭环里的角色：

```text
若某 rank 的 p.grad 为 None 且选择 continue（旧版行为，提交 3b04428）:
    该 rank 跳过本轮剩余语句 → 包括那行 all_gather
    其他 rank 仍在等待本轮 all_gather → 通信序列错位 → 挂死或错配
填零（修复，提交 5bc6993 "Force synchronization"）:
    梯度缺失被视为 0 梯度，计算照做、all_gather 照参加 → 序列恒对齐
```

顺带解释 adam 分支为什么零通信也能一致：`adam_update` 是逐元素的确定性计算，在梯度一致的前提下各 rank 冗余地算出**完全相同**的更新，参数自然保持一致，一步通信都不用。这是一个干净的工程取舍：**矩阵级的 Newton-Schulz 很贵，分摊计算 + 一次广播划算；逐元素的 Adam 更新很便宜，冗余计算反而比通信划算。** 代价则是每个 rank 都要存全量 adam 状态（与 muon 状态的分片形成对照）。

通信量角度：muon 分支每步的总通信量约为「每个参数广播一遍」（每个参数在一次 `all_gather` 中被发给所有 rank 一次），加上各轮元数据；换取的是计算时间近似除以 W。

#### 4.4.3 源码精读

核心一行 [muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94)：

```python
dist.all_gather(params_pad[base_i:base_i + dist.get_world_size()], params_pad[base_i + dist.get_rank()])
```

逐个成分拆开：

- `params_pad[base_i : base_i+W]` —— 输出列表：本轮这 W 个张量（对未补齐的轮，全是真参数；最后一轮可能混入填充）。切片是**新列表、旧对象**：写进列表元素就是写进参数本体。
- `params_pad[base_i + rank]` —— 本 rank 的贡献：rank r 拿出的正是它刚更新完的那个参数；认领不到任务的 rank 拿出的是填充张量（内容是垃圾，但接收方也只是把它写进从不读取的填充槽位）。
- 位置形状契约的满足：位置 j 的张量是 `params[base_i+j]` 的副本，其形状由**相同的模型结构**保证在所有 rank 上一致；至于「各 rank 贡献的张量形状可以不同」这一点，正是 `all_gather` 按位置对齐语义所允许的（见 4.1.1、4.4.4 的最小验证实验）。

梯度填零 [muon.py:L85-L87](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L85-L87)：

```python
if p.grad is None:
    # continue
    p.grad = torch.zeros_like(p)  # Force synchronization
```

被注释掉的 `# continue` 是历史化石——提交 `3b04428` 曾把 `p.grad is None` 改成 `continue`（跳过该参数），随后 `5bc6993` 发现这在分布式下会**跳过本轮的 `all_gather`**（当时该通信行在 `continue` 的作用范围内），造成各 rank 集合通信序列错位，于是改为填零并留下这行注释。单机版 [muon.py:L117-L119](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L117-L119) 有同样的三行——虽然单机没有死锁风险，但保持两版语义一致（「梯度缺失 ≠ 冻结参数」，动量照常衰减）。

同一机制在 `MuonWithAuxAdam` 中的镜像：muon 分支的 `all_gather` 在 [muon.py:L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L208)；adam 分支 [muon.py:L209-L223](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L209-L223) 从头到尾没有 `dist.` 调用，靠确定性与梯度一致「免费」同步。

#### 4.4.4 代码实践

**实践：最小验证「按位置对齐」契约**——两个 rank 贡献不同形状的张量，`all_gather` 应当照常工作。

1. 实践目标：确认 `all_gather` 允许各 rank 贡献形状互不相同的张量，只要每个位置上各 rank 的缓冲区形状一致——这是 `Muon.step` 每轮所依赖的语义。
2. 操作步骤：保存为 `hetero_gather.py`，运行 `torchrun --nproc_per_node=2 hetero_gather.py`（示例代码）：

```python
# hetero_gather.py（示例代码）
import torch
import torch.distributed as dist

dist.init_process_group("gloo")
rank, W = dist.get_rank(), dist.get_world_size()

# 两个 rank 贡献的形状不同：rank0 给 (4,2)，rank1 给 (2,6)
mine = torch.full((4, 2), float(rank + 1)) if rank == 0 else torch.full((2, 6), float(rank + 1))
out = [torch.empty(4, 2), torch.empty(2, 6)]   # 位置 0 对应 rank0 的形状，位置 1 对应 rank1 的
dist.all_gather(out, mine)
print(f"rank {rank}: shapes={[tuple(t.shape) for t in out]}, "
      f"首元素={[t.flatten()[0].item() for t in out]}")
dist.destroy_process_group()
```

3. 需要观察的现象：两行打印都应显示 `shapes=[(4,2), (2,6)]`、`首元素=[1.0, 2.0]`——即每个 rank 都拿到了对方的张量，且形状按位置对齐。
4. 预期结果：如上；gloo 后端下应正常完成（具体打印格式随版本略有差异，待本地验证）。如果实验失败请记录报错信息——它说明你手上的 PyTorch 版本对形状校验更严格，那么 `Muon` 本身在该版本上也需要重新评估。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `p.grad is None` 时必须填零而不能 `continue`？用「集合通信序列」的语言回答。

答案：`continue` 会连同本轮的 `all_gather` 一起跳过。如果梯度缺失只发生在部分 rank 上，其余 rank 仍会进入这次 `all_gather` 等待全员到齐——各 rank 的通信次数/顺序不再对齐，轻则挂死，重则前后两轮的通信互相错配（形状不符）。填零让「梯度缺失」退化为「零梯度」，所有 rank 走完全相同的代码路径，通信序列恒对齐（历史提交 `3b04428` 引入 continue、`5bc6993` 修复）。

**练习 2**：`all_gather` 之后，为什么能断言所有 rank 的参数**逐位**一致，而不是「数值接近」？

答案：一致的梯度（DDP `all_reduce` 后逐位相同）+ 确定性的 `muon_update`（无随机性，浮点运算顺序固定）→ owner 算出的更新与其他 rank 会算出的结果逐位相同；`all_gather` 把 owner 的字节原样复制到每个 rank 的对应 Parameter。全程没有求平均或规约，不存在浮点误差引入。

**练习 3**：`MuonWithAuxAdam` 的 adam 分支一次集合通信都不做，为什么参数也不会跑散？代价是什么？

答案：梯度一致 + `adam_update` 是确定性逐元素运算，各 rank 冗余计算得到完全相同的更新。代价是每个 rank 都要完整保存全部 adam 参数的 `exp_avg`、`exp_avg_sq` 状态并重复全部计算——对便宜的逐元素运算，这个冗余比通信更划算，与 muon 分支「贵的矩阵计算分摊 + 广播」形成互补。

## 5. 综合实践

**任务：用 `torchrun` + gloo 在 CPU 上完整验证分布式 `Muon` 的一致性，再亲手触发一次排序缺失导致的事故。**

设计思路：不引入 DDP，而是让两个 rank 用**相同种子**生成数据与初始权重，从而梯度天然逐位一致（模拟 DDP `all_reduce` 的效果）。这样 `Muon.step` 内部的 `all_gather` 就是整个系统唯一的通信，实验结论干净地指向被测机制本身。

1. 实践目标：
   - 8 个不同形状的矩阵参数交给 `Muon`，多步训练后验证两个 rank 的参数**逐位一致**；
   - 观察填充张量何时真正参与 `all_gather`；
   - 通过「跳过排序 + 各 rank 顺序不一致」复现位置形状不匹配的事故，理解排序的必要性。
2. 操作步骤：

把下面的脚本保存为 `dist_muon_lab.py`，放在**仓库根目录**（与 `muon.py` 同级，`from muon import Muon` 可直接工作；或按 u1-l2 先 `pip install git+https://github.com/KellerJordan/Muon`）（示例代码）：

```python
# dist_muon_lab.py（示例代码）
# 运行：torchrun --nproc_per_node=2 dist_muon_lab.py
# 可选：torchrun --nproc_per_node=3 dist_muon_lab.py   （观察填充张量参与最后一轮）
# 事故复现：torchrun --nproc_per_node=2 dist_muon_lab.py --break-order
import sys
import torch
import torch.distributed as dist
import torch.nn as nn
from muon import Muon

dist.init_process_group("gloo")
rank = dist.get_rank()

# 1) 相同种子 → 两个 rank 的初始权重逐位相同
torch.manual_seed(0)
dims = [64, 128, 256, 128, 96, 160, 80, 48, 32]        # 8 层 → 8 个矩阵权重
model = nn.Sequential(*[nn.Linear(a, b, bias=False) for a, b in zip(dims, dims[1:])])
hidden = [p for p in model.parameters() if p.ndim >= 2]
assert len(hidden) == 8

torch.manual_seed(7)                                    # 固定教师映射，制造可学习的目标
T = torch.randn(dims[0], dims[-1])

def build_optimizer(break_order):
    if not break_order:
        return Muon(hidden)                             # 正常路径：构造函数内部排序
    class UnsortedMuon(Muon):
        """实验用：照抄父类构造，但跳过排序，且让 rank 1 反转列表。"""
        def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
            defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
            if dist.get_rank() == 1:
                params = params[::-1]
            torch.optim.Optimizer.__init__(self, params, defaults)
    return UnsortedMuon(hidden)

break_order = "--break-order" in sys.argv
opt = build_optimizer(break_order)
if break_order and rank == 0:
    print("break-order 模式：预期第一次 step() 即因各 rank 位置形状不一致而报错/挂起")

# 2) 相同种子生成数据 → 每步梯度逐位一致（模拟 DDP 梯度 all_reduce 的效果）
for step in range(50):
    g = torch.Generator().manual_seed(1000 + step)
    x = torch.randn(128, dims[0], generator=g)
    y = x @ T + 0.1 * torch.randn(128, dims[-1], generator=g)
    loss = torch.nn.functional.mse_loss(model(x), y)
    opt.zero_grad()
    loss.backward()
    opt.step()                                          # 分片计算 + all_gather 同步都在这里
    if step % 10 == 0 and rank == 0:
        print(f"step {step:3d}  loss {loss.item():.4f}")

# 3) 一致性校验：广播 rank 0 的参数作参照，比较逐位差异后归约最大值
max_diff = torch.zeros(())
with torch.no_grad():
    for p in model.parameters():
        ref = p.detach().clone()
        dist.broadcast(ref, src=0)
        max_diff = torch.maximum(max_diff, (p.detach() - ref).abs().max())
dist.all_reduce(max_diff, op=dist.ReduceOp.MAX)
if rank == 0:
    print("各 rank 参数最大差异 =", max_diff.item())     # 预期 0.0
dist.destroy_process_group()
```

3. 需要观察的现象：
   - 正常模式：loss 明显持续下降（本实验中教师映射 `x @ T` 每个元素的方差约为 64，初始 loss 量级在几十；噪声地板约 0.01，具体数值待本地验证）；结尾打印「各 rank 参数最大差异 = 0.0」；
   - `--nproc_per_node=3`：8 % 3 = 2，补 1 个填充张量，最后一轮 `all_gather` 真正混入填充槽位，脚本仍应正常完成且差异为 0.0；
   - `--break-order` 模式：进程在第一次 `opt.step()` 处报错退出或挂起——因为 rank 0 位置 0 提供的是 (128,64)，rank 1 位置 0 提供的是反转后的 (32,48)，位置形状契约被破坏。
4. 预期结果：正常模式验证「梯度一致 + 分片计算 + all_gather 写回 ⇒ 参数逐位一致」的完整闭环；`--break-order` 展示排序缺失且各 rank 顺序不一致时的通信错配。报错的具体文本随 PyTorch 版本与后端而异（待本地验证）。
5. 一个诚实的补充说明：如果两个 rank 用**相同的**乱序（例如都反转、且形状互不相同），训练仍会正确运行——排序被跳过本身不必然出错，它破坏的是「各 rank 对第 i 个参数是谁」的共识；此外按形状字典序排序主要是负载均衡启发式。真正不可破坏的约束是：**所有 rank 的参数列表顺序必须一致**（排序以形状为键，天然提供了与传参顺序无关的规范顺序）。

## 6. 本讲小结

- 分布式 `Muon` 的核心思想是**按参数分片**：梯度已被 DDP 弄一致，让 rank \( r \) 只计算下标 \( \equiv r \pmod W \) 的参数（认领 \( \lceil (P-r)/W \rceil \) 个），把昂贵的 Newton-Schulz 计算摊薄到 W 张卡。
- 构造函数（[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69)）要求显式列表并按形状字典序降序排序：排序主要是负载均衡启发式；补齐公式 \( W - P \bmod W \)（曾写错为 \( P \bmod W \)，提交 `f0d3600` 修复）保证每轮 `all_gather` 的列表恒有 W 个槽位。
- `all_gather`（[muon.py:L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L94)）按位置对齐：位置 j 收 rank j 的贡献并**原地写回**，切片里的元素正是各 rank 的 `Parameter` 本体，一行代码完成「W 个 owner 各自广播」。
- 一致性闭环 = 梯度一致（DDP）+ 计算确定性（`muon_update`）+ 通信序列对齐（`p.grad` 填零而非跳过，提交 `3b04428`→`5bc6993` 的教训）+ 原地写回 ⇒ 参数逐位一致；优化器动量状态也随之 W 路分片。
- `MuonWithAuxAdam` 原样复用了排序（[muon.py:L169](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L169)）与分片循环（[muon.py:L193-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L193-L208)），而 adam 分支零通信：贵的矩阵计算分摊+广播、便宜的逐元素计算冗余执行，是同一定义下的两种工程取舍。
- 验证分布式代码不需要 GPU：`torchrun --nproc_per_node=N` + `dist.init_process_group("gloo")` 即可在 CPU 上完成全部实验。

## 7. 下一步学习建议

- **下一讲（u3-l4）**：把视角从「机制」转回「使用」——按 README 的替换配方，把一个 AdamW 训练脚本改造成 `MuonWithAuxAdam` 单优化器模式并调参，本讲的分布式知识会让你对「为什么 muon 组和 adam 组的学习率量纲不同、为什么分组要靠用户手动划分」有更深的体会。
- **推荐阅读的源码**：重读 [muon.py:L138-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L225)，对照本讲确认你能独立指出 `MuonWithAuxAdam` 中排序、补齐、`all_gather` 的位置；再用 `git log -p muon.py` 复看 `f0d3600`、`3b04428`、`5bc6993` 三个提交，体会分布式代码「一个小改动就是一次线上事故」的工程敏感性。
- **延伸方向（对接 u4）**：阅读 PyTorch 文档中 `torch.distributed` 的 `all_gather`、`all_reduce`、`broadcast` 条目，理解不同后端（gloo/NCCL）对张量形状与设备的要求；学有余力可以思考：如果把参数分片改成「按元素分片」（把大矩阵切成块分给各 rank），通信模式会变成什么样？这正是许多分布式优化器（如 DeepSpeed ZeRO）与 Muon 思路的分岔点。
