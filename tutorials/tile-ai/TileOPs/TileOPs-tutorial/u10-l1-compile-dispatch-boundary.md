# u10-l1 编译边界不变量

## 1. 本讲目标

本讲聚焦一个问题：**当把一个 `Op` 交给 `torch.compile` 全图编译（`fullgraph=True`）时，TileOPs 如何保证 `dynamo` 的追踪不会撞上它无法理解的 TileLang 编译器代码？**

学完后你应当能够：

1. 复述「编译边界不变量」：一个被 dynamo 追踪的 `Op.forward` **不得构造 `Kernel`、不得进入 TileLang builder**，并理解为什么「编译前先 eager 预热」并不能修复它。
2. 解释 `id()` + `weakref` 这一对组合如何同时满足「实例查找」与「stale-graph 安全」。
3. 解释为什么分发用的 instance key 必须是**字符串**而非 int（int 会被 dynamo 泛化成不可哈希的 `SymInt`）。
4. 讲清一条「不可追踪路径」是如何被藏进 `torch.library.custom_op` 的 eager 体、并用 `_infer_output_shapes` 给 fake 体的：`forward` 收敛成一行单次分发调用。

> 本讲承接 u2-l1（`Op` 基类的 `dispatch_kernel`）与 u8-l1（`__init_subclass__` codegen）。本讲只讲**编译边界**这一道闸，不讲 custom_op 注册工厂的内部细节（那是下一讲 u10-l2 的内容）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**dynamo 与图捕获。** `torch.compile` 背后的追踪器叫 dynamo。它执行一遍你的 Python 代码，把「安全的、可符号化的」操作记录成一张计算图（FX graph），把「不安全」的操作（副作用、动态控制流、它不认识的对象）要么图断（graph break），要么直接拒绝。一张图一旦编译完成并被缓存，dynamo 会用一组**守卫（guards）**来决定下次调用能否复用这张图；守卫失败就重新编译。

**SymInt 与静态常量。** 在追踪时，张量的尺寸有时是「已知的具体整数」（静态），有时是 dynamo 推导出的符号整数 `SymInt`（动态）。`SymInt` 是为了支持动态形状而设计的，但代价是**它不可哈希**——而 Python 的字典查找、`torch.library.custom_op` 的特化键都依赖哈希。dynamo 对传入 `custom_op` 的参数有一条规定：**字符串参数会被当作静态常量烘焙（bake）进守卫**，而 int 参数则可能被泛化成 `SymInt`。这条规定正是本讲「字符串 key」的全部理由。

**TileLang 的 builder 不可追踪。** TileLang 的 `@T.prim_func` kernel 在**首次**被调用某组形状/dtype 时，要跑一遍 JIT 编译（builder）：解析签名、生成 TIR、编译成 CUDA。这一步大量依赖 Python 的 `inspect` 反射和动态代码生成，是 dynamo 完全无法追踪的。kernel 一旦编译好并缓存，后续调用只是「launch 一个现成的 CUDA kernel」，这一步 dynamo 同样看不见内部，但可以用 `custom_op` 标记为不透明调用。麻烦的正是「首次/cache miss」时撞上的 builder。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`tileops/ops/compile_boundary.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py) | 编译边界的全部实现：一个 weak 全局实例注册表 + 两个函数 `register_instance` / `get_instance`。模块 docstring 本身就是不变量的权威表述。 |
| [`tileops/ops/op_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py) | `Op.dispatch_kernel` 是唯一的「零样板注册点」：每个合规 `Op` 的 `__init__` 都经过它，于是顺带把自己登记进编译边界注册表。 |
| [`docs/design/ops-design.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md) | §Compile Dispatch Boundary 给出不变量、机制与约束（含字符串 key 与 stale-graph 安全的官方解释）。 |
| [`tileops/ops/pool.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py)（参考实现） | 最干净的规范采用者：`forward` 收敛成一行 `_pool_fwd(input, self._instance_key)`，`register_fake` 真正调用了 `_infer_output_shapes`。 |
| [`tileops/ops/norm/batch_norm.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/norm/batch_norm.py)（参考实现） | 另一个规范采用者，展示了多 kernel（fwd/bwd）与 `mutates_args` 的写法。 |

> 对照组（本讲会用来说明「旧式 int key」仍并存）：[`tileops/ops/elementwise/_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py)、`tileops/ops/rope.py`、`tileops/ops/dropout.py` 各自维护了一个**家族本地**的 `WeakValueDictionary`，key 是 int 而非字符串。这是历史遗留，与本讲的规范机制并存，第 4.3 节会如实说明。

