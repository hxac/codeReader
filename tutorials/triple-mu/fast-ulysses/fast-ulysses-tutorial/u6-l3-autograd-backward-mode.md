# u6-l3 自动微分：backward 是另一个 mode

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `A2AFunction` 这个 C++ `torch::autograd::Function` 在 `saved_data` 里保存了什么、为什么保存 group 本身，以及 `backward` 为什么必须用 `apply()` 而不是裸函数。
2. 用排列矩阵的数学证明「排列的 vjp 是逆排列」，从而理解 backward 就是 `1 - mode` 的前向调用。
3. 对比 `all_to_all_single` 的 backward 交换 split sizes 与本库 splits **原样传递**的语义差异，并能解释为什么交换在这里是错的。
4. 解释 forward AD（jvp）为什么让 tangent 走**第二条集合通信**、为什么这件事必须在 Autograd 内核里手工做，以及 reverse functorch（`grad`/`vjp`/`jacrev`）不可用的边界。

本讲承接 u6-l2 的调用流水线（prepare → 屏障 → 传输 → 屏障 → copy_out）：autograd 公式套在这条流水线外面，backward 复用的正是同一条流水线。

## 2. 前置知识

### 2.1 vjp 与 jvp：两种自动微分

- **reverse mode（反向模式）**：torch 默认的 `backward()`。对一个函数 \( f: \mathbb{R}^n \to \mathbb{R}^m \)，它计算 **vjp**（vector-Jacobian product）：给定上游梯度 \( g \in \mathbb{R}^m \)，算 \( J_f^{\top} g \in \mathbb{R}^n \)。一次反向传播能把一个标量 loss 对所有参数的梯度一起算完，适合训练。
- **forward mode（前向模式）**：计算 **jvp**（Jacobian-vector product）：给定输入方向 \( \dot{x} \in \mathbb{R}^n \)，算 \( J_f \dot{x} \in \mathbb{R}^m \)。torch 里通过 `torch.autograd.forward_ad` 的 **dual tensor**（对偶张量）使用：一个 dual tensor 携带 primal（原值）与 tangent（方向）两份数据，每个算子负责让 tangent 跟着 primal 一起传播。

### 2.2 排列的 Jacobian 是排列矩阵

`all_to_all_4d` 不做任何算术——它只把元素搬家。把所有 rank 的输入拼成一个全局向量 \( x \)，输出拼成 \( y \)，则 \( y = Px \)，\( P \) 是一个**排列矩阵**（每行每列恰有一个 1）。排列矩阵是正交阵：

\[ P^{\top} = P^{-1} \]

所以 vjp 是 \( J^{\top} g = P^{\top} g = P^{-1} g \)——**逆排列作用于梯度**。而 jvp 是 \( J \dot{x} = P\dot{x} \)——**同一个排列作用于方向**。这两条性质分别决定了本讲的两个结论：backward 是另一个 mode，forward AD 是同一个 mode 再跑一遍。

### 2.3 C++ 的 torch::autograd::Function

torch 允许用 C++ 写自定义可微算子：继承 `torch::autograd::Function<子类>`，实现静态 `forward(ctx, ...)` 与 `backward(ctx, grads)`。与 Python 的 `torch.autograd.Function` 相比：

- `ctx->saved_data` 是一个 `std::map<std::string, c10::IValue>`，可以存张量以外的对象（整数、整数向量、乃至本库的 `intrusive_ptr<UlyssesGroup>` 智能指针）。
- **它没有 C++ jvp 钩子**——这是本讲 4.4 节一切绕行设计的根源。
- 调用必须经 `子类::apply(...)`：这才是建图入口，返回的张量带 `grad_fn`；直接调 forward 函数则完全不进图。

### 2.4 functorch 的「 lifting 」边界

`torch.func`（functorch）通过把 autograd 节点「提升」为自己的解释器指令来工作。它只能提升 **Python** 的 autograd 节点；一个 C++ `torch::autograd::Function` 产生的节点它无法拆开，于是 `torch.func.grad` / `vjp` / `jacrev` 对本库直接 raise。这不是 bug 而是明确的能力边界，docs/api.md 把它写成契约。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | 全部核心：`A2AFunction`、`dispatch_below_autograd`、`with_reverse_ad`、`all_to_all_4d_autograd`（forward AD）、Autograd key 注册 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python 门面：`out=` 与 async 形式对梯度输入的显式拒绝 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | `build_plan` 的两个分支；mode 1 分支的注释自称 "The inverse" |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `make_dims` 的 splits-形状交叉校验（4.3 实践要用） |
| [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py) | backward、splits 原样传递、forward AD、双重 backward 的位精确测试 |
| [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py) | 「`out=` 对梯度输入必须 raise」的断言 |
| [test/test_plan.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py) | 无 GPU 的 round trip 证据：两个 plan 用同一对不均匀 splits |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) / [docs/migration.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md) | 可微性契约、与 DeepSpeed/yunchang wrapper 的对照 |
| [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py) | 无 wrapper 的可训练模块 + 输出/梯度双校验 |

## 4. 核心概念与源码讲解

### 4.1 A2AFunction：用 C++ autograd::Function 包装集合通信

#### 4.1.1 概念说明

DeepSpeed 的 `_SeqAllToAll`、yunchang 的 `SeqAllToAll4D` 都是 **Python** 的 `torch.autograd.Function`，包住一个不可微的通信调用。fast-ulysses 把这层 wrapper 直接搬进 C++（`A2AFunction`），因为通信本身就在 C++ 里——wrapper「被删除而不是被翻译」，这是 [docs/migration.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md) 的原话（见 [docs/migration.md:147-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L147-L155)：那些库的 backward 是「交换两个索引」，本库是「另一个 mode 加同一对 splits」）。

`A2AFunction` 要解决三个问题：

