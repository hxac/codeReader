# 变量插值与格式化说明符

## 1. 本讲目标

本讲承接 [u3-l2](u3-l2-tr-macro-codegen.md)（`_tr!` 的解析与代码生成），专门讲清楚一件事：**`t!("key", name = "Jason", count = 7 : {:05})` 里，那些 `name = ...`、`count = ... : {:05}` 的参数，是如何在编译期被处理、又在运行期被填进译文里的 `%{name}`、`%{count}` 占位符的。**

学完后你应当能：

- 说清 `replace_patterns` 这个**运行时**函数如何用一个字节级「状态机」扫描 `%{name}` 占位符并完成替换；
- 说清 `count = 7 : {:05}` 中的 `: {:05}` 是怎样在**编译期**被 `Argument::parse` 解析成 `specifiers = ":05"`；
- 说清 `into_token_stream` 如何把每个参数包成 `format!("{:05}", value)`，再把所有参数的「名字」与「格式化后的值」按**相同顺序**对齐成 `keys` / `values` 两个数组交给 `replace_patterns`。

本讲的三个最小模块正好对应这条链路的三个环节：运行时扫描器、编译期说明符解析、两者之间的代码生成桥梁。

## 2. 前置知识

在进入本讲前，请确认你已经了解（这些都在前置讲义里建立过）：

- **`t!` 是转发壳**：`t!` 本身不翻译，它转发到 `crate::_rust_i18n_t!`，再转发到全局的 `_tr!` 过程宏（见 [u3-l1](u3-l1-t-macro-call-chain.md)）。
- **`_tr!` 在编译期生成代码**：它把 `t!(...)` 展开成一段调用 `crate::_rust_i18n_try_translate(locale, &key)` 查表、再用 `replace_patterns` 替换占位符的 Rust 源码（见 [u3-l2](u3-l2-tr-macro-codegen.md)）。
- **`%{name}` 是译文里的占位符**：翻译文件（YAML/JSON/TOML）里写 `Hello, %{name}!`，运行时由 `replace_patterns` 把 `%{name}` 替换成实际值。
- **`format!` 的格式说明符**：Rust 标准库里 `format!("{:08}", 123)` 会得到 `"00000123"`（零填充到 8 位）。本讲会用同样的机制，只是说明符从 `t!` 参数里来。
- **`Cow`**：`Cow<'_, str>` 是「可能借用、可能拥有」的字符串类型，命中翻译时零拷贝返回 `Cow::Borrowed`。

一句话回顾位置关系：译文里的占位符叫 `%{name}`，`t!` 参数里的名字叫 `name`，二者**必须同名**才能匹配上。本讲就是讲它们如何「对上号」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 定义**运行时**的 `replace_patterns` 函数：扫描 `%{name}` 并替换；也是 `t!` 宏的转发壳所在。 |
| [crates/macro/src/tr.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs) | 定义 `_tr!` 过程宏的全部逻辑：`Argument`（含 `specifiers` 解析）、`into_token_stream`（把参数编译成 `format!` 调用与 `keys`/`values` 数组）。 |

两个文件分工很清晰：`src/lib.rs` 负责**运行时**的字符串替换，`crates/macro/src/tr.rs` 负责**编译期**把参数准备好、再生成调用前者的代码。本讲就是把这「一运行时、一编译期」两端拼起来。

## 4. 核心概念与源码讲解

### 4.1 占位符替换的运行时扫描器：replace_patterns

#### 4.1.1 概念说明

当译文（来自翻译文件，或未命中时的原始消息字符串）里含有 `%{name}` 这样的占位符时，需要有一个函数把它们替换成实际值。这个函数就是 `replace_patterns`，它是 `rust-i18n` 根 crate 里一个**公开的纯函数**：

[src/lib.rs:L45-L45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L45) —— 函数签名 `pub fn replace_patterns(input: &str, patterns: &[&str], values: &[String]) -> String`。

它的职责用一句话讲就是：**在 `input` 里找到所有 `%{名字}`，把每个名字拿去 `patterns` 数组里查，查到就用 `values` 里同下标的值替换，查不到就原样保留 `%{名字}`。** 注意 `patterns`（名字数组）和 `values`（值数组）是**按下标一一对应**的——这是整条链路对齐的关键，4.3 节会看到 `_tr!` 正是按相同顺序构造这两个数组。

