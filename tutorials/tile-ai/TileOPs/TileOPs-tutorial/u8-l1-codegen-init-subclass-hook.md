# `__init_subclass__` 钩子与自动安装

> 本讲属于「Manifest 驱动的代码生成」单元（u8）的第一讲。前置：**u2-l1**（Op 基类与生命周期，知道 `Op` 有三个 codegen 契约方法 `_validate_dtypes` / `_infer_output_shapes` / `eval_roofline`）、**u4-l2**（Signature 与形状规约，能读懂一个 manifest 条目的 `signature` 块）。
>
> 本讲只回答一个问题：**那些 codegen 契约方法的函数体，到底是谁、在什么时候、依据什么写进去的？** 答案是：它们不是人手写的，而是由 `Op.__init_subclass__` 钩子在你定义一个 Op 子类的瞬间，根据该算子在 manifest 里的元数据自动合成的。

## 1. 本讲目标

学完本讲后，你应该能够：

1. 解释 Python `__init_subclass__` 在「类定义时」而非「实例化时」触发的语义，并说明为什么这个时机正好适合做 manifest 驱动的代码生成。
2. 读懂 `Op.__init_subclass__` 里那两行懒导入（lazy import），说清楚它们为什么要写成函数内导入、而不是写在文件顶部（答案：打破循环依赖）。
3. 复述 `maybe_install_validator` 与 `maybe_install_eval_roofline` 这两个安装函数的**三级判定**（是否已有手写 override、`status` 是否 `implemented`、manifest 块能否解析成功），并指出它们在「override 检测」上的**关键不对称**（前者只看具体类自己的 `__dict__`，后者遍历整条 MRO）。
4. 说清楚为什么一个 `status: spec-only` 的算子即使被 import 进来，它的 `eval_roofline` 仍然停留在抛 `NotImplementedError` 的 stub——以及这与信任模型（trust model）的「逐 op、逐 PR 迁移」之间的对应关系。

## 2. 前置知识

### 2.1 三个 codegen 契约方法（承接 u2-l1）

`Op` 基类声明了三个「契约方法」：`_validate_dtypes`、`_infer_output_shapes`、`eval_roofline`。在 u2-l1 里我们已经知道：它们的职责分别是校验输入 dtype、推断输出形状、返回 `(flops, bytes)` 给基准算 SOL 效率。

但 u2-l1 留了一个悬念——基类里这三个方法的实现都只是抛 `NotImplementedError` 的「桩（stub）」。一个真正能用的算子必须有真实的方法体。这些真实的方法体从哪来？这正是本讲的全部主题：**它们由 codegen 在类定义时自动合成并安装上去**，只有当条件不满足时才保留桩。

### 2.2 manifest 是接口的唯一真相（承接 u1-l1、u4-l2）

回顾 spec-driven 设计哲学：`tileops/manifest/` 里每个算子的 `signature`（输入/输出/参数的 dtype、shape 规约）和 `roofline`（算力/访存量公式）块，是该算子接口的**唯一真相来源**。代码必须服从规约，而不是反过来。

codegen 正是这条原则的执行器：它**读 manifest**，把规约翻译成 Python 方法体，再装回 Op 类。人不需要（也不应该）手写 `eval_roofline` 或 `_validate_dtypes` 的逻辑——那样就会出现「manifest 一份真相、代码又一份真相」的漂移。

### 2.3 两个 Python 小知识

