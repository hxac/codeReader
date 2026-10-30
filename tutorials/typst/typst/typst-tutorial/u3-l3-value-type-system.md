# Value 类型系统：从 none 到 dict

> 本讲基于 HEAD `64c5e8c02`，引用的行号与永久链接均指向该提交。前置讲义：u3-l2（Library 与 global 作用域）。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `Value` 枚举的全部 30 个变体及其 Rust 表示，理解「一个枚举装下脚本世界所有值」以及 32 字节的尺寸约束。
- 解释 `Value::ty()` 与 `Type` 如何给出值的类型标识，以及 short name / long name / title 在代码、诊断、文档三种场合的分工。
- 跟踪 `ops::add` 等运算符函数如何按「类型组合」做二维派发，`mismatch!` 宏如何用类型名生成统一格式的错误消息。
- 掌握 str / array / dictionary 等基础类型通过 `#[scope]` 注册方法表的机制，以及 `#((1,2).len())` 这类方法调用在求值器中的完整路径。
- 亲手构造 int、str、array、dict 四种 `Value`，执行加法与拼接、观察 repr，并故意触发一次类型错误。

## 2. 前置知识

本讲要用到几个 Rust 概念，用通俗语言先过一遍：

- **枚举（enum）**：Rust 的枚举是「带数据的标签」。`Value::Int(i64)` 表示「这个值是 Int 标签，货物是一个 i64」。`match` 可以按标签穷举，编译器会强制你处理每一种情况。
- **新类型模式（newtype）**：`pub struct Str(EcoString)` 只是把现成类型包一层，零开销地区分「任意 Rust 字符串」和「Typst 脚本里的字符串类型」。
- **EcoString / EcoVec**：来自 `ecow` crate 的字符串/向量，短内容直接存在栈内，克隆是廉价的（引用计数起步）。
- **Arc**：原子引用计数的智能指针，多个值共享同一份底层数据，写之前用 `make_mut` 做写时复制。字典、数组的廉价克隆都靠它。
- **IndexMap**：保持**插入顺序**的哈希表。普通 `HashMap` 的遍历顺序是乱的，而 Typst 字典的遍历和 repr 顺序就是写入顺序。
- **StrResult / HintedStrResult**：Typst 内部的错误类型——前者是纯字符串错误，后者额外携带「提示（hint）」。运算符函数几乎都返回它们。
- **作用域（Scope）**：u3-l2 讲过 `Library.global` 这个全局作用域；本讲的 `#[scope]` 则是把方法挂到**类型**上的作用域，两者结构相同、挂载点不同。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crates/typst-library/src/foundations/value.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs) | `Value` 枚举：所有脚本值的统一容器，本讲的纲 |
| [crates/typst-library/src/foundations/ty.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs) | `Type`：类型标识，持有名字与方法表 |
| [crates/typst-library/src/foundations/ops.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs) | 运算符按类型组合派发：add/sub/mul/div/compare… |
| [crates/typst-library/src/foundations/str.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs) | 字符串类型 `Str` 与 23 个 `#[func]` 方法 |
| [crates/typst-library/src/foundations/array.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/array.rs) | 数组类型 `Array` 与 34 个方法 |
| [crates/typst-library/src/foundations/dict.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs) | 字典类型 `Dict` 与 10 个方法 |
| [crates/typst-library/src/foundations/int.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/int.rs)、[float.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/float.rs)、[bytes.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/bytes.rs) | i64 / f64 / 字节块的原生实现与方法 |
| [crates/typst-library/src/foundations/repr.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/repr.rs) | `Repr` trait 与全局 `repr()` 函数 |
| [crates/typst-eval/src/ops.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/ops.rs) | 把脚本里的二元运算符接线到 foundations::ops |
| [crates/typst-eval/src/call.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs) | 字段访问与方法调用的求值派发 |
| [tests/suite/scripting/ops.typ](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/ops.typ) 与 [tests/suite/foundations/](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations) | 运算符与基础类型的行为基准（练习场所） |

## 4. 核心概念与源码讲解

### 4.1 Value 枚举与类型标识

#### 4.1.1 概念说明

