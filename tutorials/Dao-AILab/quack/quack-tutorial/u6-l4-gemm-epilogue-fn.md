# @gemm_epilogue 函数式创作

## 1. 本讲目标

本讲是「可组合 Epilogue 系统」系列的第四篇。前几讲我们见过两件事：一是手写 mixin（如 `GemmDefaultEpiMixin`），它直接重写 `epi_visit_subtile`，逐行写好寄存器里的线性数学；二是 `EpiOp` 词汇表（`Scalar`/`RowVecLoad`/`TileStore`/`VecReduce`…），每种张量资源是一个自管的、带生命周期的对象。

本讲要解决的问题是：**能不能不写 mixin，只写一个普通的 Python 函数（像 FlexAttention 的 `score_mod` 那样），就得到一个融合 GEMM epilogue？** 答案就是 `@gemm_epilogue`。

读完本讲，你应该能够：

- 理解 `@gemm_epilogue` 的「函数式创作」模型：一个对累加器逐元素求值的函数如何被降低（lower）到 `EpiOp` 机制上。
- 掌握 `fn_port` 值端口协议：`row` / `col` / `tile` / `scalar` / `value` / `apply` / `sink` 七种端口如何让一个 op 接入函数的数据流，且前端只根据这一个属性分发，**绝不 `isinstance` 分发**。
- 理解「铸模」（mint）与注册机制：装饰时如何为每个 `(语义指纹, 操作数种类, SM, 模式)` 铸造出一个标准的 `ComposableEpiMixin` 子类，以及它如何经 `GemmClassRef` 跨进程进入 JIT 缓存。
- 学会用一句话权衡「函数式 mod」与「手写 mixin」的取舍。

## 2. 前置知识

本讲默认你已经掌握 u6-l1（`ComposableEpiMixin` 与 `EpiOp` 生命周期）和 u6-l2（`EpiOp` 词汇表）。需要回忆的关键概念：

- **EpiOp 的设备侧生命周期**：`begin → begin_loop → end_loop → end`，这是 smem/TMA/flush 的资源协议。
- **EpiOp 的主机侧 schema 三件套**：`host_arg_key`（编译键描述符）/ `host_fake_arg`（从描述符重建 trace 期假张量）/ `host_call_arg`（每次调用的运行期实参）。
- **`_epi_ops` 声明即全集、执行即子集**：mixin 在 `__init_subclass__` 用类级 `_epi_ops` 生成 `EpilogueParams`，运行期再把 inactive 的 op 过滤出编译产物。
- **CuTe-DSL 控制流**：`const_expr(...)` 把判断标为编译期分支；`cutlass.range(..., unroll_full=True)` 完全展开。这两个会在本讲的「逐元素循环」里反复出现。

还需要一个直觉：**GEMM 的累加器（accumulator, 简称 acc）是寄存器里的一个 tile 片段**，epilogue 就是「acc 写回显存前对它做的最后一道加工」。本讲的主角就是把这道加工写成一个函数。

> 一个心智模型：把 epilogue 想象成 `D = f(acc, 各种资源)`。手写 mixin 是「自己编排这个 `f` 的循环」；`@gemm_epilogue` 是「只写 `f` 本身，循环由框架生成」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `quack/epilogue/frontend.py` | 函数式前端的「主机半边」：`gemm_epilogue` 装饰器、`EpiMod` 类（铸模 + 计划缓存 + 启动）、`EpiPlan`/`StaticEpi`/`epilogue_from_class` 逃生出口。 |
| `quack/epilogue/visit.py` | 函数式前端的「设备半边」：`_EpiModMixinBase`，把铸造好的类属性驱动成 trace 期的逐元素访问 / 预扫描循环。 |
| `quack/epilogue/library.py` | 现成 mod 库：用 `@gemm_epilogue` 写好的 linear/bias、激活、RMS-fused、gated、RoPE、LSE 等十几个融合 epilogue。 |
| `quack/epilogue/ops.py` | `EpiOp` 基类与全部具体 op（u6-l2 讲过）。本讲只引用其中 `fn_port` 与 `fn_prepare`/`fn_apply`/`fn_sink_flush` 三个端口方法。 |
| `quack/epilogue/math.py` | 函数体用的「值词汇」：`Pair`（双 lane 值）、`F2`（打包 f32x2）、`pack`/`unpack`、`pexp`。 |

调用关系一句话：`library.py` 里的每个 `@gemm_epilogue` 函数 → `frontend.py` 的 `EpiMod` → 铸造出一个继承 `_EpiModMixinBase`（来自 `visit.py`）和某 SM 基类（如 `GemmSm100`）的内核类 → 该类的设备侧循环由 `visit.py` 通用驱动，资源生命周期复用 u6-l1/u6-l2 的 `ComposableEpiMixin` 机制。

## 4. 核心概念与源码讲解

### 4.1 @gemm_epilogue fn 前端：把 epilogue 写成普通函数

#### 4.1.1 概念说明

`@gemm_epilogue` 的设计目标，用 frontend 模块文档字符串原话是「FlexAttention-style epilogue authoring: a plain Python function over the accumulator, lowered onto the EpiOp machinery」——把一个对累加器逐元素求值的普通 Python 函数，降低到已有的 `EpiOp` 机制上。

理解这套设计的关键是分清两个「接入点」（composition site vs extension site）：

- **函数是组合点（composition site）**。计算顺序由用户的源码显式写明——`rope(acc) * alpha` 和 `rope(acc * alpha)` 是两段不同代码，一眼可见、可 review。这取代了手写 mixin 里那种隐式的 `EVT` 树或调度列表。CuTe-DSL 的 trace 本来就会把函数内联，所以不需要再发明一个「epilogue IR」——下面那层 MLIR 会优化掉共享子表达式。
- **EpiOp 是扩展点（extension site）**。任何人加一个 op（新的归约、量化存储、带预取的表加载），只需写一遍资源生命周期，再加**一个端口方法**，就能从任意函数里使用，并与其它所有 op 组合，主机管线、缓存、启动全部继承自 schema。