它自带一个 doctest，把行为讲得很直白：

[src/lib.rs:L37-L44](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L37-L44) —— `"Hello, %{name}!"` 配 `&["name"]` 和 `&["world".to_string()]`，输出 `"Hello, world!"`。

#### 4.1.2 核心流程

`replace_patterns` 分两步走：**先扫描、再拼装**。

**第一步：扫描，记录每个 `%{` 与 `}` 的字节下标。** 用一个 `stage`（阶段）变量做极简状态机，逐字节读 `input`：

| 当前 stage | 读到的字节 | 动作 | 新 stage |
| --- | --- | --- | --- |
| 0（地面态） | `%` | 标记「刚见过 `%`」 | 1 |
| 1（已见 `%`，等 `{`） | `{` | 记录 `{` 的下标 | 2 |
| 2（在 `%{...` 内，等 `}`） | `}` | 记录 `}` 的下标 | 0 |
| 其它 | `%` | （任何 stage 下）重置为「已见 `%`」 | 1 |
| 其它 | 其它 | 忽略 | 不变 |

设计意图是 `%` 紧跟 `{`、再跟名字、再跟 `}`，即 `%{name}`。扫描结束后，记录下来的下标成对出现：`[{ 的下标, } 的下标, { 的下标, } 的下标, ...]`。

**第二步：拼装，按成对下标切分并替换。** 对每一对 `({下标, }下标)`：

1. 取两个下标之间的字节作为「占位符名字」（即 `%{name}` 里的 `name`）；
2. 把「上一段已处理位置」到「`%` 之前」的原文照抄进输出；
3. 拿这个名字去 `patterns`/`values` 里查：查到就追加对应的值，查不到就把整段 `%{名字}` 原样追加；
4. 推进「已处理位置」到 `}` 之后。

最后把尾部剩余原文追加上去，得到完整输出。

> **为什么用 `chunks_exact(2)` 配对？** `pattern_pos` 是一串成对的下标，用 `chunks_exact(2)` 正好把它们两两分组。如果输入里有一个**没闭合**的 `%{name`（`{` 多于 `}`），下标数量就是奇数，`chunks_exact(2)` 会**自动丢弃**最后那个落单的 `{` 下标——这是对畸形输入的一种自我保护，不会越界。

整体复杂度：扫描是 \(O(n)\)（\(n\) 为输入字节数），拼装阶段每个占位符都要在 `patterns` 里线性查找一次，故为 \(O(n + m \cdot k)\)，其中 \(m\) 为参数个数、\(k\) 为占位符个数。对翻译场景（参数与占位符都很少）完全够用。

#### 4.1.3 源码精读

**扫描阶段**（状态机）：

[src/lib.rs:L46-L64](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L46-L64) —— 用 `match (stage, b)` 同时匹配「当前阶段」与「当前字节」，把成对的 `{`/`}` 下标压入 `pattern_pos`。

关键点：

```rust
let mut pattern_pos = smallvec::SmallVec::<[usize; 64]>::new();
let mut stage = 0;
for (i, &b) in input_bytes.iter().enumerate() {
    match (stage, b) {
        (1, b'{') => { stage = 2; pattern_pos.push(i); }   // 见 %{，进占位符
        (2, b'}') => { stage = 0; pattern_pos.push(i); }   // 闭合 }，回地面态
        (_, b'%') => { stage = 1; }                         // 任何位置见 %，进入「等 {」
        _ => {}
    }
}
```

> 这里用 `SmallVec<[usize; 64]>` 而不是 `Vec`：占位符通常很少，64 个下标（约 512 字节）以内可以直接在**栈**上分配，免去堆分配。这是 rust-i18n 的一处性能优化，更系统的讲解见 [u8-l2](u8-l2-benchmark-and-optimization.md)。

**拼装阶段**：

[src/lib.rs:L65-L90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L65-L90) —— 遍历成对下标，按名字查表替换，最后用 `String::from_utf8_unchecked` 收尾。

