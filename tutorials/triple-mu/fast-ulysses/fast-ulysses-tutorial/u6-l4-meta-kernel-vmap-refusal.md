# Meta 内核、FakeTensor 与 vmap 拒绝

## 1. 本讲目标

前几讲走通了一次调用的完整流水线（u6-l2）和自动微分（u6-l3）。本讲回答 torch 集成的最后一组问题：当 torch 的**追踪生态**——FakeTensor、AOTAutograd、torch.compile、vmap——遇到这个算子时会发生什么。学完本讲你应该能：

1. 解释 `all_to_all_4d` 的 Meta 内核为什么直接复用 `group->plan()`，而不是在 Python 里重写一份形状规则；
2. 对本库六个 op 逐个说出「可 trace / 拒绝 trace」的分类，以及每条拒绝背后的具体危害（集合分配错位、work registry 残留、真实流句柄被烘进图、假测量值）；
3. 复述 vmap 的失败模式为什么是「挂起」而不是「报错」——没有 batching rule 时 vmap 静默回退成按 batch 元素的 for 循环，各 rank 因此发出**不同次数**的集合通信——以及库如何用 `FuncTorchBatched` + `Batched` 两个 dispatch key 把它变成显式报错。

## 2. 前置知识

本讲站在 u6-l1（算子注册与 dispatch key）和 u6-l3（autograd 内核）的肩膀上，再补四个 torch 生态概念。不需要 GPU 也能读懂源码部分；实践部分单卡即可完成大部分。

