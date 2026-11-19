# torch 算子注册：schema 拆分与自定义类

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `TORCH_LIBRARY` 里的 schema 字符串：每个 token 对应什么 C++ 类型、什么 Python 类型、`Tensor(a!)` 和 `-> ()` 这些注解在声明什么。
2. 解释为什么「一个集合通信」要注册成**两个算子**（functional 与 out 变体）：可变 schema 无法注册 autograd 公式，拆开让两边各自诚实。
3. 说出六个 op 各自注册了哪些 dispatch key（CompositeExplicitAutograd / Autograd / Meta / FuncTorchBatched+Batched），以及每个键的用途。
4. 说明 `UlyssesGroup` 为什么必须是 torchbind 自定义类（而不是 pybind 对象），以及 `epoch_debug` / `staging_slots_debug` 两个「仅测试用」方法的存在理由。
5. 解释 `all_to_all_4d_staged` / `all_to_all_4d_staged_out` 两个内部 op 为什么存在，以及 op 与 pybind 函数的分界线画在哪里。

本讲是 u6 单元的第一讲：只看**注册层**——schema、dispatch key、自定义类。一次调用的完整校验流水线在 u6-l2，autograd 的数学在 u6-l3，Meta 内核与 vmap 拒绝的细节在 u6-l4。

## 2. 前置知识

本讲需要几个 torch 扩展开发的核心概念。如果你写过 pybind 扩展但没写过 torch 自定义算子，这里从零讲起。

**dispatcher（调度器）与 dispatch key。** 你在 Python 里调用 `torch.ops.fast_ulysses.all_to_all_4d(...)` 时，请求先进入 torch 的 dispatcher。dispatcher 看一眼所有参数的「性质」——张量在 CUDA 上吗？`requires_grad` 吗？是 FakeTensor（只有形状没有数据的假张量）吗？在 `torch.vmap` 变换下吗？——据此算出一组 **dispatch key**，然后按优先级选出为最高键注册的那个 C++ 函数（内核）去执行。「注册一个算子」就是两件事：用 **schema** 声明签名，再把若干「键 → 函数」对写进调度表。本讲反复出现的几个键：

| dispatch key | 什么时候被选中 | 通俗理解 |
|---|---|---|
| `CompositeExplicitAutograd` | 一个「别名键」：一次注册覆盖所有后端键（CUDA、CPU……），**也覆盖 Meta** | 「我这个内核哪儿都能跑」的省事注册 |
| `Autograd` | 任何参数 `requires_grad` 且梯度模式开启 | 「要不要建反向图」的入口 |
| `Meta` | 参数是 Meta 张量 / FakeTensor（做形状传播、不碰设备） | 「只算形状，不动数据」 |
| `FuncTorchBatched` / `Batched` | `torch.vmap` / 带批量的反向 | 「被向量化包裹了」 |

**schema（算子签名）。** 一段类型字符串，例如 `add(Tensor self, Tensor other) -> Tensor`。它同时是 Python 侧的类型检查、C++ 侧的打包/解包依据，还携带**别名注解**：`Tensor(a!)` 声明「这个参数会被原地写入」。带别名注解的 schema 叫**可变（mutable）schema**。这个注解是本讲第二主角——它正是 functional/out 拆分的根源。

**torchbind 自定义类。** pybind11 的对象（`PYBIND11_MODULE` 里 `m.def` 出来的东西）**不能作为 torch 算子的参数**——schema 的类型系统里没有它的位置。要让一个 C++ 有状态对象能进 schema、能被存进 autograd 的 `saved_data`，必须继承 `torch::CustomClassHolder` 并用 `m.class_<T>("名字")` 注册成 torchbind 类，Python 侧通过 `torch.classes.命名空间.名字` 使用。u1-l4 已经引入过这个概念，本讲看它的注册代码。

**承接前讲。** u2-l2 建立了 `all_to_all_4d` 的形状契约（mode 0/1、dtype 白名单、集体调用五条纪律）；u5-l1 讲了 `launch_a2a_ce` 怎么把 `CopyOp` 落成拷贝引擎调用；u1-l4 给过调用链骨架 `group.py → prepare → 屏障 → 传输 → 屏障 → copy_out`。本讲只看这条链最外层的「接口皮」。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | 本讲主角。全文件的下半部分（L678 起）是唯一的 `TORCH_LIBRARY` 注册块与 `PYBIND11_MODULE`；上半部分是各 dispatch key 对应的内核实现 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python 门面：持有 `torch.classes` 句柄、按 `out`/`lend` 的有无选择调用哪个 op |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp) | `UlyssesGroup` 声明：`torch::CustomClassHolder` 基类、构造签名、两个 debug 方法 |
| [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py) / [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py) / [test/distributed/window_race.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py) | `epoch_debug` / `staging_slots_debug` 的真实使用者，是「为什么要有 debug 方法」的证据 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**TORCH_LIBRARY schema**、**functional/out 拆分**、**自定义类 torchbind**、**staged 内部 op**，外加把注册表串起来的 **dispatch key 全表**。

### 4.1 TORCH_LIBRARY 与 schema：六条 def 的语法

#### 4.1.1 概念说明

`TORCH_LIBRARY(命名空间, m)` 宏开启一个注册块。块内做两类事：

- `m.def("schema 字符串")`：声明一个算子——名字、参数类型、返回类型。这一步之后，`torch.ops.fast_ulysses.<名字>` 就存在了，但还没有内核。
- `m.impl("名字", dispatch_key, 函数)`：为某个键挂上内核。

