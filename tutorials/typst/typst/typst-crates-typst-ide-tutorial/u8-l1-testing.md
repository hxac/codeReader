# 测试体系与断言扩展

## 1. 本讲目标

typst-ide 的每个能力模块（tooltip / complete / definition / jump）都在自己的 `#[cfg(test)] mod tests` 里写了大量测试。这些测试不是各写各的、风格各异，而是共享一套**位于 `src/tests.rs` 的公共测试设施**，再加上每个模块各自定义的一套**链式断言扩展（`ResponseExt`）**。

本讲的目标是让你彻底读懂这套测试体系，从而能够：

- 看懂任意一个 typst-ide 测试用例是怎么把「一段 Typst 源码 + 一个光标位置」喂给被测函数的；
- 理解 `WorldLike`、`FilePos`、`cursor` 三个抽象如何让一个 `test()` 辅助函数同时接受「裸字符串」和「预先构造好的多文件 world」；
- 理解 `EXAMPLE_CLOSURE` 这类跨模块共享的「测试夹具」为何存在；
- 学会仿写 `must_include` / `must_be_at` / `must_apply_as` 等链式断言，并为 definition 或 jump 新增测试。

学完本讲，你应该能独立地为 typst-ide 的任意公共函数添加一个新的、风格一致的测试用例。

## 2. 前置知识

在进入本讲之前，你需要先具备以下认知（它们在前序讲义中已建立，这里只做最小回顾）：

- **TestWorld**：typst-ide 测试专用的最小 `World + IdeWorld` 实现，主源码固定路径为 `main.typ`，额外文件通过 `with_source` / `with_asset` 追加，所有测试共享一个经 `singleton!` 懒初始化的 `TestBase`（含标准库、字体簿）。详见 u1-l3。
- **五大 IDE 能力的签名**：例如 `tooltip(world, output, source, cursor, side)`、`autocomplete(world, output, source, cursor, explicit)`、`definition(world, output, source, cursor, side)`。它们的共同点是：第一个参数是 `&dyn IdeWorld`，并都接受一个 `&Source` 和一个字节偏移 `cursor`。
- **光标即字节偏移**：typst 里「光标」就是一个 `usize` 字节偏移，指向源码字符串里的某个位置。
- **`Side::Before` / `Side::After`**：当光标落在两个 token 的交界点时，用它来消歧，决定归属前一个还是后一个 token（详见 u2-l1）。
- **Rust 的 `Borrow` trait 与 `#[track_caller]`**：本讲会用到 `Borrow<TestWorld>` 来统一「借用」与「拥有」的 world；`#[track_caller]` 让断言失败时 panic 指向调用处而非断言宏内部。

本讲只关心「怎么测」，不重复讲被测功能本身的实现。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 在本讲中的作用 |
|------|----------------|
| `src/tests.rs` | **核心**。定义公共测试设施：`TestWorld`、`WorldLike`、`FilePos`、`cursor`、`EXAMPLE_CLOSURE`。被所有能力模块的测试子模块复用。 |
| `src/lib.rs` | 只看第 53–54 行的 `#[cfg(test)] mod tests;`，它把 `tests.rs` 装配为只在测试构建下编译的子模块。 |
| `src/tooltip.rs` | 看其 `mod tests` 里的 `type Response`、`trait ResponseExt`（`must_be_none`/`must_be_text`/`must_be_code`）和 `test()` 辅助函数，作为「最简单的链式断言」范例。 |
| `src/complete.rs` | 看其 `mod tests` 里的 `trait ResponseExt`（`must_include`/`must_exclude`/`must_be_empty`/`at`）与额外的 `trait CompletionExt`（`must_apply_as`/`must_have_detail`），作为「更复杂的链式断言」范例。 |
| `src/definition.rs`、`src/jump.rs` | 综合实践会用到它们的测试风格：definition 用 `must_be_at`/`must_be_file`/`must_be_value`；jump 用 `test_click`/`test_cursor` 这类接收 `Point`/`FilePos` 的辅助函数。 |

记住一条主线：**`tests.rs` 提供公共「输入」抽象，每个能力模块的 `mod tests` 各自提供「输出」断言扩展**。两者通过一个本地的 `test()` 辅助函数缝合。

## 4. 核心概念与源码讲解

本讲拆为五个最小模块：`WorldLike`、`FilePos`、`cursor`、`ResponseExt` 模式、`EXAMPLE_CLOSURE`。前三者解决「怎么把输入喂给被测函数」，后两者解决「怎么把输出表达成断言」以及「怎么共享测试样本」。

### 4.1 WorldLike —— 统一字符串与完整 world 的输入抽象

#### 4.1.1 概念说明

typst-ide 的测试有两种典型写法：