几个要点：

```rust
for pos in pattern_pos.chunks_exact(2) {
    let start = pos[0];            // { 的下标
    let end = pos[1];              // } 的下标
    let key = &input_bytes[start + 1..end];          // { 与 } 之间的名字
    if prev_end < start {
        let prev_chunk = &input_bytes[prev_end..start - 1];   // % 之前的原文（start-1 正是 %）
        output.extend_from_slice(prev_chunk);
    }
    if let Some((_, v)) = pattern_values.clone()             // 每轮重新克隆迭代器从头查
        .find(|(&pattern, _)| pattern.as_bytes() == key)
    {
        output.extend_from_slice(v.as_bytes());               // 命中：追加值
    } else {
        output.extend_from_slice(&input_bytes[start - 1..end + 1]); // 未命中：原样保留 %{名字}
    }
    prev_end = end + 1;
}
```

- `start - 1` 在标准 `%{name}` 里恰好指向 `%`，所以 `prev_chunk` 切片**不含** `%`（把 `%` 留给占位符语法消化）；未命中时的 `input_bytes[start - 1..end + 1]` 正好是整段 `%{name}`，原样回写。
- `pattern_values` 是 `patterns.iter().zip(values.iter())` 的产物，`find` 会消耗迭代器，所以每次循环都要 `.clone()` 重新开始查找——这就实现了「按名字匹配、按下标取值」。
- 末尾 `String::from_utf8_unchecked` 是安全的：`%`、`{`、`}` 都是 ASCII 单字节，所有切片都落在 UTF-8 边界上，而追加进去的 `values` 本身就是合法 `String`。

#### 4.1.4 代码实践

这是一个**可直接运行的纯函数实践**，不需要 `i18n!` 初始化，也不需要翻译文件——因为 `replace_patterns` 就是个普通公开函数。

1. **实践目标**：亲手调用 `replace_patterns`，验证「命中替换」与「未命中原样保留」两种行为。
2. **操作步骤**：在一个已经把 `rust-i18n` 加为依赖的 crate 里（任意带 `i18n!("locales")` 的项目都行，例如 `examples/app`），新增一个测试：

   ```rust
   #[test]
   fn practice_replace_patterns() {
       use rust_i18n::replace_patterns;
       // 命中：name 有值
       let out = replace_patterns(
           "Hello, %{name}!",
           &["name"],
           &["world".to_string()],
       );
       assert_eq!(out, "Hello, world!");

       // 未命中：没有给 msg 提供值，原样保留 %{msg}
       let out2 = replace_patterns(
           "Hello, %{name}. Your message is: %{msg}",
           &["name"],
           &["Jason".to_string()],
       );
       assert_eq!(out2, "Hello, Jason. Your message is: %{msg}");
   }
   ```

3. **运行方式**：`cargo test practice_replace_patterns`。
4. **预期结果**：测试通过。第一个断言印证 doctest；第二个断言印证「未提供的占位符会被原样保留」——这一点和集成测试 `tests/integration_tests.rs:147-149` 里 `t!("a.very.nested.message", name = "Jason")` 得到 `"Hello, Jason. Your message is: %{msg}"`（`%{msg}` 保留）是完全一致的逻辑。

> 上述断言结果是根据源码逻辑直接推导出的确定值，可放心验证。

#### 4.1.5 小练习与答案

**练习 1**：`replace_patterns` 的扫描状态机有几种 stage？分别表示什么？

**参考答案**：3 种。stage 0 = 地面态（不在占位符内）；stage 1 = 刚见到 `%`、等待 `{`；stage 2 = 已进入 `%{...`、等待闭合的 `}`。

**练习 2**：如果输入字符串里有一个未闭合的 `%{broken`（后面没有 `}`），`replace_patterns` 会怎样？

**参考答案**：扫描阶段会把 `{` 的下标记入 `pattern_pos`，但因为后续没有 `}`，该下标落单；拼装阶段 `chunks_exact(2)` 会丢弃不成对的部分，所以这个未闭合占位符被**整体忽略**，原文里的 `%{broken` 会被当作普通文本原样保留在输出里（由尾部 `remaining` 追加逻辑带出）。

