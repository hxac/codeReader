# PTO 代码生成：从降级后的 IR 到 `.pto` 指令文本

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `PTOCodegen` 的总体架构：它如何遍历已经降级好的 IR（第 47 个 Pass 之后的形态），按「常量 → tensor_view → alloc_tile → 函数体」的固定顺序生成 MLIR 风格的 PTO-ISA 文本，以及为什么项目要求它保持**严格 1:1 映射**（不做任何分析或优化）。
2. 跟踪一条 `pl.add` 的完整发射链路：`AssignStmt` 绑定结果缓冲 → `VisitExpr_(CallPtr)` 查后端算子表 → `kSimpleOps` 表把 `tile.add` 映射成 `pto.tadd` → `GenerateInsOutsClause` 用 MemRef 解析出的 SSA 名拼出 `ins(...)/outs(...)` 子句——并理解操作数为什么「来自 MemRef」。
3. 掌握控制流与标量表达式的指令化：`ForStmt`/`IfStmt` 如何变成 `scf.for`/`scf.if`（以及 Tile 为什么**不进** iter_args/scf 结果）、标量算术如何变成 `arith.addi` 一族，和累加算子 `init_cond` 运行期谓词如何降级成「两条指令形态的 `scf.if` 分支」。
4. 理解**行收窄（compact）累加器跨 Cube→Vector 边界时 TPUSH/TPOP 的 pitch 规则**：`mad` 按 \( \lceil validRow/16 \rceil \cdot 16 \) 的 N-fractal 行距写 L0C，所以无分裂推送必须**保持生产者写下的行 pitch、只扩列**；而分裂推送对这种形状**直接拒绝**（而不是静默错编），拒绝条件由 `AccPitchesCoincide` 判定。
5. 能独立读懂一段 `.pto` 产物，并把每条指令反向对应回 IR 语句与 DSL 源码行（借助 `loc(...)` 源位置后缀）。

本讲承接 u5-l6（Tile 后端降级链）：上一讲结束时，IR 已经全是 2D 的 Tile 原语、内存空间已推断、MemRef 已由 u5-l7 的内存规划三部曲发好「地契」。本讲回答最后一跳：**这些 IR 如何变成汇编器（ptoas）能吃的 `.pto` 文本**。

## 2. 前置知识

- **PTO-ISA 与 MLIR 方言**：`.pto` 产物是 MLIR 风格文本——`pto.tadd ins(%a : !pto.tile_buf<...>, %b : ...) outs(%c : ...)`。`pto.*` 是 PTO 虚拟指令集的方言名，`scf.*`/`arith.*` 是 MLIR 标准控制流/算术方言，最终由外部工具 ptoas 汇编成设备代码（u1-l1 已建立五仓库生态图景）。
- **MemRef 是内存地契**（u5-l7）：每个 Tile 变量的 `TileType` 里挂着一个 MemRef（base 分配身份 + byte_offset + size）。代码生成不重新规划内存，只把 MemRef 翻译成 `pto.alloc_tile` 的 `addr` 属性（`memory_planner=PYPTO/DSA_RP` 模式）或省略 addr 交给 ptoas `PlanMemory`（`PTOAS` 模式）。
- **Tile 的物理盒与有效区**（u2-l4、u4-l4）：`rows/cols` 是编译期物理形状，`v_row/v_col`（valid_shape）是盒内真正有数据的子矩形、可为运行期值。本讲的 pitch 规则全部围绕「物理行数」与「有效行数」的差展开。
- **compact 模式**（u4-l4、u5-l6）：`TileView.compact` 是「仅分形空间 Left/Right/Acc 有意义的有效区打包标记」，本质是 N-fractal 行距。`mad`（矩阵乘的硬件指令）把乘积按 \( \lceil validRow/16 \rceil \cdot 16 \) 的行距摊进 L0C；读者若按物理行数取步长就会走错位。`tile.matmul` 的推断器会给行收窄的累加器打 compact 标记，`AutoTileMatmulL0` 用 `tile.create(compact=True)` 给种子声明它。
- **跨核流水（tpush/tpop）**（u5-l6 提及、u6-l4 将展开）：Cube（AIC）与 Vector（AIV）核通过 GM 上的 FIFO 槽传数据，`tile.tpush_to_aiv` 推、`tile.tpop_from_aic` 弹，`split` 属性决定数据是否按行/列分给两个消费者 lane。
- **CodeEmitter 与 CodegenBase**：`CodeEmitter`（`src/codegen/code_emitter.cpp`）是纯文本输出助手——缩进栈 + 字符串缓冲；`CodegenBase` 是所有代码生成器的公共虚基类，`PTOCodegen` 继承它并实现 `Emit`/`GetExprAsCode`/`GetCurrentResultTarget` 等接口。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [src/codegen/pto/pto_codegen.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp) | 主文件：`PTOCodegen` 访问器、`MemRefCollectorVisitor` 收集器、`Generate`/`GenerateFunction` 主流程、常量与 alloc_tile 发射 |
| [src/codegen/code_emitter.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/code_emitter.cpp) | `CodeEmitter`：缩进管理 + 输出缓冲的最小文本助手（62 行） |
| [src/codegen/pto/pto_control_flow_codegen.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp) | `ForStmt`/`IfStmt`/`WhileStmt`/`YieldStmt` 的 `scf.*` 指令化 |
| [src/codegen/pto/pto_scalar_expr_codegen.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_scalar_expr_codegen.cpp) | 标量表达式的 `arith.*` 指令化（addi/subi/muli/cmpi…） |
| [src/backend/common/pto_ops_elementwise.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp) | 后端算子发射表：`kSimpleOps` 静态映射 + 累加算子（matmul_acc/gemv_acc）的自定义发射 |
| [src/backend/common/pto_ops_shared.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_shared.cpp) | `GenerateInsOutsClause`/`EmitInsOuts`：`ins()/outs()` 子句的统一拼装 |
| [src/backend/common/pto_ops_crosscore.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp) | tpush/tpop/initialize_pipe 的发射，含 **C2V 推送的 L0C pitch 规则与拒绝条件** |
| [include/pypto/ir/type_inference.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h) | `AccPitchesCoincide`：累加器「打包行距 == 物理行数」的可证判定 |
| [docs/en/dev/codegen/00-pto_codegen.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md) | 代码生成契约文档（英文权威版），本讲第 4 模块的主要对照 |
| [tests/ut/codegen/test_pto_codegen_cross_core.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py) | 跨核发射的单测，含本次 pitch 修复的四个新用例 |