本项目在 `fast_ulysses` 命名空间下注册了**六个 op**：`all_to_all_4d`、`all_to_all_4d_out`、`all_to_all_4d_staged`、`all_to_all_4d_staged_out`、`empty_output`、`all_to_all_4d_timed`。它们不是平行的六种功能，而是「一次集合通信」在四个维度上的投影：functional/out、sync/async(staged)、以及两个辅助（分配缓冲、计时）。

注册发生在**扩展 `.so` 被 dlopen 时**，早于任何 Python 调用——这一点写在文件末尾的注释里（见 4.5.3）。所以只要 `import fast_ulysses`（它 `from . import _C`），六个 op 就全部出现在 `torch.ops.fast_ulysses` 下。

#### 4.1.2 核心流程

```text
import fast_ulysses
  └─ from . import _C            (group.py 顶部)
       └─ dlopen _C.so
            └─ TORCH_LIBRARY 静态初始化器运行
                 ├─ m.class_<UlyssesGroup>("UlyssesGroup")   ← 注册自定义类
                 └─ 6 × m.def(...) + 若干 m.impl(...)        ← 声明 op 与内核
之后: torch.ops.fast_ulysses.all_to_all_4d(...)  →  dispatcher 查表选内核
```

#### 4.1.3 源码精读

整个注册块的入口在 [src/bindings.cc:678-679](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L678-L679)，注释只有一句「这里开始是 torch op 层」的延续（文件头 L1-L5 说明了分层：group 拥有一切跨调用存活的状态，Python 侧只是构造器加转发）。

第一条 schema 是六个中最有代表性的：

[src/bindings.cc:718-719](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L718-L719) 声明 `all_to_all_4d` 的签名。逐 token 拆开：

```cpp
m.def("all_to_all_4d(__torch__.torch.classes.fast_ulysses.UlyssesGroup group, Tensor input, "
      "int mode, int[]? seq_splits=None, int[]? head_splits=None) -> Tensor");
```

| schema 记法 | 对应 C++ 形参 | Python 传什么 | 含义 |
|---|---|---|---|
| `__torch__.torch.classes.fast_ulysses.UlyssesGroup group` | `const c10::intrusive_ptr<UlyssesGroup>&` | `torch.classes.fast_ulysses.UlyssesGroup` 实例 | torchbind 自定义类作参数（4.4 详述） |
| `Tensor input` | `const at::Tensor&` | `torch.Tensor` | 输入张量 |
| `int mode` | `int64_t` | `int` | 0 或 1 |
| `int[]? seq_splits=None` | `const std::optional<std::vector<int64_t>>&` | `list[int]` 或 `None` | 可选整数表，带默认值 |
| `-> Tensor` | 返回 `at::Tensor` | `torch.Tensor` | 返回**全新**张量，无别名注解 → 这是 functional 形式 |

对比另外两条返回形态不同的 schema：

- [src/bindings.cc:730-731](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L730-L731)：`all_to_all_4d_out` 以 `Tensor(a!) out` 收尾、`-> ()` 返回——`(a!)` 声明 `out` 会被原地写入（可变 schema），`() ` 表示无返回值。注意它**没有** `=None` 默认值：out 变体没有「省略 out」这回事，省略 out 就该调 functional 那个。
- [src/bindings.cc:765-767](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L765-L767)：`all_to_all_4d_timed` 返回 `(Tensor, float[])`——一个张量加一组时长，对应 C++ 的 `std::tuple<at::Tensor, std::vector<double>>`（[src/bindings.cc:624](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L624)）。

还有一条容易被忽略的不对称：[src/bindings.cc:739-741](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L739-L741) 的 `all_to_all_4d_staged` 带 `bool lend=False` 默认值，而 [src/bindings.cc:746-748](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L746-L748) 的 `staged_out` 既无 `lend`（`out=` 与 `lend=True` 互斥，u4-l4）也无任何 `=None` 默认值。

#### 4.1.4 代码实践

**实践目标**：不动设备、只靠源码，把六个 schema 全部手工解析成「C++ 签名 ↔ Python 调用」的对照表。

**操作步骤**：

1. 打开 [src/bindings.cc:678-773](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L678-L773)，抄下六条 `m.def` 的 schema 字符串。
2. 对每条 schema，在源码上半部分找到它 CompositeExplicitAutograd 键挂的 C++ 函数（如 `all_to_all_4d_staged` 挂到 [src/bindings.cc:400-407](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L400-L407)），核对参数类型逐一对应。
3. 再到 [python/fast_ulysses/group.py:129-149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L149) 与 [group.py:203-226](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L203-L226) 找到 Python 侧的调用点，确认传参顺序与 schema 一致。

**需要观察的现象**：schema 类型、C++ 形参、Python 实参三者严格一一对应；任何一处多一个少一个参数，扩展在加载或调用时会立刻报类型错误。

**预期结果**：得到一张 6 行的「op → C++ 函数 → Python 调用点」表。本实践纯阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：schema 里的 `int[]?` 与 `int[]` 差在哪？本项目哪个语义（u2-l3 讲过的）依赖这个「?」？

答案：`?` 表示可选（可为 None），对应 `std::optional<std::vector<int64_t>>`。u2-l3 讲过：不传 `seq_splits`/`head_splits` 表示「偶均分特例」，传**空表**则是一个错误——两种调用必须能区分，所以这里必须用 optional 而不是裸 vector。`UlyssesGroup` 的 plan 缓存键也为此用 `std::optional`（[include/fast_ulysses/group.hpp:156-161](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L156-L161) 的注释）。

