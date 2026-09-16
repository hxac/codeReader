# u3-l1 MoonEPCommPlan：规划输出的数据结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `MoonEPCommPlan` 中 7 个张量的**形状、dtype 与语义**，以及 6 个标量配置的含义。
2. 讲清楚每个字段的**生产者与消费者**：planning 内核写哪些、dispatch 内核写哪些、后续哪些 API 读哪些。
3. 理解 plan 为什么被设计成 `frozen` 的**不可变快照**、`clone()` 的真实用途，以及 `cu_seqlens` 为什么**不在** plan 里。
4. 掌握 `allocate_planning_outputs` 的 `_round4` 过量分配技巧（防向量化越界写）与 `_check_dedup_encoding_bounds` 的六条边界检查各自的动机。

本讲是第三单元「在线规划器」的第一讲，只讲**数据结构**本身；均衡算法（z 矩阵）、槽位映射与二分查找分别在 u3-l2 ~ u3-l4 展开，去重编码在 u3-l5 展开。

## 2. 前置知识

### 2.1 Python `dataclass` 的 `frozen` 与 `slots`

`@dataclass` 是 Python 自动生成 `__init__`/`__repr__` 等方法的语法糖。两个参数与我们的设计直接相关：

- **`frozen=True`**：实例创建后**不允许重新赋值任何字段**（`plan.dst = x` 会抛 `FrozenInstanceError`）。注意它冻结的是「字段引用」，不是张量内容——内核仍然可以 in-place 修改 `plan.dst` 里的数值。
- **`slots=True`**：实例不携带 `__dict__`，省内存、属性访问更快，同时天然禁止动态添加字段。

### 2.2 PyTorch 张量的三个基本属性

- **dtype**：MoonEP 的 plan 全部张量统一用 `torch.int32`——它们是给 GPU 内核读的「整数地址簿」，不是数据。
- **`is_contiguous()`**：元素在内存中按行优先紧凑排列。内核按裸指针 + 偏移访问，任何非连续视图都会让地址计算失效，所以 plan 的每个张量都断言连续。
- **`view(...)` 与切片**：`t[:n]` 与父张量**共享存储**，`view(r, c)` 只改变解释形状不搬数据。`allocate_planning_outputs` 大量使用「先分配大张量再切片/view」的手法。

### 2.3 16 字节向量化写与 4 元素对齐

GPU 上连续写 4 个 int32（16 字节）可以用一条 128 位 store 指令完成，事务数变为 1/4。但代价是：即使循环边界只覆盖 `n` 个元素，硬件也可能按 4 个一组**整组写入**。因此若缓冲恰好分配 `n` 个元素而 `n % 4 != 0`，尾部那一次向量写就会**越界 1~3 个元素**。MoonEP 的解法是「多分配到 4 的倍数，再切片回逻辑形状」。这个动机写在源码注释里（[moonep/planning.py:L1217-L1219](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1217-L1219)）。

### 2.4 承接前两讲

- u2-l1 已建立符号系统：\(N = S \times K\) 是每 rank 发出的 top-k 拷贝总数；\(N_{vS} = S K + (p_{pad}-1)\cdot 2E/R\)（\(p_{pad}\) 为 token_padding）是接收槽位数；B 默认 \(E/R\)。
- u2-l4 讲过 meta_buf 的对称内存布局：规划内核在 rank0 的 PLAN 区算出结果，经 NVSwitch 组播发布，再由各 rank 拷回本地。本讲的 plan 张量就是「拷回本地后」的最终落地形态。

## 3. 本讲源码地图

| 文件 | 本讲关注的段落 | 作用 |
|---|---|---|
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L31-L88) | L31-L88 `MoonEPCommPlan` 数据类 | plan 的字段、断言与 clone |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1198-L1316) | L1198-L1316 分配与检查 | `_round4`、`allocate_planning_outputs`、两个 `_check_*`、`launch_planning` 入口 |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L834-L1130) | 内核中的写入点 | 各字段在 planning 内核里被写出的位置（本讲只看「写什么」，不看「怎么算」） |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L784-L794) | L784-L794、L605-L615 | `dispatch` 中的分配调用点；`_plan_runtime_tensors` |
| [moonep/constants.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L7-L17) | L7-L17 | `KIDX_BITS = 7` 等位宽常量，决定边界检查的上限 |
| [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L540-L681) | L540-L681 | dedup builder 写入去重三件套的位置 |
| [tests/kernel_test_utils.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L153-L161) | L153-L161 | `DEDUP_PLAN_FIELDS`：测试视角的 dedup 字段契约 |

## 4. 核心概念与源码讲解

### 4.1 MoonEPCommPlan 数据类

#### 4.1.1 概念说明

一次 MoE 前向的 dispatch 需要「通信计划」：每个 token 拷贝发到哪个 rank 的哪个槽位、哪些远程专家权重需要预取、哪些 padding 行要清零。`MoonEPCommPlan` 就是这份计划的**全部落地载体**——7 个 int32 张量 + 6 个整数标量。

它被设计成**不可变快照（immutable snapshot）**：

- **结构不可变**：`frozen=True` 禁止替换字段。plan 一经构造，「这套张量 + 这套配置」的对应关系终生固定，任何代码都不会把它换成形状不同的另一组张量。
- **内容由内核填充**：`allocate_planning_outputs` 先分配空张量并构造 plan；随后 planning 内核与 dispatch 内核**原址写入**这些张量。所以「不可变」指的是引用与配置，不指数据。
- **跨阶段存活**：u1-l4 已讲过，plan 必须跨前向/反向保存——`prefetch_weight`、`combine`、`dispatch(plan=...)`（combine bwd 复用）和 `reduce_grad` 四个入口都要消费同一个 plan 实例。

