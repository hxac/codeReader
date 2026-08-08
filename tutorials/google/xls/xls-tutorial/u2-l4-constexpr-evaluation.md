# 编译期求值（constexpr）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「编译期求值（constexpr）」在 DSLX 里的含义，以及它和运行期执行的边界在哪里。
- 掌握 `ConstexprEvaluator` 的工作模型：它是一个访问者（visitor），把「是否常量」「常量值是多少」两类结论写回到上一讲的 `TypeInfo` 里。
- 理解它如何**复用运行期的字节码解释器**来完成编译期的真正计算。
- 看懂 `const for` 循环在编译期被「展开」的机制，并解释为什么数组大小、位宽、切片边界这些位置必须给出编译期常量。
- 亲手跟踪 `const_for.x` 的展开过程，并写出一个「数组大小由编译期求值决定」的 DSLX 函数。

## 2. 前置知识

本讲承接 [u2-l3 DSLX 类型推导与检查](u2-l3-dslx-type-system.md)，你需要先建立以下认知：

- **`Type` / `TypeInfo` / `ParametricEnv` 三件套**：`Type` 是已求值的具体类型；`TypeInfo` 是一张「AST 节点 → 类型」的登记册，靠 parent 链构成差分式结构；`ParametricEnv` 是一次参数化实例化的「身份证」（如 `N=5`）。
- **AST 是无类型的**：解析器只产出语法树（见 [u2-l2](u2-l2-dslx-frontend-parser-ast.md)），类型和「常量值」都是后续才填进去的注解（annotation）。
- **硬件位宽必须在编译期确定**：因为电路的每一根线都有固定的物理宽度，不可能像软件那样「运行时再决定数组多长」。这正是 constexpr 在 DSLX 里如此重要的根本原因。

此外，你最好对**访问者模式（Visitor Pattern）**有一点印象：每个 AST 节点都有一个 `AcceptExpr(visitor)` 方法，会把控制权交给 visitor 对应的 `HandleXxx`。本讲的主角 `ConstexprEvaluator` 正是一个 `ExprVisitor`。

> 名词速查：**constexpr** = constant expression，编译期常量表达式。它在 DSLX 里的角色类似 C++ 的 `constexpr`——一个能在编译时刻就被算出确定值的表达式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `xls/dslx/constexpr_evaluator.h` | `ConstexprEvaluator` 类与 `MakeConstexprEnv` 等自由函数的接口声明。 |
| `xls/dslx/constexpr_evaluator.cc` | 核心实现：每个 `HandleXxx`、`InterpretExpr`、`MakeConstexprEnv`。本讲的主角。 |
| `xls/dslx/type_system/type_info.h` | constexpr 结论的「存放处」：`NoteConstExpr` / `GetConstExpr` / `IsKnownConstExpr` 等接口。 |
| `xls/dslx/type_system_v2/constant_collector.cc` | `const for` / `unroll_for!` 的**展开**逻辑所在，是「编译期常量场景」的关键源码。 |
| `xls/dslx/frontend/ast.h` | `ConstFor` AST 节点的定义。 |
| `xls/examples/const_for.x` | 贯穿全讲的范例：用 `const for` 在编译期算出一个位掩码。 |
| `xls/dslx/errors.cc` | `NotConstantErrorStatus`：当「必须有常量」的位置却非常量时报的错。 |

一句话定位：`type_info.h` 是**账本**，`constexpr_evaluator.cc` 是**算账的人**，`constant_collector.cc` 是**要求展开循环的人**，三者协作把「编译期常量」这件事做完。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 constexpr 求值器**：讲解求值器本身的模型与实现。
- **4.2 编译期常量场景**：讲解哪些 DSLX 构造**必须**给出编译期常量，重点剖析 `const for` 的展开。

---

### 4.1 constexpr 求值器：在编译期运行 DSLX 表达式

#### 4.1.1 概念说明

回想上一讲：类型推导走完之后，`TypeInfo` 里已经存好了「每个 AST 节点的类型」。但光知道类型还不够。考虑下面这句：

```dslx
const N = u32:5;
fn f() -> u8 { /* 用到 N 的地方需要它的真实数值 */ }
```