**练习 2**：为什么 `all_to_all_4d` 的 schema 给 `seq_splits` 设了默认值 `=None`，而 `all_to_all_4d_out` 没有？

答案：默认值只是「Python 调用时可以少写这个参数」的便利。两条路径里 Python 门面（group.py）其实总是显式传全参数；functional 形式是面向用户的「一个方法两种用法」入口，保留默认值与公开文档一致；out 变体只从 `all_to_all_4d(out=...)` 的内部分支调用（[group.py:146-148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L146-L148)），参数永远齐全，写默认值没有意义。

### 4.2 一个集合通信，两个算子：functional / out 拆分

#### 4.2.1 概念说明

回顾 u4-l3：`out=` 有三态——默认分配、普通张量拷入、`empty_output()` 缓冲零拷贝。直觉上这该是一个带 `Tensor out=None` 可选参数的**单个** op。本项目偏拆成两个，根源在 torch 的注册规则：

1. 若在一个 schema 里声明 `out` 真的可能被写入，就得写 `Tensor(a!)`，schema 随即变为**可变**的。
2. torch 的 `torch/library.py` **拒绝为可变算子注册 autograd 公式**。
3. 于是「既能 `out=` 又能可微」的单 op 不存在：要么撒谎不写 `(a!)`，要么放弃可微。
4. 拆成两个，functional 那个拿到 autograd 公式和 Meta 内核；out 那个拿到与真实行为一致的别名注解。谁也不撒谎。

还有返回值形态的讲究：out 变体用 `-> ()` 而不是 `-> Tensor(a!)`，因为 torch 的 `can_auto_functionalize` 只接受「可变参数 + 空返回」这一种形状，只有这种形状能进 functionalization 图。拆分让 out 变体恰好长成这个形状。

#### 4.2.2 核心流程

```text
用户: group.all_to_all_4d(x, mode=0)            group.all_to_all_4d(x, mode=0, out=buf)
  │                                               │
  ▼                                               ▼
out is None?                                    out is None? 否
  │ 是                                            │ 先检查: 梯度开启且 x.requires_grad → RuntimeError
  ▼                                               ▼
torch.ops.fast_ulysses.all_to_all_4d(...)       torch.ops.fast_ulysses.all_to_all_4d_out(..., buf)
  (functional: 无别名 → 可注册 Autograd/Meta)      (可变 schema: Tensor(a!) out, -> ())
  │                                               │
  └────────────── 两个 op 内部都汇入同一个 run() ──┘
```

#### 4.2.3 源码精读

拆分动机的完整论证写在 [src/bindings.cc:245-252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L245-L252) 的注释里，值得逐句读：标题是「The one collective, in two forms」，随后解释可变 schema 与 autograd 公式的冲突、`-> ()` 与 `can_auto_functionalize` 的关系。

两个内核本体都很薄：functional 的 [src/bindings.cc:253-270](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L253-L270) 只做 `require_cuda` + 设备守卫，然后带 `std::nullopt` 的 out、`kSyncWindow` 角色、当前流和 `lend=false` 调 `run()`；out 变体的 [src/bindings.cc:312-330](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L312-L330) 同样薄，只是把 `out` 传进去、无返回值。真正的校验与三态分派在 `prepare()`（u6-l2 的主题）。

Python 侧的拆分调用在 [python/fast_ulysses/group.py:129-149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L149)：`out is None` 走 functional op；否则**先拒绝**梯度场景（[group.py:138-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143)——out 变体没有 backward，静默跑完会让 `x.grad` 悄悄是 None，所以宁可报错），再调 out 变体。紧邻的注释「Two ops, one method」([group.py:144-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L144-L145)) 就是 4.2.1 的浓缩版。

#### 4.2.4 代码实践

**实践目标**：用运行时证据（若环境可用）确认两个 op 的 schema 差异就是别名注解；不可运行时完成纸面推演。

**操作步骤**：

1. （需已按 u1-l3 构建）启动 Python，`import torch, fast_ulysses`。
2. 打印两个 schema：
   ```python
   print(torch.ops.fast_ulysses.all_to_all_4d.default._schema)
   print(torch.ops.fast_ulysses.all_to_all_4d_out.default._schema)
   ```
3. 对照 [src/bindings.cc:718-719](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L718-L719) 与 [730-731](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L730-L731) 的字符串，逐字符比对。

**需要观察的现象**：`all_to_all_4d` 的 schema 返回 `Tensor` 且无 `(a!)`；`all_to_all_4d_out` 的 `out` 参数带 `(a!)`、返回 `()`。

**预期结果**：打印与源码字符串一致（schema 对象的 `str()` 就是这段文本）。打印的具体格式随 torch 版本略有差异，**待本地验证**；无构建环境时以源码字符串为准完成纸面比对即可。

#### 4.2.5 小练习与答案

**练习 1**：如果把两个 op 合并成一个 `all_to_all_4d(..., Tensor(a!)? out=None) -> Tensor`，具体丢掉什么？

答案：两样。其一，schema 可变，`torch/library.py` 拒绝为它注册 autograd 公式，functional 用法失去可微性——而 DiT 示例（u2-l4）正是靠「算子本身可微、backward 是另一个 mode」才不需要 autograd.Function 包装。其二，`Tensor(a!)? out` + `-> Tensor` 不是 `can_auto_functionalize` 接受的形状，out 路径也进不了 functionalization 图。

**练习 2**：Python 门面为什么在调 out 变体**之前**自己再查一次 `torch.is_grad_enabled() and x.requires_grad`（[group.py:138-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143)）？C++ 的 out 内核自己抛不行吗？

