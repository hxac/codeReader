# 二次开发：新增一个算子的全栈流程

## 1. 本讲目标

本讲是整个学习手册的毕业实战。读完后你应当能够：

1. 说出一个 PyPTO 算子从无到有要落地的**六层落点**：C++ 注册表、类型推断、Python 绑定、Python IR 封装、DSL 包装、代码生成（外加测试与文档），并知道每层各住在仓库的哪个文件里。
2. 按 `.claude/skills/add-op/` 技能给出的三阶段工作流（Phase A/B/C），独立完成一个小 Tile 算子的全栈落地。
3. 理解「跨层同步」纪律：为什么改一层必须同步其余层，注册表在 import 期就用两道自检兜底。
4. 借 `init_cond` 家族的先例，判断一个新参数应当注册为**操作数**（operand）还是 **kwarg**（属性），并能说清 keyword-only 参数在打印器里引发的连锁修整。

本讲是 u5-l8（动手写一个新 Pass）的姊妹篇：Pass 改写既有 IR，算子扩展 IR 的词汇表，两者共享同一套「C++ → 绑定 → 桩 → 测试」的工程纪律。

## 2. 前置知识

- **算子注册表**（u4-l6）：`Call` 节点的 `op_` 指向一个注册算子或 `GlobalVar` 函数；注册表以带命名空间的算子全名（`tile.sin`、`tensor.add`）为键，是进程级单例。算子身份是**名字**而非指针，比较时用 `IsOp` 而非裸字符串。
- **类型推断**（u4-l4）：`f_deduce_type` 在建 `Call` 时同步推断结果类型；Tile 类型带 `TileView`（布局、有效区、fractal）。
- **Tile 级算子**（u2-l4）：`tile.*` 算子一一映射片上指令；累加算子族（`matmul_acc`/`gemv_acc`/`batch_matmul_acc`）共享 `init_cond` 谓词，k==0 时覆写而非累加。
- **三层架构**（u1-l3）：C++ 核心（`include/` + `src/`）、nanobind 绑定（`python/bindings/`）、Python 层（`python/pypto/`）；改 C++ 必须重编。
- **测试范式**（u5-l8 / u7-l7）：pytest + 纯 assert；Pass/降级测试用 before/after 结构化相等。

不熟悉「操作数 vs kwarg」这个词没关系——这正是本讲 4.5 节要讲透的核心设计决策。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| `.claude/skills/add-op/SKILL.md` | 新增算子的官方工作流：Phase A/B/C 三阶段任务清单 |
| `.claude/skills/add-op/reference.md` | 各层代码模板、命名约定、文件落位总表（§1–§11） |
| `include/pypto/ir/op_registry.h` | 注册表核心：`OpRegistryEntry` 流式声明、`REGISTER_OP` 宏、`IsOp`、两道校验接口 |
| `src/ir/op/tile_ops/matmul.cpp` | `tile.matmul_acc` / `tile.gemv_acc` 的真实注册（本讲的活例） |
| `src/ir/op/tile_ops/batch_matmul.cpp` | `tile.batch_matmul_acc` 注册（init_cond 家族第三个成员） |
| `src/ir/op/tensor_ops/matmul.cpp` | `tensor.matmul_acc` 注册（家族第四个成员，Tensor 级） |
| `src/ir/op/tile_ops/unary.cpp` | `tile.sin` 等一元算子注册（综合实践的对照样本） |
| `src/ir/op/tile_ops/memory.cpp` | `tile.create` 注册与推断（`compact` kwarg 的活例，本版新增） |
| `src/ir/op/type_inference.cpp` | `CheckMatmulInitCond` 等共享推断/验证助手 |
| `python/pypto/ir/op/tile_ops.py` | Python IR 层封装：薄包装调 `_ir_core.create_op_call` |
| `python/pypto/language/op/tile_ops.py` | DSL 层包装：unwrap Tile → IR 调用 → 重新包装（**文档即手册**） |
| `python/pypto/language/typing/scalar.py` | `BoolLike` 别名与 `predicate_to_expr` 谓词规整 |
| `python/bindings/modules/ir.cpp` | 绑定层的 `create_op_call`（通用入口，算子无需逐个绑定） |
| `python/bindings/bindings.cpp` | import 期自检调用点 |
| `src/backend/common/pto_ops_common.cpp` / `pto_ops_elementwise.cpp` | PTO 代码生成注册：`RegisterPTOOps` 分发与 `kSimpleOps` 表 |
| `src/ir/transforms/lower_composite_ops_pass.cpp` | 复合算子路线：`tile.sin` 被查表拆成原语菜谱 |
| `src/ir/transforms/python_printer.cpp` | 打印器对 keyword-only `init_cond` 的关键字化修整 |
| `tests/ut/ir/operators/test_tile_ops.py` | 算子单元测试样板 |
| `tests/ut/codegen/test_matmul_init_cond.py` | `init_cond` 的代码生成测试（字面量折叠 vs 运行期分支） |

## 4. 核心概念与源码讲解

### 4.1 全景：六层落点与 add-op 三阶段工作流

#### 4.1.1 概念说明

在 PyPTO 里「新增一个算子」不是写一个 Python 函数那么简单——算子要能出现在 IR 里、被类型检查、被打印回 DSL、被 Pass 识别、最终变成一条芯片指令。因此一个算子有**六个落点**：

| 落点 | 位置 | 回答的问题 |
| ---- | ---- | ---- |
| ① C++ 注册表 | `src/ir/op/{tile,tensor}_ops/<分类>.cpp` | 这个算子叫什么、吃几个参数、合法吗 |
| ② 类型推断 | 同上文件的 `f_deduce_type`（或共享助手） | 结果的 shape/dtype/TileView 是什么 |
| ③ Python 绑定 | `python/bindings/modules/ir.cpp` 的 `create_op_call` | **通用入口，通常无需为新算子改动** |
| ④ Python IR 封装 | `python/pypto/ir/op/tile_ops.py` | 在 Python 里怎么构造这个 `Call` |
| ⑤ DSL 包装 | `python/pypto/language/op/tile_ops.py` | 用户在 `pl.tile.<op>(...)` 里怎么调 |
| ⑥ 代码生成 | `src/backend/common/pto_ops_*.cpp` 或复合算子菜谱 | 最终变成哪条 PTO 指令 |

外加第 0 层的**测试与文档**。项目把这套流程固化成了 `.claude/skills/add-op/` 技能，分三个阶段：