1. **建图**：让 `all_to_all_4d` 的输出带 `grad_fn`，能进任何更大的计算图。
2. **保存上下文**：backward 时刻需要 group（传输资源）、mode、两张 splits 表——它们必须原样存进 `ctx->saved_data`。
3. **不重复建图**：推理或 `no_grad` 下走快速路径，跳过节点分配——注释说这个算子「每步每注意力层跑两次」，死节点是纯开销。

#### 4.1.2 核心流程

```
torch.ops.fast_ulysses.all_to_all_4d(...)          # Autograd key 命中
        │
        ▼
all_to_all_4d_autograd                             # 4.4 节：先摘 tangent
        │  无 tangent
        ▼
with_reverse_ad
        ├─ GradMode 关 或 input 不需要梯度 ──► dispatch_below_autograd ──► 真内核（无图）
        └─ 否则 ──► A2AFunction::apply ──► forward:
                          ├─ ctx->saved_data ← {group, mode, seq, head}
                          └─ dispatch_below_autograd(group, input, mode, splits)
                                    │
                                    ▼
                       u6-l2 讲过的流水线：prepare → 屏障 → 传输 → 屏障 → copy_out
```

#### 4.1.3 源码精读

**保存什么**——[src/bindings.cc:512-529](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L512-L529)：`forward` 把四个对象写进 `ctx->saved_data`，然后经 `dispatch_below_autograd` 执行真正的传输。

```cpp
ctx->saved_data["group"] = group;   // intrusive_ptr 存进 IValue：图持有 group 的生命周期
ctx->saved_data["mode"]  = mode;
ctx->saved_data["seq"]   = seq_splits.has_value() ? c10::IValue(*seq_splits) : c10::IValue();
ctx->saved_data["head"]  = head_splits.has_value() ? c10::IValue(*head_splits) : c10::IValue();
```

两个细节值得停下来看：

- **group 进图**。[src/bindings.cc:522](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L522) 的注释写明：「图把 group 握在手里。一张活过 `destroy()` 的图会在 backward 里 raise——这正是对的答案，因为已经没有东西可以跑集合通信了」。`intrusive_ptr` 存进 `saved_data` 意味着引用计数被图持有；这同时是 [docs/api.md:15-17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L15-L17) 集合契约的第五条：「开 autograd 时，每个 rank 的引擎必须以相同顺序到达 backward 节点」——序列并行下天然成立（所有 rank 跑同一张图），但它是挂起的一个新入口。
- **splits 的 None 语义**。`std::optional<vector<int64_t>>` 有三态（无值 / 空表 / 有值），而 `IValue()` 默认构造是 None。backward 时用 `isNone()` 还原（[src/bindings.cc:541-547](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L541-L547)），「不传 splits」这一信息必须原样穿越图，否则 backward 会把偶均分特例误当成显式表。

**为什么不直接调 `all_to_all_4d`**——[src/bindings.cc:478-493](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L478-L493)：`dispatch_below_autograd` 用 `AutoDispatchBelowADInplaceOrView` 守卫后**重新进入 dispatcher**。直接调函数会永久离开调度器，Autograd 以下的所有 key——Python、FakeTensorMode、Meta——都不再被咨询，fake tensor 会一路冲进真内核死在 `cudaMemcpy3DAsync`。守卫才是阻止 `op.call` 绕回 Autograd 内核的东西；「GradMode 关着」不够，它管建图，不管 dispatcher 选哪个内核。这条路径 u6-l1 已建立，这里是它的唯一消费者。

**快速路径**——[src/bindings.cc:565-577](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L565-L577)：

```cpp
if (!at::GradMode::is_enabled() || !input.requires_grad()) {
    return dispatch_below_autograd(group, input, mode, seq_splits, head_splits);
}
return A2AFunction::apply(group, input, mode, seq_splits, head_splits);
```

两个条件任一成立就不建节点——注释（[src/bindings.cc:565-566](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L565-L566)）：`apply()` 分配的节点在没人用它时是死重，而这个算子每步每注意力层跑两次。

**注册点**——[src/bindings.cc:718-726](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L718-L726)：functional schema 注册了三个内核——`CompositeExplicitAutograd`（真内核，L720）、`Autograd`（指向 `all_to_all_4d_autograd`，L721）、`Meta`（形状传播，L725）。其中 Autograd 内核是全库**唯一**的（`empty_output`/`timed` 用 fallthrough，见 u6-l1）——forward AD 与 reverse AD 都从 `all_to_all_4d_autograd` 这一个函数进门。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「建图 / 不建图」两条路径的分界，并确认它们搬同一份字节。

**操作步骤**（示例代码，`torchrun --nproc_per_node=1` 即可运行；ws=1 是合法一等公民）：

```python
# 示例代码：practice_gradfn.py
import os
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
ws = dist.get_world_size()
g = UlyssesGroup(require_nvlink=False)

x = torch.randn(2, 16, 4 * ws, 128, device="cuda", dtype=torch.bfloat16)

# 路径一：需要梯度 -> A2AFunction::apply -> 输出带 grad_fn
xg = x.clone().requires_grad_(True)
y = g.all_to_all_4d(xg, mode=0)
print("grad_mode on :", y.grad_fn)            # 非 None，C++ 节点（具体 repr 待本地验证）

# 路径二：no_grad -> with_reverse_ad 快速路径 -> 无节点
with torch.no_grad():
    y2 = g.all_to_all_4d(xg, mode=0)
print("no_grad     :", y2.grad_fn)            # None

assert torch.equal(y2, y.detach())            # 两条路径搬的是同一份字节

g.destroy()
dist.destroy_process_group()
```

**需要观察的现象**：第一行打印非 `None`（C++ `torch::autograd::Function` 的节点，repr 形如 `CppNode<...>`——具体显示待本地验证）；第二行是 `None`；断言通过。