答案：能抛，但晚了也软了。这里要的不是「运行中报错」而是「把不可微这件事变成显式契约」：门面层拒绝发生在任何设备工作之前，消息直接告诉用户三条出路（去掉 out=、或进 no_grad）。更关键的是 u2-l2 的纪律「校验先于第一个屏障」——被拒参数不能拖累同伴 rank；在门面层最早拒绝是最便宜的做法。

### 4.3 dispatch key 全表：每个 op 注册了哪些键、各管什么

#### 4.3.1 概念说明

把六条 `m.impl` 摊开，就是一张「op × dispatch key」矩阵。读表前记住三条规则：

1. **CompositeExplicitAutograd 是别名键**：一次注册覆盖所有后端键，**也覆盖 Meta**。所以只注册它时，FakeTensor 也会落进真实内核——对 CUDA 专用的内核这是错误行为。
2. **显式 Meta 注册优先于别名**：所以本项目对每个 op 都补了一条显式 Meta 内核（有用的做形状传播，不该被 trace 的直接抛错）。
3. **refuse_batching 给每个 op 挂两个批量键**：`FuncTorchBatched`（`torch.vmap` 走的键，优先级高于 Autograd）和 `Batched`（旧版 vmap 的键，仍可从 `torch.autograd.grad(..., is_grads_batched=True)` 到达）。挂的是一个只会抛错的 boxed 内核。理由（u6-l4 详述）：vmap 对没有 batching rule 的算子会退化成逐 batch 元素的 for 循环——对集合通信，这意味着各 rank 发出**不同次数**的集合调用，结果是挂起而非报错，所以必须把失败变成显式异常。

#### 4.3.2 核心流程

一次 `torch.ops.fast_ulysses.all_to_all_4d(g, x, 0)` 的内核选择，按 key 优先级从上到下第一个命中的生效：

```text
参数带 FuncTorchBatched/Batched 键?  → refuse_vmap（抛错）
参数 requires_grad 且 GradMode 开?   → all_to_all_4d_autograd（建图/前向 AD）
参数是 FakeTensor/Meta?              → all_to_all_4d_meta（形状传播）
否则（真实 CUDA 张量）               → all_to_all_4d（CompositeExplicitAutograd 别名）
```

#### 4.3.3 源码精读

完整注册表（与 [src/bindings.cc:717-772](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L717-L772) 逐行对应）：

| op | CompositeExplicitAutograd | Autograd | Meta | FuncTorchBatched + Batched |
|---|---|---|---|---|
| `all_to_all_4d`（L718-726） | `all_to_all_4d` | `all_to_all_4d_autograd` | `all_to_all_4d_meta`（真形状传播） | `refuse_vmap` |
| `all_to_all_4d_out`（L730-737） | `all_to_all_4d_out` | —— 未注册 | `all_to_all_4d_out_meta`（校验 out 形状） | `refuse_vmap` |
| `all_to_all_4d_staged`（L739-744） | `all_to_all_4d_staged` | —— 未注册 | `all_to_all_4d_staged_meta`（**抛错拒绝**） | `refuse_vmap` |
| `all_to_all_4d_staged_out`（L746-751） | `all_to_all_4d_staged_out` | —— 未注册 | `all_to_all_4d_staged_out_meta`（**抛错拒绝**） | `refuse_vmap` |
| `empty_output`（L753-763） | `empty_output` | `makeFallthrough()` | `empty_output_meta`（**抛错拒绝**） | `refuse_vmap` |
| `all_to_all_4d_timed`（L765-772） | `all_to_all_4d_timed` | `makeFallthrough()` | `all_to_all_4d_timed_meta`（**抛错拒绝**） | `refuse_vmap` |

几个值得逐条读的注册点：

- **唯一的 Autograd 内核**在 [src/bindings.cc:721](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L721)。`all_to_all_4d_autograd`（[src/bindings.cc:584-605](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L584-L605)）内部经 `dispatch_below_autograd`（[src/bindings.cc:478-493](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L478-L493)）**重新进入 dispatcher**——不能直调 `all_to_all_4d()`，否则永久离开调度，FakeTensorMode/Meta 键再也没机会被咨询。数学（vjp = 逆排列 = `1 - mode`）留给 u6-l3。
- **fallthrough 而不是不注册**：[src/bindings.cc:761](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L761) 和 [L771](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L771) 给 `empty_output` / `timed` 挂 `makeFallthrough()`。注释（[L757-760](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L757-L760)）说明：不挂的话 Autograd 键落到 torch 的 `autogradNotImplementedFallback`，会给一个**全新分配的缓冲**建 autograd 节点、返回一个连 `detach` 都做不了的 non-leaf——新缓冲与输入张量没有数据依赖，不该继承 `requires_grad`。
- **out 变体也挂 refuse_batching**：[src/bindings.cc:734-737](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L734-L737) 的注释说，torch 自己的 for-loop 回退**已经**拒绝 out 变体了，但那是 torch 替它自己的理由拒的；哪天 torch 长出了 out= 的批量回退，这个 op 就会开始静默循环。一行注册把这条性质变成**本库的**而非 torch 的。
- `refuse_batching` 本体在 [src/bindings.cc:711-715](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L711-L715)：一个 lambda，对两个键各挂一次同一个 boxed 内核 `refuse_vmap`（[src/bindings.cc:58-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L58-L67)，boxed 是因为 `torch::CppFunction` 没有 `makeError()`）。两个键缺一不可的论证在 [L700-710](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L700-L710) 的注释：只挂 `FuncTorchBatched` 会漏掉 `torch.autograd.grad(is_grads_batched=True)` 这条仍受支持的公开 API。
- Meta 内核「真传播」与「抛错拒绝」的分界：`all_to_all_4d_meta`（[src/bindings.cc:285-293](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L285-L293)）复用 `group->plan()` 做纯主机形状传播（为什么不把形状规则抄到 Python？[L276-284](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L276-L284) 的注释给出了「抄一份就出了 test_plan.py 够不到的第二份规则」的理由，u6-l4 展开）；而 staged / empty_output / timed 的 Meta 内核统一抛错（[L298-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L298-L308)、[L438-451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L438-L451)、[L665-674](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L665-L674)），各自的理由写在每个函数头：集合分配不能进 trace（rendezvous 位置会被图摆布）、计时返回的是没跑就没法测的量。

