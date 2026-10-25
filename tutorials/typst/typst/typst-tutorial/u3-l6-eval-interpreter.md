# 解释器主循环：typst-eval 如何执行代码

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `typst-eval` 的模块地图，说出 `eval()` 入口从 `Source` 到 `Module` 的完整流程。
2. 描述一段含 `#let`、`#for` 的代码在解释器里经过的函数路径（`eval` → `eval_markup`/`eval_code` → `Expr::eval` → `LetBinding::eval`/`ForLoop::eval` …）。
3. 理解「markup 模式求值产出 `Content`、code 模式求值产出 `Value`」这条主线，以及两个主循环如何用 `ops::join` 把相邻结果拼起来。
4. 说明 `flow.rs` 中 `FlowEvent` 信号机制：`break`/`continue`/`return` 不是异常，而是挂在 `Vm` 上的一个字段，由循环和函数调用点负责消费或拒绝。
5. 跟踪一次 `#import` 的调用链：路径解析 → `import_file` → 递归 `eval()`，以及循环导入的防护。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先快速回顾：

- **语法树与 AST 视图**（u2-l3、u2-l4）：解析产出 `SyntaxNode`（CST），`ast.rs` 在其上提供 `ast::Expr`、`ast::LetBinding`、`ast::ForLoop` 等零成本类型化视图。解释器遍历的就是这些视图。
- **Span**（u2-l4）：每个节点携带的 8 字节编号，用于错误定位与 IDE 追踪。本讲会大量看到 `.at(span)`（把错误附上位置）和 `.spanned(span)`（把值附上位置）。
- **World 与 Engine**（u3-l1）：`World` 是宿主提供的输入环境（源文件、字体等），`Engine` 是编译器内部的执行上下文（library、world、sink、route 等）。
- **Value 与 Content**（u3-l3、u3-l4）：code 模式的值统一装在 `Value` 枚举里；markup 模式的排版内容统一装在 `Content` 里。`Value::Content` 是两者的桥。
- **Func 与闭包**（u3-l5）：函数是一等公民；用户闭包的调用最终会回到本讲的 `call.rs`。

再用两句话补两个新术语：

- **树遍历解释器（tree-walking interpreter）**：不生成字节码，而是直接递归遍历 AST，边走边算。`typst-eval` 就是这种形态，入口是一个统一的 `Eval` trait。
- **虚拟机（Vm）**：这里的「虚拟机」不是模拟 CPU 的那种，只是一个装着解释器可变状态的小结构体：作用域栈、控制流信号、被追踪的 Span 等。

## 3. 本讲源码地图

本讲的主战场是 `crates/typst-eval`，它是 Cargo workspace 中专门负责「求值」的 crate（u1-l3 的流水线第 2 站）。

| 文件 | 作用 |
| --- | --- |
| [crates/typst-eval/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs) | crate 入口：模块声明、`eval()` 与 `eval_string()` 两个公开入口、`Eval` trait 定义 |
| [crates/typst-eval/src/vm.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/vm.rs) | `Vm` 结构体：解释器的全部可变状态 |
| [crates/typst-eval/src/code.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs) | code 模式主循环 `eval_code`、`ast::Expr` 的大分派、字面量/容器/字段访问求值 |
| [crates/typst-eval/src/markup.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs) | markup 模式主循环 `eval_markup`、文本/标题/强调等标记节点的求值 |
| [crates/typst-eval/src/flow.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs) | `FlowEvent` 控制流信号、条件、while/for 循环、break/continue/return |
| [crates/typst-eval/src/binding.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs) | `#let` 绑定与数组/字典解构 |
| [crates/typst-eval/src/import.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs) | `#import` / `#include`、包清单解析、递归求值 |
| [crates/typst-eval/src/call.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs) | 函数/方法调用、闭包调用（本讲只看它消费 `FlowEvent::Return` 的一小段） |
| [crates/typst-eval/src/ops.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/ops.rs) | 一元/二元运算、复合赋值的 `Eval` 实现 |
| [crates/typst-eval/src/rules.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs) | `set`/`show` 规则的求值（产出样式与 recipe） |
| [crates/typst-library/src/foundations/scope.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs) | `Scope`/`Scopes` 作用域栈的定义（支撑文件） |
| [tests/suite/scripting/](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/let.typ) | `let.typ`、`for.typ`、`import.typ`、`return.typ`、`while.typ` 等：解释器行为的黄金样本 |

## 4. 核心概念与源码讲解

### 4.1 eval 模块地图：解释器的骨架

#### 4.1.1 概念说明

`typst-eval` 是一个典型的**树遍历解释器**：没有字节码、没有 JIT，就是拿语法树自顶向下递归地「走一遍」，边走边算。

它对外的形态非常规整：

- 每种 AST 节点都实现同一个 trait `Eval`，约定「给我一个 `&mut Vm`，我还你一个 `Output`」。
- 两个公开入口：`eval()` 求值整个源文件得到一个 `Module`（模块）；`eval_string()` 求值一段字符串得到单个 `Value`（供 `context` 表达式、其他 crate 嵌入求值等场景使用）。
- crate 内部按「节点类别」拆成 12 个模块（`code`、`markup`、`math`、`flow`、`binding`、`access`、`call`、`import`、`methods`、`rules`、`ops`、`vm`），每模块负责一组 `Eval` 实现。

「模块（Module）」这个词在 Typst 里指：**一个源文件求值后的结果**——它包含该文件产出的内容（`Content`）和顶层作用域里所有可导出的绑定。`#import` 拿到的就是它。

#### 4.1.2 核心流程

`eval()` 的主干可以概括为：

```text
eval(world, library, sink, route, source)
  ├─ 1. 循环求值防护：route 里已有此文件 → panic
  ├─ 2. 组装 Engine（library、world、空 introspector、sink、route）
  ├─ 3. 组装 Vm（Context::none()、Scopes::new(Some(library))、根节点 span）
  ├─ 4. 预检语法树：收集 errors/warnings
  │      errors 非空 → 直接返回错误（不再解释执行）
  ├─ 5. root.cast::<ast::Markup>() → markup.eval(&mut vm)
  │      （递归进入 markup.rs 的主循环）
  ├─ 6. 若 vm.flow 还有残留信号 → bail!(flow.forbidden())
  └─ 7. Module::new(文件名, 顶层作用域).with_content(输出)
```