函数的契约是：

```text
fn(acc, **operands) -> {"D": ..., <outputs/sinks>...}
```

- `acc` 第一个参数，是累加器的一个元素（SM100 上是打包成 `F2` 的两个相邻 f32，pre-SM100 是标量 Float32）。
- `**operands` 是 `acc` 之后的形参名，种类由张量元数据在 plan 期推断。
- 返回一个 dict：`"D"` 可选（缺省表示保留原始累加器直接写回），其余键是声明的输出或归约（sink）。

> 直觉：函数被「逐元素」调用，你只负责一个元素的 `D = f(acc, ...)`，框架负责把这个调用铺到整个 tile 上，并复刻手写 mixin 的循环形状（SM100 上打包 f32x2 lane、gated 下 pair 视图）。

#### 4.1.2 核心流程

从装饰到启动，链路是：

```text
@gemm_epilogue(...) def my_fn(acc, bias): ...
        │  装饰器把 fn 包成 EpiMod 实例，登记到 TORCH_OP_EPI_MODS
        ▼
my_fn.gemm(A, B, D, C, epi_args={...}, tile_M=, tile_N=, ...)
        │  ① 校验 + 由张量元数据推断每个 operand 的 kind
        │  ② 拼出 mint_key = (visit_sig, SM, paired, packed_c, prepass, rounding, arg_forms, add_to_output)
        │  ③ EpiMod._mint(*mint_key)  →  铸造一个内核类（缓存于 self._minted[key]）
        │  ④ build_gemm_epi_plan(铸造类, ...) → 计划（缓存于 self._plan_cache[raw_key]）
        │  ⑤ run_gemm_epi_plan(plan, ...)   →  真正启动（命中时只走 ⑤）
        ▼
铸造类的设备侧循环（visit.py 的 epi_visit_subtile）逐元素调用 fn
```

两个缓存层次值得记住：

- **`self._minted[mint_key]`**：把 `(种类签名, SM, ...)` 映射到铸造成的内核类对象。同一 EpiMod 在不同形状/数据下，只要种类签名和模式相同，就复用同一个类。
- **`self._plan_cache[key]`**：把原始调用输入的张量元数据（dtype+shape+stride）映射到启动计划。命中时只换数据指针启动（见 4.3）。

#### 4.1.3 源码精读

**装饰器本体**非常薄——它只是把 `fn` 和声明转发给 `EpiMod` 构造器：

```python
def gemm_epilogue(outputs=(), ops=None, reduces=None, mode=None, paired=(),
                  outs=None, prepass=None, prepass_outs=(), extra_ops=(), vectorize=None):
    ...
    def wrap(fn):
        return EpiMod(fn, outputs=outputs, ops=ops, reduces=reduces, mode=mode,
                      paired=paired, outs=outs, prepass=prepass,
                      prepass_outs=prepass_outs, extra_ops=extra_ops, vectorize=vectorize)
    return wrap
```

装饰器的全部参数是「资源声明」，与函数体正交：`outputs=` 声明辅助 tile 存储，`reduces=` 声明行/列归约（`sink` 端口），`ops=` 在推断有歧义时钉死某个 operand 的 op 类型，`mode=` 声明配对/打包模式。见 [quack/epilogue/frontend.py:1737-1792](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L1737-L1792)，装饰器把 `fn` 与资源声明一起交给 `EpiMod`。

**`EpiMod.__init__`** 做四件事：解析声明、校验、构造语义指纹、登记。下面是其中「校验函数签名 + 构造语义指纹」的精要：

```python
sig = inspect.signature(fn)
params = list(sig.parameters)
if not params or params[0] != "acc":
    raise ValueError("epilogue fn must take 'acc' first")
self.operand_names = tuple(params[1:])
...
self.semantic_key = (
    _function_semantic_key(fn),
    _function_semantic_key(prepass) if prepass is not None else None,
    self.outputs, self.mode, self.prepass_outs,
    tuple(op.cache_key() for _, op in sorted(self.ops.items())),
    tuple(op.cache_key() for _, op in sorted(self.sinks.items())),
    tuple(op.cache_key() for _, op in sorted(self.output_ops.items())),
    tuple(op.cache_key() for op in self.extra_ops),
    ("vectorize", self.vectorize),
)
self.semantic_digest = hashlib.sha256(repr(self.semantic_key).encode()).hexdigest()
self._ident = f"{fn.__name__}_{self.semantic_digest[:16]}"
self._minted = {}
self._plan_cache = {}
TORCH_OP_EPI_MODS[self.semantic_digest] = self  # quack::gemm_epi resolution
```

要点：`acc` 必须是第一个形参；`semantic_key` 深度指纹函数源码（及其引用的全部全局/闭包变量，递归）加上每个 op 的 `cache_key()`；最后把这个 `EpiMod` 按摘要登记进 `TORCH_OP_EPI_MODS` 全局表，供 `quack::gemm_epi` 自定义算子反查。见 [quack/epilogue/frontend.py:382-413](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L382-L413)，这里完成签名校验、语义指纹计算与全局登记。

**铸模 `_mint`** 是前端最核心的魔法。它根据种类签名动态 `type(...)` 出一个继承 `_EpiModMixinBase` 和该 SM 基类的内核类，并把用户函数挂成类属性：