- **简单场景**：只用一段主源码就够了，直接传字符串，例如 `test("#let x = 1 + 2", -1, Side::After)`。
- **复杂场景**：需要多个文件（跨文件 import）、或需要复用同一个 world 多次查询，这时要先 `TestWorld::new(...).with_source(...)` 构造一个完整 world，再传 `&world`，例如 `test(&world, -5, Side::After)`。

如果 `test()` 只接受某一种输入，测试作者就得为两种场景写两套函数。typst-ide 的做法是用一个泛型 trait `WorldLike` 把这两种输入统一起来，让单个 `test()` 辅助函数同时兼容两者。

`WorldLike` 的核心思想是：**「输入」不关心你是字符串还是 world，只要你最终能产出一个「可被当 `&TestWorld` 借用」的东西即可**。它用一个关联类型 `type World: Borrow<TestWorld>` 来表达这个约束。

#### 4.1.2 核心流程

`test()` 辅助函数（以 tooltip 为例）处理输入的流程是：

```text
test(world: impl WorldLike, pos: ..., side: ...)
  │
  │ ① world.acquire()      —— 把「输入」归一化为 Self::World
  │      · 若输入是 &str       → 构造一个全新的 TestWorld（拥有）
  │      · 若输入是 &TestWorld → 原样返回（借用）
  ▼
  world: Self::World        （TestWorld 或 &TestWorld，都满足 Borrow<TestWorld>）
  │
  │ ② world.borrow()       —— 统一拿到 &TestWorld
  ▼
  &TestWorld
  │
  │ ③ pos.resolve(world)   —— 解析光标位置（见 4.2）
  │ ④ typst::compile(world) —— 编译，得到可选 output
  │ ⑤ 调用被测函数 tooltip(...)
  ▼
  Response
```

第 ① 步是 `WorldLike` 的职责，第 ② 步依赖 `Borrow`。

#### 4.1.3 源码精读

`WorldLike` trait 的定义只有两个要素：一个带 `Borrow<TestWorld>` 约束的关联类型，和一个 `acquire` 方法。

[文件路径:L190-L194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L190-L194) —— 定义 `WorldLike`：关联类型 `World` 必须 `Borrow<TestWorld>`，`acquire` 把 `self`（消费）转成该关联类型。注意 `acquire(self)` 接受 `self`（按值），意味着调用一次后输入即被「消费」。

接着是两个实现，恰好覆盖两种场景：

[文件路径:L196-L202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L196-L202) —— 为 `&'a TestWorld` 实现 `WorldLike`：`acquire` 直接返回 `self`（即那个借用），关联类型就是 `&'a TestWorld` 本身。这对应「复杂场景：传预先构造好的 `&world`」。

[文件路径:L204-L210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L204-L210) —— 为 `&str` 实现 `WorldLike`：`acquire` 调用 `TestWorld::new(self)` 构造一个**全新的拥有** `TestWorld`，关联类型是 `TestWorld`。这对应「简单场景：直接传字符串」。

> **为什么用 `Borrow<TestWorld>` 而不是直接返回 `TestWorld`？**
> 如果 `acquire` 一律返回拥有权的 `TestWorld`，那么传 `&world` 时也得克隆一份 world —— 既浪费又啰嗦。`Borrow<TestWorld>` 允许关联类型是 `&TestWorld`（借用，零成本）或 `TestWorld`（拥有，标准库的 blanket `impl<T> Borrow<T> for T` 让 `TestWorld` 也满足 `Borrow<TestWorld>`）。这样两种输入都能在 `world.borrow()` 这一步统一得到 `&TestWorld`，无需无谓克隆。

来看 tooltip 测试里 `test()` 怎么用这两步：

[文件路径:L327-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L327-L334) —— tooltip 的 `test()` 辅助函数：先 `world.acquire()`，再 `world.borrow()` 拿到 `&TestWorld`，编译、解析光标、调用 `tooltip`。注意它对两种输入一视同仁 —— 这正是 `WorldLike` 的价值。

#### 4.1.4 代码实践

**实践目标**：体会 `WorldLike` 的两套实现分别被哪条测试路径命中。

**操作步骤**：

1. 打开 `src/tooltip.rs` 的 `test_tooltip` 与 `test_tooltip_import` 两个测试。
2. 观察 `test_tooltip` 直接传字符串 `"#let x = 1 + 2"`，走的是 `impl WorldLike for &str`。
3. 观察 `test_tooltip_import` 先 `TestWorld::new(...).with_source("other.typ", ...)` 构造 `world`，再传 `&world`，走的是 `impl WorldLike for &TestWorld`。
4. 在 `acquire` 的两个实现里各加一行 `eprintln!`（例如 `eprintln!("acquire: &str");` 与 `eprintln!("acquire: &TestWorld");`）。

**需要观察的现象**：运行 `cargo test -p typst-ide test_tooltip -- --nocapture` 时，stderr 应分别打印对应的标记，证明同一份 `test()` 代码确实路由到了不同的 `acquire` 实现。

