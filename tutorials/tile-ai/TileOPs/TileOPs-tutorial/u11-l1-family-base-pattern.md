# 家族基类模式（T1/T2 与三层继承）

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 `Op(L1) → FamilyBase(L2) → ConcreteOp(L3)` 的三层继承结构，并说出每一层各自的职责边界。
- 区分 **T2（L1-direct，scaffold 产物）** 与 **T1（薄包装，重构产物）** 两种类形态，知道它们各自的 `forward()` 从哪来。
- 说清「何时该抽取一个 L2 家族基类」的判定条件，以及什么情况下**不该**抽。
- 解释为什么 `eval_roofline` / `_validate_dtypes` 这两个 codegen 契约**必须落在具体类（L3）的 `__dict__` 里**，而不是放在 L2 基类上被继承。
- 看懂「维度参数化家族」（如 pool 的 1d/2d/3d）如何用一个 `ndim` 泛化基类 + 表驱动命名来表达空间秩差异，并保留真正的 per-rank 差异为子类 override。

## 2. 前置知识

本讲是专家层（advanced），承接以下已建立的心智模型，不再重复：

- **u1-l1 / u1-l4**：TileOPs 的 Op(L2 主机侧) / Kernel(L1 设备侧) 双层分离；Op 是可调用对象，套路是「先实例化再调用」。
- **u2-l1**：`Op` 基类的生命周期——构造期 `dispatch_kernel` 安装 `kernel_map`、调用期在 `forward` 选并 JIT 编译 kernel；`_static_axes` 记录构造期已提交的轴；三个 codegen 契约方法 `_infer_output_shapes` / `_validate_dtypes` / `eval_roofline` 由 `__init_subclass__` 在**类定义**瞬间自动装配。
- **u8-l1**：`Op.__init_subclass__` 钩子如何用懒导入打破循环依赖，以及 `status: spec-only` 是 codegen 的总开关。

几个本讲会反复用到的术语，先对齐：

- **层（layer）与形态（shape）是两个正交的概念**。「L1/L2/L3」描述的是**继承链上的位置**；「T1/T2」描述的是**当前类是否已经经过家族重构**。两者命名容易混，本讲会严格区分。
- **codegen 契约**：指那三个由 manifest 元数据驱动、在类定义时自动合成方法体的方法（详见 u8-l1 / u8-l2 / u8-l3）。它们是 spec-driven 的落地点。
- **protocol 变量（family-base protocol）**：L2 基类声明、L3 子类用类属性覆写的小变量，如 `_kernel_key`、`kernel_cls`、`_op_name`、`ndim`。它们是「把 per-op 差异收进类属性」的载体。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`docs/design/ops-design.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md) | Op 接口设计主文档：类层次图、scaffold 七步、Family-Base Refactoring 一节给出 T2→L2 的迁移边界。 |
| [`docs/design/ops-design-reference.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md) | 权威细则：Family-Base Protocol 表、Codegen 继承规则、Development Path（何时抽 L2）、Adding a New Family Base（怎么抽）。 |
| [`tileops/ops/op_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py) | L1 `Op` 基类：共享管线 + 三个 codegen 契约的 stub + `__init_subclass__` 钩子。 |
| [`tileops/ops/_dtype_codegen.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py) | `_validate_dtypes` 的合成器与 `maybe_install_validator` 安装器（只看 `cls.__dict__`）。 |
| [`tileops/ops/_roofline_codegen.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py) | `eval_roofline` 的合成器与 `maybe_install_eval_roofline` 安装器（遍历 `cls.__mro__`）。 |
| [`tileops/ops/pool.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py) | 维度参数化家族范本：`_AvgPoolFwdOpBase` / `_MaxPoolFwdOpBase` 两个 ndim 泛化基类 + 1d/2d/3d 薄包装。 |
| [`tileops/ops/elementwise/_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py) | elementwise 三大伞形基类（`UnaryOp` / `BinaryOp` / `FusedGatedOp`）：`kernel_cls` + `_op_name` 模板化的另一类 L2。 |
| [`tests/ops/test_pool.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_pool.py) | `test_pool_codegen_slots_are_class_local`：强制 codegen 契约落在具体类 `__dict__`。 |

---

## 4. 核心概念与源码讲解

### 4.1 三层继承：L1 / L2 / L3

#### 4.1.1 概念说明

TileOPs 的所有 Op 类组织成一条三层继承链。设计文档用一张图概括了它：

```
Op                          ← L1: thin base, shared by all ops
  └── FamilyBase            ← L2: family-specific forward() flow (optional)
        └── ConcreteOp      ← L3: leaf class emitted by the scaffold
```

