# u4-l7 IR 构建、打印与解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 不写任何 DSL 语法，直接用 `IRBuilder` API（Python 包装层或 C++ 层）从零构造出一个完整的 `ir.Function`，包括参数、返回类型、赋值与控制流。
2. 说清 `python_print` 把一条 `Call` 打印回 Python 文本的三条路径，以及「IR 是数据、DSL 是语法」这一不对齐为什么催生了一批关键字化打印特例。
3. 解释 `init_cond` 谓词在三个累加算子上的打印差异：`tile.matmul_acc` 按位置参数打印，而 `tensor.matmul_acc` 与 `tile.gemv_acc` 必须按 `init_cond=` 关键字打印——后者是 HEAD `ec5d20c`（PR #2528）刚补齐的行为，本讲是按这一版源码更新的。
4. 独立完成「构造 IR → 打印 → 再解析 → 结构化比较」的往返（round-trip）实验，用它验证打印器与解析器互为逆变换。

## 2. 前置知识

- **Builder（建造者）模式**：不一次性 new 出完整对象，而是先开一个"上下文"，往里逐条累积内容，最后关闭上下文拿到成品。PyPTO 的 `IRBuilder` 用一个**上下文栈**管理"当前正在建什么"（函数？循环？if 分支？），保证嵌套结构合法。
- **Span（源位置）**：每个 IR 节点都带一个 `Span`，记录它来自哪个文件哪一行。它不影响语义，但决定报错信息能否指到你的源码（u4-l1 已介绍）。
- **操作数 vs 关键字参数**：IR 的 `Call` 节点把所有输入平铺存在 `args_` 列表里（纯数据，只有位置没有名字）；而 Python DSL 函数签名区分位置参数和关键字参数（语法）。同一个语义在两边可以"错位"——比如 IR 里 `args_[3]` 是谓词，DSL 签名的第 4 个位置参数却是另一个东西。打印器必须消解这种错位，否则打出来的文本解析不回去。
- **结构化相等（structural equality）**：`assert_structural_equal` 比较两份 IR 的逻辑结构，**忽略 Span 与变量名**（见 [docs/en/dev/ir/03-structural_comparison.md:L14](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/03-structural_comparison.md#L14)）。它是往返实验的裁判，细节在 u4-l8 展开。
- 建议先完成 u4-l6（算子注册表）：你知道了 `Call` 的 `op_` 来自 `OpRegistry`，本讲就回答"那 `Call` 本身从哪来、又怎么变回文本"。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [src/ir/builder.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp) | C++ `IRBuilder` 实现：上下文栈、Begin/End 配对、校验 |
| [include/pypto/ir/builder.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/builder.h) | C++ Builder 接口声明与各 Context 类 |
| [python/pypto/ir/builder.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py) | Python 包装层：`with` 上下文管理器、自动 span、类型助手 |
| [src/ir/transforms/python_printer.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp) | `IRPythonPrinter`：IR → Python 文本，本讲精读其 `Call` 打印路径 |
| [python/pypto/ir/printer.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/printer.py) | `python_print` 的 Python 入口（薄封装，按类型分发到 C++） |
| [src/ir/op/tile_ops/matmul.cpp](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp) | `tile.gemv_acc` 的算子注册：`init_cond` 注册为第 4 个操作数 |
| [python/pypto/ir/op/tile_ops.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py) | IR 层算子封装：`gemv_acc(acc, lhs, rhs, init_cond=...)` 拼 `args_` |
| [python/pypto/language/op/tile_ops.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py) | DSL 层 `pl.tile.gemv_acc` 签名：`init_cond` 为 keyword-only |
| [tests/ut/ir/printing/test_python_printer.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/printing/test_python_printer.py) | 打印-再解析往返测试，含 `init_cond` 三形态断言 |
| [tests/ut/ir/high_level/test_builder.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/high_level/test_builder.py) | Builder API 单元测试，实践的参照物 |
| [docs/en/dev/ir/06-builder.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/06-builder.md) / [07-parser.md](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/07-parser.md) | 官方 Builder / 解析器文档 |

## 4. 核心概念与源码讲解

### 4.1 C++ IRBuilder：上下文栈如何一步步长出一个函数

#### 4.1.1 概念说明

到这里为止，你见过的 IR 都来自 DSL 解析：`@pl.function` 的 Python 源码经 AST 解析变成 `ir.Function`。但还有第二条路——**直接手工构造**。这条路服务于三类场景：

1. **Pass 测试与教学**：想测一个 Pass，最省事的方式是手工搭一段最小 IR，而不是写一段 DSL 再绕过解析。
2. **解析器自身**：AST 解析器把 Python 语句翻译成 IR 时，底层正是复用这套 Builder（见 u3-l1 的调用链）。
3. **代码生成器**：如 `pypto.debug.torch_codegen` 这类把别的框架算子翻译成 PyPTO IR 的工具。

C++ `IRBuilder` 的核心设计是**上下文栈 + Begin/End 配对**：进入函数压入 `FunctionContext`，进入循环压入 `ForLoopContext`……每条语句 `Emit` 到栈顶上下文；`EndXxx` 弹栈并把累积的内容打包成不可变的 IR 节点。这保证了三条硬约束：不允许嵌套函数、循环的 iter_arg 与 return_var 数量必须相等、语句必须落在正确的上下文里。

#### 4.1.2 核心流程

构造一个函数的时序：

```text
BeginFunction(name, span, type, ...)
  ├── 栈空校验：不允许在函数里再开函数
  └── 压入 FunctionContext
FuncArg(name, type, span, direction)     # 每个参数：造一个 Var 挂到参数表
ReturnType(type)                          # 声明返回类型
  ├── Var / Assign / Emit ...             # 函数体逐条累积在上下文里
  └── （循环/If 同理：Begin → 累积 → End）
EndFunction(end_span)
  ├── 函数体语句打包：1 条直接用，多条包成 SeqStmts
  ├── 合并 begin/end 两个 span 成跨行 span
  ├── new Function(params, directions, return_types, body, span, ...)
  └── 弹栈，返回不可变的 FunctionPtr
```

注意"1 条语句不包 SeqStmts"这个细节：它让最简单的函数体就是那条语句本身，打印输出也更干净。

#### 4.1.3 源码精读

嵌套函数检查——Builder 的校验哲学是"当场报错，绝不默默修复"：

- [src/ir/builder.cpp:L41-L53](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp#L41-L53)：`BeginFunction` 入口。若已在函数内，抛 `pypto::RuntimeError`，错误信息带上外层函数名和它的 begin span，让你能定位到"是在哪儿嵌套的"。

参数与返回类型的累积：

- [src/ir/builder.cpp:L55-L67](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp#L55-L67)：`FuncArg` 为每个参数造一个 `Var` 并连同方向（`ParamDirection`，默认 `In`）挂进当前 `FunctionContext`；`ReturnType` 追加返回类型。

收尾打包：

- [src/ir/builder.cpp:L88-L112](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp#L88-L112)：`EndFunction`。第 94-95 行做"单语句直用、多语句包 `SeqStmts`"的分流；第 97-100 行把 begin/end 两个 span 合成一个覆盖整个函数的跨行 span；第 103-106 行以不可变方式构造 `Function` 并弹栈。

最小单元工厂：

- [src/ir/builder.cpp:L518-L526](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp#L518-L526)：`Assign` 造赋值语句并入栈顶上下文；`Var` 就是 `std::make_shared<ir::Var>(name, type, span)` 的一行封装——Builder 不发明新节点，只保证节点落在正确的上下文里。

#### 4.1.4 代码实践

**实践目标**：跑通项目自带的 Builder 冒烟测试，确认 C++ 层行为，再对照文档例子读懂每一步对应哪个 Begin/End。

**操作步骤**（环境按 u1-l2 搭好，构建产物可导入 `pypto`）：

1. 打开 [tests/ut/ir/high_level/test_builder.py:L20-L42](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/high_level/test_builder.py#L20-L42)，阅读 `test_simple_function_with_auto_span`：建函数 → 两个参数 → 返回类型 → `ib.var` + `ib.assign` → `f.get_result()`。
2. 运行它（按项目规则先 `source .claude/skills/testing/load-env.sh`，再显式传并行度）：

   ```bash
   python -m pytest tests/ut/ir/high_level/test_builder.py -v -n "$PYPTO_TEST_JOBS"
   ```

3. 对照 [docs/en/dev/ir/06-builder.md:L122-L157](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/06-builder.md#L122-L157) 的 C++ 版 `sum_to_n` 例子，把每个 `BeginXxx`/`EndXxx` 与 Python 测试里的 `with` 语句一一配对，写成两列对照表。

**需要观察的现象**：测试全绿；C++ 例子里 `ib.BeginForLoop(...)` 对应 Python 的 `with ib.for_loop(...)`，`ib.EndForLoop` 没有 Python 对应物（由 `with` 块退出隐式完成）。

**预期结果**：`test_simple_function_with_auto_span` 通过，能拿到 `func.name == "my_func"`、两个参数、一个返回类型。若尚未构建环境，此步**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EndFunction` 里"1 条语句不包 SeqStmts"是个值得写进实现的细节，而不只是省一个节点？

**答案**：函数体是单个语句还是 `SeqStmts` 包裹的单语句，是两种不同的 IR 结构。若总是包裹，所有 Pass 测试的期望 IR 都得多一层 `SeqStmts`，打印输出也多一层缩进；直用单语句让最小函数的 IR 与直觉一致，也减少下游 Pass 需要匹配的形态数。

**练习 2**：如果在一个函数上下文里直接 `BeginFunction` 开第二个函数，会发生什么？错误信息里为什么带 span？

**答案**：抛 `pypto::RuntimeError`（[src/ir/builder.cpp:L45-L49](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/builder.cpp#L45-L49)）。带 span 是因为嵌套错误几乎总是"我以为这段代码在函数外面"造成的，外层函数的 begin span 直接指出外层函数从哪一行开始，帮作者定位误解来源。

---

### 4.2 Python 包装层：with 语句、自动 span 与类型助手

#### 4.2.1 概念说明

C++ 层要求每个方法显式传 `Span`，Python 层则把这套 API 包成**上下文管理器**：`with ib.function(...) as f:` 进入时调 `begin_function`，退出时自动调 `end_function`。这层包装解决三件事：

1. **自动 span 捕获**：用 `inspect` 沿调用栈回退两帧，拿到**用户代码**的文件名与行号，免去手工传 span。
2. **跨行 span 合成**：`with` 进入和退出各捕一次 span，合并后得到覆盖整个构造过程的区间。
3. **类型助手**：`memref()` / `tile_view()` / `tile_type()` / `tensor_type()` 把 int 自动规范化成 `ConstInt` 表达式，省去手写 `ir.ConstInt(0, DataType.INT64, span)` 的样板。

#### 4.2.2 核心流程

`with ib.function("f") as f:` 的生命周期：

```text
__enter__ 路径（生成器 yield 前）
  begin_span = _capture_call_span()          # 用户代码的 with 行
  self._builder.begin_function(...)
  yield FunctionBuilder(self)                # with 体执行
__exit__ 路径（生成器 finally）
  end_span = _capture_call_span()
  combined  = _combine_spans(begin, end)     # 起止行合成区间
  result    = self._builder.end_function(combined)
```

`_capture_call_span` 的栈帧回退：

```text
frame 0 = _capture_call_span
frame 1 = 包装方法（function/var/assign/...）
frame 2 = 用户代码   ← 取这一帧的 filename + lineno
```

#### 4.2.3 源码精读

- [python/pypto/ir/builder.py:L57-L107](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L57-L107)：`function()` 上下文管理器全貌。`try/finally` 保证 with 体抛异常也会走 `end_function`；注意它不像 `for_loop` 那样抑制 end 阶段的二次异常（函数没有"结构数量必须相等"一类的收尾校验，二次异常不会掩盖真因）。

- [python/pypto/ir/builder.py:L864-L880](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L864-L880)：`_capture_call_span`——`frame.f_back.f_back` 回退两帧，`ir.Span(info.filename, info.lineno, -1)` 造 span。

- [python/pypto/ir/builder.py:L882-L898](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L882-L898)：`_combine_spans`——取 begin 的文件/起点与 end 的终点，合成多行 span。与 C++ `EndFunction` 里的合成逻辑（builder.cpp:L97-L100）一一对应。

- [python/pypto/ir/builder.py:L160-L180](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L160-L180)：`for_loop` 的 `body_ok` 抑制——若 with 体本身抛了错，`end_for_loop` 的结构校验（如"N 个 iter_arg 但 0 个 return_var"）只是**症状**，重新抛出会替换掉带用户 span 的真诊断，因此被吞掉。这是"错误信息质量优先"的一个好范例。

- [python/pypto/ir/builder.py:L428-L510](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L428-L510)：`let()`——"造变量 + 赋值"一步完成，并做类型推断/覆写校验（覆写只允许同 kind，如 TensorType→TensorType），还能对 `GlobalVar` 调用自动从函数签名回填返回类型。

- [python/pypto/ir/builder.py:L913-L949](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L913-L949)：`FunctionBuilder.param/return_type/get_result`——参数方向默认 `In`，可传 `direction=ir.ParamDirection.Out` 等（呼应 u4-l5 的参数方向体系）。

#### 4.2.4 代码实践

**实践目标**：验证自动 span 捕获确实指向**你的**代码行，而不是 builder.py 内部。

**操作步骤**（以下为示例代码）：

```python
# span_probe.py —— 示例代码
from pypto import ir
from pypto.ir import IRBuilder

ib = IRBuilder()
with ib.function("probe") as f:
    x = f.param("x", ir.ScalarType(ir.DataType.INT64))
    y = f.param("y", ir.ScalarType(ir.DataType.INT64))
    f.return_type(ir.ScalarType(ir.DataType.INT64))
    r = ib.let("r", ir.Add(x, y, ir.DataType.INT64, ir.Span.unknown()))

func = f.get_result()
print(func.span.filename, func.span.begin_line, func.span.end_line)
print(r.span.filename, r.span.begin_line)
```

运行 `python span_probe.py`。

**需要观察的现象**：`func.span` 覆盖 `with` 块的起止行（比如 begin_line 是 `with` 那行、end_line 是块末行）；`r.span.begin_line` 是 `ib.let` 所在行；两者 filename 都是 `span_probe.py`。

**预期结果**：span 全部指向用户文件与用户行号。若把 `ib.let` 挪到别的行，`r.span.begin_line` 跟着变。**待本地验证**（脚本为本讲新写，行为依据 [python/pypto/ir/builder.py:L864-L880](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L864-L880) 的栈帧回退逻辑推断）。

#### 4.2.5 小练习与答案

**练习 1**：`_capture_call_span` 为什么恰好回退两帧？三帧会取到谁？

**答案**：frame 0 是 `_capture_call_span` 自身，frame 1 是调用它的包装方法（`var`/`assign`/`function`…），frame 2 才是用户代码。回退三帧会跳过用户代码，取到调用用户的更外层（比如 `__main__` 模块帧或测试框架帧），span 就指错文件了。

**练习 2**：`for_loop` 的 `finally` 里为什么要有 `body_ok` 判断，而 `function` 不需要？

**答案**：`end_for_loop` 会做"iter_arg 数与 return_var 数相等"的结构校验；with 体中途抛异常时该校验必然失败，但其报错只是真异常的症状，重抛会掩盖真因，故吞掉（[python/pypto/ir/builder.py:L160-L180](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py#L160-L180)）。`end_function` 没有这类收尾校验，不会产生误导性的二次异常，无需处理。

---

### 4.3 IRPythonPrinter：Call 的打印主流程与 init_cond 的关键字化特例

#### 4.3.1 概念说明

`python_print`（Python 入口在 [python/pypto/ir/printer.py:L15-L46](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/printer.py#L15-L46)，按输入类型分发到 C++ `python_print` / `python_print_type`）把 IR 反向打印成合法的 Python DSL 文本。它存在的意义：dump 调试（u3-l5 的 `dump_passes` 导出的就是打印文本）、`.pto` 之外的文本交换、以及**往返**——打出来的文本必须能被解析器原样读回。

打印一条 `Call` 有三条路径：

1. 被调者是 `GlobalVar`（跨函数调用）→ 打成 `self.method_name(...)`；
2. 被调者是注册算子（名字带点）→ 打成 `pl.tile.gemv_acc(...)` / `pl.tensor.add(...)` 形式（`pld.` 前缀的分布式算子裸打）；
3. 其它 → 按名字裸打。

核心矛盾在路径 2：**IR 是纯位置数据（`args_` 列表），DSL 是带关键字语法的方法签名**。当某个操作数在 IR 里的位置与它在 DSL 签名里的位置参数槽**错位**时，照位置打印就会产出解析不回去（甚至语义错乱）的文本。打印器为此维护了一组"关键字化特例"。本 HEAD 上最重要的一个就是 `init_cond`：

| DSL 签名 | 第 4 个位置槽归谁 | `init_cond` 打印形态 |
| -------- | ---------------- | -------------------- |
| `pl.tile.matmul_acc(acc, lhs, rhs, init_cond=None)` | `init_cond` 自己 | 位置参数：`pl.tile.matmul_acc(acc, lhs, rhs, k0 == 0)` |
| `pl.tensor.matmul_acc(acc, lhs, rhs, a_trans, b_trans, init_cond)` | `a_trans` | 关键字：`..., init_cond=k0 == 0, a_trans=False, b_trans=False` |
| `pl.tile.gemv_acc(acc, lhs, rhs, acc_phase, *, init_cond)` | `acc_phase` | 关键字：`..., init_cond=k0 == 0, acc_phase='unspecified'` |

前两行是既有行为；**第三行是本次更新新增**：HEAD `ec5d20c`（PR #2528）给 `tile.gemv_acc` 补上了与 `matmul_acc` 相同的 `init_cond` 谓词，同时把它纳入关键字化打印。若按位置打印，谓词会绑到 `acc_phase`（字符串参数）上，解析必然报错。

#### 4.3.2 核心流程

`VisitExpr_(const CallPtr&)` 对注册算子的打印流程：

```text
1. GlobalVar? → self.fn(...) 路径，提前分流
2. 算子名打印：pl.<ns>.<op>( / pld.<op>( 裸打 / NeedsDslUnderscore 修正（tile.and → tile.and_）
3. 特殊整Call早退：reinterpret_view / full / syncall（各自重排参数）
4. 关键字化判定：
     gather_row_kw_valid  = (tile|tensor).gather_row 且 args_.size() == 6
     acc_kw_init_cond     = (tensor.matmul_acc || tile.gemv_acc) 且 args_.size() == 4
     mgather 系列…
5. 位置参数循环：跳过被关键字化的下标（init_cond 是 args_[3]）
6. 关键字段输出：valid_shape=… / init_cond=… / scratch=… / deps=[…]
7. kwargs_ 逐个按键值类型打印（枚举还原成 pl.Xxx.Yyy 成员）
8. print_serialized_attrs：机器专属 attrs 进开放世界 denylist 的 attrs={...}
```

`init_cond` 在 IR 与 DSL 两层的存放差异（构造侧）：

```text
IR 层 gemv_acc 封装：
  init_cond is None → args = [acc, lhs, rhs]          # 3 个操作数
  init_cond 给定    → args = [acc, lhs, rhs, init_cond] # 谓词追加为第 4 个操作数
打印层：
  args_.size() == 4 且算子属于关键字化集合 → 跳过位置循环中的 args_[3]
                                          → 在关键字段打印 "init_cond=" + args_[3]
```

#### 4.3.3 源码精读

**打印入口与 GlobalVar 分流**：

- [src/ir/transforms/python_printer.cpp:L941-L948](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L941-L948)：`VisitExpr_(CallPtr)` 入口；`GlobalVar` 被调者在 Program 上下文中打印为 `self.method_name(`——这解释了为什么打印出的多函数程序是 `@pl.program` 类形态（u4-l5）。

**算子名与前缀**：

- [src/ir/transforms/python_printer.cpp:L1080-L1098](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1080-L1098)：名字带点即注册算子；`pld.` 前缀裸打（分布式命名空间），其余加 `pl.` 前缀；`NeedsDslUnderscore` 把 Python 关键字冲突名（如 `tile.and`）补成 `tile.and_`。

**机器属性的兜底**：

- [src/ir/transforms/python_printer.cpp:L1110-L1140](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1110-L1140)：`print_serialized_attrs` 用**开放世界 denylist** 把所有没有专属 DSL 表面的 `attrs_` 键收进尾部的 `attrs={...}` 字典——新加的 attr 永远不会被静默丢弃，没有编解码器的值类型会当场报错。这是往返不丢元数据的保障。

**init_cond 关键字化判定（本次更新的落点）**：

- [src/ir/transforms/python_printer.cpp:L1241-L1258](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1241-L1258)：注释逐条说明两个关键字化算子的原因——`tensor.matmul_acc` 的谓词按位打印会重解析成转置旗标再与 `a_trans=` 冲突；`tile.gemv_acc` 的谓词会绑到同样以关键字打印的 `acc_phase` 上。`acc_kw_init_cond` 的条件是 `(IsOp(op, "tensor.matmul_acc") || IsOp(op, "tile.gemv_acc")) && op->args_.size() == 4`。`tile.matmul_acc` 的谓词占第 4 位置槽，无需修整。相对上一版（`c7ba9fb`），这段从只匹配 `tensor.matmul_acc` 扩展为两个算子。

**位置循环与关键字输出**：

- [src/ir/transforms/python_printer.cpp:L1260-L1279](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1260-L1279)：位置参数循环；`acc_kw_init_cond && i == 3` 时 `continue`，把谓词从位置序列里摘出。
- [src/ir/transforms/python_printer.cpp:L1288-L1292](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1288-L1292)：以 `", init_cond="` 前缀打印 `args_[3]`，落在 `acc_phase='unspecified'` 这类 kwarg 之前。

**构造侧：init_cond 为何是操作数**（承接 u4-l6 的注册表视角）：

- [src/ir/op/tile_ops/matmul.cpp:L482-L503](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L482-L503)：`tile.gemv_acc` 注册为 4 个操作数（`acc/lhs/rhs/init_cond`），`acc_phase` 是 attr；`set_output_reuses_input(0)` 与 `ArgEffect::ReadWrite` 声明它原地累加进 `acc`。
- [python/pypto/ir/op/tile_ops.py:L2002-L2035](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L2002-L2035)：IR 层封装——`init_cond` 给定时 `args` 长成 4 个元素，经 `create_op_call("tile.gemv_acc", args, {"acc_phase": ...}, span)` 建 `Call`。`create_op_call` 的两个重载签名见 [python/pypto/pypto_core/ir.pyi:L3034-L3060](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/pypto_core/ir.pyi#L3034-L3060)。

**三个 DSL 签名对照**（打印差异的根源）：

- [python/pypto/language/op/tile_ops.py:L1292-L1302](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1292-L1302)：`pl.tile.matmul_acc(acc, lhs, rhs, init_cond=None)`——`init_cond` 占第 4 位置槽，**按位打印正确**。
- [python/pypto/language/op/tensor_ops.py:L677-L684](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tensor_ops.py#L677-L684)：`pl.tensor.matmul_acc(acc, lhs, rhs, a_trans=False, b_trans=False, init_cond=None)`——`a_trans` 占第 4 槽，谓词必须关键字化。
- [python/pypto/language/op/tile_ops.py:L1449-L1478](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1449-L1478)：`pl.tile.gemv_acc(acc, lhs, rhs, acc_phase="unspecified", *, init_cond=None)`——签名里 `*` 之后 `init_cond` 是 keyword-only，docstring 明说"`init_cond` is keyword-only because `acc_phase` already owns the fourth positional slot"。

#### 4.3.4 代码实践

**实践目标**：亲眼看到同一份 IR 里三个累加算子调用的三种 `init_cond` 打印形态。

**操作步骤**：

1. 打开 [tests/ut/ir/printing/test_python_printer.py:L1750-L1809](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/printing/test_python_printer.py#L1750-L1809) 的 `test_acc_init_cond_print_parse_roundtrip`，阅读它的源程序（三个函数分别用 `tensor.matmul_acc` / `tile.matmul_acc` / `tile.gemv_acc`，谓词都是 `k0 == 0`）与三行断言。
2. 运行：

   ```bash
   python -m pytest tests/ut/ir/printing/test_python_printer.py::test_acc_init_cond_print_parse_roundtrip -v
   ```

3. 想直接看打印文本，可跑最小脚本（示例代码，取自该测试的单函数裁剪版）：

   ```python
   # acc_print_probe.py —— 示例代码
   import textwrap
   import pypto.language as pl
   from pypto import ir
   from pypto.ir import python_print

   src = textwrap.dedent("""\
       @pl.program
       class P:
           @pl.function(type=pl.FunctionType.InCore)
           def tile_gemv_acc(
               self,
               acc: pl.Tile[[16, 64], pl.FP32, pl.Mem.Acc, pl.TileView(valid_shape=[1, 64])],
               lhs: pl.Tile[[1, 128], pl.FP16, pl.Mem.Mat],
               rhs: pl.Tile[[128, 64], pl.FP16, pl.Mem.Mat],
               k0: pl.Scalar[pl.INDEX],
           ) -> pl.Tile[[16, 64], pl.FP32, pl.Mem.Acc, pl.TileView(valid_shape=[1, 64])]:
               return pl.tile.gemv_acc(acc, lhs, rhs, init_cond=k0 == 0)
   """)
   printed = python_print(pl.parse_program(src), format=False)
   print(printed)
   ```

**需要观察的现象**：打印文本中出现 `pl.tile.gemv_acc(acc, lhs, rhs, init_cond=k0 == 0, acc_phase='unspecified')`——谓词带 `init_cond=` 前缀，`acc_phase` 以字符串 kwarg 跟在后面；对照 `tile.matmul_acc` 版本则是 `pl.tile.matmul_acc(acc, lhs, rhs, k0 == 0)`（裸位置）。

**预期结果**：步骤 2 测试通过（该用例是项目 CI 既有断言，三行期望文本见测试 L1803-L1805）；步骤 3 输出含上述关键字形态。若本地环境未就绪，步骤 3 **待本地验证**，但期望文本以测试断言为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tile.matmul_acc` 不需要关键字化修整，而 `tensor.matmul_acc` 需要？从两者的 DSL 签名说出关键差别。

**答案**：`tile.matmul_acc` 的 DSL 签名第 4 个位置参数就是 `init_cond`（[python/pypto/language/op/tile_ops.py:L1292](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1292)），IR 里 `args_[3]` 按位打印恰好落回正确参数；`tensor.matmul_acc` 的第 4 槽被 `a_trans` 占据（[python/pypto/language/op/tensor_ops.py:L677-L684](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tensor_ops.py#L677-L684)），按位打印的谓词会重解析成转置旗标并与 `a_trans=` 关键字冲突。

**练习 2**：`gemv_acc` 的判定条件里 `op->args_.size() == 4` 起什么作用？去掉它会怎样？

**答案**：`args_.size() == 4` 表示"谓词操作数存在"（3 个必选 + 1 个可选谓词，见 [python/pypto/ir/op/tile_ops.py:L2034](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/op/tile_ops.py#L2034)）。去掉它后，无谓词的 3 操作数调用也会走 `i == 3` 的跳过逻辑——虽然循环里 `i` 达不到 3 不会出错，但关键字段的 `init_cond=` 打印若不与 size 绑定，在边界改动时容易引用越界的 `args_[3]`；条件同时是"操作数在场"的守卫和文档。

**练习 3**：如果要新加一个"第 4 位置槽已被占用"的累加类算子（比如 `tile.batch_gemv_acc`），打印器要改哪几处？

**答案**：三处——(1) [python_printer.cpp:L1252-L1253](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L1252-L1253) 的 `acc_kw_init_cond` 条件加进新算子名；(2) 确认位置循环的跳过下标（新算子谓词若仍在 `args_` 尾部则复用 `i == 3` 或按实际下标调整）；(3) 给打印-再解析测试补一条断言（仿照 test L1803-L1805）。更好的做法是先检查能否复用现有条件，避免特例清单无限膨胀。

---

### 4.4 打印 → 再解析：往返一致性实验

#### 4.4.1 概念说明

打印器的验收标准不是"人看着像"，而是**往返**：`parse(print(IR))` 与 `IR` 结构化相等，且 `print(parse(print(IR)))` 与第一次打印逐字符相同。前者证明没丢语义，后者证明没引入非确定性（变量名、参数顺序、格式）。

从 u3-l1 已知：整程序级的完美往返仍有例外（`CommDomainScopeStmt` 打成注释、window_buffer 回引不打印），所以相关测试固定用 BEFORE_AND_AFTER 模式在**每个 Pass 之前**打印。但对本讲关注的算子调用层（含 `init_cond` 三形态），往返是完整成立的，且有专门测试守护。

#### 4.4.2 核心流程

```text
src(DSL 源码)
  → pl.parse_program          # 解析：语法 → IR
  → python_print(format=False) # 打印：IR → 文本①
  → pl.parse_program(文本①)    # 再解析：文本① → IR'
  → ir.assert_structural_equal(IR, IR')   # 语义没丢（忽略 span/名字）
  → python_print(IR') == 文本①            # 文本稳定
```

`format=False` 用于断言逐字符相等：关掉注册的格式化回调，输出只由 IR 内容决定。

#### 4.4.3 源码精读

- [tests/ut/ir/printing/test_python_printer.py:L1800-L1809](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/printing/test_python_printer.py#L1800-L1809)：`test_acc_init_cond_print_parse_roundtrip` 的收尾三行正是上面流程图的落地——`parse_program(printed)` → `assert_structural_equal` → 再打印比对。L1803-L1805 的三行断言固定了三种 `init_cond` 形态。

- [python/pypto/ir/printer.py:L15-L46](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/printer.py#L15-L46)：`python_print` 统一入口。`prefix="pl"` 对应 `import pypto.language as pl`；`concise=True` 省略中间类型注解；`explicit_layout=True` 打出每个 tile 的完整布局（自描述输出）。

- [docs/en/dev/ir/07-parser.md:L72-L92](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/07-parser.md#L72-L92)：文本入口 `pl.parse(code)` / `pl.loads(path)`，自动识别 function/program——再解析这一步用的就是它们（`pl.parse_program` 是其按 Program 的特化，旧别名见文档说明）。

- [docs/en/dev/ir/03-structural_comparison.md:L14](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/03-structural_comparison.md#L14)：结构化相等忽略 `Span`——这正是 Builder 产物与 DSL 产物可以判等的根据（两者的 span 必然不同：一个来自用户 with 行，一个来自 DSL 源码行）。

#### 4.4.4 代码实践

**实践目标**：亲手做一次最小往返，体会"文本稳定"的含义。

**操作步骤**（示例代码）：

```python
# roundtrip_probe.py —— 示例代码
import textwrap
import pypto.language as pl
from pypto import ir
from pypto.ir import python_print

src = textwrap.dedent("""\
    @pl.function
    def add(x: pl.Tensor[[128, 128], pl.FP32], y: pl.Tensor[[128, 128], pl.FP32]) -> pl.Tensor[[128, 128], pl.FP32]:
        result: pl.Tensor[[128, 128], pl.FP32] = pl.add(x, y)
        return result
""")

f1 = pl.parse(src)                      # 或 pl.parse_program 视产物而定
t1 = python_print(f1, format=False)
f2 = pl.parse(t1)
t2 = python_print(f2, format=False)

ir.assert_structural_equal(f1, f2)
assert t1 == t2
print(t1)
```

**需要观察的现象**：断言全部通过；`t1` 是规范化的 DSL 文本（注解用下标记法、`pl.add` 展开为注册算子调用形态）。

**预期结果**：`assert_structural_equal` 与 `t1 == t2` 均成立。**待本地验证**（脚本为本讲新写；单函数 `pl.parse` 的用法见 [docs/en/dev/ir/07-parser.md:L78-L87](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/07-parser.md#L78-L87)，等价模式以项目测试 L1807-L1809 为准）。

#### 4.4.5 小练习与答案

**练习 1**：为什么断言要同时做 `assert_structural_equal(f1, f2)` 和 `t1 == t2`，只做其中一个不够吗？

**答案**：不够。只做结构化相等：无法发现打印引入的**不确定性**（比如按遍历顺序打印 kwargs），只要语义在就通过，但文本不稳定会让 diff 工具与缓存失效。只做文本相等：理论上可能两份文本相同但都错了（打印器与解析器犯了镜像的错误，文本一致地丢失信息）。两个断言分别锁定语义与文本两层。

**练习 2**：往返实验里为什么用 `format=False`？

**答案**：`format=True`（默认）会应用注册的格式化回调，可能做换行/排版美化；逐字符比较需要输出完全由 IR 内容决定，关掉格式化后 `t1 == t2` 才是强断言。项目测试（L1801、L1809）同样传 `format=False`。

---

## 5. 综合实践

把本讲三块内容（Builder 构造、打印、再解析）串成一个实验。分两段：

**第一段：Builder 版"hello world"与 DSL 版判等**

hello world 的计算语义是 `c = a + b`（[128,128] FP32，见 u1-l4）。不用 DSL 语法构造它的 Tensor 级等价函数，并与 DSL 版做结构化比较（示例代码）：

```python
# builder_vs_dsl.py —— 示例代码
import textwrap
import pypto.language as pl
from pypto import DataType, ir
from pypto.ir import IRBuilder, python_print

# DSL 版（参照物）
dsl_src = textwrap.dedent("""\
    @pl.function
    def add(x: pl.Tensor[[128, 128], pl.FP32], y: pl.Tensor[[128, 128], pl.FP32]) -> pl.Tensor[[128, 128], pl.FP32]:
        result: pl.Tensor[[128, 128], pl.FP32] = pl.add(x, y)
        return result
""")
dsl_func = pl.parse(dsl_src)

# Builder 版（不经任何 DSL 语法）
ib = IRBuilder()
with ib.function("add", type=ir.FunctionType.Opaque) as f:
    t = ib.tensor_type([128, 128], DataType.FP32)
    x = f.param("x", t)
    y = f.param("y", t)
    f.return_type(t)
    call = ir.create_op_call("tensor.add", [x, y], ir.Span.unknown())
    r = ib.let("result", call)
    ib.return_stmt(r)
builder_func = f.get_result()

ir.assert_structural_equal(builder_func, dsl_func)
print(python_print(builder_func))
print(python_print(dsl_func))
```

观察两份打印文本：逐行对比参数注解、函数类型、`pl.tensor.add(x, y)` 调用与 return 语句，确认除 span 外完全同构。判等成立的关键就是结构化比较忽略 span 与名字（[docs/en/dev/ir/03-structural_comparison.md:L14](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/03-structural_comparison.md#L14)）。`@pl.function` 默认类型与 `ib.function` 默认值一致，均为 `Opaque`（[python/pypto/language/parser/decorator.py:L339-L343](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/decorator.py#L339-L343)）。

**第二段：带 init_cond 的 gemv_acc——构造、关键字打印、往返**

1. 先用 4.3.4 的脚本（或直接跑项目测试）确认打印形态是 `pl.tile.gemv_acc(acc, lhs, rhs, init_cond=k0 == 0, acc_phase='unspecified')`。
2. 挑战：改用 Builder 路径构造同一个函数——`ib.function(type=ir.FunctionType.InCore)`、用 `ib.tile_type(..., memref=..., memory_space=...)` 声明三个 tile 参数与 `Scalar[INDEX]` 参数 `k0`，谓词用 `ir.Eq(k0, ir.ConstInt(0, ir.DataType.INDEX, ir.Span.unknown()), ...)` 造 Bool 表达式，再经 `ir.create_op_call("tile.gemv_acc", [acc, lhs, rhs, pred], {"acc_phase": "unspecified"}, span)` 建 Call、`ib.return_stmt` 返回。
3. 对 Builder 版做 4.4 的三步往返（打印 → 再解析 → 判等 → 文本相等），并与 DSL 版 `assert_structural_equal` 判等。

**预期结果**：第一段判等通过、两份文本同构（**待本地验证**——代码为本讲新写，API 依据 [python/pypto/ir/builder.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/builder.py) 与 [python/pypto/pypto_core/ir.pyi:L3034-L3060](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/pypto_core/ir.pyi#L3034-L3060)）。第二段第 3 步若类型推断拒绝手造的 tile 参数（例如 valid_shape/T 物理形状约束不满足），记录报错并对照 [tests/ut/ir/printing/test_python_printer.py:L1790-L1797](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/printing/test_python_printer.py#L1790-L1797) 的注解修正类型后重试——报错本身就是对 u4-l6 注册表约束的复习。

## 6. 本讲小结

- `IRBuilder` 用**上下文栈 + Begin/End 配对**把"手工构造 IR"变成受校验的增量过程：嵌套函数当场报错、循环 iter_arg/return_var 数量守恒、`EndFunction` 把语句打包成不可变 `Function`（单语句不包 `SeqStmts`）。
- Python 包装层把 Begin/End 变成 `with`，用 `inspect` 回退两帧自动捕获用户代码的 span，进入/退出各捕一次合成跨行 span；`for_loop` 的 `body_ok` 抑制展示了对错误信息质量的刻意保护。
- `python_print` 打印 `Call` 的三条路径：GlobalVar→`self.fn(...)`、注册算子→`pl.<ns>.<op>(...)`、其它裸打；机器专属 attrs 经开放世界 denylist 收进 `attrs={...}`，保证往返不丢元数据。
- **IR 是位置数据、DSL 是带关键字的签名**，两者错位时打印器必须做关键字化修整：`init_cond` 在 `tile.matmul_acc` 按位打印，在 `tensor.matmul_acc`（`a_trans` 占槽）与 `tile.gemv_acc`（`acc_phase` 占槽）按 `init_cond=` 关键字打印——`gemv_acc` 一行是 HEAD `ec5d20c`（PR #2528）新增。
- 往返实验（parse→print→parse→判等→文本相等）是打印器的验收标准；结构化相等忽略 span 与名字，因此 Builder 产物与 DSL 产物可以直接判等。
- 谓词类参数注册为**操作数**（可依赖循环变量、进 use-def 链）而非 kwarg——打印层的这些特例正是"操作数放对了语义层、签名层却错位"的代价，两层的合同由往返测试守护。

## 7. 下一步学习建议

- 下一讲 **u4-l8（序列化与结构化比较）**：本讲把 `assert_structural_equal` 当工具用，下一讲拆开它的实现——`AreExprsEqual` 的比较粒度（ConstInt 按值、二元/一元/Call 按结构、其余按指针）与 `.pto` 二进制序列化的往返。
- 建议顺手阅读：[docs/en/dev/ir/06-builder.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/06-builder.md) 的 matmul_tile 完整例子（tile 类型 + memref + tile_view 的组装），以及 [tests/ut/ir/high_level/test_builder.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/high_level/test_builder.py) 中 while/if 部分的用例。
- 想看打印器更多特例（`full`/`reinterpret_view`/`syncall` 的参数重排、`NeedsDslUnderscore`），直接读 [src/ir/transforms/python_printer.cpp:L941-L1300](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L941-L1300) 的 `VisitExpr_` 实现，每个特例都带注释说明动机。