## 4. 核心概念与源码讲解

### 4.1 编译边界不变量：dynamo 不得撞见 builder

#### 4.1.1 概念说明

「编译边界不变量」是一句话：

> 一个被 dynamo 追踪的 `Op.forward` **不得构造 `Kernel`，也不得进入 TileLang builder。**

它解决的问题是 **lazy-dispatch 算子**——即那些在 `forward` 里、根据**调用时**才确定的形状去查 kernel cache、cache miss 时现构造 kernel 的算子（典型代表是 pool 家族，形状在运行时从输入推断）。这种「构造 kernel」会触发 TileLang 的 JIT builder，而 builder 是 dynamo 完全无法追踪的。

一个常见的**错误直觉**是：「我在 `torch.compile` 之前先 eager 跑一次，把 kernel 编译好、填进 cache，dynamo 追踪时就只会命中 cache、不会撞 builder 了」。这条思路是错的——它只隐藏了 miss 路径，**并没有满足冷调用契约**：dynamo 仍会在追踪期把 `if cache_miss: build_kernel()` 这个分支记录进图，而那条分支的 builder 调用是 dynamo 看不懂的，于是要么图断、要么报错。正确做法是把整条「查 cache → 构造 → launch」藏到一个 dynamo 看不到内部的 `custom_op` 里。

#### 4.1.2 核心流程

```
用户:  torch.compile(op, fullgraph=True)(x)
       │
dynamo 追踪 op.forward
       │
       ▼
op.forward 必须是「dynamo 能追踪」的形态
   ├─ 不得: Kernel(...) 构造
   ├─ 不得: 进入 TileLang builder（cache miss 分支）
   └─ 允许: 一行分发调用 _family_fwd(x, self._instance_key)
            └─ custom_op 边界 ── dynamo 在此停手，只记一个不透明调用
                  ├─ eager 体: get_instance(key)._eager_forward(x)  ← 整条不可追踪路径都在这里
                  └─ fake 体:  用 _infer_output_shapes 推输出 shape
```

#### 4.1.3 源码精读

不变量的权威表述在 `compile_boundary.py` 的模块 docstring 里：

[compile_boundary.py:L1-L13](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L1-L13) — docstring 第 3–6 行写明不变量与机制：lazy-dispatch 算子把 `forward` 路由进一个 `custom_op`，其 eager 体在这里 resolve 实例、跑「cache 查找、kernel 构造、launch」这条不可追踪路径。

设计文档里同样的表述（更详细，且点名了「eager 预热不算数」）：

[docs/design/ops-design.md:L270-L279](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L270-L279) — §Compile Dispatch Boundary 的「Invariant」段：cache miss 会跑 dynamo 无法追踪的 JIT 机制；eager 预热只藏了 miss 路径，不满足冷调用契约。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把「为什么 eager 预热无效」用自己的话讲一遍。
2. **步骤**：
   - 打开 [docs/design/ops-design.md:L275-L279](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L275-L279)。
   - 想象一个 lazy-dispatch 算子的 `forward` 形如：
     ```python
     # 示意代码（非项目原有，仅为说明）
     def forward(self, x):
         key = self._cache_key(x.shape)
         kernel = self._kernel_cache.get(key)
         if kernel is None:                 # ← cache miss 分支
             kernel = self.kernel_map["k"](x.shape, ...)   # ← 进入 builder
             self._kernel_cache[key] = kernel
         return kernel(x)
     ```
   - 解释：即便预热让 `kernel is None` 在运行期为假，dynamo 追踪期仍会把这条 `if` 与 `kernel = self.kernel_map[...](...)` 纳入图。
3. **观察 / 预期**：你会得出「必须把整段藏进 custom_op，而不是靠预热」的结论。这与设计文档一致。

#### 4.1.5 小练习与答案

- **练习 1**：不变量是「不得构造 Kernel」。那么「调用一个**已经构造好**的 kernel」算不算违反不变量？
  - **答案**：不算违反「构造」这一条，但 kernel launch 对 dynamo 仍然是不透明的，仍需用 `custom_op` 把这次 launch 标记为不透明调用。不变量针对的是「构造/builder」，而「不透明 launch」是另一个（更轻的）问题，二者都靠 `custom_op` 解决，但动机不同。