#### 4.3.4 代码实践

**实践目标**：让 dispatcher 亲口告诉你这张表。

**操作步骤**（需已构建扩展；构建方法见 u1-l3）：

```python
import torch, fast_ulysses  # import 即注册
name = "fast_ulysses::all_to_all_4d"
print(torch._C._dispatch_dump(name))          # 该 op 的完整调度表
print(torch._C._dispatch_has_kernel(name, "Meta"))        # 预期 True
print(torch._C._dispatch_has_kernel(name, "FuncTorchBatched"))  # 预期 True
```

对六个 op 名各跑一遍 `dispatch_dump`，与 4.3.3 的表格逐行核对。

**需要观察的现象**：`all_to_all_4d` 的 dump 里能看到 Autograd、Meta、CompositeExplicitAutograd（或其展开的后端键）与两个批量键；`all_to_all_4d_out` 的 dump 里没有 Autograd 内核；`empty_output` 的 Autograd 条目是 fallthrough。

**预期结果**：与 4.3.3 表格一致。dump 输出的具体排版与键名展开方式随 torch 版本变化，**待本地验证**。无构建环境时，本实践退化为对 [src/bindings.cc:717-772](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L717-L772) 的逐行表格填写（答案就是 4.3.3）。

#### 4.3.5 小练习与答案

**练习 1**：`all_to_all_4d_out`、`staged`、`staged_out` 三个 op 为什么**不**注册 Autograd 内核也不会出乱子？

答案：因为 Python 门面把它们全部挡在了梯度路径之外：out 变体在 [group.py:138-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L138-L143) 被拒，async（staged 的唯一入口）在 [group.py:192-197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L192-L197) 被拒（AsyncCollectiveTensor 是 leaf，梯度会被静默丢弃）。可微的口径由门面统一收紧，注册层就不用为不可微路径挂内核。

**练习 2**：`empty_output` 用 `makeFallthrough()`，而 `all_to_all_4d_out` 干脆不注册 Autograd 键。这两个「不建图」的手段差别在哪？

答案：fallthrough 是「让调用**穿过** Autograd 键继续往下走」——empty_output 返回新缓冲，希望调用方拿到与输入 `requires_grad` 无关的普通张量，穿过去落在 CompositeExplicitAutograd 内核上正好。不注册则是「让 Autograd 键落到 torch 的默认回退」——对可变 out 变体，调用本来就只在 no_grad 下发生，默认回退不会被触发；两个手段各自匹配「要结果」与「要副作用」的语义。

### 4.4 自定义类 torchbind：UlyssesGroup 与两个 debug 方法

#### 4.4.1 概念说明

`UlyssesGroup` 是全库唯一的有状态对象：窗口、plan 缓存、staging 池、传输流全在它体内（u4 系列、u7 系列）。它必须同时满足三个诉求，只有 torchbind 自定义类做得到：

1. **能进 schema**：六个 op 的第一个参数都是它。pybind 对象进不了 schema 的类型系统。
2. **能进 autograd 的 `saved_data`**：functional 的 backward 需要把 group 存进 `AutogradContext`（[src/bindings.cc:523](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L523)），`saved_data` 是 `IValue` 表，能装 custom class，装不了 pybind 对象。
3. **能挂方法**：构造、`destroy`，以及两个 debug 方法。

注册行是 [src/bindings.cc:680-681](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L680-L681)：`.def(torch::init<std::string, int64_t, int64_t, int64_t>())` 对应 [include/fast_ulysses/group.hpp:41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L41) 的构造函数（进程组名、rank、world_size、设备号）。Python 侧在 [python/fast_ulysses/group.py:87-89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L87-L89) 用 `torch.classes.fast_ulysses.UlyssesGroup(...)` 造出句柄存进 `self._handle`，之后每个 op 调用都把它当第一个参数传回 C++。

**两个 debug 方法的存在理由**是本模块的核心。它们注释里都写着「TESTS ONLY」，但不是可有可无的便利——它们各自补上一个**结果本身无法证明**的正确性维度：

- `epoch_debug(probe, role)`：读本 rank 屏障的 epoch 计数（[include/fast_ulysses/group.hpp:139-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L139-L143)）。存在理由（[src/bindings.cc:683-687](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L683-L687)）：数据撕裂只是握手死掉的**充分**信号，不是必要信号——一次「恰好没有 peer 迟到」的重放照样干干净净地回来，只有 epoch 能说明屏障真的活着。
- `staging_slots_debug(like)`：读 `(shape, dtype)` 键下 staging 槽的数量（[group.hpp:145-149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L145-L149)）。存在理由（[src/bindings.cc:692-695](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L692-L695)）：「多个同形状 async 调用同时在飞」正是 staging 池存在的意义，但**结果看不出来**——共享一个槽也是正确的，只是把调用串行化；槽计数才能把两者分开。