类型系统告诉我们 `N` 的类型是 `u32`，但很多场景要的不是「类型」，而是**那个具体的数值 5**——比如要把 `N` 当作数组长度、当作循环上界、当作位宽。这些数值必须在编译时刻就确定。

`ConstexprEvaluator` 就是干这件事的组件：它遍历 AST 表达式，判断「这个表达式能不能在编译期算出一个确定值」，如果能，就把那个值（一个 `InterpValue`）写回 `TypeInfo`。

这里有一个设计上的关键认知：**编译期求值和运行期执行用的是同一套「计算引擎」**。求值器并不会自己重新实现一遍加减乘除，而是把表达式编译成字节码（bytecode），然后交给运行期那套字节码解释器去跑——只不过跑的时机是「现在（编译期）」，而不是程序真正运行的时候。这带来一个直接后果：**凡是运行期能算的表达式，编译期原则上也能算**，差别只在于它的输入是不是都已知。

> 这个「编译期复用运行期解释器」的设计，会在 [u2-l5 DSLX 字节码解释器](u2-l5-dslx-bytecode-interpreter.md) 里展开。本讲你只需要记住：`InterpretExpr` 内部会发射字节码并调用 `BytecodeInterpreter::Interpret`。

#### 4.1.2 核心流程

求值器对外暴露两个静态入口（你可以把它理解成「询问」）：

```
Evaluate(expr)         → 不强求 expr 是常量；能算就记下值，不能算就标记「非常量」
EvaluateToValue(expr)  → 强求 expr 是常量；算不出来就返回错误 NotConstantError
```

对一个表达式求值时，内部遵循一个**三态 + 访问者**的模型：

1. **三态判断**：先查 `TypeInfo`，节点可能已经是「已知常量」「已知非常量」「未知」。只有「未知」才需要真正求值（这是缓存/记忆化，避免重复计算）。
2. **访问者分派**：对未知节点，构造一个 `ConstexprEvaluator` 实例，调用 `expr->AcceptExpr(this)`，于是控制权流到与节点类型匹配的 `HandleXxx`。
3. **先递归子表达式，再下结论**：大多数 `HandleXxx` 的套路是——先把子表达式逐一求值（用宏 `EVAL_AS_CONSTEXPR_OR_RETURN`），只要有任一子表达式非常量，当前表达式也就不是常量，提前返回；只有所有子表达式都是常量时，才继续算出当前节点的值。
4. **记录结论**：把结论（值或「非常量」标记）写回 `TypeInfo`。

`HandleXxx` 内部最终落到三类处理方式之一：

- **直接记值**：节点本身简单到不用解释器，比如 `HandleNumber`（数字字面量）、`HandleConditional`（条件表达式，挑中那个分支的值）、`HandleXlsTuple`（拼元组）。
- **调解释器**：节点稍复杂，子表达式都确认是常量后，交给 `InterpretExpr` 跑字节码，比如 `HandleBinop`、`HandleCast`、`HandleArray`。
- **短路返回 Ok**：节点天然非常量，直接记「非常量」并返回成功，比如 `HandleFor`（普通运行期 `for` 循环）、`HandleInvocation` 里的 `send`/`recv`（I/O 操作）。

伪代码概括 `Evaluate` 的主流程：

```
Evaluate(expr):
    if type_info 已知 expr 是常量或非常量: return Ok   # 缓存命中
    evaluator = ConstexprEvaluator(...)
    return expr.AcceptExpr(evaluator)               # 分派到 HandleXxx
```

而单个二元运算（以加法为例）的套路：

```
HandleBinop(expr):
    EVAL_AS_CONSTEXPR_OR_RETURN(expr.lhs)   # 左子表达式必须可求值
    EVAL_AS_CONSTEXPR_OR_RETURN(expr.rhs)   # 右子表达式必须可求值
    return InterpretExpr(expr)              # 两边都常量 → 跑解释器算和
```

#### 4.1.3 源码精读

**两个静态入口**——这是求值器对外的全部「门面」：[xls/dslx/constexpr_evaluator.cc:93-123](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L93-L123)。注意 `Evaluate` 里的三态短路：

```cpp
if (type_info->IsKnownConstExpr(expr) ||
    type_info->IsKnownNonConstExpr(expr)) {
  return absl::OkStatus();   // 已经有结论，不必再算
}
```