- **`__init_subclass__`**：这是 Python 的一个隐式钩子方法。当你写 `class GemmOp(Op): ...` 时，Python 在创建 `GemmOp` 这个类对象的瞬间（注意：还没实例化任何对象，只是定义了类）就会自动调用 `Op.__init_subclass__(cls=GemmOp)`。它常被用来给子类「自动登记」「自动装配属性」。
- **循环依赖（circular import）**：模块 A 在顶层 `import B`，模块 B 又在顶层 `import A`，Python 就会在加载时卡住。常用解法是把其中一个 `import` 推迟到函数内部（懒导入），让两个模块不必同时完成顶层加载。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `tileops/ops/op_base.py` | `Op` 抽象基类，定义 `__init_subclass__` 钩子与三个 stub 契约方法 | **核心**：钩子在这里，桩也在这里 |
| `tileops/ops/_dtype_codegen.py` | 从 manifest `signature.inputs` 合成 `_validate_dtypes` 的安装器 `maybe_install_validator` | **核心**：dtype 侧安装函数 + 它的「只看 `__dict__`」判定 |
| `tileops/ops/_roofline_codegen.py` | 从 manifest `roofline` 块合成 `eval_roofline` 的安装器 `maybe_install_eval_roofline` | **核心**：roofline 侧安装函数 + 它的「遍历 MRO」判定 |
| `tests/ops/test_pool.py` | 含 `test_pool_codegen_slots_are_class_local`，断言生成的 slot 必须落在具体类 `__dict__` 里 | 实践依据：用真实测试验证不对称性 |
| `tileops/manifest/pool.yaml` | `AvgPool1dFwdOp` 等池化算子的 manifest，含 `status: implemented` 与 spec-only 注释 | 实践依据：真实的 status 字段样例 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `__init_subclass__`**：钩子本身——什么时候触发、为什么懒导入。
- **4.2 `maybe_install_*`**：两个安装函数——三级判定与关键不对称。
- **4.3 spec-only 跳过**：`status` 字段如何成为 codegen 的关卡。

### 4.1 `__init_subclass__`：子类定义即触发的安装钩子

#### 4.1.1 概念说明

我们希望「每定义一个新的 Op 子类，就自动检查它的 manifest，把能合成的方法合成好」。这件事不能推迟到「实例化」——因为一个算子可能先被 import 但很久之后才被实例化，而我们希望「规约写错了」这类错误**尽早**暴露，最好是 import 这一行就报错，而不是等到某次 forward 调用。

Python 的 `__init_subclass__` 正好提供了这个时机：它在**类定义语句执行完的瞬间**被调用，比任何实例化都早。TileOPs 就把 codegen 的两个安装函数挂在了这里。

为什么不直接在子类里手写 `eval_roofline`？因为 manifest 才是真相。如果让每个算子作者手写，迟早会写成和 manifest 不一致的版本。把 codegen 集中到 `__init_subclass__`，意味着「只要 manifest 改了、类被重新定义，方法体就自动跟着改」——规约和实现不可能漂移。

#### 4.1.2 核心流程

`__init_subclass__` 的执行可以用下面这段伪代码描述：

```
# 当解释器执行到 `class GemmOp(Op): ...` 的最后一行时：
Op.__init_subclass__(cls=GemmOp):
    super().__init_subclass__()          # 走完父类链，遵守协议
    # —— 懒导入，避免循环依赖 ——
    from ._dtype_codegen   import maybe_install_validator
    from ._roofline_codegen import maybe_install_eval_roofline
    maybe_install_validator(cls)         # 尝试装 _validate_dtypes
    maybe_install_eval_roofline(cls)     # 尝试装 eval_roofline
```

关键点有三：

1. **触发时机是「类定义」，不是「实例化」**。所以 import 一个 op 模块时（这一步会执行类定义语句），钩子就已经跑了。
2. **懒导入**。两个 `import` 写在函数体里，不是写在文件顶部。
3. **两个安装函数都是「尽力而为」**：满足条件就装真实方法体，不满足就什么都不做、留下基类的 stub。

#### 4.1.3 源码精读

先看钩子本身。它非常短：

[文件路径:tileops/ops/op_base.py:57-72](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L57-L72) —— `__init_subclass__`：先调 `super().__init_subclass__`，再懒导入两个 codegen 安装函数并依次调用。

注意它的 docstring 里明确写了三条「什么时候是 no-op」：子类没有 manifest 元数据、子类自己提供了 override、或被标成 `status: spec-only`。这三条正是后面 4.2 / 4.3 要展开的内容。

那两个懒导入为什么要写成函数内导入？因为这两个 codegen 模块反过来又需要 `Op` 这个类（例如 `maybe_install_eval_roofline` 内部要 `from tileops.ops.op_base import Op` 来判断 MRO 边界）。如果在 `op_base.py` 顶部就 `import _roofline_codegen`，那么：

```
加载 op_base.py  →  顶部 import _roofline_codegen  →  加载 _roofline_codegen.py
              →  _roofline_codegen 顶部 import op_base（还没加载完！）→  循环报错
```

把它推迟到 `__init_subclass__` 函数体里，两个模块都能先各自完成顶层加载，等真正要装方法时（已经有子类被定义了）才互相引用，循环就断了。这是打破 Python 循环依赖的经典手法。

#### 4.1.4 代码实践