- **L1（`Op`）**：所有算子共享的「主机侧管线」——kernel 分发与校验（`dispatch_kernel` / `_install_kernel_map`）、kernel 缓存（`_cache_key` / `_static_axes`）、autotune 入口，以及三个 codegen 契约方法的 **stub**（占位实现）。L1 不含任何算子家族的专属逻辑。
- **L2（`FamilyBase`）**：**可选**的一层。一个家族一个，承载该家族共享的 `forward()` 控制流，以及一组「protocol 变量」（如 pool 的 `ndim`、elementwise 的 `kernel_cls` + `_op_name`、reduction 的 `_kernel_key` + `_op_kind`）。**scaffold 不产出 L2**——它是后续重构的产物。
- **L3（`ConcreteOp`）**：叶子类，scaffold 的直接目标。它给出 `default_kernel_map`、自己的类属性，以及（关键）让 codegen 在自己身上合成 `_validate_dtypes` / `eval_roofline`。

注意「L2」在文档里有两个含义容易混：一是这里说的「家族基类这一层」；二是 u1-l1 里说的「Op 是 L2 主机侧入口」。本讲只要看到 `FamilyBase` 就是指**继承链上的第二层**，看到「Op/Kernel 双层」就是指主机侧/设备侧。两者不是一回事。

这条三层链的**核心张力**在于：L2 想共享代码，而 codegen 却按**具体类（L3）的名字**去读 manifest、合成方法。下一节会看到，这个张力直接决定了「哪些东西能上提到 L2、哪些必须留在 L3」。

#### 4.1.2 核心流程

类定义到第一次调用的全过程：

```text
import 模块
   │
   ▼
Python 解释器创建 ConcreteOp 类对象
   │
   ▼
Op.__init_subclass__(cls) 触发               ← 类定义瞬间，非实例化
   │  （懒导入两个 codegen 模块，打破循环依赖）
   ├── maybe_install_validator(cls)          ← 合成 _validate_dtypes（仅看 cls.__dict__）
   └── maybe_install_eval_roofline(cls)      ← 合成 eval_roofline（遍历 cls.__mro__）
   │
   ▼  （合成成功就把 fn 绑到 cls 上；失败被吞，留 L1 stub，由 validator 在 CI 兜底）
ConcreteOp(...)  实例化
   │
   ▼
dispatch_kernel → _install_kernel_map        ← 构造期：合并默认/覆盖 kernel_map + 架构校验
   │
   ▼
op(*inputs) → __call__ → forward(...)        ← 调用期：选 kernel、JIT 编译、缓存
```

`__init_subclass__` 的关键在于它**在类定义时（import 即触发）**就跑，不是实例化时。这意味着 codegen 装配发生在「类对象刚被创建」的那一刻，且装配与否取决于该具体类的 manifest 条目（按 `cls.__name__` 查 YAML）和 `status` 字段。

#### 4.1.3 源码精读

**L1 基类与三层职责划分。** 设计文档明确写出三层各自承担什么——L1 管共享管线与 codegen 契约，L2 管家族共享 `forward()`，L3 是 scaffold 的叶子目标：

[docs/design/ops-design.md:9-19](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L9-L19) —— 类层次图，标注 L1/L2/L3 三层职责。

**L1 的 codegen 契约是 stub。** `Op` 基类不为这三个方法提供真实体，只声明契约并抛 `NotImplementedError`，等待 `__init_subclass__` 在子类上合成：

[tileops/ops/op_base.py:127-155](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L127-L155) —— `eval_roofline` stub，注释点明「L1 base only declares the contract; concrete ops supply the body」，并引用 roofline.md §4.4.6 的 Evaluator Surface Boundary（禁止在 L1 放通用求值器）。`_validate_dtypes`（行 104-125）与 `_infer_output_shapes`（行 79-102）同理。

**`__init_subclass__` 钩子触发自动装配。** 这是连接「继承」与「codegen」的桥梁：

[tileops/ops/op_base.py:57-72](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L57-L72) —— 钩子体只有两行实质调用，但通过懒导入（`from tileops.ops._dtype_codegen import maybe_install_validator`）打破了 `op_base` 与两个 codegen 模块之间的循环依赖。每个 codegen pass 在「子类未声明 manifest 元数据 / 已自带 override / 标了 spec-only」时都是 no-op。

**关键不对称：dtype 侧只看 `__dict__`，roofline 侧遍历 MRO。** 这是本讲最隐蔽、也最常踩坑的点。两个安装器对「基类是否已经定义了该方法」的判定方式不同：

[tileops/ops/_dtype_codegen.py:336-346](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L336-L346) —— `maybe_install_validator` 的判定：`if "_validate_dtypes" in cls.__dict__: return`。**只看具体类自己的 `__dict__`**。文档字符串直言：「a manual `_validate_dtypes` on an intermediate family base is shadowed by the synthesized one, so bind it in the concrete class body」。也就是说，如果你把 `_validate_dtypes` 写在 L2 基类上，codegen 仍会按具体类的 manifest signature 合成一个新的、绑到具体类上，**把基类的版本盖掉**——基类那份成了死代码。