**练习 3**：为什么 `values` 的类型是 `&[String]` 而不是 `&[&str]`？

**参考答案**：因为每个值在进入 `replace_patterns` **之前**就已经被 `format!(...)` 格式化成了一个 `String`（见 4.3 节）。也就是说，「应用格式说明符」发生在调用方（编译期生成的代码里），`replace_patterns` 收到的已经是成型的字符串，它本身只管查找与拼接，不再做格式化。

---

### 4.2 编译期解析格式说明符：Argument.specifiers

#### 4.2.1 概念说明

在 4.1 里我们看到的 `values` 都是已经格式化好的 `String`。那么 `count = 7 : {:05}` 中的 `: {:05}` 是从哪来的？它是在**编译期**由 `_tr!` 的参数解析器读出来的，存在每个 `Argument` 的 `specifiers` 字段里。

回顾 [u3-l2](u3-l2-tr-macro-codegen.md)：`_tr!` 把每个参数解析成一个 `Argument` 结构。它的定义里除了 `name` 和 `value`，还有一个关键字段：

[crates/macro/src/tr.rs:L99-L104](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L99-L104) —— `Argument { name, value, specifiers: Option<String> }`。

`specifiers` 就是格式说明符的「裸内容」。对于 `count = 7 : {:05}`，`specifiers` 最终是 `Some(":05")`（注意：是花括号**里面**的内容，不含外层 `{}`）。如果参数没写 `: {…}`，`specifiers` 就是 `None`，表示用默认的 `{}`（即 `Display`）格式化。

这套语法支持任意 Rust 标准 `std::fmt` 说明符：`{:08}`（零填充 8 位）、`{:.2}`（浮点两位小数）、`{:>10}`（右对齐宽度 10）等，因为它们最终都被原样塞进 `format!` 里。

#### 4.2.2 核心流程

`Argument::parse` 解析一个参数的顺序是：

1. 跳过前导逗号；
2. 解析名字（标识符或字符串字面量，如 `count` 或 `"count"`）；
3. 解析分隔符（`=` 或 `=>`，二者等价）；
4. 解析值（任意表达式，如 `7`、`a / 2`、`"world"`）；
5. **可选地**解析说明符：若跟了 `:`，再看是否跟了一对花括号 `{…}`，若是，则把花括号里的 token 逐个读出、拼接成字符串，存进 `specifiers`。

第 5 步是本节重点。它的关键技巧是：花括号里的内容（如 `:05`）本身**不是**一个完整的 Rust token，而是若干零散 token（`:` 是标点、`05` 是整数字面量）。解析器用 `while let Ok(s) = content.parse::<TokenTree>()` 一个一个读，把每个 token 的字符串形式拼起来，最终得到 `":05"`。

#### 4.2.3 源码精读

[crates/macro/src/tr.rs:L153-L169](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L153-L169) —— 在值之后，`peek(Token![:])` 判断是否有冒号；若有且后面跟花括号，则读出花括号内所有 token 拼成 `specifiers`。

```rust
let specifiers = if input.peek(Token![:]) {
    let _ = input.parse::<Token![:]>()?;
    if input.peek(Brace) {
        let content;
        let _ = syn::braced!(content in input);          // 进入 {…} 内部
        let mut specifiers = String::new();
        while let Ok(s) = content.parse::<proc_macro2::TokenTree>() {
            specifiers.push_str(&s.to_string());          // 逐 token 拼接
        }
        Some(specifiers)                                   // 例如 ":05"
    } else {
        None
    }
} else {
    None
};
```

把 `count = 7 : {:05}` 套进去：解析完 `count = 7` 后，看到 `:`，再看到 `{`，进入花括号；里面读到 `:`（拼成 `":"`）和 `05`（拼成 `"05"`），最终 `specifiers = Some(":05")`。

> 这里有个有趣之处：解析器并不理解 `:05` 的语义，它只是「把花括号里的原始 token 原样拼回字符串」。至于这个字符串是不是合法的 `std::fmt` 说明符，要等到 4.3 节包进 `format!("{:05}", ...)` 后，由 Rust 编译器在编译生成的代码时去校验。写错了（比如 `:zz`）会得到一个 `format!` 编译错误，而不是 `_tr!` 自己报错。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**（无需运行），目标是让你亲手验证说明符的解析路径。