Typst 脚本里的一切——`none`、整数、字符串、数组、函数、甚至排版内容——在 Rust 侧都装进同一个类型 `Value`。为什么用一个大枚举而不是 trait 对象？

- **性能**：枚举值是栈上的紧凑数据，没有虚表跳转；单元测试还硬性约束它的尺寸**不超过 32 字节**。
- **可穷举**：`match` 一个 `Value` 时编译器强制处理每个变体，新增变体时所有遗漏点都会报错。这比「到处 downcast 一个 trait 对象」安全得多。
- **逃生舱**：Rust 生态里既有的类型（比如布局用的 `Alignment`）不想重复造轮子，就走 `Value::Dyn(Dynamic)` 这条路，把任意满足约束的类型装箱进来。

#### 4.1.2 核心流程

一个 `Value` 在编译器里有三副「面孔」，分别服务不同场合：

| 面孔 | 入口 | 用途 |
|---|---|---|
| 展示形态 | `Value::display()` | 把值放进文档时的样子（`#1` 在 PDF 里显示 `1`） |
| 源码形态 | `Repr::repr()` | 把值变回「可以重新读入的 Typst 源码」（错误消息、调试） |
| 调试形态 | `impl Debug` | Rust 侧 `dbg!` / 断言用 |

而「这个值是什么类型」由 `Value::ty()` 回答：每个变体映射到对应的 `Type`，`Type` 再携带该类型的名字（三种）与方法表（scope）。诊断消息里看到的 `integer`、`string`，都是 `Type` 的 long name。

#### 4.1.3 源码精读

**① 枚举本体：30 个变体，`none` 是默认值。**

- [value.rs:L26-L88](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L26-L88) 定义 `pub enum Value`，每个变体的文档注释就是它在脚本里的写法。按语义分组大致是：空值语义（`None` 默认、`Auto`）；标量（`Bool`、`Int(i64)`、`Float(f64)`、`Decimal`）；几何量（`Length`、`Angle`、`Ratio`、`Relative(Rel<Length>)`、`Fraction`）；可视化（`Color`、`Gradient`、`Tiling`）；符号与文本（`Symbol`、`Str`、`Bytes`）；身份与时间（`Label`、`Version`、`Datetime`、`Duration`）；文档（`Content`、`Styles`）；集合（`Array`、`Dict`）；可调用与元对象（`Func`、`Args`、`Type`、`Module`）；以及兜底的 `Dyn(Dynamic)`。
- 注意 `#[default]` 挂在 `None` 上（L28-29）：`Value::default() == Value::None`。

**② 尺寸约束写成了单元测试。** [value.rs:L720-L723](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L720-L723) 断言 `size_of::<Value>() <= 32`。这就是「30 个变体也要挤进一个机器字加负载」的工程纪律——大负载（字符串、数组）放在堆上，枚举里只存指针。

**③ 类型标识：`Value::ty()` 把变体翻译成 `Type`。**

- [value.rs:L116-L149](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L116-L149)：每个变体返回 `Type::of::<T>()`，例如 `Self::Int(_) => Type::of::<i64>()`、`Self::Str(_) => Type::of::<Str>()`；`Dyn(v)` 委托给动态值自己报告的类型（L147）。
- [ty.rs:L64-L66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L64-L66)：`Type` 本身是指向静态 `NativeTypeData` 的轻量句柄（`Copy`、可哈希）。
- 一个类型有三个名字，见 [ty.rs:L74-L87](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L74-L87)：`short_name` 是代码里用的（如 `str`，即 `type("a")` 的返回值）；`long_name` 用于诊断（如 `string`）；`title` 用于文档标题（如 `String`）。
- [ty.rs:L179-L183](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L179-L183)：`Type` 的 `Display` 用的是 **long_name**——这就是错误消息里出现「cannot add integer and string」而不是「int + str」的原因（4.2 节闭环验证）。

**④ 展示形态：`display()` 决定值进入文档的样子。**

- [value.rs:L191-L207](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L191-L207)：`None` 变空内容；`Int`/`Float`/`Str`/`Symbol` 等变成文本元素；其余一切（数组、字典、函数……）退化为一段 `typc` 语言的行内 raw 代码，内容正是它的 `repr()`（L202-205）。