**预期结果**：两条测试分别命中两个 `acquire`，且都通过。**待本地验证**（你需自行加 `eprintln!` 后运行）。

> 注意：临时 `eprintln!` 属于调试改动，验证完应移除，不要把调试输出提交到源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `acquire` 的签名是 `fn acquire(self)` 而不是 `fn acquire(&self)`？

**参考答案**：因为 `&str` 这条实现需要消费字符串去构造一个**拥有**的 `TestWorld`（`TestWorld::new(self)` 按值取走字符串）。若用 `&self`，`&str` 实现里只能拿到 `&&str`，无法干净地交出所有权；而 `&TestWorld` 实现里 `self` 本身就是拷贝一个引用，按值消费也无所谓。用 `self` 能让两种实现都自然书写。

**练习 2**：如果删掉关联类型上的 `Borrow<TestWorld>` 约束，`test()` 里的 `world.borrow()` 还能编译吗？

**参考答案**：不能。`borrow()` 来自 `Borrow` trait，没有该约束编译器不知道 `Self::World` 实现了 `Borrow<TestWorld>`，也无法把 `world.borrow()` 的结果推断为 `&TestWorld`。该约束是把「可能是拥有的 `TestWorld`、也可能是借用的 `&TestWorld`」统一成 `&TestWorld` 的关键。

### 4.2 FilePos —— 统一主文件与多文件的位置抽象

#### 4.2.1 概念说明

光标位置有两层信息：**「在哪个文件里」** + **「文件内的字节偏移」**。

- 大多数测试只测主文件 `main.typ`，这时只需要一个偏移量。
- 跨文件测试（如 `test_definition_cross_file` 点击 `#import "other.typ": x` 之后的 `#x`）需要指定「在被导入的某个文件里」。

`FilePos` 是和 `WorldLike` 对称的设计：用一个 trait 把「主文件的一个偏移」和「(文件名, 偏移) 二元组」统一起来，让 `pos.resolve(world)` 一律返回 `(Source, usize)`。

#### 4.2.2 核心流程

```text
pos.resolve(world: &TestWorld) -> (Source, usize)
  │
  ├─ 若 pos 是 isize：
  │     取主源码 world.main，调用 cursor(world.main, pos) 解析偏移
  │     返回 (world.main.clone(), cursor)
  │
  └─ 若 pos 是 (&str, isize)：
        用 self.0（路径）构造 FileId，从 world.source(id) 取该文件
        调用 cursor(&source, self.1) 解析偏移
        返回 (source, cursor)
```

注意 `resolve` 需要 `&TestWorld`，因为「(文件名, 偏移)」那条路径要去 world 里按 `FileId` 取 `Source`；这也解释了为什么 `FilePos::resolve` 必须在拿到 world 之后才能调用。

#### 4.2.3 源码精读

[文件路径:L212-L216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L212-L216) —— `FilePos` trait 定义：唯一方法 `resolve`，输入 `&TestWorld`，输出 `(Source, usize)`。注释说明负数从末尾索引、`-1` 在最末尾。

[文件路径:L218-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L218-L223) —— 为 `isize` 实现 `FilePos`：解析主源码。它把真正的偏移换算委托给私有函数 `cursor`（见 4.3）。

[文件路径:L225-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L225-L234) —— 为 `(&str, isize)` 实现 `FilePos`：先把路径字符串经 `RootedPath::new(VirtualRoot::Project, ...)` 转成 `FileId`（与 `with_source` 用同一套路径构造，保证能查到），再 `world.source(id)` 取文件，最后同样委托给 `cursor`。

> **关键点**：这里的路径 `"other.typ"` 必须与构造 world 时 `with_source("other.typ", ...)` 用的路径一致，因为两者都经 `RootedPath::new(VirtualRoot::Project, VirtualPath::new(path))` 算 `FileId`。路径字符串不同会查不到文件、`resolve` 直接 `unwrap` panic。

来看 tooltip 测试如何同时用到 `WorldLike`（多文件 world）和 `FilePos`（`-5` 即主文件偏移）：

[文件路径:L503-L507](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L503-L507) —— `test_tooltip_import`：`test(&world, -5, Side::After)` 把 `&world`（`WorldLike` 的 `&TestWorld` 实现）和 `-5`（`FilePos` 的 `isize` 实现）组合使用，证明两个抽象彼此正交、可自由搭配。

#### 4.2.4 代码实践

**实践目标**：理解 `(path, offset)` 形式的 `FilePos` 如何命中「被导入文件」内的位置。

**操作步骤**：

1. 阅读 `src/definition.rs` 的 `test_definition_cross_file`：`test("#import "other.typ": x; #x", -2, ...)` 用的是 `isize`，点的是主文件里的 `#x`。
2. 想象一个新测试：要**直接点 `other.typ` 文件内的 `x` 定义处**，该如何写光标？答案是 `test(&world, ("other.typ", 5), Side::After)`，其中 `5` 是 `#let x = 1` 里 `x` 的字节偏移（`#`=0,`l`=1,`e`=2,`t`=3,` `=4,`x`=5）。
3. 不必运行，只需对照 `(&str, isize)` 的实现，确认 `("other.typ", 5)` 会先按路径取到 `other.typ` 的 `Source`，再用 `cursor` 算出偏移 `5`。