两者都是方法而不是 pybind 函数，原因见 [src/bindings.cc:683-684](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L683-L684) 的注释：「the group is a torchbind object, which pybind cannot take」——要拿 group 当参数，就只能在 torchbind 类上挂方法。

#### 4.4.2 核心流程

```text
Python: torch.classes.fast_ulysses.UlyssesGroup(name, rank, ws, dev)   ← 集合构造（u2-l1）
          │  存为 group._handle
          ├─ torch.ops.fast_ulysses.<op>(group._handle, ...)           ← 六个 op 的第一参
          ├─ group._handle.destroy()                                   ← 受支持的收尾
          ├─ group._handle.epoch_debug(probe, role)                    ← 仅测试
          └─ group._handle.staging_slots_debug(like)                   ← 仅测试
```

#### 4.4.3 源码精读

- 类注册与四个方法：[src/bindings.cc:680-698](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L680-L698)。`epoch_debug` 的 lambda 把 `int64_t role` 转回 `WindowRole` 枚举（`like` 张量携带决定窗口键的 dtype）；`staging_slots_debug` 的 `like` 携带 (shape, dtype)。
- 类声明与基类：[include/fast_ulysses/group.hpp:38-41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L38-L41)——`class UlyssesGroup: public torch::CustomClassHolder`，torchbind 的入场券。
- 真实使用者的断言：
  - [test/distributed/cudagraph.py:98-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L98-L116)：每次 CUDA graph 重放后采样 epoch，断言**恰好 +2**（每次调用两道屏障）。这是「重放没有消费陈旧状态」的必要信号——u5-l3 的主题在这里落成一行断言。
  - [test/distributed/correctness.py:203-209](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L203-L209)：五个同形状 async 调用逐个采样槽计数，断言增长序列恰为 `[1, 2, 3, 4, 4]`（第 5 个停在池上限 4，即 u7-l2 的 `kMaxStagingPerShape`）。
  - [test/distributed/window_race.py:257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L257)：`epoch_debug(普通张量, 0) == -1` 用作「内部 sync 窗口不存在」的探测器——窗口从未分配返回 -1，从而证明零拷贝路径真的让 peers 直写了 `out` 缓冲。
  - [test/distributed/correctness.py:143-144](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L143-L144)：一次调用后 `empty_output` 缓冲的 epoch 恰为 2——分配会清零 signal pad，只有真正跑过屏障的缓冲才会推进计数。

#### 4.4.4 代码实践

**实践目标**：通过精读测试，理解「为什么结果相等不够、必须加可观察量」。

**操作步骤**：

1. 读 [test/distributed/cudagraph.py:96-118](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L96-L118)，找出「位精确」与「epoch +2」各自证明什么。
2. 读 [test/distributed/correctness.py:186-209](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L186-L209) 的注释与断言。
3. 回答：若删掉 `staging_slots_debug`，`growth == [1, 2, 3, 4, 4]` 这条断言换成什么？能换吗？

**需要观察的现象**（纸面）：位精确校验只能证明「结果对」，无法区分「并发」与「串行但正确」；epoch 只能证明「屏障推进」，无法证明数据没被撕裂。两个 debug 方法各补一维，互不可替代。

**预期结果**：第 3 步的答案——不能换。五个调用的结果全部正确与「共享一个槽串行执行」完全兼容（共享只是让下一次 staging 拷贝在调用者流上等前一次交换退休），只有槽计数这个组外观察量能区分。这正是 [group.hpp:145-148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L145-L148) 注释的原话：「a bit-exact pair proves nothing on its own and the count does」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `epoch_debug` 要做成**同步**（blocking）的方法，而普通调用全是异步的？

答案：它读的是屏障内核在设备侧推进的计数器，必须用一次阻塞的设备到主机拷贝才能取回（[group.hpp:139-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L139-L143) 的 doc 注释写明「Synchronising」）。它是测试探针，不在性能路径上，同步无所谓；也因此 [test/distributed/overlapping_barriers.py:224-225](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L224-L225) 在采样前先做了显式同步。

**练习 2**：`epoch_debug(probe, role)` 的第一个参数叫 `probe`（探针）而不是 `window`，为什么？

答案：它接受的可以是**任意**同 dtype 张量：来自 `empty_output()` 的缓冲按地址在 `owned_` 里解析出它自己的窗口；普通张量则按 `(role, dtype)` 解析到内部窗口；两者都不是则返回 -1（[window_race.py:257](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L257) 正是利用这个 -1 做检测）。探针的 (dtype / 形状) 只是「定位哪扇窗」的键，其数据从不被读。

### 4.5 staged 内部 op 与 op/pybind 的边界

#### 4.5.1 概念说明

`all_to_all_4d_staged` / `all_to_all_4d_staged_out` 是异步路径（u7 的主题）的**设备侧一半**，本讲只看它们的注册形态。它们的存在回答三个问题：