为什么这样设计？因为 plan 是**所有通信内核之间的唯一契约**。规划、派发、去重展开、归并、梯度归约分属 5 个以上独立编译的 GPU 内核，它们之间不传参、不通信，唯一的共享状态就是 plan 里的这些张量。把契约冻结成一个不可变结构，等于把「内核 A 写的形状」与「内核 B 读的形状」在 Python 层一次性锁死。

#### 4.1.2 核心流程

plan 的完整生命周期：

```
allocate_planning_outputs(ctx)        # ① 分配 8 个空张量（含 cu_seqlens），构造 plan
        │
launch_planning(ctx, ..., plan)       # ② planning 内核原址写：dst / experts_to_copy
        │                                / zero_fill_ranges / remote_stats / cu_seqlens
launch_dispatch(..., plan,            # ③ dispatch 的 dedup builder 原址写：
    build_dedup_map=True)             #    dup_groups / dup_loffs / dup_counts
        │
prefetch_weight(plan)                 # ④ 读 experts_to_copy[本rank行]
dispatch_epilogue(plan)               # ⑤ 读 dup 三件套（展开重复行）
combine_prologue(plan) + combine(plan)# ⑥ 读 dup 三件套、dst
dispatch(hidden, plan=plan)           # ⑦ 复用：跳过 ②③，直接读既有张量（combine bwd）
reduce_grad(plan)                     # ⑧ 读 experts_to_copy
        │
（可选）plan.clone()                  # ⑨ 深拷贝快照，防止后续 dispatch 改写内容
```

注意 ③：去重三件套虽然「属于」plan（由 `allocate_planning_outputs` 分配），但**不是 planning 内核写的**——fresh dispatch 的 builder warps 才填充它们；plan 复用路径则完全不重建（见 [moonep/dispatch.py:L8-L10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L8-L10) 的模块级说明）。

#### 4.1.3 源码精读

**(a) 字段定义**

[moonep/planning.py:L31-L50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L31-L50) 定义了整个数据类，装饰器与字段如下（节选）：

```python
@dataclass(frozen=True, slots=True)
class MoonEPCommPlan:
    dst: torch.Tensor              # [N] int32，扁平 top-k 目的槽位表
    experts_to_copy: torch.Tensor  # [R, B] int32，每 rank 的 top-B 预取专家
    zero_fill_ranges: torch.Tensor # [E+B, 2] int32，padding 行清零区间
    remote_stats: torch.Tensor     # [2] int32，诊断统计
    N: int                         # 以下 6 个为整数配置快照
    R: int
    E: int
    B: int
    NvS: int
    K: int
    dup_groups: torch.Tensor       # [NvS, 3] int32，去重组头
    dup_loffs: torch.Tensor        # [NvS] int32，去重槽扁平表
    dup_counts: torch.Tensor       # [2] int32，紧凑前缀长度
```

6 个标量（N/R/E/B/NvS/K）把「这份 plan 对应的 Buffer 配置」一并冻结进去，任何消费方拿到的都是自描述对象，不需要再回头问 Buffer 要配置。

**(b) 构造期断言**

[moonep/planning.py:L52-L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L52-L71) 的 `__post_init__` 在构造后立即校验全部 7 个张量的 dtype、连续性与形状，例如：

```python
assert self.dst.dtype == torch.int32 and self.dst.is_contiguous()
assert self.dst.numel() == N
assert self.experts_to_copy.dtype == torch.int32 and self.experts_to_copy.is_contiguous()
assert tuple(self.experts_to_copy.shape) == (R, B)
...
assert tuple(self.dup_groups.shape) == (NvS, 3)
assert tuple(self.dup_loffs.shape) == (NvS,)
assert tuple(self.dup_counts.shape) == (2,)
```

这是「契约前置」的典型做法：内核按裸指针访问，形状错了不会在 Python 层报错，而是直接越界或静默算错。把检查压到构造瞬间，错误在出现越界**之前**就会被抓住。

**(c) clone：深拷贝快照**

[moonep/planning.py:L73-L88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L73-L88)：

```python
def clone(self) -> "MoonEPCommPlan":
    return type(self)(
        dst=self.dst.clone(),
        experts_to_copy=self.experts_to_copy.clone(),
        ...                           # 7 个张量逐一 clone（深拷贝，新存储）
        N=self.N, R=self.R, E=self.E, B=self.B, NvS=self.NvS, K=self.K,
    )
```

两个真实用途（都来自测试）：

- **对比实验快照**：[tests/test_e2e.py:L238-L240](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L238-L240) 在做 plan 复用对照前先 `plan_snapshot = plan_sync.clone()`——因为**同一个 plan 对象**里的 dedup 三件套会被下一次 fresh dispatch 重建，不快照就会被改写。
- **错误注入测试**：[tests/test_dispatch.py:L487-L491](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L487-L491) 先 `bad_plan = plan.clone()` 再故意破坏字段，验证内核的形状检查会拒绝非法 plan。

一句话总结：`frozen` 保证「谁在用这个 plan」，`clone` 保证「用的时候内容还没被下一次迭代改掉」。

#### 4.1.4 代码实践

**实践目标**：不跑任何 GPU 内核，只用 PyTorch 在 CPU 上手工构造一个「合法」与若干「非法」的 plan，把 `__post_init__` 的每条断言触发一遍。