- **Phase A（必做）**：Tile 算子定义 + IR/DSL 封装 + 单测 + 文档；
- **Phase B（可选）**：Tensor 算子 + tensor→tile 降级规则 + 测试；
- **Phase C（可选）**：代码生成（编排/PTO）+ 系统测试。

#### 4.1.2 核心流程

```text
用户写 pl.tile.sin_poly(t)
        │
        ▼
⑤ DSL 包装：t.unwrap() ──────────► Tile 解包成 ir.Expr
        │
        ▼
④ IR 封装：_ir_core.create_op_call("tile.sin_poly", [expr], {}, span)
        │
        ▼
③ 绑定层 create_op_call ──► OpRegistry::CreateUserFacing
        │                       └─► ① 查注册表：名字、参数表、kwarg 校验
        ▼                       └─► ② f_deduce_type 推断结果类型
   返回带类型的 Call 节点
        │
        ▼ （编译流水线中）
⑥ LowerCompositeOps 查表拆原语，或 PTO 代码生成查 kSimpleOps
        │
        ▼
   .pto 产物中的一条/一串指令
```

关键认识：**注册是一次声明、处处消费**。内存规划读它的内存空间规格，依赖分析读它的参数副作用，代码生成读它的指令映射——所以注册表里漏声明一项，错误往往不在注册处爆，而是在很远的后端以竞态或死锁形式出现。

#### 4.1.3 源码精读

三阶段工作流与任务清单定义在技能入口文件里：