```python
cls = type(
    cls_name,
    (_EpiModMixinBase, _SM_BASE[sm]),
    {
        "_epi_ops": tuple(epi_ops),
        "_epi_mod_fn": staticmethod(self.fn),
        "_epi_mod_operands": kind_sig,
        "_epi_mod_outputs": self.outputs,
        "_epi_mod_sinks": tuple(self.sinks),
        "_epi_mod_group_n": 2 if paired_acc else 1,
        "_epi_mod_packed_cd": packed_c,
        ...
        "EpilogueArguments": Args,
        "__module__": __name__,
        "__qualname__": cls_name,
    },
)
setattr(sys.modules[__name__], cls_name, cls)  # 便于检查与进程内复用
self._minted[key] = cls
return cls
```

请仔细体会：铸造出来的类**就是一个标准的 `ComposableEpiMixin` 子类**（它继承 `_EpiModMixinBase`，后者继承 `ComposableEpiMixin`）。它的 `_epi_ops`、`EpilogueArguments` 与手写 mixin 完全同构；唯一的区别是它的逐元素循环不在自己写的 `epi_visit_subtile` 里，而是由 `visit.py` 的通用实现根据 `_epi_mod_fn`/`_epi_mod_operands` 等类属性驱动。这就是文档里那句「fn form 是同套机制上的捷径，绝非第二套框架」的含义。见 [quack/epilogue/frontend.py:525-555](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L525-L555)，铸造类并登记到模块命名空间与 `self._minted` 缓存。

`_SM_BASE` 是 SM 版本到基类的映射表，注意 SM100（`10`）和 SM110（`11`，即更早的 Blackwell 编号）都映射到 `GemmSm100`：

```python
_SM_BASE = {8: GemmSm80, 9: GemmSm90, 10: GemmSm100, 11: GemmSm100, 12: GemmSm120}
```

见 [quack/epilogue/frontend.py:204-214](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L204-L214)，SM 选类表与 `kind → op` 的内置映射。

#### 4.1.4 代码实践

**实践目标**：亲手写一个最简单的函数式 epilogue `bias_epi`（数学 \(D_{mn} = \text{acc}_{mn} + b_n\)），并跟踪它从装饰到铸造的路径。

**操作步骤**（源码阅读 + 可选 GPU 运行）：

1. 阅读上文的装饰器与 `EpiMod.__init__`，确认 `acc` 必须是首参。
2. 把下面这段「示例代码」写进一个 `.py` 文件（**注意**：CuTe-DSL 靠 `inspect.getsourcelines()` 读源码，不能直接在 REPL 里定义；必须落盘，见 u1-l4）：

```python
# 示例代码：一个只做 D = acc + bias 的融合 epilogue
from quack.epilogue import gemm_epilogue

@gemm_epilogue()
def bias_epi(acc, bias):
    return {"D": acc + bias}
```

3. 推断 `bias` 的种类：当你用形状 `(l, n)` 的张量调用时，`_infer_kind` 会发现它沿 N 广播，于是推断为 `"row"`（详见 4.2.3）。
4. 跟踪 `bias_epi.gemm(...)` 内部：在 [quack/epilogue/frontend.py:914-952](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L914-L952) 处，`kind_sig` 会被拼成 `(("bias", "row"),)`，随后在 [quack/epilogue/frontend.py:1074-1084](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L1074-L1084) 处 `mint_key` 喂给 `self._mint(...)`。
5. （**待本地验证**，需 SM90+ GPU）参照 `tests/test_gemm_epilogue.py::test_epi_mod_scalar_and_c` 的写法，用真实张量调用 `bias_epi.gemm(A, B, D, epi_args=dict(bias=...), tile_M=128, tile_N=192, cluster_M=1, cluster_N=1)`，并和 `torch.einsum("lmk,lnk->lmn", A.float(), B.float()) + bias` 对比数值。

**需要观察的现象**：

- 第一次调用会触发「冷编译」（铸造类 + 编译 cubin，数百毫秒到秒级）。
- 第二次同形状调用应几乎瞬时命中 `self._plan_cache`，只换数据指针启动。

**预期结果**：输出 `D` 与 PyTorch 参考在 bfloat16 容差内一致；冷热两次调用耗时差异显著。

#### 4.1.5 小练习与答案

**练习 1**：如果 `bias_epi` 不返回 `"D"` 键，会发生什么？
**答案**：根据函数契约，省略 `"D"` 表示「保留原始累加器直接写回」——等价于一个纯 GEMM（`library.py` 的 `identity_epi` 就是 `return {"D": acc}` 的对照）。框架在 `epi_visit_subtile` 里用 `const_expr("D" in res)` 判断：有则覆盖 acc，无则不动。

**练习 2**：为什么 `fn` 被挂成 `staticmethod(self.fn)` 而不是直接 `self.fn`？
**答案**：设备侧循环 `visit.py` 以 `self._epi_mod_fn(...)` 形式调用，但它**不应接收 `self`**——函数体只该看到 `(acc, **operands)`。挂成 staticmethod 避免把内核实例当成隐式首参传入。

### 4.2 fn_port 值端口协议：一个属性让任意 op 接入数据流

#### 4.2.1 概念说明

`fn_port` 是 `EpiOp` 基类上的一个类属性，它是「这个 op 如何加入函数逐元素数据流」的**唯一声明**。前端只根据 `fn_port` 这一个字符串分发，**从不 `isinstance` 分发**——这是让任意自定义 op 都能组合的关键。

七种端口（来自基类文档与 `_OPERAND_PORTS`）：

| fn_port | 含义 | 函数里收到什么 | 现有 op 例子 |
|---------|------|----------------|--------------|
| `"row"` | 沿 N 广播的行向量 | 每个元素的标量值 | `RowVecLoad` |
| `"col"` | 沿 M 广播的列向量（varlen 下退化为 `(total_m,)` rank-1） | 每个元素的标量值 | `ColVecLoad` |
| `"tile"` | 整块加载 | 每个元素的标量值 | `TileLoad` |
| `"scalar"` | Python 标量或单元素张量 | 标量值（整个 tile 共享） | `Scalar` |
| `"value"` | 自定义值源 op | 每个元素的值（`fn_prepare` 把 begin_loop 状态变成稠密片段） | `HeadRstd`、`RotaryCosSinLoadHost` |
| `"apply"` | 函数收到一个**可调用对象** | `y = rope(acc)`，op 的数学在用户选定处插入 | （协议已定义；本 HEAD 暂无内置 op 使用） |
| `"sink"` | 函数**返回**该 op 的值 | 不作为入参；前端收集稠密片段后 `fn_sink_flush` | `ColVecReduce`/`RowVecReduce`/`OnlineLSEReduce` |

