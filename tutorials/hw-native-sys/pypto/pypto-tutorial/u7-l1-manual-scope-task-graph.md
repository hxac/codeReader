# u7-l1 manual_scope 与任务图编程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PyPTO 运行时**不是逐条执行**编排函数，而是先构建任务依赖图、再调度就绪任务——以及「语句顺序本身不表达任何顺序」这一核心事实。
2. 掌握 `pl.submit` 的完整形态：参数、`deps=` 关键字、以及它必然解包成「任务输出 + TaskId」二元组的返回约定。
3. 理解**手动依赖**（`deps=`）与**自动依赖推导**（OverlapMap/TensorMap）的边界：两者是正交机制，最终等待集是两者的并集。
4. 能独立编写多任务依赖的编排程序（含 TaskId 数组 fan-in、循环携带、phase fence 压缩）。
5. 会用 `enable_dep_gen` 导出 `deps.json`，并用 `deps_viewer` 的 `reduced` 与 `reduced_dataflow` 两种模式审计依赖图中的**冗余边**，且知道为什么 `reduced` 单独报 0 不算证据。

本讲是高级单元的第一讲，承接 u3-l4（运行时执行）对 runner 与 RunConfig 的认知，把视角从「一次编译+执行」转向「执行背后的任务图是怎么被塑造的」。

## 2. 前置知识

### 2.1 任务图与依赖边

PyPTO 的编排函数（`FunctionType.Orchestration`）运行在 AICPU 侧，它**不是**逐语句解释执行的。运行时把它发射的每个内核调用看成一个**任务（task）**，任务之间用**依赖边（dependency edge）**连接，构成一张有向无环图；调度器每次挑出「所有前驱都已完成」的任务派发到空闲核心。

于是有一条容易踩的推论：**两条派发语句写在前后两行，并不意味着它们有顺序关系**。顺序只有两个来源——运行时从缓冲区读写推出来的边，或者你亲手声明的边。

### 2.2 TaskId：任务的句柄

每个被提交的任务会得到一个 `Scalar[TASK_ID]` 类型的值，它是这个任务的**名字**。拿到生产者的 TaskId，就能在消费者的 `deps=` 里引用它。TaskId 不参与算术，只用于依赖 wiring；`pl.TaskId` 是它的 DSL 别名（u2-l2 已讲过注解体系）。

### 2.3 三种经典数据冒险

| 冒险 | 含义 | 运行时是否自动追踪 |
| --- | --- | --- |
| RAW（read-after-write） | 读者要等写者 | 追踪 |
| WAW（write-after-write） | 新写者要等旧写者 | 追踪 |
| WAR（write-after-read） | 写者要等所有在途读者读完 | **不追踪** |

WAR 不追踪是刻意的设计取舍：写者要找到所有在途读者，等于每次写都遍历一遍读者集合，太贵。需要 WAR 顺序时，由你自己用 `deps=` 声明。

### 2.4 传递约简与冗余边

若边 \((u, v)\) 之外还存在一条从 \(u\) 到 \(v\) 的更长路径，则 \((u, v)\) 是**冗余边**：删掉它不改变任何可达性，也就不改变执行顺序，只减轻调度器的簿记负担。把图中所有这类边删掉得到的结果叫**传递约简（transitive reduction）**：

\[ T = \{(u,v) \in E \mid \neg\,\exists\, \text{path}(u \to v) \text{ in } E \setminus \{(u,v)\}\} \]

这是本讲最后一节审计工具的数学基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pypto/language/scope.py` | DSL 侧的 `ScopeMode` / `scope` / `manual_scope` / `submit` / `spmd_submit` 定义（本体只是文档 + 抛错占位，真正逻辑在解析器） |
| `examples/intermediate/07_task_graph.py` | 教学示例：推断边 vs 声明边的最小对照 |
| `examples/utils/phase_fence_dep_compression.py` | 进阶示例：manual_scope + TaskId 数组 + 循环携带的相栏压缩程序 |
| `python/pypto/language/parser/ast_parser.py` | 解析器：`with pl.manual_scope():` 与 `out, tid = pl.submit(...)` 的语法拦截 |
| `include/pypto/ir/expr.h` | `Submit` IR 节点定义，`deps_` 一等字段 |
| `include/pypto/ir/stmt.h` | `RuntimeScopeStmt`，`manual_` 标志 |
| `src/codegen/orchestration/orchestration_codegen.cpp` | 编排代码生成：把 `deps_` 发射成 `set_dependencies(...)` |
| `src/ir/op/sync_ops/task.cpp` | `system.task_invalid` / `system.task_dummy` 算子注册 |
| `docs/en/dev/language/02-manual_dependencies.md` | 两种机制的权威参考（机制 A / 机制 B） |
| `docs/en/user/tutorials/04-task-graph.md` | 用户教学篇：任务图塑造五步法 |
| `docs/en/user/performance/03-dependencies.md` | 性能篇：依赖管理 + 冗余边审计（本版新增章节） |
| `tests/ut/codegen/test_orchestration_manual_scope.py` | manual_scope 编排代码生成的回归测试 |

## 4. 核心概念与源码讲解

### 4.1 运行时作用域：ScopeMode、scope 与 manual_scope

#### 4.1.1 概念说明

运行时作用域（runtime scope，IR 里降为 `PTO2_SCOPE` 块）是**资源管理 + 依赖追踪的边界**：它限定 OverlapMap 自动依赖追踪的范围，并给嵌套作用域各自独立的 HeapRing 内存回收层级。

它有两种模式，由 `ScopeMode` 枚举区分：

- `AUTO`：自动依赖追踪**开**，降为 `PTO2_SCOPE()`；
- `MANUAL`：自动依赖追踪**关**，区域内每条边都由你用 `deps=` 声明，降为 `PTO2_SCOPE(PTO2ScopeMode::MANUAL)`。

关键认知：**写作用域从来不是正确性要求，而是调优/控制手段**。运行时本就有隐式顶层作用域；默认 `auto_scope=True` 时编译器还会替你在函数体和每个 `for`/`if` 体外包一层 AUTO 作用域。你亲手写 `manual_scope`，是因为推断出来的边大多是对的但你想全权接管。

#### 4.1.2 核心流程

```text
with pl.manual_scope():            ──解析──▶  RuntimeScopeStmt(manual_=true)
     │                                            │
     │  区域内每次 pl.submit(...)                   │ Pass 44 MaterializeRuntimeScopes
     │                                            ▼
     │                                     编排代码生成发射 PTO2_SCOPE(PTO2ScopeMode::MANUAL)
     ▼
运行时：跳过该区域内所有 OverlapMap 查找与插入
        （创建者保留、生产者查找一并跳过——每条边都是你写的）