注意第 4 步：**解析错误在求值前就被拦下**（u2-l3 讲过「解析永不失败、错误进树」，这里就是树上的错误被收割的时刻）。唯一的例外是 IDE 正在追踪某个 span（`vm.inspected.is_some()`）时仍会继续执行，以便悬停提示能给出值。

#### 4.1.3 源码精读

先看模块声明，这就是 crate 的「目录」：

[crates/typst-eval/src/lib.rs:1-20](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L1-L20) 声明了 12 个内部模块并再导出 `CapturesVisitor`、`eval_closure`、`FlowEvent`、`import`、`Vm` 等公开项。模块划分与上一节的地图一一对应。

入口函数 `eval`：

[crates/typst-eval/src/lib.rs:37-47](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L37-L47) 是签名与两个属性：`#[comemo::memoize]` 让同一个源文件的求值结果被 comemo 缓存（增量编译的求值层缓存点，见 u5-l8），`#[typst_macros::time(name = "eval", ...)]` 为 `--timings` 性能数据埋点。

[crates/typst-eval/src/lib.rs:48-52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L48-L52) 做循环求值防护：`route` 记录了「当前正在求值的文件链」，再次遇到同一文件说明 `A import B, B import A` 这类环，直接 panic。import.rs 里还有一层更友好的报错（见 4.5），这里是兜底。

[crates/typst-eval/src/lib.rs:54-69](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L54-L69) 组装 `Engine` 和 `Vm`：作用域栈以 `Scopes::new(Some(library))` 创建，`base` 指向标准库，所以全局函数（`text`、`upper`……）从这里来。注意 eval 阶段的 introspector 是 `EmptyIntrospector`——求值发生在排版之前，还没有页面可内省（两遍编译的因果，见 u4-l7）。

[crates/typst-eval/src/lib.rs:71-86](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L71-L86) 是预检 + 真正的求值调用：warnings 进 sink；errors 非空（且不在追踪模式）就直接返回；否则把根节点 cast 成 `ast::Markup` 调 `markup.eval(&mut vm)`。

[crates/typst-eval/src/lib.rs:88-97](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L88-L97) 收尾：残留的控制流信号说明 `break`/`return` 出现在了非法位置，转成错误；最后 `Module::new(name, vm.scopes.top)` 把**顶层作用域**连同内容一起打包成模块返回——这就是 `#import` 时对方能看到的全部导出。

第二个入口 `eval_string`：

[crates/typst-eval/src/lib.rs:99-118](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L99-L118) 接收字符串与 `SyntaxMode`，先按模式选择 `parse_code`/`parse`/`parse_math`（u2-l3 讲过的三个解析入口在这里对接）。

[crates/typst-eval/src/lib.rs:156-167](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L156-L167) 展示了本讲的一条主线的「官方版本」：Code 模式求值返回 `Value`；Markup 模式返回 `Value::Content(...)`；Math 模式把数学内容包成一个非块级 `EquationElem`。**同一个 `Eval` 机制，三种模式三种产出。**

支撑一切的 trait：

[crates/typst-eval/src/lib.rs:177-184](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L177-L184) 定义 `Eval` trait：关联类型 `Output` 允许 `ast::Markup::eval` 产 `Content` 而 `ast::Code::eval` 产 `Value`，类型系统替我们区分了两种模式。

最后看一眼 `Vm` 长什么样：