一个关键区分：`row/col/tile/scalar/value/apply` 是**入参端口**（operand），`sink` 是**返回端口**（output）。`_OPERAND_PORTS` 显式排除了 `"sink"`：

```python
_OPERAND_PORTS = {"apply", "col", "row", "scalar", "tile", "value"}
```

见 [quack/epilogue/frontend.py:1718-1734](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L1718-L1734)，`_OPERAND_PORTS` 定义与 `_pinned_visit_kind` 把任意 op 的 `fn_port` 翻译成函数循环的 visit 种类。

#### 4.2.2 核心流程

`fn_port` 在两个时机被读取：

```text
【主机侧 · plan 期】
  对每个 operand 形参：
    若用户用 ops={name: EpiOp(...)} 钉死 → visit_kind = op.fn_port（经 _pinned_visit_kind 校验）
    否则 → kind = _infer_kind(由张量 shape 推断)  # 得到 "row"/"col"/"tile"/"scalar"
  拼出 visit_sig = ((name, visit_kind), ...)，作为 mint_key 的一部分

【设备侧 · trace 期】（visit.py 的 epi_visit_subtile）
  对每个 (name, kind) in _epi_mod_operands：
    按 kind 分支，准备 fragments[name]：
      scalar → 整 tile 一个标量
      row/col → 已是 acc dtype 的广播片段
      tile/c → 转成 acc dtype 的稠密片段
      value → ops_by_name[name].fn_prepare(...)  把 begin_loop 状态稠密化
      apply → ops_by_name[name].fn_prepare(...)  返回一个可调用状态的句柄
  逐元素循环里把 fragments 按 kind 包成值（标量 / F2 / Pair / 可调用），喂给 fn
```

注意 `value`/`apply` 端口让 op 自己决定如何把它的 `begin_loop` 结果变成函数能消费的东西——这是「一个方法让任意 op 组合」的落点。

#### 4.2.3 源码精读

**基类声明与三个端口方法**：

```python
class EpiOp:
    fn_port = None  # 默认：不可用于函数前端（仅手写 mixin）

    def fn_prepare(self, gemm, state, paired):
        """从这个 op 的 begin_loop 结果派生「每子 tile 端口状态」。
        paired: 函数循环按相邻-N pair 运行（值是 Pair）。"""
        return state  # 默认：begin_loop 状态本身就是片段

    def fn_apply(self, gemm, pstate, i, value):
        raise NotImplementedError  # apply 端口专用

    def fn_sink_flush(self, gemm, state, frag):
        """把函数产出的一片段值折叠进这个 op 的累加器。"""
        raise NotImplementedError
```

文档里那段注释点明：`fn_port` 是「op 如何加入函数逐元素数据流」的**唯一**声明，前端永不 `isinstance` 分发；而下方的资源生命周期（`begin/begin_loop/...`）保持为 smem/TMA/flush 协议，与端口正交。见 [quack/epilogue/ops.py:298-335](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L298-L335)，`EpiOp` 的值端口协议与三个端口方法。

**内置 op 的 fn_port 声明**散布在 ops.py 各处，每个具体 op 只需一行：

- `Scalar.fn_port = "scalar"` — [quack/epilogue/ops.py:521](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L521)
- `RowVecLoad` 隐含 `fn_port = "row"`（其基类在 [quack/epilogue/ops.py:724](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L724)）
- `ColVecLoad` 隐含 `fn_port = "col"`（[quack/epilogue/ops.py:736](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L736)）
- `TileLoad.fn_port = "tile"` — [quack/epilogue/ops.py:1186](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1186)
- `VecReduce.fn_port = "sink"` — [quack/epilogue/ops.py:1454](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1454)
- 统计类 `GroupedColStatsBase`（如 `HeadRstd`）`fn_port = "value"` — [quack/epilogue/ops.py:2154](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2154)

**主机侧种类推断 `_infer_kind`**：当用户没有用 `ops=` 钉死时，由张量元数据反推端口种类：

```python
def _infer_kind(name, value, m, n, varlen_m=False):
    if not hasattr(value, "stride"):  # python 数字
        return "scalar"
    if value.ndim == 0 or value.numel() == 1:
        return "scalar"
    if value.ndim in (2, 3) and tuple(value.shape[-2:]) == (m, n):
        return "tile"
    inner = value.shape[-1]
    if value.ndim <= 2 and inner in (m, n):
        ...
        if m == n:
            raise ValueError(f"operand '{name}': m == n makes row/col inference ambiguous; "
                             f"pin it via @gemm_epilogue(ops={{'{name}': ...}})")
        return "row" if inner == n else "col"
    raise ValueError(...)
```

要点：内层维度等于 `n` → `row`（沿 N 广播）；等于 `m` → `col`；`m == n` 时无法区分，强制用户用 `ops=` 钉死。这正是「`row` 端口如何把一个 `RowVecLoad` 接进来」的主机侧入口。见 [quack/epilogue/frontend.py:217-243](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L217-L243)，由张量 shape 推断 operand 种类。

`_KIND_TO_OP` 把内置种类映射回 op 类，铸模时据此构造默认 op：

```python
_KIND_TO_OP = {"row": RowVecLoad, "col": ColVecLoad, "tile": TileLoad, "scalar": Scalar}
```