**预期结果**：你能口头复述 `("other.typ", 5)` 经 `resolve` 后得到 `(other.typ 的 Source, 5)`。这是本讲综合实践中会真实用到的写法。

#### 4.2.5 小练习与答案

**练习 1**：`FilePos::resolve` 为什么要拿 `&TestWorld` 做参数，而 `WorldLike::acquire` 不需要？

**参考答案**：`acquire` 只负责「把输入归一化为一个 world 对象」，与 world 内部内容无关，不需要外部 world。而 `resolve` 的 `(path, offset)` 路径要按 `FileId` 去 world 里**取文件**，必须依赖一个已构造好的 `&TestWorld`；因此调用顺序固定为「先 `acquire` 拿到 world，再用 world 去 `resolve` 位置」。

**练习 2**：若 `with_source("other.typ", ...)` 与 `("other.typ", 5)` 里的路径大小写或拼写不一致会怎样？

**参考答案**：两者各自经 `RootedPath::new` 算出的 `FileId` 不同，`world.source(id)` 找不到该文件，`resolve` 里的 `.unwrap()` 会 panic，测试直接失败。因此两处路径必须逐字符一致。

### 4.3 cursor —— 负数从末尾索引的光标解析

#### 4.3.1 概念说明

`FilePos` 只声明「负数从末尾索引」，真正的换算在私有函数 `cursor` 里。为什么要支持负数？因为测试作者写 `"#let x = 1 + 2"` 时，关心的是**末尾**那个 `2`，但手算字符串长度（14）再写 `13` 很容易错；写 `-1`、`-2` 更直观、更抗编辑（在前面插字符也不会让负数光标漂移）。这是测试可维护性的小优化。

规则：正数就是普通字节偏移；负数从末尾倒数，`-1` 表示「最末尾」（字符串长度那个位置，即末尾之后），`-2` 表示倒数第一个字符的位置，依此类推。

#### 4.3.2 核心流程

```text
cursor(source, cursor: isize) -> usize
  if cursor < 0:
      return source.text().len().checked_add_signed(cursor + 1)
                     //          ↑ 为什么 +1？见下
  else:
      return cursor as usize
```

关键是 `cursor + 1` 这一步。设字符串长度为 `len`：

- `cursor = -1` → `cursor + 1 = 0` → `len + 0 = len`（末尾之后的位置）；
- `cursor = -2` → `cursor + 1 = -1` → `len - 1`（最后一个字符的起始偏移）。

即「`-1` 在最末尾」对应偏移 `len`，符合「光标可停在字符串末尾之后」的直觉。用数学表达：

\[ \text{pos}(-k) = \text{len} - k + 1, \quad k \geq 1 \]

其中 \(-k\) 即 `cursor`，`pos` 为返回的无符号偏移。代 \(k=1\) 得 \(\text{len}\)，代 \(k=2\) 得 \(\text{len}-1\)，与上文一致。

#### 4.3.3 源码精读

[文件路径:L236-L244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L236-L244) —— `cursor` 函数：负数分支用 `checked_add_signed`（带溢出检查的有符号加法，溢出返回 `None` 再 `unwrap`）把负偏移安全地映射到末尾附近的正偏移；非负分支直接 `as usize`。`#[track_caller]` 让溢出 panic 指向调用处。

来看一段真实的负数光标测试，体会换算结果：

[文件路径:L336-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L336-L341) —— `test_tooltip`：字符串 `"#let x = 1 + 2"` 长度为 14。
- `test("#let x = 1 + 2", -1, Side::After)` → 偏移 14（末尾之后），那里是 trivia，`must_be_none()`；
- `test("#let x = 1 + 2", 5, Side::After)` → 偏移 5 正是 `x`（`#let x` 的 `x`），悬停得到其值 `3`。

> **设计权衡**：`cursor` 是私有（非 `pub`）函数，只在 `tests.rs` 内被两个 `FilePos` 实现调用。它不对外暴露，因为换算规则是测试约定，不属于公共 API。`FilePos` 才是对外的抽象。

#### 4.3.4 代码实践

**实践目标**：亲手验证负数光标换算，体会其抗编辑的便利。

**操作步骤**：

1. 取字符串 `s = "#let x = 1 + 2"`，确认 `len = 14`。
2. 手算：`-1` → 14、`-2` → 13（最后的 `2`）、`-3` → 12（`2` 前的空格）、`-5` → 10（`+`）。
3. 在 `src/tooltip.rs` 的 `test_tooltip` 里临时把第一条改成 `test("#let x = 1 + 2", -2, Side::After)`，推测它会命中 `2`（字面量）→ 因为字面量无 tooltip（`expr.is_literal()` 短路），预期 `must_be_none()`。