[crates/typst-eval/src/vm.rs:18-30](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/vm.rs#L18-L30) `Vm` 只有五个字段：`engine`（共享引擎）、`flow`（控制流信号，4.4 的主角）、`scopes`（作用域栈，4.3 的主角）、`inspected`（IDE 追踪的 span）、`context`（`context` 表达式的隐式环境）。每个模块求值、每次函数调用都会新建一个 `Vm`（见 4.4.3 中闭包调用的代码），所以它必须轻。

#### 4.1.4 代码实践

**实践目标**：不修改任何源码，直观感受「eval 是编译流水线的一个计时阶段」。

**操作步骤**：

1. 在仓库根目录写一个 `demo.typ`：

   ```typst
   #let greet(name) = [Hello, #name!]
   #for i in range(3) [
     #greet("Typst") #i
   ]
   ```

2. 用源码构建的 CLI 编译并导出计时数据：

   ```bash
   cargo run -p typst-cli -- compile demo.typ demo.pdf --timings timings.json
   ```

3. 打开 `timings.json`，在 JSON 里搜索 `"name": "eval"`（该文件可载入 <https://ui.perfetto.dev> 可视化）。

**需要观察的现象**：`timings.json` 中存在一条名为 `eval` 的计时记录（对应 `#[typst_macros::time(name = "eval", ...)]` 埋点），以及 `for loop` 的子区间记录（来自 flow.rs 里的同名埋点，见 4.4.3）。

**预期结果**：能看到 eval 阶段的耗时区间，且其内部嵌套着循环的更小区间。具体 JSON 字段结构以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`eval()` 为什么在求值前先检查 `root.errors_and_warnings()`，而不是边求值边报错？

**答案**：因为解析阶段从不失败（u2-l3），错误以 `Error` 节点形式留在树上；树上既然已有已知错误，继续解释执行可能产生误导性的连锁错误，所以提前收割。唯一例外是 IDE 追踪模式（`vm.inspected.is_some()`），为了让悬停提示仍然给出值而继续执行。

**练习 2**：`eval()` 返回的 `Module` 里装了什么？`#import` 时导入的到底是什么？

**答案**：装了两样东西——模块求值产出的 `Content`（`.with_content(output)`）和求值结束时的顶层作用域 `vm.scopes.top`（`Module::new(name, vm.scopes.top)`）。`#import "x.typ": a` 拿到的 `a` 就是从这个顶层作用域里按名字取出的绑定；`#include "x.typ"` 拿到的则是那份 `Content`。

### 4.2 代码模式与标记模式：两个主循环

#### 4.2.1 概念说明

Typst 语法有 markup、code、math 三种模式（u2-l2/u2-l3）。求值层面真正有「主循环」的是前两种，它们结构惊人地对称，但产出类型不同：

- **markup 主循环**（`eval_markup`）：逐个求值标记表达式，把结果**收集进一个 `Vec<Content>`**，最后 `Content::sequence(seq)` 拼成一份内容。适合「文档是一串东西」的直觉。
- **code 主循环**（`eval_code`）：逐个求值代码表达式，用 `ops::join` 把相邻的 `Value` **折叠成一个值**（`none` 是连接的单位元，字符串相连、内容相连）。适合「代码块有最后一个/合并值」的语义。

两个循环还共享一个精巧的设计：**`set`/`show` 规则只作用于「其后尚未求值的尾部」**。遇到 set/show 时，主循环不是先把余下内容求值完再补样式，而是带着「余下的表达式迭代器」递归调用自己，把产出的尾部再包上样式。这就是为什么 `#set text(size: 12pt)` 只影响后面的内容。

#### 4.2.2 核心流程

两个主循环的伪代码（合并表示）：

```text
eval_markup(vm, exprs):            eval_code(vm, exprs):
  flow = vm.flow.take()              flow = vm.flow.take()
  seq = []                           output = none
  loop:                              loop:
    expr = exprs.next()                expr = exprs.next()
    if expr 是 SetRule:                if expr 是 SetRule:
      styles = set.eval()                styles = set.eval()
      tail = eval_markup(剩余)           tail = eval_code(剩余).display()
      seq.push(tail.styled(styles))      output = join(output,
    if expr 是 ShowRule:                          Content(tail.styled(styles)))
      recipe = show.eval()             if expr 是 ShowRule: （同理，包 recipe）
      tail = eval_markup(剩余)          else:
      seq.push(tail.styled(recipe))      value = expr.eval()
    else:                                output = join(output, value)
      value = expr.eval()               若 vm.flow 有信号 → break
      Label 值 → 附加给前一个元素     若 vm.flow 有信号 → break
      其他值   → seq.push(value.display())
    若 vm.flow 有信号 → break        若外层本有信号 → 恢复
  若外层本有信号 → 恢复              return output
  return Content::sequence(seq)
```

其中 join 满足：\( \mathrm{join}(x, \mathrm{none}) = \mathrm{join}(\mathrm{none}, x) = x \)，字符串与字符串拼接、内容与内容连接，类型不合则报错。这就是 `#{ "a"; "b" }` 得到 `"ab"`、`#{ 1; 2 }` 直接报错的原因。

#### 4.2.3 源码精读

code 主循环：

[crates/typst-eval/src/code.rs:26-33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L26-L33) `eval_code` 接收**表达式迭代器**（而不是已收集的列表）——这正是 set/show「尾递归」技巧的前提：迭代器可以在递归调用中继续被消费。

[crates/typst-eval/src/code.rs:36-57](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L36-L57) 是 set/show 两个特殊分支：求出样式/recipe 后，用 `eval_code(vm, exprs)` 递归求值**剩余全部表达式**，再把结果 `styled_with_map` / `styled_with_recipe` 包起来。注意每个分支求值后都检查 `vm.flow.is_some()` 就提前退出（return 语句可能出现在 set 的参数里）。

[crates/typst-eval/src/code.rs:58-67](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L58-L67) 是普通路径：`expr.eval(vm)` 后 `ops::join(output, value)` 折叠；若 `vm.flow` 出现信号，先记一条「丢弃内容」的警告（见下）再 break。

[crates/typst-eval/src/code.rs:69-73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L69-L73) 收尾恢复外层信号（4.4.3 详述这个 take/restore 惯用法）。

大分派：

[crates/typst-eval/src/code.rs:76-160](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L76-L160) 是 `ast::Expr::eval` 的完整 match：几十种表达式各回各的 `Eval` 实现，绝大多数只是 `.map(Value::Content)` 或直接转发。两个值得注意的点：

- [crates/typst-eval/src/code.rs:140-141](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L140-L141) `set`/`show` 在**表达式位置**是被禁止的（「only allowed directly in code and content blocks」），因为它们只在主循环层级才有「作用其后」的语义。
- [crates/typst-eval/src/code.rs:152-157](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L152-L157) 每个产出的值都 `.spanned(span)` 并调用 `vm.trace_at(span, &value)`——这是 IDE 悬停提示「这个表达式的值是什么」的数据来源。

标识符查找（变量是怎么取到的）：

[crates/typst-eval/src/code.rs:162-169](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L162-L169) `ast::Ident::eval` 只有一行：在作用域栈里按名字查找（`binding_guard` 与特性开关/弃用检查有关），失败则报「unknown variable」并带上 span。

markup 主循环：

[crates/typst-eval/src/markup.rs:27-52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L27-L52) `eval_markup` 的开头与 set/show 分支，结构和 `eval_code` 完全同构，只是收集目标是 `Vec<Content>`。

[crates/typst-eval/src/markup.rs:53-75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L53-L75) 是 markup 独有的两个行为：一是 `Value::Label` 不变成内容，而是**倒序找到前一个可标注的元素**直接贴上去（`#[label]` 语法「贴给左边」的真相，重复贴/贴不到都会发警告）；二是其他任何值经 `value.display()` 转成 `Content` 后入列——这就是 `#upper("hi")`、`#1 + 2` 能直接出现在正文里的原因。

[crates/typst-eval/src/markup.rs:83-88](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L83-L88) 收尾：恢复外层信号，`Content::sequence(seq)` 把收集的片段拼成一份内容。

标记节点如何变成元素：

[crates/typst-eval/src/markup.rs:90-96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L90-L96) 纯文本求值就是把字符串打包成一个 `TextElem`——markup 求值的本质就是「语法节点 → Content 元素」的翻译。

[crates/typst-eval/src/markup.rs:150-157](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L150-L157) 与 [crates/typst-eval/src/markup.rs:217-225](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L217-L225) 分别是 `*strong*` 和 `= 标题`：先递归求值 body 得到 `Content`，再包进 `StrongElem`/`HeadingElem`（u3-l4 讲过的元素构造）。**求值阶段的产物只是「元素树」，外观要到 realize 阶段（u4-l2）才由 show 规则决定。**

最后一条辅助逻辑：

[crates/typst-eval/src/code.rs:416-437](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L416-L437) `warn_for_discarded_content`：当**无条件**的 `return` 把前面已 join 的内容整个丢弃时发警告（若内容里含 state/counter 更新还会追加提示「这些内容必须进入文档才生效」）。`FlowEvent::Return` 的第三个布尔字段「是否条件性返回」正是为这里的区分服务（见 4.4.3）。

#### 4.2.4 代码实践

**实践目标**：用现成测试验证「for 循环体在 code 模式下逐值 join」的行为。

**操作步骤**：

1. 打开 [tests/suite/scripting/for.typ:39-60](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/for.typ#L39-L60)（测试名 `for-loop-integrated`，stage 为 `eval`）：该测试在循环体里 `out += (v,)` 收集每轮结果，最后断言数组内容。
2. 运行单个测试（名字过滤；若名字过滤命中多个，配合 `--exact`）：

   ```bash
   cargo testit for-loop-integrated
   ```

3. 把该测试头改成 `--- for-loop-integrated-mine eval ---` 并复制一份到同文件（或观察原文件），故意把循环体里的 `out += (v,)` 改成 `out.push(v)` 之类不存在的写法，重跑观察报错。

**需要观察的现象**：`eval` stage 的测试只执行到求值层，不排版、不渲染；改坏后报错信息会指向出错的表达式 span。

**预期结果**：原测试通过；改坏后失败并显示与源码位置对应的错误。测试过滤器的确切匹配行为待本地验证（可先 `cargo testit --list | grep for-loop` 查看可用测试名）。

#### 4.2.5 小练习与答案

**练习 1**：`#{ "a"; "b"; 3 }` 的求值结果是什么？为什么？

**答案**：报错。`eval_code` 用 `ops::join` 折叠相邻值：`"a"` 与 `"b"` join 成 `"ab"`，但 `"ab"` 与 `3` 类型不合，join 报错（错误带在 `3` 的 span 上）。

**练习 2**：为什么 `eval_code`/`eval_markup` 接收的是 `&mut impl Iterator` 而不是 `Vec`？

**答案**：为了实现「set/show 只作用于其后内容」：遇到 set/show 时，把**还没被消费的迭代器**递归传入再调一次自己，剩余表达式求出的整体作为「尾部」被包上样式。若是已收集的列表，就得切片/拼装，既低效也别扭。

**练习 3**：markup 模式里 `#none` 会往 `seq` 里塞什么？

**答案**：`none` 也是 `Value`，走 `value.display()`：`none` 的 display 是空内容，所以等价于塞入空内容，最终不影响文档，但「占了一个位置」。这就是 `#none` 可以安全地当占位符用的原因。

### 4.3 作用域与绑定：Vm::define、Scopes 与解构

#### 4.3.1 概念说明

变量系统由三块组成：

- **`Scopes`（作用域栈）**：定义在 typst-library。`top` 是当前最内层作用域，`scopes` 是外层作用域组成的栈，`base` 指向标准库（`Library` 的全局作用域）。查找名字时从 `top` 开始逐层向外，最后落到 `base`——这就是「局部变量遮蔽全局函数」的实现。
- **块的边界**：`{...}` 代码块、`[...]` 内容块、循环体都会 `enter()`/`exit()` 一个新作用域，块内 `#let` 的绑定出块即消失。
- **绑定与解构**：`#let` 左侧不只是标识符，还可以是模式（pattern）：占位符 `_`、括号、数组/字典解构（含 `..sink` 收集剩余项）。`binding.rs` 里的 `destructure` 家族负责把一个值拆进模式里的各个绑定。

`Vm::define`/`Vm::bind` 是所有绑定的必经之路，它顺带做了两件「副业」：为 IDE 追踪记录绑定值；对将来讲成为关键字的标识符（如 `is`）提前发警告。

#### 4.3.2 核心流程

一次 `#let (a, (b, c)) = (1, (2, 3))` 的流程：

```text
LetBinding::eval
  ├─ 求值右侧 init → Value::Array((1, (2, 3)))
  ├─ 若 vm.flow 已有信号 → 直接返回 none（不绑定）
  └─ kind 为 Normal(模式) → destructure(vm, 模式, 值)
        └─ destructure_impl 按模式分派：
             Normal(ident)      → f: vm.define(ident, value)
             Placeholder(_)     → 丢弃
             Parenthesized(p)   → 递归拆 p
             Destructuring(d)   → 值是数组 → destructure_array
                                  值是字典 → destructure_dict
```

`destructure_impl` 接受一个回调 `f`：`#let` 传「定义新变量」版本，普通解构赋值 `(a, b) = (x, y)`（`DestructAssignment`）传「定位既有变量并写入」版本（定位走 access.rs 的 `Access` trait，支持 `a.b`、`a[0]` 这样的左值）。

#### 4.3.3 源码精读

作用域栈的本体：

[crates/typst-library/src/foundations/scope.rs:19-28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L19-L28) `Scopes` 三个字段：`top`、`scopes`（外层栈）、`base`（标准库）。[crates/typst-library/src/foundations/scope.rs:36-46](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L36-L46) 的 `enter`/`exit` 用 `mem::take` 交换实现 O(1) 压栈弹栈；[crates/typst-library/src/foundations/scope.rs:67-70](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L67-L70) 显示查找顺序：`once(&top).chain(scopes.iter().rev())`——从最内层往外找。

块的边界在 code.rs：

[crates/typst-eval/src/code.rs:321-341](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L321-L341) `CodeBlock::eval` 与 `ContentBlock::eval`：进来 `enter()`，求值 body，出去 `exit()`。两者的唯一差别是关联类型——前者产 `Value`，后者产 `Content`。

绑定的必经之路：

[crates/typst-eval/src/vm.rs:52-77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/vm.rs#L52-L77) `Vm::define`/`Vm::bind`：先 `trace_at` 记录绑定值（IDE 悬停显示 `#let x = 1` 的 x 值就靠它），再对 `is` 这类未来关键字发警告，最后 `scopes.top.bind(...)` 写进当前作用域。

`#let` 本体：

[crates/typst-eval/src/binding.rs:9-28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L9-L28) `LetBinding::eval`：无初始化式时值为 `none`；求值后若已有控制流信号（比如 init 里 `return` 了）就不绑定；然后按 `LetBindingKind` 分派——普通模式走 `destructure`，`#let f(x) = ...` 这种「函数定义糖」走 `vm.define(ident, value)`（右侧已被求成一个闭包值）。

解构核心：

[crates/typst-eval/src/binding.rs:45-82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L45-L82) `destructure`（面向 `#let`，回调里 `vm.define`）与 `destructure_impl`（四种模式的分派中枢，值不是 array/dict 却要解构时直接报错）。

[crates/typst-eval/src/binding.rs:84-127](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L84-L127) `destructure_array`：按位置消费，`..rest` 模式用 `(1 + len) - 模式项数` 算出 sink 尺寸切走剩余元素；数量不匹配走 [crates/typst-eval/src/binding.rs:179-209](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L179-L209) 的 `wrong_number_of_elements` 生成「too many/not enough elements」错误与 hint。

[crates/typst-eval/src/binding.rs:129-175](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L129-L175) `destructure_dict`：按名字取，`..rest` 收集**未被点名的键**组成剩余字典——`#let (a, ..rest) = (a: 1, b: 2, c: 3)` 里 `rest` 是 `(b: 2, c: 3)`。

普通解构赋值：

[crates/typst-eval/src/binding.rs:30-42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L30-L42) `DestructAssignment::eval`：与 `destructure` 共用 `destructure_impl`，但回调换成 `expr.access(vm)?` 取得可变位置再写入——复用同一套模式匹配逻辑、只换「写哪里」。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「块和循环体是独立作用域」「解构按预期工作」。

**操作步骤**：

1. 写 `scope-demo.typ`：

   ```typst
   #let (a, (b, c)) = (1, (2, 3))
   #let (head, ..tail) = (1, 2, 3, 4)
   #test(tail, (2, 3, 4))

   #{
     let inner = "块内可见"
     test(inner, "块内可见")
   }

   // 取消下一行注释，观察报错：
   // #test(inner, "块外")

   #for x in (1, 2) {
     let y = x * 10
   }
   // 取消下一行注释，观察报错：
   // #test(y, 0)
   ```

2. 编译：`cargo run -p typst-cli -- compile scope-demo.typ scope-demo.pdf`（`#test` 断言失败会以错误形式报告；`tests/suite/foundations/assert.typ` 展示了 `#test` 的用法）。

3. 依次取消两处注释再编译。

**需要观察的现象**：全部注释时编译通过；任一注释取消后报「unknown variable: inner / y」，且错误定位在块外的引用处。

**预期结果**：与 [crates/typst-eval/src/code.rs:321-330](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L321-L330) 的 `enter`/`exit` 及 [crates/typst-eval/src/flow.rs:124](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L124)（循环体作用域）的源码行为一致。

#### 4.3.5 小练习与答案

**练习 1**：`#let f(x) = x * 2` 走的是 `destructure` 还是 `vm.define`？

**答案**：`vm.define`。这种「函数定义糖」的 `LetBindingKind::Closure(ident)` 分支直接把求值出的闭包值绑定到 `f` 上；只有普通模式（含解构）才进 `destructure`（[crates/typst-eval/src/binding.rs:21-24](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L21-L24)）。

**练习 2**：为什么查找名字要「从 `top` 向外逐层」而不是全局一张大表？

**答案**：为了遮蔽（shadowing）语义：内层同名绑定先被找到，自然遮蔽外层与标准库同名项；同时每层作用域可以整体丢弃（`exit`），块内绑定不泄漏。u3-l2 讲过 prelude 与 global 的注入，这里正是消费端。

**练习 3**：`(a, b) = (b, a)` 交换变量在 Typst 里能工作吗？走哪段代码？

**答案**：能（前提是 a、b 已定义）。它解析为 `DestructAssignment`，[crates/typst-eval/src/binding.rs:30-42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L30-L42) 先求值右侧整个元组，再对每个分量 `access` 定位并写入，右侧是先整体求值的，所以不会读到已被覆盖的值。

### 4.4 控制流信号：FlowEvent 而非异常

#### 4.4.1 概念说明

`break`、`continue`、`return` 在很多解释器里用异常实现，Typst 没有这样做：**控制流是 `Vm` 上的一个普通字段 `flow: Option<FlowEvent>`**。

三种事件：

| 事件 | 语法 | 语义 |
| --- | --- | --- |
| `Break(Span)` | `break` | 跳出最近一层循环 |
| `Continue(Span)` | `continue` | 跳过本轮剩余部分 |
| `Return(Span, Option<Value>, bool)` | `return` / `return v` | 从当前函数返回；第三个布尔标记是否「条件性返回」（出现在 `if` 分支里） |

规则很简单：

- **产生**：`break`/`continue`/`return` 的 `Eval` 只在 `vm.flow` 为空时设置信号，然后返回 `none`。
- **传播**：主循环和块看到 `vm.flow.is_some()` 就立刻停止求值（不再执行后面的表达式）。
- **消费**：`for`/`while` 消费 Break/Continue；函数调用点（`call.rs`）消费 Return。
- **拒绝**：谁都不该消费的信号（比如函数体外的 `break`）一路冒到 `eval()` 收尾处，`flow.forbidden()` 转成「cannot break outside of loop」错误。

还有一个贯穿的惯用法：**进入可中断结构时 `take()` 走已有信号，结束时若外层本有信号则恢复**。这保证外层已发生的信号不被内层块意外吞掉，也不会被子结构的新信号覆盖。

#### 4.4.2 核心流程

以 `#let f(x) = { if x == 0 { return "b" }; "z" }` 为例：

```text
call f(0)                         （call.rs：新建 Vm，绑定参数 x）
  └─ body = CodeBlock
       └─ eval_code
            ├─ Conditional::eval：条件为真 → eval if 体
            │    └─ 体是 CodeBlock → eval_code
            │         └─ FuncReturn::eval：vm.flow = Return(span, Some("b"), false)
            │              随后本层 eval_code 发现信号 → break
            │    Conditional 收尾：把 Return 标记为 conditional = true
            ├─ eval_code 发现信号 → warn_for_discarded_content → break（"z" 不求值）
            └─ 返回已 join 的 output
  └─ call.rs 匹配 vm.flow：
       Return(_, Some(explicit), _) → 函数返回值为 "b"
       Return(_, None, _)          → 返回 body 求值 output
       其他                         → forbidden 错误
```

对比测试样例 [tests/suite/scripting/return.typ:11-29](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/return.typ#L11-L29)（`return-join`）：`f(0)` 得 `"b"`（提前 return），`f(1)` 得 `"ac"`（无 return，join），`f(2)` 得 `"ad"`（无条件 `return` 丢弃其后的 `"e"`）。

#### 4.4.3 源码精读

信号定义与「拒绝」：

[crates/typst-eval/src/flow.rs:12-22](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L12-L22) `FlowEvent` 枚举；[crates/typst-eval/src/flow.rs:24-39](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L24-L39) `forbidden()` 为三种事件各生成一条「cannot … outside of …」错误。

产生信号的三处：

[crates/typst-eval/src/flow.rs:191-223](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L191-L223) `LoopBreak`、`LoopContinue`、`FuncReturn` 的 `eval`：都是「若 `vm.flow` 为空则设置信号」，`return` 会先求值返回值表达式（[crates/typst-eval/src/flow.rs:217](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L217)）——若求值返回值本身触发了外层信号，就不覆盖。

循环消费信号：

[crates/typst-eval/src/flow.rs:114-146](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L114-L146) `ForLoop::eval` 的 `iter!` 宏：先 `vm.scopes.enter()` 给整个循环开一个作用域；每轮 `destructure` 绑定循环变量 → 求值 body → `ops::join` 累积；然后匹配 `vm.flow`——`Break` 清信号并跳出，`Continue` 清信号继续，`Return` 保留信号但跳出（让它继续冒到函数调用点）。

[crates/typst-eval/src/flow.rs:148-176](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L148-L176) 决定「能循环什么」：数组按值、字典按键值对、字符串按字素（仅普通/占位模式）、字节串按整数值；对字符串/字节串解构、或其他类型一律报「cannot loop over/destructure …」。

函数调用点消费 Return：

[crates/typst-eval/src/call.rs:739-748](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L739-L748) 闭包调用求值完 body 后匹配 `vm.flow`：带值的 Return 直接以该值作为函数结果；空 Return 用 body 的 join 结果；Break/Continue 出现在函数体内则 `forbidden()` 报错（「cannot break outside of loop」）。

条件返回的标记：

[crates/typst-eval/src/flow.rs:54-58](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L54-L58) `Conditional::eval` 结束时把 Return 的第三字段改为 `true`。它是 4.2.3 中 `warn_for_discarded_content` 的判据：只有**无条件**的 return 丢弃内容才警告；`if cond { return }` 属于条件性返回，丢内容是正常控制流，不警告。

while 的两条防线：

[crates/typst-eval/src/flow.rs:63-83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L63-L83) `WhileLoop::eval` 开头定义了 `MAX_ITERATIONS = 10_000`（[crates/typst-eval/src/flow.rs:9-10](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L9-L10)），第 0 轮若条件是「不变式且循环体不能发散」（辅助函数 [crates/typst-eval/src/flow.rs:226-245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L226-L245)）直接报「condition is always true」；否则最多跑一万轮报「loop seems to be infinite」。`while` 的循环体匹配与 `for` 相同（[crates/typst-eval/src/flow.rs:88-96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L88-L96)）。

顶层兜底：

[crates/typst-eval/src/lib.rs:88-91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L88-L91) 模块顶层若还有信号（文档里直接写 `#return`），转成 forbidden 错误。`eval_string` 收尾同理（[crates/typst-eval/src/lib.rs:169-172](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L169-L172)）。

递归防护（函数版）：

[crates/typst-eval/src/call.rs:31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L31) 每次函数调用前 `route.check_call_depth()`——无限递归会在调用深度超限时被拦下（模块环的防护在 import 层，见 4.5）。

#### 4.4.4 代码实践

**实践目标**：验证信号的「产生—传播—拒绝」三段行为。

**操作步骤**：

1. 写 `flow-demo.typ`：

   ```typst
   // ① 循环正常消费 break
   #for i in range(10) {
     if i == 3 { break }
     [#i ]
   }

   // ② 函数消费 return
   #let early(n) = {
     if n < 0 { return "负数" }
     "非负"
   }
   #test(early(-1), "负数")

   // ③ 顶层 return 被拒绝：取消下行注释
   // #return
   ```

2. `cargo run -p typst-cli -- compile flow-demo.typ flow-demo.pdf`。
3. 取消 `#return` 那行注释，重新编译，观察错误消息。
4. 再运行现成测试：`cargo testit return-with-value`（对应 [tests/suite/scripting/return.typ:3-9](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/return.typ#L3-L9)）。

**需要观察的现象**：① 输出 `0 1 2 ` 后停止；③ 报错 `cannot return outside of function`，定位到 `#return` 所在 span。

**预期结果**：与 `flow.forbidden()`（[crates/typst-eval/src/flow.rs:34-36](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L34-L36)）及 `eval()` 收尾检查（[crates/typst-eval/src/lib.rs:88-91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L88-L91)）一致。`cargo testit return-with-value` 的名字过滤行为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`#let f() = { break; 1 }`，调用 `f()` 会怎样？

**答案**：函数体求值时 `break` 设置信号，`eval_code` 停止；到 `call.rs` 的匹配处，Break 不属于函数应消费的事件，走 `forbidden()` 报「cannot break outside of loop」（[crates/typst-eval/src/call.rs:744](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L744)）。

**练习 2**：为什么每个可中断结构开头都要 `vm.flow.take()`、结尾条件性恢复？

**答案**：`take()` 把「进入前就存在的信号」暂存并清空，保证本结构内对信号的判断是干净的；结尾若外层本有信号则恢复——外层信号优先于本结构内新产生的信号，避免子作用域把传播中的 return 改写或吞掉。

**练习 3**：`#while true { break }` 会触发「loop seems to be infinite」吗？

**答案**：不会。`break` 在第 0 轮就把 `vm.flow` 设为 Break，循环匹配后清信号跳出；`MAX_ITERATIONS` 只统计完整执行的轮数。而 `#while 1 + 1 == 2 []` 这种条件恒真且体不能发散的，第 0 轮就被 `is_invariant`/`can_diverge` 判定为「condition is always true」（[crates/typst-eval/src/flow.rs:76-83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L76-L83)）。

### 4.5 模块导入：import 的递归求值

#### 4.5.1 概念说明

Typst 没有「编译单元链接」的概念，模块系统是解释器层面的**按需递归求值**：

- `#import "module.typ": a, b` —— 把目标文件（或包、或模块值、或带作用域的函数/类型）作用域里的指定绑定引入当前作用域。
- `#include "module.typ"` —— 把目标模块求值产出的 `Content` 直接插入当前位置。

「导入一个模块」的完整含义是：解析路径/包名 → 通过 World 读到源文件 → **递归调用 `eval()`** 求值整个文件 → 从返回的 `Module` 顶层作用域里取绑定。由于 `eval()` 带 `#[comemo::memoize]`，同一文件无论被 import 多少次，实际只求值一次（增量编译友好）。

两道防线防环：`import_file` 里基于 `route` 的「cyclic import」报错，和 `eval()` 入口的 panic 兜底（4.1.3）。

#### 4.5.2 核心流程

`#import "utils/lib.typ": helpers.sort as sort` 的流程：

```text
ModuleImport::eval
  ├─ 求值 source 表达式
  │    Value::Str("utils/lib.typ") → import(engine, path, span)
  │        ├─ 以 '@' 开头 → 解析 PackageSpec → import_package
  │        │     └─ resolve_package：读 typst.toml → 校验 → 取 entrypoint FileId
  │        └─ 否则 → 相对当前 span 所在文件解析路径 → import_file(FileId)
  │             ├─ engine.world.source(id) 读源文件
  │             ├─ route.contains(id)? → 报 "cyclic import"
  │             └─ 递归 eval(...) → Module（可能命中 comemo 缓存）
  ├─ 若有 as new_name → vm.define(new_name, 整个模块)
  └─ 按 imports 绑定：
       None（裸导入）     → 用文件名/包名作绑定名（动态字符串需显式 as）
       Wildcard (*)       → 遍历模块 scope，逐个 bind（跳过特性门控项）
       Items(a, b as c)   → 逐项（支持 a.b.c 嵌套路径）从 scope 取出 bind
```

#### 4.5.3 源码精读

source 可以是什么：

[crates/typst-eval/src/import.rs:24-60](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L24-L60) `ModuleImport::eval` 先求值 source 再按类型分派：函数（须有作用域，如模块内部函数）、类型、模块直接可用；字符串走 `import()`（[crates/typst-eval/src/import.rs:35-42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L35-L42)），`RootedPath` 值走 `import_file`；其余类型报「expected path, module, function, or type」。

改名与裸导入：

[crates/typst-eval/src/import.rs:63-77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L63-L77) `as new_name` 把整个模块绑定到新名字（`import x as x` 会收到「unnecessary rename」警告）。

[crates/typst-eval/src/import.rs:81-108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L81-L108) 裸导入（不带冒号）：从源表达式推导绑定名（文件名去扩展名、包名）；动态字符串或非法标识符路径要求显式 `as` 命名，`#import module`（标识符形式）会警告「this import has no effect」。

两种导入清单：

[crates/typst-eval/src/import.rs:110-118](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L110-L118) 通配导入：遍历模块作用域逐个 `bind`，特性门控未启用的项被 `silent_binding_guard` 静默跳过。

[crates/typst-eval/src/import.rs:119-188](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L119-L188) 列表导入：支持 `chap2.chap1.name` 这样的**嵌套路径**——逐段下钻子模块作用域（中途不是模块则报「expected module, function, or type」），最后一段取出绑定 `vm.bind(...)`；`x as x` 同样有警告。

路径与包：

[crates/typst-eval/src/import.rs:223-234](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L223-L234) `import()`：以 `@` 开头按包规范解析（`@preview/...` 形式），否则把路径解析到 `FileId`——相对路径以**当前 span 所在文件**为基准。

[crates/typst-eval/src/import.rs:238-256](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L238-L256) `import_file`：`engine.world.source(id)` 读源文件（World 抽象，u3-l1）；`route.contains` 防「cyclic import」；然后**递归调用 `eval()`**。注意循环导入在这里报的是带 span 的友好错误，而不是 4.1.3 那个兜底 panic。

[crates/typst-eval/src/import.rs:259-292](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L259-L292) 包导入：读取并解析包根的 `typst.toml` 清单，`manifest.validate` 校验名称版本匹配，取 `entrypoint` 字段得到入口文件，再走 `import_file`。

include：

[crates/typst-eval/src/import.rs:195-220](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L195-L220) `ModuleInclude::eval`：同样的导入流程，返回 `module.content()`——把模块求值时产出的正文直接并入当前文档，对应 4.1.3 里 `Module::new(...).with_content(output)` 的那份内容。

#### 4.5.4 代码实践

**实践目标**：亲手跟踪「从 `eval()` 入口到一次 `#import` 完成」的完整调用栈（本讲的综合任务预演版）。

**操作步骤**：

1. 建两个文件。`lib.typ`：

   ```typst
   #let version = "1.0"
   #let double(x) = x * 2
   ```

   `main-demo.typ`：

   ```typst
   #import "lib.typ": double, version
   #let n = double(21)
   #for i in range(2) [
     #version — #n — #i
   ]
   ```

2. 先正常编译确认通过：`cargo run -p typst-cli -- compile main-demo.typ main-demo.pdf`。

3. **在你的本地工作副本中**（练习性质、可随时 `git checkout -- crates/typst-eval` 还原）给下列五个位置各加一行 `eprintln!` 打点：
   - [crates/typst-eval/src/lib.rs:47](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L47)（`eval` 入口，打印 `source.id()` 对应的 vpath）
   - [crates/typst-eval/src/markup.rs:34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L34)（markup 主循环每个表达式，打印 `expr.span()` 或 kind）
   - [crates/typst-eval/src/binding.rs:12](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/binding.rs#L12)（`LetBinding::eval` 入口）
   - [crates/typst-eval/src/flow.rs:118](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/flow.rs#L118)（`ForLoop::eval` 入口）
   - [crates/typst-eval/src/import.rs:238](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L238)（`import_file` 入口，打印 `id`）
4. 重新编译（增量构建很快）：`cargo run -p typst-cli -- compile main-demo.typ main-demo.pdf`。
5. 记录打点输出顺序，然后还原源码：`git checkout -- crates/typst-eval`。

**需要观察的现象**：输出里应能看到两次 `eval` 入口（`main-demo.typ` 在前），`import_file` 出现在两次 `eval` 之间；markup 循环按序吐出 `import`、`let`、`for` 等节点；`ForLoop` 打点被触发一次、其循环体表达式在 `eval`（lib.typ）之后出现。

**预期结果**（调用链骨架）：

```text
eval(main-demo.typ)
 └─ eval_markup
     ├─ ModuleImport::eval ─ import("lib.typ") ─ import_file(lib.typ)
     │     └─ eval(lib.typ) ─ eval_markup(lib.typ)   ← 递归求值，产出 Module
     ├─ LetBinding::eval（n = double(21)）
     │     └─ （函数调用进入 call.rs，u3-l5 已覆盖）
     └─ ForLoop::eval ─ enter 作用域 ─ 2 轮 body
```

具体打点文本以你的 eprintln 内容为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：`#import "lib.typ"`（不带冒号）和 `#import "lib.typ": *` 有什么区别？

**答案**：前者是**裸导入**：把整个模块作为一个值绑定到推导出的名字 `lib`，使用时要写 `lib.double`；后者是**通配导入**：遍历模块顶层作用域，把每个导出绑定直接引入当前作用域（特性门控项除外），可以直接写 `double`。分别对应 [crates/typst-eval/src/import.rs:81-108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L81-L108) 与 [crates/typst-eval/src/import.rs:110-118](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L110-L118)。

**练习 2**：三个文件 `A import B`、`B import C`、`C import A`，编译哪个文件会在哪里被拦下？

**答案**：求值 A 时 route 为 `[A, B, C]`，轮到 C 再 import A，`import_file` 里 `engine.route.contains(A)` 成立，在 C 的 import 语句 span 上报「cyclic import」（[crates/typst-eval/src/import.rs:243-245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/import.rs#L243-L245)）。`eval()` 入口的 panic 只是兜底，正常路径不会走到。

**练习 3**：`lib.typ` 被 import 两次，会被求值两次吗？

**答案**：不会。`eval` 标注了 `#[comemo::memoize]`（[crates/typst-eval/src/lib.rs:38](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L38)），第二次 `import_file` 递归调用 `eval` 时直接命中缓存返回同一份 `Module`（输入 `Source` 未变的前提下）。这也是 watch 模式下改一处只重算受影响模块的基础（u5-l8）。

## 5. 综合实践

把本讲四个机制串成一条线：**用打点还原一段 `#let` + `#for` + `#import` 脚本的完整求值调用栈**。

**任务**：给定如下 `trace.typ` 与 `greet.typ`：

```typst
// greet.typ
#let greet(name) = [Hello, #name!]
```

```typst
// trace.typ
#import "greet.typ": greet
#let names = ("Alice", "Bob")
#for name in names [
  #greet(name)
]
```

**步骤**：

1. **基线**：编译通过并查看输出：`cargo run -p typst-cli -- compile trace.typ trace.pdf`。
2. **无侵入观测**：加 `--timings timings.json`，确认其中出现 `eval`、`for loop` 两级计时区间（埋点见 4.1.3 与 4.4.3）。
3. **打点还原调用链**：在本地工作副本按 4.5.4 的五个位置加 `eprintln!`，另外在 [crates/typst-eval/src/markup.rs:90-96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L90-L96)（Text 节点）加一个，重新编译并记录输出。
4. **整理成表**：把打点输出整理成「层级 | 函数 | 关键分支 | 对应源码行」的表格，至少覆盖：`eval(trace.typ)`、`eval_markup`、`ModuleImport::eval → import → import_file → eval(greet.typ)`、`LetBinding::eval → vm.define`、`ForLoop::eval → scopes.enter → destructure → body`、`Text::eval → TextElem::packed`。
5. **验证一个预测**：先预测「把 `#let names` 移到 `#for` 之后会怎样」（答案：markup 主循环按出现顺序求值，for 时 `names` 尚未定义，报 unknown variable），再实际交换验证。
6. **清理**：`git checkout -- crates/typst-eval` 还原所有打点。

**预期产出**：一张约 8～12 行的调用链表 + 一条你亲手验证过的顺序依赖结论。所有打点输出因 eprintln 内容而异，待本地验证。

## 6. 本讲小结

- `typst-eval` 是树遍历解释器：入口 `eval()` 负责「防环 → 组装 Engine/Vm → 语法错误预检 → `markup.eval()` → 残留信号拒绝 → 打包 `Module`」；每种 AST 节点实现统一的 `Eval` trait。
- 两个主循环对称而不同：`eval_markup` 收集 `Content` 成序列（标签直接贴给左侧元素、值经 `display()` 入列），`eval_code` 用 `ops::join` 把 `Value` 折叠成一个值；两者都以「带着迭代器递归调用自己」实现 set/show 只作用于其后内容。
- 变量系统 = `Scopes` 作用域栈（`top` + 外层栈 + 标准库 `base`，从内向外查找）+ `Vm::define/bind` 唯一写入口（附带 IDE 追踪与未来关键字警告）+ `destructure` 家族处理 `#let` 模式与解构赋值。
- 控制流是数据不是异常：`Vm::flow` 携带 `FlowEvent::{Break, Continue, Return}`，主循环见信号即停，`for`/`while` 消费循环事件、闭包调用点消费 Return，顶层残留由 `forbidden()` 收尾；`take()`/恢复惯用法保证外层信号优先。
- 模块即文件求值结果：`#import` 解析字符串路径或 `@包名` → 读 manifest/源文件 → **递归 `eval()`**（comemo 记忆化，重复导入不求值两次）→ 从 `Module` 顶层作用域取绑定；`route` 防循环导入。
- 求值只到「元素树」为止：`*strong*`、`= 标题` 在此变成 `StrongElem`/`HeadingElem`，外观留给 realize 阶段的 show 规则（下一讲）。

## 7. 下一步学习建议

- **u4-l1（编译主流程总览）**：把本讲的 eval 放回 parse → eval → realize → layout → export 五阶段数据流，看 `Module::content` 如何交接给排版。
- **u4-l2（realize 阶段）**：本讲多次出现的 `styled_with_map`/`styled_with_recipe` 产出的样式化 Content 如何被 show 规则消费，正是下一讲的入口；可先读 [crates/typst-eval/src/rules.rs:11-66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L11-L66) 里 set/show 规则如何求值出 styles/recipe。
- **u3-l7（Styles 与 set/show 数据模型）**：如果想先补齐样式链的数据结构基础。
- **延伸阅读（选）**：[crates/typst-eval/src/access.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/access.rs)（赋值左值的定位）与 [crates/typst-eval/src/math.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/math.rs)（数学模式求值），两者机制都已在本讲的框架内。