见 [quack/epilogue/frontend.py:209-214](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L209-L214)。

**设备侧分发 `epi_visit_subtile`**（visit.py）。这里把 host 推断的 `kind` 落实成「喂给函数的值」。下面是按 kind 准备片段的分支（节选）：

```python
for name, kind in self._epi_mod_operands:
    if const_expr(kind == "apply"):
        # apply 端口：每子 tile 的端口状态；函数拿到一个 callable
        frags[name] = ops_by_name[name].fn_prepare(self, epi_loop_tensors[name], paired)
    elif const_expr(kind == "c"):
        ...  # GEMM 的 C 操作数
    elif const_expr(kind == "tile"):
        frags[name] = epi_loop_tensors[name].to(self.acc_dtype)
    elif const_expr(kind == "value"):
        # 自定义值源：fn_prepare 把 begin_loop 状态变成稠密片段
        frags[name] = ops_by_name[name].fn_prepare(self, epi_loop_tensors[name], paired)
    else:  # "row" / "col" 片段已是 acc dtype；"scalar" 是一个值
        frags[name] = epi_loop_tensors[name]
```

然后在逐元素循环里，把这些片段按 kind 包成值并调用 `fn`。以非打包标量路径为例：

```python
for i in cutlass.range(cute.size(tRS_rD), unroll_full=True):
    kw = {
        name: (
            (lambda v, _n=name, _i=i: ops_by_name[_n].fn_apply(self, frags[_n], _i, v))
            if kind == "apply"
            else (frags[name] if kind == "scalar" else frags[name][i])
        )
        for name, kind in self._epi_mod_operands
    }
    res = fn(tRS_rD[i], **kw)
    if const_expr("D" in res):
        tRS_rD[i] = res["D"]
    ...
```