**操作步骤**（示例代码，可直接保存为 `plan_shape_probe.py` 运行，仅需 `import torch`；`import moonep.planning` 需要 GPU 环境，故脚本做双路径处理）：

```python
# plan_shape_probe.py —— 示例代码
import torch
from dataclasses import dataclass

# 路径 A：有完整 MoonEP 环境时用真类；否则用等价复刻
try:
    from moonep.planning import MoonEPCommPlan
except Exception:
    @dataclass(frozen=True, slots=True)
    class MoonEPCommPlan:  # 只复刻断言逻辑，字段同 planning.py L31-50
        dst: torch.Tensor; experts_to_copy: torch.Tensor
        zero_fill_ranges: torch.Tensor; remote_stats: torch.Tensor
        N: int; R: int; E: int; B: int; NvS: int; K: int
        dup_groups: torch.Tensor; dup_loffs: torch.Tensor; dup_counts: torch.Tensor

        def __post_init__(self):
            N, R, E, B, NvS = self.N, self.R, self.E, self.B, self.NvS
            assert self.dst.dtype == torch.int32 and self.dst.is_contiguous() and self.dst.numel() == N
            assert self.experts_to_copy.dtype == torch.int32 and tuple(self.experts_to_copy.shape) == (R, B)
            assert tuple(self.zero_fill_ranges.shape) == (E + B, 2)
            assert tuple(self.remote_stats.shape) == (2,)
            assert tuple(self.dup_groups.shape) == (NvS, 3)
            assert tuple(self.dup_loffs.shape) == (NvS,)
            assert tuple(self.dup_counts.shape) == (2,)

S, K, E, R, token_padding = 4096, 8, 256, 8, 128
B = E // R
N, NvS = S * K, S * K + (token_padding - 1) * 2 * (E // R)

plan = MoonEPCommPlan(
    dst=torch.zeros(N, dtype=torch.int32),
    experts_to_copy=torch.zeros(R, B, dtype=torch.int32),
    zero_fill_ranges=torch.zeros(E + B, 2, dtype=torch.int32),
    remote_stats=torch.zeros(2, dtype=torch.int32),
    N=N, R=R, E=E, B=B, NvS=NvS, K=K,
    dup_groups=torch.zeros(NvS, 3, dtype=torch.int32),
    dup_loffs=torch.zeros(NvS, dtype=torch.int32),
    dup_counts=torch.zeros(2, dtype=torch.int32),
)

# 依次打开下面每行，观察FrozenInstanceError / AssertionError
# plan.dst = torch.zeros(1)                          # frozen：结构不可变
# plan2 = MoonEPCommPlan(**{**plan.__dict__, "dst": torch.zeros(N - 1, dtype=torch.int32)})
#                                                   # 断言：dst.numel() != N
# plan3 = MoonEPCommPlan(**{**plan.__dict__, "dup_groups": torch.zeros(NvS, 4, dtype=torch.int32)})
#                                                   # 断言：dup_groups 形状错

snap = plan.clone()
snap.dst[0] = 123                                   # clone 后内容独立
print("ok:", plan.dst[0].item(), snap.dst[0].item())  # 0 123
```

> 注意：`slots=True` 的实例没有 `__dict__`，上面的 `{**plan.__dict__}` 写法在真类（slots）下不可用——需要用 `dataclasses.asdict(plan)` 或手动传参。这正是 slots 与 frozen 组合的一个小坑，亲手触发一次有助于记住它。

**需要观察的现象**：
1. 合法构造静默通过；
2. 重新赋值抛 `FrozenInstanceError`；
3. 形状/dtype 错误在**构造瞬间**（而不是内核运行时）抛 `AssertionError`；
4. `clone` 得到的副本修改互不影响。

**预期结果**：全部符合。错误注入段的具体报错信息依赖 Python 版本，**待本地验证**（本环境无 GPU，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：`frozen=True` 之后，内核为什么还能「写入 plan」？

**答案**：`frozen` 冻结的是字段**绑定**（`plan.dst` 永远指向同一个张量对象），不冻结张量内部的数值。内核拿 `plan.dst.data_ptr()` 按裸指针原址写入，Python 层的冻结机制根本不参与。

**练习 2**：如果把 `dup_counts` 的断言从 `(2,)` 改成 `(1,)`，最早在哪里出错？

**答案**：在 `MoonEPCommPlan.__post_init__`——`allocate_planning_outputs`（L1233）分配的就是 2 个元素，构造 dataclass 的瞬间断言就失败，轮不到任何内核执行。

**练习 3**：`clone()` 为什么必须逐张量深拷贝，而不是返回 `self` 或浅拷贝？

**答案**：plan 的张量内容会被后续 fresh dispatch 原址改写（尤其 dedup 三件套每次重建）。返回 `self` 或浅拷贝的话，快照与原对象共享存储，「快照」失去意义。`tests/test_e2e.py:L240` 的用法正是为了在对照实验中隔离这种改写。

---

### 4.2 各字段的语义与生产-消费关系

#### 4.2.1 概念说明

plan 的 7 个张量分三组：

1. **路由结果组**：`dst` —— 本 rank 发出的 \(N = S \times K\) 份拷贝各自落到「哪个 rank 的哪个接收槽」。
2. **均衡执行组**：`experts_to_copy`、`zero_fill_ranges`、（诊断用）`remote_stats` —— 描述动态冗余专家方案：预取哪些远程权重、哪些 padding 行要清零。
3. **去重组**：`dup_groups` / `dup_loffs` / `dup_counts` —— 同一 token 的多个 top-k 落到同一目的 rank 时，只传一份 payload（详见 u3-l5），这三个张量记录重复槽的分组信息。