中文说明：如果账本里已经写明这个表达式是常量或非常量，直接返回，不重复求值。`EvaluateToValue` 则在 `Evaluate` 之后多查一次，查不到常量值就抛 `NotConstantError`。

**驱动宏**——求值器内部两个最重要的宏：[xls/dslx/constexpr_evaluator.cc:127-146](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L127-L146)。

```cpp
#define EVAL_AS_CONSTEXPR_OR_RETURN(EXPR) ...
#define GET_CONSTEXPR_OR_RETURN(LHS, EXPR) ...
```

中文说明：`EVAL_AS_CONSTEXPR_OR_RETURN(e)` 的语义是「尽力把子表达式 `e` 求值；求值后若 `e` 不是常量，就让当前函数直接返回 `OkStatus()`」——这正是「只要有一个子表达式非常量，父表达式就不必再算」的实现。`GET_CONSTEXPR_OR_RETURN(lhs, e)` 在前者基础上把 `e` 的常量值取出来赋给 `lhs`。

**数字字面量——最基础的常量**：[xls/dslx/constexpr_evaluator.cc:436-484](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L436-L484)。

```cpp
absl::Status ConstexprEvaluator::HandleNumber(const Number* expr) {
  // Numbers should always be [constexpr] evaluatable.
  ...
  XLS_ASSIGN_OR_RETURN(InterpValue value, EvaluateNumber(*expr, *type_ptr));
  type_info_->NoteConstExpr(expr, value);
  return absl::OkStatus();
}
```

中文说明：数字字面量永远能求值。这里有个细节——一个没有类型标注的裸数字（如数组 `[0, 1, 2, 3]` 里的 `0`），其类型要靠上下文（比如数组元素类型 `u32`）才能定，所以 `Evaluate` 接口里才有一个可选的 `type` 参数。

**名字引用——常量值的传播**：[xls/dslx/constexpr_evaluator.cc:391-408](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L391-L408)。

```cpp
absl::Status ConstexprEvaluator::HandleNameRef(const NameRef* expr) {
  ...
  if (type_info_->IsKnownNonConstExpr(name_def) ||
      !type_info_->IsKnownConstExpr(name_def)) {
    return absl::OkStatus();
  }
  type_info_->NoteConstExpr(expr, type_info_->GetConstExpr(name_def).value());
  return absl::OkStatus();
}
```

中文说明：当一个表达式引用某个名字（比如 `N`）时，求值器去查这个名字的 `NameDef` 是不是常量；若是，就把那个值「搬」到当前 `NameRef` 上。这样常量值就能沿着引用链一路传播。这也解释了为什么「账本」要按 AST 节点（包括 `NameDef`）索引，而非只按 `Expr`。

**真正计算——交给字节码解释器**：[xls/dslx/constexpr_evaluator.cc:584-624](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L584-L624)。

```cpp
absl::Status ConstexprEvaluator::InterpretExpr(const Expr* expr) {
  XLS_ASSIGN_OR_RETURN(ConstexprEnvData constexpr_env_data,
                       MakeConstexprEnv(...));
  XLS_ASSIGN_OR_RETURN(std::unique_ptr<BytecodeFunction> bf,
                       BytecodeEmitter::EmitExpression(..., constexpr_env_data.env, ...));
  ...
  XLS_ASSIGN_OR_RETURN(InterpValue constexpr_value,
                       BytecodeInterpreter::Interpret(import_data_, bf.get(), ...));
  type_info_->NoteConstExpr(expr, constexpr_value);
  return absl::OkStatus();
}
```

中文说明：这是「编译期复用运行期」的核心。它先把表达式里用到的自由变量收集成一个常量环境 `env`（比如 `N=5`），再发射字节码，最后调用 `BytecodeInterpreter::Interpret` 真正算出结果。注意这里还有一个 `rollover_hook`：当 `warn_rollover_` 打开时，编译期算术若发生回绕（溢出），会收集成一条警告——这就是为什么你能在类型检查阶段就看到「constexpr evaluation detected rollover」这类提示。

**搭建常量环境**：[xls/dslx/constexpr_evaluator.cc:626-689](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L626-L689) 的 `MakeConstexprEnv`。它先种入 `ParametricEnv`（参数化绑定，如 `N=5`），再扫描表达式的自由变量，逐个判断能否解析成常量，能解析的进 `env`，不能的记进 `non_constexpr`。这个 `env` 就是上面 `InterpretExpr` 喂给字节码解释器的「已知数值表」。