**预期结果**：`grad_fn` 的有无精确对应 `with_reverse_ad` 的两个分支；字节相等说明快速路径没有省略任何传输。若第一行也是 `None`，说明 Autograd key 没有被命中，需要检查注册（不应发生）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 group 存进 `saved_data` 改成存裸指针（或只存 rank/world_size），会坏什么？

**答案**：存裸指针则图的存活期不再延长 C++ 对象——`destroy()` 后 group 被释放，backward 解引用悬垂指针是未定义行为而非干净报错；只存 rank/world_size 则 backward 无法访问窗口、plan 缓存与传输流，等价于重造一个 group，而对称内存 rendezvous 是集合操作，放进 backward 会让所有 rank 的引擎节点顺序变成新的挂起来源。源码选择存 `intrusive_ptr`，让「活过 destroy() 的图在 backward 里 raise」成为受支持的错误路径（[src/bindings.cc:521-523](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L521-L523)）。

**练习 2**：`with_reverse_ad` 为什么检查 `input.requires_grad()`，而不是只检查 `GradMode`？

**答案**：即使 GradMode 开着，一个不需要梯度的输入（例如推理张量、常量）在它上面建节点也不会产生任何梯度——节点是死重。反过来 GradMode 关着时建图本身就被禁止。两个条件是「节点可能有用」的两个必要方面，任一不满足都走 `dispatch_below_autograd`。

### 4.2 backward = 1 − mode：排列的 vjp 是逆排列

#### 4.2.1 概念说明

backward 需要把上游梯度 \( g \)（形状同输出）变换回输入侧梯度。2.2 节已给出数学：\( y = Px \Rightarrow \text{vjp}(g) = P^{\top}g = P^{-1}g \)。剩下的问题是：**逆排列 \( P^{-1} \) 在本库的算子表里是哪一个？** 答案：mode 1、同一对 splits 的前向。于是 backward 不需要任何新代码——它就是一次方向相反的合法调用。

对偶地，mode 1 的 backward 是 mode 0，所以统一写成 `1 - mode`。

#### 4.2.2 核心流程

先把 mode 0 写成显式的元素映射。记 \( S_r = \sum_{i<r} \text{seq\_splits}[i] \)（序列前缀和）、\( H_p = \sum_{i<p} \text{head\_splits}[i] \)（头前缀和）、\( \mathrm{owner}(n) \) 为唯一满足 \( n \in [H_p, H_p + \text{head\_splits}[p]) \) 的 \( p \)。mode 0 是全局元素集上的双射：

\[
\sigma:\quad \underbrace{(b,\, s,\, n,\, k)}_{\text{rank } r \text{ 的输入本地坐标}}
\;\longmapsto\;
\underbrace{(b,\, S_r+s,\, n-H_{\mathrm{owner}(n)},\, k)}_{\text{rank } \mathrm{owner}(n) \text{ 的输出本地坐标}}
\]

即输出 rank \( p \) 上 \( y_p[b, t, n, k] = x_r[b,\, t - S_r,\, n + H_p,\, k] \)，其中 \( r = \mathrm{seq\_owner}(t) \)。全局拼接后 \( y = Px \)。三个推论：

1. \( P \) 与方向无关地由两张 splits 表完全决定，\( P^{-1} \) 对应**同一对表**下方向相反的搬运——即 `build_plan` 的 mode 1 分支。源码注释在 [src/a2a_plan.cc:194-198](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L194-L198) 直接以 "The inverse" 开头描述该分支。
2. vjp：\( \text{grad}_x = P^{\top} g = P^{-1} g \)——把 \( g \) 当数据做一次 mode 1 前向即可。位精确，不是近似：排列不改变字节的值，只改变位置。
3. 双重 backward 有干净的闭式：对 \( L = \sum_i y_i^2,\ y = Px \)，一阶梯度 \( g_1 = \nabla_x L = P^{\top}(2y) = 2x \)，二阶 \( \partial g_1 / \partial x = 2I \)——**处处等于 2**。这正是 [test/distributed/correctness.py:348-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L348-L358) 断言的常量。

#### 4.2.3 源码精读

**理论注释即规格**——[src/bindings.cc:495-511](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L495-L511)：「集合通信的伴随（adjoint）是反方向的集合通信」；「排列的 Jacobian 是排列矩阵，其转置即逆，故 vjp 是 `1 - mode`」。这段注释是全库数学密度最高的六行，逐句对应 4.2.2 的推导。

**backward 本体**——[src/bindings.cc:531-560](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L531-L560)：

```cpp
if (!grads[0].defined()) {                       // 上游没给梯度：五路全空
    return {at::Tensor(), at::Tensor(), at::Tensor(), at::Tensor(), at::Tensor()};
}
...
return {at::Tensor(),
        A2AFunction::apply(group, grads[0].contiguous(), 1 - mode, seq, head),
        at::Tensor(), at::Tensor(), at::Tensor()};
```

四个关键决定：

1. **返回五个元素**（[src/bindings.cc:534](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L534)）：forward 有五个非 ctx 参数（group、input、mode、seq、head），torch 要求 backward 逐参返回梯度；只有 `input` 可微，其余四路返回未定义张量。
2. **`1 - mode`**：本讲的全部数学浓缩成的两个字。
3. **`grads[0].contiguous()`**：上游梯度可能是任意跨步视图，而传输层要求连续（u6-l2 的 prepare 会做 `contiguous()`，这里提前做省一次隐式拷贝的疑惑）。
4. **`apply()` 而非裸函数**（[src/bindings.cc:554-556](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L554-L556)）：注释只有一句——「用 apply() 而不是普通函数，这样 grad-of-grad 会路由回这里」。递归进 `A2AFunction` 意味着一阶梯度的结果带 `grad_fn`，二阶 `torch.autograd.grad` 才能继续穿过多一层的排列节点。若用 `dispatch_below_autograd`，一阶梯度成叶节点，双重 backward 直接 raise。