```

解析器在这条路径上执行三条放置规则校验：

1. MANUAL 不能嵌套在另一个 MANUAL 里；
2. AUTO 不能嵌套在 MANUAL 里（运行时禁止）；
3. 手写 AUTO `with pl.scope():` 必须配合 `@pl.function(auto_scope=False)`，否则报错——默认模式下编译器拥有 AUTO 放置权，一个散落的 `with pl.scope():` 只会变成无声的 no-op，所以直接拒绝。

#### 4.1.3 源码精读

先看 DSL 侧定义。`ScopeMode` 是一个只有两个值的枚举，注释里直接写明了两种模式各自降成什么：

[python/pypto/language/scope.py:L16-L25](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L16-L25)

这段定义了 `AUTO = 0` / `MANUAL = 1`，并说明 MANUAL 模式下「用户通过 `pl.submit(..., deps=[...])` 声明每一条边」。

`scope` 类是通用入口，接受 `mode=` 关键字。注意它的规则列表：必须出现在 Orchestration 函数里（不能在 InCore 里）；手写 AUTO 需要 `auto_scope=False`；MANUAL 在两种模式下都合法，因为它是「依赖语义选择」而非 ring 调优：

[python/pypto/language/scope.py:L28-L67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L28-L67)

`manual_scope` 是 `pl.scope(mode=pl.ScopeMode.MANUAL)` 的别名，也是三种「自动追踪豁免」里最粗粒度的一种。它的 docstring 列出了完整的三档豁免，这点很重要，值得记牢：

[python/pypto/language/scope.py:L70-L108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L70-L108)

三种粒度从粗到细：

| 豁免面 | 写法 | 效果 |
| --- | --- | --- |
| 整个区域 | `with pl.manual_scope():` | 区域内所有任务都跳过 OverlapMap |
| 单个张量的一生 | `pl.create_tensor([...], dtype=..., manual_dep=True)` | 任何引用该张量的任务都跳过 |
| 单次调用的单个参数 | `pl.no_dep(arg)` / `with pl.at(..., no_dep_args=[t])` | 仅这一任务的这一参数跳过 |

注意 docstring 里那句加粗的话：**手动依赖边与上述豁免是正交的**——运行时把显式边叠加在剩余的自动追踪边之上（最终 fanin = auto ∪ explicit），所以 `deps=` 在 auto scope 里同样可用。这是本讲最容易误解的一点，后面 4.3 会反复用到。

再看 IR 落点。`manual_scope` 被解析成 `RuntimeScopeStmt`，它只是 `ScopeStmt` 的一个薄包装，多了一个 `manual_` 布尔字段：

[include/pypto/ir/stmt.h:L948-L991](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/stmt.h#L948-L991)

注释里写清了两条降级对应关系（`manual_ = false → PTO2_SCOPE()`、`manual_ = true → PTO2_SCOPE(PTO2ScopeMode::MANUAL)`），以及 manual 区域内 `deps=` 列表写入 `Submit::deps_` 类型化字段、代码生成打包成栈上 `PTO2TaskId[]` 数组并发射一次 `params.set_dependencies(arr, count)` 的完整链路。

最后看解析器的放置规则实现。`_parse_manual_scope` 只做参数检查（不接受任何参数），然后调 `_emit_runtime_scope(manual=True)`：

[python/pypto/language/parser/ast_parser.py:L3956-L4005](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L3956-L4005)

这里能看到 4.1.2 列出的三条规则的真实代码：`_manual_scope_depth > 0` 时嵌套 MANUAL 报错（用 try/finally 维护深度计数器）；`_func_auto_scope` 为真时手写 AUTO 报错；AUTO 嵌套在 MANUAL 里也报错。

#### 4.1.4 代码实践

**实践目标**：亲手触发解析器的三条放置规则，把报错信息与源码分支对应起来。

**操作步骤**：

1. 新建一个临时文件 `scope_rules.py`，先写一个合法基线（能跑通的最小 manual_scope）；
2. 依次做三个非法变体，每个都单独运行、记录报错：
   - 变体一：`with pl.manual_scope():` 内部再嵌一层 `with pl.manual_scope():`；
   - 变体二：`with pl.manual_scope():` 内部写 `with pl.scope():`（手写 AUTO）；
   - 变体三：不加 `auto_scope=False`，直接在函数体顶层写 `with pl.scope():`。
3. 打开 [python/pypto/language/parser/ast_parser.py:L3966-L4005](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L3966-L4005)，把三条报错的 `raise ParserSyntaxError(...)` 分别与你的三个变体对上号。

**需要观察的现象**：三条报错的 hint 文案不同（「Flatten the nested manual scope…」「requires @pl.function(auto_scope=False)」「The runtime forbids AUTO scope nested in MANUAL scope」），分别对应三条不同的规则。

**预期结果**：三个变体都在**解析期**（还没进 Pass 流水线）被拒绝，且报错附带的 span 指向你源码里出错的 `with` 行。若某变体没有报错而是跑通了，说明你的写法实际落在了别的分支——回去核对 `_emit_runtime_scope` 的判断顺序。

**待本地验证**：具体报错文案以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：`manual_scope` 为什么不允许嵌套？从运行时语义角度给出理由。

**参考答案**：嵌套的 MANUAL 相对外层 MANUAL 不会带来任何新语义——内层能做的事（关闭自动追踪）外层已经全关了，剩下的只是多一层 `PTO2_SCOPE` 包装和一次多余的 ring 层级切换。而如果允许「外 MANUAL 内 AUTO」，语义会变得不可判定：内层 AUTO 重新打开的 OverlapMap 追踪会引用外层已经跳过注册的生产者信息，产出的边是残缺的。所以运行时两条都禁，解析器提前拦。

**练习 2**：`manual_` 是 `RuntimeScopeStmt` 的 `UsualField`（见 [include/pypto/ir/stmt.h:L984-L987](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/stmt.h#L984-L987)）。这意味着结构化相等比较（u4-l8）默认会比较它吗？这个选择合理吗？

**参考答案**：`UsualField` 跟随调用方开关——`assert_structural_equal` 默认路径会比较它。合理：`manual_` 决定的是这段代码的**调度契约**（区域内边从哪来），两个除了 `manual_` 以外完全相同的程序在运行时会产生不同的任务图，把它们判成「结构相等」会掩盖真实的语义差异。这和 `Span`、变量名被忽略形成对照——那些确实不影响语义。

### 4.2 pl.submit：任务发射与 Submit IR 节点

#### 4.2.1 概念说明

`pl.submit` 是把一个内核调用**升格为可命名的任务**的原语。它与普通 `out = self.kernel(...)` 的区别有三点：

1. **返回多一个 TaskId**：返回类型恒为 `Tuple[<被调内核返回>..., Scalar[TASK_ID]]`，所以必须写成 `out, tid = pl.submit(...)` 解包；
2. **可以携带 `deps=`**：声明这个任务要等待哪些生产者；
3. **是解析器构造而非运行时函数**：`pl.submit` 这个 Python 名字下的函数体只会 `raise RuntimeError`，解析器在 AST 层拦截这个调用，从不真正执行它。

第 3 点和 `pl.range` 是同一类设计（u3-l1 讲过解析器拦截）：函数体存在的意义只是让 import 和 linter 不报未定义。

普通调用形式 `out = self.kernel(...)` 是**发射后不管（fire-and-forget）**：不返回任务句柄，也不接受 `deps=`——解析器直接报错并提示「use `pl.submit`」。

#### 4.2.2 核心流程

```text
out, tid = pl.submit(self.kernel, x, out, deps=[prev_tid])
   │
   ├─ 解析器 _parse_submit_assignment：
   │    1. 校验 LHS 恰好 2 个目标（结果 + TaskId）
   │    2. 校验第一个实参是 self.<kernel> 方法引用（传内核本身，不是调用它）
   │    3. _parse_kernel_call(as_submit=True) 构造 ir.Submit
   │    4. 校验结果目标数 == 内核返回元数 + 1
   │    5. builder.let 绑定扁平 tuple，逐元素 TupleGetItemExpr 解包
   │    6. 记录 _submit_producer_tid（供 predicate= 契约检查用）
   ▼