一个容易混淆的点先澄清：**`PTOCodegen` 自己几乎不知道任何具体算子怎么发射**。它负责遍历 IR、管理 SSA 名字表、发常量和 alloc_tile；每个 `tile.*` 算子的指令形态住在后端（`src/backend/common/pto_ops_*.cpp`）的注册表里。这是「遍历逻辑」与「算子知识」的分离，后端因此可以按 910B/950 各自注册不同发射器（u6-l3 展开）。

## 4. 核心概念与源码讲解

### 4.1 PTOCodegen 总体架构：严格 1:1 映射与 Generate 主流程

#### 4.1.1 概念说明

代码生成是编译器的「翻译最后一跳」。PyPTO 给它立了一条铁律（[docs/en/dev/codegen/00-pto_codegen.md:L5-L17](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L5-L17)）：**codegen 必须是 IR 到产物的严格 1:1 翻译**——只做「IR 节点 → 输出构造」的映射、类型格式转换、命名与 SSA 簿记；任何数据流分析、IR 重构、优化都属于更早的 Pass。理由很实际：塞进分析的 codegen 又脆又难独立测试，而且会跟已经做過同一件事的 Pass 重复（历史上确有先例：编排 codegen 里的返回值追踪被重构成了 `NormalizeReturnOrder` Pass）。

理解这条铁律后，`PTOCodegen` 的结构就顺理成章：它是一个 `IRVisitor`，访问到什么就发什么；所有「决定」都应该已经在前 47 个 Pass 里做完。

#### 4.1.2 核心流程

```text
PTOCodegen::Generate(program, emit_tile_addr, emit_source_loc)
  ├─ PrepareGMSlotBufferLayout: 扫 initialize_pipe，规划 GM 槽缓冲偏移
  ├─ 发 "module attributes { pto.target_arch = "..." } {"
  ├─ 对 program 里每个函数（必须全是 InCore 变体）:
  │    GenerateFunction(func)
  │      ├─ 收集张量参数形状里的动态维（dyn_vars）
  │      ├─ BuildVarToMemRefMapping + MemRefCollectorVisitor 一遍体遍历:
  │      │    收集 MemRef/TileType、检测隐藏运行时参数
  │      │    (SDMA workspace / SPMD block idx / subblock idx / deferred completion)
  │      ├─ 为每个 tile 变量分配 SSA 名（PTOAS 模式下同 MemRef 身份共享句柄）
  │      ├─ 发 func.func 签名: 张量在前标量在后 + 尾随 index 参数
  │      └─ VisitStmt(body_) —— 进入语句访问器（4.2 / 4.3）
  └─ 发 "}"
```

产物的固定生成顺序（[docs:L32-L42](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L32-L42)）：**常量 → tensor_view → alloc_tile → 函数体**。一个实现细节：tensor_view/alloc_tile 前奏先渲染进独立缓冲，常量块最后定稿——这样只在形状/步长表达式里出现的常量（如复合维度 `M * 2` 里的 `2`）也能先声明后使用。

#### 4.1.3 源码精读

入口 [pto_codegen.cpp:L668-L704](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L668-L704)：`Generate` 重置所有缓冲，从后端句柄拿 `pto.target_arch`，然后逐函数生成。循环里有一个 `INTERNAL_CHECK_SPAN`（[L686-L688](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L686-L688)）拦截非 InCore 函数——PTO 后端只编译设备内核，编排函数走另一条 codegen 路（u6-l2）。两个布尔参数值得记住：`emit_tile_addr` 由内存规划器模式决定（`PTOAS` 模式传 false，省略 addr），`emit_source_loc` 控制 `loc(...)` 后缀。

一遍体遍历的收集器 `MemRefCollectorVisitor`（[pto_codegen.cpp:L540-L646](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L540-L646)）是「收集与检测合一」的范本：`VisitExpr_(VarPtr)` 把每个带 MemRef 的 Tile 变量登记进 `memrefs_`/`memref_tile_types_`（按 base 指针去重，重复出现时合并 TileView 的 pad 属性，[L621-L645](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L621-L645)）；`VisitExpr_(CallPtr)` 顺路检测四类「隐藏运行时参数」（[L586-L608](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L586-L608)）——函数体里调了 `tile.get_block_idx` 就在 MLIR 签名尾上补一个合成 i32 参数，这类值**只存在于生成的 MLIR/C++ 里，永远不进 IR 的 `Function.params`**（文档 [L816-L833](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L816-L833) 有完整说明）。注意这里用的是 `ir::IsOp` 而非裸字符串比较（u4-l6 建立的规则）。

每个 tile 变量的 SSA 绑定在 [pto_codegen.cpp:L932-L971](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L932-L971)：`PYPTO` 模式下每个变量一个名；`PTOAS` 模式（无 addr）下用 `MemRefIdentityKey`（base+offset+size，[L148-L158](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L148-L158)）让「同一缓冲」的多个变量共享一个 tile_buf 句柄——否则 ptoas `PlanMemory` 会把它们当两个独立缓冲，循环携带累加器的原地写就断了。

`func.func` 签名按「张量在前、标量在后」重排（[pto_codegen.cpp:L989-L1046](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L989-L1046)）。这不是风格偏好：PTOParam 运行时按 `[tensors..., scalars...]` 顺序分发实参，MLIR 签名必须与之对齐；动态维再以 `%argN: index` 尾随参数补齐。