**实践目标**：确认 `__init_subclass__` 的触发时机是「类定义」而非「实例化」。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 在 `tileops/ops/op_base.py:71-72` 的两行 `maybe_install_*` 调用上方，**想象**各加一行 `print(f"[hook] installing for {cls.__name__}")`。
2. 打开一个 Python 解释器（在本项目环境下），执行 `import tileops.ops.gemm`。
3. 观察是否在你「还没写 `GemmOp(...)` 实例化」之前就看到了那条打印。

**需要观察的现象**：import 语句（它执行了 `class GemmOp(Op): ...` 这条定义）触发了钩子；你不必实例化 `GemmOp`。

**预期结果**：import 时钩子就跑了。> 待本地验证：如果你不愿真改源码（本讲禁止改源码），可以用 `import tileops.ops.gemm` 后立刻 `inspect.getsource(GemmOp.eval_roofline)` 看 `__qualname__` 是否形如 `GemmOp.eval_roofline`——若 codegen 装上了真实体，它的 `__qualname__` 就是合成时写的 `f"{op_name}.eval_roofline"`（见 `_roofline_codegen.py:172-173`），而不是基类的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init_subclass__` 里的两个 `import` 移到 `op_base.py` 的文件顶部，会发生什么？为什么？

> **参考答案**：会触发 `ImportError` / 部分初始化模块错误（循环依赖）。因为 `_roofline_codegen.py` 内部要 `from tileops.ops.op_base import Op`（见 `_roofline_codegen.py:684`），而此时 `op_base.py` 还没加载完（卡在顶部 import）。函数内懒导入让两个模块都能先各自完成顶层定义，循环就断了。

**练习 2**：钩子里为什么要先调 `super().__init_subclass__(**kwargs)`？

> **参考答案**：`Op` 的父类是 `ABC`，`ABC` 最终连到 `object`，而 `object.__init_subclass__` 是这个协议的「根」。调用 `super()` 保证整条父类链上的 `__init_subclass__` 都被正确触发，并正确处理可能传入的关键字参数，避免破坏 Python 的类创建协议。

### 4.2 `maybe_install_*`：解析 manifest、判定是否安装

#### 4.2.1 概念说明

`maybe_install_validator`（装 `_validate_dtypes`）和 `maybe_install_eval_roofline`（装 `eval_roofline`）是一对结构几乎相同的安装函数。它们的任务都是：**判断「要不要装」，要装就调对应的合成器生成方法体，再 `cls.<方法名> = <生成的函数>` 装上去**。

它们共享一套「判定流程」，但在一个细节上**故意不对称**——这恰恰是本轮代码更新在 `_dtype_codegen.py` 的 docstring 里专门补充说明的点（见下方源码精读）。理解这个不对称，是本讲的核心收获。

#### 4.2.2 核心流程

两个安装函数的判定流程可以抽象成同一条流水线：

```
maybe_install_XXX(cls):
  ① 【override 检测】该类是否已经（自己或祖先）提供了 XXX？
       是 → 直接 return（尊重手写实现）
  ② 【manifest 解析】先看类上挂的 __manifest_*__ 属性，没有再按 cls.__name__
       去 load_manifest() 查；查不到 → return
  ③ 【status 关卡】resolved status == "implemented" ?
       不是（含 spec-only / 缺失） → return
  ④ 【合成 + 安装】调 synthesize_XXX(...) 生成 fn；失败(ValueError) → return
       成功 → cls.XXX = fn