1. **实践目标**：在源码里定位说明符解析分支，并预测几个写法对应的 `specifiers` 值。
2. **操作步骤**：
   - 打开 [crates/macro/src/tr.rs:L153-L169](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L153-L169)。
   - 对照下表，逐一预测 `specifiers` 的值：

     | `t!` 参数写法 | `name` | `value` | `specifiers` |
     | --- | --- | --- | --- |
     | `count = 7` | `"count"` | `7` | `None` |
     | `count = 7 : {:05}` | `"count"` | `7` | `Some(":05")` |
     | `price = 3.14 : {:.2}` | `"price"` | `3.14` | `Some(":.2")` |
     | `id => 42 : {:08}` | `"id"` | `42` | `Some(":08")` |

3. **需要观察的现象**：注意 `=>` 与 `=` 等价（都只是分隔符），以及 `specifiers` 只存花括号**内部**内容、不含外层花括号。
4. **预期结果**：上表的预测即答案。最后一行 `id => 42 : {:08}` 正是 README 与根 crate 文档里给出的经典示例，其运行结果在下一节会得到验证。

#### 4.2.5 小练习与答案

**练习 1**：如果写成 `count = 7 :` 但**后面没有花括号**（即 `count = 7 :` 结尾），`specifiers` 会是什么？

**参考答案**：`None`。根据源码，`peek(Token![:])` 为真会消费冒号，但随后 `peek(Brace)` 为假，进入 `else { None }` 分支。也就是说「光有冒号、没有花括号」不会产生说明符（冒号被默默消费，值按默认 `{}` 格式化）。

**练习 2**：为什么 `specifiers` 存的是 `":05"` 而不是 `"{:05}"`？

**参考答案**：解析时 `syn::braced!` 已经「吃掉」了外层的 `{` 与 `}`，`specifiers` 只保留内部内容。外层花括号会在 4.3 节由 `format!("{{{}}}", s)` 重新包回去——这样做是为了和「无说明符时用 `{}`」统一处理。

**练习 3**：`specifiers = "abc"` 这种非法说明符会在什么时候报错？

**参考答案**：不会在 `_tr!` 解析阶段报错（解析器不校验语义）。它会在 4.3 节被包成 `format!("{abc}", value)` 写进生成代码，由 Rust 编译器在编译这段生成代码时报「格式字符串非法」的错误。

---

### 4.3 把键值对齐并拼装 format!：into_token_stream 的桥梁

#### 4.3.1 概念说明

前两节我们有了两个端点：运行时的 `replace_patterns(input, patterns: &[&str], values: &[String])`，和编译期解析出的 `Argument { name, value, specifiers }`。本节的 `into_token_stream` 就是把它们接起来的**桥梁**——它用 `quote!` 生成一段代码，在这段代码里：

- 把所有参数的 `name` 收集成 `keys`（字符串数组）；
- 把所有参数的 `value` 配合 `specifiers` 包成 `format!("{…}", value)`，收集成 `values`（字符串数组）；
- 两个数组**按下标一一对应**，再连同译文一起调用 `replace_patterns`。

`name`（来自 `t!` 参数）必须与译文里的 `%{name}` 同名，正是因为这里的 `keys` 数组会被 `replace_patterns` 拿去和占位符名字逐个比对。

#### 4.3.2 核心流程

`into_token_stream` 在「有参数」分支里生成大致如下骨架（伪代码，省略 miss 分支）：

```text
{
    let msg_val = <原始消息>;          // 例如 "Zero padded number: %{count}"
    let msg_key = <查找用的 key>;
    let keys   = &[ <每个参数的名字> ];           // &["count"]
    let values = &[ <每个参数 format! 后的值> ];  // &[format!("{:05}", count)]
    if let Some(translated) = crate::_rust_i18n_try_translate(locale, &msg_key) {
        let replaced = rust_i18n::replace_patterns(&translated, keys, values);
        Cow::from(replaced)
    } else {
        // 未命中：对原始 msg_val 也做一次 replace_patterns
        let replaced = rust_i18n::replace_patterns(rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
        Cow::from(replaced)
    }
}
```