文本输出助手 `CodeEmitter`（[code_emitter.cpp:L23-L58](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/code_emitter.cpp#L23-L58)）只有缩进计数器加 `std::stringstream`：`EmitLine` 拼缩进、`IncreaseIndent`/`DecreaseIndent` 维护层级、`GetCode` 取走全文。`PTOCodegen` 没有直接用它输出，而是自带同构的 `Emit`/`EmitStructural`（见 4.2.3），但基类接口（`GetCurrentResultTarget` 等）由 `CodegenBase` 统一——后端发射函数拿到的是 `CodegenBase&`，因此同一套发射器可被其它 codegen 复用。

#### 4.1.4 代码实践

1. **实践目标**：不经 DSL，直接对一个手工构造的 IR 程序调用 `PTOCodegen`，看清生成顺序与签名重排。
2. **操作步骤**（示例代码，模仿 [tests/ut/codegen/test_pto_codegen_cross_core.py:L694](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py#L694) 的构造方式）：

   ```python
   from pypto.pypto_core import codegen, ir
   from pypto.language import pl
   from pypto.backend import BackendType, backend

   span = ir.Span.unknown()
   # ... 用 ir.Var / ir.Call / ir.Function 手工搭一个最小 InCore 函数 ...
   backend.reset_for_testing()
   backend.set_backend_type(BackendType.Ascend910B)
   print(codegen.PTOCodegen().generate(ir.Program([func], "demo", span)))
   ```

   先跑通该测试文件里的任意一个用例，再把其构造替换成自己的最小函数。
3. **需要观察的现象**：输出以 `module attributes {pto.target_arch = "..."} {` 开头；函数体内 `arith.constant` 在最前、`pto.make_tensor_view` 次之、`pto.alloc_tile` 再次、计算指令最后；签名里张量参数排在标量参数前。
4. **预期结果**：即使把 IR 里参数写成「标量在前」，生成的 `func.func` 也是张量在前；`generate()` 可反复调用，第二次输出与第一次逐字符相同（无隐藏状态泄漏）。`pto.target_arch` 的具体值与后端绑定（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MemRefIdentityKey` 用「base 指针 + offset + size」三要素，而不是只用 base？
答案：同一 base 上还可以开出**视图**——共享 base 但 offset/size 不同。只比 base 会把视图和本体误判成同一缓冲；三要素全同才是「同一块字节」，才允许共享 tile_buf 句柄做原地写。

**练习 2**：`Generate` 里对非 InCore 函数抛 `INTERNAL_CHECK` 而不是 `CHECK`，符合项目规范吗？
答案：符合。到达 codegen 的 IR 已经过整条流水线验证，出现编排函数说明上游 Pass 出了问题（编译器 bug），按 error-checking 规范该用 `INTERNAL_CHECK`（且 `_SPAN` 变体带上源位置）。

### 4.2 算子发射：注册表分发与 `pl.add → pto.tadd` 全链路

#### 4.2.1 概念说明

`tile.*` 算子有约 150 个（u2-l4），逐个在访问器里写 `if/else` 不可维护。PyPTO 的做法是把「算子名 → 发射函数」的映射表放进后端：`Backend::RegisterOp(op_name).f_codegen(fn)` 注册，`PTOCodegen` 访问到 `Call` 节点时查表调用。于是新增一个算子的发射只需在表里加一行或一个 lambda（u7-l8 的全栈流程会走一遍）。

简单算子（add/sub/mul/exp…）进一步共享一张**静态描述表** `kSimpleOps`，每行只有三字段：IR 算子名、PTO 指令名、操作数个数。注册循环为每行生成同一个通用 N 元发射器——这就是「数据驱动」消除重复代码的教科书案例。

#### 4.2.2 核心流程

以 hello world 里的 `tile_c = pl.add(tile_a, tile_b)` 为例（此刻 IR 已是 `AssignStmt(var=tile_c, value=Call(tile.add, [tile_a, tile_b]))`）：

```text
VisitStmt_(AssignStmt)
  ├─ tile_c 的 TileType 带 MemRef → EmitAllocTileForVar(tile_c)   # 发 %tile_c = pto.alloc_tile ...
  ├─ 查 var_to_mlir_ 得结果缓冲 SSA 名 → 存入 current_result_buf
  └─ VisitExpr(Call)
       └─ VisitExpr_(CallPtr)
            ├─ backend_->GetOpInfo("tile.add") → OpInfo{codegen_func}
            └─ codegen_func(op, *this) 返回 "pto.tadd ins(...) outs(...)"
                 └─ MakeNaryCodegenPTO
                      ├─ GetExprAsCode(args[i]) → 各操作数的 SSA 名（查 var_to_mlir_）
                      └─ GenerateInsOutsClause
                           ins = (tile_a 的 SSA, tile_b 的 SSA) [: 类型注解]
                           outs = (current_result_buf) [: 结果类型注解]
```

操作数「来自 MemRef」的确切含义：`tile_a` 是 IR 的 `Var`，它的 `TileType::memref_` 在 u5-l7 已定；`GenerateFunction` 阶段按 MemRef 给变量绑好了 MLIR SSA 名（同缓冲共享句柄），发射时只需查表回显。**地址信息不在指令里，而在 `alloc_tile` 的 `addr` 属性里**——指令操作数是缓冲句柄（SSA 名），硬件寻址由缓冲定义承担。

#### 4.2.3 源码精读

语句访问器 [pto_codegen.cpp:L2056-L2070](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2056-L2070) 的 `VisitStmt` 先做两件事：拒收 `SplitAivScopeStmt`（它本应在 Pass 20 被消掉，漏到这里说明上游出错，宁可大声失败也不静默解包），然后建立 `SpanScope`——当前语句的源位置将作为其下所有指令的默认 `loc(...)`。

`VisitStmt_(AssignStmtPtr)`（[pto_codegen.cpp:L2072-L2156](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2072-L2156)）是结果绑定的核心：[L2087-L2091](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2087-L2091) 为带 MemRef 的结果变量发 `pto.alloc_tile`（`set_validshape` 与原地别名两种情况跳过）；[L2093-L2145](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2093-L2145) 分四类决定结果缓冲——普通 Tile 用预绑名、无 MemRef 的 Tile（如 tpop 结果，数据住在保留槽里）现铸一个名、原地算子直接别名到输入的 SSA、标量结果预占一个名——最后把名字存进 `current_result_buf` 再 `VisitExpr(op->value_)`。这个「先定靶再发射」的顺序是后端发射器能调用 `GetCurrentResultTarget()` 的前提。

表达式访问器 [pto_codegen.cpp:L2230-L2246](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2230-L2246) 只有十几行：查后端 `OpInfo`，查不到就抛「无代码生成」错误；查到则先做一次 span 精化——`SpanContains`（[L129-L141](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L129-L141)）检查 Call 自己的 span 是否真嵌在语句 span 内，是才采用（列级精确），否则保留语句 span。这个包含测试挡住的是 Pass 重建 Call 时塞进来的粗 span：`ConvertTensorToTileOps` 合成的 tile op 带的是**整个函数**的 span，不加过滤大部分指令都会报到 `def` 行去。发射完成后 `Emit` 拼上 `LocSuffix()`（[L2266-L2282](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2266-L2282)）——`pto.tadd ins(...) outs(...) loc("kernels/foo.py":41:9)`。`@pl.jit` 下 span 已从合成的 `<jit:name>` 文本重映射回真实源码，所以 ptoas 的诊断能指到用户写的 `.py` 行而非他从未见过的 `.pto` 行。

简单算子表 [pto_ops_elementwise.cpp:L581-L697](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L581-L697)：`SimpleOpEntry{op_name, pto_op_name, arity}` 三字段，`{"tile.add", "pto.tadd", 2}` 在 [L591](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L591)。表里能读出不少语义：标量变体统一加 `s` 后缀（`tile.muls → pto.tmuls`）；`tile.rem`/`tile.xor` 是 3 操作数（多一个 tmp 暂存）；`tile.matmul → pto.tmatmul` 也在表里，而三个 `_acc` 变体被注释掉走自定义发射（原因见下）。注册循环 [L700-L717](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L700-L717) 给每行挂同一个 `MakeNaryCodegenPTO`，行级布局约束（row_major）按需叠加。

通用 N 元发射器 `MakeNaryCodegenPTO`（[pto_ops_elementwise.cpp:L108-L174](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L108-L174)）核心一行在 [L172](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L172)：`codegen.Emit(pto_op_name + " " + GenerateInsOutsClause(op, codegen))`。`GenerateInsOutsClause`（[pto_ops_shared.cpp:L327-L368](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_shared.cpp#L327-L368)）把每个实参经 `GetExprAsCode` 变成 SSA 名拼进 `ins(...)`，类型注解经 `GetExprTypeAnnotation` 收集在冒号后；`outs` 侧直接取 `GetCurrentResultTarget()` 与 `GetCurrentResultTileBufTypeString()`。类型注解「全有或全无」——位置子句漏一个就会错位绑定（发射器里有对应的 `INTERNAL_CHECK`）。需要显式控制操作数列表的发射器改用 `EmitInsOuts`（[pto_ops_shared.cpp:L378-L402](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_shared.cpp#L378-L402)），两者共享同一格式约定。

`GetExprAsCode`（[pto_codegen.cpp:L2284-L2305](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L2284-L2305)）是操作数解析的入口：`Var` 查名字表、`ConstInt`/`ConstFloat` 走常量池（`GetOrEmitConstant` 按「值+dtype」去重并写进常量段，[L1806-L1851](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L1806-L1851)；无符号整数还要经 `unrealized_conversion_cast` 桥接，因为 MLIR 的 `arith.constant` 要求无符号整型），复杂表达式回落到访问器现场发射。

`pto.alloc_tile` 的发射在 `EmitAllocTileForVar`（[pto_codegen.cpp:L1759-L1798](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_codegen.cpp#L1759-L1798)）：按变量去重、按句柄名去重（PTOAS 共享句柄只发一次定义）、多槽分配优先走 `pto.alloc_multi_tile` 区域，否则发 `%name = pto.alloc_tile [addr = ...] [valid_row = ...] [valid_col = ...] : !pto.tile_buf<...>`。tile_buf 类型串里的 `compact=1` 属性正是 4.4 模块的主角——它只在非默认值时打印（[docs:L676-L701](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L676-L701)）。

#### 4.2.4 代码实践

1. **实践目标**：导出 hello world 的 `.pto` 产物，找到 `pl.add` 对应的指令行，反向指认它的三个操作数各来自哪个 `alloc_tile`。
2. **操作步骤**：
   - 复制 [examples/beginner/01_hello_world.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/01_hello_world.py#L28-L42)，把最后一行调用改成带保存配置：

     ```python
     tile_add(a, b, c, config=RunConfig(save_kernels=True, save_kernels_dir="/tmp/pto_out"))
     ```

     `save_kernels`/`save_kernels_dir` 是 `RunConfig` 的字段（[python/pypto/runtime/runner.py:L336-L339](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/runtime/runner.py#L336-L339)）；默认平台 `a2a3sim` 是模拟器，无硬件也能跑。
   - 打开输出目录（结构见 [docs:L757-L772](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L757-L772)），读 `ptoas/tile_add.pto`（MLIR 就是 PTOCodegen 的直接输出）。
3. **需要观察的现象**：
   - 常量段（`%c128_index = arith.constant 128 : index` 等）→ `pto.make_tensor_view`×3 → `pto.alloc_tile`×3（a/b/c 三个 128×128 FP32 缓冲）；
   - 两行 `pto.partition_view` + `pto.tload`（load 的两段式，[docs:L426-L452](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L426-L452)）；
   - 一行 `pto.tadd ins(%a_buf : ..., %b_buf : ...) outs(%c_buf : ...)`，尾随 `loc("...01_hello_world.py":33:17)` 之类的源位置；
   - `pto.partition_view` + `pto.tstore` 收尾。
4. **预期结果**：`pto.tadd` 的 `ins` 两个操作数 = 前面两个 `pto.tload` 的 `outs` 句柄；`outs` 操作数 = 第三个 `alloc_tile` 的句柄，且与 `pto.tstore` 的 `ins` 相同。`loc()` 报告的行号能对上 `tile_c = pl.add(tile_a, tile_b)` 所在行（列号来自 Call span 精化）。目录内文件的具体命名以本地运行为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tile.add` 的发射逻辑可以只有一个通用的 `MakeNaryCodegenPTO`，而 `tile.matmul_acc` 必须自定义？
答案：`pto.tadd` 的操作数布局与 IR 实参一一对应（ins = 全部实参，outs = 结果）。`pto.tmatmul.acc` 要求 ptoas 层 `ins(acc) == outs`（原地累加），累加器要从结果缓冲取而非实参；再叠加 `init_cond` 的两形态分支，通用模板表达不了。

**练习 2**：把 `pto.tadd` 的类型注解去掉一个（只注 `ins` 第一个操作数）会怎样？
答案：`: t0, t1` 是**位置**子句，注一个就把 `t0` 绑到第一个操作数、剩下的操作数无注解，语义错位。发射器用 `INTERNAL_CHECK(!any || all)` 拦截「部分注解」，这正是 u4-l8 讲过的「全有或全无」约束在 codegen 的镜像。

### 4.3 控制流与标量表达式的指令化

#### 4.3.1 概念说明

设备侧控制流落到 MLIR 的 `scf.` 方言：`ForStmt → scf.for`、`IfStmt → scf.if`、`WhileStmt → scf.while`。关键设计决策是：**Tile 不进 scf 的 iter_args/结果位**。`scf.for` 的 iter_args 是函数式 SSA 语义（每轮产生新值），而 Tile 是可变引用、经 `outs()` 原地写——把 Tile 塞进 iter_args 会逼着每轮发一条拷贝。所以只有标量走 iter_args/yield，Tile 直接映射到它的 MemRef 句柄，循环体原地写。

标量表达式（循环边界、偏移量、比较）落到 `arith.` 方言。这部分是「表达式树 → 指令序列」的直译，没有惊喜，但要注意类型边界：MLIR 的 `index` 类型与 i32 之间要显式 `arith.index_cast`。

#### 4.3.2 核心流程

```text
ForStmt(lower, upper, step, body, iter_args)
  ├─ 编译期检查 step > 0（scf.for 只支持正步长，降序循环直接 CHECK 报错）
  ├─ start/stop/step 逐个求值并 EmitCastToIndex
  ├─ 循环变量绑定新 SSA 名
  ├─ iter_args 分类: 标量 → scf.for iter_args + 末尾 scf.yield
  │                 Tile/Tensor → 原地引用，不进 iter_args
  └─ EmitStructural("scf.for %i = %start to %stop step %step {") / body / "}"

IfStmt(cond, then, else, return_vars)
  ├─ 求值 cond
  ├─ 无 return_vars → "scf.if %cond {" then ["} else {" else] "}"
  └─ 有 return_vars: 标量 → scf.if 结果位
                    Tile → 函数头部预声明 phi 句柄（两分支共用、支配所有读点）
                    Tensor/Array → 可变引用，两分支 yield 同一 SSA
```

#### 4.3.3 源码精读

`VisitStmt_(ForStmtPtr)` 在 [pto_control_flow_codegen.cpp:L519](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L519) 起。开头一段教科书级的用户错误处理（[L536-L568](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L536-L568)）：`scf.for` 只定义正步长，降序循环直译会被汇编器折成零趟、静默丢掉循环体——所以这里用 `CHECK_SPAN` 挡下**编译期可判定的**非正步长，错误信息直接给出改写配方（`for i in pl.range(64, 0, -1)` → `for t in pl.range(0, 64)` 加 `i = 64 - t`）。注释特意说明这是永久检查而非临时补丁，且报错点名用户的循环、指明改法，比汇编器对着用户从未见过的 `.pto` 报错强得多。循环骨架在 [L570-L600](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L570-L600)（含「只有标量需要 iter_args」的决策注释）与 [L649-L652](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L649-L652)（简单 `scf.for` 的发射）。

`VisitStmt_(IfStmtPtr)` 在 [pto_control_flow_codegen.cpp:L214-L238](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L214-L238) 处理无返回值分支；带返回值的路径（[L240-L336](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L240-L336)）按类型三路分发：标量进 `scf.if` 结果位；Tile 的 phi 句柄**提升到函数头声明**以支配两分支与所有后续读点，动态 valid_shape 因不能出现在头部声明的操作数位而先按物理盒声明、紧跟一条 `pto.set_validshape` 恢复逻辑形状（[L324-L329](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_control_flow_codegen.cpp#L324-L329)）；Tensor 走可变引用（路由进 scf.if 会把它重定型成全动态 `!pto.tensor_view<?x?>`，丢掉 `pto.partition_view` 需要的具体维度）。

标量算术的派发只有三行（[pto_scalar_expr_codegen.cpp:L192-L194](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_scalar_expr_codegen.cpp#L192-L194)）：`Add → arith.addi/addf`、`Sub → arith.subi/subf`、`Mul → arith.muli/mulf`——整数与浮点各一个指令名，由操作数 dtype 选择；比较走 `arith.cmpi`（[L46](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/codegen/pto/pto_scalar_expr_codegen.cpp#L46)）。

本节最有教学价值的一段是**累加算子的 `init_cond` 谓词降级**。u2-l4/u4-l6 已建立：`init_cond` 是 `matmul_acc`/`gemv_acc` 家族可选的第四操作数，谓词为真时覆写累加器。发射器 [pto_ops_elementwise.cpp:L974-L1061](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L974-L1061) 的 `make_acc_codegen` 揭示了它到硬件的最后一跳：**PTO 指令层没有「带谓词的累加」这一种指令**，谓词只存在于 MAD 的 `Xt` 寄存器一位，而 `pto.*` 层把它暴露为「累加形与非累加形两条指令的选择」。于是：

- 无 `init_cond`（3 实参）→ 直接发 `pto.tmatmul.acc ins(%dst, %lhs, %rhs) outs(%dst)`，注意 **`ins` 首位就是 `outs`**（原地约定，[L1027-L1030](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L1027-L1030)）；
- 字面量谓词（`ConstInt` 或被化简器折叠出的 `ConstBool`）→ 编译期选一边，只发一条（[L1038-L1045](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L1038-L1045)；漏掉 `ConstBool` 分支会在常量条件上留一个双臂 `scf.if`，MAD 数翻倍）；
- 运行期谓词 → 先在区域外求值条件（保证支配两臂），再发 `scf.if %cond { 非累加形 } else { 累加形 }`（[L1047-L1059](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L1047-L1059)）。两臂都原地写 `dst`，所以**不需要 phi**——这正是 u5-l6 讲过的「谓词化单条累加、无 phi 两段流水」在指令层的形态。

本次更新（PR #2528）把这条路径对齐到了 `tile.gemv_acc`：发射工厂删掉了 `supports_init_cond` 开关，`gemv_acc` 与 `matmul_acc` 共用同一谓词逻辑（GEMV 本就是 M=1 的 matmul，跑在同一个 Cube MAD 上，携带同一个初始化位），diff 见 [pto_ops_elementwise.cpp:L972-L979](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_elementwise.cpp#L972-L979)。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `init_cond` 的三档降级差异。
2. **操作步骤**：
   - 写一个 split-K 矩阵乘：K 维两段循环，`tile.matmul_acc(..., init_cond=(k0 == 0))`（参照 u2-l4 的实践）；
   - 用 `RunConfig(save_kernels=True, dump_passes=True)` 编译，读 `ptoas/*.pto`；
   - 再写一个**运行期才知道**的谓词版本（如 `init_cond=(m_valid > 0)`，`m_valid` 来自标量参数）对比。
3. **需要观察的现象**：字面量/可折叠谓词版本里只有**一条** `pto.tmatmul` 或 `pto.tmatmul.acc`；运行期谓词版本里出现 `scf.if %cmp ... { pto.tmatmul ... } else { pto.tmatmul.acc ... }`，条件指令（`arith.cmpi`）在 `scf.if` 之外。
4. **预期结果**：两版本数值一致（与 torch 对照）；`ins` 首操作数与 `outs` 相同（原地累加契约）。若谓词写成常量 `True` 却看到双臂 `scf.if`，说明化简没折叠——那是 bug 线索（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么运行期 `init_cond` 的条件求值必须放在 `scf.if` 区域之外？
答案：MLIR 区域内的指令只支配区域内的值。条件放进区域内，另一臂就引用不到；先在区域外求值，条件 SSA 支配两臂，两条指令形态都能用它。

**练习 2**：Tile 不进 `scf.for` iter_args，那循环携带的累加器靠什么保证「下一轮读到上一轮写的值」？
答案：靠 MemRef 身份。`MaterializeSemanticAliases`（u5-l7）已让 iter_arg、循环结果与初值共享同一 MemRef，codegen 又让同一 MemRef 身份的变量共享同一 tile_buf 句柄，`outs(%acc)` 的原地写天然跨轮可见——不需要 SSA 函数式传递。

### 4.4 跨核推送：compact 累加器的 C2V pitch 规则与拒绝条件

#### 4.4.1 概念说明

本模块讲本次更新（PR #2531，修 issue #2510）的核心：**一个行收窄的矩阵乘累加器跨 Cube→Vector 边界时，TPUSH 必须保持生产者写下的行 pitch**。先铺三条硬件事实：

1. **`mad` 按有效行数决定 L0C 行距**。矩阵乘指令把乘积摊进 L0C 时，N-fractal 行距取自左操作数（L0A）的**有效**行数：
   \[ \text{pitch} = \left\lceil \frac{validRow}{16} \right\rceil \cdot 16 \]
   一个 64 行的盒只有效 16 行时，`mad` 用 16 的行距写——每个 fractal 块 j 落在偏移 4j 处（4 = 16/16·每行 fractal 数）。
2. **L0C 的读者按 validRow 推导步长**。TPUSH 走 `TStoreAccNz2nd` 读 L0C，源 pitch 对 compact tile 是 \( \lceil validRow/16 \rceil \cdot 16 \)、否则是物理 `Rows`。也就是说「读 pitch」跟着**当时的 validRow** 走。
3. **C2V FIFO 槽按物理盒布局**。槽内数据按 `valid_col` 行距紧凑存放，消费者 lane 按同一步长读回（[docs:L219-L233](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L219-L233)）。

三者凑出一个陷阱：**如果在推送前把 `validRow` 扩成物理盒行数**（旧代码对部分 Acc 载荷的做法），TPUSH 就会按 \( \lceil 64/16 \rceil \cdot 16 = 64 \) 的步长去读一块 `mad` 按 16 步长写的 L0C——读到的是错位的陈旧字节。issue #2510 的症状正是：65536 个元素错 14336 个，每 `N_TILE` 只有前 16 列正确。

#### 4.4.2 核心流程

修复后的 `EmitTpushTransportValidShape`（无分裂 Acc→Vec 方向）：

```text
transport_row / transport_col 决策矩阵（split == 0，源在 Acc 空间）:
  有效区为静态 0        → 不扩（协议性空操作）
  列（transport_col）   → 扩到物理盒列数
                          （列按物理盒放驻；部分列区间会在有效行内留下陈旧字节）
  行（transport_row）   → 保持 valid_shape[0]（生产者写的行距）
                          （扩行 = 重推 pitch = 走错位；行数以外的区域本来就承诺是陈旧的）

split != 0（真分裂）且 tile 为 compact 且 pitch 不重合:
  → CHECK_SPAN 拒绝编译，错误信息给出两条 DSL 替代方案
```

pitch 是否「重合」由 `AccPitchesCoincide` 判定：可证 \( validRow = Rows \)、或静态 \( \lceil validRow/16 \rceil \cdot 16 = Rows \)、或物理行数恰为一个 fractal 块（16）——这些情况下 compact 标记改变不了任何读者的步长，照旧过界。**比较的是 pitch 而非 validRow 与 Rows**：一个 `[16, N]` 的 gemv 累加器有效 1 行也能打包进自己的盒，按行数比较会误拒合法程序。

分裂方向为什么只能拒绝：lane 1 从盒半处开始读自己的带——那片数据只有生产者写过全盒才存在；而写全盒又意味着按物理 pitch 读 L0C——正是 `mad` 没用的那个步长。两个要求互斥，没有正确指令序列可发，所以宁可编译失败也不静默错编（实测该形状在设备上 8192 错 1808，且无声）。

#### 4.4.3 源码精读

整个决策住在 [pto_ops_crosscore.cpp:L81-L196](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L81-L196) 的 `EmitTpushTransportValidShape`。函数开头（[L84-L103](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L84-L103)）先分清三种需要「盒规范化传输」的 `split == 0` 场景：真无分裂（不管）、910B 双 AIV 无分裂调度（扩列保行，旧逻辑）、**无分裂 Acc→Vec（本修复的对象）**。

修复本体在 [pto_ops_crosscore.cpp:L122-L146](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L122-L146)：注释把因果链写透了——列必须全宽（槽的行距是物理列数，部分列区间会在**有效行内**留陈旧字节），行必须原样（每个 L0C 读者都从 validRow 推导步长；扩行让 TPUSH 以 `mad` 从未写过的步长走 L0C）。落到代码是两步：静态零维直接返回不扩（空 Tile 是协议性操作），然后 `transport_row = valid_shape[0]`——一行赋值就是整个 bug fix 的行为面。

分裂拒绝在 [pto_ops_crosscore.cpp:L170-L186](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L170-L186)：`CHECK_SPAN`（用户可见错误——这是「文档化的 DSL 限制」而非编译器 bug，符合 error-checking 规范里 pass/codegen 少用 CHECK 的例外条款）断言「非分裂 或 非 compact 或 pitch 重合」，错误消息点名两条出路：**别收窄 matmul 左操作数的行（改用 `pl.set_validshape` 收窄结果）**，或**把累加器经 GM 中转、在第二个作用域里再消费**。判定函数 `AccPitchesCoincide` 在 [type_inference.h:L693-L708](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L693-L708)——注释明言「stamp 与 verifier 不得漂移」，它同时被本发射器和 `AccCompactValid` 验证器（u5-l1 提过）共用。

指令序列的组装在 `MakeTpushCodegenPTO`（[pto_ops_crosscore.cpp:L249-L286](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L249-L286)）：先调 `EmitTpushTransportValidShape`（返回是否发了扩形 `set_validshape`），再发 `pto.tpush_to_aiv(%buf : type) {split = N}`，最后若扩过形就用 `EmitLogicalTpushValidShapeRestore`（[L198-L209](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L198-L209)）把生产者 tile 的逻辑有效形恢复回来——**临时扩、推、还原**三明治结构。消费者侧的 `MakeTpopCodegenPTO`（[L290-L358](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L290-L358)）镜像地扩 TPOP（含 `validCol` 收窄会把 GM 间隙塌成零、连续读错位的坑，[L313-L327](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/backend/common/pto_ops_crosscore.cpp#L313-L327) 的注释），再用**纯元数据的 `pto.treshape`**（而非 `set_validshape`——tpop 结果不是本地绑定的 PTOAS tile）恢复逻辑形。

四个新单测（[tests/ut/codegen/test_pto_codegen_cross_core.py:L835-L893](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py#L835-L893)）各自锁定一个行为面：`test_no_split_acc_to_vec_transport_keeps_the_producer_row_extent`（行保真）、`test_no_split_acc_to_vec_transport_widens_columns_only`（只扩列）、`test_split_acc_to_vec_rejects_a_row_narrowed_compact_accumulator`（`pytest.raises(ValueError, match="cannot cross a split Cube-to-Vector boundary")`）、`test_split_acc_to_vec_allows_a_single_fractal_block_accumulator`（单 fractal 块豁免）。另一个现成的对照用例 [L694](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py#L694)（列收窄、行全宽的 16×32 盒有效 16×24）断言生产者发**两条** `set_validshape` 夹住 tpush（第一条 `, %c16_index, %c32_index :` 扩列、第二条 `, %c16_index, %c24_index :` 还原），消费者发 `pto.tpop_from_aic(%c16_index, %c32_index)` 后跟 `pto.treshape`——它演示的就是不含行收窄时的旧规则全貌。

契约的另一端（谁给累加器打 compact 标记、为什么 `tile.create` 要加 `compact=True` 声明）在 [docs:L703-L738](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L703-L738)：标记只在「累加器布局确立处」打——`matmul` 推断器打、`matmul_acc` **继承**累加操作数的模式、`set_validshape` 同样继承（元数据操作不改变已写下的 pitch）；声明（kwarg）是唯一能扛过 `InferTileMemorySpace` 重推断的形式，这正是 u5-l6 讲过的 `tile.create(compact=True)` 种子。文档还留了一个已知缺口：Acc→L1 的读者（`TExtractAccToMat` 等）在两个架构上都没有 `CompactMode` 分支，行收窄累加器经 `tile.extract`/`tile.move` 进 L1 仍按物理 `Rows` 读——需要配套的 PTO-ISA 改动（[L736-L738](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/codegen/00-pto_codegen.md#L736-L738)）。

#### 4.4.4 代码实践

1. **实践目标**：用三个手工 IR 程序复现新规则的三个分支（行保真、只扩列、分裂拒绝），不依赖设备。
2. **操作步骤**：以 [tests/ut/codegen/test_pto_codegen_cross_core.py:L791](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py#L791) 的 `_row_narrowed_acc_push_program` 辅助函数为模板（它构造一个 AIC 函数，把 64×128、有效行 `valid_rows`、compact 模式可选的 Acc tile 推进 C2V FIFO），复制出三个调用：
   - `_row_narrowed_acc_push_program(16, 128, ir.CompactMode.normal)`（split=0，行收窄）；
   - `_row_narrowed_acc_push_program(16, 96, ir.CompactMode.normal)`（split=0，行列双收窄）；
   - `_row_narrowed_acc_push_program(16, 128, ir.CompactMode.normal, split=1)`（分裂）。
   分别 `codegen.PTOCodegen().generate(...)` 取产物文本。
3. **需要观察的现象**：
   - 第一个：tpush 前后的两条 `pto.set_validshape` 都是 `, %c16_index, %c128_index :`——行**没有**被扩到 64；
   - 第二个：扩形那条把列扩到物理盒宽（`, %c16_index, %c128_index`），还原条恢复逻辑列（`, %c16_index, %c96_index`）；行始终是 16，不被扩到 64；
   - 第三个：`pytest.raises(ValueError)`，消息含 "cannot cross a split Cube-to-Vector boundary" 与两条替代方案。
4. **预期结果**：与三个单测的断言一致（[L835-L893](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py#L835-L893)）。第二个实验里若把 `compact` 换成 `ir.CompactMode.null`（非 compact tile，读者按物理 Rows 推步长），扩行到 64 反而是安全的——观察传输 `set_validshape` 变成 `, %c64_index, %c128_index`。跑通后试着回答：为什么单 fractal 块（`rows=16`、有效 8）在 split=1 下仍被放行？——因为 \( \lceil 8/16 \rceil \cdot 16 = 16 = Rows \)，pitch 重合（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么消费者侧恢复逻辑形用 `pto.treshape` 而不用 `pto.set_validshape`？
答案：`set_validshape` 只能作用于本地绑定的 PTOAS tile（alloc、`scf.if` 结果、跨核 pop 槽句柄）；tpop 的结果 SSA 是前端弹出的值，不是本地分配的 tile，ptoas 会拒绝对它 set_validshape。`treshape` 是纯元数据的重定型视图，且能携带目标 `v_row/v_col`——但它只能恢复**静态**逻辑形，这正是 TPOP 全盒路径要求静态逻辑形的原因。

**练习 2**：修复为什么选择「扩列保行」而不是把 compact 标记传播到弹出侧的 Vec tile？
答案：issue 报告里提议过后者，但 pto-isa 的 Vec 读取路径根本不读 `TileData::Compact`，且载荷到达 FIFO 槽时已是普通 ND——类型不一致只是症状，根因在推送侧的 pitch 推导。修推送点（不扩行）才是修因。

**练习 3**：`EmitTpushTransportValidShape` 里的拒绝检查为什么用 `CHECK_SPAN` 而非 `INTERNAL_CHECK`？
答案：它拦的是**用户程序形状**（行收窄的 matmul 左操作数跨了 `pl.split` 边界），消息面向 DSL 作者、给出改法，是「文档化的用户可见限制」——按 error-checking 规范归 CHECK 一类；其余「发射器内部不变量」（如结果必须绑 SSA）才用 INTERNAL_CHECK。

## 5. 综合实践

**任务：给一个「Cube 计算 + Vector 尾巴」的混合小核做一次完整的 `.pto` 导读。**

1. 写一个最小混合核：`pl.at(CUBE)` 里做 `pl.matmul`（左操作数用 `valid_shape` 收窄行，模拟 M 维尾块），结果 `tpush_to_aiv`；`pl.at(VECTOR)` 里 `tpop_from_aic` 后接一个 `pl.mul` 加 `pl.store`。可以混合引用 [examples/advanced/03_mixed_kernel.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/03_mixed_kernel.py) 与 [tests/ut/codegen/test_pto_codegen_cross_core.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py) 的写法。
2. `RunConfig(save_kernels=True, dump_passes=True)` 编译后，对 `ptoas/` 下两个函数的 `.pto` 各写一份「行号注释」：
   - 每条 `pto.*` 指令标注它来自哪个 IR 语句/Pass（`ConvertTensorToTileOps` 合成的 load/store 也能从 `loc()` 认出源行）；
   - 找到 `alloc_tile` 的 `compact=1` 属性（若 M 收窄生效）与 `addr` 属性，说明它们各自由哪一环决定（推断器 / 内存规划 Pass）；
   - 在 Cube 侧圈出「扩形 set_validshape → tpush_to_aiv → 还原 set_validshape」三明治，核对扩形条**只扩列不扩行**。
3. 把 M 的收窄去掉重编一次，diff 两份 `.pto`：观察 `compact=1` 消失、传输 `set_validshape` 的行操作数变成物理盒行数。
4. 最后回答一个问题并写进笔记：如果把这个核包进 `pl.split(UP_DOWN)`，编译会发生什么？为什么这是正确的行为？（答案在 4.4：pitch 不重合的 compact 累加器过分裂边界会被 `CHECK_SPAN` 拒绝，错误信息给两条出路。）

## 6. 本讲小结

- **严格 1:1 映射**是 PTOCodegen 的宪法：只做节点翻译、类型格式转换与 SSA 簿记，分析与优化全属上游 Pass；产物按「常量 → tensor_view → alloc_tile → 函数体」固定顺序生成。
- **遍历与算子知识分离**：`PTOCodegen` 管遍历、命名与结果绑定（`AssignStmt` 先定 `current_result_buf` 再访问右值），每个 `tile.*` 的指令形态住在后端注册表（`kSimpleOps` 表 + 自定义发射器），`ins/outs` 子句由 `GenerateInsOutsClause`/`EmitInsOuts` 统一拼装；操作数是 MemRef 决定的缓冲 SSA 句柄，地址在 `alloc_tile` 的属性里。
- **控制流指令化**：`ForStmt → scf.for`（正步长检查是永久性用户错误拦截）、`IfStmt → scf.if`；Tile 因原地写语义不进 iter_args/scf 结果位，靠共享 MemRef 句柄跨轮跨分支；标量表达式直译 `arith.*`。
- **`init_cond` 的指令层形态**是「两条指令、一个分支」：字面量谓词编译期选边，运行期谓词降级为无 phi 的 `scf.if` 双臂（两臂都原地写 dst）；本次更新后 `gemv_acc` 与 `matmul_acc` 共用同一路径。
- **C2V pitch 规则**：`mad` 按 \( \lceil validRow/16 \rceil \cdot 16 \) 写 L0C，无分裂 Acc→Vec 推送因此**保行扩列**（行数以外本就承诺陈旧）；分裂推送对 pitch 不重合的 compact 累加器**拒绝编译**（`AccPitchesCoincide` 判定，单 fractal 块豁免），消费者侧用 `treshape` 恢复逻辑形。
- **`loc(...)` 源位置**让 ptoas 的诊断指回用户 `.py` 行：语句 span 为主、Call span 经包含测试精化，Pass 重建的粗 span 会被过滤。

## 7. 下一步学习建议

- **u6-l2（编排代码生成）**：看另一半 codegen——host 编排函数如何生成 PTO2 运行时 C++（`rt_submit_task` 等），`Submit` 的任务输出与 TaskId 如何在编排产物中表达；与本讲对照「设备侧 MLIR 文本 vs 主机侧 C++ 文本」两种产物形态。
- **u6-l3（后端抽象与 BackendHandler）**：本讲反复出现的 `backend_->GetOpInfo`/`GetHandler()` 在那里的全景——910B/950 各自的发射器注册如何覆盖 `kSimpleOps`，`pto.target_arch` 从哪来。
- **u6-l6（PTO ISA 与编译产物）**：`.pto` 文本如何被 ptoas 汇编成 ELF、`TPUSH/TPOP` 的 buffer 管理细则，以及 `--pto-level=level2/3` 与三种 `memory_planner` 模式的对接。
- 想动手的读者可以直接做一次「发射器手术」：在 `kSimpleOps` 表里加一行假想算子、跑通编译，体会 u7-l8 全栈新增算子的 codegen 一环有多薄；再读 [tests/ut/codegen/test_pto_codegen_cross_core.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_pto_codegen_cross_core.py) 的四个 pitch 用例，把它们改成断言失败，观察错误信息如何指向 span。