- [.claude/skills/add-op/SKILL.md:15-25](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/SKILL.md#L15-L25) — 声明 Phase A（必做）/ Phase B / Phase C 的分层工作流，并规定「动手前先问用户要哪几个阶段」。
- [.claude/skills/add-op/SKILL.md:27-37](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/SKILL.md#L27-L37) — 可勾选的任务跟踪清单：A1 C++ tile op → A2 IR 包装 → A3 DSL 包装 → A4 单测 → A5 文档。

代码生成落点在当前 HEAD 分成了按语义类别的多个文件，由一个总入口分发：

- [src/backend/common/pto_ops_common.cpp:33-44](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_common.cpp#L33-L44) — `RegisterPTOOps` 依次调 `RegisterElementwiseOps`、`RegisterMemoryOps`、`RegisterDataMoveOps` 等。注意：add-op 技能的 reference 写的是「简单算子加进 `pto_ops_common.cpp` 的 `kSimpleOps`」，但表实际已搬到 `pto_ops_elementwise.cpp`——**文档可能滞后，落点以代码为准**（这也正是本讲被标记为 update 的原因之一）。
- [src/backend/common/pto_ops_elementwise.cpp:589-597](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L589-L597) — `kSimpleOps` 静态表：每行是 `{算子名, PTO 指令, 元数}` 三元组，如 `{"tile.add", "pto.tadd", 2}`。新增一个「一对一映射单条指令」的算子只需加一行。

#### 4.1.4 代码实践

**实践目标**：在动手之前先建立「落点地图」的肌肉记忆。

**操作步骤**：

1. 打开 [.claude/skills/add-op/reference.md:477-495](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/reference.md#L477-L495) 的「File Locations by Layer」总表。
2. 对照表中每一行，用 `Grep` 在仓库里验证该文件确实存在（例如确认 `src/ir/op/tile_ops/` 下有哪些分类文件）。
3. 找出表格与当前代码的**一处不一致**（提示：kSimpleOps 的所在文件），记录下来。

**需要观察的现象**：技能文档描述的是工作流骨架（基本稳定），而具体文件路径会随重构漂移。

**预期结果**：得到一张自己验证过的六层落点清单；能说出「简单算子加一行 kSimpleOps」与「复合算子注册 LowerCompositeOps 菜谱」的区别。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Phase B（Tensor 算子 + 降级规则）是可选的，而 Phase A 是必做的？

**答案**：Phase A 定义的是 Tile 级「词汇」——没有它，编译器根本不认识这个算子，任何层面都无法表达。Phase B 只是把同一个能力暴露到 Tensor 级（算法开发者视角）并教会 `ConvertTensorToTileOps` 如何自动降级；纯 Tile 级使用的算子（如底层搬运原语）不需要这层。Phase C 同理：若算子会被 `LowerCompositeOps` 拆成既有原语（如 `tile.sin`），则不需要自己的 PTO 发射器。

**练习 2**：`create_op_call` 是通用绑定，为什么新算子通常不用改绑定层？

**答案**：绑定层暴露的是「按名字建 Call」的通用入口（见 4.4.3），算子特异性全部由注册表条目和 Python 侧的封装函数承载；只有引入新的**通用机制**（如内部算子回读入口 `_create_internal_op_call`）才需要动绑定。

### 4.2 第一层：C++ 注册表的流式声明与导入期自检

#### 4.2.1 概念说明

`OpRegistryEntry` 用**流式（fluent）API** 描述一个算子的全部元数据：名字、类别、描述、参数表、kwarg 类型、内存空间规格、参数副作用、类型推断函数。`REGISTER_OP` 宏在静态初始化期把条目写进进程级单例 `OpRegistry`。

最重要的设计是**两道导入期自检**：`import pypto` 的瞬间（nanobind 模块初始化时）会跑 `ValidateTileOps()` 和 `ValidateArgEffects()`——前者要求所有 `tile.*` 算子声明内存规格，后者要求所有原地更新参数的算子声明它对该参数做了什么。任何一条不满足，整个 import 直接失败。这意味着**你不可能注册一个「半成品」算子而不被发现**：忘写 `set_output_memory` 不是等到后端发射时才炸，而是 import 就炸。

#### 4.2.2 核心流程

```text
REGISTER_OP("tile.xxx")
  ├─ set_op_category("TileOp")            # 或 "TensorOp"
  ├─ functional_execution_memory_access() # 发射的 PTO 操作真实读写的执行内存契约
  ├─ set_description(...)                 # 必填，缺了 GetOp() 抛错
  ├─ add_argument(name, desc) × N         # 位置操作数表（含可选尾随操作数）
  ├─ set_attr<T>(key) × M                 # kwarg 声明（T 受白名单限制）
  ├─ set_input_memory(i, space) × N       # 第 i 个操作数允许的内存空间
  ├─ set_output_memory(space)             # 结果的内存空间
  ├─ set_output_reuses_input(0)           # 结果复用参数 0 的缓冲（原地算子）
  ├─ set_arg_effect(0, ArgEffect::ReadWrite)  # 副作用声明（依赖分析消费）
  └─ f_deduce_type(lambda)                # 类型推断函数
        │
        ▼  import pypto 时
  ValidateTileOps()   ── tile.* 缺内存规格 → ValueError，import 失败
  ValidateArgEffects()── 原地算子未声明副作用 → ValueError，import 失败
```

必填字段有五个（name 自动设置、description、op_category、arguments、deduce_type），由 `GetOp()` 逐项检查——见 [include/pypto/ir/op_registry.h:263-267](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L263-L267) 的注释清单。

#### 4.2.3 源码精读

- [include/pypto/ir/op_registry.h:253-294](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L253-L294) — `OpRegistryEntry` 类与 `GetOp()` 的必填字段校验：description / op_category / arguments / deduce_type 缺一即 `CHECK` 抛 `ValueError`。注册不完整的算子在第一次被使用时就会大声失败。
- [include/pypto/ir/op_registry.h:1176-1178](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1176-L1178) — `REGISTER_OP` 宏本体：利用 `__COUNTER__` 生成唯一静态变量名，在静态初始化期执行 `OpRegistry::GetInstance().Register(OpName)`。这就是「约 300 个算子在 import 前就注册完毕」的机制。
- [include/pypto/ir/op_registry.h:1005-1024](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L1005-L1024) — `ValidateArgEffects()` 的文档说明为什么副作用声明是强制的：方向推断把未声明的算子当纯消费者，写操作凭空消失，错误会在设备上以竞态/死锁形式出现，而不是编译期。
- [python/bindings/bindings.cpp:73-77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/bindings.cpp#L73-L77) — 两道自检的真实调用点：模块初始化函数末尾先 `ValidateTileOps()` 再 `ValidateArgEffects()`。你新注册的算子在这两行处接受体检。

真实注册样例（本讲贯穿的活例）——`tile.matmul_acc`：

- [src/ir/op/tile_ops/matmul.cpp:429-449](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L429-L449) — 注意四个细节：`init_cond` 用 `add_argument` 注册为**第四个可选位置操作数**（不是 `set_attr`）；`set_output_reuses_input(0)` 声明结果复用 acc 缓冲；`set_arg_effect(0, ArgEffect::ReadWrite)` 声明「既读又写」——因为 `C += A@B` 要读它累加的和；三条 `set_input_memory` 分别钉死 Acc/Left/Right 三个片上空间。

#### 4.2.4 代码实践

**实践目标**：体验导入期自检的「快失败」。

**操作步骤**：

1. 阅读 [src/ir/op/tile_ops/unary.cpp:199-209](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/unary.cpp#L199-L209)（`tile.sin` 的注册），记下它声明了哪些字段。
2. 在本地分支上复制这段注册、改名 `tile.sin_poly`，**故意删掉** `set_output_memory(MemorySpace::Vec)` 那一行，重新编译。
3. `import pypto` 观察报错。

**需要观察的现象**：import 立刻抛出 `ValueError`，错误信息以 "The following tile ops are missing a memory spec" 开头并列出你的算子名。

**预期结果**：错误在你删掉声明的**当次 import** 就出现，且消息直接告诉你该加哪个链式调用。补回该行后 import 恢复正常。（本实践需重新编译 C++，机器并行度须先 `source .claude/skills/testing/load-env.sh`；具体报错文案以本地为准——待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`set_output_reuses_input(0)` 和 `set_arg_effect(0, ArgEffect::ReadWrite)` 是不是重复声明？

**答案**：不是。前者是**内存规划**事实——结果与参数 0 共享缓冲（InitMemRef/MemoryReuse 据此别名）；后者是**依赖分析**事实——参数 0 被读也被写（任务图据此连边）。`ValidateArgEffects` 正是检查「声明了复用却没声明副作用」这种半吊子状态：见 [src/ir/op_registry.cpp:390-438](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op_registry.cpp#L390-L438) 的实现，它专门问「被复用的那个参数声明了效果吗」。

**练习 2**：为什么 `REGISTER_OP` 用静态变量而不是让用户在 main 里手动调 Register？

**答案**：算子注册分散在几十个 `.cpp` 里，静态初始化让「定义即注册」，无需集中清单；代价是注册顺序不可控——所以条目之间不能有注册顺序依赖，跨算子的共享逻辑放在首次使用时惰性建立（如 `GetOp` 查表）。

### 4.3 类型推断层：f_deduce_type 与共享验证助手

#### 4.3.1 概念说明

`f_deduce_type` 是注册时挂上的推断函数：输入操作数表达式列表和 kwargs，输出结果 `TypePtr`。它不只是「算 shape」——它同时是**用户输入的第一道防线**：非法 shape、非法 dtype、非法谓词都在这里被 `CHECK` 拦下，报错带上算子名和实际收到的值。

为避免 300 个算子各写一遍相同校验，同类推断逻辑抽成**共享助手**（如 `DeduceTileOpElementwiseUnaryType`、`DeduceTileFP32OnlyType`），文件内匿名命名空间存放，跨文件共享的放进 `src/ir/op/type_inference.cpp` 并在 `include/pypto/ir/type_inference.h` 声明。`init_cond` 的校验就是这样一个跨文件助手：`CheckMatmulInitCond` 被三个文件的推断函数复用。

#### 4.3.2 核心流程

以 `tile.gemv_acc(acc, lhs, rhs, init_cond?)` 为例：

```text
f_deduce_type(args, kwargs)
  ├─ ValidateGemvAccPhase(kwargs)              # acc_phase kwarg 合法性
  ├─ CHECK(args.size() == 3 || 4)              # 操作数个数（init_cond 可选）
  ├─ CheckMatmulInitCond(args, 3)              # 第 4 操作数须为 BOOL 标量
  ├─ DeduceTileMatMulType({args[1], args[2]})  # 复用 matmul 的几何推断
  ├─ ValidateGemvInputDtypes(...)              # GEMV 特有的 dtype 组合检查
  ├─ BuildGemvResultType(...)                  # 构造 [1, N] 结果类型
  └─ 逐维 CHECK acc 与期望形状/dtype/valid_shape 一致
```

推断结果直接挂在 `Call` 节点上，后续所有 Pass 都信任它——所以这里的 CHECK 用的是用户错误级别的 `CHECK`（抛 `pypto::ValueError`），而不是 `INTERNAL_CHECK`。

#### 4.3.3 源码精读

- [src/ir/op/type_inference.cpp:459-469](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/type_inference.cpp#L459-L469) — `CheckMatmulInitCond`：若第 `index` 个操作数存在，必须是 `ScalarType` 且 dtype 为 `BOOL`；错误信息甚至教用户怎么改——"Write a comparison such as `k == 0` rather than passing the index itself"（直接传索引本身会被拒，必须传比较表达式）。
- [src/ir/op/tile_ops/matmul.cpp:347-387](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L347-L387) — `DeduceTileGemvAccType` 完整实现：第 351-354 行先做个数检查并调 `CheckMatmulInitCond(args, 3, ...)`；随后复用 `DeduceTileMatMulType` 推断乘积几何，最后逐维校验 acc 的物理形状、dtype 与 valid_shape。**这是「可选尾随操作数」的标准写法**：`CHECK(args.size() == 3 || args.size() == 4)` 加一个带越界保护的助手调用。
- [src/ir/op/tile_ops/matmul.cpp:124-130](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L124-L130) — `DeduceTileMatMulAccType` 用同样的三行模式接受 3 或 4 个操作数。`matmul_acc` 与 `gemv_acc` 的推断共享同一个谓词校验助手，这正是 init_cond 家族在推断层的一致性来源。

模板参考（新增算子时照抄的骨架）：

- [.claude/skills/add-op/reference.md:20-43](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/reference.md#L20-L43) — `REGISTER_OP` 完整模板，含 `.functional_execution_memory_access()` 与 `f_deduce_type` lambda 的标准形状。
- [.claude/skills/add-op/reference.md:55-62](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/reference.md#L55-L62) — 共享推断助手清单：一元/二元逐元素、matmul 形状规则各有现成函数，新算子应优先复用而不是重写。

#### 4.3.4 代码实践

**实践目标**：体会「推断即校验」——错误信息如何引导用户。

**操作步骤**：

1. 在本地写一个最小 DSL 算子，用 `pl.tile.matmul_acc(acc, a, b, init_cond=k0)`（把循环变量 `k0` **本身**而不是 `k0 == 0` 传给 init_cond）。
2. 运行并捕获异常。
3. 对照 [src/ir/op/type_inference.cpp:459-469](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/type_inference.cpp#L459-L469)，指出是哪一行 CHECK 拒绝了你、报错里为什么还打印出实际 dtype。

**需要观察的现象**：异常消息包含算子名、期望（BOOL）、实际收到的 dtype，以及那句「写比较表达式而非索引」的指引。

**预期结果**：`ValueError`，消息形如 "The operator tile.matmul_acc requires init_cond to have dtype BOOL, but got INDEX..."。若本地行为不一致，以待本地验证为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CheckMatmulInitCond(args, 3, ...)` 的第一行是 `if (args.size() <= index) return;`？

**答案**：`init_cond` 是**可选**操作数——只有调用方真的传了第 4 个参数才需要校验。这行越界保护让同一个助手既能服务 3 参数调用又能服务 4 参数调用，是「可选尾随操作数」模式的固定组成部分。

**练习 2**：推断函数里的检查应该用 `CHECK` 还是 `INTERNAL_CHECK`？

**答案**：`CHECK`。推断函数直接消费用户构造的表达式，非法输入是**用户错误**（抛 `pypto::ValueError`，消息面向用户）；`INTERNAL_CHECK` 留给「流水线早前 Pass 已保证却仍被违反」的编译器内部 bug。这与项目错误检查规范（CHECK = user error）一致。

### 4.4 Python 双层封装：IR 封装、DSL 包装与绑定入口

#### 4.4.1 概念说明

C++ 注册之后，Python 侧有**两层**薄封装，职责严格分开：

- **IR 层**（`python/pypto/ir/op/tile_ops.py`）：函数签名面向 IR 表达式（`Expr`），做的事只有「规整参数 → `_ir_core.create_op_call(名字, args, kwargs, span)`」。它面向编译器内部与高级用户。
- **DSL 层**（`python/pypto/language/op/tile_ops.py`）：函数签名面向 DSL 对象（`Tile`），做「unwrap → 调 IR 层 → 重新 wrap」。它面向算法开发者，**它的 docstring 就是发布的产品手册**——`docs/en/user/api/tile.md` 直接渲染它，且 `tests/lint/check_op_docstring_parity.py` 会强制 DSL 文档不得比 IR 文档更简略。

绑定层（`python/bindings/modules/ir.cpp`）提供通用的 `create_op_call`，因此这两层都是纯 Python——改它们**不需要重编 C++**；反之改注册/推断必须重编。

#### 4.4.2 核心流程

```text
DSL 层                          IR 层                       C++
def matmul_acc(acc, lhs, rhs,   def matmul_acc(acc, lhs,    OpRegistry::
        init_cond=None):                rhs, *, init_cond):    CreateUserFacing
  expr = predicate_to_expr(       args = 3 个或 4 个           ├─ 查表/门禁
      init_cond)                  (init_cond 追加在尾)         ├─ kwargs 校验
  call = _ir_ops.matmul_acc(      return create_op_call(       ├─ f_deduce_type
      acc.unwrap(), lhs.unwrap(),     "tile.matmul_acc",       └─ 内存回填
      rhs.unwrap(),                   args, {}, span)
      init_cond=expr)
  return Tile(expr=call)
```

两个规整点值得注意：DSL 层用 `BoolLike`（`bool | Scalar | Expr`）接受谓词并用 `predicate_to_expr` 统一成 `Expr`；IR 层把可选的 `init_cond` **物理上追加到 args 列表尾部**——IR 是纯位置数据，`None` 就不追加，这正对应 C++ 侧「3 个或 4 个操作数」的推断分支。

#### 4.4.3 源码精读

- [python/bindings/modules/ir.cpp:743-760](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp#L743-L760) — 绑定层两个 `create_op_call` 重载：三参版（兼容）与四参版（带 kwargs 字典，顺序保持）。它们统一走 `CreateUserFacing`——这是所有用户可见算子构造的唯一入口。
- [python/bindings/modules/ir.cpp:762-783](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp#L762-L783) — 本版新增的 `_create_internal_op_call`：走 `CreateInternal`，能到达标记 `internal_only` 的算子（`CreateUserFacing` 按设计拒绝）。这是「什么时候才需要动绑定层」的活例——不是为了某个新算子，而是为了**新通用机制**（打印→再解析往返需要重建 Pass 42 降级出的内部派发，而没有任何 DSL 包装能拼出那个名字）。注意它带下划线前缀且注释明确声明「NOT a public API」。
- [python/pypto/ir/op/tile_ops.py:1824-1856](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L1824-L1856) — IR 层 `matmul_acc`：第 1855 行 `args = [acc, lhs, rhs] if init_cond is None else [acc, lhs, rhs, init_cond]`，然后一行 `create_op_call`。docstring 完整解释了 split-K `k == 0` 惯用法与「保持累加器单定义、避免 phi」的动机——这些内容**必须**同步带到 DSL 层。
- [python/pypto/ir/op/tile_ops.py:2002-2035](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L2002-L2035) — IR 层 `gemv_acc`：签名是 `(acc, lhs, rhs, span, *, acc_phase="unspecified", init_cond=None)`。注意 `acc_phase` 是 **kwarg**（进 kwargs 字典），`init_cond` 是**可选操作数**（进 args 列表）——同一个函数里两种参数机制的对照样本。
- [python/pypto/language/op/tile_ops.py:1449-1496](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1449-L1496) — DSL 层 `gemv_acc`：签名 `(acc, lhs, rhs, acc_phase="unspecified", *, init_cond=None)`。docstring 里带可运行的 split-K 代码示例，并明确写出设计理由：「`init_cond` is keyword-only because `acc_phase` already owns the fourth positional slot」（第 1476-1477 行）。这是 4.5 节讨论的原文依据。
- [python/pypto/language/typing/scalar.py:325-349](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/typing/scalar.py#L325-L349) — `BoolLike` 别名与 `predicate_to_expr`：Python `bool` 变成 `ConstInt(.., BOOL)`（编译期常量，可被折叠），`Scalar` 解包为符号表达式（保持运行期值）。一字之差决定代码生成走「选边」还是「分支」。
- [.claude/skills/add-op/reference.md:150-176](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/reference.md#L150-L176) — DSL 模板与文档平价规则：docstring 必须写成**源码字面量**（不能运行时赋 `__doc__`，否则 mkdocstrings 渲染为空），且内容不得比 IR 版本简略。

#### 4.4.4 代码实践

**实践目标**：完成「打印 → 再解析」往返，验证两层封装的一致性。

**操作步骤**：

1. 写一个 InCore 函数，在 K 维两段循环里用 `pl.tile.gemv_acc(acc, a, b, init_cond=(k0 == 0))`（可参照 [tests/ut/codegen/test_matmul_init_cond.py:378-397](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L369-L397) 的写法，那里展示了 `pl.gemv_acc(acc, a, b, acc_phase="partial", init_cond=(k0 == 0))`）。
2. `python_print` 打印该函数的 IR。
3. 检查打印文本里 `init_cond=` 是否以**关键字形式**出现（而非第 4 个位置参数）。
4. 把打印文本重新 `pl.parse`，用 `ir.assert_structural_equal` 比较与原 IR 是否结构相等。

**需要观察的现象**：打印文本中出现 `init_cond=(k0 == 0)` 关键字实参；`acc_phase` 同样以关键字出现；往返后结构相等断言通过。

**预期结果**：往返一致。原因在 4.5.3 的打印器修整——`init_cond` 若按位置打印会错误地绑定到 `acc_phase` 槽位。

#### 4.4.5 小练习与答案

**练习 1**：IR 层封装为什么要提供 `span: Span | None = None` 参数？

**答案**：统一走 `_get_span_or_capture(span)`——调用方显式给 span 就用之，否则从调用点的 Python 栈帧自动捕获源位置。这让手工构造的 IR 也能携带调试信息，错误能定位回作者源码行列（u5-l1 讲过的 span 诊断依赖它）。

**练习 2**：如果把 DSL 层 `gemv_acc` 的 `init_cond` 从 keyword-only 改成第 5 个位置参数，会破坏什么？

**答案**：会破坏与打印器的契约。打印器对 `gemv_acc` 的 4 操作数形式按 `init_cond=` 关键字打印（见 4.5.3）；若 DSL 接受第 5 个位置参数，用户按位置传参的旧代码语义改变，且打印→再解析的往返文本不再匹配 DSL 签名。位置槽已被 `acc_phase` 占据是**既成事实的 ABI**，只能保持 keyword-only。

### 4.5 签名设计决策：操作数 vs kwarg（init_cond 家族活例）

#### 4.5.1 概念说明

新增算子参数时的第一个设计决策：注册为**操作数**（`add_argument`，进 `args_`）还是 **kwarg**（`set_attr<T>`，进 kwargs）？判据一句话：

> **值会随运行/循环变化的放操作数，编译期定死的放 kwarg。**

深层原因是 IR 的使用-定义链（use-def）：操作数是 SSA 值，参与依赖分析、公共子表达式消除、替换与 DCE；kwarg 是编译期元数据，类型受白名单限制（`DataType`、`bool`、`MemorySpace` 等），不进任何数据流。一个引用循环变量的谓词（`k0 == 0`）**必须**让编译器看见它依赖 `k0`——只有操作数能做到。

但「编译期定死」只是 kwarg 判据的一半。本版（ec5d20c）新增的 `tile.create` 的 `compact` 旗标补上了另一半：**kwarg 是「推断函数每次重推时都要重读的输入」**。Pass 在改写 IR 后常常会重新对 `Call` 跑一遍 `f_deduce_type`（`InferTileMemorySpace` 就会），如果某个影响结果类型的元数据只被塞进类型节点而不进 kwargs，重推一次就被冲掉；kwarg 则随节点永久携带、每次重推都被重新消费。也就是说两种机制真正的分野是：

- **运行期有值、要进数据流** → 操作数（`init_cond`：`k0 == 0`）；
- **编译期常量、但要被推断反复重读** → kwarg（`compact`：声明 L0C 缓冲的分形行距）。

`init_cond` 家族是这条判据的活教材：两次 HEAD 之间的 PR #2528/#2529 把它带进了 `gemv_acc` 并让 `matmul_acc` 接受自己已在累加的操作数，家族现在有**四个成员**——`tile.matmul_acc`、`tile.gemv_acc`、`tile.batch_matmul_acc`、`tensor.matmul_acc`。

#### 4.5.2 核心流程

```text
设计一个新参数 P：
  P 的值依赖循环变量/运行时数据吗？ ──是──► 操作数（add_argument）
  │                                     │  • 进 args_，参与 use-def
  │                                     │  • f_deduce_type 里校验它
  │                                     │  • 打印时按 DSL 签名的槽位决定
  │                                     │    位置 or keyword-only
  └─否（编译期常量）──► 再问：P 影响结果类型、会被重推冲掉吗？
      ├─ 否（纯提示/旗标）──► kwarg（set_attr<T>），打完就完
      └─ 是（塑造 TileView 等类型细节）──► kwarg，且 f_deduce_type
                                            每次都从 kwargs 重读它
```

对比同一家族里的两种选择：

| 参数 | 机制 | 理由 |
| ---- | ---- | ---- |
| `init_cond` | 可选第 4 **操作数** | 值是 `k0 == 0` 这类循环相关表达式，须进 use-def 链 |
| `acc_phase`（`"partial"`/`"final"`） | **kwarg**（`set_attr<std::string>`） | 编译期提示，无运行时值 |
| `a_trans`/`b_trans`（tensor.matmul_acc） | **kwarg**（`set_attr<bool>`） | 布局旗标，编译期定死 |
| `compact`（tile.create，本版新增） | **kwarg**（`set_attr<bool>`） | 编译期常量，但它塑造结果的 TileView；走 kwarg 才能在 Pass 重推类型时被重新读到 |

#### 4.5.3 源码精读

四成员的操作数注册（`add_argument("init_cond", ...)`）：

- [src/ir/op/tile_ops/matmul.cpp:429-438](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L429-L438) — `tile.matmul_acc`：`init_cond` 是第 4 个 `add_argument`，描述写明「谓词成立处覆写而非累加（split-K 的 `k == 0` 步）」。
- [src/ir/op/tile_ops/matmul.cpp:482-491](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L482-L494) — `tile.gemv_acc`：同样的第 4 操作数声明（第 489-491 行）；同链上还有 `set_attr<std::string>("acc_phase")`——**一条注册链里同时出现两种机制**，直接对照。
- [src/ir/op/tile_ops/batch_matmul.cpp:303-306](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/batch_matmul.cpp#L303-L306) — `tile.batch_matmul_acc` 的 `init_cond` 描述更进一步：说明谓词会被 `FlattenTileNdTo2D` **逐字透传**给每个 2D unroll 出的 `tile.matmul_acc`（u5-l6 讲过的行为）。
- [src/ir/op/tensor_ops/matmul.cpp:310-317](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tensor_ops/matmul.cpp#L310-L317) — `tensor.matmul_acc`：Tensor 级同款第 4 操作数，同链上 `a_trans`/`b_trans` 走 kwarg——跨层保持同一设计判断。

而「kwarg 因被推断重读而胜出」的反面活例，是本版给 `tile.create` 新增的 `compact` 旗标：

- [src/ir/op/tile_ops/memory.cpp:591-598](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L591-L598) — 推断函数内的注释逐字给出选 kwarg 的理由：「在创建时声明而不是事后盖章，正是该模式能存活的原因——Pass 盖的类型细化在任何 Pass 重新推断该调用时都会被丢弃（`InferTileMemorySpace` 就会重推），而 kwarg 会被重读」。
- [src/ir/op/tile_ops/memory.cpp:618-624](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L618-L624) — 推断里的快失败校验：`compact=true` 只允许 `target_memory=Acc`（L0C），并解释了为什么——Left/Right 空间的分形行距由填充它们的 `tile.extract` 决定，只有累加器的行距才由有效行数推出。
- [src/ir/op/tile_ops/memory.cpp:639-641](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L639-L641) 与 [src/ir/op/tile_ops/memory.cpp:1068-1077](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L1068-L1077) — 推断把 kwarg 翻译成结果类型上的 `tile_view.compact = CompactMode::normal`；注册链上是 `.set_attr<bool>("compact")`。
- [python/pypto/language/op/tile_ops.py:296-339](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L296-L339) — DSL 层 `create` 的同款 keyword-only 参数，docstring 明说「Compiler-internal. Kernels do not set this」——IR 层、DSL 层、C++ 注册三处描述保持同一口径，又是一例跨层文档同步。

keyword-only 的**原因**写在打印器里（这是最容易被忽视的连锁后果）：

- [src/ir/transforms/python_printer.cpp:1243-1256](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1243-L1256) — 注释逐字解释：`init_cond` 在 `tensor.matmul_acc`（第 4 位置槽被 `a_trans` 占有）和 `tile.gemv_acc`（第 4 槽被 `acc_phase` 占有）的 DSL 签名里是 keyword-only，因为按位置打印会错绑到别的参数；而 `tile.matmul_acc` 的第 4 槽就是它自己，无需修整。
- [src/ir/transforms/python_printer.cpp:1258-1291](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1258-L1291) — 实现：位置循环里 `if (acc_kw_init_cond && i == 3) continue;` 跳过该操作数，随后在第 1288-1290 行以 `, init_cond=` 关键字补打。**IR 是纯位置数据、DSL 签名带关键字**，二者错位时打印器必须做这种关键字化修整，否则往返解析会把谓词吃成转置旗标。

机制差异的最终消费端在代码生成测试里看得最清楚：

- [tests/ut/codegen/test_matmul_init_cond.py:10-23](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L11-L30) — 模块 docstring 点明设计：字面量 `True`/`False` 在编译期折叠成单一形态；运行期谓词降级为**双臂分支且累加器上无 phi**。若 `init_cond` 当初做成 kwarg（只能存编译期常量），运行期谓词这个能力根本无从表达——这就是操作数选择的根本回报。docstring 还解释了 GEMV 为何与 matmul 同文件同机制（同一个 cube MAD 上的同一谓词位）。
- [tests/ut/codegen/test_matmul_init_cond.py:156-196](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L156-L196) — matmul_acc 的四个基础用例：无谓词只发累加形态、字面量 True 折叠为覆写形态、字面量 False 折叠为累加形态、运行期谓词分支两形态；第 196 行起还有非 BOOL 谓词被拒的负例。
- [tests/ut/codegen/test_matmul_init_cond.py:369-397](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L369-L397) 与 [tests/ut/codegen/test_matmul_init_cond.py:399-448](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L399-L448) — 本版为 `gemv_acc` 补齐的**平行测试组**：与 matmul 版一一对应的五个用例（无谓词/True/False/运行期分支/非 BOOL 拒绝）。`GemvAccSplitK` 程序顺带展示了 `[16, N]` 物理缓冲加 `set_validshape` 收窄到 `[1, N]` 的 GEMV 累加器惯例，并断言 `acc_phase` 与谓词共存。**给既有算子加参数时，把既有测试组平行复制一份**是这个仓库的测试纪律。
- [src/backend/common/pto_ops_elementwise.cpp:966-982](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L966-L982) — 代码生成端的共享发射器 `make_acc_codegen`：注释说明谓词只是「MAD 的 Xt 寄存器一个位」，但 PTO 层只有累加/不累加两条指令，所以运行期谓词发成分支。本版把原来的 `supports_init_cond` 模板参数**删掉了**——`gemv_acc` 获得谓词后，两个算子的参数个数检查统一为「3 或 4」（第 976-978 行）。这行 `INTERNAL_CHECK` 必须跟着注册表的推断同步改，否则新参数会在代码生成处被当成编译器 bug 拦下。第 [1108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L1108) 行与 [1112](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L1112) 行的两处注册现在共用同一个发射器。

**跨层同步的实物证据**：`gemv_acc` 加 `init_cond` 这一个参数（PR #2528）在仓库里留下的落点，恰好就是本讲 4.1 的六层清单：

| 层 | 文件与行 | 改动 |
| ---- | ---- | ---- |
| ① 注册 | [matmul.cpp:489-491](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L489-L491) | 追加第 4 个 `add_argument("init_cond", ...)` |
| ② 推断 | [matmul.cpp:348-352](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L348-L352) | `CHECK(args.size() == 3 \|\| 4)` 加 `CheckMatmulInitCond(args, 3, ...)` |
| ④ IR 封装 | [ir/op/tile_ops.py:2002-2035](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L2002-L2035) | 加 keyword-only `init_cond`，非 None 时追加进 args 列表 |
| ⑤ DSL 包装 | [language/op/tile_ops.py:1449-1500](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1449-L1500) | keyword-only 参数加 split-K docstring 示例 |
| 打印器 | [python_printer.cpp:1243-1256](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1243-L1256) | `acc_kw_init_cond` 条件扩到 `tile.gemv_acc` |
| ⑥ 代码生成 | [pto_ops_elementwise.cpp:974-979](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L974-L979) | 删 `supports_init_cond`，个数检查放宽到 4 |
| ⓪ 测试 | [test_matmul_init_cond.py:399-448](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L399-L448) | 平行复制五个用例 |

一个参数、六处文件、零处可漏——「跨层同步」纪律的具体形状就是这张表。

#### 4.5.4 代码实践

**实践目标**：把判据用到自己的算子上——为一个「带预热步的算子」设计签名。

**操作步骤**：

1. 假设你要新增 `tile.sin_poly(tile, degree)`，其中 `degree` 是多项式阶数（int，编译期选定）。
2. 再假设变体 `tile.sin_poly_warm(tile, warm)`，`warm` 是「当前是否循环首步」的谓词。
3. 对两个参数分别决定操作数 or kwarg，写下理由。
4. 对照 [src/ir/op/tile_ops/matmul.cpp:482-495](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L482-L495)（`gemv_acc` 同时含 `acc_phase` kwarg 与 `init_cond` 操作数）检验你的答案。

**需要观察的现象**：`degree` 是纯编译期常量——kwarg，且 `int` 恰好在类型白名单内（[include/pypto/ir/op_registry.h:441-445](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/op_registry.h#L441-L445) 写明只允许 `bool, int, std::string, double, DataType, MemorySpace`，越界类型如 `float`、`vector` 在编译期被 `static_assert` 拒绝）；`warm` 依赖循环变量——操作数，且推断里调 `CheckMatmulInitCond` 同款校验。

**预期结果**：能说出「`warm` 若做成 kwarg，编译器看不见它依赖 `k0`，use-def 缺边、CSE 可能错删、打印也无法往返」这一整条后果链。

#### 4.5.5 小练习与答案

**练习 1**：`tile.matmul_acc` 的 `init_cond` 按位置打印没问题，`tile.gemv_acc` 却要关键字打印。同一个参数名为何待遇不同？

**答案**：打印的是 **DSL 签名**不是 IR。`gemv_acc` 的 DSL 第 4 位置槽属于 `acc_phase`，`init_cond` 是 keyword-only；IR 侧两者都存第 4 操作数。打印器必须按 DSL 签名回写，否则再解析时谓词会绑到 `acc_phase`。见 [src/ir/transforms/python_printer.cpp:1247-1252](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1243-L1256) 的原注释（`tensor.matmul_acc` 与 `tile.gemv_acc` 两个 keyword-only 签名各占一条）。

**练习 2**：如果 `init_cond` 当初注册成 `set_attr<bool>("init_cond")`，会发生什么？

**答案**：三件事立刻坏掉：(1) 只能存编译期 bool，`k0 == 0` 这类表达式无处安放——split-K 首步剥除能力消失；(2) use-def 链上没有它，依赖分析看不见算子读 `k0`；(3) kwarg 走 `ValidateKwargs` 类型校验而非 `CheckMatmulInitCond` 的表达式校验，错误信息也无法指导用户写比较表达式。

**练习 3**：`predicate_to_expr` 为什么把 Python `bool` 转成 `ConstInt(.., BOOL)` 而不是直接存 `std::any` 里的 bool？

**答案**：因为参数机制已经选定为操作数——操作数必须是 `Expr`。把字面量包装成 `ConstInt` 后，它成为普通 SSA 值：代码生成端看到常量就在编译期折叠选边（测试 `test_literal_true_folds_to_the_non_accumulating_form`），看到符号表达式就发双臂分支。同一条管线自然处理两种情形，无需 kwarg 旁路。

**练习 4**：`compact` 是编译期常量，`init_cond` 里那种「值随循环变」的理由对它不成立——那它为什么不干脆由 `AutoTileMatmulL0` 在生成累加器种子后直接改写类型节点（`tile_view.compact = ...`），而要走 `tile.create` 的 kwarg？

**答案**：因为类型改写活不过下一次重推。Pass 改写 IR 后常会对 `Call` 重新跑 `f_deduce_type`（`InferTileMemorySpace` 就会），重推用注册表里可见的信息从零重建结果类型，任何「事后盖章」的字段都会在这一刻丢失；而 kwarg 随节点持久携带、每次重推都被推断重新消费（[src/ir/op/tile_ops/memory.cpp:594-598](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L594-L598) 的注释原话）。所以判据的完整版是：要进数据流的放操作数；推断每次都要重读的元数据放 kwarg。

## 5. 综合实践

**毕业任务：全栈新增 `tile.sin_poly` —— 一个多项式近似的正弦算子。**

> 注意：仓库里**已有** `tile.sin`（[src/ir/op/tile_ops/unary.cpp:199-209](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/unary.cpp#L199-L209)，FP32-only，由 `LowerCompositeOps` 查表拆成 Cody-Waite + Horner 原语菜谱）。因此你的新算子必须换名（本任务用 `tile.sin_poly`），否则 `REGISTER_OP` 与既有注册冲突。

按 Phase A → C 完整走一遍（机器资源限制：先 `source .claude/skills/testing/load-env.sh`，构建/测试均显式传 `$PYPTO_BUILD_JOBS` / `$PYPTO_TEST_JOBS`）：

1. **A1 C++ 注册**：在 `src/ir/op/tile_ops/unary.cpp` 追加 `REGISTER_OP("tile.sin_poly")`，照 `tile.sin` 的链：`set_op_category("TileOp")` → `functional_execution_memory_access()` → 描述 → `add_argument("tile", ...)` → `set_input_memory(0, MemorySpace::Vec)` → `set_output_memory(MemorySpace::Vec)` → `f_deduce_type` 里复用 `DeduceTileFP32OnlyType`（保持 FP32-only 语义，多项式系数才有统一定义域）。重新编译；import 成功即通过两道自检。
2. **A2 IR 封装**：在 `python/pypto/ir/op/tile_ops.py` 加 `def sin_poly(tile: Expr, span: Span | None = None) -> Call`，一行 `create_op_call("tile.sin_poly", [tile], {}, actual_span)`，Google 风格 docstring。
3. **A3 DSL 包装**：在 `python/pypto/language/op/tile_ops.py` 加 `def sin_poly(tile: Tile) -> Tile`（unwrap → 调 IR → wrap 回 `Tile`）。**docstring 不得比 A2 简略**——写明定义域限制与需要先 `pl.cast` 到 FP32，跑 `tests/lint/check_op_docstring_parity.py` 验证。
4. **A4 单测**：在 `tests/ut/ir/operators/test_tile_ops.py` 仿照 [test_tile_sin_creates_call](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/operators/test_tile_ops.py#L550-L563)：构造 FP32 Tile 变量 → `tile.sin_poly` → 断言 `Call`、算子名、结果类型；再加一个 FP16 被拒的负例。
5. **C2 代码生成**：`tile.sin_poly` 不对应单条 PTO 指令，走**复合算子路线**——在 `src/ir/transforms/lower_composite_ops_pass.cpp` 的规则表（[第 2209-2210 行](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L2209-L2210) 已登记 `tile.sin`/`tile.cos`）旁注册你自己的菜谱：用 `tile.mul`/`tile.add`/`tile.mul` 标量变体拼 Horner 多项式（7 阶奇多项式在 |x|≤π 内误差可小于 1e-6；区间归约可先省略，注明定义域限制）。
6. **端到端验证**：写一个 `@pl.jit` InCore 算子：`pl.load` FP32 张量 → `pl.tile.sin_poly` → `pl.store`，与 `torch.sin` 对照 `torch.allclose`（容差按你的阶数定，建议 1e-5）。运行后 dump IR 确认 `LowerCompositeOps` 之后 `tile.sin_poly` 已消失、只剩原语链。
7. **签名设计问答**（对应本讲 4.5）：假如你的算子要支持「循环首步直接覆写、后续步累加」的变体 `sin_poly_acc(acc, tile, warm)`——`warm` 应注册成什么？写出三行理由（提示：`warm` 依赖循环变量；对照 `gemv_acc` 的 `init_cond` 是 keyword-only 操作数，而你的新算子第 3 位置槽若是空的，`warm` 可以直接占位置槽、连打印器修整都不需要）。再问一个反面：假如你的算子有一种「输出按 N-fractal 行距打包」的编译期布局模式（对照 `tile.create` 的 `compact`，见 [src/ir/op/tile_ops/memory.cpp:591-598](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L591-L598)），它该进操作数还是 kwarg？答案应是 kwarg——编译期常量且塑造结果类型，推断每次重推都要重读它。
8. **文档**：在 `docs/en/dev/ir/05-operators.md` 与 `docs/zh/dev/ir/05-operators.md` 的 TileOp 表各加一行（en 为权威，zh 同步）。

验收标准：`pytest tests/ut/ir/operators/test_tile_ops.py -v -k sin_poly` 通过；DSL 端到端与 torch 对照通过；`pre-commit run --all-files` 通过。若本地无编译环境，步骤 1/5/6 标注「待本地验证」，但步骤 2/3/4/7 的代码与文档必须完整写出。

## 6. 本讲小结

- 一个算子有六个落点：C++ 注册表、类型推断、（通用）绑定、Python IR 封装、DSL 包装、代码生成；add-op 技能把它们组织成 Phase A（必做）/B/C（可选）三阶段，`reference.md` 提供每层模板与文件落位总表。
- 注册用 `REGISTER_OP` 流式链；`import pypto` 时 `ValidateTileOps` + `ValidateArgEffects` 两道自检让「半成品算子」当场失败——内存规格与副作用声明不是可选项。
- `f_deduce_type` 同时承担 shape 推断与用户输入校验；可选尾随操作数的标准写法是「`CHECK(args.size() == N || N+1)` + 带越界保护的校验助手」，`CheckMatmulInitCond` 是跨三个文件复用的样板。
- Python 两层封装职责分明：IR 层面向表达式，DSL 层 unwrap/wrap 面向用户；DSL docstring 是发布的产品手册，受平价 lint 约束，必须写成源码字面量。
- 操作数 vs kwarg 的判据完整版是两条：值随运行/循环变化、要进 use-def 链的放操作数（`init_cond` 引用 `k0 == 0`，家族现有四个成员）；编译期定死、但要被推断每次重推时重读的元数据放 kwarg（`tile.create` 的 `compact`——事后盖章的类型改写活不过一次重推）。keyword-only 与否取决于 DSL 位置槽被谁占有，打印器会据此做关键字化修整保住往返一致。
- 文档会滞后于代码（kSimpleOps 已搬进 `pto_ops_elementwise.cpp` 而技能文档仍写 `pto_ops_common.cpp`）——落点永远以代码为准。

## 7. 下一步学习建议

至此学习手册的主线完结。三个方向的后续深挖：

1. **Phase B 补全**：给 `tile.sin_poly` 配一个 `tensor.sin_poly` 并在 `src/ir/transforms/op_conversion_registry.cpp` 注册 `RegisterSimple`/`RegisterCustom` 降级规则，按 [tests/ut/ir/transforms/test_convert_tensor_to_tile_ops.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_convert_tensor_to_tile_ops.py) 的 before/after 范式补测试——把 u5-l5 讲的降级机制亲手用一遍。
2. **执行内存契约与管道推断**：重读 add-op 技能中 `.functional_execution_memory_access()` / `.no_execution_memory_access()` / `.f_infer_pipe(...)` 三档（[.claude/skills/add-op/SKILL.md:59-78](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.claude/skills/add-op/SKILL.md#L59-L78)），结合 u5-l7 的 DSA 复用危害识别，理解「内存空间描述值住在哪，执行内存访问描述算子是否真读写」这条边界。
3. **向上游贡献**：用 `/create-issue` 与 git-commit 技能把你的算子走完提交流程；提交前重读 `.claude/rules/cross-layer-sync.md` 与 `.claude/rules/documentation.md`，对照本讲的跨层同步清单自查一遍。