**`dst` 的编码**：非负值 \(= \text{dest\_rank} \times N_{vS} + \text{loff}\)，即「目的 rank 的接收缓冲内偏移」（rank-stride 编码，与 u2-l2 讲过的对称内存布局一一对应）；重复目的 rank 的后续项编码为 \(-\text{raw\_dst} - 1\)（负数），表示「只传权重、不传 payload」。本讲只需记住**编码规则**，规范化过程在 u3-l5 精读。

**去重三件套的契约**（源自 [moonep/planning.py:L44-L47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L44-L47) 的注释）：

- `dup_counts = [n_groups, n_dup_loffs]`：前缀长度。
- 只有 `dup_groups[:n_groups]` 与 `dup_loffs[:n_dup_loffs]` 是**有效紧凑前缀**，其后的行是未初始化垃圾。
- `dup_groups[g] = (primary_loff, dup_loffs_begin, dup_count)`：第 g 组的主行槽位、该组重复槽在 `dup_loffs` 中的起始下标、重复槽数。
- **顺序不稳定**：组的排列顺序由 builder 的 `atomicAdd` 到达顺序决定，逐次运行可能不同——消费者必须按下标迭代、按集合语义比较（这正是 u6-l4 测试方法论的伏笔）。

**为什么 `cu_seqlens` 不在 plan 里**？`allocate_planning_outputs` 明明分配了它，却作为返回元组的第二项单独交出（[moonep/planning.py:L1203](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1203)）。原因有二：

1. **生命周期不同**：`cu_seqlens` 是交给用户组 GEMM 的输出契约（README：「a planner-produced `cu_seqlens`」驱动 `[E+B]` 段的激活行选择），属于「用户可见输出」；plan 是 MoonEP 内核间契约。plan 复用路径（combine bwd）`cu_seqlens` 为 `None`（[moonep/api.py:L793](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L793)）——用户手里已经握着上次 dispatch 返回的那份。
2. **冻结语义不同**：plan 是 frozen 快照；`cu_seqlens` 每次新鲜规划都会重写。塞进 plan 反而让「复用时不重算」的语义变得含糊。

#### 4.2.2 核心流程

一图看清「谁写谁读」：

| 字段 | 形状 / dtype | 生产者（写入者） | 消费者（读取者） | 一句话语义 |
|---|---|---|---|---|
| `dst` | `[N]` int32 | planning 内核（passB + 去重规范化） | dispatch S2G warp、combine 内核 | 第 `offv` 份 top-k 拷贝的目的槽位（非负）或重复标记（负） |
| `experts_to_copy` | `[R, B]` int32 | planning 内核（top-B 选择） | `prefetch_weight`（只取本 rank 行）、`reduce_grad` | 每 rank 要预取的 top-B 远程专家；`-1` 表示空槽 |
| `zero_fill_ranges` | `[E+B, 2]` int32 | planning 内核（Phase C 段布局） | dispatch 的 zero warp | 每个分段 `(pad_start_loff, n_pad_rows)`：padding 行清零区间 |
| `remote_stats` | `[2]` int32 | planning 内核 | 诊断/测试（内核主路径不读） | `[0]` 本 rank 的远程非空专家数；`[1]` 本 rank 专家被其他 rank 预取的次数 |
| `dup_groups` | `[NvS, 3]` int32 | dispatch 的 dedup builder | dispatch epilogue、combine prologue | 重复组头三元组 `(primary_loff, loffs_begin, dup_count)`，前 `dup_counts[0]` 行有效 |
| `dup_loffs` | `[NvS]` int32 | dispatch 的 dedup builder | 同上 | 所有重复槽的 loff 扁平表，前 `dup_counts[1]` 个有效 |
| `dup_counts` | `[2]` int32 | dispatch 的 dedup builder | 同上（含设备端读取） | 紧凑前缀长度 `[n_groups, n_dup_loffs]` |
| （`cu_seqlens`） | `[E+B]` int32 | planning 内核 | 用户组 GEMM、dispatch/测试 | 每分段的**padded**结束偏移；不在 plan 内 |

#### 4.2.3 源码精读

**(a) `dst` 的两个写入点**

[moonep/planning.py:L1067](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1067) 写非负编码（`lo` 是二分查得的目的 rank，`bo` 是段内起始偏移）：

```python
dst_out[offv] = lo * NvS + bo + (global_rank - pc)
```

[moonep/planning.py:L1098-L1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1098-L1113) 在每个 token 的 K 个槽内用两个 int64 位图检测重复目的 rank，重复项改写为负数：

```python
if dup:
    dst_out[base_idx + k] = -(dst_vals[k]) - 1
```

**(b) `experts_to_copy` / `remote_stats` 的写入**

[moonep/planning.py:L866-L884](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L866-L884)：rank0 对每个目的 rank 用「扫最大-清零-再扫最大」的单 warp 循环选 top-B 最热远程专家；`best_cnt > 0` 时记专家号，否则记 `-1`：

```python
expert_idx = best_idx if best_cnt > 0 else -1
s_selected_experts[slot] = expert_idx
all_experts_to_copy[dest_rank, slot] = expert_idx
```

同一段里，`[dest_rank, 0]` 记录该 rank 的远程非空专家数（[L865](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L865)），每当选中一个属于 `owner_rank` 的专家，就对 `[owner_rank, 1]` 原子加一（[L876-L882](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L876-L882)）。随后在 [L1118-L1120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1118-L1120)，每个 rank 把**自己那行**统计从 meta_buf 拷进 `plan.remote_stats`。