请体会：`row/col/tile` 端口在循环里就是 `frags[name][i]`（按下标取一个元素），`scalar` 是整 tile 共享的 `frags[name]`，`apply` 是一个闭包（调用时才触发 op 的 `fn_apply`）。这就是「`row` 把 `RowVecLoad` 接入逐元素数据流」的设备侧落点。见 [quack/epilogue/visit.py:140-184](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/visit.py#L140-L184)（片段准备）与 [quack/epilogue/visit.py:363-383](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/visit.py#L363-L383)（标量循环逐元素调用 fn）。

> 关于 `apply` 端口：协议（`_OPERAND_PORTS` 与上面的 `kind == "apply"` 分支）完整支持它，但**截至本 HEAD，仓库里没有任何 op 真正声明 `fn_port = "apply"` 或覆写 `fn_apply`**（基类的 `fn_apply` 仍 `raise NotImplementedError`）。文档提到的「RotaryCosSinLoad 15 行适配器」如今走的是 `"value"` 端口（见 [quack/epilogue/rotary.py:175](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/rotary.py#L175)）。`apply` 是为「op 数学在用户选定处插入」预留的端口。

#### 4.2.4 代码实践

**实践目标**：跟踪 `fn_port='row'` 如何把一个 `RowVecLoad` op 接进函数的逐元素数据流。

**操作步骤**：

1. 在 [quack/epilogue/frontend.py:217-243](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L217-L243) 确认：传入 `(l, n)` 的 `bias` 张量，`inner == n` → 推断 `"row"`。
2. 在 [quack/epilogue/frontend.py:461-474](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L461-L474)（`_mint` 内）确认：`"row"` 经 `_KIND_TO_OP` 造出一个 `RowVecLoad("bias")`，加入 `_epi_ops`。
3. 在 [quack/epilogue/visit.py:183-184](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/visit.py#L183-L184) 确认：`row` 走 `else` 分支，`frags["bias"]` 是 `RowVecLoad.begin_loop` 产出的、已是 acc dtype 的广播片段。
4. 在 [quack/epilogue/visit.py:363-375](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/visit.py#L363-L375) 确认：循环里 `kind != "scalar"` 且非 apply，于是取 `frags[name][i]` 作为 `bias` 的值喂给 `fn`。

**需要观察的现象**：`row` 端口在主机侧只是字符串 `"row"`，在设备侧才落实为「按下标取元素」——`RowVecLoad` 自身的 cp.async 加载、zero-stride 广播（u3-l2/u6-l2 讲过）都隐藏在 `begin_loop` 里，函数体对此无感。

**预期结果**：你能用一句话说清——「`row` 端口 = 主机侧由 shape 推断 / 钉死，设备侧由 `RowVecLoad` 的生命周期负责把行向量变成广播片段，函数只看到逐元素的标量」。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 operand 形参的张量形状是 `(m, n)`（整块），它会被推断成哪个端口？函数里收到的是什么？
**答案**：`"tile"`（见 `_infer_kind` 的 `tuple(value.shape[-2:]) == (m, n)` 分支）。设备侧走 `kind == "tile"`，`frags[name] = epi_loop_tensors[name].to(self.acc_dtype)`，循环里取 `frags[name][i]`——本质是一个 `TileLoad` op 的逐元素值。`library.py` 的 `residual_epi` 就是这样：`def residual_epi(acc, res): return {"D": acc + res}`，`res` 是整块残差。

**练习 2**：`value` 端口和 `row/col/tile` 端口有什么本质区别？
**答案**：`row/col/tile` 的片段是「加载型 op 的 `begin_loop` 结果直接转 dtype」；`value` 端口允许 op **自定义**如何把 `begin_loop` 状态变成稠密片段——通过覆写 `fn_prepare`。典型例子是 `HeadRstd`（u6-l5 会讲）：它的 `begin_loop` 返回的是统计扫描的中间状态，`fn_prepare`（[quack/epilogue/ops.py:2464-2477](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2464-L2477)）把 finalized 的 per-row rstd 广播成与 acc 同形的稠密片段。这让「先扫一遍算统计量、再逐元素乘」这种需要预扫描的 op 也能像普通向量一样在函数里使用。

### 4.3 library 注册与 minted class：缓存的语义身份

#### 4.3.1 概念说明

`library.py` 是「现成 mod 库」——每个条目就是一个用 `@gemm_epilogue` 写好的函数。比如默认线性 epilogue 被重写成一个三行函数：

```python
@gemm_epilogue()
def linear_epi(acc, c, alpha, beta, bias_n, bias_m):
    """The default (linear) epilogue as a mod: alpha*acc + beta*C + rowvec + colvec."""
    return {"D": acc * alpha + c * beta + bias_n + bias_m}
```

`c` 是 GEMM 的 C 操作数（保留字），`alpha/beta` 是标量（推断为 `scalar`），`bias_n` 是 `(l,n)` 行向量（`row`），`bias_m` 是 `(l,m)` 列向量（`col`）。这一个函数等价于手写的 `apply_linear_epilogue`（u6-l3），但写法天差地别。

「铸模」（mint）与注册要解决的核心问题是 **缓存的语义身份**：

- 不同形状的调用要复用同一份 cubin（只要种类签名和模式相同）。
- 同一份函数源码改动（或它引用的辅助函数改动）必须使缓存失效，否则会静默复用错误的内核。
- 跨进程的异步编译 worker 要能重新铸造出同一个类。

#### 4.3.2 核心流程

```text
装饰期：
  semantic_key = (fn指纹, prepass指纹, outputs, mode, prepass_outs,
                  各 op 的 cache_key(), vectorize)
  semantic_digest = sha256(repr(semantic_key))   # 16 位进 _ident

调用期（EpiMod.gemm）：
  mint_key = (visit_sig, SM, paired_acc, packed_c, prepass_sig, rounding, arg_forms, add_to_output)
  cls = self._minted.get(mint_key)  or  self._mint(mint_key)
        └─ type(...) 铸造类 → setattr 到 frontend 模块命名空间（便于检查/复用）
  class_ref = self._class_ref(mint_key)
        └─ 有可导入锚点 → GemmClassRef("epi_mod", module, global_name, ...)
           无锚点（__main__/notebook）→ register_local + cloudpickle 载荷
  plan = build_gemm_epi_plan(cls, ..., gemm_cls_ref=class_ref, ...)
        └─ JIT 缓存收到的是 class_ref（可 pickle），从不接收进程局部类对象
```

关键不变量：**JIT 缓存的磁盘键里包含语义摘要，但绝不包含进程局部的类对象**。编译产物（`.o`）跨进程复用，靠的是 worker 在使用点按 `class_ref` 重新铸造（import 模块全局的 EpiMod，或对无锚点者用 cloudpickle 载荷）。

#### 4.3.3 源码精读

**语义指纹是「失败即关闭」（fail-closed）的**。`_function_semantic_key` 递归指纹函数源码及它引用的全部全局/闭包变量；任何无法指纹的捕获都会在装饰期抛错——因为「太粗的键静默复用错误内核」是不可接受的。`cache_key()` 是 op 实现该协议的入口：

```python
def cache_key(self):
    return (type(self).__module__, type(self).__qualname__, self.name, self.config_key())

def __quack_semantic_key__(self):
    # op 实例被 epilogue 函数捕获时，指纹成它的 cache 身份
    return self.cache_key()
```

`config_key()` 同样 fail-closed：有静态配置却没覆写 `config_key()` 的 op 会直接报错，避免「悄悄漏掉一个实例属性」导致两个语义不同的 epilogue 在持久 JIT 缓存里撞键。见 [quack/epilogue/ops.py:340-365](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L340-L365)，`config_key`/`cache_key` 与 `__quack_semantic_key__`。

**铸造类的缓存与登记**。`_mint` 先查 `self._minted[key]`，命中直接返回；否则铸造、登记到 frontend 模块命名空间、存入缓存：

```python
key = (kind_sig, sm, paired_acc, packed_c, prepass_sig, rounding, arg_forms, add_to_output)
cls = self._minted.get(key)
if cls is not None:
    return cls
...  # 铸造 epi_ops、构造 EpilogueArguments NamedTuple
cls = type(cls_name, (_EpiModMixinBase, _SM_BASE[sm]), {...})
setattr(sys.modules[__name__], cls_name, cls)  # 检查与进程内复用
self._minted[key] = cls
return cls
```

注意类名 `cls_name` 把所有影响编译的维度都编码进去（`paired_acc` → `'g'`、`packed_c` → `'p'`、种类签名、`arg_forms`、SM、rounding、`add_to_output`），并且有「类名碰撞」断言（同名的已有类必须是同一 `class_semantic_key`，否则报错）。见 [quack/epilogue/frontend.py:453-474](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L453-L474)（`_mint` 入口与缓存）和 [quack/epilogue/frontend.py:506-555](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L506-L555)（类名编码、铸造、登记）。

**跨进程身份 `GemmClassRef`**。`_class_ref` 决定 worker 如何在使用点重新铸造：

```python
def _class_ref(self, mint_key):
    locator = self._module_locator()
    if locator is None:
        # 无可导入锚点（__main__/notebook）：磁盘键仍正确（摘要进了 ref），
        # 解析走进程局部注册表，pool 提交按值运送本 EpiMod（cloudpickle）
        register_local_epi_mod(self.semantic_digest, self)
        return GemmClassRef("epi_mod_local", "", "", mint_key=mint_key,
                            semantic_digest=self.semantic_digest)
    return GemmClassRef("epi_mod", *locator, mint_key=mint_key,
                        semantic_digest=self.semantic_digest)
```

`module_locator` 返回 `(module, global_name)`——前提是这个 EpiMod 在一个可被新进程 import 的模块里绑定了全局名。脚本/notebook 里定义的 EpiMod 没有这种锚点，于是走「按值运送 + 进程局部注册表」的旁路，**但这套旁路绝不触碰缓存键**（缓存键里只有摘要）。同一摘要 → 同一磁盘 `.o`，跨进程跨 worker 一致。见 [quack/epilogue/frontend.py:425-451](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L425-L451)，锚点探测与两种 `GemmClassRef`。

**library.py 里的端口覆盖面**。库里的 mod 几乎用遍了所有端口，值得逐个对照：

- 纯 `row/col/scalar`：`linear_epi`（[quack/epilogue/library.py:73-76](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L73-L76)）、`scaled_residual`（`c` + scalar）。
- `sink` 归约：`rms_fused` 用 `reduces={"sqsum": ColVecReduce("sqsum", scaled=True)}`，函数返回 `{"sqsum": (acc, acc)}`——`scaled=True` 让折叠是一次融合 `fma(val, scale, acc)`，与直接折叠乘积逐位一致。见 [quack/epilogue/library.py:110-114](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L110-L114)。
- `sink` 的耦合累加器：`lse_epi` 用 `outs={"lse": OnlineLSEReduce("lse")}`——`combine=...` 表达不了的耦合 (max,sum) 累加器，直接作为一个类被任意 mod 在 `outs=` 里点名。见 [quack/epilogue/library.py:279-283](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L279-L283)。
- `value` 端口 + prepass：`qknorm_epi` 用 `ops={"qk": HeadRstd(...), "w": RowVecLoad("w")}`，配 `prepass=_sq_prepass`——先对原始 acc 做平方和预扫描，再让 `qk` 作为 value 端口把 finalized rstd 逐元素乘上去。见 [quack/epilogue/library.py:350-366](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L350-L366)。
- 配对模式：`swiglu_mod` 用 `mode="acc_pair"`，函数体 `gate, up = unpack(acc)`。见 [quack/epilogue/library.py:196-201](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L196-L201)。

> 关于 `Pair`/`unpack`/`pack`：当 `mode="acc_pair"`（gated，aux 缓冲是 GEMM-N 的一半）时，累加器按相邻 N 列配对，函数收到的 `acc` 是一个 `Pair`；`unpack` 拆成两 lane，`pack`（就是 `Pair` 的别名）把它们装回去。`Pair` 的 `+ - *` 是逐 lane 的、标量会广播，所以 `acc * rstd + bias` 这种仿射可以在 unpack 之前先做。这套值词汇定义在 [quack/epilogue/math.py:16-73](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/math.py#L16-L73)。

**逃生出口**。当函数式契约表达不了某种数据流（比如对称 GEMM 的 scheduler），可以退回手写 mixin——而且手写 mixin 仍是头等公民。`epilogue_from_class(GemmCls) -> StaticEpi` 把一个手写 epilogue GEMM 类包进同样的 `plan()/run()` 接口，只是不做 operand 推断、不分配输出：

```python
def epilogue_from_class(GemmCls) -> StaticEpi:
    """Wrap a hand-written epilogue GEMM class in the plan/run interface."""
    return StaticEpi(GemmCls)
```

见 [quack/epilogue/frontend.py:1854-1916](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L1854-L1916)，`StaticEpi`（「第三档逃生口」）与 `epilogue_from_class`。文档把它和函数式 / 钉单参（`ops=`）/ 加一个端口方法并列，称为「分级而非悬崖」的逃生出口。

#### 4.3.4 代码实践

**实践目标**：对比「函数式 mod」与「手写 mixin」两种写同一个 epilogue 的取舍。

**操作步骤**：

1. 读 [quack/epilogue/library.py:73-76](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/library.py#L73-L76) 的 `linear_epi`（3 行函数）。
2. 回顾 u6-l3 讲过的 `apply_linear_epilogue` + `GemmDefaultEpiMixin`（手写 mixin，要重写 `epi_visit_subtile`、手工编排 `α→β·C→rowvec→colvec` 的顺序、处理缺省项的 `const_expr` 折叠）。
3. 在 [quack/epilogue/frontend.py:1074-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L1074-L1124) 确认：`linear_epi.gemm(...)` 会铸造出一个 `(c, row, row, col, scalar, scalar)` 种类签名的类，其设备侧循环由 `visit.py` 通用驱动——形状与手写 mixin 逐位一致（文档声称 bitwise-or-1-ulp、性能 ≤1% 差异）。

**需要观察的现象 / 取舍**：

| 维度 | 函数式 mod（`@gemm_epilogue`） | 手写 mixin（`GemmDefaultEpiMixin`） |
|------|-------------------------------|-------------------------------------|
| 表达 | 一个普通函数，顺序即源码 | 重写 `epi_visit_subtile`，手写循环 |
| 新 op 接入 | 声明 `fn_port` 即可组合 | 需在 mixin 里手工编排 |
| 控制力 | 受契约约束（逐元素、标准循环形状） | 完全自由（可改 scheduler、数据流） |
| 适用 | 绝大多数逐元素融合 epilogue | 契约表达不了的（如 symmetric scheduler） |

**预期结论**：能用函数式表达的优先用函数式（低边际成本、可 review）；函数式表达不了的（文档原话「symmetric's scheduler」）才退回 mixin——二者共享同一套 `EpiOp` 机制，不是两套框架。

#### 4.3.5 小练习与答案

**练习 1**：为什么 JIT 缓存接收的是 `GemmClassRef`（可 pickle 的配方），而不是铸造出来的类对象本身？
**答案**：铸造类是进程局部的动态类（`type(...)` 产物），跨进程无法 pickle 还原成「同一个类」。但它的**身份**可以序列化：`GemmClassRef` 携带 `(module, global_name, mint_key, semantic_digest)`，worker 在使用点据此重新铸造出同一个类（import 模块全局的 EpiMod 再 `_mint(mint_key)`）。这样磁盘 `.o` 键只依赖语义摘要，跨进程稳定。

**练习 2**：如果我把 `linear_epi` 里 `c * beta` 改成 `c * beta * 2`，缓存会失效吗？
**答案**：会。`semantic_key` 深度指纹函数源码（`_function_semantic_key(fn)`），源码一改，摘要变，`_ident` 变，铸造类名变，磁盘 `.o` 键变——自动重新编译。这也是「失败即关闭」指纹的意义：源码改即失效，不会静默复用旧内核。

## 5. 综合实践

把本讲三块内容串起来：**亲手用函数式前端实现一个「带行偏置 + 列缩放」的融合 epilogue，并解释它从装饰到启动的完整身份链。**

任务：实现 \(D_{mn} = (\text{acc}_{mn} + b_n) \cdot s_m\)，其中 `b` 是 `(l, n)` 行偏置，`s` 是 `(l, m)` 列缩放。

建议步骤：

1. 写函数（示例代码）：

```python
# 示例代码
from quack.epilogue import gemm_epilogue

@gemm_epilogue()
def bias_scale_epi(acc, bias, scale):
    return {"D": (acc + bias) * scale}
```

2. **预测端口**：`bias` 形状 `(l, n)` → 推断 `"row"`（`RowVecLoad`）；`scale` 形状 `(l, m)` → 推断 `"col"`（`ColVecLoad`）。
3. **跟踪铸模**：在 [quack/epilogue/frontend.py:914-952](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L914-L952) 确认 `kind_sig = (("bias","row"), ("scale","col"))`；在 [quack/epilogue/frontend.py:453-474](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L453-L474) 确认铸造出的类继承 `(_EpiModMixinBase, _SM_BASE[sm])`，`_epi_mod_operands` 就是这个签名。
4. **跟踪设备侧**：在 [quack/epilogue/visit.py:183-184](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/visit.py#L183-L184) 确认 `row`/`col` 都走 `else` 分支拿到广播片段；在循环里 `(acc[i] + bias[i]) * scale[i]` 被逐元素求值。
5. **验证身份链**：用 `print(bias_scale_epi._ident)` 查看 `函数名_语义摘要前16位`；调用两次同形状，确认第二次命中 `self._plan_cache`（可在 [quack/epilogue/frontend.py:757-775](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L757-L775) 的热路径处观察）。
6. （**待本地验证**，需 SM90+ GPU）参照 `tests/test_gemm_epilogue.py::test_epi_mod_linear`，与 `torch.einsum("lmk,lnk->lmn", A.float(), B.float())` 加偏置再乘缩放的参考对比数值。

**验收标准**：你能用自己的话讲清——装饰器把函数包成 `EpiMod`、调用时按张量元数据推断端口种类、按 `(种类签名, SM, ...)` 铸造出一个标准 mixin 子类、设备侧循环由 `visit.py` 按 `fn_port` 分发逐元素求值、缓存键由语义指纹保证改源码即失效。

## 6. 本讲小结

- `@gemm_epilogue` 是 FlexAttention 式的 epilogue 创作前端：把一个对累加器逐元素求值的普通函数，降低到已有的 `EpiOp` 机制上。**函数是组合点，EpiOp 是扩展点**。
- 函数契约是 `fn(acc, **operands) -> {"D": ..., <outputs/sinks>...}`；`acc` 必须首参，`"D"` 可选（缺省=保留原始累加器）。
- `fn_port` 是 op 加入函数数据流的**唯一**声明（`row/col/tile/scalar/value/apply/sink`），前端只按这一个属性分发，**绝不 `isinstance` 分发**——这是「一个方法让任意 op 组合」的关键。其中 `apply` 端口协议已就绪但本 HEAD 暂无内置 op 使用。
- 设备侧循环全在 `visit.py` 的 `_EpiModMixinBase.epi_visit_subtile`，按 `fn_port` 把每个 op 的片段包成值（标量 / `F2` / `Pair` / 可调用）喂给函数；形状与手写 mixin 逐位对齐。
- 铸模 `_mint` 按 `(种类签名, SM, 模式, ...)` 动态 `type(...)` 出一个继承 `_EpiModMixinBase` + SM 基类的标准 mixin 子类，缓存于 `self._minted`。
- 缓存身份是**失败即关闭**的语义指纹：函数源码 + 各 op `cache_key()` → 摘要；JIT 缓存收 `GemmClassRef`（可 pickle 配方）而非类对象，跨进程靠 worker 重新铸造，同摘要 → 同 `.o`。
- 逃生出口是分级的：钉单参（`ops=`）→ 加一个端口方法 → 退回手写 mixin（`epilogue_from_class`），每一步都保持其余部分组合。

## 7. 下一步学习建议

- **u6-l5 领域 epilogue**：本讲的 `value` 端口 + prepass（`HeadRstd`）、`sink` 耦合累加器（`OnlineLSEReduce`）、量化输出（`BlockScaleFactorStore`）都会在那里深入；建议重点读 `rotary.py`、`scaled_exp.py`、`head_rmsnorm.py`、`quantize_out.py` 四个领域模块如何各自实现 `fn_port`/`fn_sink_flush`。
- **u8-1 自动调优**：`EpiMod.gemm_tuned`（[quack/epilogue/frontend.py:557-563](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/frontend.py#L557-L563)）委托 `quack.gemm_runtime.autotune.tuned_mod_gemm` 做 config 空间扫描；学完本讲可去 u8-1 看函数式 mod 如何参与自动调优。
- **延伸阅读**：读 `tests/test_gemm_epilogue.py` 的 `test_epi_mod_semantic_cache_key_and_resolver` 与 `test_epi_mod_async_compile`，验证你对语义指纹与异步编译 worker 重新铸造的理解。