**backward 永走 copying 路径**——[src/bindings.cc:549-554](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L549-L554) 的注释给出两个理由，值得逐条读：

> "The copying form, never `out` from empty_output(): that is a collective allocation, and putting one inside backward would hand the rendezvous position to the autograd engine's node order rather than to the caller's own program. A gradient also belongs to the engine, which may accumulate into it or hold it, while a window is single-buffered."

- `empty_output()` 是集合分配，把它放进 backward 等于把 rendezvous 的位置交给 autograd 引擎的节点执行顺序——这是 [docs/api.md:15-17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L15-L17) 契约的新违反面。
- 梯度属于引擎：引擎可能向它累加、也可能长期持有它；而窗口是单缓冲（u4-l2），下一次调用就会覆写。零拷贝路径的结果语义与梯度语义根本不兼容。

于是 backward 的输出走 u6-l2 的默认路径：`at::empty` 分配 + 一条平坦 copy_out——梯度是调用者侧的普通张量，没有任何生命周期规则。

**测试证据**——[test/distributed/correctness.py:296-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L308) 对两个方向断言位精确（注释强调：backward 搬同样字节、不重算任何东西，「close」会掩盖错误的 plan）：

```python
group.all_to_all_4d(xg, mode=mode).backward(upstream)
check.equal(f"backward mode={mode} is forward mode={1 - mode}",
            xg.grad, group.all_to_all_4d(upstream, mode=1 - mode))
```

#### 4.2.4 代码实践

**实践目标**：验证「mode 0 前向 + mode 1 前向串联」与 backward 的效果等价——这正是任务规格要求的脚本，也是 [test/distributed/correctness.py:162](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L162) round trip 与 [test_plan.py:236-257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L257) 无 GPU 重放在 GPU 上的合流点。

**操作步骤**（示例代码，`torchrun --nproc_per_node=<N>` 运行；ws≥2 时序列轴才真正跨 rank，ws=1 时退化为恒等但仍验证机制）：

```python
# 示例代码：practice_backward_equiv.py
import os
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
rank, ws = dist.get_rank(), dist.get_world_size()
g = UlyssesGroup(require_nvlink=False)

s_total = 97 * ws + 13                              # 故意不整除
seq_splits = [s_total // ws + (1 if p < s_total - (s_total // ws) * ws else 0) for p in range(ws)]
head_splits = [4] * ws
kw = {"seq_splits": seq_splits, "head_splits": head_splits}

x = torch.randn(2, seq_splits[rank], sum(head_splits), 128,
                device="cuda", dtype=torch.bfloat16)
xg = x.clone().requires_grad_(True)

# (a) backward 的效果：一次 mode 0 + backward
upstream = torch.randn_like(g.all_to_all_4d(x.detach(), mode=0, **kw))
g.all_to_all_4d(xg, mode=0, **kw).backward(upstream)

# (b) 等价的前向串联：mode 0 -> mode 1（把 upstream 当第一次的输入）
chained = g.all_to_all_4d(g.all_to_all_4d(upstream, mode=0, **kw), mode=1, **kw)

assert torch.equal(xg.grad, chained), "backward 与 mode1(mode0(g)) 不一致"
print(f"rank {rank}: backward == mode1(mode0(upstream)), bit-exact")
g.destroy()
dist.destroy_process_group()
```

**需要观察的现象**：断言通过；`upstream` 的形状与 mode 0 输出一致（`(2, s_total, 4, 128)`），`chained` 回到本 rank 分片形状 `(2, seq_splits[rank], 16, 128)`。

**预期结果**：三个断言全部位精确。若把 (b) 里第二个调用的 splits 故意改错（例如换成别的表），要么在 `make_dims` 的交叉校验处 raise（形状对不上），要么结果错位——见 4.3 的实践。

**运行前提**：需要安装好的 `_C` 扩展与 CUDA 设备；输出数值本身待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `backward` 对 `grads[0].defined()` 为假的情形要返回五个未定义张量，而不是直接报错？

**答案**：torch 的 autograd 引擎在计算大图时会对不需要梯度的分支传入未定义梯度张量（例如 `Tensor.requires_grad=False` 混合在图中）。返回五个未定义张量是 `torch::autograd::Function` 的标准协议：告诉引擎「这条路没有梯度可传」，图的其他分支照常收敛。报错会把正常的不依赖路径变成异常。

**练习 2**：\( L = \sum_i y_i^2 \)、\( y = Px \)。不用源码，推出 \( \partial^2 L / \partial x^2 \) 并说明哪个测试断言了它。

**答案**：\( \nabla_y L = 2y \)，\( \nabla_x L = P^{\top} 2y = 2P^{\top}Px = 2x \)，再对 \( x \) 求导得 \( 2I \)——Hessian 是常量 2 乘单位阵。[test/distributed/correctness.py:348-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L348-L358) 用 `create_graph=True` 取一阶梯度后再求一次，断言 `second == 2`。这个测试同时覆盖了 4.2.3 的第 4 点：能算二阶，说明 backward 真的经 `apply()` 回到了 `A2AFunction`。

**练习 3**：mode 0 的双射性在某个 rank 的 `seq_splits[r] == 0` 时还成立吗？

**答案**：成立。零长度分片意味着该 rank 输入为空、没有任何元素需要映射，双射定义在**全局元素集**上，与某侧定义域为空集不冲突；`build_plan` 对 `s_me == 0` / `s_p == 0` 有显式跳过守卫（[src/a2a_plan.cc:181-183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L181-L183)、[src/a2a_plan.cc:201-203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L201-L203)）。逆映射同样不受影响。

### 4.3 splits 不交换：与 all_to_all_single 的语义差异

#### 4.3.1 概念说明

这是最容易踩的迁移陷阱，也是 [docs/migration.md:153-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L153-L155) 特意点名的差异：偶均分时「交换索引」与「同一对 splits」给出相同结果，**一旦不均分就分道扬镳**。根源是两个库的参数描述的是不同的对象：