**需要观察的现象**：用 `-2` 命中字面量 `2` 时应返回 `None`；改回 `-5` 命中 `+`（运算符）也应是 `None`。

**预期结果**：你的手算偏移与「实际悬停命中的字符」一致。**待本地验证**（临时改动验证后请还原）。

#### 4.3.5 小练习与答案

**练习 1**：为什么负数分支是 `cursor + 1` 而不是直接 `cursor`？

**参考答案**：若不加 1，`-1` 会映射到 `len - 1`（最后一个字符的起点），无法表达「光标停在字符串末尾之后」这一常见状态（许多测试需要把光标放在整段代码的最后）。加 1 后 `-1` → `len`，保留了「最末尾」语义。

**练习 2**：对一个含多字节 UTF-8 字符的字符串（如 `"#表"`，`#` 后跟中文），负数光标 `-1` 仍指向「末尾之后」吗？

**参考答案**：是。`cursor` 用的是 `source.text().len()`，即**字节**长度而非字符长度，typst 的偏移一律按字节计。多字节字符不影响「末尾字节位置 = 字节长度」这一结论，负数换算与字符数无关。

### 4.4 ResponseExt 模式 —— 每个模块各自的链式断言扩展

#### 4.4.1 概念说明

`WorldLike` / `FilePos` 解决了「输入」，`ResponseExt` 解决「输出怎么断言」。不同能力函数返回类型不同：

- `tooltip` → `Option<Tooltip>`
- `autocomplete` → `Option<(usize, Vec<Completion>)>`
- `definition` → `Option<Definition>`

typst-ide 没有把这些断言塞进公共 `tests.rs`，而是让**每个模块的 `mod tests` 各自定义一个本地 `type Response` 和一个本地 `trait ResponseExt`**（名字都叫 `ResponseExt`，但互不相干）。这是 Rust 测试里一种常见的「断言扩展（assertion extension）」模式：给返回类型挂上 `must_xxx` 方法，让断言读起来像自然语言，并支持链式调用。

这种模式有三个共性约定：
1. `type Response = <被测函数返回类型>`（有时附带额外上下文，见 definition）。
2. 每个 `must_xxx` 方法都标 `#[track_caller]`，并返回 `&Self` 以支持链式。
3. 断言内部用 `assert_eq!` / `assert!`，失败信息尽量带上实际值便于排错。

#### 4.4.2 核心流程

以 tooltip 为例的「断言扩展」工作流：

```text
test(world, pos, side)  ──▶  Response = Option<Tooltip>
                                 │
                                 ▼  调用 ResponseExt 方法
        .must_be_none()           —— 断言 == None
        .must_be_text("...")      —— 断言 == Some(Tooltip::Text("..."))
        .must_be_code("...")      —— 断言 == Some(Tooltip::Code("..."))
```

更复杂的 complete 还分两层扩展：`ResponseExt`（针对整组补全：`must_include`/`must_exclude`/`must_be_empty`/`at`）与 `CompletionExt`（针对单个 `Completion`：`must_apply_as`/`must_have_detail`），通过 `at("label")` 把前者桥接到后者。

#### 4.4.3 源码精读

**最简单：tooltip 的 ResponseExt。** 它只有三个断言方法：

[文件路径:L298-L305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L298-L305) —— tooltip 测试里定义本地 `type Response = Option<Tooltip>` 和 `trait ResponseExt`（`must_be_none` / `must_be_text` / `must_be_code`）。

[文件路径:L307-L325](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L307-L325) —— `ResponseExt` 的实现：每个方法用 `assert_eq!` 比对期望值，返回 `self` 以支持链式。注意 `must_be_text` 构造 `Tooltip::Text(text.into())` 再比较，这样断言里只写字符串、不必手写枚举。

**更复杂：complete 的两层扩展。**

[文件路径:L1517-L1524](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1517-L1524) —— complete 的 `ResponseExt`，除断言外还有两个**查询**方法 `completions()` 与 `labels()`（返回 `BTreeSet<&str>`，便于集合包含判断），以及 `at(label)` 桥接到单个补全项。

[文件路径:L1539-L1570](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1539-L1570) —— `must_be_empty` / `must_include` / `must_exclude`：失败信息都带上实际 `labels`（`{labels:?}`），便于一眼看出多/少了什么。

[文件路径:L1572-L1578](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1572-L1578) —— `at(label)`：在补全列表里按 `label` 找到那个 `&Completion`，找不到就 panic。它是连接「整组断言」与「单项断言」的枢纽 —— 见下面的链式写法。

[文件路径:L1581-L1598](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1581-L1598) —— 第二层 `CompletionExt` 直接挂在 `Completion` 上：`must_apply_as` 比对 `apply` 字段（`as_deref`，故能传 `None` 表示「无 apply」），`must_have_detail` 比对 `detail` 字段。