**⑤ 源码形态：`Repr` 的总派发。**

- [value.rs:L256-L291](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L256-L291)：`Value` 的 `repr()` 把 30 个变体一一转发给各自类型的 `Repr` 实现。
- [repr.rs:L50-L53](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/repr.rs#L50-L53) 定义 trait 本体：`fn repr(&self) -> EcoString`；脚本层的全局函数 `repr(x)` 在 [repr.rs:L41-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/repr.rs#L41-L42)（`#[func]` 标注的 `pub fn repr`）。

**⑥ 内置测试是 repr 的「黄金基准」。** [value.rs:L711-L751](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L711-L751) 的 `mod tests` 定义了 `test(value, exp)` 辅助函数（断言 `value.into_value().repr() == exp`），并给出基准：字符串 `"hello"` 的 repr 是带引号的 `"\"hello\""`、空数组 `"()"`、单元素数组 `"(none,)"`（保留逗号）、空字典 `"(:)"`、字典 `(two: false, one: 1)` **保持插入顺序**。

**⑦ Rust 运算符也走同一张表。** [value.rs:L293-L303](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L293-L303)：`Value` 的 `PartialEq` 委托给 `ops::equal`，`PartialOrd` 委托给 `ops::compare`——脚本层的 `==`/`<` 与 Rust 层用的是同一套语义（见 4.2）。

**⑧ 逃生舱 `Dynamic`。** [value.rs:L92-L97](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L92-L97) 的 `Value::dynamic` 要求装入的类型满足 `Debug + Repr + NativeType + PartialEq + Hash + Sync + Send + 'static`；底层容器是 [value.rs:L495](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L495) 的 `Dynamic(Arc<dyn Bounds>)`。

#### 4.1.4 代码实践

**实践目标**：用内置单元测试检验你对 repr 的预测能力。

1. 阅读黄金基准 [value.rs:L726-L751](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L726-L751)。
2. 不看源码，先预测下面几个值的 repr：`3.0`、`(1, 2, 3)`、键为 `"a b"` 的字典（提示：这个键不是合法标识符）。
3. 运行 `cargo test -p typst-library test_value`（会执行 `test_value_size` 与 `test_value_debug`）。
4. 核对预测：`3.0` 见 4.3 ④ 的 float 实现；非标识符键的字典见 4.3 ③ 的 `is_ident` 分支。

**观察现象**：测试输出 `test_value_debug ... ok`。

**预期结果**：两个测试通过；整数值的 float repr 也带 `.0`（与 int 区分）。命令未在本环境执行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Value` 有 30 个变体，为什么还能不超过 32 字节？
**答案**：枚举本身只存标签加一个小负载；字符串、数组等大负载放在堆上（`EcoString`/`Arc`），枚举里是指针。[value.rs:L720-L723](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L720-L723) 的断言把这个约束固化为测试。

**练习 2**：错误消息说「cannot add integer and string」，但代码里类型短名是 `int`、`str`。名字从哪来？
**答案**：`Type` 的 `Display` 用 `long_name()`（[ty.rs:L179-L183](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L179-L183)），integer/string 正是 `i64`/`Str` 类型的 long name。

**练习 3**：`#none` 和 `#(1, 2)` 直接放进文档，各自渲染成什么？
**答案**：`none` 经 `display()` 变成空内容；数组落到兜底分支，被包成一段 `typc` 行内代码，显示其 repr `(1, 2)`（[value.rs:L191-L207](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L191-L207)）。

### 4.2 运算符派发 ops

#### 4.2.1 概念说明

脚本里的 `+` 语义按「左右类型组合」定义：`1 + 2` 是算术，`"a" + "b"` 是拼接，`1pt + 50%` 产生相对长度，`red + 1pt` 甚至会产生一条描边。这是一张**类型组合 → 行为**的二维表，Rust 用 `match (lhs, rhs)` 穷举它；没写到的组合落进兜底分支，由 `mismatch!` 宏生成错误。

关键设计：**错误消息里打印的是类型名而不是值**。这样消息简短、可预测，也不会把用户数据（比如超长数组）整段塞进诊断。

#### 4.2.2 核心流程

以 `#(1 + "a")` 为例，完整链路是：

```text
脚本 #(1 + "a")
 → typst-eval 求值 ast::Binary（crates/typst-eval/src/ops.rs:L26-L41）
 → BinOp::Add => apply_binary(self, vm, ops::add)
 → foundations::ops::add(Value::Int(1), Value::Str("a"))
 → match (lhs, rhs) 逐臂匹配……全部落空
 → 兜底臂 mismatch!("cannot add {} and {}", a, b)
 → 宏把 a.ty()/b.ty() 填进 {}（ops.rs:L16-L21）
 → Type 的 Display 输出 long_name
 → 最终错误："cannot add integer and string"
```

这条消息不是猜测——它是仓库测试里钉死的基准：[tests/suite/scripting/ops.typ:L452-L454](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/ops.typ#L452-L454)。另外注意：**短路求值发生在求值器而非 ops**——[crates/typst-eval/src/ops.rs:L59-L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/ops.rs#L59-L73) 的 `apply_binary` 先求左边操作数，`and`/`or` 遇到定值直接返回，右边根本不求值。

#### 4.2.3 源码精读

**① 错误消息的「模板引擎」：`mismatch!` 宏。**

- [ops.rs:L16-L21](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L16-L21)：宏体只有一行 `eco_format!($fmt, $($value.ty()),*)`——格式串里的每个 `{}` 都被填成对应值的**类型**。
- 于是全文件的错误消息形态统一：「cannot join X with Y」（[L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L42)）、「cannot add X and Y」（[L159-L170](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L159-L170)）、「cannot subtract {1} from {0}」（[L214](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L214)，注意占位符顺序对调）、「cannot multiply X with Y」（[L284](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L284)）、「cannot compare X and Y」（[L500](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L500)）。

**② 加法：一张类型组合表的样板。** [ops.rs:L91-L172](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L91-L172) 的 `pub fn add`，值得逐段读：

- `none` 是加法单位元：`(a, None) => a, (None, b) => b`（[L94-L95](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L94-L95)）。
- 数值组用 `checked_add` 防溢出，溢出映射为 `too_large()` 错误（[L97](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L97)，消息见 [L575-L577](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L575-L577) 的 `"value is too large"`）；`Int + Float` 混算向 Float 提升（[L98-L100](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L98-L100)）。
- 几何代数：`Length + Ratio` 升级为 `Relative`（[L113-L125](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L113-L125)）——这就是 `20% + 5cm` 合法的根源。
- 文本与集合：`Str + Str` 拼接、`Symbol + Symbol` 变字符串、`Content` 与文本互拼、`Array + Array`、`Dict + Dict`、`Args + Args`（[L129-L142](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L129-L142)）。
- 跨域彩蛋：`Color + Length` 产生 `Stroke`（描边），且两个操作数顺序都可（[L144-L153](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L144-L153)）。
- `Dyn + Dyn` 的特例：两个 `Alignment` 相加得到对齐组合，其余报错（[L159-L168](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L159-L168)）——`top + right` 的实现位置。
- 最后的兜底臂 `(a, b) => mismatch!(...)`（[L170](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L170)）收走一切未定义组合。

**③ 一元与比较。**

- 一元负号 [ops.rs:L73-L88](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L73-L88)：数值与几何量取负，其余 `mismatch!`。
- `compare` [ops.rs:L471-L502](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L471-L502)：同类型基本可比；跨类型只放行数值互比（`Int`/`Float`/`Decimal`，L487-L490）和「退化的 Relative」（比值为零时 Length 与 Relative 可比，L491-L494）；数组按字典序逐元素比（辅助函数 `try_cmp_arrays` 在 [L517](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L517)）。字典**没有**排序臂——对两个字典用 `<` 会得到「cannot compare」错误。

**④ 谁在调用这张表。**

- 脚本侧：[crates/typst-eval/src/ops.rs:L26-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/ops.rs#L26-L41) 把 `ast::BinOp::Add => apply_binary(self, vm, ops::add)`（Sub/Mul/Div/Eq/Lt… 同理），求值器只做转发，语义全在 foundations。
- 标记拼接侧：求值器把标记片段逐个并进已有内容时调用的是更严格的 `ops::join`（[crates/typst-eval/src/code.rs:L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L61)）。`join` 与 `add` 的区别见 [ops.rs:L24-L44](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L24-L44)：它只接受「可拼接」的组合（数值不参与），错误消息是「cannot join X with Y」——测试基准见 [tests/suite/foundations/array.typ:L345-L349](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations/array.typ#L345-L349)。
- Rust 侧：`PartialEq`/`PartialOrd` 也委托到 `equal`/`compare`（4.1 ③ ⑦），两层不会漂移。

#### 4.2.4 代码实践

**实践目标**：验证「错误消息 = 模板 + 类型名」的规律。

1. 在任意临时目录创建 `ops-lab.typ`（示例代码，非仓库文件）：

   ```typst
   #(1 + "a")
   ```

2. 编译：`cargo run -p typst-cli -- compile ops-lab.typ`（或已安装的 `typst compile ops-lab.typ`）。
3. 观察错误消息，然后逐个替换表达式再编译，先预测再验证：

   ```typst
   #(true + true)   // 预测：cannot add boolean and boolean
   #((1,) + none)   // 预测：不报错——none 是加法单位元，结果 (1,)
   #(none - 1)      // 预测：报错——sub 没有 none 单位元臂（以实际输出为准）
   ```

**观察现象**：每个错误都形如「cannot <动词> <类型 long name> (with|and|by) <类型 long name>」。

**预期结果**：第一条消息与 [ops.typ:L454](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/ops.typ#L454) 的基准一致。命令未在本环境执行，**待本地验证**；后两条的动词与顺序由对应 `mismatch!` 模板决定（[L214](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L214)、[L284](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L284)、[L340](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L340)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `#(1 + none)` 不报错而 `#(1 - none)` 的行为不同？
**答案**：`add` 显式写了 `(a, None) => a`（[ops.rs:L94-L95](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L94-L95)），none 是加法单位元；`sub`（[L175](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L175) 起）没有对应的 none 臂，具体行为以其兜底臂为准，建议本地实测。

**练习 2**：`(1, 2) < (1, 3)` 是合法比较吗？依据是什么？
**答案**：合法。`compare` 有 `(Array, Array)` 臂并委托 `try_cmp_arrays`（[ops.rs:L498](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L498)），按字典序逐元素比较。

**练习 3**：脚本层 `==` 和 Rust 层 `Value::eq` 语义会不一致吗？
**答案**：不会。`Value` 的 `PartialEq` 就是调用 `ops::equal`（[value.rs:L293-L297](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L293-L297)），而脚本 `==` 经 typst-eval 也进入同一函数。

### 4.3 基础类型方法表

#### 4.3.1 概念说明

`"hello".len()` 里的 `len` 从哪来？关键认知：**方法不挂在值上，而挂在「类型」上**。`Value::scope()`（[value.rs:L172-L179](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L172-L179)）只对 `Func`/`Type`/`Module` 三种值返回作用域；字符串、数组的方法表由它们**类型**的 `#[scope]` 块注册（与 u3-l2 的 global scope 同构）。这样「所有字符串共享一张方法表」，而不是每个值各带一份。

每个基础类型文件的组织高度统一：`#[ty(...)] struct Xxx(...)` 声明类型与 Rust 表示 → 普通方法 → `#[scope] impl` 块里用 `#[func]` 注册脚本方法、`#[func(constructor)]` 注册构造函数 → 底部的 `Repr` 等实现。方法名由过程宏从 snake_case 转成 kebab-case（如 `to_unicode` → `to-unicode`，`str.rs` 的文档注释里直接写着 `@str.to-unicode`）。

#### 4.3.2 核心流程

`#((1, 2).len())` 的完整路径：

```text
1. 求值 target：得到 Value::Array
2. typst-eval/src/call.rs::eval_field_callee：
   target.ty().scope().get("len") 命中 → 标记 is_method_call
3. 返回 FieldCallee::Method(func, target)
4. 真正调用前：args.insert(0, target_span, target)
   —— 接收者被插到第一个位置参数
5. 宏生成的原生函数把第一个参数绑定到 &self，执行 self.0.len()
```

若第 2 步在类型作用域找不到，才会考虑 Content 元素作用域、Symbol/Type/Module 的字段语法等分支（[call.rs:L255-L266](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L255-L266)）；都不行则报「不可调用的字段」错误。字段**访问**（不调用）走另一条路：[value.rs:L157-L169](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L157-L169) 的 `Value::field`——字典按键取值、Symbol 取修饰、Content 取字段名，正是 `#dict.key` 的实现。

#### 4.3.3 源码精读

**① Str：新类型 + 方法注册样板。**

- [str.rs:L74-L78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L74-L78)：`#[ty(scope, cast, title = "String")] pub struct Str(EcoString)`——薄封装，脚本名 `str`，文档标题 String。
- [str.rs:L129-L130](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L129-L130)：`#[scope] impl Str` 开始注册方法表；构造函数 `str(value, base:)` 在 [L152-L155](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L152-L155)（`#[func(constructor)]`）。此后 `len`（[L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L186)）、`first`/`last`/`at`/`slice`、`clusters`/`codepoints`、`to_unicode`/`from_unicode`、`contains`/`starts_with`/`find`/`match_`/`matches`/`replace`/`trim`/`split`/`rev` 等共 23 个 `#[func]` 依序注册（数量按 `grep -c '#\[func'` 统计，含构造函数）。
- repr 的转义规则在 [str.rs:L728-L743](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L728-L743)：外层加引号，内部对 `"`、控制字符等做 `escape_debug`——这正是 4.1 ⑥ 黄金基准里 `"\n"`、`"\""` 的出处。`Str` 自身只是转发给 `str`（[L716-L720](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/str.rs#L716-L720)）。

**② Array：EcoVec 存 Value。**

- [array.rs:L73-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/array.rs#L73-L76)：`pub struct Array(EcoVec<Value>)`——数组就是「值的向量」，克隆廉价。
- [array.rs:L151-L176](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/array.rs#L151-L176)：`#[scope] impl Array`、构造函数 `array(value)`（只做「集合到数组」的转换，注释明确说造数组请用 `(1, 2, 3)` 语法）、`len`。全套 34 个方法覆盖 first/at/push/pop/slice、find/position/filter/map/enumerate/zip/fold/sum/reduce、sorted/dedup 等。
- [array.rs:L1177-L1190](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/array.rs#L1177-L1190)：repr 最多展示 40 个元素，超出补 `.. (N items omitted)`；单元素数组带尾逗号 `(1,)`，靠 `pretty_array_like` 区分。

**③ Dict：Arc\<IndexMap\> 保插入序。**

- [dict.rs:L79-L81](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L79-L81)：`#[ty(scope, cast, name = "dictionary")] pub struct Dict(Arc<IndexMap<Str, Value, FxBuildHasher>>)`——共享底表 + 写时复制；`name = "dictionary"` 显式指定脚本名。
- [dict.rs:L100-L105](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L100-L105)：`at_mut` 取不到键时报 `missing_key` 错误，并附 hint「use `insert` to add or update values」——`HintedStrResult` 的用武之地。
- `#[scope] impl Dict` 在 [dict.rs:L173](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L173) 起，含 construct（[L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L186)）、`at`（[L222](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L222)）、`insert`（[L243](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L243)）等 10 个方法。
- [dict.rs:L369-L383](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L369-L383)：repr——空字典 `(:)`；键是合法标识符时写 `key: value`，否则写带引号的 `"key": value`（`is_ident` 分支）；同样有 40 项截断。插入顺序就是输出顺序（4.1 ⑥ 的黄金基准可证）。

**④ 数值与字节三兄弟。**

- [int.rs:L441-L445](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/int.rs#L441-L445)：`i64` 的 repr 直接用 Debug 格式（就是十进制数字）；int 类型另有 10 个方法（位运算、符号、字节转换等）。
- [float.rs:L198-L202](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/float.rs#L198-L202)：`f64` 的 repr 委托 `repr::format_float`，整数值也显示 `.0`（如 `repr(3.0)` 是 `"3.0"`），与 int 拉开区分；float 有 6 个方法。
- [bytes.rs:L44-L46](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/bytes.rs#L44-L46)：`Bytes(Arc<LazyHash<dyn Bytelike>>)`——字节块的共享与缓存哈希；4 个方法。

**⑤ 方法调用的两个关键接线点（typst-eval）。**

- [call.rs:L246-L254](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L246-L254)：`eval_field_callee` 第一步就是 `target.ty().scope().get(field)`——「到接收者**类型**的作用域里找方法」；命中则打包成 `FieldCallee::Method(func, target)`（[L315](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L315)）。
- [call.rs:L153-L156](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L153-L156)：真正调用前 `args.insert(0, target_span, target)`——**接收者被插为第一个位置参数**，所以 `#[scope]` 方法里的 `&self` 由调用器代填，脚本里不必（也不能）显式传。

方法表规模一览（`grep -c '#\[func'`，含构造函数）：str 23、array 34、dict 10、int 10、float 6、bytes 4。数组方法是了解「脚本函数式工具箱」的最佳样本。

#### 4.3.4 代码实践

**实践目标**：亲手追踪一次方法调用从脚本到 Rust 的落点。

1. 写一个临时文档 `methods-lab.typ`（示例代码）：

   ```typst
   #((1, 2, 3).map(x => x * 2).at(1))
   #("Hello".contains("ell"))
   #((a: 1).insert("b", 2))
   ```

2. 编译并确认输出 `2`、`true` 且页面正常。
3. 在仓库里反查三个方法的 Rust 落点：`grep -n 'pub fn map' crates/typst-library/src/foundations/array.rs`、`grep -n 'pub fn contains' crates/typst-library/src/foundations/str.rs`、`grep -n 'pub fn insert' crates/typst-library/src/foundations/dict.rs`。
4. 对照 4.3.2 的五步流程，说出 `map` 的接收者 `(1, 2, 3)` 是在哪一步变成第一个参数的。

**观察现象**：三个方法分别命中 array.rs、str.rs、dict.rs 的 `#[scope]` 块。

**预期结果**：能指出 `args.insert(0, …)`（[call.rs:L153-L156](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/call.rs#L153-L156)）是接收者入参的位置。编译命令未在本环境执行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`#("a" + 1)` 报「cannot add string and integer」，但 `#("a" + "b")` 正常。这两条消息共用哪段代码？
**答案**：都落在 `add` 的兜底臂与 `mismatch!` 宏（[ops.rs:L16-L21](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L16-L21)、[L170](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L170)）；`(Str, Str)` 臂在 [L130](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ops.rs#L130)。

**练习 2**：为什么 `repr((a: 1))` 是 `(a: 1)`，而键为 `"a b"` 时会变成带引号的形式？
**答案**：`Dict` 的 repr 用 `is_ident(key)` 判断键是否为合法标识符，是则裸写、否则字符串转义（[dict.rs:L369-L383](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/dict.rs#L369-L383)），保证 repr 可被重新解析。

**练习 3**：给某个类型新增一个脚本方法需要改几个文件？
**答案**：核心只改一个——在该类型文件的 `#[scope] impl` 块里加一个 `#[func]` 方法即自动进方法表；方法名转 kebab-case、参数解析代码都由 typst-macros 过程宏生成（详见 u5-l5）。

## 5. 综合实践

把本讲三块知识串成一个测试任务：**在官方测试套件里为 Value 的运算与 repr 写一组断言，并钉死一条类型错误消息**。

1. **实践目标**：构造 int、str、array、dict 各一个 `Value`，执行加法/拼接、验证 repr，触发并固定一次类型错误，全部交给 `cargo testit` 裁决。

2. **操作步骤**：

   1. 新建文件 `tests/suite/foundations/value-ops-practice.typ`（这是你自己创建的未跟踪文件，练习后删除即可，不要提交）：

      ```typst
      --- value-ops-practice eval ---
      // 1) 四种基础类型各构造一个值，做一次加法/拼接
      #test(1 + 2, 3)                        // int + int -> int
      #test("typ" + "st", "typst")           // str + str -> str
      #test((1, 2) + (3,), (1, 2, 3))        // array + array -> 拼接
      #test((a: 1) + (b: 2), (a: 1, b: 2))   // dict + dict -> 合并

      // 2) repr：值的「源码形态」（#test 是测试运行器注入的函数）
      #test(repr(1), "1")
      #test(repr("hi"), "\"hi\"")
      #test(repr((1, "a")), "(1, \"a\")")
      #test(repr((a: 1)), "(a: 1)")

      // 3) 故意的类型错误：先不写下一行注解，跑一次看失败报告
      #(1 + "a")
      ```

   2. 运行 `cargo testit value-ops-practice`（按名字过滤，用法见 u1-l5 与 tests/README.md）。
   3. 预期前 8 条 `#test` 通过、最后一条「缺少错误注解」失败；**从失败输出里抄下错误消息和列区间**，在 `#(1 + "a")` 的上一行补注解（形态参考 [tests/suite/scripting/ops.typ:L452-L454](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/scripting/ops.typ#L452-L454)，数字是表达式在该行的列区间，消息应为 `cannot add integer and string`）：

      ```typst
      // Error: 3-10 cannot add integer and string
      #(1 + "a")
      ```

   4. 再跑一次 `cargo testit value-ops-practice`，全绿即完成。
   5. 删除 `tests/suite/foundations/value-ops-practice.typ`（或移出仓库目录）。

3. **需要观察的现象**：第 3 步失败报告里，错误消息由 4.2 的 `mismatch!` 模板生成（`cannot add {类型} and {类型}`），类型名是 long name（integer/string）；列区间随表达式书写位置变化。

4. **预期结果**：补注解后该测试转绿。这说明：`#test` 断言的是值的相等（走 `ops::equal`）；错误注解断言的是诊断文本与定位——两者正是本讲「派发表 + 错误模板」的直接产物。本任务未在本环境执行，**待本地验证**。

## 6. 本讲小结

- `Value` 是 30 个变体的单一枚举，装下脚本世界所有值，尺寸被测试约束在 32 字节内；第三方类型经 `Value::Dyn` 装箱。
- 每个值经 `ty()` 映射到 `Type`；一个类型三个名字：short（代码）、long（诊断）、title（文档），错误消息用 long name。
- 运算符语义是 `match (lhs, rhs)` 的类型组合表：`none` 是加法单位元、`Int + Float` 提升、`Length + Ratio` 升级为 `Relative`、`Color + Length` 生成描边；兜底臂用 `mismatch!` 打印「类型级」错误消息；短路求值在 typst-eval 的 `apply_binary` 里完成。
- `add`/`join`/`compare` 同时服务脚本层（typst-eval 接线）与 Rust 层（`PartialEq`/`PartialOrd` 委托），两层语义不会漂移。
- 方法挂在**类型**的 `#[scope]` 上而不是值上；`x.m()` 的求值路径是「类型作用域查表 → 接收者插为第一个参数」。
- str/array/dict 分别是 `EcoString`/`EcoVec<Value>`/`Arc<IndexMap>` 的薄封装；repr 是「可重新解析的源码形态」，集合类有 40 项截断、单元素数组带尾逗号、字典保插入序。

## 7. 下一步学习建议

- 下一讲 u3-l4（Content 与元素机制）：`Value::Content` 变体只是个入口，真正的文章表示在 `foundations/content/` 目录，show 规则与选择器都建立在其上。
- u3-l5（Func 与 Args）：本讲反复出现的「`#[func]` 方法被调用」的参数消费协议（expect/find/named）在那里展开。
- 提前翻一翻 [tests/suite/foundations/](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/foundations) 下的 array.typ、dict.typ、str.typ——它们是本讲所有类型行为的「可执行规格」。
- 对 `#[scope]`/`#[func]` 如何生成注册代码感兴趣，可预习 u5-l5（typst-macros 过程宏），配合 `cargo expand` 观察展开结果。