#### 4.1.4 代码实践

**实践目标**：用一个能直接观察到「编译期 vs 运行期」差异的小例子，体会求值器只对**能在编译期确定**的部分提前算账。

**操作步骤**：

1. 新建 `/tmp/ce_demo.x`，内容如下（示例代码）：

   ```dslx
   const WIDTH = u32:4;          // 编译期常量

   fn add(x: u32, y: u32) -> u32 { x + y }

   fn main(a: u32[WIDTH]) -> u32 {   // WIDTH 必须是编译期常量 → 数组大小
       a[u32:0] + a[u32:1]            // 下标也偏好编译期常量
   }

   #[test]
   fn t() {
       assert_eq(add(u32:2, u32:3), u32:5);   // 实参 2、3 是编译期常量
   }
   ```

2. 运行解释器跑测试（命令来自 [u1-l5](u1-l5-full-toolchain-walkthrough.md)）：

   ```bash
   bazel-bin/xls/dslx/interpreter_main /tmp/ce_demo.x
   ```

3. 然后故意把数组大小改成「非常量」来观察错误边界：新建 `/tmp/ce_bad.x`（示例代码）：

   ```dslx
   fn bad(n: u32) -> u32 {
       let arr: u32[n] = u32[n]:[0, 0];   // n 是运行期入参，不是编译期常量
       arr[u32:0]
   }
   ```

   同样用 `interpreter_main` 运行。

**需要观察的现象**：

- `ce_demo.x` 应当正常通过测试。`WIDTH` 作为数组大小合法，正是因为它在编译期可求值为 `4`。
- `ce_bad.x` 会在类型检查/编译期求值阶段报错，错误信息里会出现 `NotConstantError`（见下文 4.2 节）。

**预期结果**：

- 合法例子：`PASS`，测试通过。
- 非法例子：编译期失败，提示 `n` 不是 constexpr。

> 如果本地尚未构建 `interpreter_main`，参考 [u1-l2](u1-l2-build-and-run.md) 先 `bazel build`。若你无法确定运行结果，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`ConstexprEvaluator::Evaluate` 为什么在函数开头就检查 `IsKnownConstExpr` / `IsKnownNonConstExpr` 并直接返回？如果不做这个检查会有什么后果？

**参考答案**：这是记忆化（memoization）。同一棵 AST 的节点会被多次求值（不同父节点都会触达它）。不缓存会导致重复计算，更糟的是可能重复触发解释器、重复收集回绕警告，且在差分式 `TypeInfo` 里可能产生不一致的结论。直接返回保证「一个节点只下结论一次」。

**练习 2**：`HandleInvocation` 里对 `send`/`recv` 这类名字直接返回 `OkStatus()` 而不继续求值，这体现了什么设计原则？

**参考答案**：I/O 操作本质上有副作用、依赖运行期通道，不可能在编译期求出确定值。求值器对这类「天然非常量」的节点选择**短路**，既不浪费时间调解释器，也避免把一个本就不该被当作常量的东西误算。它体现了「能算的算、不能算的干净退出」的保守策略。

**练习 3**：为什么 `InterpretExpr` 在调字节码解释器之前，要先调 `MakeConstexprEnv` 构建 `env`？

**参考答案**：字节码解释器执行表达式时，需要知道表达式里所有自由变量的当前取值（比如 `N` 到底是 5 还是 8）。`MakeConstexprEnv` 把参数化绑定和可解析的常量自由变量汇成一张「值表」喂给解释器；缺少它，解释器遇到名字引用就无从取值。

---

### 4.2 编译期常量场景：哪些地方必须有常量

#### 4.2.1 概念说明

上一节讲的是「求值器怎么算」。这一节回答另一个同样重要的问题：**DSLX 里哪些位置非要有编译期常量不可？**

答案都指向同一个硬件事实——**电路结构在制造时就固定了**。你不能造一块「等输入来了再决定自己有几根线」的芯片。因此在 DSLX 里，以下信息必须能在编译期定死：