| | `dist.all_to_all_single` | fast-ulysses |
| --- | --- | --- |
| 参数含义 | **本次调用**每个 rank 的发送量 / 接收量（`split_sizes`） | **组的切分几何**：`seq_splits[p]`/`head_splits[p]` 是 rank p 的序列/头分片 |
| 方向反转时 | 收发角色互换 → **split sizes 必须交换** | 几何是组的属性，与数据流向无关 → **同一对表原样传递** |
| 谁持有 | 只有被调用轴的切分 | 两根轴的完整所有权表（每 rank 持全表，前缀和免通信算偏移） |

`mode` 选择的是「哪根轴到达时已分片」，而不是「两张表谁是发送谁是接收」。

#### 4.3.2 核心流程

为什么交换在这里必然出错，可以用 4.2 的映射直接论证：\( P \) 由 \( (S_r, H_p) \) 两组前缀和决定，\( P^{-1} \) 用**同一组**前缀和；若把表交换，前缀和全部错位，得到的既不是 \( P^{-1} \) 也不是任何合法搬运的逆。不均匀时第一个失败点是形状：mode 1 期望输入的序列轴长为 \( \sum_p \text{seq\_splits}[p] \)，交换后这个和变成 \( \sum_p \text{head\_splits}[p] \)，一般不相等。

另一个方向的论证（不传 splits 的情形，[src/bindings.cc:509-511](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L509-L511)）：mode 0 无 splits 从形状反推 `seq=[x1]*ws, head=[x2/ws]*ws`；在这个输出形状上做 mode 1 且同样不传，反推出**同一对表**；且 mode 1 检查整除的是序列轴——它是 ws 的倍数（`ws * x1` 的商再乘 ws），「forward 已通过的检查 backward 不可能失败」。

#### 4.3.3 源码精读

**规格注释**——[src/bindings.cc:502-507](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L502-L507)：

> "The splits do NOT swap. That is worth stating because all_to_all_single's backward DOES swap its input and output split sizes: those describe one call's send and receive counts, so reversing the direction reverses their roles. seq_splits[p] / head_splits[p] are a different object -- rank p's sequence and head shard, a property of the GROUP that holds whichever way the data moves."

**文档契约**——[docs/api.md:54-58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L54-L58)：「vjp of a permutation is the inverse permutation, so the backward is the other mode with the **same** splits」。

**防「好心修复」的测试**——[test/distributed/correctness.py:310-325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L310-L325) 用一组**严重倾斜**的 splits（`[s_total - (ws-1)] + [1]*(ws-1)` 配 `[4]*ws`）断言 `xu.grad == all_to_all_4d(up, mode=1, **kw)`。注释说明这是「有人按 all_to_all_single 的方式交换 splits『修复』backward 时会挂掉的检查——而且用倾斜切分，它挂在形状上，不只是数值上」。

**形状交叉校验**——[src/group.cc:88-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L88-L109)：`make_dims` 按模式核对「本 rank 可见的两根轴」——mode 1 期望输入形状为 `(b, seq_total(), head_splits[rank], d)`，与实际张量不符即 `TORCH_CHECK` 失败，消息形如 `input is [...] but the splits imply [...]`。它先于第一个屏障（u6-l2 的纪律），所以被拒的 rank 不拖累同伴——前提是**所有 rank** 都被拒，4.3.4 的实践会验证这一点。

**无 GPU 侧的旁证**——[test/test_plan.py:236-257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L257)：`test_round_trip` 对真正不均匀的 `seq_splits=[5,3,4,2]`、`head_splits=[2,3,1,2]` 分别以 `SCATTER_HEAD` 与 `GATHER_HEAD` 建两套 plan——**同一对表**——numpy 重放后逐 rank 断言 `gather(scatter(x)) == x`。往返还原是「mode 1 是 mode 0 的精确逆」的最直接证据，且完全不需要 GPU。

#### 4.3.4 代码实践

**实践目标**：亲手让「交换 splits」失败，看清失败发生在形状上；再对照 `all_to_all_single` 的语义总结差异。

**操作步骤**（示例代码，任一 ws 可运行）：

```python
# 示例代码：practice_no_swap.py
import os
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
rank, ws = dist.get_rank(), dist.get_world_size()
g = UlyssesGroup(require_nvlink=False)

s_total = 97 * ws + 13
seq_splits = [s_total - (ws - 1)] + [1] * (ws - 1)   # 严重倾斜
head_splits = [4] * ws
kw = {"seq_splits": seq_splits, "head_splits": head_splits}

upstream = torch.randn(2, s_total, 4, 128, device="cuda", dtype=torch.bfloat16)

# 正确用法：同一对表，mode 1
y = g.all_to_all_4d(upstream, mode=1, **kw)
print("same splits ok:", tuple(y.shape))             # (2, seq_splits[rank], 4*ws, 128)

# 错误用法：模仿 all_to_all_single 的 backward 交换两张表
swapped = {"seq_splits": head_splits, "head_splits": seq_splits}
try:
    g.all_to_all_4d(upstream, mode=1, **swapped)
except RuntimeError as e:
    print("swapped raises:", str(e).splitlines()[0]) # input is [...] but the splits imply [...]

g.destroy()
dist.destroy_process_group()
```

**需要观察的现象**：正确用法输出形状为 `(2, seq_splits[rank], 16, 128)`；交换用法在每个 rank 上都 raise，消息指出形状与 splits 隐含形状矛盾（交换后期望的序列轴长是 `sum(head_splits) == 4*ws`，而实际是 `s_total`）。