- **练习 2**：为什么「`torch.compile` 前先跑一次」不能让一个 lazy-dispatch 算子通过编译？
  - **答案**：dynamo 在追踪期会把 `if cache_miss` 分支连同 builder 调用一起记录进图；预热只改变了运行期的真假，没有改变追踪期的图结构，冷调用契约仍未满足。

---

### 4.2 weak 实例注册表：`id()` + `weakref` 兼顾查找与 stale-graph 安全

#### 4.2.1 概念说明

要把「实例」从 `custom_op` 的 eager 体里找回来，最朴素的办法是用 `id(op)` 当 key、用一个全局 `dict` 存 `{id(op): op}`。但全局 `dict` 会**强引用** op，导致 op 永远无法被垃圾回收——这在长生命周期的训练循环里是内存泄漏。

更危险的是 **stale-graph（陈旧图）问题**：Python 的 `id()` 在对象被回收后**会被复用**给新对象。设想实例 A 被编译成图 G（dynamo 用 A 的身份做守卫），随后 A 被回收、`id(A)` 被复用给新实例 B。如果注册表里 `id` 仍能「命中」一个对象，B 就可能被错误地塞进为 A 编译的陈旧图 G 里——这是静默的错误。

TileOPs 的解法是两个弱引用机制叠加：注册表本身用 `weakref.WeakValueDictionary`，且 dynamo 自己的 `ID_MATCH` 守卫也持弱引用。

#### 4.2.2 核心流程

```
Op.__init__
   └─ dispatch_kernel()
         └─ register_instance(self)
               key = str(id(self))            # 字符串 key（4.3 节解释为何是字符串）
               _OP_REGISTRY[key] = self       # WeakValueDictionary：弱引用 self

# 当 self 被回收：
#   _OP_REGISTRY[key] 自动消失（weakref）→ 不会泄漏，也不会让 key 命中新对象

# dynamo 侧：
#   为 self 编译的图 G，守卫里持 self 的弱引用
#   self 死 → 守卫失败 → 强制重编译 → id() 即使被复用，也无法复用陈旧图 G
```

#### 4.2.3 源码精读

注册表本身只有一个容器：

[compile_boundary.py:L17-L17](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L17) — `_OP_REGISTRY: weakref.WeakValueDictionary[str, object]`，弱值字典：值（op 实例）一旦没有别处强引用，条目自动消失。

注册与查找两个函数都极短：

[compile_boundary.py:L20-L24](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L20-L24) — `register_instance(op)` 用 `str(id(op))` 当 key 存入弱值字典并返回该 key。

[compile_boundary.py:L27-L29](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L27-L29) — `get_instance(key)` 反查；key 找不到会抛 `KeyError`。

`Op.dispatch_kernel` 是唯一的零样板注册点——所有合规 `__init__` 都经过它，因此注册只需写在这里一处：

[op_base.py:L10-L10](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L10) — `from .compile_boundary import register_instance`。

[op_base.py:L192-L197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L192-L197) — `dispatch_kernel` 先调 `_install_kernel_map`（u2-l2 讲过），再 `self._instance_key = register_instance(self)`。注释点明这是「为编译分发边界做的零样板注册点」。

stale-graph 安全的官方解释在设计文档里：

[docs/design/ops-design.md:L300-L302](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L300-L302) — dynamo 的 `ID_MATCH` 守卫持编译可调用对象的弱引用；实例死亡 → 强制重编译；于是被复用的 `id()` 无法对陈旧图生效。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：用 `id()` + weakref 解释「查找」与「stale-graph 安全」如何同时成立。
2. **步骤**：
   - 读 [op_base.py:L192-L197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L192-L197) 与 [compile_boundary.py:L17-L24](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L17-L24)。
   - 回答：为什么用普通 `dict` 而非 `WeakValueDictionary` 会同时引发「泄漏」和「stale-graph 命中」两个问题？
3. **预期结果**：普通 `dict` 强引用 op → 泄漏；且 op 死后条目不消失 → `id()` 被复用时 `get_instance` 仍能命中一个**不同的**新对象。`WeakValueDictionary` 让条目随 op 死亡而消失，配合 dynamo 的弱 `ID_MATCH` 守卫，双重保证「实例活着才能命中，实例死了图也作废」。