来读一段真实的「双层链式」用法：

[文件路径:L1808-L1825](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1815-L1825) —— `test_autocomplete_bracket_mode`：`test("#", 1).at("list").must_apply_as("list(${})")` 先用 `ResponseExt::at("list")` 拿到名为 `list` 的补全项，再用 `CompletionExt::must_apply_as(...)` 断言它的 `apply` 文本。一行同时表达「列表里要有 list」与「它的 apply 是 `list(${})`」两层期望。注意 `q!` 宏会把字面量包上引号（[文件路径:L1509-L1513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1509-L1513)）。

**带上下文的 Response：definition。** definition 的断言需要把 `Span` 翻译成 `(路径, 字节范围)`，离不开 world，因此它的 `Response` 把 world 一起带上：

[文件路径:L101-L107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L101-L107) —— definition 的 `type Response = (TestWorld, Option<Definition>)`：携带 world 克隆，供 `must_be_at` 用 `self.0.range(span)` 解析范围；三个断言对应 `Definition` 的三种变体 `Span` / `File` / `Std`。

[文件路径:L109-L144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L109-L144) —— `must_be_at` 比对 `(路径, 字节范围)`、`must_be_file` 比对文件路径、`must_be_value` 比对标准库值；匹配错误变体即 panic，给出「expected span/file/std definition」提示。

[文件路径:L146-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L146-L154) —— definition 的 `test()` 返回 `(world.clone(), def)`，刻意克隆一份 world 交给断言用。

[文件路径:L156-L160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L156-L160) —— `test_definition_let`：`test("#let x; #x", -2, Side::After).must_be_at("main.typ", 5..6)`，`-2` 命中最后的 `x`，跳转定义指向 `#let x` 的 `x`（偏移 5..6）。

> **为什么不让 `ResponseExt` 公共化、跨模块复用？** 因为各模块返回类型差异大（`Tooltip` vs `(usize, Vec<Completion>)` vs `Definition`），强行抽象公共 trait 反而失之牵强。各模块各自定义、名字复用 `ResponseExt`，是务实且内聚的选择 —— 断言与被测类型就近放置，读测试时一眼可见全部可用断言。

#### 4.4.4 代码实践

**实践目标**：学会读懂「链式断言」并预测其检查内容。

**操作步骤**：阅读 complete 的下用例，逐环解释它断言了什么：

```rust
test("#", 1).must_include(["int", "if conditional"]).must_exclude(["colon"]);
```

**预期结果**：
- `test("#", 1)`：在 `#` 之后（偏移 1）请求补全，返回 `Option<(usize, Vec<Completion>)>`。
- `.must_include(["int", "if conditional"])`：补全项的 `label` 集合必须同时包含 `"int"` 和 `"if conditional"`。
- `.must_exclude(["colon"])`：且不得包含 `"colon"`（code 模式才有 `colon`，markup 模式不该有）。

你能复述：「链上每个方法都返回 `&Self`，所以能 `.a().b().c()` 连写；某环失败立即 panic，后续不再执行。」

**延伸**：对照 `test_autocomplete_bracket_mode` 里 `.at("list").must_apply_as("list(${})")`，说明 `at` 之后链上的方法属于 `CompletionExt` 而非 `ResponseExt`（因为 `at` 返回 `&Completion`）。**待本地验证**（你可加一条故意失败的断言，观察 panic 信息是否如注释所述带上实际值）。

#### 4.4.5 小练习与答案

**练习 1**：为什么每个 `must_xxx` 都要标 `#[track_caller]`？

**参考答案**：`#[track_caller]` 让 `assert_eq!` / `assert!` panic 时把调用栈位置指向**调用 `must_xxx` 的那行测试代码**，而不是 `must_xxx` 实现内部。测试成百上千条，定位到具体哪条用例失败至关重要；不加的话所有失败都指向同一个断言实现行，失去排错价值。

**练习 2**：complete 的 `ResponseExt` 为什么把 `completions()`、`labels()` 做成「查询方法」而非「断言方法」？

**参考答案**：它们是给 `must_include`/`must_exclude`/`at` 共享的**内部工具**（`must_include` 内部就调用了 `labels()`），本身不做断言、不 panic。把它们暴露在同一个 trait 上，是为了让断言方法能复用同一份「取出 label 集合」的逻辑，避免重复，也让 `at()` 这类查询在测试里可用。

**练习 3**：definition 的 `must_be_at` 为何需要 `Response` 里带一份 `TestWorld`？

**参考答案**：因为 `Definition::Span(span)` 只有 `Span`，而断言要比对的是「人类可读的 (路径, 字节范围)」。把 `Span` 翻译成范围必须查 world（`world.range(span)`），所以 `test()` 克隆一份 world 塞进 `Response`，供断言方法在事后使用。tooltip / complete 的返回值本身已是可直接比较的数据，无需此携带。