**预期结果**：因为 `s_total != 4*ws` 对每个 rank 都成立，`make_dims` 的交叉校验在**所有** rank 上、于第一个屏障之前失败——没有任何 rank 进入屏障等待，进程组干净退出。这也是一条工程教训：若某次误用只在**部分** rank 上触发形状矛盾（想象各 rank 分片恰好巧合相等），其余 rank 会进入屏障自旋——本地可判定的错误要抢在集合操作之前，正是 u6-l2 的纪律。

**纸面部分**：写出 `dist.all_to_all_single(output, input, output_split_sizes=..., input_split_sizes=...)` 反向传播时两个列表如何互换（发送量与接收量角色对调），然后各用一句话回答：为什么那个库必须换、这个库不能换。

**运行前提**：数值输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：偶均分时「交换」与「不交换」结果相同吗？为什么迁移代码常在测试里发现不了这个 bug？

**答案**：迁移语境里的「交换」指上游库的约定「backward 时交换 `scatter_idx` / `gather_idx`（或收发表）」，不是字面互换本库的两张表——后者在偶均分下也几乎必挂在形状校验上（交换后 `sum(seq_splits)` 与实际序列轴长不再相等）。偶均分时「交换索引」与「同一对 splits」两种约定给出**相同的字节**：均匀块的分发与汇合是对称操作，方向反转恰好互逆，谁也不需要知道切分几何。所以只用偶均分形状做迁移验证时，把上游的「交换」约定照搬过来结果照样正确；不均分一出现，两种约定分道扬镳——[docs/migration.md:153-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L153-L155) 的原话是 "Identical while shards are even, not once they are not"。本库的测试特意用倾斜切分让这个错误显形（[test/distributed/correctness.py:313](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L313)）。

**练习 2**：backward 里 `seq`/`head` 从 `saved_data` 还原时为什么必须区分 `isNone()` 与空向量？

**答案**：`std::optional` 的「调用方没传」与「传了空表」是两个语义：没传意味着走偶均分特例，由 backward 自己的输入形状反推同一对表（4.3.2 的论证保证这反推结果与 forward 一致）；空表则会被 `validate()` 拒绝。`IValue` 把「没传」编码为 None，还原时用 `isNone()` 区分，避免把「没传」误变成「传了空表」而在 backward 里抛错。

### 4.4 forward AD 双通道：tangent 走第二条集合通信

#### 4.4.1 概念说明

forward AD 与集合通信的组合有个别的库很少处理的坑：**C++ `torch::autograd::Function` 没有 jvp 钩子**。一个携带 tangent 的 dual tensor 如果直接进 `A2AFunction::apply()` 会 raise；如果走了 `with_reverse_ad` 的快速路径，tangent 会被**静默丢弃**——dual 出来时 tangent 是 None，什么都不报。本库的选择是在唯一的 Autograd 内核 `all_to_all_4d_autograd` 里**手工搬运 tangent**：

- jvp 的数学：\( \dot{y} = P\dot{x} \)——排列对自己的方向向量做的就是同一个排列。所以 tangent 走**同 mode** 的第二次集合通信。
- 代价是每层每步多一倍通信量；收益是 `torch.autograd.forward_ad`、`torch.func.jvp`、`torch.func.linearize` 全部可用。
- 集合维度：每个 rank 都跑同一张图，所以「tangent 触发的第二条集合通信」也是所有 rank 一致发出的——不违反 SPMD 契约。

#### 4.4.2 核心流程

```
all_to_all_4d_autograd(group, input, ...)
    │
    ├─ tangent = input._fw_grad(0)          # 摘出 level-0 方向
    │
    ├─ tangent 未定义（普通调用）
    │       └─ with_reverse_ad(...)          # 4.1 的路径，reverse mode 专属
    │
    └─ tangent 已定义（dual 输入）
            ├─ result = with_reverse_ad(primal)          # 原值：保留 reverse 历史
            ├─ result._set_fw_grad(with_reverse_ad(tangent))  # 方向：第二条集合通信
            └─ return result                             # 仍是 dual，primal+tangent 都对
```

注意两个分支复用 `with_reverse_ad`：tangent 与 primal 各自可能建图（tangent 若 `requires_grad`，其对偶传播也进 reverse 图）。

#### 4.4.3 源码精读

**唯一的 forward-AD 级**——[src/bindings.cc:579-580](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L579-L580)：`constexpr uint64_t kFwLevel = 0;`——torch 目前只有 level 0 这一个前向 AD 层级。

**手工搬运**——[src/bindings.cc:584-605](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L584-L605)：

```cpp
const at::Tensor tangent = input._fw_grad(kFwLevel);
if (!tangent.defined()) {
    return with_reverse_ad(group, input, mode, seq_splits, head_splits);   // 普通调用
}
at::Tensor result = with_reverse_ad(group, input._fw_primal(kFwLevel), mode, seq_splits, head_splits);
result._set_fw_grad(with_reverse_ad(group, tangent, mode, seq_splits, head_splits),
                    kFwLevel, /*is_inplace_op=*/false);
return result;
```

入口注释（[src/bindings.cc:590-594](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L590-L594)）逐条说明动机：C++ Function 无 jvp 钩子、dual 进 `apply()` 会 raise、走快速路径会被静默丢弃、排列的 jvp 是排列本身所以「tangent 走同一条调用——多一次集合通信，每个 rank 都发出，因为每个 rank 都跑这张图」。第二个细节：primal 走 `with_reverse_ad` 时**保留 reverse 历史**（注释：primal 不携带 tangent，所以 `apply()` 可以看到它）。

**可用与不可用的边界**——[docs/api.md:64-71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L64-L71) 把边界写成契约：

| 入口 | 结果 | 原因 |
| --- | --- | --- |
| `torch.autograd.forward_ad` / `torch.func.jvp` / `torch.func.linearize` | **可用**，流量 ×2 | tangent 走第二条集合通信 |
| `torch.func.grad` / `vjp` / `jacrev` | **raise** | backward 是 C++ `torch::autograd::Function`，functorch 无法 lift |
| `torch.vmap` / `jacfwd` | **raise**（显式检查） | vmap 会按 batch 逐元素循环集合通信 → rank 挂起；u6-l4 详述 |