- **Meta key 与 meta 张量**：torch 调度器里有一个专门做「形状与 dtype 运算」的 dispatch key，叫 Meta。挂在它下面的内核拿到的张量只有形状、dtype、步长——没有数据，也没有设备内存（`at::empty` 在 Meta dispatch 下只登记元数据）。凡是「不碰设备就能算出输出形状」的 op 都可以也应该提供 Meta 内核。
- **FakeTensor**：`torch._subclasses.fake_tensor` 提供的张量子类，内部包一个 meta 张量、外带一个「我假装在哪个设备」的标记。在 `FakeTensorMode` 上下文里创建的张量都是 FakeTensor；对它们调用 op 时，调度最终落到 Meta 内核。这正是 torch.compile / AOTAutograd 做**无设备试运行（trace）**的机制：整个模块的形状流转被推演一遍，一次 CUDA 都不发生。
- **vmap 与 batching rule**：`torch.func.vmap` 把「对一个样本的函数」变成「对一批样本的函数」。它依赖每个被包裹的 op 有向量化规则（batching rule）；**没有规则的 op 不会报错，而是静默回退为沿 batch 维的 for 循环**——一次调用悄悄变成每 batch 元素各一次。这个「静默」对普通 op 只意味着慢，对一个集合通信 op 意味着挂起（4.4 详述）。
- **torchbind 自定义类**：承接 u6-l1——`UlyssesGroup` 经 `m.class_` 注册为 torch 自定义类，在 schema 里以 `__torch__.torch.classes.fast_ulysses.UlyssesGroup` 类型出现，Python 持有的是它的 ScriptObject 包装。关键事实：`FakeTensorMode` 的参数转换**只碰张量**，非张量参数原样通过——torchbind 对象可以原封不动地进入 Meta 内核。
- **dispatch key 优先级**：承接 u6-l1——直接注册在某个 key 上的内核优先于别名键（`CompositeExplicitAutograd` 虽覆盖 Meta，但显式 Meta 注册胜过它）；张量子类的 key（如 `FuncTorchBatched`）排在 Autograd 之前。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | 本讲主场：全部六个 Meta 内核、`refuse_vmap` 拒绝内核、`refuse_batching` 注册器都在这里 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | Tracing 一节：为什么不给 group 注册 fake class、trace 边界的设计论证 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) | 把这些行为写成契约：meta 内核、vmap/jacfwd/jacrev 各自的下场 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | `all_to_all_4d_async` 的 Python 包装——staged 拒绝 trace 的理由全部指向这段 eager-only 代码 |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp) / [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `plan()` 的声明与实现：Meta 内核复用的那段纯主机代码 |
| [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py) | 多卡上断言每条 vmap 拒绝都真的 raise、且各 rank 一致 |

先给全结论总表，后面四个模块分别展开：

| op | Meta key 下的行为 | 一句话理由 |
| --- | --- | --- |
| `all_to_all_4d` | **真形状传播**（复用 `group->plan()`） | plan 是纯主机代码，不碰设备 |
| `all_to_all_4d_out` | **真校验**（检查 `out` 形状/dtype，不推导） | 结果就是调用者的缓冲，形状只能核对不能发明 |
| `empty_output` | **拒绝** | 集合分配，rendezvous 的位置不能交给图 |
| `all_to_all_4d_staged` / `_staged_out` | **拒绝** | 完成事件挂 work registry、真实流句柄做参数 |
| `all_to_all_4d_timed` | **拒绝** | 返回值一半是测量，trace 无可测之物 |

## 4. 核心概念与源码讲解

### 4.1 Meta 内核：形状传播复用 plan()

#### 4.1.1 概念说明

torch 的追踪生态（FakeTensor、AOTAutograd、`torch.export`）推演一个模块时，靠每个 op 的 Meta 内核回答「输出长什么样」。没有 Meta 内核的自定义 op 会发生什么？本库的 `all_to_all_4d` 注册在 `CompositeExplicitAutograd` 这个**别名键**上，而别名键连 Meta 一起覆盖——于是一个 fake 张量会一路落到**真实内核**里，死在 `require_cuda` 上：报错说的是「这不是 CUDA 张量」，而真正的问题是「这个 op 在 trace 里跑不了」。这是一条误导性的错误消息。

所以本库给 functional 形式显式注册了 Meta 内核。它要回答的问题是：给定 \((b, s_{local}, n_{global}, d)\) 与 mode、splits，输出形状是什么？——这恰好是 u3 里 `build_plan` 已经回答过的问题。于是 Meta 内核只有一行实质内容：调 `group->plan()`，取 `output_shape`，`at::empty` 出一个 meta 张量。

为什么不在 Python 里重写一份形状规则？因为不均匀分片的规则（splits 与形状的交叉校验、整除性反推、`window_numel` 取全组最大）有一整套细节，重写意味着**第二份实现**——一份在 C++ 里被 `test_plan.py` 覆盖，另一份在 Python 里谁也够不着。design.md 把这一点讲得很直白：a2a_plan.cc 之所以刻意做成无 torch、无 CUDA 的翻译单元，就是为了让这份规则只有一份、且无 GPU 也能测。

还有两个精妙的配合：

1. **group 是真实对象**。Meta 内核的 schema 第一个参数是 torchbind 的 `UlyssesGroup`；`FakeTensorMode` 的转换只碰张量，ScriptObject 原样到达——于是 `world_size` 可用。这很关键：不传 splits 时，输出形状里的 \(s_{global} = ws \cdot s_{local}\) 依赖 world_size，而它**无法从输入形状单独推出**。
2. **autograd 路径也通到 Meta**。u6-l3 讲过 Autograd 内核经 `dispatch_below_autograd` 重入调度器；正是这次重入让「Python、FakeTensorMode、Meta」这些位于 Autograd 之下的 key 仍然被咨询——fake 张量穿过 autograd 内核后照样落到 Meta 内核，而不是坠入真实内核。

#### 4.1.2 核心流程

一次 FakeTensor 调用的调度路径（伪代码）：

```text
with FakeTensorMode():
    y = group.all_to_all_4d(fake_x, mode=0)
        └─ python/group.py → torch.ops.fast_ulysses.all_to_all_4d(handle, fake_x, ...)
             └─ 调度器：fake_x 是张量子类 → 走 Meta 语义
                 └─ 显式 Meta 内核 all_to_all_4d_meta          # 胜过 CompositeExplicitAutograd 别名
                     ├─ group->plan(sizes, mode, dtype, seq, head)
                     │    ├─ 查 plans_ 缓存（纯主机 std::map）
                     │    └─ 未命中 → make_dims（全部形状/dtype/splits 校验）→ build_plan
                     └─ at::empty(plan.output_shape, ...)        # Meta 下只登记元数据
返回 FakeTensor：形状正确，无设备内存，无集合通信
```

输出形状由切分几何决定：mode 0 输出为 \((b, \sum_p \text{seq\_splits}[p],\ \text{head\_splits}[rank],\ d)\)。

#### 4.1.3 源码精读

Meta 内核本体与它的理由注释（值得整段读）：

[src/bindings.cc:272-293](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L272-L293)——注释说明了三件事：`CompositeExplicitAutograd` 已覆盖 Meta、显式注册优先于别名、**算术留在 `group->plan()` 里**（纯主机、不在设备上分配、顺路跑完所有形状与 splits 校验；在 Python 重写会把第二份规则放到 `test_plan.py` 够不着的地方）。最后四行注释点出 group 作为真实对象到达、`world_size` 因此可用。

`plan()` 本体——Meta 路径复用的正是这份纯主机缓存逻辑：

[src/group.cc:167-196](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L167-L196)——以 `(sizes, mode, dtype, seq, head)` 为键查 `std::map`，未命中才 `make_dims` + `build_plan`。全程只有堆上的 `std::vector` 分配，「几微秒的主机时间」。注意一个副作用：**Meta 调用也会填充 plan 缓存**——它是主机态，无害，且让随后的 eager 调用直接命中。

注册处，以及一个刻意的「不做」：

[src/bindings.cc:717-726](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L717-L726)——第 725 行显式挂 Meta。注释解释为什么**不同时**用 `torch.library.register_fake`：在 torch 2.13 上它会静默替换已存在的 Meta 内核，留下两份形状规则、且没有报错说明跑的是哪一份。

autograd 重入与 Meta 的关系（承接 u6-l3）：

[src/bindings.cc:470-477](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L470-L477)——「若直接调 `all_to_all_4d()` 就永远离开了调度，Autograd 之下的一切 key——Python、经它的 FakeTensorMode、Meta——都不会被咨询」。

out 变体的 Meta 内核是「校验」而非「推导」：

[src/bindings.cc:332-355](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L332-L355)——调用者的缓冲就是结果，形状只能对照 `plan->output_shape` 核对（`out has shape ...` / `out has dtype ...`），没有东西可返回。

design.md 的 Tracing 一节给出整体边界：**形状传播可用，Dynamo 不可用**——

[docs/design.md:117-128](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L117-L128)——group 是 torchbind 对象、没有注册 fake class，Dynamo 无法在图里表示它、于是在调用处 graph-break；注册一个意味着给 group 一个有意义的 fake——窗口、peer 地址、背后的 rendezvous——这些状态是 trace 无法代言的。而 functional op 的显式 Meta 内核让 FakeTensor 与 AOTAutograd 不碰设备就看到输出形状，backward 也像普通 op 一样可 trace。

#### 4.1.4 代码实践（示例代码）

目标：亲手验证 Meta 内核做了形状传播、没碰设备、校验照常触发。

```python
# u6_l4_meta_probe.py —— 需要至少 1 块 GPU；形状变化需 ws>=2，建议：
#   torchrun --nproc_per_node=2 u6_l4_meta_probe.py
import os

import torch
import torch.distributed as dist
from torch._subclasses.fake_tensor import FakeTensor, FakeTensorMode

from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
ws = dist.get_world_size()
group = UlyssesGroup()

b, s, n, d = 2, 16, 4 * ws, 128
before = torch.cuda.memory_allocated()

with FakeTensorMode():
    xf = torch.empty(b, s, n, d, dtype=torch.bfloat16, device=group.device)  # FakeTensor
    yf = group.all_to_all_4d(xf, mode=0)
    print("类型:", type(yf).__name__)      # 预期: FakeTensor
    print("mode0 形状:", tuple(yf.shape))  # 预期: (2, 16 * ws, 4, 128)
    print("mode1 形状:", tuple(group.all_to_all_4d(yf, mode=1).shape))  # 预期: 回到 (2, 16, 4*ws, 128)
    # 校验在 Meta 路径照常触发：make_dims 在 plan() 里被同一条代码路径执行
    try:
        group.all_to_all_4d(torch.empty(2, 16, 128, dtype=torch.bfloat16, device=group.device), mode=0)
    except RuntimeError as e:
        print("3D 输入被拒:", "must be 4D" in str(e))  # 预期: True

print("设备内存增量:", torch.cuda.memory_allocated() - before)  # 预期: 0
group.destroy()
dist.destroy_process_group()
```

观察要点与预期结果：

1. `yf` 是 `FakeTensor`，形状符合 mode 0 的映射；注意 ws=1 时输入输出形状恒相同（双射退化为恒等），所以「形状真的变了」要在 ws≥2 下看。
2. 设备内存增量为 0——Meta 内核没有在 GPU 上分配任何东西。
3. 3D 输入在 fake 世界里被和 eager 世界同一条规则拒绝，证明复用的是同一份 `make_dims`。
4. 全程没有任何屏障/传输发生（没有 GPU 集合通信被执行）。

以上运行结果为基于源码的预期，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `m.impl("all_to_all_4d", c10::DispatchKey::Meta, ...)` 这一行，`FakeTensorMode` 下的调用会怎样？
**答案**：fake 张量会经 `CompositeExplicitAutograd` 别名键落到真实内核 `all_to_all_4d`，死在 `require_cuda` 的「input must be a CUDA tensor」上——一条关于 CUDA 张量的报错，掩盖了真正的问题（op 在 trace 里不可运行）。这正是 [src/bindings.cc:272-275](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L272-L275) 注释描述的场景。

**练习 2**：为什么 Meta 内核必须拿到真实的 group 对象，而不能只靠输入形状算输出？
**答案**：不传 splits 时输出形状含 \(s_{global} = ws \cdot s_{local}\)（mode 0）或 \(n_{local} = n_{global}/ws\)（mode 1 的逆），都要用到 world_size；它不在输入形状里。而 `FakeTensorMode` 只转换张量参数，torchbind 的 ScriptObject 原样通过，`group->world_size()` 因此可用（[src/bindings.cc:281-284](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L281-L284)）。

**练习 3**：Meta 调用会不会污染 group 的状态、影响后续 eager 调用？
**答案**：唯一副作用是填充 `plans_` 缓存（[src/group.cc:182-195](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L182-L195)）——纯主机 `std::map`，不含窗口、流或设备资源；后续同键 eager 调用直接命中，反而省了几微秒。

### 4.2 empty_output 与 timed：能算出形状 ≠ 应该返回形状

#### 4.2.1 概念说明

`empty_output` 和 `all_to_all_4d_timed` 的输出形状**完全可以算出来**——前者就是 `all_to_all_4d` 的输出形状，后者是 `(输出张量, 四个阶段耗时)`。但两者的 Meta 内核都选择**抛错拒绝**，而不是返回「半个答案」。

理由是一个贯穿本库的原则：**返回形状不是半个答案，而是错误答案**。

- `empty_output` 是**集合分配**（u4-l1：`empty_strided_p2p` + `rendezvous`，所有 rank 必须在各自程序的同一时刻到达同一分配）。如果它被 trace 进图里，重放时 rendezvous 会发生在**图碰巧放置它的地方**，而不是每个 rank 自己程序到达的位置——各 rank 位置一旦错位就是挂起。所以错误消息直接说：在被 trace 的区域之外**急切地（eagerly）**调用它，把缓冲传进去。
- `all_to_all_4d_timed` 返回值的一半是**测量**：四个 CUDA 事件夹出的耗时。被 trace 的调用不执行任何东西，也就无可测量。若 Meta 内核返回 `(形状, [0,0,0,0])`，读到的人拿到四个「单位和形状都像真的」的零——一个把这些数字平均进去的 benchmark 会报告一次「耗时为零的调用」。零不是缺失的答案，是错误的答案。

这条原则与 4.1 并不矛盾：`all_to_all_4d` 的输出形状是调用**语义的一部分**，传播它等于传播真相；而这两个 op 的语义里含有 trace 世界不存在的东西（一次集合分配、一次物理测量），传播形状等于撒谎。

#### 4.2.2 核心流程

```text
FakeTensorMode 下调用 empty_output(x) / group._timed(x)
  └─ 调度器 → Meta key
      ├─ empty_output_meta:  TORCH_CHECK(false, "…collective allocation and cannot be traced…")
      └─ timed_meta:         TORCH_CHECK(false, "…returns CUDA event timings…")
两者都在触碰任何设备之前抛出；错误消息给出替代方案
（eager 调用；形状请用 all_to_all_4d 的 Meta 内核拿）
```

#### 4.2.3 源码精读

`empty_output` 的拒绝内核：

[src/bindings.cc:295-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L295-L308)——「重放 trace 会把 rendezvous 放到图放置它的地方，而不是每个 rank 自己程序到达的位置。说出来，而不是让它落到一条 CUDA 张量的抱怨上」。注意函数签名刻意不命名参数：它根本不看它们。

`timed` 的拒绝内核与理由：

[src/bindings.cc:658-674](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L658-L674)——「这个 op 返回的东西一半是测量，被 trace 的调用没有东西可测。零向量不是缺失的答案而是错误的答案……形状的那一半可以从 `all_to_all_4d` 拿到，它搬同样的字节」。

注册处（含一个与 trace 无关但同属「不做半答案」的决定）：

[src/bindings.cc:753-772](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L753-L772)——两个 op 都挂 Meta 拒绝内核，且 Autograd key 走 `makeFallthrough()`：新缓冲不应对其形状来源的张量产生数据依赖、也不该继承 `requires_grad`（u4-l3 已讲）。

api.md 把这条契约写给用户：

[docs/api.md:60-62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L60-L62)——「A **meta** kernel propagates shapes under FakeTensor and AOTAutograd… `empty_output()` refuses to be traced at all, with a message saying why」。

#### 4.2.4 代码实践（示例代码）

目标：亲手触发两条拒绝，核对消息与源码一致；再验证「形状的那一半」确实可从 `all_to_all_4d` 拿到。

```python
# 接 4.1.4 的环境（group、ws 已就绪）
with FakeTensorMode():
    xf = torch.empty(2, 16, 4 * ws, 128, dtype=torch.bfloat16, device=group.device)
    for name, fn in [
        ("empty_output", lambda: group.empty_output(xf, mode=0)),
        ("timed", lambda: group._timed(xf, mode=0)),
    ]:
        try:
            fn()
            print(name, ": 竟然被接受了——回归！")
        except RuntimeError as e:
            print(name, "拒绝片段命中:", "cannot be traced" in str(e))  # 预期均为 True
    # 对照组：同一形状，functional op 正常传播
    print("functional 形状:", tuple(group.all_to_all_4d(xf, mode=0).shape))
```

预期结果：两条 `RuntimeError` 的消息分别含 `collective allocation and cannot be traced` 与 `all_to_all_4d_timed cannot be traced`（[src/bindings.cc:304-307](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L304-L307)、[src/bindings.cc:671-673](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L671-L673)），且 functional 的形状传播不受影响。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `empty_output_meta` 的拒绝理由对 `all_to_all_4d` 不成立——它不是也会在首次调用新形状时触发集合窗口分配吗？
**答案**：会，但那是**eager 首次调用**的分配，由 rank 自己的程序位置决定，SPMD 契约保证各 rank 对齐；被 trace 的是「重放位置由图决定」的分配。Meta 内核只传播形状、不执行调用，也就不会触发任何分配——`all_to_all_4d_meta` 只调 `plan()`（主机态）。危险仅在「分配本身成为被 trace 的效果」时存在，`empty_output` 的全部语义就是分配。

**练习 2**：假如要给 `empty_output` 提供 Meta 内核，正确的做法是什么？
**答案**：没有正确的「内核内」做法——集合分配不能在 trace 里发生。正确做法就是现在的设计：拒绝并指路（eager 调用、把缓冲作为 `out=` 传进被 trace 的区域），由调用方在图外完成分配。

### 4.3 staged 一对：work registry 与真实流句柄进不了图

#### 4.3.1 概念说明

`all_to_all_4d_staged` / `all_to_all_4d_staged_out` 是 `all_to_all_4d_async` 的设备侧一半（u7 会展开它们的 eager 形态）。它们**完全算得出输出形状**——搬的字节与 `all_to_all_4d` 一模一样——但 Meta 内核同样拒绝，且拒绝理由比 `empty_output` 更具体，分三层：

1. **完成机制无法重放**。这两个 op 只会经 `all_to_all_4d_async` 的 Python 包装到达，而那段包装（[python/fast_ulysses/group.py:198-232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L198-L232)）是纯 eager 的：op 返回后，包装继续在 comm 流上**记录完成事件**、交给 torch 的 work registry（进程全局）、再对结果 `record_stream`。若 op 在 trace 里「成功返回形状」，包装就会往下走——于是一个被 trace 的调用在进程全局 registry 里留下一个**永远没人等待的真实 Work**，并返回一个 `AsyncCollectiveTensor`，其 `wait()` 声称一次从未发出的传输。
2. **参数本身就是真实世界的锚点**。schema 里的 `caller_stream_id` / `caller_device_index` 命名一条**活着的 CUDA 流**（调用者当时的流）。把它烘进图里，图在别的上下文重放时引用的就是一条错误的流——处处皆错。
3. **形状那一半另有出处**：`all_to_all_4d`（或其 out 形式）搬同样的字节，trace 它即可。

这与 CUDA graph 的捕获边界（u5-l3）互为印证：async 路径因跨图事件依赖本来不可捕获；这里是从 torch trace 生态一侧给出同一结论。

#### 4.3.2 核心流程

```text
FakeTensorMode 下调用 group.all_to_all_4d_async(fake_x)
  └─ python 包装：取 current_stream → 进入 comm 流上下文 → 调 staged op
      └─ 调度器 → Meta key → all_to_all_4d_staged_meta
          TORCH_CHECK(false, "all_to_all_4d_async cannot be traced: …work registry…
                      …takes the caller's stream as an argument…")
  ↳ 抛错发生在包装记录完成事件之前——registry 里什么都不留
```

若没有这个拒绝内核会发生什么（推演）：staged op 经别名键落入真实内核 → `require_cuda` 报 CUDA 张量错误（误导）；而若有人「修好」它返回形状，则上面的三层危害逐一兑现。

#### 4.3.3 源码精读

staged 拒绝内核与完整的理由注释：

[src/bindings.cc:425-451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L425-L451)——「在这里返回形状不是半个答案，是错误答案」；随后逐条列出 registry 残留、`AsyncCollectiveTensor.wait()` 的虚假承诺、流句柄被烘进图；结尾指路「Trace all_to_all_4d instead, which moves the same bytes」。

out 形式的同款拒绝（完成事件注册在 `out` 上）：

[src/bindings.cc:453-468](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L453-L468)——消息唯一差别是指路到 `all_to_all_4d(out=...)`。

被保护的那段 eager-only 包装：

[python/fast_ulysses/group.py:198-232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L198-L232)——`caller = torch.cuda.current_stream(self.device)`（真实流，即 schema 里那两个整数的来源）、comm 流上下文、`_C.register_stream_completion`（往 work registry 注册）、两次 `record_stream`。

schema 本身把流句柄写进签名：

[src/bindings.cc:739-751](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L739-L751)——`int caller_stream_id, int caller_device_index`；两个 Meta 拒绝内核就挂在这里。

#### 4.3.4 代码实践（示例代码）

目标：经 Python 包装触发 staged 拒绝，并分别记录带 `out=` 与不带的消息差异。

```python
# 接 4.1.4 的环境
out_buf = group.empty_output(torch.empty(2, 16, 4 * ws, 128, dtype=torch.bfloat16, device=group.device), mode=0)
with FakeTensorMode():
    xf = torch.empty(2, 16, 4 * ws, 128, dtype=torch.bfloat16, device=group.device)
    for name, fn in [
        ("async", lambda: group.all_to_all_4d_async(xf, mode=0)),
        ("async out=", lambda: group.all_to_all_4d_async(xf, mode=0, out=out_buf)),
    ]:
        try:
            fn()
        except RuntimeError as e:
            msg = str(e)
            print(name, "命中:", "all_to_all_4d_async cannot be traced" in msg)
            print(name, "指路:", "Trace all_to_all_4d(out=...) instead" in msg)
```

预期结果：两条都 raise；消息分别指路到 `Trace all_to_all_4d instead` 与 `Trace all_to_all_4d(out=...) instead`（对应 [src/bindings.cc:447-450](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L447-L450) 与 [src/bindings.cc:463-467](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L463-L467)）。注意 `out=` 那组里 `empty_output` 是在 fake 上下文**之外**eager 调用的——这本身正是 4.2 教的正确用法。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「被 trace 的 staged 调用会在 work registry 里留下一条没人等待的记录」是危害而不是噪音？
**答案**：registry 按 storage 键把 Work 绑到那次调用的结果上（u7-l3）；下个使用该结果的 aten op 会尝试等待它。一条记录对应的传输从未发出，等待的语义就是假的；同时进程全局 registry 里累积死条目（api.md 也提醒：被丢弃的 async 结果会在退出时被 torch 计数打印）。

**练习 2**：`caller_stream_id` 为什么必须由 Python 侧算好、以整数进 schema，而不是让 C++ 内核自己取当前流？
**答案**：包装里 `torch.cuda.current_stream(self.device)` 取的是**调用者**的流，而 op 是在 `with torch.cuda.stream(self._comm_stream)` 上下文里被调用的——C++ 里取「当前流」拿到的是 comm 流。同时这个整数进 schema 的事实正是它不能被 trace 的原因之一：图重放时这个常数指向的流未必还存在/还是那条流。

### 4.4 vmap 双 key 拒绝：把「挂起」翻译成「报错」

#### 4.4.1 概念说明

先复述失败模式。`torch.vmap` 包裹一个函数时，若某个 op 没有向量化规则，vmap **不报错**，而是静默回退为沿 batch 维的 for 循环。对一个普通 op 这只是慢；对一个集合通信 op，这是本库文档里喊得最响的失败：

- 设 rank A 的 batch 维是 3、rank B 是 5。vmap 回退后，A 发出 3 次 `all_to_all_4d`、B 发出 5 次。
- SPMD 契约（u1-l2、u2-l2）要求**每个 rank 发出相同的调用序列**——第 4 次调用时 A 已经跑完，B 还在等一个永远不会来的对端。
- 结果是**挂起**：没有异常、没有超时、GPU 卡在自旋屏障内核里（u5-l3 的那类内核），唯一症状是任务不动了。

对会分配的 op 还有一条：循环的每次迭代都会 rendezvous 一次（`empty_output`），同样的挂起多了一个入口。

库的对策是把「失败应该可见」做成调度器的属性：在 `FuncTorchBatched` 和 `Batched` 两个 dispatch key 上注册一个**只会抛错**的内核。为什么是两个 key？

- `FuncTorchBatched` 是现代 `torch.vmap` 用的 key。它排在 Autograd 之前，所以同一个注册顺带覆盖了「对 requires_grad 输入做 vmap」和嵌套 vmap。
- `Batched` 是**遗留** vmap 的 key，今天仍有受支持的公共 API 走它：`torch.autograd.grad(..., is_grads_batched=True)`——以及经它到达的 `torch.autograd.functional.*` 的 `vectorize=True`——走的是 `torch._vmap_internals` 而非 functorch。只注册第一个 key，这些入口仍会静默循环：**backward 会成为按行循环集合通信的那一半**。

还有一个刻意保留的合法角落：若传给集合通信的张量只是 vmap 函数闭包里捕获的**常量**（不是被 batch 的张量），batching key 根本不在该次调度的 key 集合里，拒绝内核不触发——「在别人的 vmap 里恰好调用一次」是合法且正确的行为。

#### 4.4.2 核心流程

```text
torch.vmap(f)(batched_x)                     # batched_x 是 BatchedTensor
  └─ 对 op 的调用带上 FuncTorchBatched key
      ├─ 无注册：functorch 找不到 batching rule → 回退 for 循环
      │    └─ f(x_i) 逐元素执行 → b 次集合通信 → 各 rank 次数不一致 → 挂起（无报错）
      └─ 有注册（现状）：显式内核胜过兜底 → TORCH_CHECK(false, "… does not support torch.vmap …")
           ↳ 错误消息解释原因并指路：在 vmap 之外对完整张量调用一次；
             想按 batch 处理就把 batch 维折进 b（集合通信本来就带 b 轴）

torch.autograd.grad(…, is_grads_batched=True)  # 遗留路径
  └─ torch._vmap_internals → Batched key → 同一个拒绝内核
```

注册覆盖全部六个 op（[src/bindings.cc:726](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L726)、[L737](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L737)、[L744](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L744)、[L751](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L751)、[L763](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L763)、[L772](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L772)）。

#### 4.4.3 源码精读

拒绝内核本体（一个内核服务所有 op）：

[src/bindings.cc:50-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L50-L67)——开头注释完整给出挂起论证（「batch 大小不一致的 rank 会发出不同**次数**的集合通信并挂起，什么都不 raise、什么都不超时」；「对会分配的 op 循环还意味着每次迭代 rendezvous」）。实现细节：**boxed** 内核（签名是 `(OperatorHandle, Stack*)`），因为它要从 `op.schema()` 取 op 名拼进消息——一个内核给六个 op 各自的报错；注释说明原因：`torch::CppFunction` 没有 `makeError()`。消息末尾点名 `torch.autograd.grad(is_grads_batched=True)` 也经同一循环到达这里。

注册器与「两个 key」的完整理由：

[src/bindings.cc:700-715](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L700-L715)——`FuncTorchBatched` 赢过 op 的 Autograd 内核（覆盖 grad 输入的 vmap 与嵌套 vmap）；`Batched` 是遗留 key，`is_grads_batched=True` 经 `torch._vmap_internals` 到达；「只注册第一个会让那条路径回到按行循环」；闭包常量不触发任何一格、保持合法。

out 变体上的一个「多此一举」及其理由：

[src/bindings.cc:734-737](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L734-L737)——torch 自己的 for 循环兜底**已经**拒绝 out 变体，但那是 torch 为自己的理由做的拒绝；「哪天 torch 长出 out= 的 batching 兜底，这个 op 就会开始静默循环。一行代码把它变成这个库的属性」。

`empty_output` 上的注册理由：

[src/bindings.cc:762-763](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L762-L763)——「循环会每个 batch 元素 rendezvous 一次，同一个挂起多了一个入口」。

多卡验证（每个拒绝都检查「真的 raise 了」且各 rank 一致）：

[test/distributed/validation.py:194-267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L194-L267)——六项 functorch 入口（functional / out= / async / async out= / empty_output / timed）加一项**遗留 key** 入口（`is_grads_batched=True`）；第 196-200 行注释点明测试哲学：「检查的是调用 **raise**；这里若显示 accepted 意味着循环回来了」。第 251-256 行解释为什么最后一项不可省：上面六项全走 functorch，只注册 `FuncTorchBatched` 时它们照样全过——**挂起来的会是 backward**。

api.md 给用户的分类（承接 u6-l3 的 AD 边界）：

[docs/api.md:64-71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L64-L71)——`vmap` 由显式检查 raise（按 batch 元素循环、batch 不一致的 rank 挂起）；`jacfwd` 经同一检查 raise（它是 vmap 套 jvp）；`grad` / `vjp` / `jacrev` 则是 functorch 自己 raise（无法 lift C++ autograd 节点）；指路：把 batch 轴 reshape 进 `b`。

#### 4.4.4 代码实践（示例代码）

目标：记录三类入口的报错，区分「本库的拒绝」与「functorch 的拒绝」；并写下挂起论证。

```python
# 接 4.1.4 的环境（ws >= 1 均可；拒绝与拓扑无关）
dev = group.device
x = torch.randn(2, 16, 4 * ws, 128, dtype=torch.float32, device=dev)
batched = torch.randn(3, 2, 16, 4 * ws, 128, dtype=torch.float32, device=dev)

# 1) torch.vmap：本库的显式拒绝
try:
    torch.vmap(lambda t: group.all_to_all_4d(t, mode=0))(batched)
except RuntimeError as e:
    print("[vmap]", "does not support torch.vmap" in str(e))   # 预期: True
    print(str(e)[:200])                                        # 开头应为 fast_ulysses.all_to_all_4d does not support torch.vmap…

# 2) 遗留 key：is_grads_batched=True 走 torch._vmap_internals
src = x.detach().requires_grad_(True)
out = group.all_to_all_4d(src, mode=0)
g = torch.stack([torch.randn_like(out) for _ in range(3)])
try:
    torch.autograd.grad(out, src, g, is_grads_batched=True)
except RuntimeError as e:
    print("[legacy]", "does not support torch.vmap" in str(e)) # 预期: True

# 3) jacrev：functorch 自己的拒绝（不是 refuse_vmap）
try:
    torch.func.jacrev(lambda t: group.all_to_all_4d(t, mode=0).sum())(x)
except Exception as e:
    print("[jacrev] 类型:", type(e).__name__, "| 本库消息?", "does not support torch.vmap" in str(e))
    # 预期: 抛错，但消息来自 functorch（无法 lift C++ Function 节点），不含本库的拒绝话术
```

然后在脚本旁写一段注释回答（这是本实践的交付物之一）：

> **为什么没有拒绝内核时 vmap 的失败模式是挂起而不是报错？** vmap 对无 batching rule 的 op 静默回退成 for 循环，循环里的每次调用都拿到普通（非 batch）张量、正常调度到真实内核——于是 rank r 一共发出 \(b_r\) 次集合通信。SPMD 契约要求各 rank 发出相同调用序列；\(b_r\) 不同则从第 \(\min_r b_r + 1\) 次起对端缺失，屏障内核永远等不到 flag，无异常、无超时。**库如何把它变成显式报错**：在 `FuncTorchBatched` 与 `Batched` 两个 key 上注册只抛错的 boxed 内核，batch 张量的调用在落入 functorch 兜底循环之前就被拦下，错误消息解释原因并指路（在 vmap 外调用；或把 batch 维折进 `b` 轴）。

预期结果：前两项命中本库消息（文本见 [src/bindings.cc:60-66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L60-L66)）；第 3 项 raise 但消息是 functorch 的（精确措辞随 torch 版本变化，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么拒绝内核要同时注册 `FuncTorchBatched` 和 `Batched`，而 `torch.vmap` 只用前者？
**答案**：`Batched` 是遗留 vmap 的 key，仍被受支持的公共 API 触达：`torch.autograd.grad(..., is_grads_batched=True)`（及经它的 `autograd.functional.*` 的 `vectorize=True`）走 `torch._vmap_internals`。只注册 functorch 的 key，这些入口照旧静默循环——且循环的是 backward（[src/bindings.cc:702-707](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L702-L707)）。

**练习 2**：`torch.vmap(f)` 里 `f` 闭包捕获了一个普通张量常量并对其调用 `all_to_all_4d`，会触发拒绝吗？该行为正确吗？
**答案**：不会触发——该次调用的张量不是 BatchedTensor，batching key 不在其 dispatch key 集合里，走正常内核。「在别人的 vmap 里恰好 genuine 地调用一次集合通信」是合法的（[src/bindings.cc:709-710](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L709-L710)）；注意若 `f` 本身处于某个**别的 op** 的回退循环里，这次调用仍会随循环执行多次——拒绝内核管不到这一层，SPMD 纪律仍由调用者自负。

**练习 3**：用户真的需要按样本批处理时，正确写法是什么？
**答案**：把 batch 维 reshape 进 `b` 轴——集合通信本来就携带 `b` 轴且不交换它（[docs/api.md:70-71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L70-L71)）。例如形状 `(3, 2, 16, n, d)` 的批（3 是 vmap 想向量化的样本维）用 `x.flatten(0, 1)` 并成 `(6, 16, n, d)`，对它调用一次 `all_to_all_4d` 即可——通信次数与样本数无关，各 rank 恒定各发一次。

## 5. 综合实践

写一个「**可追踪性探测脚本**」，把 4.1–4.4 串起来：对六个 op 在 `FakeTensorMode` 下逐一探测，输出一张分类表，再做 vmap 三入口探测。运行方式：`torchrun --nproc_per_node=2 probe.py`（单卡也可，形状变化不可见但分类表不变）。

```python
# probe.py（示例代码）—— 需 GPU；world_size=1 亦可运行
import os

import torch
import torch.distributed as dist
from torch._subclasses.fake_tensor import FakeTensorMode

from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
ws, dev = dist.get_world_size(), torch.device("cuda", torch.cuda.current_device())
group = UlyssesGroup()
shape = (2, 16, 4 * ws, 128)
real_x = torch.empty(shape, dtype=torch.bfloat16, device=dev)
out_buf = group.empty_output(real_x, mode=0)  # 集合操作：图外 eager 完成

rows = []
with FakeTensorMode():
    xf = torch.empty(shape, dtype=torch.bfloat16, device=dev)
    probes = {
        "all_to_all_4d":        lambda: group.all_to_all_4d(xf, mode=0),
        "all_to_all_4d_out":    lambda: torch.ops.fast_ulysses.all_to_all_4d_out(
                                       group._handle, xf, 0, None, None, out_buf),
        "empty_output":         lambda: group.empty_output(xf, mode=0),
        "staged":               lambda: group.all_to_all_4d_async(xf, mode=0),
        "staged_out":           lambda: group.all_to_all_4d_async(xf, mode=0, out=out_buf),
        "timed":                lambda: group._timed(xf, mode=0),
    }
    for name, fn in probes.items():
        try:
            y = fn()
            rows.append((name, "可 trace", tuple(y.shape) if torch.is_tensor(y) else "-"))
        except RuntimeError as e:
            frag = ("collective allocation" if "collective allocation" in str(e)
                    else "async cannot be traced" if "cannot be traced" in str(e)
                    else "timed cannot be traced" if "cannot be traced" in str(e) else str(e)[:60])
            rows.append((name, "拒绝: " + frag, "-"))

# vmap 三入口（fake 之外、真实张量上）
x = torch.randn(shape, dtype=torch.float32, device=dev)
batched = torch.randn(3, *shape, dtype=torch.float32, device=dev)
try:
    torch.vmap(lambda t: group.all_to_all_4d(t, mode=0))(batched)
    rows.append(("vmap", "!!! 未拒绝——回归", "-"))
except RuntimeError as e:
    rows.append(("vmap", "拒绝: does not support torch.vmap", "-"))
src = x.detach().requires_grad_(True)
out = group.all_to_all_4d(src, mode=0)
try:
    torch.autograd.grad(out, src, torch.stack([torch.randn_like(out) for _ in range(3)]),
                        is_grads_batched=True)
    rows.append(("is_grads_batched", "!!! 未拒绝——回归", "-"))
except RuntimeError as e:
    rows.append(("is_grads_batched", "拒绝: does not support torch.vmap", "-"))

for r in rows:
    print(r)
group.destroy()
dist.destroy_process_group()
```

预期产出（基于源码推演，待本地验证）：

| 探测 | 结果 |
| --- | --- |
| all_to_all_4d | 可 trace，形状 `(2, 16*ws, 4, 128)` |
| all_to_all_4d_out | 可 trace（out 校验通过，无返回值；探测函数返回 None 属正常——`-> ()` 的 schema） |
| empty_output | 拒绝: collective allocation |
| staged / staged_out | 拒绝: async cannot be traced |
| timed | 拒绝: timed cannot be traced |
| vmap / is_grads_batched | 拒绝: does not support torch.vmap |

两个值得写进实验报告的观察：(1) `empty_output` 的真实调用在 fake 上下文之外完成——这是 4.2 教的图外分配模式；(2) `all_to_all_4d_out` 探测里传的 `out_buf` 是真张量而 `xf` 是 fake——Meta 内核对 `out` 只做形状/dtype 核对（[src/bindings.cc:337-355](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L337-L355)），两种世界的张量在纯主机比较下可以相遇。

## 6. 本讲小结

- **Meta 内核复用 `group->plan()`**：形状算术与全部校验只有一份实现（torch-free、CUDA-free 的 a2a_plan 层），Meta 路径与 eager 路径跑同一份代码；torchbind 的 group 经 `FakeTensorMode` 原样到达，`world_size` 因此可用；显式 Meta 注册胜过 `CompositeExplicitAutograd` 别名，避免 fake 张量坠入真实内核死于误导性的 `require_cuda`。
- **拒绝 trace 的三个 op 族各有具体危害**：`empty_output` 是集合分配（rendezvous 位置不能交给图）；`staged` 一对连着 work registry 注册与真实流句柄参数（被 trace 的调用会留下没人等待的 Work、返回声称传输已发出的 wrapper）；`timed` 返回测量值（零是错误答案而非缺失答案）。共同原则：**返回形状不是半个答案，是错误答案**。
- **vmap 的原生失败模式是挂起而非报错**：无 batching rule 时静默回退 for 循环，各 rank 发出不同次数的集合通信，无异常无超时。
- **双 key 拒绝**：`FuncTorchBatched` 覆盖 `torch.vmap`（及其对 grad 输入、嵌套的情形），`Batched` 覆盖 `is_grads_batched=True` 等遗留入口；一个 boxed 只抛错的内核服务全部六个 op，消息从 schema 取名。闭包常量不触发、保持合法。
- **整体 trace 边界**：形状传播可用（FakeTensor / AOTAutograd / backward trace），`torch.compile` 在 group 调用处 graph-break（torchbind 无 fake class），async/分配/计时一律 eager——api.md 与 design.md 把这条边界写成了用户契约。

## 7. 下一步学习建议

- **u7-l1（异步调用全景）**：本讲反复引用的 `all_to_all_4d_async` 包装（comm 流、work registry、`AsyncCollectiveTensor`）在 eager 世界的完整形态是下一单元的主题——你会看到 staged op 拒绝 trace 所保护的那套机制实际怎么工作。
- **u9-l2（correctness 与 validation 工作负载）**：本讲的 vmap 断言来自 `validation.py`；那篇讲义会讲「每个拒绝都必须各 rank 一致、且先于第一个屏障」是如何被系统性检验的。
- **延伸阅读**：torch 官方文档中 dispatcher 的 alias key 与 fallthrough 语义、`FakeTensorMode` 的设计说明，以及 functorch 关于 batching rule 与回退循环的文档——对照本讲四个模块读，能把你在这里见到的「注册一个只抛错的内核」放回更大的图景。