#### 4.2.5 小练习与答案

- **练习**：如果 `register_instance` 用 `key = id(op)`（int）而注册表仍是 `WeakValueDictionary`，stale-graph 安全还成立吗？
  - **答案**：weakref 这一层仍成立（条目随实例消失），stale-graph 安全也仍成立（由 dynamo 的 `ID_MATCH` 弱守卫保证）。但 int key 会在「同一帧里第二个实例编译」时引发另一类问题——这正是下一节的主题。也就是说，weakref 解决「生死/泄漏」，字符串解决「SymInt 泛化」，二者是**正交**的两个问题。

---

### 4.3 字符串 key：为何优于 int key（SymInt 不可哈希）

#### 4.3.1 概念说明

`custom_op` 的参数会进入 dynamo 的守卫与特化键。dynamo 对这些参数的规则是：

- **字符串参数** → 当作**静态常量**烘焙进守卫。不同的字符串值 → 不同的守卫 → 不同的特化图。
- **int 参数** → 可能被**泛化**成符号整数 `SymInt`，以支持动态形状。

问题出在 int 泛化上：当**同一帧**里有第二个实例也经过这个 `custom_op` 编译时，dynamo 会把那个 int 参数提升为 `SymInt`，而 `SymInt` **不可哈希**。一旦不可哈希，`custom_op` 内部用于特化/缓存的字典查找就会抛错，编译失败。

因此 TileOPs 的规范设计要求：**instance key 必须是字符串**。`str(id(op))` 既保留了 `id()` 的唯一性，又获得「静态常量」的烘焙待遇。

#### 4.3.2 核心流程

```
key = str(id(op))           # "1397..."，字符串
                             #
forward: _pool_fwd(x, key)  # 字符串实参 → dynamo 当静态常量烘焙
                             #   守卫: key == "1397..."  → 命中本实例的特化图
                             # 不同实例 → 不同字符串 → 不同守卫 → 各自一张图，互不干扰

# 对比：key = id(op)（int）
#   第二个实例同帧编译 → dynamo 把 int 提升为 SymInt → SymInt 不可哈希 → custom_op 报错
```

#### 4.3.3 源码精读

设计文档的官方解释（与 `compile_boundary.py` docstring 一致）：

[docs/design/ops-design.md:L297-L302](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L297-L302) — 「key 是字符串：dynamo 把字符串 custom_op 参数烘焙为静态常量；而 int key 在第二个实例经同一帧编译时会被泛化成不可哈希的 SymInt。stale-graph 安全来自 dynamo 的 ID_MATCH 守卫持弱引用。」

规范采用者 `pool.py` 与 `batch_norm.py` 都把 key 标注为 `str` 并用 `get_instance` 查全局表：

[pool.py:L27-L27](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L27-L27) — `from .compile_boundary import get_instance`。

[pool.py:L915-L917](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L917) — `_pool_fwd(input, instance_key: str)` 的 eager 体 `return get_instance(instance_key)._eager_forward(input)`，参数类型显式是 `str`。