### 4.5 EXAMPLE_CLOSURE —— 跨模块共享的测试夹具

#### 4.5.1 概念说明

有些测试样本（一段带文档注释的函数定义）同时被 tooltip 测试（验文档提取）和 complete 测试（验补全 detail）用到。若各自抄一份，改一处要改多处、还可能抄错。typst-ide 把这个公共样本提取为一个 `pub const EXAMPLE_CLOSURE: &str`，放在 `tests.rs` 里供所有模块引用。

这是「测试夹具（test fixture）」的最朴素形态：一个跨测试复用的、确定性的输入数据。

#### 4.5.2 核心流程

```text
EXAMPLE_CLOSURE（一段带 /// 与 // 注释的函数定义）
        │
        │  被 with_source 注入为某个文件的内容
        ▼
TestWorld（如 lib.typ 的内容就是 EXAMPLE_CLOSURE）
        │
        ├─ tooltip 测试：悬停函数名/参数名 → 验文档摘要
        └─ complete 测试：触发补全后 .at("foo").must_have_detail("A useful function.")
```

#### 4.5.3 源码精读

[文件路径:L246-L260](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L246-L260) —— `EXAMPLE_CLOSURE` 定义：一段函数 `foo`，带普通注释 `//`、空行注释、以及参数级 `///` 文档注释（`tree`、`forest`）。注释注释里还混了强调标记（`*useful*`、`*trees*`）与数学（`$1+2$`），专门用来测 `docs.rs` 的 `summary` 文本处理（去强调、取首句）。它是 `pub`，故其它模块经 `crate::tests::EXAMPLE_CLOSURE` 引用。

看两个消费方如何复用同一段样本：

[文件路径:L530-L537](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L530-L537) —— tooltip 的 `test_tooltip_user_function`：把 `EXAMPLE_CLOSURE` 注入为 `lib.typ`，悬停函数名得到 `"A useful function."`（来自首行 `// A *useful* function.`，去强调后取首句），悬停参数 `tree` 得到 `"Tree with three slashes."`（来自 `/// Tree with three slashes.`）。

[文件路径:L2058-L2064](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L2058-L2064) —— complete 的 `test_autocomplete_user_function`：同样注入 `EXAMPLE_CLOSURE`，补全 `.foo` 后 `.at("foo").must_have_detail("A useful function.")`，复用同一段样本验证补全项的 `detail`。

> **要点**：`EXAMPLE_CLOSURE` 是 `tests.rs` 里少数 `pub` 的常量之一（与 `TestWorld`、`WorldLike`、`FilePos`、`main_id` 一样 `pub`），正是因为它要被兄弟模块的测试子模块跨模块引用。而 `cursor`、`TestBase`、`TestFiles` 是私有，只在 `tests.rs` 内部用。

#### 4.5.4 代码实践

**实践目标**：理解 `EXAMPLE_CLOSURE` 为何能同时验「文档摘要」与「补全 detail」。

**操作步骤**：

1. 读 `EXAMPLE_CLOSURE`，定位首行注释 `// A *useful* function.`。
2. 回顾 u3-l1（docs.rs 的 `summary`）：它会去掉 `*` 强调、取首句。故 `summary` 结果是 `"A useful function."`。
3. 对照 tooltip 与 complete 两条用例，确认它们期望的字符串都是 `"A useful function."` —— 同一段样本、同一套文本处理、同一份期望，只是入口函数不同（`tooltip` vs `autocomplete`）。

**预期结果**：你能解释「为什么 tooltip 的文档摘要与 complete 的 detail 文本完全一致」——因为两者底层都走 `docs.rs` 的同一套文档提取逻辑（`find_value_docs` / `collect_doc_comment` / `summary`），而 `EXAMPLE_CLOSURE` 是它们共享的输入。

#### 4.5.5 小练习与答案

**练习 1**：若把 `EXAMPLE_CLOSURE` 首行改成 `// A useful function. With more.`，两条用例（tooltip 与 complete）的期望字符串需要怎么改？

**参考答案**：`summary` 只取「第一段第一句」，故即使加了 `. With more.`，摘要仍是首句 `"A useful function."`，两条用例期望**都不用改**。这正是 `summary` 取首句语义的体现 —— 想验证它取整段才需要改期望。

**练习 2**：为什么 `EXAMPLE_CLOSURE` 要刻意混入 `*useful*`、`$1+2$` 这类标记？

**参考答案**：为了在同一个样本里覆盖 `docs.rs` 文本处理的多种边界：`*useful*` 测「去强调」、`$1+2$` 测「数学内容在注释里的处理」。把多个边界塞进一个共享样本，既减少重复，又让两个消费方（tooltip / complete）都能顺带验证这些边界。

## 5. 综合实践