```

步骤 ② 的「双重解析顺序」值得记住：优先看类属性 `__manifest_signature__` / `__manifest_roofline__` / `__manifest_status__`（供单元测试和旁路调用使用），没有才退回去用 `cls.__name__` 当 key 去 manifest YAML 里查。这让测试可以不碰 YAML 就喂入假数据。

关键不对称出现在**步骤 ①**：

| 维度 | `maybe_install_validator`（dtype） | `maybe_install_eval_roofline`（roofline） |
| --- | --- | --- |
| override 检测范围 | **只看 `cls.__dict__`**（具体类自己的方法体） | **遍历整条 `cls.__mro__`**（含中间家族基类） |
| 含义 | 哪怕中间基类写了 `_validate_dtypes`，具体类没写就会被合成版**遮蔽** | 中间基类（如 `UnaryOp`）写了 `eval_roofline`，就会被**原样保留** |
| 因此生成的 slot 必须 | 落在每个具体类的 `__dict__` 里 | 可以继承自家族基类 |

这张表是本讲最需要记住的结论。它解释了为什么 `tests/ops/test_pool.py` 里有那么一个测试，强制要求 pool 家族**每个具体算子**的 `eval_roofline` 和 `_validate_dtypes` 都必须落在自己的 `__dict__` 里（见 4.2.3）。

#### 4.2.3 源码精读

先看 dtype 侧。注意 docstring 里那段「**diff 本轮新增**」的不对称说明：

[文件路径:tileops/ops/_dtype_codegen.py:322-344](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L322-L344) —— `maybe_install_validator` 的判定条件清单，其中明确写出：它「与 `_roofline_codegen.maybe_install_eval_roofline` 不同——后者尊重 MRO 中任意位置的 override；而在 dtype 侧，中间家族基类上的手写 `_validate_dtypes` 会被合成版遮蔽，因此要把它绑定在具体类体内」。

而代码里的 override 检测就一行，确实只看 `cls.__dict__`：

[文件路径:tileops/ops/_dtype_codegen.py:345-346](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L345-L346) —— `"if "_validate_dtypes" in cls.__dict__: return"`：只在具体类自己的命名空间里查，不沿 MRO 往上找。

再看 roofline 侧，对照之下它**遍历 MRO**：

[文件路径:tileops/ops/_roofline_codegen.py:686-692](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L686-L692) —— `for base in cls.__mro__: if base is Op: break; if "eval_roofline" in base.__dict__: return`：沿着继承链一路往上，只要遇到（除 `Op` 自身外）任何定义了 `eval_roofline` 的祖先类，就原样保留它、跳过合成。

后面两条关卡（status 关卡、合成+安装）两者写法一致。以 roofline 侧为例：

[文件路径:tileops/ops/_roofline_codegen.py:694-714](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L694-L714) —— 先按「类属性 `__manifest_roofline__` → manifest 查表」解析元数据；`status != "implemented"` 或 `roofline is None` 就 return；否则调 `synthesize_eval_roofline` 合成，合成抛 `ValueError` 也吞掉 return，成功才 `cls.eval_roofline = fn`。

注意最后「合成失败被吞掉」这个细节很关键：**codegen 故意不让一个写坏的 manifest 条目在 import 阶段就炸掉整个模块**。它宁可留下基类的 stub，把问题交给 manifest 验证器（`scripts/validate_manifest.py`，见 u9-l2）在 CI 里以更友好的方式报出来。这体现了一条贯穿全项目的设计取向：codegen 是「尽力装」，验证器才是「权威关卡」。

那不对称性的现实后果是什么？看这个真实测试：

[文件路径:tests/ops/test_pool.py:1584-1593](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_pool.py#L1584-L1593) —— `test_pool_codegen_slots_are_class_local` 断言每个 pool 具体算子的 `eval_roofline` 与 `_validate_dtypes` 都必须在其 `__dict__` 里。它的 docstring 一语道破天机：「从中间基类**仅继承**而来的定义，要么被生成代码悄悄遮蔽，要么悄悄绕过逐算子生成」。

> 解读：因为 dtype 侧只看 `__dict__`，如果你把 `_validate_dtypes` 只写在家族基类上、具体类没写，那么 `maybe_install_validator` 在具体类上**不会**看到它（没在 `__dict__`），就会合成一份装上——家族基类那份被「悄悄遮蔽」。而 roofline 侧遍历 MRO，家族基类上写了就会被保留、具体类**不会**被逐个生成——「悄悄绕过」。两种偏差方向相反，但都让人搞不清「这个算子到底用的是哪份方法体」。所以这条测试强制要求：**两个 slot 都要落在每个具体类里**，杜绝歧义。

#### 4.2.4 代码实践

**实践目标**：亲手验证「只看 `__dict__`」与「遍历 MRO」的差异，并读懂那条 pool 测试为何必须存在。

**操作步骤**（源码阅读 + 思考型实践，无需 GPU）：

1. 打开 [tileops/ops/_dtype_codegen.py:345](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L345) 与 [tileops/ops/_roofline_codegen.py:686-692](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L686-L692)，分别标出 override 检测的代码。
2. 假设有一个三层层级 `Op → FamilyBase → ConcreteOp`，且 `FamilyBase` 自己定义了 `_validate_dtypes` 和 `eval_roofline`，`ConcreteOp` 两个都没写。分别推理：
   - `maybe_install_validator(ConcreteOp)` 会不会装合成版？（提示：`ConcreteOp.__dict__` 里有 `_validate_dtypes` 吗？）
   - `maybe_install_eval_roofline(ConcreteOp)` 会不会装合成版？（提示：MRO 里有没有祖先定义了它？）
3. 对照 `tests/ops/test_pool.py:1584-1593`，解释这条测试为什么用 `assert "..." in op_cls.__dict__` 而不是 `assert hasattr(op_cls, "...")`。

**需要观察的现象 / 推理结论**：
- dtype 侧：`ConcreteOp.__dict__` 里**没有** `_validate_dtypes`（它只在 `FamilyBase`），所以 `maybe_install_validator` 会**装合成版**，把家族基类那份遮蔽。
- roofline 侧：MRO 里 `FamilyBase` 定义了 `eval_roofline`，所以遍历到它就 `return`，**不装合成版**，具体类静默继承家族基类版。

**预期结果**：两种情况下「这个算子最终用到的方法体」来源不同——这正是测试要拦截的歧义。`hasattr` 会沿 MRO 往上找、把继承来的也算「有」，掩盖问题；只有 `in __dict__` 才能精确断言「方法体就落在具体类自己身上」。

#### 4.2.5 小练习与答案

**练习 1**：步骤 ② 里「先看类属性 `__manifest_*__`、再查 YAML」的设计，主要服务谁？

> **参考答案**：主要服务**单元测试**和想绕过 YAML 加载的旁路调用。测试可以临时在一个假 Op 子类上挂 `__manifest_signature__` / `__manifest_status__ = "implemented"`，就能在不碰真实 manifest 的情况下驱动 codegen 路径，断言它合成了正确的函数体。

**练习 2**：为什么 `maybe_install_eval_roofline` 在合成抛 `ValueError` 时选择吞掉异常、留下 stub，而不是让 import 直接失败？

> **参考答案**：因为 codegen 的定位是「尽力装」，不是「权威校验」。如果让一个写坏的 manifest 条目在 import 时就抛错，会阻断整个 op 模块的加载，错误信息也不够友好。把问题留给 manifest 验证器（CI 里的 L3/L4 检查，见 u9-l2）以结构化方式报出更合适；此时该算子保留 stub，调用时抛 `NotImplementedError`，行为可预测、不污染其他算子。

### 4.3 spec-only 跳过：codegen 是状态关卡

#### 4.3.1 概念说明

manifest 里每个算子都有一个 `status` 字段，取值通常是 `implemented` 或 `spec-only`（见 u4-l2、u9-l3）：

- `implemented`：规约已经和实现对齐，这个算子是「真」的，有 kernel、能跑。
- `spec-only`：只有规约（接口契约），实现还没跟上。它存在的意义是「先把接口定下来，留待后续 PR 实现」。

`status` 字段正是 codegen 的**总开关**。`maybe_install_*` 在判定流程的步骤 ③ 会检查它：**只有 `status: implemented` 才合成并安装真实方法体；`spec-only` 一律跳过，保留基类那个抛 `NotImplementedError` 的 stub。**

为什么这样设计？因为 codegen 合成的方法体依赖 manifest 里的 `signature` / `roofline` 块，而对一个 `spec-only` 算子来说，这些块描述的是「未来要实现的接口」，可能还没有对应的 kernel、甚至还没有完整可解析的 roofline 公式。如果强行合成，要么装上一个指向不存在 kernel 的方法、要么装上一个语义未定的公式——都是谎话。保留 stub、抛 `NotImplementedError`，是在诚实地说：「这个算子还没实现」。

这与信任模型的「逐 op、逐 PR 迁移」直接对应：一个算子从 `spec-only` 翻成 `implemented` 需要单独的实现 PR（见 u9-l3 的 carve-out），翻转之后 codegen 才会在下一次类定义时给它装上真实体。换句话说，**codegen 是 `status` 字段的事实执行者**——status 改了，行为立刻跟着改，无需手改代码。

#### 4.3.2 核心流程

`status` 关卡的判定是一个简单的分支：

```
resolved_status = 解析得到的 status   # 来自 __manifest_status__ 或 manifest 查表
if resolved_status != "implemented":
    return        # spec-only / 缺失 / 任何其它值 → 跳过，保留 stub