[tileops/ops/_roofline_codegen.py:686-692](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L686-L692) —— `maybe_install_eval_roofline` 的判定：`for base in cls.__mro__:`，只要 MRO 里**任何一个**非 `Op` 的类在 `__dict__` 里有 `eval_roofline`，就 `return`（保留它）。也就是说，如果你把 `eval_roofline` 写在 L2 基类上，它会**被沿用**——但代价是**静默跳过了为该具体类按 manifest 生成 roofline 的流程**。

把两者合起来看，结论是：**无论你把 codegen 契约放在 L2 还是「靠继承拿到」，manifest 驱动的 per-op 生成都不会按你预期发生**——dtype 侧会被覆盖、roofline 侧会被绕过。设计文档把这个事实写进了 pool 基类的 docstring：

[tileops/ops/pool.py:106-112](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L106-L112) —— `_AvgPoolFwdOpBase` docstring：「Concrete subclasses set `ndim`, supply `default_kernel_map`, and keep `eval_roofline` / `_validate_dtypes` in their own class body so manifest codegen resolves them per concrete class.」（`_MaxPoolFwdOpBase` 在 [行 445-452](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L445-L452) 写了同样的话。）

**测试把这条规则钉死。** `test_pool_codegen_slots_are_class_local` 对所有 9 个 pool 具体类断言两个 slot 都在 `__dict__` 里：