**测试证据**——[test/distributed/correctness.py:330-346](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L330-L346)：在 `fwd_ad.dual_level()` 里把 `make_dual(x, tangent)` 喂进调用，断言三件事——jvp 非 None（tangent 没被丢）、`jvp == all_to_all_4d(tangent)`（同 mode 前向，位精确）、primal 等于普通调用的结果。注释点明负控制的意义：「没有手工搬运时 dual 出来没有 tangent 且什么都不 raise」。

**与 `out=` / async 的对照**：可微性只属于普通 functional 调用。`out=` 变体是可变 schema、无 autograd 公式，Python 侧对「grad 模式 + 需梯度输入」显式 raise（[python/fast_ulysses/group.py:133-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L133-L143)：返回值就是调用者自己的缓冲，静默丢梯度与「调用者本来就不想要梯度」无法区分，所以 raise）；async 形式因 `AsyncCollectiveTensor` 是叶节点同样 raise（[python/fast_ulysses/group.py:188-197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L188-L197)）。两者的拒绝都被 [test/distributed/validation.py:186-192](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L186-L192) 断言，`no_grad` 下的 `out=` 则被显式接受（[test/distributed/validation.py:350-357](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L350-L357)）。

#### 4.4.4 代码实践

**实践目标**：验证 tangent 确实走了第二条集合通信、primal 不受污染；同时确认 functorch 边界与文档一致。

**操作步骤**（示例代码）：

```python
# 示例代码：practice_forward_ad.py
import os
import torch
import torch.autograd.forward_ad as fwd_ad
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
ws = dist.get_world_size()
g = UlyssesGroup(require_nvlink=False)

x = torch.randn(2, 16, 4 * ws, 128, device="cuda", dtype=torch.bfloat16)
tangent = torch.randn_like(x)

with fwd_ad.dual_level():
    dual = fwd_ad.make_dual(x, tangent)
    out = g.all_to_all_4d(dual, mode=0)
    primal, jvp = fwd_ad.unpack_dual(out)

assert torch.equal(jvp, g.all_to_all_4d(tangent, mode=0))   # tangent 走了同一条路
assert torch.equal(primal, g.all_to_all_4d(x, mode=0))      # primal 不受污染
print("forward AD: jvp == a2a(tangent), primal == a2a(x)")

# torch.func.jvp 走同一条手工搬运路径（流量 ×2）
import torch.func
_, j2 = torch.func.jvp(lambda t: g.all_to_all_4d(t, mode=0), (x,), (tangent,))
assert torch.equal(j2, jvp)

# reverse functorch 的边界：应当 raise（具体消息待本地验证）
try:
    torch.func.vjp(lambda t: g.all_to_all_4d(t, mode=0), x)
    print("vjp did NOT raise -- unexpected!")
except Exception as e:
    print("vjp raises:", type(e).__name__)

g.destroy()
dist.destroy_process_group()
```

**需要观察的现象**：两个位精确断言通过；`torch.func.jvp` 与手工 dual 的结果一致；`torch.func.vjp` raise（类型与消息待本地验证，文档预告它「自己 raise」）。

**预期结果**：dual 进、dual 出，tangent 与 primal 各自等于独立的普通调用。如果某个实现丢掉了 tangent，`unpack_dual` 返回的 `jvp` 会是 `None`——correctness.py 的负控制检查的正是这个。

**运行前提**：需要 GPU 与已安装扩展；结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 tangent 的搬运放在 Autograd 内核，而不是给 `A2AFunction` 加一个 jvp？

**答案**：C++ `torch::autograd::Function` 根本没有 jvp 钩子可加（[src/bindings.cc:590-592](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L590-L592)）。dual 一旦进 `apply()` 就 raise；而若让它误入 `with_reverse_ad` 的快速路径（GradMode 关或不需梯度），tangent 被静默丢弃——最危险的失败模式是没有失败。Autograd 内核是所有调用（含快速路径）的必经之门，在这里摘 tangent 才能保证无一遗漏。

**练习 2**：`jvp` 是 `mode=0` 而不是 `mode=1`，用一句话说清与 backward 的差别。

**答案**：backward 算的是 \( P^{\top}g = P^{-1}g \)（转置＝逆，方向反转），jvp 算的是 \( P\dot{x} \)（同一个排列，方向不变）——所以 vjp 换 mode，jvp 不换。

**练习 3**：一个需要梯度的输入在 `torch.no_grad()` 下走 `out=` 调用会怎样？在 grad 模式下呢？

**答案**：`no_grad()` 下被接受（[test/distributed/validation.py:350-357](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L350-L357) 断言「accepts」）；grad 模式下在 Python 侧 raise `RuntimeError`，消息以 "does not support autograd" 开头（[python/fast_ulysses/group.py:138-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143)、[test/distributed/validation.py:186-192](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L186-L192)）。

## 5. 综合实践

把本讲四条线串成一个脚本：**「backward 是另一个 mode」的完整验证 + 双重 backward 的递归证据 + 与示例的梯度校验对照**。

**实践目标**：证明下面三件事在真实多卡（或 ws=1）环境同时成立：

1. 对同一不均匀 splits，`mode 0 → mode 1` 前向串联逐字节还原数据，且与 backward 产出的梯度逐字节一致。
2. 双重 backward（grad-of-grad）能算，且 \( L=\sum y^2 \) 的二阶导处处为 2——这只有在 backward 经 `A2AFunction::apply` 递归建图时才可能。
3. `examples/dit_attention.py` 的「输出 + 输入梯度」双校验方法与本讲的单元验证互补：前者防「接错轴」，后者防「排列本身错」。

**操作步骤**（示例代码）：