要点：

- `keys` 与 `values` 用**同一个**迭代源（`self.args`）构造，顺序天然一致，下标对齐；
- 进入 `into_token_stream` 前，`filter_arguments` 已把 `locale`、`_minify_key*` 等系统参数从 `self.args` 里剔除（见 [u3-l2](u3-l2-tr-macro-codegen.md)），所以留下的全是「真正的占位符参数」；
- 即便译文**未命中**（`try_translate` 返回 `None`），也会对原始消息字符串做一次 `replace_patterns`——这就是为什么 `t!("Zero padded number: %{count}", count = 7 : {:05})` 即使没有任何翻译文件、也能返回插好值的结果。

#### 4.3.3 源码精读

**构造 `keys` 数组**（取每个参数的名字）：

[crates/macro/src/tr.rs:L418-L418](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L418-L418) —— `self.args.keys()` 返回名字的 `Vec<String>`（其定义见 [crates/macro/src/tr.rs:L192-L194](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L192-L194)），逐个 `quote! { #v }` 转成 token。

**构造 `values` 数组**（把说明符包进 `format!`）：

[crates/macro/src/tr.rs:L419-L431](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L419-L431) —— 这是本讲最核心的一段：`specifiers` 为 `None` 时用 `"{}"`，为 `Some(s)` 时用 `format!("{{{}}}", s)` 把 `s` 包回带花括号的形式，再生成 `format!(#spec, #value)`。

```rust
let values: Vec<_> = self.args.as_ref().iter().map(|v| {
    let value = &v.value;
    let sepecifiers = v.specifiers.as_ref()
        .map_or("{}".to_owned(), |s| format!("{{{}}}", s));   // None→"{}" ; Some(":05")→"{:05}"
    quote! { format!(#sepecifiers, #value) }                   // 例如 format!("{:05}", count)
}).collect();
```

> 注意 `format!("{{{}}}", s)` 的转义：在 Rust 格式串里 `{{` 表示一个字面 `{`、`}}` 表示一个字面 `}`。所以 `"{{{}}}"` = 字面 `{` + 占位 `{}` + 字面 `}`，代入 `s=":05"` 后得到 `"{:05}"`。代码里的局部变量名 `sepecifiers`（拼写如此）是源码原样，不影响逻辑。

**生成的调用点**（把两个数组交给 `replace_patterns`）：

[crates/macro/src/tr.rs:L446-L462](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L446-L462) —— 「有参数」分支：先 `let keys = &[#(#keys),*];` / `let values = &[#(#values),*];` 用 `quote!` 的重复语法展开数组，命中与未命中两条路径都调用 `replace_patterns`。

```rust
let keys = &[#(#keys),*];      // 例如 &["count"]
let values = &[#(#values),*];  // 例如 &[format!("{:05}", count)]
if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) {
    let replaced = rust_i18n::replace_patterns(&translated, keys, values);
    std::borrow::Cow::from(replaced)
} else {
    #logging
    let replaced = rust_i18n::replace_patterns(rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
    std::borrow::Cow::from(replaced)
}
```

把整条链路串起来看 `t!("Zero padded number: %{count}", count = 7 : {:05})`：

1. **编译期**：`Argument::parse` 得到 `name="count"`, `value=7`, `specifiers=Some(":05")`；
2. **编译期**：`into_token_stream` 把它包成 `format!("{:05}", 7)`，`keys = ["count"]`，`values = [format!("{:05}", 7)]`；
3. **运行期**：生成的 `format!("{:05}", 7)` 求值为 `"00007"`；
4. **运行期**：`replace_patterns("Zero padded number: %{count}", &["count"], &["00007".to_string()])` 扫描到 `%{count}`，匹配名字 `count`，替换成 `"00007"`，最终得到 `"Zero padded number: 00007"`。

这正是示例 `examples/app-minify-key/src/main.rs:54` 里 `t!("Zero padded number: %{count}", count = i : {:08})` 的同款写法，只是那里用 `{:08}`（填充到 8 位）。