[tests/ops/test_pool.py:1584-1593](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_pool.py#L1584-L1593) —— `assert "eval_roofline" in op_cls.__dict__` 与 `assert "_validate_dtypes" in op_cls.__dict__`。docstring 一语道破：「a definition inherited only from an intermediate base either gets silently shadowed by generated code or silently bypasses per-op generation.」

**参考表的官方说法。** 设计参考文档用一张表归纳了家族基类层级里 codegen 方法的归属：

[docs/design/ops-design-reference.md:363-369](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L363-L369) —— 三行规则：家族共享逻辑放 L2（子类继承不覆写）；某个成员有变体逻辑（如多输出）放 L3 覆写；T2（L1-direct）由 scaffold 直接产出方法体。

> **小结**：三层继承本身不难，难的是「codegen 按 L3 的名字干活」。所以 L2 能共享的是 `forward()` 控制流与 protocol 变量；**不能**靠继承传递的是那两个 codegen 契约——它们必须显式落在 L3 的 `__dict__` 里（可以体面地委托给一个共享 helper 函数，但方法绑定本身必须在具体类上）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「dtype 侧被覆盖、roofline 侧被绕过」这条不对称，从而理解为什么 codegen 契约必须落在具体类。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `_dtype_codegen.py` 的 `maybe_install_validator`，定位 `if "_validate_dtypes" in cls.__dict__: return`。确认它**不**遍历 MRO。
2. 打开 `_roofline_codegen.py` 的 `maybe_install_eval_roofline`，定位 `for base in cls.__mro__:` 循环。确认它**会**遍历 MRO，且遇到任意非 `Op` 基类的 override 就 `return`。
3. 打开 `pool.py`，挑一个具体类（如 `AvgPool1dFwdOp`，[行 304-352](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L304-L352)），确认它**自己**绑定了 `_validate_dtypes = _validate_pool_input_dtypes` 并定义了 `eval_roofline`，而不是依赖基类。
4. 看 `MaxPool1dFwdOp.eval_roofline`（[行 648-649](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L648-L649)）如何 `return _max_pool_roofline(self, indices=False)`——这就是「方法绑定 class-local，逻辑委托共享 helper」的合法写法。

**需要观察的现象**：

- dtype 安装器的早退条件只引用 `cls.__dict__`，没有任何 MRO 遍历。
- roofline 安装器的早退条件在一个 `for base in cls.__mro__` 循环里。

**预期结果**：你能用一句话向别人解释——「把 `_validate_dtypes` 放基类会被 codegen 合成的版本盖掉；把 `eval_roofline` 放基类会让 codegen 跳过为该具体类生成。两种情况都偏离了 spec-driven，所以测试要求两者都 class-local。」

> 待本地验证：若你想跑动态验证，可在解释器里构造一个临时 `class _Fake(Op)` 带 `__manifest_signature__`/`__manifest_status__`，分别在「基类有/无 `_validate_dtypes`」两种情况下检查 `"_validate_dtypes" in _Fake.__dict__`，观察合成是否发生。

#### 4.1.5 小练习与答案

**练习 1**：假如某天 `maybe_install_validator` 也改成遍历 MRO（像 roofline 侧那样），`test_pool_codegen_slots_are_class_local` 还需要存在吗？

**参考答案**：仍然建议保留，但性质会变。若两边都遍历 MRO，基类的 `_validate_dtypes` 就不会被覆盖、而是被沿用——表面上「放基类」也能工作。但 spec-driven 的意图是「每个具体类的 manifest signature 驱动自己的校验」；放基类等于让一个手写版本替代了 per-op 生成的版本，manifest 与代码会产生两份真相。测试的作用会从「防 codegen 覆盖」变成「防手写漂移」，依然有价值。

**练习 2**：为什么 pool 选择了「每个具体类各自写 `eval_roofline`，但都委托给同一个 `_max_pool_roofline` helper」？直接把 `_max_pool_roofline` 的逻辑写进基类的 `eval_roofline` 有什么不好？

**参考答案**：直接写进基类 `eval_roofline` 会触发 roofline 侧的 MRO 沿用——codegen 跳过为该具体类生成，等于用手写逻辑替代了 manifest 驱动的 per-op roofline。而「具体类 `eval_roofline` + 共享 helper」既满足 class-local（方法绑定在 L3，codegen 不介入），又复用了逻辑（helper 被多个具体类调用）。这是 spec 推荐的折中：**绑定 class-local，逻辑可共享**。

---

### 4.2 T1 / T2 两种形态

#### 4.2.1 概念说明

「T1 / T2」描述的是**一个具体类当前处于重构的哪个阶段**，与 L1/L2/L3（继承位置）正交：

- **T2（L1-direct）**：具体类**直接继承 `Op`**，自己 owns 完整的 `forward()`。这是 scaffold 产出的初始形态，也是每个家族冷启动时的样子。文档原话：「New ops start by inheriting L1 directly (T2 shape)」。
- **T1（thin wrapper）**：具体类**继承一个 L2 家族基类**，自己的 `forward()` 来自基类；具体类只负责设置几个类属性（如 `ndim`、`kernel_cls`、`_op_name`）。这是家族成熟、抽取 L2 之后的形态。

一句话记忆：**T2 是「胖叶子」（直接挂 L1，自带 forward），T1 是「瘦叶子」（挂 L2，只填类属性）**。

需要警惕的是：T1 和 T2 **不是两种并行设计**，而是同一条演进路径上的前后两站。设计文档明确：「L1-direct ops are candidates for future L2 extraction, not an alternative design.」T2 是过渡态，不是终点。

#### 4.2.2 核心流程

```text
家族冷启动                            家族成熟（2-3 个 op 共享同一 forward 流程）
   │                                            │
   ▼                                            ▼
scaffold-op 产出 T2（L1-direct）           家族重构：抽取 L2 FamilyBase
   • 自带 forward()                            • 共享 forward() 上提到 L2
   • 自带 default_kernel_map                   • per-op 差异 → 类属性 / hooks
   • 自带 _validate_dtypes / eval_roofline     • 已有 T2 op 改写成 T1（瘦包装）
   │                                            │
   ▼                                            ▼
单跑可用                              多个 T1 共享一份 forward，改一处惠及全部
```

两种形态在运行时的调用链**对用户完全透明**——都是 `op(*inputs) → __call__ → forward`。区别只在 `forward` 的定义点是 L3（T2）还是 L2（T1）。

#### 4.2.3 源码精读

**T2 是 scaffold 的唯一产物。** scaffold 七步只产出 T2，T1 不在它的职责内：

[docs/design/ops-design.md:32-34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L32-L34) —— 「The scaffold emits a T2 (L1-direct) op file from one manifest entry.」示例算子 `ExampleCumsumFwdOp` 就是一个 T2：它直接 `class ExampleCumsumFwdOp(Op)`，自带完整的 `forward()`（[行 143-176](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L143-L176)）与 `eval_roofline`（[行 214-217](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L214-L217)）。

**T1 改写是单独的重构步骤。** Out of Scope 一节把「Family-base (T1) subclassing」明确排除出 scaffold：

[docs/design/ops-design.md:256-264](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L256-L264) —— 列出 scaffold 不产出的项，含「Family-specific protocol variables」「Optional hooks」「Family-base (T1) subclassing」。

**T1 范本一：elementwise 伞形基类。** `UnaryOp` 是一个 L2 基类，用 `kernel_cls` + `_op_name` 两个类属性模板化了所有一元 elementwise 算子：

[tileops/ops/elementwise/_base.py:566-587](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L566-L587) —— `class UnaryOp(Op)`，子类只需设 `kernel_cls` 与 `_op_name`。它的 `default_kernel_map` 直接由这两个属性拼出：

[tileops/ops/elementwise/_base.py:625-627](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L625-L627) —— `return {self._op_name: self.kernel_cls}`。这就是 T1 的精髓：**子类不写 forward，只填两张表**。

注意 `UnaryOp` 在 L2 上定义了 `eval_roofline`（[行 634-647](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L634-L647)），靠 roofline 侧的 MRO 沿用生效——这是 elementwise 家族的**遗留手写路径**（它也仍用 int-key 注册表，见 u10-l2），尚未迁移到 per-op codegen。把它和 pool 的 class-local 做法对比，就能看到同一种「L2 共享」在不同家族有两种合法但取向不同的实现：elementwise 走手写共享、pool 走 codegen-per-concrete。

**T1 范本二：pool 的瘦叶子。** `AvgPool1dFwdOp` 继承 `_AvgPoolFwdOpBase`，几乎全是类属性：

[tileops/ops/pool.py:304-352](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L304-L352) —— 它只设了 `ndim = 1`、绑 `_validate_dtypes`、给 `default_kernel_map`、写自己的 `eval_roofline`、覆写 `_param_tuples`（因为 1d 把归一化后的 1-tuple 解包成标量以对齐 `torch.nn.functional.avg_pool1d`）。`forward()` 一行都没有——它来自基类的 `_eager_forward`（经编译边界 custom_op 分发，见 u10-l1）。

> **小结**：T2 = 胖叶子（自带 forward，scaffold 产物）；T1 = 瘦叶子（继承 L2，只填类属性）。elementwise 与 pool 是两种 T1 风格的代表——前者把 roofline 也留在了 L2（手写共享），后者把 codegen 契约强制 class-local（spec 推荐路径）。

#### 4.2.4 代码实践

**实践目标**：在同一份代码里同时识别出 T2 与 T1，体会「形态」是独立于「继承位置」的属性。

**操作步骤**（源码阅读型）：

1. 在 `tileops/ops/` 下找一个直接 `class XxxOp(Op):` 的算子（如 `pool.py` 里的 `MeanPoolingForwardOp`，[行 44-78](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L44-L78)），确认它自带 `forward()`——这是一个 T2。
2. 再看同文件里的 `MaxPool2dFwdOp`（[行 694-729](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L694-L729)），它继承 `_MaxPoolFwdOpBase`，确认它**没有**自己的 `forward()`——这是一个 T1。
3. 对照两者：它们都是 L3（叶子类），但一个 T2、一个 T1。说明「层」与「形态」正交。

**需要观察的现象**：T2 类体内有 `def forward(...)`；T1 类体内没有，`forward` 来自其基类。

**预期结果**：你能指着任意一个 Op 类，分别回答两个问题——「它在继承链的第几层？」「它是 T1 还是 T2？」且两个答案互不决定。

#### 4.2.5 小练习与答案

**练习 1**：一个家族只有 1 个 op 时，应该用 T1 还是 T2？为什么？

**参考答案**：T2。T1 的前提是「存在一个 L2 基类共享 forward」，而抽 L2 至少要 2-3 个 op 共享同一控制流才有意义（见 4.3）。只有 1 个 op 时强行抽 L2 等于把一份 forward 搬到基类、再让唯一子类继承，徒增一层而无收益，参考文档明确把「only 1 op uses the pattern」列为不该抽 L2 的情形。

**练习 2**：`UnaryOp` 把 `eval_roofline` 写在 L2 上，而 `MaxPool2dFwdOp` 把它写在 L3 上。两者都能跑通，哪种更符合 spec-driven？

**参考答案**：pool 的 class-local 写法更符合。spec-driven 要求「每个具体类的 manifest 条目驱动自己的 roofline」；pool 的写法让 codegen 在每个具体类上各自解析（即便逻辑委托给 `_max_pool_roofline` helper）。`UnaryOp` 的写法靠 roofline 侧 MRO 沿用，让一份手写逻辑替代了 per-op 生成——能跑，但属于待迁移的遗留路径。

---

### 4.3 何时抽取 L2（含维度参数化家族）

#### 4.3.1 概念说明

「何时抽 L2」是本讲最实际的决策点。抽早了是无用抽象，抽晚了是重复代码。设计文档给出了一条朴素的判定线：

> 当一个家族积累了 **2-3 个共享完全相同 `forward()` 控制流**的 op，且共享样板代码相当可观，且 per-op 差异能塞进类变量或 hooks 时，才抽 L2。

反过来，三种情况**不该**抽：

1. **只有 1 个 op** 用这个模式——没有共享对象。
2. op **数学相同但控制流不同**——共享的是数学，不是流程，强行抽会逼出大量 `if/else`。
3. 一个公共基类**需要过多 `if/else`** 才能覆盖差异——说明差异不是「类属性级」的，抽象错了维度。

第三条尤其重要，也是本轮（本次更新）spec 文档特意强调的一句话：「Family bases MUST NOT normalize genuine per-op behavior differences.」——**L2 的职责是共享，不是抹平**。真正的 per-op 行为差异（参数有无、快路径策略）必须保留为子类的显式 override，重构不得把它们归一化掉。

**维度参数化家族**是 L2 的一个重要特例：当一组 op 仅在「空间秩」上不同（同一个操作的 1d/2d/3d 变体），用一个**以 `ndim` 为类属性参数的单一泛化基类**表达，而不是写三个独立基类。变体超出秩的部分（如是否输出 indices）也是额外的类属性（`_returns_indices`），而不是子类方法体。

#### 4.3.2 核心流程

抽取 L2 的标准动作（来自 reference 文档「Adding a new family base」）：

```text
1. 先实现 2-3 个具体 T2 op，理解模式          ← 不要先抽象再实现
        │
        ▼
2. 识别共享的 forward() 步骤
        │
        ▼
3. 把共享步骤上提到 L2 基类
   • per-op 差异 → 类变量（ndim / kernel_cls / _op_name …）
   • 或 → 可覆写 hooks（_pad_value / _validate_dim / _pre_kernel / _post_kernel）
        │
        ▼
4. 迁移已有 op 为 T1 薄包装；验证测试不变
        │
        ▼
5. 若引入了新的 protocol 变量，登记进 Family-Base Protocol 表
```

维度参数化家族在此基础上多两条硬约束：

- **保留每个变体的 kernel-cache key 内容与 kernel 构造关键字名**——重构不能改变缓存命中行为，否则性能回归。
- **秩相关命名表驱动，永不位置参数**——用 `_POOL_DIM_NAMES[ndim]` 之类的表查名字，而不是按位置传参。

#### 4.3.3 源码精读

**判定条件的权威表述。** Development Path 与 Adding a New Family Base 两节给出了抽 L2 的完整前提：

[docs/design/ops-design-reference.md:388-396](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L388-L396) —— 三步务实序列：T2 冷启动 → 家族积累 → 抽 L2；并在末句列出「Create an L2 when …」与「Do NOT create one when …」的对照。

[docs/design/ops-design-reference.md:398-404](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L398-L404) —— 五步抽取流程。

**主文档的浓缩表述。** 本轮 spec 把原先散在「Dimension-parametrized families」小节里的几条细则（codegen 契约 class-local、kernel-cache key 保留、表驱动命名、per-rank 差异保留为 override）浓缩成一句总纲：

[docs/design/ops-design.md:311-313](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md#L311-L313) —— 「once a family accumulates 2-3 ops sharing an identical `forward()` flow, a separate family-specific refactoring extracts an L2 base … Family bases MUST NOT normalize genuine per-op behavior differences.」被浓缩的细则现在由代码（pool.py 的基类 docstring）、测试（`test_pool_codegen_slots_are_class_local`）与 reference 文档分头承载。

**维度参数化家族范本：pool 的 ndim 泛化基类。** `_AvgPoolFwdOpBase` 用单一 `ndim` 类属性泛化 1d/2d/3d：

[tileops/ops/pool.py:106-164](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L106-L164) —— `ndim: ClassVar[int]`，`__init__` 用 `nd = self.ndim` 驱动整个构造流程。子类只设 `ndim`。

**表驱动命名。** 秩相关的名字（`l` / `h,w` / `d,h,w`）来自字典查找，不写死、不按位置：

[tileops/ops/pool.py:85-95](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L85-L95) —— `_POOL_LAYOUTS` / `_POOL_DIM_NAMES` / `_AVG_POOL_PARAM_SUFFIXES` / `_MAX_POOL_PARAM_SUFFIXES` 四张表。注释还点出一个历史细节：1d max-pool 的池化轴历史上叫 `w`，所以 `_MAX_POOL_PARAM_SUFFIXES[1]` 是 `("w",)` 而非 `("l",)`——这正是「保留既有 kernel 构造关键字名」约束的体现。

**per-rank 差异保留为显式 override（不被归一化）。** 基类的 `_use_spatial_fast_path` 给出严格策略，而 2d 因为历史策略更宽松，在子类显式覆写：

[tileops/ops/pool.py:391-399](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L391-L399) —— `AvgPool2dFwdOp._use_spatial_fast_path` 覆写，注释写明「Laxer historical 2d policy … asymmetric with 1d/3d」。这就是「Family bases MUST NOT normalize genuine per-op behavior differences」的活样本：2d 的不同策略被如实保留为 override，而不是在基类里用 `if ndim == 2` 抹平。

**protocol 变量与 hooks 的登记表。** reference 文档用一张表汇总了各家族的 protocol 变量，抽 L2 时若新增变量要回填这张表：

[docs/design/ops-design-reference.md:268-281](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L268-L281) —— `_kernel_key` / `_kernel_cls` / `_op_kind`（reduction）、`_op_name` / `kernel_cls`（elementwise）等。同节还有 Optional Hooks 表（`_pad_value` / `_validate_dim` / `_pre_kernel` / `_post_kernel`）。

> **小结**：抽 L2 的前提是「2-3 个 op 共享同一 forward 流程 + 样板可观 + 差异可入类属性/hooks」；维度参数化家族用 `ndim` 泛化基类 + 表驱动命名，且必须保留 kernel-cache key 与构造关键字名、把真正 per-rank 差异留作 override。本轮 spec 把这些细则浓缩为一句总纲，细则下沉到代码与测试。

#### 4.3.4 代码实践

**实践目标**：把「抽取 L2 的前提条件」从抽象规则落到具体代码，理解为什么有些差异能上提、有些必须留下。

**操作步骤**（源码阅读型）：

1. 读 [ops-design-reference.md:388-396](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L388-L396)，抄出「Create an L2 when …」的三条与「Do NOT create one when …」的三条。
2. 在 `pool.py` 里找证据，逐条对应：
   - 「shared forward flow」→ `_AvgPoolFwdOpBase.forward`（[行 280-281](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L280-L281)）三个 avg 变体共用。
   - 「per-op 差异 → 类变量」→ `ndim`（[行 307](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L307)）。
   - 「per-op 差异 → hooks」→ `_use_spatial_fast_path`（[行 179-189](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L179-L189)）被 2d 覆写。
3. 解释为什么 `eval_roofline` / `_validate_dtypes` **没有**上提到 L2（回顾 4.1.3 的 codegen 不对称），而 `_max_pool_roofline` 这个 helper **可以**共享。

**需要观察的现象**：

- avg/max 两个家族各有一个泛化基类，但共享 forward 的方式不同（avg 有 spatial fast-path，max 没有）——所以是**两个** L2 基类而不是一个，印证「数学相同但控制流不同就不该硬合」。
- `_validate_dtypes` 在每个具体类里都是同一句 `_validate_dtypes = _validate_pool_input_dtypes`，看似重复却**不能**只放基类。

**预期结果**：你能列出抽取 L2 的三条正向前提与三条反向禁忌，并对 pool 家族的每处设计选择标注它对应哪一条。

#### 4.3.5 小练习与答案

**练习 1**：avg-pool 和 max-pool 都是 pool 家族，为什么不合并成一个 `_PoolFwdOpBase`？

**参考答案**：因为它们的 forward 控制流不同——avg 有 spatial fast-path 与 generic kernel 的双槽选择（`_generic_slot` / `_spatial_slot`）、有 `count_include_pad` / `divisor_override`；max 有 dilation、有 with/without indices 的双输出变体。合并会逼出大量 `if self._is_avg` 之类的分支，正好命中「common base would need excessive if/else」这条禁忌。所以保持两个 L2 基类，各自内部再用 `ndim` 泛化 1d/2d/3d。

**练习 2**：重构 pool 家族时，若有人把 1d 的 kernel 构造关键字从 `kernel_w` 改成 `kernel_l` 以「统一命名」，会破坏什么？

**参考答案**：会破坏「保留每个变体的 kernel 构造关键字名」这条硬约束。`_MAX_POOL_PARAM_SUFFIXES[1] = ("w",)` 是为了对齐既有 1d max-pool kernel 的形参名（注释 [pool.py:89-90](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L89-L90) 明示）。改了名字，`self.kernel_map[name](**kernel_kwargs)` 就会因未知关键字报错；即便同步改 kernel，也会让一次「纯 Op 层重构」被迫牵连 Kernel 层改动，违背 Op/Kernel 双层独立可改的设计。表驱动命名正是为了让这种历史命名差异**可表达而不被抹平**。

---

## 5. 综合实践

**任务**：给一个假想的「家族重构」场景做架构评审，把本讲三条主线（三层继承、T1/T2、何时抽 L2）串起来用。

**背景**：假设 reduction 家族目前有三个独立的 T2 op——`SumFwdOp`、`MeanFwdOp`、`MaxFwdOp`——它们的 `forward()` 都是「校验 dtype → 归一化 dim → reshape 成 (M, N) → 查/建 kernel → 调 kernel → reshape 回去」这同一条流水，只在「累加器初值」「是否要 indices 输出」「pad value」上有差异。

**要求**，请逐项作答（可写成一份简短评审意见）：

1. 这个家族**是否**满足抽 L2 的前提？逐条对照 [ops-design-reference.md:390-396](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L390-L396) 的三正三反条件。
2. 若抽 L2（设为 `_ReduceOpBase`），三个差异分别应该用**类属性**还是 **hook** 承载？（提示：参考 [Family-Base Protocol 表](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L268-L281) 与 [Optional Hooks 表](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design-reference.md#L308-L318)：`_op_kind` 是类变量，`_pad_value` 是 hook。）
3. 抽完 L2 后，`eval_roofline` / `_validate_dtypes` 应该放在哪里？为什么不能放 `_ReduceOpBase`？（回顾 4.1.3）
4. 写出 `MaxFwdOp` 重构后的 T1 骨架（伪代码即可），要求：继承 `_ReduceOpBase`；只设类属性与必要的 hook；`forward` 不出现在类体里；两个 codegen 契约 class-local。

**参考要点**（先自己想再对照）：

- 满足前提：三 op 共享同一 forward 控制流 ✓、样板可观 ✓、差异（op-kind / indices / pad-value）均可入类属性或 hooks ✓；不触发任何反向禁忌。
- 承载方式：op-kind → 类属性 `_op_kind`；indices 输出 → 类属性 `_returns_indices`；pad value → hook `_pad_value`（`MaxFwdOp._pad_value = -inf`，对应 reduction 家族的 `ArgmaxFwdOp` 范例）。
- codegen 契约：放每个具体类（`SumFwdOp` / `MeanFwdOp` / `MaxFwdOp`）的 `__dict__`，因为 dtype 侧只看 `cls.__dict__`、roofline 侧会沿用基类而绕过 per-op 生成；可委托共享 helper，但绑定必须 class-local。
- T1 骨架示例（**示例代码，非项目原有**）：

  ```python
  class MaxFwdOp(_ReduceOpBase):
      _op_kind = "max"
      _returns_indices = False
      _validate_dtypes = _validate_reduce_input_dtypes   # class-local 绑定

      @property
      def default_kernel_map(self):
          return {"max_fwd": MaxKernel}

      def _pad_value(self):           # hook 覆写
          return float("-inf")

      def eval_roofline(self):        # class-local，可委托共享 helper
          return _reduce_roofline(self)
      # 注意：没有 forward() —— 来自 _ReduceOpBase
  ```

## 6. 本讲小结

- **三层继承**：`Op(L1 共享管线+codegen stub) → FamilyBase(L2 家族共享 forward，可选) → ConcreteOp(L3 叶子)`。L2 是重构产物，scaffold 只产出 L3。
- **codegen 契约必须 class-local**：`_validate_dtypes` 的安装器只看 `cls.__dict__`（基类版会被合成版覆盖），`eval_roofline` 的安装器遍历 MRO（基类版会被沿用、从而绕过 per-op 生成）。两者都不能靠继承传递，故 `test_pool_codegen_slots_are_class_local` 强制它们落在具体类 `__dict__`（可委托共享 helper）。
- **T1 / T2 是形态、与层正交**：T2 = 直接继承 `Op`、自带 forward（scaffold 产物、过渡态）；T1 = 继承 L2、只填类属性（重构后形态）。两者对用户调用透明。
- **何时抽 L2**：2-3 个 op 共享**同一 forward 控制流** + 样板可观 + 差异可入类属性/hooks；只有 1 个 op、数学同而流程不同、或需要大量 if/else 时不抽。
- **维度参数化家族**：用单一 `ndim` 泛化基类 + 表驱动命名表达 1d/2d/3d；必须保留 kernel-cache key 与构造关键字名；真正 per-rank 差异保留为显式 override，**绝不归一化**（「Family bases MUST NOT normalize genuine per-op behavior differences」）。
- **两种合法的 L2 共享风格**：elementwise 把 roofline 留 L2（遗留手写路径），pool 把 codegen 契约强制 class-local（spec 推荐路径）。

## 7. 下一步学习建议

- **u11-l2 elementwise 三大伞形基类**：深入 `UnaryOp` / `BinaryOp` / `FusedGatedOp` 如何用 `kernel_cls` + `_op_name` 模板化数十个算子，以及广播 coalesce、FP8 后置 cast、inplace 分发等共享逻辑——本讲只点了它的 L2 定位，细节在那里。
- **u11-l3 维度参数化家族（pool / reduction）**：继续读 pool 的完整重构与 reduction 的 `_multidim` / `_primitives` 共享原语，把本讲的 `ndim` 泛化模式看全。
- **u10-l1 / u10-l2 编译边界与 custom_op 工厂**：本讲多次提到 `forward` 收敛成一行分发、经 custom_op eager 体跑 `_eager_forward`——这条链的机制在这两讲。
- **延伸阅读源码**：直接对照 [`tileops/ops/pool.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py)（维度参数化范本）与 [`tileops/ops/elementwise/_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py)（伞形基类范本），把两种 L2 风格并排阅读，体会「共享 vs 抹平」的分寸。