**(c) `zero_fill_ranges` 的写入**

[moonep/planning.py:L940-L951](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L940-L951)：Phase C 里 rank0 为每个非空分段计算 padded 布局，padding 部分记录 `(pad_start, pad_count)`；空分段写 `(0, 0)`。消费方是 dispatch 的 zero warp，见 [moonep/dispatch.py:L453-L472](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L453-L472)（注释明确写着 `zero_fill_ranges[e] = (pad_start_loff, n_pad_rows)`）。

**(d) dedup 三件套的写入（dispatch 内核）**

[moonep/dispatch.py:L540-L542](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L540-L542) 每次 fresh dispatch 先把 `dup_counts` 两个计数器清零；[L627-L639](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L627-L639) 每个 warp 用**两次** warp 聚合的 `atomicAdd` 预订本 warp 的紧凑前缀区间（而不是逐记录原子加——注释说明后者曾把 dispatch 延迟串行化）；[L665-L678](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L665-L678) 写出三元组与重复槽：

```python
dup_groups_tensor[dup_group_key]     = loff          # 主行槽位
dup_groups_tensor[dup_group_key + 1] = my_dup        # 该组在 dup_loffs 的起点
dup_groups_tensor[dup_group_key + 2] = dup_count     # 重复槽数
dup_loffs_tensor[my_dup + pos]       = cur_dup_loff  # 逐个重复槽
```

[L644-L647](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L644-L647) 的注释直接解释了「顺序不稳定」：紧凑前缀的跨 warp 顺序跟随 atomicAdd 到达顺序，**逐次运行不保证一致**，消费者按下标迭代、测试按集合比较。测试侧的对应工具是 [tests/kernel_test_utils.py:L153-L161](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L153-L161) 的 `DEDUP_PLAN_FIELDS` 与 `clone_dedup_plan_fields`。

**(e) API 侧的消费点**

- `prefetch_weight` / `reduce_grad` 都只取 `experts_to_copy` 的**本 rank 行**：[moonep/api.py:L716-L717](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L716-L717) 的 `experts_to_copy[int(ctx['rank'])]`。
- 异步路径上，plan 的 7 个张量统一交给 CUDA 做流生存期登记：[moonep/api.py:L605-L615](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L605-L615) 的 `_plan_runtime_tensors`，在 dispatch（[L834](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L834)）与 combine（[L1059](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1059)）的 `record_stream` 清单里展开。这也是「plan 字段清单必须收口在一处」的工程理由：加一个张量字段而忘了登记，异步路径就会踩到已释放显存。

#### 4.2.4 代码实践

**实践目标**：纯源码阅读型实践——跟踪 `remote_stats` 两个分量的语义闭环，并写出它的「人类可读」注释。

**操作步骤**：

1. 打开 [moonep/planning.py:L857-L865](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L857-L865)，找出 `stats[0]` 被赋的值来自哪个计数（提示：`remote_expert_counts` 中大于 0 的项数，即「该目的 rank 由远程专家服务的非空分段数」）。
2. 打开 [L876-L882](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L876-L882)，确认 `stats[1]` 的原子累加发生在 `owner_rank = expert_idx // epn` 上（即「我的专家被别人借走了几次」）。
3. 打开 [L1118-L1120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1118-L1120)，确认每个 rank 拷回的是 `rank * 2` 偏移的**自己那行**。
4. 在 `tests/` 里 grep `remote_stats`，观察测试如何对拍这两个值。

**需要观察的现象**：`stats[0]` 的统计范围是「目的 rank 视角」（我要从远程借多少个专家分段），`stats[1]` 是「所有者视角」（我的专家被借走多少次）；二者统计的都是**专家数**而不是 token 数，且都与 top-B 选择的循环共处同一段代码。

**预期结果**：能写出两条注释——`remote_stats[0]`：本 rank 计划中由远程专家承接的非空分段数；`remote_stats[1]`：本 rank 的 home 专家被其他 rank 选入预取槽的次数。二者都不进入通信内核的执行路径，属于规划质量的诊断信号（消费细节**待确认**：仓库主路径未读取，主要用于测试与观测）。

#### 4.2.5 小练习与答案

**练习 1**：`experts_to_copy` 的形状为什么是 `[R, B]` 而不是 `[B]`？

**答案**：planning 是全组视角——rank0 为**每个**目的 rank 各选一份 top-B（[L834](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L834) 的 `for dest_rank in cutlass.range(pid, R, num_sms)`）。`[R, B]` 的完整矩阵被组播发布，各 rank 再经 Phase D 拷回自己那份完整副本；消费时本 rank 只取自己那一行。若只存 `[B]`，`reduce_grad` 等需要全组信息的消费方就无据可查。

**练习 2**：`dup_groups` 分配了 `NvS` 行，但有效行只有 `dup_counts[0]` 行。为什么不按需分配？

**答案**：有效组数要到 dispatch builder 在设备上数完才知道，而分配发生在 Python 侧、内核启动之前。若想知道精确数量就得设备-宿主同步——这正是 MoonEP 静态形状设计要消灭的东西（u2-l1）。所以按最坏情况（≤ NvS）静态分配，用 `dup_counts` 记录紧凑前缀长度，epilogue 甚至在设备端直接读 `dup_counts[0]`。

**练习 3**：测试里比较两次运行的 dedup 结构，为什么不能 `torch.equal(dup_groups_a, dup_groups_b)`？