# 只有 implemented 继续往下合成
```

一个 `spec-only` 算子的方法解析顺序（MRO）查找因此永远是：自己的 `__dict__` 没有 → 中间基类也没有 → 最终命中 `Op` 基类的 stub → 调用时抛 `NotImplementedError`。

#### 4.3.3 源码精读

roofline 侧 codegen 模块的 docstring 开宗明义地写明了这条关卡：

[文件路径:tileops/ops/_roofline_codegen.py:28-29](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L28-L29) —— 「`status: spec-only` 的条目保留 L1 stub——一旦 status 翻转，codegen 会重新求值它们」。

而那个被保留的 stub，就是 `Op` 基类里这三个方法。以 `eval_roofline` 为例：

[文件路径:tileops/ops/op_base.py:127-155](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L127-L155) —— `Op.eval_roofline` 的 stub：docstring 说明方法体应由 codegen 按 `docs/design/roofline.md §4.4` 合成，L1 基类只声明契约；方法体则抛 `NotImplementedError`，并配了一段 `FIXME(staged-rollout)` 块。

那段 `FIXME(staged-rollout)` 块（`op_base.py:136-150`）解释了「为什么这里用 stub 而不是 `@abstractmethod`」：

> 引自 [tileops/ops/op_base.py:136-150](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L136-L150)（`eval_roofline` 的 FIXME 块）：当前若立刻改成 `@abstractmethod`，会一次性破坏 `tileops/ops/` 下**所有**还没迁移的具体算子（它们大多还没装上 codegen 合成的 `eval_roofline`）；信任模型要求这是「逐 op 的独立迁移 PR」。等所有具体算子都装上了（经 codegen），再把这个 stub 和上面两个 stub（`_infer_output_shapes`、`_validate_dtypes`）一起转成 `@abstractmethod`。

dtype 侧的 stub 与 FIXME 块同构：

[文件路径:tileops/ops/op_base.py:104-125](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L104-L125) —— `Op._validate_dtypes` 的 stub + 同款 `FIXME(staged-rollout)` 块。

这两个 `FIXME(staged-rollout)` 块是项目「分阶段铺开」约定的物化（code-style 规则要求这类标记必须写明「破坏的不变量 / 为什么 / 清理条件」，且清理条件要描述不变量而非 PR 号）。它和 `status: spec-only` 的跳过逻辑是同一枚硬币的两面：**stub 是过渡期的安全网，codegen 是终态的供给方式**。在过渡期，stub 保证「没迁移的算子能被 import、但调用契约方法时诚实地报未实现」；终态下，所有算子都 `implemented`，codegen 装上真实体，stub 就可以退役成 `@abstractmethod`。

最后看一个真实的 manifest 样例，体会 `status` 的两种值：

[文件路径:tileops/manifest/pool.yaml:7-10](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/pool.yaml#L7-L10) —— `AvgPool1dFwdOp` 声明 `status: implemented`，于是它的 codegen 契约方法会被合成装上。而文件头注释（[pool.yaml:3-5](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/pool.yaml#L3-L5)）说明同 family 里另有条目「在实现向该契约对齐之前，有意保持 spec-only」——同文件里两种 status 并存，codegen 据此分别处理。

#### 4.3.4 代码实践

**实践目标**：解释「为什么一个 `spec-only` 算子被 import 后，它的 `eval_roofline` 仍是抛 `NotImplementedError` 的 stub」。

**操作步骤**（源码阅读型实践）：

1. 在 `tileops/manifest/` 里任找一个 `status: spec-only` 的算子条目（用 `Grep` 搜 `spec-only`）。记下它的名字。
2. 假设该算子对应的 Op 子类已被 import（即 `__init_subclass__` 已对其触发）。按本讲 4.2.2 的四级判定流水线，逐步推理 `maybe_install_eval_roofline` 会走到哪一步、在哪一步 return。
3. 据此推断：调用 `SomeSpecOnlyOp(...).eval_roofline()` 会发生什么？异常从哪个方法抛出？

**需要观察的现象**：判定在步骤 ③（status 关卡）就 return 了，因为 `resolved_status == "spec-only" != "implemented"`。

**预期结果**：方法体从未被合成，`eval_roofline` 沿 MRO 解析到 `Op` 基类的 stub，调用时抛 `NotImplementedError`，异常信息指向 `docs/design/roofline.md §4.4 (codegen)`（见 [op_base.py:151-155](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L151-L155)）。> 待本地验证：若你能在项目环境里 `import` 到一个 spec-only 算子类，可用 `inspect.getsource(SomeOp.eval_roofline)` 确认它仍是基类 stub（含 `raise NotImplementedError`）。

#### 4.3.5 小练习与答案

**练习 1**：一个 `spec-only` 算子后来在某个实现 PR 里被翻成 `status: implemented`（且补齐了 kernel_map）。翻完后，它的 `eval_roofline` 会立刻可用吗？需要人改代码吗？

> **参考答案**：会立刻可用，且**不需要人改 `eval_roofline` 的代码**。只要 manifest 里 `status` 翻成 `implemented`、`roofline` 块存在且可解析，下一次该 Op 子类被定义（import）时，`__init_subclass__` → `maybe_install_eval_roofline` 就会通过 status 关卡、合成真实体并装上。这正是 codegen 作为「status 事实执行者」的价值：翻转 status 即自动获得行为，规约与实现不会漂移。

**练习 2**：基类那三个 stub 为什么当前用「抛 `NotImplementedError` 的普通方法」而不是 `@abstractmethod`？什么时候才能改成 `@abstractmethod`？

> **参考答案**：因为现在还有大量具体算子处于迁移过渡期（还没全部装上 codegen 合成的真实体）。如果立刻用 `@abstractmethod`，这些未迁移的算子会因没实现抽象方法而**无法被实例化**，一次性破坏整个 `tileops/ops/`。`FIXME(staged-rollout)` 块写明了清理条件：**等所有具体算子都实现了这三个方法（经 codegen）之后**，再把 stub 转成 `@abstractmethod`。这是信任模型「逐 op、逐 PR 迁移」在代码里的直接体现。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「全链路推理」任务。

**场景**：假设你要新增一个算子 `FooFwdOp`，它是 elementwise 家族 `UnaryOp` 的子类（即继承链 `Op → UnaryOp → FooFwdOp`）。你在 manifest 里给它写了 `status: implemented`、一份带 `signature.inputs` 的 signature、以及一个 inline 模式的 `roofline` 块（`vars` + `flops` + `bytes`）。`FooFwdOp` 类体里你**没有**手写 `_validate_dtypes`，也**没有**手写 `eval_roofline`；但 `UnaryOp`（家族中间基类）自己**手写**了一份 `eval_roofline`（用于该家族的共享字节模型）。

**任务**：逐步回答（无需运行，纯推理，依据本讲源码）：

1. 当 `import` 触发 `class FooFwdOp(UnaryOp): ...` 定义时，`Op.__init_subclass__` 会调用哪两个函数？为什么那两个 import 是懒导入？
2. `maybe_install_validator(FooFwdOp)`：它走到四级流水线的哪一步停下？最终 `FooFwdOp._validate_dtypes` 是合成版还是基类 stub？为什么？（提示：`FooFwdOp.__dict__` 里有 `_validate_dtypes` 吗？`UnaryOp` 的那份会被考虑吗？）
3. `maybe_install_eval_roofline(FooFwdOp)`：它走到哪一步停下？最终 `FooFwdOp.eval_roofline` 用的是谁的方法体——`UnaryOp` 的手写版，还是合成版，还是 stub？为什么？（提示：roofline 侧遍历 MRO。）
4. 如果你的同事后来说「`UnaryOp` 那份 `eval_roofline` 其实不准确，`FooFwdOp` 必须用 manifest 公式」，你会怎么改，才能让 codegen 合成版生效？（提示：回顾 4.2 的不对称与那条 pool 测试。）
5. 如果 `status` 暂时是 `spec-only`（实现还没好），上面 2、3 的结论分别变成什么？

**参考要点**（自行对照）：
1. 调 `maybe_install_validator` 和 `maybe_install_eval_roofline`；懒导入是为打破 `op_base ↔ _*_codegen` 的循环依赖（codegen 模块内部要 `import Op`）。
2. `maybe_install_validator` 在 override 检测处停下——但**不**会停在「发现 override」上，因为它只看 `cls.__dict__`，而 `FooFwdOp.__dict__` 里没有 `_validate_dtypes`（`UnaryOp` 的那份不在考虑范围）。于是它继续过 status 关卡（`implemented` ✓）、合成成功，给 `FooFwdOp` 装上**合成版**。家族基类那份被遮蔽。
3. `maybe_install_eval_roofline` 遍历 MRO，在 `UnaryOp` 处发现 `"eval_roofline" in UnaryOp.__dict__`，**立即 return**，不合成。`FooFwdOp.eval_roofline` 用的是 **`UnaryOp` 的手写版**（静默继承）。
4. 要让合成版生效，必须在 `FooFwdOp` **具体类体**里把 `eval_roofline` 显式定义出来（哪怕只是 `del` 掉继承再让 codegen 接管，或按 pool 测试的要求直接让具体类持有 slot）——因为 roofline 侧尊重 MRO 里的 override，只要祖先有就会被保留。这也正是 `test_pool_codegen_slots_are_class_local` 要求「slot 必须落在具体类 `__dict__`」的现实意义。
5. 若 `status: spec-only`：dtype 侧和 roofline 侧都在 status 关卡（步骤 ③）return，都不合成；`FooFwdOp._validate_dtypes` 与 `eval_roofline` 分别解析到 `UnaryOp`（若有）或最终 `Op` 基类的 stub，调用契约方法时抛 `NotImplementedError`。

> 这个综合练习把「触发时机 → 安装判定 → status 关卡 → 不对称后果」整条链路走了一遍。能独立完成，说明你已经掌握了 `__init_subclass__` 钩子如何把 manifest 变成运行时方法体。

## 6. 本讲小结

- **`__init_subclass__` 是 codegen 的触发点**：它在 Op 子类**定义**（import）的瞬间触发，而非实例化时；钩子里两行懒导入 `maybe_install_validator` / `maybe_install_eval_roofline` 写成函数内导入，是为了打破 `op_base` 与两个 codegen 模块之间的循环依赖。
- **两个安装函数共享四级判定流水线**：① override 检测 → ② manifest 解析（先类属性 `__manifest_*__`、再按 `cls.__name__` 查 YAML）→ ③ `status == "implemented"` 关卡 → ④ 合成并 `cls.XXX = fn`；合成失败被吞掉、留下 stub，把校验权威让给 manifest 验证器。
- **关键不对称**：`maybe_install_validator` 只看 `cls.__dict__`（家族基类的手写版会被合成版遮蔽），`maybe_install_eval_roofline` 遍历整条 MRO（家族基类的手写版会被保留、具体类静默继承）。这正是本轮 `_dtype_codegen.py` docstring 专门补充、并由 `test_pool_codegen_slots_are_class_local` 强制约束的点——两个 slot 都必须落在具体类自己的 `__dict__` 里以杜绝歧义。
- **`status: spec-only` 是 codegen 的总开关**：非 `implemented` 一律跳过合成、保留基类抛 `NotImplementedError` 的 stub。这意味着「接口先于实现」的算子可以安全 import，调用契约方法时诚实地报未实现；翻转 status 后无需人改代码，下次定义即自动获得真实体。
- **stub 当前是普通方法而非 `@abstractmethod`**：三处 `FIXME(staged-rollout)` 块说明，等所有具体算子都迁移完毕（经 codegen 装上真实体），才会把这些 stub 一起转成 `@abstractmethod`——这是信任模型「逐 op、逐 PR 迁移」在代码层的直接体现。
- **codegen 是「尽力装」，验证器才是「权威关卡」**：合成失败不阻断 import，问题交由 CI 里的 manifest 验证器以结构化方式报告。

## 7. 下一步学习建议

本讲讲清了「方法体是谁、何时、据什么装上去的」。接下来：

- **u8-l2 Roofline 代码生成**：钻进 `synthesize_eval_roofline`，看 inline 模式如何把 manifest 的 `vars` / `flops` / `bytes` 表达式字符串编译成纯 Python 方法体，以及 AST 校验如何守住「vars 层 / 算术层」的命名空间边界。
- **u8-l3 Dtype 校验代码生成**：钻进 `synthesize_validate_dtypes`，看 manifest `signature.inputs` 的 dtype 并集、`same_as(ref)`、`dtype_combos` 如何被编译成 `_validate_dtypes` 的函数体，以及它的关键字参数名为何要镜像 `signature.inputs`。
- **u9-l2 验证器的五级检查**：看 manifest 验证器如何用 `inspect.signature` 对 codegen 合成的方法做 parity 探测（L2/L3），把 codegen「尽力装」留下的缺口在 CI 里兜住。
- 若想验证本讲结论，可读 `tests/ops/test_pool.py` 的 `test_pool_codegen_slots_are_class_local`（[1584-1593](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_pool.py#L1584-L1593)）——它是「不对称性」这条结论最直接的测试物证。