#### 4.3.4 代码实践

这是本讲的主实践，目标是让你**写一个带格式说明符的 `t!`**，并解释说明符从参数到 `format!` 的完整旅程。

1. **实践目标**：用 `count = 7 : {:05}` 让翻译结果出现 5 位零填充数字 `00007`，并说明 `specifiers` 如何被包成 `"{:05}"`。
2. **操作步骤**：在任意已调用 `i18n!("locales")` 的项目（推荐直接用 `examples/app`）的 `main` 或测试里写：

   ```rust
   // 即使 locales 目录里没有这条翻译，未命中分支也会对原始消息做插值
   let s = t!("Zero padded number: %{count}", count = 7 : {:05});
   println!("{}", s);
   ```

   运行：`cargo run`（在 `examples/app` 下用 `cargo run --example app`）。
3. **需要观察的现象**：输出应为 `Zero padded number: 00007`。再把 `: {:05}` 改成 `: {:08}`，观察输出变成 `00000007`（8 位）。
4. **预期结果**：

   - `count = 7 : {:05}` → `"Zero padded number: 00007"`（因为 `format!("{:05}", 7)` = `"00007"`）；
   - `count = 7 : {:08}` → `"Zero padded number: 00000007"`（与 README:225-226 及根 crate文档 src/lib.rs:132-134 的 `sn = 123 : {:08}` → `000000123` 同源）。

   **解释 `specifiers` 的旅程**：`count = 7 : {:05}` 在 `Argument::parse` 里读出花括号内容 `:05`，存为 `specifiers = Some(":05")`；`into_token_stream` 用 `format!("{{{}}}", ":05")` 把它包回 `"{:05}"`，生成 `format!("{:05}", 7)`；运行期求值为 `"00007"`，再被 `replace_patterns` 填进 `%{count}`。

5. **待本地验证的部分**：上述输出值由源码逻辑严格推导得出，可直接验证；若你的项目对 `count` 这个 key 恰好配置了别的译文，命中分支会用那条译文（但 `%{count}` 占位符替换逻辑不变）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `keys` 和 `values` 两个数组「下标对齐」如此重要？

**参考答案**：因为 `replace_patterns` 用 `patterns.iter().zip(values.iter())` 把两个数组按下标配对，靠「名字相等」找到值。如果 `keys` 和 `values` 顺序不一致，`zip` 配对就会错位，导致把甲参数的值填进乙占位符。`into_token_stream` 通过对**同一个** `self.args` 顺序迭代来构造两者，从源头保证了对齐。

**练习 2**：`t!("msg %{a} %{b}", a = 1, b = 2)` 和 `t!("msg %{a} %{b}", b = 2, a = 1)` 的结果是否相同？为什么？

**参考答案**：相同。因为 `replace_patterns` 是**按名字匹配**（`pattern.as_bytes() == key`），不是按下标匹配占位符。无论参数书写顺序如何，`%{a}` 总会匹配到名为 `a` 的值、`%{b}` 总会匹配到名为 `b` 的值。下标对齐只保证 `keys[i]` 与 `values[i]` 是同一参数的名字与值，与占位符在译文里的出现顺序无关。

**练习 3**：如果一个参数既不是 `locale`、也不是任何占位符的名字（例如译文里没有 `%{foo}`，却传了 `foo = 1`），会发生什么？

**参考答案**：`foo` 会被正常解析进 `keys/values`，生成的 `format!("{}", 1)` 也会在运行期求值；但 `replace_patterns` 扫描译文时找不到任何 `%{foo}` 占位符，于是这个值**永远不会被用到**，相当于静默忽略。不会报错，只是这次格式化白做了。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**手算并验证一次「带格式说明符的多占位符插值」。**

设想译文（可以放进你的 `locales/en.yml`，也可以直接用字面消息触发未命中分支）：

```yaml
en:
  invoice: "Order %{id}, total $%{price}, due in %{days} days."
```

任务：

1. 写出 `t!("invoice", id = 42 : {:08}, price = 19.5 : {:.2}, days = 3)` 的**预期输出**（先手算，再运行验证）。
2. 对照源码说明每个参数的 `specifiers` 分别是什么、各自被包成怎样的 `format!` 调用。
3. 写出对应的 `keys` 和 `values` 两个数组的内容，并解释它们如何被 `replace_patterns` 消费。