```python
# 示例代码：practice_u6l3_final.py
#   torchrun --nproc_per_node=<N> practice_u6l3_final.py
import os
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
rank, ws = dist.get_rank(), dist.get_world_size()
g = UlyssesGroup(require_nvlink=False)

s_total = 97 * ws + 13
seq_splits = [s_total - (ws - 1)] + [1] * (ws - 1)      # 倾斜：交换 splits 必挂形状
head_splits = [4] * ws
kw = {"seq_splits": seq_splits, "head_splits": head_splits}

# --- 1. 串联还原 + 与 backward 一致 -------------------------------------------------
x = torch.randn(2, seq_splits[rank], sum(head_splits), 128,
                device="cuda", dtype=torch.bfloat16)
xg = x.clone().requires_grad_(True)
up = torch.randn_like(g.all_to_all_4d(x.detach(), mode=0, **kw))
g.all_to_all_4d(xg, mode=0, **kw).backward(up)
chained = g.all_to_all_4d(g.all_to_all_4d(up, mode=0, **kw), mode=1, **kw)
assert torch.equal(xg.grad, chained)
print(f"rank {rank}: [1] backward == mode1(mode0(up)), bit-exact")

# --- 2. 双重 backward：sum(y**2) 的二阶导是 2 ---------------------------------------
# 递归链条：A2AFunction::backward 里调的是 A2AFunction::apply（不是裸函数），
# 所以一阶梯度 g1 = 2x 仍带 grad_fn，第二次 grad 能再穿一层排列节点。
# 数学：g1 = P^T (2 P x) = 2x  =>  dg1/dx = 2I。
xd = torch.randn(2, 16, 4 * ws, 128, device="cuda", dtype=torch.bfloat16,
                 requires_grad=True)
(first,) = torch.autograd.grad((g.all_to_all_4d(xd, mode=0) ** 2).sum(), xd,
                               create_graph=True)
assert first.grad_fn is not None, "一阶梯度必须仍在图上，否则 apply() 递归没发生"
(second,) = torch.autograd.grad(first.sum(), xd)
assert torch.allclose(second.float(), torch.full_like(second.float(), 2.0))
print(f"rank {rank}: [2] double backward == 2 everywhere")

g.destroy()
dist.destroy_process_group()
```

**步骤 3（阅读对照）**：打开 [examples/dit_attention.py:52-62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L52-L62)——模块里两行注释各自写着「mode 0: ... Differentiable; its backward is mode 1」与「mode 1: ... Its backward is mode 0」，正是本讲结论在用户代码里的样子；再看 [examples/dit_attention.py:96-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L96-L116)：梯度经 `all_gather` 拼回全局后与单卡参照比 `allclose`（容差 5e-2，因为 SDPA 与投影的归约顺序不同）。写三行注释回答：为什么示例用 `allclose` 而本讲的单元断言用 `torch.equal`？

**需要观察的现象**：两个断言通过；示例跑出 `d/dinput max |diff|` 小于容差的 OK 行。

**预期结果**：单元断言位精确（纯排列不引入算术误差）；示例的误差来自注意力而非交换。如果第 2 步的 `first.grad_fn is None`，说明 backward 没走 `apply()`——正是源码注释（[src/bindings.cc:554-556](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L554-L556)）要防住的退化。

**运行前提**：需要已构建扩展与 CUDA 设备；所有数值输出待本地验证。

## 6. 本讲小结

- `A2AFunction` 是 C++ `torch::autograd::Function`：`saved_data` 保存 group（图持有其生命周期）、mode 与两张 splits 表（`IValue` 的 None 语义保留「没传」），forward 经 `dispatch_below_autograd` 重入调度器以保住 Meta/Python key。
- 排列的 vjp 是逆排列（\( P^{\top}=P^{-1} \)），而逆排列就是同一几何下另一个 mode 的前向——所以 backward 是 `1 - mode`，位精确、不重算。
- splits **不交换**：`all_to_all_single` 的 split sizes 描述单次调用的收发量（方向反转即角色互换），本库的两张表描述组的切分几何（与流向无关）；倾斜切分下交换会在 `make_dims` 的形状校验上失败。
- backward 永走 copying 路径：`empty_output()` 是集合分配（rendezvous 位置不能交给引擎的节点顺序），且梯度会被引擎累加/持有，与单缓冲窗口不兼容；用 `apply()` 而非裸函数，使 grad-of-grad 递归回 `A2AFunction`。
- forward AD 在唯一的 Autograd 内核里手工搬运 tangent：jvp 是同一个排列，tangent 走第二条集合通信（流量 ×2）；`forward_ad`/`jvp`/`linearize` 可用，`grad`/`vjp`/`jacrev` 因 functorch 无法 lift C++ Function 而 raise，`vmap`/`jacfwd` 被显式拒绝。
- `out=` 与 async 形式不可微且**拒绝而非静默丢梯度**：返回值分别是调用者的缓冲与叶节点 wrapper，静默通过会让 `x.grad` 悄悄变 `None`。

## 7. 下一步学习建议

- **u6-l4（Meta 内核、FakeTensor 与 vmap 拒绝）**：本讲多次遇到「哪些入口可 trace」——下一讲系统讲 Meta 内核如何复用 `group->plan()` 做纯主机形状传播，以及 `FuncTorchBatched + Batched` 双 key 拒绝 vmap 的完整论证。
- **u7-l1（异步调用全景）**：本讲确认了 async 形式不可微的原因（`AsyncCollectiveTensor` 是叶），第七单元讲它换来的东西——comm 流上的重叠与 `record_stream` 记账。
- **源码延伸阅读**：把 [test/distributed/correctness.py:296-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L358) 完整读一遍（backward、splits 传递、forward AD、双重 backward 四段连续出现），再对照 [test/test_plan.py:236-257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L257) 的无 GPU round trip——「GPU 上的位精确」与「主机上的重放还原」互为表里，是本库测试哲学的缩影。