**答案**：组的排列顺序由 builder 的 atomicAdd 到达顺序决定，逐次运行不稳定（[moonep/dispatch.py:L644-L647](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L644-L647) 注释明说）。必须把每组 `(primary_loff, {重复槽集合})` 当作集合来比较——`tests/kernel_test_utils.py` 的 `dedup_plan_semantic_errors` 就是干这个的。

---

### 4.3 `allocate_planning_outputs`：分配、对齐与边界检查

#### 4.3.1 概念说明

`allocate_planning_outputs(ctx)` 是 plan 唯一的出生地，它做三件事：

1. **读 ctx**：从 Buffer 的上下文里取 `E/B/S/K/NvS/R` 与 `meta_buf` 的 device。这些字段全部由 `_create_context`（u2-l1 精读过）在 Buffer 构造期算定，其中 `NvS = S*K + (token_padding-1)*2*E/R`、`B` 默认 `E/R`（[moonep/api.py:L273-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L273-L288)）。
2. **过量分配再切片**：每个张量先分配 \(\operatorname{round4}(n) = \lceil n/4 \rceil \times 4\) 个元素，再 `[:n]` 切回逻辑形状、必要时 `view` 成二维。物理多出来的 1~3 个元素是给内核尾部 128 位向量写的「安全垫」，**返回形状不变**。
3. **构造并返回 `(plan, cu_seqlens)`**：dedup 三件套在此分配但**不填**——fresh dispatch 的 builder 才填（docstring [L1204-L1207](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1204-L1207) 明确说明）。

**边界检查**分两层，`launch_planning`（[L1294-L1316](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1294-L1316)）每次启动前依次调用：

- `_check_planning_outputs`：plan/cu_seqlens 的标量与 Buffer 配置逐项相等、dtype 与形状正确——防止用户把别的 Buffer 的 plan 混进来，或张量被中途替换。
- `_check_dedup_encoding_bounds`：六条静态上限，全部源于**位编码的物理约束**（在编译期/启动期拦截，而不是让内核静默溢出）。

六条检查与动机对照：

| 检查 | 上限 | 动机 |
|---|---|---|
| `N <= NvS` | S·K ≤ NvS | `src_info` 以 NvS 为 stride 做 rank-stride 编码，源偏移必须能装进一个 stride 段 |
| `R * NvS <= int32_max` | 约 2³¹ | `src_info` 线性编码 `src_rank * NvS + offv` 不能溢出 int32 |
| `R <= 128` | 128 | dst 去重规范化用两个 int64 位图（各 64 位）覆盖目的 rank |
| `K <= 2^KIDX_BITS - 1` | 127（KIDX_BITS=7） | `primary_packed` 打包编码里的 kidx 字段位宽 |
| `NvS <= 2^(32-1-KIDX_BITS) - 1` | 2²⁴−1 | `primary_packed` 的 loff 字段位宽（总 32 位去掉符号位与 kidx 字段） |
| `K <= 32` | 32 | `kmask` 是单个 b32 位掩码，每 kidx 占一位 |

#### 4.3.2 核心流程

```
Buffer.dispatch(plan=None)
  └─ allocate_planning_outputs(ctx)            # api.py L790
       ├─ 读 ctx 的 E/B/S/K/R/NvS 与 device
       ├─ 8 个 torch.empty(_round4(n), int32)[:n]（其中 7 个进 plan，cu_seqlens 单独返回）
       └─ MoonEPCommPlan(...) → __post_init__ 断言
  └─ launch_planning(ctx, topk, tpe, cu_seqlens, plan)
       ├─ _check_planning_outputs   # 形状/配置一致性
       ├─ _check_dedup_encoding_bounds  # 六条位宽上限
       └─ _launch_planning_kernel   # in-place 写入 plan 的 4 个张量 + cu_seqlens
```

分配用的是 `torch.empty`（未初始化）——安全的前提是「planning 内核会写满所有逻辑元素」：`dst` 的 N 个、`experts_to_copy` 的 R×B 个、`zero_fill_ranges` 的 (E+B)×2 个都会被 Phase D 回写覆盖；只有 dedup 三件套的有效前缀之外留着垃圾，由 `dup_counts` 界定。

#### 4.3.3 源码精读

**(a) `_round4` 与分配主体**

[moonep/planning.py:L1198-L1199](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1198-L1199) 定义了对齐函数，[L1220-L1233](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1220-L1233) 是八次同构分配，节选三个代表：

```python
dst = torch.empty(_round4(N), dtype=torch.int32, device=dev)[:N]
experts_to_copy = torch.empty(_round4(ctx['R'] * B), dtype=torch.int32, device=dev)[
    :ctx['R'] * B
].view(ctx['R'], B)                                   # 切片后 view 成 [R, B]
dup_groups = torch.empty(_round4(NvS * 3), dtype=torch.int32, device=dev
    )[:NvS * 3].view(NvS, 3)
```

三个细节：

- **切片与 view 的顺序**：永远先 `[:逻辑元素数]` 再 `view`。若先 view 再切片，`_round4` 多出的尾巴会污染形状推导。
- **device 来自 `ctx['meta_buf'].device`**（[L1215](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1215)）：plan 必须与通信缓冲同卡，内核才能裸指针访问。
- **切片仍是父张量的视图**：返回的 `dst` 背后是 `_round4(N)` 大小的存储，多出的元素不会被 Python 侧读到，但物理上承接了内核的越界向量写。

**(b) `launch_planning` 的检查链**