1. **为什么不能复用 `all_to_all_4d`？** async 调用要在**comm 流**上传输、在**调用者流**上暂存输入（u5-l1 讲过两流互不排序，u7-l1 展开）。而「调用者流是哪条」Python 知道、C++ 不知道——Python 已经用 `with torch.cuda.stream(comm)` 把当前流切成了 comm 流，C++ 里再取「当前流」拿到的是 comm。所以调用者流必须**作为参数**传进去。
2. **流怎么进 schema？** CUDA 流的裸句柄不是合法 schema 类型，于是拆成两个 `int`：`caller_stream_id` 与 `caller_device_index`，C++ 用 `c10::Stream::unpack3` 重建出与调用者同一个流对象（[src/bindings.cc:385-388](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L385-L388)：从 torch 自己的标识符重建而非包裸句柄，是为了让缓存分配器看到同一个流对象）。schema 里出现「整数其实是流」这种泄漏，正是这个 op 只做内部使用的信号。
3. **为什么又拆一对 functional/out？** 与 4.2 同理：`staged_out` 声明 `Tensor(a!) out`，`staged` 可带 `lend=True`（u4-l4 的窗口轮转入口）。

**op 与 pybind 的分界线**在文件末尾的 [src/bindings.cc:776-811](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L776-L811)：`PYBIND11_MODULE` 里只放**进不了 schema** 的东西——`register_stream_completion`（拿裸流句柄、改进程级注册表，注释明说「neither of which belongs in a schema」）、`nvlink_matrix` / `check_nvlink`（纯主机拓扑查询，u8-l1）、`_set_ce_fault`（故意弄坏算子的负控制，u9-l3）、`build_info`。凡是「拿 group 或张量、需要 dispatcher 语义（Meta 拒绝、vmap 拒绝、autograd）」的，都必须是 op。

#### 4.5.2 核心流程

```text
Python: group.all_to_all_4d_async(x, lend=True)
  1. caller = torch.cuda.current_stream(self.device)     ← 切流之前取调用者流
  2. with torch.cuda.stream(comm_stream):                ← 当前流 = comm
  3.   torch.ops.fast_ulysses.all_to_all_4d_staged(
         handle, x, mode, seq, head,
         caller.stream_id,      ← int，schema 里的 caller_stream_id
         caller.device_index,   ← int，schema 里的 caller_device_index
         lend)
  4. _C.register_stream_completion(y, comm)              ← pybind，不是 op
  5. y.record_stream(...) ×2                              ← 双流记账
```

#### 4.5.3 源码精读

- 两条 schema 与内核挂载：[src/bindings.cc:739-751](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L739-L751)。注意 Meta 内核挂的是**抛错**版本（4.3 的表格），拒绝理由的长注释在 [src/bindings.cc:425-437](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L425-L437)：这个 op 只经 `all_to_all_4d_async` 到达，而那层 Python 是 eager-only；被 trace 的调用会在进程级 work registry 里留下永远没人等的 Work、返回一个 `wait()` 撒谎的结果。「返回一个形状不是半个答案，是错答案」——形状要拿就从 `all_to_all_4d` 拿，它搬的字节完全相同。
- 两个内核共享的 `run_staged`：[src/bindings.cc:364-396](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L364-L396)。开头三条检查（`check_alive` / `require_cuda` / `require_group_device`）**先于 `stage()`**，注释（[L374-380](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L374-L380)）解释了为什么这两条不留给 `prepare()`：staging 表项在 `destroy()` 之后创建就永远没人释放；staging 缓冲按 (shape, dtype) 缓存却取输入的设备，一次错设备调用会留下一个错设备的缓冲毒害后续所有同形状调用。
- Python 侧调用点：[python/fast_ulysses/group.py:202-226](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L202-L226)。注意 `caller = torch.cuda.current_stream(self.device)` 在 `with` **之前**（[group.py:202](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L202)），且取的是 `self.device` 而非当前设备——注释（[L198-201](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L198-L201)）解释：aten 算子跑在「x 的设备的当前流」上，传输必须与那条流排序，别的设备当前时无参形式会拿错流。
- pybind 边界：[src/bindings.cc:776-811](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L776-L811)。首行注释（[L775](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L775)）点出时序：Python 的 `import _C` 需要 `PYBIND11_MODULE`，而 `TORCH_LIBRARY` 在 dlopen 时就已注册完毕——两类注册共存于同一个 .so，各司其职。

#### 4.5.4 代码实践

**实践目标**：纸面追踪一次 async 调用穿过「op 与 pybind 两种出口」，并说出每个出口为什么在那一边。

**操作步骤**：

1. 从 [python/fast_ulysses/group.py:151](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L151) 读到 [L232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L232)，给每行标注它触达的是：torch op / pybind 函数 / 纯 Python。
2. 对 `_C.register_stream_completion(y, self._comm_stream.cuda_stream)`（[group.py:227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L227)），对照 [src/bindings.cc:778-783](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L778-L783) 的注释，列出它做不成 op 的两个理由。

**需要观察的现象**：一次 async 调用混合使用了三种出口——staged op（要 dispatcher）、pybind 的 completion 注册（裸句柄 + 进程级注册表）、Python 的 `record_stream`（torch 原生 API）——每种出口恰好对应一种「参数或副作用能否写进 schema」的判定。

**预期结果**：第 2 步答案在注释里：它拿裸的流句柄作参数，且会改动进程范围的注册表（返回 `c10d::Work` 这个 C++ 接口），两者都不属于 schema。本实践纯阅读，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：`caller_stream_id` / `caller_device_index` 为什么必须由 Python 传入，而不是 C++ 内部取当前流？

答案：Python 在调用 op 之前已进入 `with torch.cuda.stream(self._comm_stream)`（[group.py:203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L203)），C++ 侧的「当前流」就是 comm 流；而 staging 拷贝必须在**调用者**的流上做，否则调用者张量就要靠 `record_stream` 跨流存活（[src/bindings.cc:389-392](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L389-L392) 的注释）。所以「用户原来的流」这一信息只能显式传参。