ir.Submit { op_, args_, deps_, ... }
   ▼
编排代码生成 EmitManualDeps → set_dependencies(...)
```

#### 4.2.3 源码精读

先看 DSL 侧的文档本体。`submit` 的 docstring 是这个构造最完整的一手说明，明确写了「解析器构造，永不真正调用此函数体」：

[python/pypto/language/scope.py:L117-L143](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L117-L143)

这段列出了全部表面形式：`out, tid = ...`、`(a, b), tid = ...`（多输出内核）、`deps=[...]`、`dumps=[...]`（选择性张量转储）、`allow_early_resolve=True`（投机早派发提示）。并且再次强调 `deps=` 在 auto 和 manual 两种 scope 里都可用。

函数体只有一句抛错，坐实「拦截而非执行」：

[python/pypto/language/scope.py:L195-L199](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L195-L199)

解析器侧的核心是 `_parse_submit_assignment`。它的文档字符串写清了脱糖结果——「单个 `ir.Submit`，返回类型是扁平的 `TupleType([*<内核结果>, Scalar(TASK_ID)])`」：

[python/pypto/language/parser/ast_parser.py:L2051-L2133](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L2051-L2133)

注意其中两处校验：LHS 必须**恰好**两个目标且 TaskId 目标必须是裸变量名；第一个实参必须是 `self.<kernel>` 形式的 `ast.Attribute`（`method_attr.value.id == "self"`），传 `self.kernel(x, y)` 这种「已经调用」的形式会被拒绝。

解包绑定部分展示了扁平 tuple 如何被拆开——元素 `0..N-1` 是内核结果，元素 `N` 是生产者 TaskId：

[python/pypto/language/parser/ast_parser.py:L2146-L2160](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L2146-L2160)

`builder.let("_submit_tmp", call_expr)` 先把整个 Submit 绑成一个临时变量，再用 `TupleGetItemExpr` 逐槽取出。最后把每个结果 Var 与它的生产者 TaskId 记进 `_submit_producer_tid`，这是 `predicate=` 契约检查（「谓词操作数张量的生产者必须在 `deps=` 里」）的数据来源。

IR 层的 `Submit` 节点。`deps_` 是一等字段，不是 attr：

[include/pypto/ir/expr.h:L960-L1055](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/expr.h#L960-L1055)

类前的注释交代了三件事：`deps_` 承载 `deps=[tid1, tid2, ...]` 传入的显式跨任务依赖，每个条目是引用 `Scalar[TASK_ID]` Var 或 `Array[N, TASK_ID]` Var 的 `ExprPtr`；**遍历变量使用点的 Pass 必须把 `deps_` 算进去**（否则 TaskId 变量看起来无人使用，会被 DCE 误删——这正是 u4-l2 讲过的 Submit 与 Call 的字段差异）；`kAttrManualDepEdges` 这个 attr 键在 `Submit` 上**故意不读**，一律走类型化的 `deps_`。

`deps_` 的合法类型由一个独立谓词函数把关，它接受两种形状：

[python/pypto/language/parser/ast_parser.py:L233-L250](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L233-L250)

即 `ScalarType(TASK_ID)`（单个 TaskId，含 `None` 降成的 `system.task_invalid` 哨兵）和 `ArrayType(..., TASK_ID)`（`pl.array.create(N, pl.TASK_ID)` 造出的按槽 TaskId 数组）。**张量不能进 `deps=`**。

`spmd_submit` 是 SPMD 兄弟：一次编排任务扇出到 `core_num` 个逻辑块（每块内核用 `pl.tile.get_block_idx()` 读自己的块号），但仍然只返回**一个**生产者 TaskId，整个派发可以被命名成后续任务的依赖。`core_num` 是必填关键字：

[python/pypto/language/scope.py:L202-L233](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L202-L233)

#### 4.2.4 代码实践

**实践目标**：验证 `pl.submit` 的「拦截而非执行」性质，以及返回元数校验的真实行为。

**操作步骤**：

1. 在 Python 里直接 `import pypto.language as pl` 然后调用 `pl.submit(print)`，观察异常；
2. 读 [python/pypto/language/scope.py:L195-L199](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/scope.py#L195-L199)，确认异常文案就是这里写死的；
3. 写一个双输出内核（返回两个 `pl.Tensor`），在编排函数里分别用 `(a, b), tid = pl.submit(self.dual, x)` 和 `a, tid = pl.submit(self.dual, x)` 两种形式，后者用 `kernel.compile()` 触发解析；
4. 对照 [python/pypto/language/parser/ast_parser.py:L2137-L2144](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L2137-L2144)，确认第 3 步的报错来自返回元数校验（`unpacks N result value(s) but kernel returns M`）。

**需要观察的现象**：第 1 步抛 `RuntimeError` 且文案以 "pl.submit is a DSL parser construct" 开头；第 3 步的错误形式解包成功、错误形式在解析期报元数不匹配。

**预期结果**：你会直观看到「DSL 函数体只负责让名字可解析，真正逻辑全在解析器」这一分层——和 u3-l1 讲过的 `_dsl_invoker` 拦截机制是同一套路。

**待本地验证**：具体异常类型与文案以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pl.submit` 的 `deps_` 必须是一等 IR 字段，而不能像其他元数据那样塞进 `attrs_` 字符串字典？