[moonep/planning.py:L1309-L1310](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1309-L1310) 两行调用顺序固定：先验形状、再验编码上限，最后才进 `_launch_planning_kernel`。`_check_dedup_encoding_bounds` 的定义在 [L1267-L1291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1267-L1291)，其中 `NvS_BITS = 32 - 1 - KIDX_BITS`（[L1272](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1272)）——`KIDX_BITS = 7` 来自 [moonep/constants.py:L7-L10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L7-L10)，注释称之为「planning / dispatch 内核和测试的 single source of truth」。

**(c) 分配调用点**

[moonep/api.py:L784-L794](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L784-L794)：`dispatch` 在 `plan is None` 分支里分配；`plan` 传入时直接走复用路径（`cu_seqlens = None`）。除 API 外，测试（`tests/test_planning.py`、`tests/test_dispatch.py`、`tests/test_combine.py`）与基准（`benchmarks/bench_comm.py:L198`）都直接调用 `allocate_planning_outputs` 来绕开 `Buffer.dispatch` 单独压测规划/派发内核——它是事实上的「内核级测试入口」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：编写 `plan_alloc_repro.py`，用纯 PyTorch（CPU 即可）复现 `allocate_planning_outputs` 的分配逻辑；为一组配置打印全部 plan 张量的形状、字节数与一行语义注释，并复刻六条边界检查自检。

**操作步骤**：

```python
# plan_alloc_repro.py —— 示例代码（对照 moonep/planning.py L1198-L1250 复现）
import torch

def _round4(n):                       # planning.py L1198-1199
    return (n + 3) & ~3

def alloc_plan(S, H, K, E, R, token_padding, B=None, dev="cpu"):
    epn = E // R
    if B is None:
        B = epn                       # api.py L273-274：B 默认 E/R
    N = S * K
    extra = (token_padding - 1) * 2 * epn   # api.py L287
    NvS = N + extra                   # api.py L288
    ctx = dict(E=E, B=B, S=S, K=K, R=R, N=N, NvS=NvS,
               meta_buf=torch.empty(1, dtype=torch.int32, device=dev))

    tensors = {
        "dst":               torch.empty(_round4(N),           dtype=torch.int32, device=dev)[:N],
        "cu_seqlens":        torch.empty(_round4(E + B),       dtype=torch.int32, device=dev)[:E + B],
        "experts_to_copy":   torch.empty(_round4(R * B),       dtype=torch.int32, device=dev)[:R * B].view(R, B),
        "zero_fill_ranges":  torch.empty(_round4((E + B) * 2), dtype=torch.int32, device=dev)[:(E + B) * 2].view(E + B, 2),
        "remote_stats":      torch.empty(_round4(2),           dtype=torch.int32, device=dev)[:2],
        "dup_groups":        torch.empty(_round4(NvS * 3),     dtype=torch.int32, device=dev)[:NvS * 3].view(NvS, 3),
        "dup_loffs":         torch.empty(_round4(NvS),         dtype=torch.int32, device=dev)[:NvS],
        "dup_counts":        torch.empty(_round4(2),           dtype=torch.int32, device=dev)[:2],
    }

    # 复刻 _check_dedup_encoding_bounds（planning.py L1267-L1291，KIDX_BITS=7）
    KIDX_BITS = 7
    NvS_BITS = 32 - 1 - KIDX_BITS
    assert N <= NvS
    assert R * NvS <= 2**31 - 1
    assert R <= 128
    assert K <= (1 << KIDX_BITS) - 1
    assert NvS <= (1 << NvS_BITS) - 1
    assert K <= 32

    notes = {
        "dst":              "第 offv 份 top-k 拷贝的目的槽位；非负=dest_rank*NvS+loff，负=-raw-1 只传权重",
        "cu_seqlens":       "每分段 padded 结束偏移 [E+B]，给用户组 GEMM，不进 plan",
        "experts_to_copy":  "每 rank 的 top-B 预取专家号，-1 为空槽；prefetch/reduce_grad 只读本 rank 行",
        "zero_fill_ranges": "每分段 (pad_start, n_pad_rows)：dispatch zero warp 据此清零 padding 行",
        "remote_stats":     "[远程非空专家数, 本地专家被预取次数]，诊断用",
        "dup_groups":       "重复组头 (primary_loff, loffs_begin, dup_count)，前 dup_counts[0] 行有效",
        "dup_loffs":        "重复槽 loff 扁平表，前 dup_counts[1] 个有效",
        "dup_counts":       "[n_groups, n_dup_loffs] 紧凑前缀长度，epilogue/prologue 设备端读取",
    }
    print(f"S={S} K={K} E={E} R={R} B={B} tp={token_padding} -> N={N} NvS={NvS} "
          f"(extra={extra})")
    for name, t in tensors.items():
        pad = torch.empty(_round4(t.numel()), dtype=torch.int32).numel() - t.numel()
        print(f"{name:18s} shape={str(tuple(t.shape)):14s} "
              f"bytes={t.numel()*4:8d} round4_pad={pad}  # {notes[name]}")
    return tensors

if __name__ == "__main__":
    alloc_plan(S=4096, H=7168, K=8, E=256, R=8, token_padding=128)
    alloc_plan(S=1024, H=4096, K=4, E=64,  R=4, token_padding=64, B=32)
```

**需要观察的现象**：

1. 第一组配置的输出应为：`N=32768`、`extra=(128-1)*2*32=8064`、`NvS=40832`；`dst` 为 `[32768]`，`experts_to_copy` 为 `[8, 32]`，`zero_fill_ranges` 为 `[288, 2]`（E+B=256+32），`dup_groups` 为 `[40832, 3]`。
2. `round4_pad` 一列显示哪些张量被垫了尾巴（例如第一组 `cu_seqlens` 288 已是 4 的倍数 → pad=0；`E+B` 为奇数时 `cu_seqlens`/`zero_fill_ranges` 会出 pad）。
3. 把 `R` 改成 256 重跑：第五条检查失败前，`R <= 128` 先断言报错；把 `K` 改成 33：`K <= 32` 报错。