**练习 2**：`all_to_all_4d_staged` 的 Meta 内核如果老老实实返回正确形状（而不是抛错），会发生什么？

答案：trace 会「成功」，但 `all_to_all_4d_async` 这个 eager-only 的 Python 包装层随后会在真实 comm 流上记完成事件、把它交给 work registry、对结果 `record_stream`——被 trace 的调用因此在进程级注册表里留下一个永远无人等待的真实 Work，并返回一个 `wait()` 宣称传输已完成的 `AsyncCollectiveTensor`，而传输从未发射。所以注释说返回形状「不是半个答案，是错答案」（[src/bindings.cc:428-437](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L428-L437)）。

## 5. 综合实践

**任务**：枚举 `torch.ops.fast_ulysses` 命名空间的全部 op 与 schema，产出一张「op × dispatch key」表，与 `TORCH_LIBRARY` 注册段逐行对照。这是本讲所有模块的汇合点。

**操作步骤**：

1. 环境准备（按 u1-l3 构建；无 GPU 也能完成本实践，只需要 import 成功）：

   ```python
   import torch
   import fast_ulysses  # from . import _C → dlopen → TORCH_LIBRARY 注册
   ```

2. 枚举 schema。首选 torch 的 schema 数据库：

   ```python
   schemas = sorted(
       (str(s) for s in torch._C._jit_get_all_schemas()
        if s.name.startswith("fast_ulysses::")),
   )
   print("\n".join(schemas))
   ```

   预期恰好 6 条，名字与 [src/bindings.cc:717-772](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L717-L772) 的六条 `m.def` 一一对应。注意：`dir(torch.ops.fast_ulysses)` 未必列得出算子名——torch 的命名空间对象是惰性 `__getattr__`，未被访问过的算子可能不在 `dir()` 里；若遇到这种情况以 `_jit_get_all_schemas()` 为准（**待本地验证**，行为随 torch 版本可能不同）。

3. 逐 op dump 调度表：

   ```python
   for name in ("all_to_all_4d", "all_to_all_4d_out", "all_to_all_4d_staged",
                "all_to_all_4d_staged_out", "empty_output", "all_to_all_4d_timed"):
       print(f"=== {name} ===")
       print(torch._C._dispatch_dump(f"fast_ulysses::{name}"))
   ```

4. 把 dump 结果整理成 4.3.3 那张五列表格（CompositeExplicitAutograd / Autograd / Meta / FuncTorchBatched+Batched），逐格注明「内核函数名 / fallthrough / 未注册」。

**需要观察的现象**：

- 只有 `all_to_all_4d` 拥有真正的 Autograd 内核；`empty_output` 与 `all_to_all_4d_timed` 的 Autograd 条目是 fallthrough；其余三个无 Autograd 条目。
- 六个 op 全部带两个批量键的拒绝内核。
- schema 文本与源码字符串逐字符一致（别名注解、默认值、返回形态）。

**预期结果**：你的表格与 4.3.3 的表格完全一致；不一致的地方只有两种可能——torch 版本把别名键展开了不同的写法（无害），或源码已更新而本讲义基于的 HEAD（ad1a4a0）过时（此时以源码为准）。完整输出**待本地验证**。

## 6. 本讲小结

- **六个 op、四种投影**：`all_to_all_4d` / `_out` / `_staged` / `_staged_out` / `empty_output` / `_timed` 是一次集合通信在 functional/out、sync/async、辅助三个维度上的拆分，全部注册在唯一的 `TORCH_LIBRARY` 块里。
- **functional/out 拆分的根源是可变 schema**：`Tensor(a!)` 让 schema 变可变，`torch/library.py` 拒绝为可变算子注册 autograd 公式；拆开让 functional 拿到可微性与 Meta 内核、out 变体拿到诚实的别名注解与 `can_auto_functionalize` 接受的 `-> ()` 形状。
- **dispatch key 各司其职**：CompositeExplicitAutograd 是覆盖 Meta 的别名键，显式 Meta 注册覆盖它（真传播或显式拒绝）；唯一的 Autograd 内核经 `dispatch_below_autograd` 重新进调度器；`empty_output`/`timed` 用 fallthrough 避免给新缓冲建图；每个 op 都挂 `FuncTorchBatched` + `Batched` 双键的 `refuse_vmap`，把「vmap 逐元素循环导致 rank 挂起」变成显式报错。
- **UlyssesGroup 必须是 torchbind 类**：要进 schema、要进 autograd 的 `saved_data`、要挂方法——pybind 对象三者都做不到。
- **两个 debug 方法补的是结果证明不了的维度**：`epoch_debug` 区分「屏障活着」与「恰好没撕裂数据」，`staging_slots_debug` 区分「真并发」与「共享一槽的串行正确」。
- **op 与 pybind 的分界**：拿 group/张量、需要 dispatcher 语义的是 op；拿裸流句柄、改进程级注册表、纯主机查询的是 pybind 函数。

## 7. 下一步学习建议

下一讲 **u6-l2「一次调用的完整流水线：prepare → 屏障 → 传输 → 屏障 → copy_out」**深入 `prepare()` 的校验链与三条缓冲路径——本讲只看了接口皮，u6-l2 看皮之下的顺序纪律（全部校验先于第一个屏障）。之后 **u6-l3** 展开 `A2AFunction` 的数学（vjp = 逆排列 = `1 - mode`、splits 不交换），**u6-l4** 展开 Meta 内核与 FakeTensor、vmap 拒绝的实操。阅读源码时建议带着本讲的表格：每读一个内核函数，先问「它挂在哪个键上、谁会调到它」。