[batch_norm.py:L34-L34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/norm/batch_norm.py#L34-L34) — `from ..compile_boundary import get_instance`。

[batch_norm.py:L470-L485](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/norm/batch_norm.py#L470-L485) — `top::norm_batch_norm_fwd` 的 eager 体，`instance_key: str` + `get_instance(instance_key)`，并演示了 `mutates_args=("running_mean","running_var")` 的写法。

> **如实说明：仓库里并存着旧式 int key。** elementwise / rope / dropout 三个家族各自维护了一个**家族本地** `WeakValueDictionary`，key 是 `id(self)`（int），custom_op 签名里写的是 `instance_key: int`，且**没有**走 `compile_boundary.py` 的全局表。例如：
> - [elementwise/_base.py:L30-L34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L30-L34) 模块级 `_OP_REGISTRY = weakref.WeakValueDictionary()`，注释还残留「key is a plain int」的说法——这与规范设计相抵触。
> - [elementwise/_base.py:L139-L141](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L139-L141) `_wrapped(x, instance_key: int)` 用本地表查找。
> - 这些家族的 `__init__` 会先经 `dispatch_kernel`（在全局表里登记一个字符串 key），随后**用 `id(self)` 覆盖** `self._instance_key` 并登记进本地 int 表，例如 [elementwise/_base.py:L599-L605](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L599-L605)。
>
> 这是历史遗留，规范机制（`compile_boundary.py` + pool/batch_norm）是较新的收敛方向。阅读时以 `compile_boundary.py` 与 `pool.py` 为「应该长这样」的范本，把 elementwise/rope/dropout 视为待迁移的旧实现。这也呼应了项目「逐 op、逐 PR 迁移」的信任模型节奏。

#### 4.3.4 代码实践（源码阅读型 + 本地验证）

1. **目标**：亲手比对「规范字符串 key」与「旧式 int key」两种写法，理解 int key 在多实例同帧编译下的风险。
2. **步骤**：
   - 读 [pool.py:L915-L924](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L924)（字符串）与 [elementwise/_base.py:L138-L148](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L138-L148)（int）。
   - （待本地验证）在有 GPU 的机器上，构造**两个不同的** elementwise op 实例，把它们放进同一个被 `torch.compile(fullgraph=True)` 的函数里先后调用，观察是否触发 `SymInt` 相关的哈希/特化错误；再用两个 pool 实例对照。
3. **观察 / 预期**：int key 路径在「同一帧、多个实例」时更容易撞上 SymInt 泛化问题；字符串 key 路径因静态烘焙而无此问题。**该对比的实际报错形态以本地复现为准（待本地验证）**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么不能用「实例的某个属性字符串」（如 op 名）当 key，而要用 `str(id(op))`？
  - **答案**：op 名在同类多个实例间会重复，无法区分不同实例；而 `id(op)` 在任一时刻唯一标识一个活对象，`str(id(op))` 既有唯一性又是字符串，满足「静态常量 + 区分实例」。
- **练习 2**：字符串 key 会被烘焙为静态常量。这意味着同一 op 类的 N 个实例会编译出 N 张图吗？这是不是「缓存爆炸」？
  - **答案**：是的，每个实例身份对应一张特化图。这是「为每个实例身份单独编译」的代价，换取的是「不在追踪期撞 builder」。实例死亡后图因弱守卫而失效、注册表条目也消失，长生命周期里由 weakref 兜底，不会无限堆积。

---

### 4.4 custom_op 三件套：eager 体 / fake / forward 单分发

#### 4.4.1 概念说明

`torch.library.custom_op` 注册一个 dynamo 看不见内部的算子。它需要三样东西：

1. **eager 体**：真正执行的函数。dynamo 在此停手，因此可以把「查 cache、构造 kernel、launch」整条不可追踪路径放进来。
2. **fake 体**（`@xxx.register_fake`）：dynamo 追踪时用来推「输出张量的 shape/dtype」的函数。它**不能**碰真张量数据，只能根据输入的 meta 信息算出输出 meta。规范做法是调用 Op 的 `_infer_output_shapes`。
3. **forward 的改写**：原本的 `forward` 函数体被搬到 `_eager_forward`，新的 `forward` 收敛成一行单次分发调用，把 `self._instance_key` 作为字符串实参传进 custom_op。

设计文档把这三步明确列出：

[docs/design/ops-design.md:L281-L293](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L281-L293) — Mechanism 三步：① `dispatch_kernel` 在 `__init__` 登记；② 家族为每种输出 arity 定义一个 custom_op，eager 体 resolve 实例调 `_eager_forward`，fake 用 `_infer_output_shapes` 推 shape；③ `forward` 变成 `return _family_fwd(input, self._instance_key)`，旧体改名 `_eager_forward` 原样保留。

#### 4.4.2 核心流程（以 pool.py 为范例）

```
# 1) __init__ 期：dispatch_kernel 登记实例（4.2 节）
op._instance_key = str(id(op))

# 2) custom_op 三件套（模块级，加载时注册一次）
@torch.library.custom_op("top::pool_fwd", mutates_args=())
def _pool_fwd(input, instance_key: str) -> torch.Tensor:
    return get_instance(instance_key)._eager_forward(input)   # eager：不可追踪路径

@_pool_fwd.register_fake
def _pool_fwd_fake(input, instance_key: str) -> torch.Tensor:
    op = get_instance(instance_key)
    shapes = op._infer_output_shapes(tuple(input.shape))       # fake：只推 meta
    return input.new_empty(shapes["output"])

# 3) forward 收敛成一行
def forward(self, input):
    return _pool_fwd(input, self._instance_key)
```

#### 4.4.3 源码精读

`pool.py` 是最干净的范例。`forward` 真的就是一行：

[pool.py:L280-L281](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L280-L281) — `def forward(self, input): return _pool_fwd(input, self._instance_key)`。无校验、无构造、无分支——dynamo 追踪它只会得到一个不透明调用节点。

custom_op + fake 体：

[pool.py:L915-L924](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L924) — `_pool_fwd` eager 体 `get_instance(instance_key)._eager_forward(input)`；`_pool_fwd_fake` 调 `op._infer_output_shapes(tuple(input.shape))` 取 `shapes["output"]` 造空张量。这正是设计文档「fake 用 `_infer_output_shapes`」的落地——注意 `_infer_output_shapes` 接收的是**纯 shape 元组**、不碰真张量，因此可安全用于 fake。

被改名的旧 `forward` 体内核：

[pool.py:L283-L286](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L283-L286) — `_eager_forward` 才是原来「解析输入、contiguous、launch kernel」的实体；它只在 custom_op 的 eager 体（非追踪路径）里被调用。

多输出（带 indices 的 max-pool）与 `mutates_args` 的进阶写法：

[batch_norm.py:L518-L527](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/norm/batch_norm.py#L518-L527) — BatchNorm 的 `forward` 同样收敛成一行分发；[batch_norm.py:L470-L473](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/norm/batch_norm.py#L470-L473) 的 custom_op 用 `mutates_args=("running_mean","running_var")` 声明原地修改，让 dynamo 正确追踪副作用。

> 边界的适用范围（设计文档约束）：

[docs/design/ops-design.md:L303-L309](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L303-L309) — 边界只覆盖前向编译；若编译图需要反向，还须为分发 custom_op 注册 autograd 公式；**构造期即建好 kernel 的算子不需要这道边界**（见 4.4.5 与综合实践）。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：用 pool.py 串起「eager / fake / forward」三件套与 `compile_boundary` 的关系。
2. **步骤**：
   - 依次读 [pool.py:L280-L281](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L280-L281)（forward 单分发）、[pool.py:L915-L924](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L924)（custom_op + fake）、[pool.py:L283-L286](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L283-L286)（`_eager_forward`）。
   - 画出调用链：`forward → _pool_fwd(custom_op) → get_instance(key) → _eager_forward → kernel launch`，并标出「dynamo 在哪一步停手」。
3. **预期结果**：dynamo 在 `_pool_fwd` 这一步停手（记一个不透明节点），`get_instance` / `_eager_forward` / kernel 构造都在追踪期之外运行。

#### 4.4.5 小练习与答案

- **练习**：fake 体为什么必须调 `_infer_output_shapes`，而不能像某些简单算子那样直接 `torch.empty_like(input)`？
  - **答案**：当输出 shape 不等于输入 shape（如 pool 的下采样、带 indices 的多输出）时，`empty_like` 给不出正确 meta，fullgraph 编译就会因 shape 不符而失败。`_infer_output_shapes` 是 Op 层「纯 shape 推断」的 codegen 契约（u8-l1），专门为这种「不构造张量也能算输出 shape」的场景服务，因此是 fake 体的正确数据源。简单算子（如 unary elementwise，输出同形）用 `empty_like` 只是特例。

## 5. 综合实践

**任务**：用本讲的三块积木（不变量 / weak 注册表 / 字符串 key / custom_op 三件套），给一个假想的 lazy-dispatch 算子**接上编译边界**，并解释「构造期已建 kernel 的算子为何不需要这道边界」。

1. **第一问 — 接线**：假设有个 `FooFwdOp`，其 `forward(x)` 里会 `key = (x.shape, x.dtype); kernel = self._kernel_cache.get(key) or self._build(key)`。请按规范写出：
   - `__init__` 里只需 `self.dispatch_kernel(kernel_map)`（它已替你 `register_instance`）；
   - 模块级 `@torch.library.custom_op("top::foo_fwd", mutates_args=())` 的 eager 体：`return get_instance(instance_key)._eager_forward(x)`，签名里 `instance_key: str`；
   - `register_fake` 体：`op = get_instance(instance_key); shapes = op._infer_output_shapes(tuple(x.shape)); return x.new_empty(shapes["output"])`；
   - `forward` 改成 `return _foo_fwd(x, self._instance_key)`，原体改名 `_eager_forward`。
   - 对照 [pool.py:L915-L924](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L924) 自检写法是否一致。
2. **第二问 — 边界豁免**：阅读 [docs/design/ops-design.md:L307-L309](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L307-L309)，解释「构造期即建好 kernel 的算子不需要这道边界」。
   - **参考答案**：这类算子的 `forward` **不构造 kernel、不进 builder**——kernel 在 `__init__` 就建好了。于是 4.1 的不变量在「构造」维度上被空真（vacuously）满足，`forward` 里没有那条会让 dynamo 撞墙的 cache-miss 分支，自然不需要用「弱注册表 + 字符串 key + lazy eager 体」这套机制去藏它。（它仍可能需要一个更简单的 custom_op 来标记不透明的 kernel launch，但那是 launch 不透明性问题，不是 lazy-dispatch 的 builder 问题，二者动机不同。）例如 elementwise 的 `UnaryOp` 在 `__init__` 里就 `self.kernel = self._build_kernel_instance(...)`（[elementwise/_base.py:L599-L601](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L599-L601)），其 `forward` 不再构造 kernel，故其 custom_op 只需承担 launch 不透明性，而不依赖 lazy 分发。

   > 注意区分：构造期建 kernel 的算子（如 `UnaryOp`）「不需要 lazy 分发边界」，但仓库里它们仍注册了（旧式 int key 的）custom_op——这正说明「藏 builder」与「藏 launch」是两件事。规范的 lazy 分发边界（字符串 key + 全局 weak 表）专门服务于「forward 会构造 kernel」的算子，如 pool、batch_norm。

## 6. 本讲小结

- **不变量**：被 dynamo 追踪的 `Op.forward` 不得构造 `Kernel`、不得进 TileLang builder；eager 预热只藏 miss 路径，不满足冷调用契约。
- **机制**：把不可追踪路径藏进 `torch.library.custom_op` 的 eager 体（resolve 实例 → `_eager_forward`），fake 体用 `_infer_output_shapes` 推输出 meta，`forward` 收敛成一行单次分发。
- **weak 注册表**：`compile_boundary._OP_REGISTRY` 是 `WeakValueDictionary`，`register_instance` 在 `Op.dispatch_kernel` 这一个零样板点登记；weakref 让条目随实例死亡消失，避免泄漏。
- **stale-graph 安全**：`id()` 会被复用，但 dynamo 的 `ID_MATCH` 守卫持弱引用，实例死则图作废，复用的 `id()` 无法命中陈旧图。
- **字符串 key**：dynamo 把字符串 custom_op 参数烘焙为静态常量；int key 在「同帧第二个实例编译」时被泛化成不可哈希的 `SymInt`。规范采用者是 pool.py / batch_norm.py。
- **如实记录**：elementwise / rope / dropout 仍用旧式 int key + 家族本地表，与规范并存，属待迁移状态；阅读时以 `compile_boundary.py` + `pool.py` 为范本。

## 7. 下一步学习建议

- **u10-l2 custom_op 注册工厂**：本讲只把 `custom_op` 当「黑盒边界」用。下一讲进入 `tileops/ops/elementwise/_base.py` 的 `_register_unary/binary/...` 工厂、`_wrapped_inplace` 的 `mutates_args` 分发、以及 `register_fake` 里 `broadcast_shapes` 如何支撑 fullgraph——你会看到这套旧式 int key 机制的全貌。
- **回看 u8-l1**：本讲的 fake 体依赖 `_infer_output_shapes` 这一 codegen 契约；建议重读 u8-l1 的 `__init_subclass__`，确认 `_infer_output_shapes` 的方法体确实是 codegen 合成的纯 shape 推断。
- **动手验证**：在有 Hopper GPU 的机器上，仿照 [tests/test_compile.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_compile.py) 的 `torch.compile(op, fullgraph=True)` 模式，分别编译一个 pool 实例（规范边界）与一个 elementwise 实例（旧式边界），用 `TORCH_LOGS="dynamic"` 观察 dynamo 在 custom_op 处停手、不进 builder 的日志证据（待本地验证）。