- **位宽**：`uN[W]` 里的 `W`、`bits[N]` 里的 `N`。
- **数组大小**：`u32[K]` 里的 `K`（决定要分配多少个寄存器/连线）。
- **切片边界**：`x[start:end]`（决定截取的物理线段）。
- **普通 `for` 循环的迭代范围上界**：因为循环要被展开成流水线。
- **`const for` / `unroll_for!` 的迭代范围**：要在编译期完全展开。
- **`const` 常量定义的右值**：`const X = <这里>` 自然要是常量。
- **派生参数化（parametric）默认值**：如 `B: u32 = {double(A)}`，花括号里的表达式要能编译期求值。

这些场景的共同点是：它们的值会**直接改变生成的电路结构**。位宽差一，线的根数就差一；数组大小差一，寄存器数就差一。所以编译器必须在此刻拿到确定数值，拿不到就报 `NotConstantError`（详见 [xls/dslx/errors.cc:254-259](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/errors.cc#L254-L259)）：

```cpp
absl::Status NotConstantErrorStatus(const Span& span, const Expr* expr,
                                    const FileTable& file_table) {
  return absl::InvalidArgumentError(absl::StrFormat(
      "NotConstantError: %s expr `%s` is not constexpr.",
      span.ToString(file_table), expr->ToString()));
}
```

中文说明：当某个位置要求常量却拿不到时，编译器抛出 `NotConstantError`，明确告诉你「这个表达式不是 constexpr」。

DSLX 文档也直接点出这一性质——派生参数化表达式「类似 C++ 的 constexpr，是一个能在编译期被求值的简单表达式」：[docs_src/dslx_reference.md:244-248](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx_reference.md#L244-L248)。

#### 4.2.2 核心流程：`const for` 的编译期展开

`const for` 是最能体现「编译期求值」威力的构造。看 `const_for.x` 里的核心函数：[xls/examples/const_for.x:15-19](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/const_for.x#L15-L19)

```dslx
fn const_for_mask<N: u32>() -> u8 {
    const for (idx, mask): (u32, u8) in u32:0..N {
        mask | (u8:1 << idx)
    }(u8:0)
}
```

语义：从 `mask = u8:0` 出发，对 `idx = 0..N` 的每个值，把 `mask` 的第 `idx` 位置 1。最终结果是一个低 N 位全 1 的掩码。`#[test]` 验证 `const_for_mask<u32:5>() == 0b11111`、`const_for_mask<u32:8>() == u8::MAX`（[const_for.x:21-25](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/const_for.x#L21-L25)）。

注意：`N` 是参数化的，它的具体值只有在**实例化**（如 `const_for_mask<u32:5>()`）后才确定。所以「循环跑几次」是参数化实例化之后才知道的事——这正是上一讲 `ParametricEnv` 与本讲的交汇点：实例化给出 `N=5`，求值器才能算出迭代范围 `0..5`，进而展开。

展开（unrolling）的语义在源码注释里写得非常清楚，[xls/dslx/type_system_v2/constant_collector.cc:427-448](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L427-L448)。即把：

```
let X = const for (i, a) in iterable { body; last_body_expr }(init);
```

改写成一段**没有循环的顺序语句**：

```
let a = init;
let i = iterable[0]; body; let a = last_body_expr;
let i = iterable[1]; body; let a = last_body_expr;
...  // 对 iterable 每个元素重复
let X = last_body_expr;
```

以 `N=3` 为例，`const_for_mask<u32:3>()` 被展开成大致等价于：

```dslx
// 展开 3 轮，mask 逐轮演化
mask = u8:0 | (u8:1 << 0);   // 0b001
mask = mask  | (u8:1 << 1);  // 0b011
mask = mask  | (u8:1 << 2);  // 0b111
```

整个展开流程可以概括为：

1. **先求迭代范围的大小**：把 `iterable`（这里是 `u32:0..N`）当作整体喂给 `ConstexprEvaluator::EvaluateToValue`，拿到循环次数 `size`（[constant_collector.cc:479-497](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L479-L497)）。
2. **按 size 复制循环体**：用一个普通 C++ `for (i = 0; i < size; i++)` 把循环体克隆 `size` 份，每份用新的名字定义（shadowing）替换掉迭代变量和累加器，把累加器的值「接」到下一轮（[constant_collector.cc:502](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L502)）。
3. **重新填充类型表 + 求最终值**：对展开后的整段语句重新做类型推导，再对最后那个累加器表达式调一次 `EvaluateToValue` 得到整个 `const for` 的常量结果，并 `NoteConstExpr` 记下（[constant_collector.cc:607-616](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L607-L616)）。
4. **登记展开结果**：把展开后的表达式通过 `NoteUnrolledLoop` 存进 `TypeInfo`，供后续阶段（如 IR 转换）取用（[constant_collector.cc:619-620](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L619-L620)）。

展开产生的循环次数若记为 \(n\)，则展开后语句条数与 \(n\) 成正比：

\[
\text{语句数} = O(n)
\]

这正是 `const for` 与普通 `for` 的根本区别：普通 `for` 在综合时被展开成**流水线**（每个时钟周期跑一轮），而 `const for` 在**编译期**就被完全摊平成一串顺序表达式——循环消失了，只剩一串直算。

> 重要区分：
> - 普通 `for`：综合后是**时序电路**（带寄存器的流水线），求值器对它直接返回「非常量」（见 `HandleFor`，[constexpr_evaluator.cc:302-304](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L302-L304)）。
> - `const for`：编译期**完全展开**，结果是单个编译期常量。

#### 4.2.3 源码精读

**`ConstFor` AST 节点**：[xls/dslx/frontend/ast.h:4268-4295](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L4268-L4295)。它继承 `ForLoopBase`，与普通 `for` 共享结构，靠 `is_unroll_for_` 区分是 `const for` 还是 `unroll_for!`：

```cpp
std::string_view GetNodeTypeName() const override {
  return is_unroll_for_ ? "unroll-for" : "const for";
}
```

中文说明：`const for` 和 `unroll_for!` 在 AST 层是同一个节点类，只是关键字不同、语义略有差异，但都走「编译期展开」这条路。

**求值器如何处理 `ConstFor`**：[xls/dslx/constexpr_evaluator.cc:559-566](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/constexpr_evaluator.cc#L559-L566)

```cpp
absl::Status ConstexprEvaluator::HandleConstFor(const ConstFor* expr) {
  std::optional<const Expr*> unrolled =
      type_info_->GetUnrolledLoop(expr, bindings_);
  if (unrolled.has_value()) {
    return (*unrolled)->AcceptExpr(this);
  }
  return absl::OkStatus();
}
```

中文说明：求值器自己**不展开**循环。它的策略是「去找展开结果」——调用 `type_info_->GetUnrolledLoop`，如果 `constant_collector` 那边已经展开好并存了（见 4.2.2 第 4 步），就直接对展开后的表达式再跑一遍求值（递归 `AcceptExpr`）。这是一个典型的**关注点分离**：展开是类型系统侧（`constant_collector`）的职责，求值是 `ConstexprEvaluator` 的职责，二者通过 `TypeInfo` 这本账本解耦。

**展开结果的存取接口**：[xls/dslx/type_system/type_info.h:457-465](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system/type_info.h#L457-L465)

```cpp
void NoteUnrolledLoop(const ConstFor* loop, const ParametricEnv& env,
                      Expr* unrolled_expr);
std::optional<Expr*> GetUnrolledLoop(const ConstFor* loop,
                                     const ParametricEnv& env) const;
std::vector<Expr*> GetAllUnrolledLoops(const ConstFor* loop) const;
```

中文说明：展开结果按 `(loop, ParametricEnv)` 存取——同一个 `const for` 在 `N=3` 和 `N=5` 下展开结果不同，所以要带上参数化环境作 key。这与上一讲 `TypeInfo` 的差分式设计一脉相承。

**数组的「编译期大小」实例**：再看 `const_for.x` 用常量定数组维度的写法，[xls/examples/const_for.x:46-51](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/const_for.x#L46-L51)

```dslx
const NUM_OF_CHANNELS = u32:2;

#[test_proc]
proc ConstForProcSpawnTest {
    req_s: chan<()>[NUM_OF_CHANNELS] out;
    resp_r: chan<u32>[NUM_OF_CHANNELS] in;
```

中文说明：`NUM_OF_CHANNELS` 是一个 `const`，编译期可求值为 `2`，因此可以合法地用作通道数组 `chan<()>[NUM_OF_CHANNELS]` 的大小。如果换成运行期入参，编译器会立刻报 `NotConstantError`。

#### 4.2.4 代码实践

**实践目标**：跟踪 `const_for.x` 中 `const for` 的编译期展开，并亲手写一个「数组大小依赖编译期求值」的函数。

**操作步骤**：

1. **跟踪展开逻辑**。打开 `xls/dslx/type_system_v2/constant_collector.cc`，定位 `HandleConstFor`（[L427](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/type_system_v2/constant_collector.cc#L427)）。对照 `const_for_mask<N>`（[const_for.x:15-19](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/const_for.x#L15-L19)），在纸上为 `N=3` 手工模拟：
   - `size` 从 `u32:0..3` 求出 = 3；
   - 复制循环体 3 次，每次 `idx` 取 0、1、2，`mask` 从 `u8:0` 演化为 `0b001 → 0b011 → 0b111`；
   - 最终 `NoteConstExpr` 记下结果 `u8:0b111`。

2. **写一个依赖编译期求值定数组大小的函数**。新建 `/tmp/array_pow2.x`（示例代码）：

   ```dslx
   // 编译期算出 2^N，用作数组大小
   fn pow2<N: u32>() -> u32 {
       const for (i, acc): (u32, u32) in u32:0..N {
           acc * u32:2
       }(u32:1)
   }

   fn make_table<N: u32>() -> u32[pow2<N>()] {
       u32[pow2<N>()]:[0, ...]
   }

   #[test]
   fn t_pow2() {
       assert_eq(pow2<u32:3>(), u32:8);
       assert_eq(pow2<u32:0>(), u32:1);
   }

   fn main() -> u32 { pow2<u32:4>() }
   ```

   注意 `make_table<N>` 的返回类型 `u32[pow2<N>()]`——**数组大小本身就是一个 `const for` 求值的常量**，这是「编译期求值支撑电路结构」最直接的体现。

3. 运行验证：

   ```bash
   bazel-bin/xls/dslx/interpreter_main /tmp/array_pow2.x
   ```

**需要观察的现象**：

- 手工模拟的 `N=3` 展开结果应与 `interpreter_main` 跑 `const_for.x` 里 `const_for_mask<u32:3>()`（即 `main`）的输出一致。
- `array_pow2.x` 中 `pow2<u32:3>()` 应等于 8，`pow2<u32:0>()` 应等于 1（循环跑 0 次，直接返回初值 `u32:1`）。

**预期结果**：测试 `PASS`，`main` 返回 `16`（\(2^4\)）。如果 `make_table` 的数组大小写成了运行期非常量，会在编译期报 `NotConstantError`。

> 若本地未构建 `interpreter_main`，请先按 [u1-l2](u1-l2-build-and-run.md) 构建；若不便运行，请标注「待本地验证」，但手工展开模拟务必完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `const_for_mask` 的迭代范围 `u32:0..N` 必须在编译期可求值？把它改成普通 `for` 会怎样？

**参考答案**：`const for` 的语义是「编译期完全展开」，展开需要知道循环次数 `size`，因此迭代范围必须是编译期常量。若改成普通 `for`，它就变成运行期流水线循环（求值器对 `HandleFor` 直接返回「非常量」），函数也无法返回单个编译期常量值、不能再用作数组大小等结构位置。

**练习 2**：求值器的 `HandleConstFor` 自己并不展开循环，而是去 `GetUnrolledLoop` 取展开结果。这种分工有什么好处？

**参考答案**：展开需要重新克隆 AST、重新做类型推导，属于类型系统（`constant_collector`）的能力范围；求值器只擅长「对一段无循环的表达式求值」。把两者解耦后，`ConstexprEvaluator` 保持简单（只处理已展开的表达式），而展开这个复杂、易错的步骤只在一个地方实现，避免重复。二者通过 `TypeInfo` 的 `NoteUnrolledLoop`/`GetUnrolledLoop` 通信。

**练习 3**：`const NUM_OF_CHANNELS = u32:2;` 之后用作 `chan<()>[NUM_OF_CHANNELS]`。请说明从这行 `const` 到「通道数组宽度为 2」之间，编译器大致经过了哪些步骤。

**参考答案**：(1) 类型系统为 `NUM_OF_CHANNELS` 的 `NameDef` 在 `TypeInfo` 里记下常量值 `2`；(2) 求值器在处理数组维度 `NUM_OF_CHANNELS` 时，通过 `HandleNameRef` 把这个值传播到维度表达式节点；(3) IR 转换/代码生成阶段读这个编译期常量来决定要实例化多少条通道连线。任何一步拿不到确定数值，都会触发 `NotConstantError`。

## 5. 综合实践

把本讲两个模块串起来，完成下面这个「编译期查表」小任务。

**任务**：用 `const for` 在编译期生成一个长度为 `N` 的查表数组 `LUT`，其中 `LUT[i] = i * i`（`i` 从 0 到 `N-1`），并写一个函数 `lookup<N>(idx) -> u32` 从中按下标取值。

**要求**：

1. `LUT` 的**内容**用 `const for` 在编译期算出（提示：把累加器设计成「数组」或用元组携带中间结果；也可先用 `const for` 算出每个元素再拼接）。如果你觉得数组累加器较难，可以退而用 `const for` 算单个值 `square<N>()`，再据此构造定长数组。
2. `LUT` 的**长度** `N` 必须是编译期常量（参数化或 `const`）。
3. 配一个 `#[test]` 验证 `lookup` 的若干取值，例如 `square` 序列 `[0, 1, 4, 9, 16]`。
4. 用 `interpreter_main` 跑通后，再用 `ir_converter_main` 把它转成 IR（命令见 [u1-l5](u1-l5-full-toolchain-walkthrough.md)），观察：编译期常量在 IR 里是否已经变成具体的立即数/常量节点，循环是否已经消失。

**思考题**（写进你的实践笔记）：在生成的 IR 里，你能找到任何「循环」的痕迹吗？为什么？（提示：`const for` 在编译期已被展开成顺序表达式，到 IR 阶段已经没有循环结构了。）

> 这一步把「语言层（`const for`）→ 求值器（编译期展开）→ IR（无循环的常量）」三层打通，是检验你是否真正理解本讲的好办法。

## 6. 本讲小结

- **constexpr = 编译期可求值的表达式**。在 DSLX 里，它的角色类似 C++ 的 `constexpr`，由 `ConstexprEvaluator` 负责判断和求值。
- **求值器是访问者 + 三态模型**：通过 `AcceptExpr` 分派到各 `HandleXxx`，先递归子表达式（`EVAL_AS_CONSTEXPR_OR_RETURN`），全部是常量才算当前节点；用 `IsKnownConstExpr`/`IsKnownNonConstExpr` 做缓存避免重复计算。
- **编译期复用运行期**：真正的算术求值不另起炉灶，而是发射字节码后调用运行期的 `BytecodeInterpreter::Interpret`（`InterpretExpr`）。
- **结论写回 `TypeInfo`**：常量值经 `NoteConstExpr` 存入账本，经 `HandleNameRef` 沿引用传播——这本账本与上一讲的类型信息共享同一套差分式结构。
- **编译期常量是「电路结构」的前提**：位宽、数组大小、切片边界、循环上界、`const` 右值、派生参数化默认值都必须编译期可定，否则报 `NotConstantError`。
- **`const for` 在编译期完全展开**：由 `constant_collector` 克隆循环体、用 `NoteUnrolledLoop` 登记，求值器再对展开结果求值；普通 `for` 则综合成运行期流水线，二者本质不同。

## 7. 下一步学习建议

- 下一讲 [u2-l5 DSLX 字节码解释器](u2-l5-dslx-bytecode-interpreter.md) 会拆开本讲反复提到的 `BytecodeEmitter` 和 `BytecodeInterpreter`——即 `InterpretExpr` 内部到底如何把表达式变成字节码、如何在栈帧上求值。读完那一讲，你会对「编译期复用运行期」有完整的底层画面。
- 如果你对「常量如何驱动 IR 结构」更感兴趣，可以跳到 [u3-l1 IR 总览](u3-l1-ir-overview.md) 与 [u3-l4 从 DSLX 到 IR 的转换](u3-l4-dslx-to-ir-conversion.md)，看编译期常量在 IR 转换阶段如何变成字面量节点。
- 想再深入求值边界，建议阅读：`xls/dslx/constexpr_evaluator_test.cc`（看各类表达式的求值断言）、`xls/dslx/type_system_v2/constant_collector.cc` 全文（看 `const for` 之外的常量收集场景，如格式化宏的 verbosity、`const_assert!` 等）。