把本讲五个模块串起来：用 typst-ide 现有的测试风格，**为 `definition` 新增一个测试用例**，要求同时用到 `WorldLike`（多文件 world）、`FilePos`（`isize` 或 `(path, offset)` 两种之一）、以及 `ResponseExt` 的链式断言（`must_be_at` / `must_be_file` / `must_be_value`）。

**任务**：在 `src/definition.rs` 的 `mod tests` 中，为「跨文件字段访问的定义跳转」补一个用例 —— 场景是主文件 `#import "other.typ": rec; #rec.name`，点击 `#rec.name` 里的 `name`，期望跳到 `other.typ` 里 `name` 字段的定义处。

参考骨架（**示例代码，非项目原有，需你据实际类型填写并验证偏移**）：

```rust
#[test]
fn test_definition_cross_file_field() {
    let world = TestWorld::new("#import \"other.typ\": rec; #rec.name")
        .with_source("other.typ", "#let rec = (name: 1, age: 2)");
    // 点击主文件最后的 name（用负数光标，需手算字节数）
    test(&world, -2, Side::After).must_be_at("main.typ", /* 期望范围，待确认 */ .. /* */);
}
```

**操作步骤**：

1. 按 4.3 的方法手算 `"-2"` 命中的字符，确认它确实落在 `name` 上；若不在，调整负数或改用正偏移。
2. 推理 `definition` 会把 `rec.name` 经 `analyze_expr` 解析出 `rec` 的值（一个 dict），再从字段上挖出 span。预期 `Definition::Span` 落在 `other.typ` 中 `name: 1` 的 `name` 处。
3. 用 `must_be_at("other.typ", 起始..结束)` 表达期望；若实际行为是落到 `rec` 的定义或返回别的变体，据实改用 `must_be_at` / `must_be_file` 之一，并写注释说明。
4. 运行 `cargo test -p typst-ide test_definition_cross_file_field`。

**预期结果**：测试通过；若行为与你的推理不符，以实际运行结果为准并修正期望（**待本地验证**，不要凭空写一个无法通过的断言）。

> 进阶可选：改用 `(path, offset)` 形式的 `FilePos`，直接点 `other.typ` 内 `name` 的定义处，断言 `must_be_at("other.typ", ..)` —— 这会同时练到 4.2 的第二种 `FilePos` 实现。若你更想测 `jump`，可仿照 `jump.rs` 的 `test_cursor`，构造一个源码光标，断言 `jump_from_cursor` 返回的页内坐标（用 `pos(page, x, y)` 辅助函数）。

## 6. 本讲小结

- typst-ide 的测试体系由「公共输入抽象」+「各模块输出断言扩展」两层组成，前者在 `src/tests.rs`，后者分散在各 `mod tests`。
- `WorldLike` 用 `acquire(self)` + 关联类型 `World: Borrow<TestWorld>`，让 `test()` 同时接受裸字符串（构造新 world）和 `&TestWorld`（零成本借用）。
- `FilePos` 用 `resolve(&TestWorld) -> (Source, usize)`，把主文件偏移（`isize`）与多文件位置（`(&str, isize)`）统一，多文件路径必须与 `with_source` 完全一致。
- `cursor` 把负数光标换算成末尾附近的正偏移，公式 \(\text{pos}(-k)=\text{len}-k+1\)，`-1` 对应 `len`（最末尾）；换算按字节计。
- `ResponseExt` 模式：每个模块各自定义 `type Response` 与同名 `trait ResponseExt`，`must_xxx` 方法标 `#[track_caller]`、返回 `&Self` 支持链式；complete 进一步用 `at(label)` 桥接到 `CompletionExt` 做单项断言；definition 因需把 `Span` 翻译成范围，把 `TestWorld` 带进 `Response`。
- `EXAMPLE_CLOSURE` 是跨模块共享的测试夹具，同时被 tooltip 与 complete 复用以验证同一套 `docs.rs` 文本处理逻辑。

## 7. 下一步学习建议

- **动手扩展测试**：按第 5 节综合实践，为 definition 或 jump 真正提交一个测试用例；这是检验你是否读懂本讲的最佳方式。
- **阅读 u8-l2（集成实践与架构取舍）**：本讲只讲「库内部怎么自测」，u8-l2 讲「外部语言服务器如何集成 typst-ide」，届时会用到 `IdeWorld` 的可选方法与 `AsOutput`，与本讲的 `output`（编译产物）概念衔接。
- **回看 docs.rs（u3-l1）**：若你对 `EXAMPLE_CLOSURE` 的 `summary` 行为好奇，可对照 `docs.rs` 的 `summary` 状态机源码，把「样本输入 → 文本输出」的链路彻底打通。
- **对照 jump.rs 的测试**：jump 的 `test_click` / `test_cursor` 与本讲的 `test()` 同构但输入是 `Point`（点击坐标）而非字节偏移，阅读它能加深对「双向跳转」测试编排的理解（承接 u7-l3）。