**参考答案**：`deps_` 的每个条目是一个**真实的 SSA 值**（TaskId Var 或 TaskId 数组 Var），参与 use-def 链。塞进 attrs 意味着：(a) 收集变量使用的 Pass 看不到它们，TaskId 变量会被判为死代码删掉；(b) SSA 支配性检查不会验证 `deps_` 里的 TaskId 是否在 Submit 之前定义；(c) 序列化与结构化比较要额外特判。项目为此专门设了一条 `ManualDepsOnSubmitOnly` 不变量（普通 Call 永不携带 `manual_dep_edges`），并有独立验证器守护（`src/ir/verifier/verify_manual_deps_on_submit_only.cpp`）。

**练习 2**：`out, tid = pl.submit(self.k, x)` 里 `out` 和 `tid` 各自的 IR 类型是什么？如果 `self.k` 声明返回 `pl.Tensor[[64], pl.FP32]`，整个 Submit 表达式的类型又是什么？

**参考答案**：`out` 是 `Tensor[[64], FP32]`（Submit 返回元组的元素 0），`tid` 是 `Scalar[TASK_ID]`（元素 1）。整个 Submit 表达式的类型是 `TupleType([TensorType([64], FP32), ScalarType(TASK_ID)])`——被调内核的返回类型打平后追加一个 TASK_ID 标量，固定在尾部。

### 4.3 deps= 显式依赖边：从解析到 set_dependencies

#### 4.3.1 概念说明

`deps=` 解决的问题是：**有些顺序关系永远不会以缓冲区重叠的形态出现**。比如任务 B 要等任务 A，因为 A 里做了某个 host 侧副作用（配置了某个寄存器、推进了某个流指针），而两者不共享任何张量——OverlapMap 从缓冲区推导边，它看不见这种顺序，你必须写出来。

要用对 `deps=`，先要内化这条等式：

\[ \text{final wait set} \;=\; \text{auto-tracked edges} \;\cup\; \text{explicit } deps{=} \]

两者**组合而非替代**。这意味着：

- 在 auto scope 里用 `deps=` 是「精准修补工具」——自动推导负责大部分图，你只补它推不出的那几条；
- 在 `manual_scope` 里用 `deps=` 是「全权接管」——区域内自动追踪整个关掉了，每条边都是你写的。

#### 4.3.2 核心流程

```text
deps=[tid_a, tid_b]
   │ 解析器写入 Submit::deps_（类型化字段）
   ▼
编排代码生成 EmitManualDeps(call, task_var)：
   1. GetDependencyEdges(call)      取边（用户边 + 编译器派生边，去重）
   2. CountManualDeps(...)           数出数组容量 K
   3. 发射 PTO2TaskId <task>_deps[K]; uint32_t <task>_deps_count = 0;
   4. 逐条 EmitDepArrayInsert：
        - 新鲜直接生产者 id（静态可证有效）→ 直接赋值
        - 其他（可能是 invalid 哨兵）→ if (name.is_valid()) 赋值
   5. 发射 <task>.set_dependencies(<task>_deps, <task>_deps_count);
```

数组条目可能是 `PTO2TaskId::invalid()` 哨兵的场景：`None` 循环携带种子、循环首迭代的 iter_arg 携带、未写入的数组槽、外提/哨兵/dummy tid。哨兵绝不能进 `set_dependencies`，所以 `is_valid()` 守卫是必须的。

#### 4.3.3 源码精读

教学示例 `07_task_graph.py` 用同一对任务展示了两种写法。第一个内核什么都不声明，边从缓冲区方向「掉出来」：