**预期结果**：形状与 pad 列全部与上表一致；非法配置在 Python 断言处立即失败（本环境无 GPU，脚本为纯 CPU 复现，**待本地验证**；有 GPU 环境时可在脚本尾部追加 `from moonep.planning import allocate_planning_outputs` 对拍真实分配的形状）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cu_seqlens` 需要 `_round4`（E+B=288 时 pad=0，但若 E+B=33 呢）？

**答案**：E+B=33 时 `_round4(33)=36`，内核按 4 个 int32 一组做尾部向量写时，第 33 个元素那组会触及 34~36 号元素——没有安全垫就会越界写坏相邻显存。`_round4` 是给**所有**内核可向量化写出的输出统一上的保险，与逻辑值无关。

**练习 2**：`dup_loffs` 的最坏情况长度是 `NvS`，实际什么时候会接近这个上界？

**答案**：`dup_loffs` 只收重复槽（每组 `dup_count` 个，非主行）。当大量 token 的多个 top-k 命中同一目的 rank（即 K 路里同 rank 重复很多）时，重复槽数趋近总槽位数，上界就是 `NvS`。所以按 `NvS` 静态分配是对任意路由都安全的最坏预算。

**练习 3**：如果给 `MoonEPCommPlan` 新增一个张量字段（比如 per-rank 的 token 计数），除了改 dataclass 与 `__post_init__`，还必须同步改哪三处？

**答案**：① `allocate_planning_outputs` 分配它并传入构造（否则构造断言失败）；② `MoonEPCommPlan.clone` 加一行深拷贝（否则快照不完整）；③ `Buffer._plan_runtime_tensors`（[moonep/api.py:L605-L615](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L605-L615)）登记它（否则异步路径 `record_stream` 漏掉，comm stream 可能访问已释放存储）。

## 5. 综合实践

把本讲三个模块串起来：编写 `plan_contract_report.py`，输入任意 `(S, K, E, R, token_padding, B)` 配置，产出一份「plan 契约报告」：

1. **复现分配**：按 4.3.4 的 `alloc_plan` 分配全部张量（CPU 即可）。
2. **构造合法 plan**：用分配出的张量构造 `MoonEPCommPlan`（或本地复刻版），让 `__post_init__` 断言全过——验证「分配 → 构造 → 断言」链路自洽。
3. **模拟最小内容**：手工把 `experts_to_copy[rank]` 填成若干专家号与 `-1`、`zero_fill_ranges` 填 2~3 个 `(start, count)` 区间、`dst` 的前 3×K 个元素填成 `rank_id * NvS + loff` 与一个 `-raw-1` 负数样例，逐项打印并在注释里解释每个数字的含义。
4. **clone 验证**：`snap = plan.clone()`，改 `snap.dst[0]`，确认原 plan 不变。
5. **边界扫描**：循环把 K 从 1 加到 33、R 从 1 加到 129（其余固定），记录断言首次失败的位置，输出「哪条上限先拦住」的表格。

有 GPU 环境时的加强版：把第 1 步换成真实 `allocate_planning_outputs(ctx)`（参照 `tests/test_planning.py:L262-L270` 的方式拿 ctx），与你的 CPU 复现逐张量 `torch.equal` 对拍。

## 6. 本讲小结

- `MoonEPCommPlan` 是 7 个 int32 张量（`dst` / `experts_to_copy` / `zero_fill_ranges` / `remote_stats` / `dup_groups` / `dup_loffs` / `dup_counts`）+ 6 个配置标量（N/R/E/B/NvS/K）的 `frozen` 快照；冻结的是「结构契约」，内容由 planning 与 dispatch 内核原址填充。
- 字段分三组：路由结果（dst）、均衡执行（experts_to_copy / zero_fill_ranges / remote_stats）、去重（dup 三件套）；`cu_seqlens` 由同一函数分配但**不进** plan——它是用户可见输出，且复用路径下为 `None`。
- dedup 三件套只有 `dup_counts` 界定的紧凑前缀有效，顺序由 atomicAdd 决定、**不稳定**，必须按集合语义比较。
- `allocate_planning_outputs` 用 `_round4` 过量分配再切片，为内核 128 位向量写留安全垫；`launch_planning` 前置两层检查——形状/配置一致性 + 六条位编码上限（R≤128、K≤32 等，全部源自 int32 打包编码的物理约束）。
- `clone()` 是深拷贝快照，用于防止后续 fresh dispatch 原址改写（`tests/test_e2e.py`）与错误注入测试（`tests/test_dispatch.py`）。

## 7. 下一步学习建议

plan 的「壳」已经讲完，下一讲进入「芯」：

- **u3-l2 群组级均衡**：`experts_to_copy` 和 `dst` 背后的算法第一步——tokens_per_expert 如何汇聚到 rank0、surplus/deficit 贪心循环如何生成 z 矩阵（对应 planning 内核 Phase A）。
- 精读顺序建议：先读 `tests/planning_reference.py` 的 PyTorch 参考实现建立直觉，再回头看 [moonep/planning.py:L601-L702](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L601-L702) 的 Phase A。
- 若对 `dst` 负数编码与 `src_info` 溯源感兴趣，可提前跳读 u3-l5；对 dedup builder 的设备端实现感兴趣则看 u4-l3。