**参考解答**：

- `id = 42 : {:08}` → `specifiers = Some(":08")` → `format!("{:08}", 42)` = `"00000042"`；
- `price = 19.5 : {:.2}` → `specifiers = Some(":.2")` → `format!("{:.2}", 19.5)` = `"19.50"`；
- `days = 3` → `specifiers = None` → `format!("{}", 3)` = `"3"`；
- `keys = ["id", "price", "days"]`，`values = ["00000042", "19.50", "3"]`；
- `replace_patterns` 扫描 `"Order %{id}, total $%{price}, due in %{days} days."`，依次把 `%{id}` → `00000042`、`%{price}` → `19.50`、`%{days}` → `3`，最终得到：

  ```text
  Order 00000042, total $19.50, due in 3 days.
  ```

把这个 `t!` 调用写进测试（`assert_eq!`）运行验证即可。这个任务同时覆盖了 `Argument.specifiers` 解析（4.2）、`format!` 拼装与 `keys/values` 对齐（4.3）、以及 `replace_patterns` 的按名替换（4.1）。

## 6. 本讲小结

- **`replace_patterns` 是运行时的纯函数扫描器**：用一个三态（0/1/2）字节级状态机定位 `%{name}`，按名字在 `keys` 里查找、用同下标的 `values` 替换；未命中名字则原样保留 `%{name}`；用 `SmallVec` 和 `chunks_exact(2)` 兼顾性能与对畸形输入的鲁棒性。
- **格式说明符在编译期由 `Argument::parse` 解析**：`count = 7 : {:05}` 中的 `: {:05}` 被读成 `specifiers = Some(":05")`——只存花括号**内部**内容，原样拼接 token，不做语义校验。
- **`into_token_stream` 是连接两端的桥梁**：它把 `specifiers` 包回 `format!("{:05}", value)`，把所有参数的名字与格式化后的值按相同顺序收成 `keys`/`values` 两个数组，再生成对 `replace_patterns` 的调用。
- **名字对齐是核心契约**：`t!` 参数名必须与译文 `%{name}` 同名；替换按名字匹配、与参数书写顺序和占位符出现顺序都无关。
- **未命中也会插值**：即使 `try_translate` 返回 `None`，原始消息字符串也会经过一次 `replace_patterns`，所以字面消息加参数的写法（如 `t!("... %{count}", count = 7 : {:05})`）即使没有翻译文件也能正常工作。
- **整条链路的分工**：编译期负责「解析 + 生成 `format!` 调用」，运行期只负责「扫描 + 查表 + 拼接」——格式化发生在调用方、替换发生在 `replace_patterns`。

## 7. 下一步学习建议

- **继续运行时机制**：本讲的 `replace_patterns` 总能拿到一个 `locale`，但「这个 locale 是怎么定的、找不到译文时又怎么逐级回退」留到了 [u3-l4 翻译回退机制](u3-l4-fallback-mechanism.md)，建议接着读，补全 `_tr!` 生成代码里 `_rust_i18n_try_translate` → `_rust_i18n_lookup_fallback` 的回退链。
- **回到代码生成的全貌**：本讲聚焦插值与格式化；如果想看 `into_token_stream` 里另外几条与 minify_key 相关的 codegen 分支，请进入 [u6 minify_key 短键机制](u6-l1-minify-key-algorithm.md) 系列。
- **性能视角**：本讲提到的 `SmallVec`、`Cow` 零拷贝等优化，在 [u8-l2 性能基准与内存优化](u8-l2-benchmark-and-optimization.md) 里有系统讲解，可对照 `benches/bench.rs` 看 `t` 与 `t_with_args` 的耗时差异。
- **源码延伸阅读**：重读 [crates/macro/src/tr.rs:L390-L466](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L466) 的 `into_token_stream` 全貌，并结合 [src/lib.rs:L45-L91](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L91) 的 `replace_patterns`，把「编译期生成 → 运行期替换」这条链路在脑子里完整跑一遍。