[examples/intermediate/07_task_graph.py:L39-L51](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/07_task_graph.py#L39-L51)

stage1 把 `scratch` 声明为输出，运行时记下它是生产者；stage2 读同一缓冲区，于是推出一条 RAW 边。**什么都不写，顺序也有保证**——这是不需要本讲任何机制的默认情形。

第二个内核把同样的顺序**说出来**。`as first` 绑定生产者区域的 TaskId，`deps=[first]` 让消费者等它：

[examples/intermediate/07_task_graph.py:L54-L67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/07_task_graph.py#L54-L67)

示例的 docstring 特意提醒：这里**没有** `manual_scope`——显式边叠加在自动追踪之上，不是取代它。也正因两个区域共享 `scratch`，这条边在第一步里**已经**被推出来了；在这个可验证的用例上写出它，改变不了什么。它演示的是机制，不是需求。**你真正需要 `deps=` 的场合，是两个任务完全不共享缓冲区的时候——那时错误答案是一场竞态而不是一个数字，无法靠 golden 对照发现。**

`pl.at(..., deps=[...]) as tid` 是 `pl.submit` 的外提区域版：整个块被外提成 InCore 内核 + Submit，`as tid` 捕获合成 Submit 的 TaskId。权威参考的机制 B 表格列出了全部五种「声明显式边」的表面形式：

[docs/en/dev/language/02-manual_dependencies.md:L25-L37](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/language/02-manual_dependencies.md#L25-L37)

按生产者形状选：单个内核调用用 `pl.submit`、单个 SPMD 派发用 `pl.spmd_submit`、外提的 `pl.at` 块用 `with pl.at(..., deps=...) as tid:`、外提的 SPMD 派发用 `with pl.spmd(N, deps=...) as tid:`、纯 fan-in 点用 `pl.system.task_dummy(deps=[...])`。表格最后一行还有一个容易忽略的条目：Python 字面量 `None` 是「还没有生产者」的哨兵，用来给 TaskId 循环携带变量播种。

代码生成侧，`EmitManualDeps` 是把 `deps_` 变成 C++ 文本的完整实现：

[src/codegen/orchestration/orchestration_codegen.cpp:L2916-L2954](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/orchestration/orchestration_codegen.cpp#L2916-L2954)

逐行读可以看到：`dep_capacity == 0` 时直接返回（无边的 no-op）；数组大小精确等于边数（栈上定长数组，没有每次调用的边上限）；`emitted_names` 集合去重；数组携带（array-carry iter_arg）会展开成每个有效槽一条；最后**只发一次** `set_dependencies` 调用。函数前的注释还强调了一遍正交性——这在 auto 和 manual 两种 scope 里都会执行。

守卫逻辑在 `EmitDepArrayInsert`，二选一：

[src/codegen/orchestration/orchestration_codegen.cpp:L2904-L2915](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/orchestration/orchestration_codegen.cpp#L2904-L2915)

`guaranteed_valid_task_ids_` 集合里的名字（被静态证明恒有效的直接生产者 id）跳过守卫直接赋值，其余一律包上 `if (name.is_valid())`。

最后看一条端到端的测试断言，它把整条链的期望产物写成了正则：

[tests/ut/codegen/test_orchestration_manual_scope.py:L1311-L1388](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_orchestration_manual_scope.py#L1311-L1388)

这个测试的程序体就是 `scratch, tid = pl.submit(self.stage1, ...)` 接 `out, _ = pl.submit(self.stage2, ..., deps=[tid])`，断言生成代码里出现 `PTO2TaskId <x> = task_0_outs.task_id();`、`params_t1_deps[params_t1_deps_count++] = <x>;`、`params_t1.set_dependencies(params_t1_deps, params_t1_deps_count);` 三行——正是 4.3.2 流程图预言的产物。这个测试也是你做本讲综合实践时的「期望输出模板」。

#### 4.3.4 代码实践

**实践目标**：跑通 `07_task_graph.py`，并在生成的编排代码里找到那条显式边。

**操作步骤**：

1. 运行示例：`python examples/intermediate/07_task_graph.py`，确认打印 `OK`（两种模式的结果都与 `(x + x) + (x + x)` 一致）；
2. 阅读用户教学篇的对应章节，它逐步解释了这两段代码各自证明什么、不证明什么：[docs/en/user/tutorials/04-task-graph.md:L60-L91](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/04-task-graph.md#L60-L91)；
3. 仿照 [tests/ut/codegen/test_orchestration_manual_scope.py:L1311-L1388](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_orchestration_manual_scope.py#L1311-L1388) 的做法，把 `declared_edge` 改写成 `@pl.program` 形式（`pl.submit` 需要的是 `self.<kernel>` 方法引用，`@pl.jit` 顶层函数里没有 `self`，所以要么用 `pl.at(..., deps=...) as tid`，要么搬进 program 类）；
4. dump 编排产物，找出 `set_dependencies` 那一行和它上面填充数组的行。

**需要观察的现象**：`declared_edge` 的编排产物里有一条被显式填充的 deps 数组（长度 1）加一次 `set_dependencies` 调用；`inferred_edge` 的产物里则**没有** `set_dependencies`——它的顺序完全靠运行时缓冲区追踪保证。

**预期结果**：两份产物的**执行结果相同**（都被 `torch.allclose` 断言），但**产物文本不同**——这正是「显式边叠加而非替代」在文件层面的证据。

**待本地验证**：编排产物的具体文件名与 dump 方式（`--save-kernels` / `RunConfig` 的 dump 开关）依本地环境而定，建议参考 u6-l2 讲过的编排代码生成导出方法。

#### 4.3.5 小练习与答案

**练习 1**：把 `07_task_graph.py` 的 `declared_edge` 里 `deps=[first]` 删掉，程序还能跑对吗？为什么？这说明了什么？

**参考答案**：能跑对。因为 stage1 和 stage2 共享 `scratch` 缓冲区，运行时自动推导的 RAW 边已经保证了顺序，显式边只是叠加。这说明：**在共享缓冲区的场合写 `deps=` 是冗余的**；`deps=` 的真正用武之地是没有任何缓冲区重叠的顺序关系，而那种场合删掉边会得到一场间歇性竞态，而不是一个能被 `allclose` 抓住的错误。这也正好引出 4.5 节的冗余边审计。

**练习 2**：`deps=[tensor_a]`（传一个张量）会发生什么？为什么不设计成允许张量？

**参考答案**：解析期报错。`_is_dep_var_type`（[python/pypto/language/parser/ast_parser.py:L233-L250](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L233-L250)）只接受 `ScalarType(TASK_ID)` 和 `ArrayType(..., TASK_ID)`。不允许张量是因为依赖的单位是**任务**而不是数据：运行时的边语义是「等这个任务完成」，张量没有单一的完成时刻（多任务写同一张量的不同区域时尤其如此）。若想表达「等这个张量的生产者」，你要么拿到那个生产者的 TaskId，要么依赖自动追踪。

**练习 3**：同一个 TaskId 在 `deps=[tid, tid]` 里出现两次，会生成两条边吗？

**参考答案**：不会。`EmitManualDeps` 里的 `emitted_names` 集合（[src/codegen/orchestration/orchestration_codegen.cpp:L2924-L2928](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/orchestration/orchestration_codegen.cpp#L2924-L2928)）按名字去重，第二次插入被跳过。运行时的 `set_dependencies` 语义也是集合式的，重复边不改变等待集。

### 4.4 fan-in 与循环携带：TaskId 数组、task_dummy 与 phase fence

#### 4.4.1 概念说明

一个 TaskId 只命名一个任务。当消费者要等**一批**生产者时，需要把 TaskId 收集进数组再整体传入。这带来三个子问题：

1. **数组怎么造**：`pl.array.create(N, pl.TASK_ID)`，在 `pl.parallel` 循环里逐槽写入 `tids[branch] = tid`；
2. **循环怎么携带**：把数组作为 `pl.range(..., init_values=(tids,))` 的 iter_arg，用 `pl.yield_(tids_next)` 交出下一代——否则下一阶段读到的还是旧数组；
3. **fan-out 太宽怎么办**：\(N\) 个生产者 × \(M\) 个消费者的全连接要 \(N \times M\) 条边，中间插一个 dummy 屏障只要 \(N + M\) 条：

\[ \text{direct fanout} = N \cdot M \quad\text{vs}\quad \text{phase fence} = N + M \]

#### 4.4.2 核心流程

```text
方案一（直接 fan-out）：          方案二（phase fence）：
  tids[N] ──┬─> consumer_1          tids[N] ──┬─> task_dummy ─┬─> consumer_1
            ├─> consumer_2                     │              ├─> consumer_2
            └─> ...  M 条                      └─ N 条         └─ ...
                                              共 N + M 条
```

`ExpandManualPhaseFence`（Pass 39）会自动做有利可图的这种改写：它找「deps 里恰好一个条目且是 `Array[TASK_ID]`」的消费者，估算 \(N \cdot M\) 对 \(N + M\) 的收益，跳过 \(N \to 1\)、\(2 \to 2\) 这类无利形状，然后插入一个 `system.task_dummy` 屏障并把被覆盖的消费者改写成依赖屏障 TaskId。你也可以手写同样的形状。

#### 4.4.3 源码精读

进阶示例的编排主函数把三个子问题的答案全部展示了一遍：

[examples/utils/phase_fence_dep_compression.py:L71-L93](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/utils/phase_fence_dep_compression.py#L71-L93)

逐行拆解：

- `tids = pl.array.create(branches, pl.TASK_ID)` —— 在 manual_scope **内**造数组（这是与性能篇推荐的「外提到编排作用域」写法的差别，见下）；
- `for a_phase, (tids_a,) in pl.range(2, init_values=(tids,)):` —— 数组作为循环携带，本代的依赖源是上一代交出的数组；
- `for branch in pl.parallel(branches):` —— 并行分支各写各的槽；
- `out, tid = pl.submit(self.kernel_stripe, ..., deps=[tids_a])` —— 整个数组作为一条 deps 条目；
- `tids_next[branch] = tid` —— 把本任务 TaskId 记进下一代数组；
- `tids = pl.yield_(tids_next)` —— 交出。

手写屏障的版本在循环开头多一行 `deps = pl.system.task_dummy(deps=[tids_a])`，然后 `pl.submit(..., deps=[deps])`，见 [examples/utils/phase_fence_dep_compression.py:L131-L148](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/utils/phase_fence_dep_compression.py#L131-L148)。

两个算子的注册。`system.task_invalid` 是 `None` 字面量的落点：

[src/ir/op/sync_ops/task.cpp:L58-L69](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/sync_ops/task.cpp#L58-L69)

注释写明了它的两个出现位置（`prev_tid = None` 播种、`deps=[None]` 条目）和下游 `is_valid()` 守卫如何让它不产生边。

`system.task_dummy` 是依赖专用占位任务，`no_argument()`、类型推断恒为 `Scalar[TASK_ID]`：

[src/ir/op/sync_ops/task.cpp:L71-L80](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/sync_ops/task.cpp#L71-L80)

`pl.parallel` 的数组携带有一条硬约束，写在机制参考的专门章节里：

[docs/en/dev/language/02-manual_dependencies.md:L244-L267](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/language/02-manual_dependencies.md#L244-L267)

**`pl.parallel` 的行程数必须是 Python 字面量**（静态可知）。携带 manual 依赖的动态行程数会在代码生成期被拒绝。原因：代码生成要把携带降成 `PTO2TaskId[N]` 数组、让下游依赖**每一个**槽（而不是最后派发的那一个）——数组大小必须编译期定。

性能篇补充了一个在真实模型里更常用的变体：**把 TaskId 数组外提到 manual_scope 之外**，让 scope 之后的消费者仍能门控 scope 之内创建的任务：

[docs/en/user/performance/03-dependencies.md:L196-L212](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/03-dependencies.md#L196-L212)

并且提醒：该模型用**逐下标列表**（`deps=[down_tids[k] for k in range(...)]`）而不是整数组传递——两种拼法都存在，但在跨作用域外提的场景下逐下标形式是那个模型依赖的形态。这个细节在做综合实践时值得记一笔。

#### 4.4.4 代码实践

**实践目标**：跑通 phase fence 示例，并验证行程数必须是字面量这条约束。

**操作步骤**：

1. 直接运行 `python examples/utils/phase_fence_dep_compression.py`，观察它打印的两个程序名与函数列表；
2. 对照 [examples/utils/phase_fence_dep_compression.py:L77-L85](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/utils/phase_fence_dep_compression.py#L77-L85)，在纸上画出第一代的任务图：4 个 `kernel_stripe` 任务、4 条来自 `tids_a` 的入边、4 条写入 `tids_next` 的出边；
3. 做一个最小破坏性实验：把 `for branch in pl.parallel(branches):` 改成 `for branch in pl.range(branches):`（把并行循环换成顺序循环），再运行一次，对比两版产物的差异；
4. 再做一个约束实验：把 `branches` 换成一个运行期才确定的值（比如从环境变量读），触发「statically-known trip count」报错。

**需要观察的现象**：第 3 步换成 `pl.range` 后程序仍能跑（顺序循环是合法的），但任务图从「4 个可并行任务」退化为「4 个串行任务」——语义变了，这就是 `pl.parallel` 是**断言而非请求**的含义。第 4 步应在代码生成期得到带 "statically-known trip count" 字样的报错。

**预期结果**：你会看到「循环种类叠加在缓冲区追踪规则之上」这句话的具体含义：`pl.range` 顺序、`pl.parallel` 断言迭代互相独立，但它**不会移除**自动推导的边，只是承诺你没有制造任何要紧的边。

**待本地验证**：第 3、4 步的具体报错文案与产物差异以本地为准。

#### 4.4.5 小练习与答案

**练习 1**：`N = 8` 个生产者、`M = 8` 个消费者的场景，直接 fan-out 和 phase fence 各要多少条边？`ExpandManualPhaseFence` 会接受哪种？

**参考答案**：直接 fan-out \(8 \times 8 = 64\) 条，phase fence \(8 + 8 = 16\) 条。Pass 会选屏障（收益显著）。但注意 Pass 的拒绝清单：混合 deps、标量 deps、未解析数组、当前循环的 iter-arg 数组、循环体内定义的数组、经同存储别名更新的数组、非 manual 作用域、非编排函数——命中任一条就保持直接 fan-out。`N \to 1` 和 `2 \to 2` 这类低收益形状也会被主动跳过。

**练习 2**：为什么 `pl.parallel` 携带 TaskId 时下游要依赖**每个**槽，而不是最后一个派发的任务？

**参考答案**：`pl.parallel` 的各迭代完成顺序与派发顺序**不保证一致**——迭代 3 可能先于迭代 0 结束。「最后派发」在完成时刻的意义上没有良定义。如果只依赖一个槽，其他槽的任务可能还没写完，下游就读到了部分完成的数据。所以代码生成把携带降成 `PTO2TaskId[N]` 数组、让下游等待全部槽位，这也是行程数必须静态可知的原因（数组大小要编译期定）。

**练习 3**：`prev_tid = None` 播种之后，第一次迭代的 `deps=[prev_tid]` 会产生一条边吗？

**参考答案**：不会。`None` 降成 `system.task_invalid`，也就是 `PTO2TaskId::invalid()` 哨兵。代码生成在 `EmitDepArrayInsert` 里用 `if (name.is_valid())` 守卫（[src/codegen/orchestration/orchestration_codegen.cpp:L2904-L2915](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/orchestration/orchestration_codegen.cpp#L2904-L2915)），哨兵被跳过，运行时在首迭代看不到任何边。

### 4.5 冗余边审计：deps.json 与 deps_viewer 的两种约简模式

#### 4.5.1 概念说明

手工加完边之后，值得问一个反向问题：**哪些边本来就已经被蕴含了？** 回到 2.4 的定义——边 \((u, v)\) 冗余当且仅当 \(v\) 从 \(u\) 出发还有另一条路径可达。删掉冗余边不改变执行顺序，只减轻调度器簿记。

审计工具链是：

1. `RunConfig(enable_dep_gen=True)` 让运行时把 PTO2 依赖边捕获到 `<work_dir>/dfx_outputs/deps.json`；
2. `python -m simpler_setup.tools.deps_viewer <deps.json> --edge-mode <mode>` 做传递约简并报告删了多少条。

关键陷阱：**两种约简模式语义不同，`reduced` 单独报 0 不构成任何证据。**

#### 4.5.2 核心流程

```text
enable_dep_gen=True
   ▼
<work_dir>/dfx_outputs/deps.json     （边列表，每条带 source 标注）
   ▼
deps_viewer --edge-mode reduced            结构约简：creator 边无条件受保护
deps_viewer --edge-mode reduced_dataflow   数据流约简：creator 边有条件可删
```

边携带一个 `source` 字段。其中 **`creator` 边**的职责不是表达顺序，而是**保活**——让「拥有某个仍被消费者引用的张量」的那个任务不被回收。因为顺序不是它编码的东西，结构约简**无条件**保护它；保护按边对判定，一条 creator 标注就护住整条边。

`reduced_dataflow` 让 creator 边变得可删，但只在能**证明字节流**的时候：该边对上的每个 creator 标注都必须是**精确可知的 `INOUT` 区域**，且每个字节都可证从更早的 `Output` 流向同一 creator 拥有的更晚 `INOUT`。步长元数据含糊或过于复杂则保留边；`OUTPUT_EXISTING` 边（开启一次复用世代）也保留。

#### 4.5.3 源码精读

`enable_dep_gen` 是 `RunConfig` 的一个字段，docstring 写明了产物路径与渲染命令：

[python/pypto/runtime/runner.py:L247-L251](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/runtime/runner.py#L247-L251)

注意渲染工具的模块路径是 `simpler_setup.tools.deps_viewer`——它住在 simpler 运行时仓库（u1-l1 讲过的五仓库生态之一），不在 pypto 本仓库里。CLI 默认文本输出，`--format html` 出网页。

字段默认关闭：

[python/pypto/runtime/runner.py:L347](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/runtime/runner.py#L347)

审计方法与两个「看起来像答案但其实不是」的警示，来自本版本新增的章节：

[docs/en/user/performance/03-dependencies.md:L232-L269](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/03-dependencies.md#L232-L269)

这段是本节的核心，三条结论都值得背下来：

1. **`reduced` 报 0 是关于模式的证据，不是关于你的图的证据。** 文档给了一组实测数字：一张有 5120 条 `creator` 加 1008 条 `tensormap` 边的图上，全部 2032 个冗余边对都带 creator 标注——`reduced` 报了 `0`，`reduced_dataflow` 删掉了 992 条。
2. **深度为 1 的图根本不可能含冗余边。** 没有两跳路径就没有东西能蕴含一条边。先看深度；深度为 1 时报 0 意味着审计到此为止，而不是图已最小。
3. **环会禁用约简但不报失败。** 工具在 stderr 上告警、输出完整图、**仍然退出码 0**。要读 stderr；退出码 0 不是约简发生过的证明。

同一段还交代了审计的成本边界：它只消费 `deps.json` 一个文件，不需要任何时序产物、不需要设备。加 `--func-names` 会多读一个 `name_map*.json`，把边列表里的数字 id 换成内核名——值得加。

#### 4.5.4 代码实践

**实践目标**：导出一张真实任务图的 `deps.json`，用两种模式跑约简，亲眼看到两者数字不一致。

**操作步骤**：

1. 挑一个你已经跑通的 manual_scope 程序（可直接用 4.4 的 phase fence 示例改造成可运行的完整脚本，或用综合实践产出的程序）；
2. 把 `RunConfig` 加上 `enable_dep_gen=True`，运行一次，找到 `<work_dir>/dfx_outputs/deps.json`；
3. 依次运行两条命令：
   ```bash
   DEPS_JSON="<work_dir>/dfx_outputs/deps.json"
   python -m simpler_setup.tools.deps_viewer "$DEPS_JSON" --edge-mode reduced
   python -m simpler_setup.tools.deps_viewer "$DEPS_JSON" --edge-mode reduced_dataflow
   ```
4. 两条都加 `--func-names` 再跑一遍；
5. 检查两次运行的 **stderr**（不是 stdout），确认没有环告警；
6. 记录：图的总边数、图深度、`reduced` 删的条数、`reduced_dataflow` 删的条数。

**需要观察的现象**：两种模式报告的「删除条数」不同，通常 `reduced_dataflow` ≥ `reduced`。若两者都是 0，先看深度——深度为 1 时这个 0 什么都不说明。

**预期结果**：你手工加的 `deps=` 里那些与自动推导边重叠的部分（比如 4.3 练习 1 里那种共享缓冲区的场景）会被约简识别出来；数量差就是「creator 保护」造成的口径差。

**待本地验证**：`enable_dep_gen` 在模拟平台（`a2a3sim`）上的可用性、`deps.json` 的确切路径、以及 `simpler_setup` 包是否已安装，都需要本地确认。若 `simpler_setup` 不可用，说明运行时仓库的工具链尚未接入本地环境，此步只能做源码阅读（读 03-dependencies.md 的方法章节并写出你预期两种模式各自会删哪些边）。

#### 4.5.5 小练习与答案

**练习 1**：`reduced` 和 `reduced_dataflow` 对 creator 边的处理差别是什么？为什么结构约简不能碰 creator 边？

**参考答案**：`reduced` 无条件保护 creator 边；`reduced_dataflow` 在「每个 creator 标注都是精确可知的 INOUT 区域，且每个字节可证从更早 Output 流向同 creator 的更晚 INOUT」时才删。结构约简不能碰它，是因为 creator 边编码的不是顺序而是**保活**——它让拥有某个仍被引用张量的任务不被回收。传递约简的合法性论证是「删掉不改变可达性」，但保活不是可达性问题，这个论证对它不成立。

**练习 2**：你在 `deps.json` 上跑 `reduced` 得到 0，跑 `reduced_dataflow` 得到 0，图的深度是 1。你能下什么结论？

**参考答案**：几乎什么都下不了。深度 1 的图**结构上不可能**有冗余边（冗余需要一条两跳路径来蕴含它），所以 0 是图的形状决定的，不是边都必要决定的。正确的下一步是找一张更深的图重跑，或者人工核对边的必要性。这正是文档「先看深度；0 在那里终结的是审计而不是证明图最小」的意思。

**练习 3**：审计命令退出码是 0，你能断言约简成功执行了吗？

**参考答案**：不能。存在环时工具会在 stderr 告警、照常输出完整图、并以退出码 0 结束。退出码只反映进程正常结束，不反映约简是否发生。必须读 stderr。这类「静默降级」的退出码语义在审计工具里尤其危险，因为一个错误的「0 条冗余」会直接终结调查。

## 5. 综合实践

把本讲全部内容串成一个任务：**写一个三任务流水 + 一个并行分支，对比手动与自动两种依赖模式，最后审计冗余边**。

### 任务描述

任务图形状（A→B→C 串成流水，D 与 B 并行）：

```text
A ──▶ B ──▶ C
      │
      └──▶ D      （D 与 C 都等 B；D 与 C 互相独立）
```

### 步骤一：写 manual_scope 版本

`pl.submit` 需要的是 `self.<kernel>` 方法引用，所以要搭一个 `@pl.program` 类（这是从 `@pl.jit` 顶层函数写不出 submit 的原因）。骨架（示例代码，非项目原有）：

```python
@pl.program
class Pipeline:
    @pl.function(type=pl.FunctionType.InCore)
    def stage_a(self, x, out_a: pl.Out[pl.Tensor]) -> pl.Tensor:
        ...  # load/compute/store 三段式

    # stage_b / stage_c / stage_d 同构，省略

    @pl.function(type=pl.FunctionType.Orchestration)
    def main(self, x, out: pl.Out[pl.Tensor]) -> pl.Tensor:
        with pl.manual_scope():
            a, ta = pl.submit(self.stage_a, x)
            b, tb = pl.submit(self.stage_b, a, deps=[ta])
            c, tc = pl.submit(self.stage_c, b, deps=[tb])
            d, _  = pl.submit(self.stage_d, b, deps=[tb])
        return c
```

要点：

- 四个 TaskId 里只用到三个——`tc` 和 D 的 tid 没有下游消费者，用 `_` 丢弃即可（对照 [examples/utils/phase_fence_dep_compression.py:L83](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/utils/phase_fence_dep_compression.py#L83) 的 `out, tid = ...` 写法）；
- 期望的显式边数是 **3**（A→B、B→C、B→D）。注意如果各 stage 之间共享缓冲区，运行时还会自动加边——但 manual_scope 里自动追踪整个关了，所以区域内恰好只有你写的 3 条。

### 步骤二：数边

dump 编排产物，统计 `set_dependencies` 出现次数。每次出现对应一个带显式边的任务，所以应该是 3 次。对照 [tests/ut/codegen/test_orchestration_manual_scope.py:L1382-L1388](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_orchestration_manual_scope.py#L1382-L1388) 的断言写法核对你找到的每一行。

### 步骤三：改成 AUTO 对比

把 `with pl.manual_scope():` 整行删掉（或换成 `with pl.scope():`，但那需要 `@pl.function(auto_scope=False)`，删掉更省事），保持四个 `pl.submit` 原样。重新编译：

- 产物里 `set_dependencies` 应该**仍然出现 3 次**——`deps=` 在 auto scope 里同样工作（正交性）；
- 但运行时还会从缓冲区重叠推出额外的边（如果 stage 之间共享缓冲区，RAW/WAW 链会叠加进来）；
- 若 stage 之间用独立张量传递，自动推导可能推不出 A→B 之外的边，此时删掉 `deps=` 试试：看哪条边消失了、结果是否变成非确定的。

### 步骤四：审计冗余边

按 4.5.4 的步骤导出 `deps.json`，分别用 `reduced` 与 `reduced_dataflow` 跑一遍，回答：

1. 你手工写的 3 条边里，有几条是冗余的？
2. 两种模式的数字为什么不同？（提示：检查被保护的边是否带 creator 标注）
3. 图深度是多少？如果两种模式都报 0，这个 0 说明了什么？

### 验收标准

- manual 版本运行结果与 torch 参考实现 `allclose`；
- 能在产物里指出 3 次 `set_dependencies` 并解释每次对应的边；
- 能说出 AUTO 版本与 MANUAL 版本在边来源上的差别；
- 能解释两种约简模式数字差异的原因，或者（若工具不可用）写出基于 [docs/en/user/performance/03-dependencies.md:L232-L269](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/03-dependencies.md#L232-L269) 的预测并标注「待本地验证」。

## 6. 本讲小结

- **运行时不逐语句执行编排函数**：它构建任务图、调度就绪任务。语句顺序本身不表达顺序；边只来自缓冲区推导或你的声明。
- **`pl.submit` 是解析器构造**：脱糖为单个 `ir.Submit`，返回类型恒为 `Tuple[<内核返回>..., Scalar[TASK_ID]]`，必须解包成二元组；`deps=` 写进一等字段 `Submit::deps_`，参与 use-def 链。
- **手动与自动是正交机制**：最终等待集 = auto ∪ explicit。`deps=` 在 auto scope 里是精准修补工具，在 `manual_scope` 里是唯一边源；豁免自动追踪有三档粒度（区域 / 张量一生 / 单次调用单参数），取最窄够用的那档。
- **fan-in 靠 TaskId 数组与 dummy 屏障**：数组经 `pl.parallel` 逐槽写入、经 `pl.range(init_values=...)` 循环携带；\(N \cdot M\) 的直接 fan-out 可用 \(N + M\) 的 phase fence 替代，Pass 39 会自动改写有利可图的形状。
- **每个豁免都是编译器无法核对的断言**：区域不真 disjoint 时你不是修好了串行化，而是造出了一场在别人机器上才复现的间歇性竞态。WAR 不被自动追踪，需要时自己声明。
- **冗余边审计必须两个模式都跑**：`reduced` 无条件保护 creator 边，单独报 0 只说明模式口径；深度 1 的图结构上不可能有冗余边；环会让约简静默失效且退出码仍为 0。

## 7. 下一步学习建议

- **u7-l2（分布式编程模型）**：把任务图从单卡扩展到多 rank，会看到 `WindowBuffer` 与通信域作用域如何叠加在本讲的依赖机制之上。
- **u7-l4（性能优化实践）**：本讲的依赖审计是它的前置——泳道图与时序分析回答「图是对的之后运行时拿它做了什么」。重点读 `docs/en/user/performance/` 目录的 00-swimlane 与本讲引用的 03-dependencies 的其余章节。
- **源码延伸阅读**：
  - `src/ir/transforms/auto_derive_task_dependencies_pass.cpp` 与 [docs/en/dev/passes/38-auto_derive_task_dependencies.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/38-auto_derive_task_dependencies.md)——编译器侧的依赖分析，看它如何追踪存储根与矩形区域；
  - `src/ir/transforms/expand_manual_phase_fence_pass.cpp` 与 [docs/en/dev/passes/39-expand_manual_phase_fence.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/39-expand_manual_phase_fence.md)——屏障改写的收益估算与拒绝清单；
  - `tests/ut/codegen/test_orchestration_manual_scope.py`——三十多个用例覆盖了本讲每个构造的期望产物，是最好的「行为规格书」。
- **用户文档延伸**：`docs/en/user/tasks/` 目录（00-model、01-scopes、02-submit、03-tuning）是本讲主题更细的用户视角参考，其中 02-submit 的「fan-in through a TaskId array」小节展开讲了整数组与逐下标两种拼法的差别。
