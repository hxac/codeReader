# 变体命名与文档：BaseXX 规则与自动生成的 doc 注释

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `DynamicSpacing::BaseXX` 中的 `XX` 取自哪个值——无论是 `Single` 输入还是 `Tuple` 输入，统一规则是「Default 档（默认密度）下的像素值」。
2. 解释 `format_ident!("Base{:02}", n)` 中 `{:02}` 补零的作用，以及为什么 `4` 会变成 `Base04` 而 `12` 只是 `Base12`。
3. 读懂宏自动生成的 `` `Apx`|`Bpx`|`Cpx (@16px/rem)` `` 文档注释：三个数字分别对应哪档密度，`@16px/rem` 又是什么意思。
4. 掌握向 `ui/src/styles/spacing.rs` 的宏调用中**新增一个间距值**的完整流程，并能在编译前预测出新变体名和它的文档内容。

本讲承接 u2-l2：上一讲我们知道了 `quote!` 模板如何「两轮备料 + 一次组装」，本讲聚焦备料阶段里最有业务味道的两样产物——**变体名**与 **doc 注释**。

## 2. 前置知识

### 2.1 枚举变体（enum variant）

Rust 中枚举的每个取值叫一个变体。宏最终生成的是这样一个枚举：

```rust
pub enum DynamicSpacing {
    Base00,
    Base02,
    // ...
    Base24,
}
```

`Base00`、`Base24` 就是变体。变体名必须在编译期确定，这正是过程宏的用武之地——它在编译期「发明」这些名字。

### 2.2 doc 注释与 `#[doc = "...]`

写在项上面的 `/// 说明文字` 是文档注释，rustdoc 会把它渲染进文档。它和属性 `#[doc = "说明文字"]` **完全等价**——`///` 只是后者的语法糖。这个等价性是本讲的关键：宏生成代码时没法「写 `///`」（`///` 必须字面出现在源码里），但可以拼出一个字符串再以 `#[doc = #doc_string]` 的形式注入，效果一模一样。u2-l2 已经预演过这个技巧，本讲看它的实际产出。

### 2.3 Rust 格式化补零：`{:02}`

`format!("{:02}", 4)` 得到 `"04"`：`0` 表示用 `0` 填充，`2` 表示最小宽度为 2。规则是**至少**两位，不够补零，够了两倍也没关系：

- `4` → `"04"`（补一位零）
- `12` → `"12"`（正好两位）
- `8` → `"08"`
- `100` → `"100"`（三位，不截断）

### 2.4 像素、rem 与三档密度

Zed 的 UI 间距有两层单位：

- **px（像素）**：屏幕上的绝对尺寸。
- **rem**：相对于「基准字号」的比例单位。Zed 取 \( 1\,\text{rem} = 16\,\text{px} \)，即 `BASE_REM_SIZE_IN_PX = 16.0`。用户调大 UI 字号时，rem 不变、实际像素变大，间距随之缩放——这就是文档里 "Scales with the user's rem size" 的含义。

同时，主题设置里有三档 UI 密度（`UiDensity`）：`Compact`（紧凑）、`Default`（默认）、`Comfortable`（宽松）。同一个间距值在三档下对应三个不同像素值。本讲只关心「数字从哪来」；三档值在运行时如何被选用是下一讲 u3-l2 的主题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui_macros/src/dynamic_spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs) | 宏实现：解析输入、生成变体名与 doc 注释、拼装枚举 |
| [crates/ui/src/styles/spacing.rs](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs) | 全仓库唯一的宏调用点，14 个间距值清单 |
| [crates/ui/src/styles.rs](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles.rs#L16) | `pub use spacing::*;` 把生成的枚举再导出到 ui crate |
| [crates/ui/src/components/button/button.rs](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465) | 使用点：按钮内用 `DynamicSpacing::Base04.rems(cx)` |
| [crates/ui/src/components/toggle.rs](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/toggle.rs#L518-L519) | 使用点：开关控件用 `Base32`/`Base20` 定宽高 |

## 4. 核心概念与源码讲解

### 4.1 BaseXX 变体命名规则

#### 4.1.1 概念说明

`derive_dynamic_spacing!` 的输入有两种形态（u2-l1 解析的产物）：

- `Single(n)`：写一个数，如 `24`。三档值按标准公式推导：\( (\max(n-4,\,0),\; n,\; n+4) \)。
- `Tuple(a, b, c)`：写一个三元组，如 `(1, 2, 4)`。三档值直接用 \( (a,\, b,\, c) \)，分别对应 Compact、Default、Comfortable。

变体名规则只有一句话：**`XX` 永远取 Default 档（中间那档）的像素值**。

- `Single(n)`：Default 档就是 \( n \) 本身，所以 `Base{ n }`。
- `Tuple(a, b, c)`：Default 档是 \( b \)，所以 `Base{ b }`。

两种输入殊途同归。设计意图很直白：变体名回答「默认设置下这个间距是几像素」，这正是使用者在清单里挑间距时最关心的数字。`{:02}` 补零则保证个位数也能对齐成两位（`Base04` 而不是 `Base4`），让 `Base02`、`Base04`、`Base08`… 排序后在 IDE 补全列表里看起来整齐一致。

#### 4.1.2 核心流程

```
对每个输入值 v：
  若 v = Single(n)：
      三档 = ( max(n-4, 0),  n,  n+4 )
      变体名 = "Base" + pad_zero_2(n)
  若 v = Tuple(a, b, c)：
      三档 = ( a,  b,  c )
      变体名 = "Base" + pad_zero_2(b)
```

注意 `max(n-4, 0)`：紧凑档不允许出现负像素，小值会被钳到 0。另外命名用的是**整数解析**（`u32`），而计算三档值用的是**浮点解析**（`f32`）——同一个字面量被解析了两次、用于两个目的。

#### 4.1.3 源码精读

命名逻辑出现在**两轮备料**中，代码几乎相同。第一轮（为 `spacing_ratio` 的 match 分支造变体名）：

[crates/ui_macros/src/dynamic_spacing.rs:L56-L63](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L56-L63) —— 对 `Single` 取 `n`、对 `Tuple` 取中间值 `b`，用 `format_ident!("Base{:02}", ...)` 拼出变体标识符：

```rust
let variant = match v {
    DynamicSpacingValue::Single(n) => {
        format_ident!("Base{:02}", n.base10_parse::<u32>().unwrap())
    }
    DynamicSpacingValue::Tuple(_, b, _) => {
        format_ident!("Base{:02}", b.base10_parse::<u32>().unwrap())
    }
};
```

注意 `Tuple(_, b, _)` 用两个 `_` 直接丢弃了 `a` 和 `c`——命名只关心中间值。

[crates/ui_macros/src/dynamic_spacing.rs:L127-L142](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L127-L142) —— 总模板里变体名的最终去向：`#variant_names` 在 `#(...)*` 重复语法中被逐个展开成枚举变体：

```rust
pub enum DynamicSpacing {
    #(
        #[doc = #doc_strings]
        #variant_names,
    )*
}
```

把当前 `spacing.rs` 里的 14 个输入代入规则，得到完整对照表（这也是逆向使用点代码时的查表依据）：

| 宏输入 | 形态 | Compact | Default | Comfortable | 生成的变体 |
| --- | --- | --- | --- | --- | --- |
| `(0, 0, 0)` | Tuple | 0 | 0 | 0 | `Base00` |
| `(1, 1, 2)` | Tuple | 1 | 1 | 2 | `Base01` |
| `(1, 2, 4)` | Tuple | 1 | 2 | 4 | `Base02` |
| `(2, 3, 4)` | Tuple | 2 | 3 | 4 | `Base03` |
| `(2, 4, 6)` | Tuple | 2 | 4 | 6 | `Base04` |
| `(3, 6, 8)` | Tuple | 3 | 6 | 8 | `Base06` |
| `(4, 8, 10)` | Tuple | 4 | 8 | 10 | `Base08` |
| `(10, 12, 14)` | Tuple | 10 | 12 | 14 | `Base12` |
| `(14, 16, 18)` | Tuple | 14 | 16 | 18 | `Base16` |
| `(18, 20, 22)` | Tuple | 18 | 20 | 22 | `Base20` |
| `24` | Single | 20 | 24 | 28 | `Base24` |
| `32` | Single | 28 | 32 | 36 | `Base32` |
| `40` | Single | 36 | 40 | 44 | `Base40` |
| `48` | Single | 44 | 48 | 52 | `Base48` |

两个值得注意的推论：

1. **清单里没有 `Base05`、`Base07`、`Base10` 这类变体**。`Base06` 存在是因为写了 `(3, 6, 8)`，而不是因为写了 `6`（若写 `6`，紧凑档会被钳成 2，三档是 `2|6|10`）。命名规则允许的变体集合完全由这张表决定。
2. **命名可能撞车**。宏没有任何查重逻辑：如果清单里同时写 `24` 和 `(20, 24, 28)`，两个输入都会生成 `Base24`，展开后的枚举出现重复变体，rustc 会报「变体重复定义」类的编译错误。这个坑在新增值时要靠人肉避免。

#### 4.1.4 代码实践

**实践目标**：验证「变体名 = Default 档像素值」这条规则，学会从使用点反推宏输入。

**操作步骤**：

1. 打开 [crates/ui/src/components/toggle.rs:L518-L519](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/toggle.rs#L518-L519)，这里用 `DynamicSpacing::Base32.rems(cx)` 定宽度、`Base20.rems(cx)` 定高度。
2. 回到对照表反查：`Base32` 只能来自 `Single(32)`（三档 `28|32|36`）；`Base20` 来自 `Tuple(18, 20, 22)`（三档 `18|20|22`）。
3. 再看 [crates/ui/src/components/button/button.rs:L465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465) 的 `Base04` 和 [crates/repl/src/notebook/cell.rs:L328](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/repl/src/notebook/cell.rs#L328) 的 `Base12`，分别反推它们的宏输入与三档像素值。

**需要观察的现象**：`BaseXX` 的 `XX` 与宏输入清单中的某个数字一一对应（`Tuple` 对应中间值，`Single` 对应其本身），不存在「凭空出现」的变体。

**预期结果**：`Base04` ← `(2, 4, 6)`；`Base12` ← `(10, 12, 14)`。这是纯源码阅读型实践，结论可直接在上述两处源码中核对，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：若在宏调用中写 `8`（Single），生成的变体名是什么？三档像素值分别是多少？

答案：变体名是 `Base08`（`8` 补零成 `08`）。三档按公式 \( \max(8-4,0)=4,\ 8,\ 8+4=12 \)，即 Compact 4px、Default 8px、Comfortable 12px。

**练习 2**：为什么 `(1, 2, 4)` 生成的变体叫 `Base02` 而不是 `Base01` 或 `Base04`？

答案：命名规则统一取 **Default 档**的像素值，`Tuple(a, b, c)` 的 Default 档是中间值 `b = 2`，补零后得到 `Base02`。`1` 是 Compact 档、`4` 是 Comfortable 档，都不参与命名。

**练习 3**：`format!("{:02}", 12)` 和 `format!("{:02}", 7)` 分别输出什么？如果某个间距值是 100，变体名会是什么？

答案：分别是 `"12"`（已达两位，不补）和 `"07"`（补一位零）。100 已经超过最小宽度 2，不会被截断，变体名是 `Base100`。

### 4.2 自动生成文档注释

#### 4.2.1 概念说明

`DynamicSpacing` 是**生成**出来的枚举，手写文档会有个致命问题：文档说 `Base24` 是 24px，可如果有人把宏输入从 `24` 改成 `(20, 24, 28)` 的邻值，文档立刻过时。宏的做法是把文档的生成和代码的生成放在**同一处、同一批数据**上——既然三档值已经算出来了，顺手把它写进 doc 注释，文档永远与实现同步。

每 个变体的文档格式是：

```
`{compact}px`|`{default}px`|`{comfortable}px (@16px/rem)` - Scales with the user's rem size.
```

三段竖线分隔的 `px` 值按 **Compact | Default | Comfortable** 顺序排列，`(@16px/rem)` 说明这些像素值以 \( 16\,\text{px} = 1\,\text{rem} \) 为换算基准，最后一句点明它会随用户的 rem 设置缩放。

#### 4.2.2 核心流程

```
对每个输入值 v（第二轮备料）：
  若 v = Single(n)：
      compact    = max(n - 4, 0)
      comfortable = n + 4
      doc = f"{compact}px | {n}px | {comfortable}px (@16px/rem) - Scales..."
  若 v = Tuple(a, b, c)：
      doc = f"{a}px | {b}px | {c}px (@16px/rem) - Scales..."
  产出 (变体名 token, doc 字符串 token) 对
```

关键点：**doc 里的三个数字与 `spacing_ratio` 分支里的三个数字来自同一轮计算**，只是前者以十进制文本形式进了 `#[doc]`，后者以字面量形式进了代码。二者不可能不一致。

#### 4.2.3 源码精读

[crates/ui_macros/src/dynamic_spacing.rs:L103-L112](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L103-L112) —— Single 输入的文档构造：先算出 `compact` 与 `comfortable`，再 `format!` 成固定格式：

```rust
DynamicSpacingValue::Single(n) => {
    let n = n.base10_parse::<f32>().unwrap();
    let compact = (n - 4.0).max(0.0);
    let comfortable = n + 4.0;
    format!(
        "`{}px`|`{}px`|`{}px (@16px/rem)` - Scales with the user's rem size.",
        compact, n, comfortable
    )
}
```

[crates/ui_macros/src/dynamic_spacing.rs:L113-L121](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L113-L121) —— Tuple 输入直接把 `a`、`b`、`c` 填进同一模板：

```rust
DynamicSpacingValue::Tuple(a, b, c) => {
    let a = a.base10_parse::<f32>().unwrap();
    let b = b.base10_parse::<f32>().unwrap();
    let c = c.base10_parse::<f32>().unwrap();
    format!(
        "`{}px`|`{}px`|`{}px (@16px/rem)` - Scales with the user's rem size.",
        a, b, c
    )
}
```

一个细节：这里解析成 `f32` 后用 `{}` 显示。对整数输入（`24`、`(5, 7, 9)`），`f32` 的 `Display` 输出仍是 `24`、`5` 这样的整数样式，所以文档读起来干净；这也是宏输入**只接受整数字面量**（`LitInt`）的好处之一。

[crates/ui_macros/src/dynamic_spacing.rs:L123](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L123) —— `format!` 得到的是 `String`，经 `quote!(#doc_string)` 变成字符串字面量 token：`(quote!(#variant), quote!(#doc_string))`，随后 `unzip` 拆成两个等长的 `Vec`（`variant_names` 与 `doc_strings`）。

[crates/ui_macros/src/dynamic_spacing.rs:L136-L142](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L136-L142) —— 注入点：`#[doc = #doc_strings]` 紧贴在每个变体上方。rustdoc 会把每个字符串渲染成该变体的说明。

最后别忘了枚举**整体**也有一段静态文档，它写死在模板里、不随输入变化：

[crates/ui_macros/src/dynamic_spacing.rs:L128-L135](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L128-L135) —— 其中一句直接解释了本讲的命名规则："The number following \"Base\" refers to the base pixel size at the default rem size and spacing settings."（Base 后面的数字 = 默认 rem 与默认密度下的像素值）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到宏生成的文档在 rustdoc 里的样子。

**操作步骤**：

1. 在 Zed 仓库根目录运行 `cargo doc -p ui --no-deps --document-private-items`（加最后那个 flag 是因为 `DynamicSpacing` 虽是 `pub`，但部分关联项可能在私有模块中；若不加也能看到就可省略）。
2. 打开 `target/doc/ui/` 下生成的文档，搜索 `DynamicSpacing`（它经 [crates/ui/src/styles.rs:L16](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles.rs#L16) 的 `pub use spacing::*;` 从 ui crate 导出）。
3. 对照变体列表，核对 `Base24` 的文档是否为 `` `20px`|`24px`|`28px (@16px/rem)` - Scales with the user's rem size. ``。

**需要观察的现象**：每个变体下方都挂着一段格式一致、数值各异的说明；数值与 4.1 对照表的三档值完全吻合。

**预期结果**：`Base16`（来自 `(14, 16, 18)`）的文档应为 `` `14px`|`16px`|`18px (@16px/rem)` - Scales with the user's rem size. ``。待本地验证（本讲义编写时未实际运行 rustdoc）。

#### 4.2.5 小练习与答案

**练习 1**：写出 `Base08`（来自 `(4, 8, 10)`）的完整文档字符串。

答案：`` `4px`|`8px`|`10px (@16px/rem)` - Scales with the user's rem size. `` —— Tuple 输入按顺序直接填 `a|b|c`。

**练习 2**：为什么宏用 `#[doc = #doc_string]` 而不是直接在 `quote!` 里写 `/// ...`？

答案：`///` 是源码层的语法糖，`quote!` 模板里的 `///` 不会被当作 doc 注释处理（模板里的 `#` 开头是插值语法、`///` 只是普通 token）；而 `#[doc = "..."]` 是属性形式，字符串可以作为插值变量动态传入。两者对 rustdoc 等价，但只有后者能接住运行时（宏展开时）拼出来的字符串。

**练习 3**：文档里的 `(@16px/rem)` 想告诉使用者什么？如果用户把 UI 字号（rem 基准）调大，`Base24` 的实际像素会怎么变？

答案：它说明文档中的像素值都以 16px = 1rem 为基准标注。`Base24` 的 Default 档间距比例是 \( 24 / 16 = 1.5 \) rem；用户调大 rem 基准后，rem 值不变、实际渲染像素按比例放大——这正是 "Scales with the user's rem size" 的含义（换算细节在 u3-l2 展开）。

### 4.3 宏调用处 spacing.rs：新增间距值的完整流程

#### 4.3.1 概念说明

[crates/ui/src/styles/spacing.rs](https://github.com/zed-industries-zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs) 是 `derive_dynamic_spacing!` 全仓库唯一的调用点，文件只有 55 行：一个 `use`、一大段注释、一次宏调用、一个辅助函数。它是「间距清单」的唯一真身——**所有**组件用到的间距变体都必须先登记在这里。文件顶部的注释块本身就是使用手册，值得逐句读：

[crates/ui/src/styles/spacing.rs:L5-L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L5-L28) —— 注释明确写出了两种输入的语义（含 `(1, 2, 4)` 与 `24` 两个例子）、`(n-4, n, n+4)` 标准公式，以及 BaseXX 命名规则："XX = the pixel value @ default rem size and the default UI density"。

#### 4.3.2 核心流程

向间距系统新增一个值的完整链路：

```
1. 编辑 spacing.rs 的宏调用清单，加入新值（Single 或 Tuple）
2. cargo check -p ui：ui crate 重新编译，宏重新展开
3. DynamicSpacing 枚举多出对应变体（含自动生成的 doc）
4. 组件代码中即可使用 DynamicSpacing::BaseXX.rems(cx) / .px(cx)
5. （若走查清单）确认没有与现有值撞名（重复变体会导致编译失败）
```

#### 4.3.3 源码精读

[crates/ui/src/styles/spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) —— 调用本体：14 个值，前 10 个是 Tuple（精细控制三档），后 4 个是 Single（直接套公式）：

```rust
derive_dynamic_spacing![
    (0, 0, 0),
    (1, 2, 4),
    // ... 共 10 个三元组
    24,
    32,
    40,
    48
];
```

[crates/ui/src/styles.rs:L16](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles.rs#L16) —— `pub use spacing::*;`：宏生成的枚举由此进入 `ui` crate 的命名空间，组件才能 `use crate::DynamicSpacing` 或从 prelude 拿到它（如 [crates/ui/src/components/button/button_like.rs:L9](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button_like.rs#L9)）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：走完「新增间距值 → 变体出现 → 预测文档 → 还原」的完整闭环。以下操作都是**本地临时实验，结束后必须还原**。

**操作步骤**：

1. 打开 `crates/ui/src/styles/spacing.rs`，把宏调用改为（新增两个值）：

   ```rust
   derive_dynamic_spacing![
       (0, 0, 0),
       (1, 1, 2),
       (1, 2, 4),
       (2, 3, 4),
       (2, 4, 6),
       (3, 6, 8),
       (4, 8, 10),
       (5, 7, 9),
       (10, 12, 14),
       (14, 16, 18),
       (18, 20, 22),
       24,
       32,
       40,
       48,
       56
   ];
   ```

2. 在仓库根目录运行 `cargo check -p ui`，确认编译通过。
3. 验证新变体确实存在。任选其一：
   - 在编辑器里打开任意用到 `DynamicSpacing` 的文件（如 `button_like.rs`），输入 `DynamicSpacing::` 看补全列表里是否出现 `Base07` 与 `Base56`；
   - 或临时在 `spacing.rs` 文件末尾加一行 `const _CHECK: () = { let _ = DynamicSpacing::Base07; let _ = DynamicSpacing::Base56; };` 再跑一次 `cargo check -p ui`——变体存在则通过，不存在则报「找不到变体」的编译错误（这行只是探针，随清单一起还原）。
4. 在纸上写下你预测的两个新变体的 doc 注释与三档像素值。
5. **还原**：把 `spacing.rs` 恢复原样（`git checkout -- crates/ui/src/styles/spacing.rs` 或手动删除新增内容），再跑一次 `cargo check -p ui` 确认干净。

**需要观察的现象**：新增 `(5, 7, 9)` 与 `56` 后 `ui` crate 正常编译；移除后一切如初。若第 3 步用了探针常量，还原前它也应编译通过。

**预期结果**（按 4.1/4.2 的规则推算，待本地验证）：

| 新输入 | 新变体 | 三档像素值（Compact/Default/Comfortable） | 预测的 doc 注释 |
| --- | --- | --- | --- |
| `(5, 7, 9)` | `Base07` | 5 / 7 / 9 | `` `5px`|`7px`|`9px (@16px/rem)` - Scales with the user's rem size. `` |
| `56` | `Base56` | 52 / 56 / 60 | `` `52px`|`56px`|`60px (@16px/rem)` - Scales with the user's rem size. `` |

同时 `spacing_ratio` 会各多出一个分支：`Base07` 在三档下分别返回 \( 5/16 \)、\( 7/16 \)、\( 9/16 \)；`Base56`（Single 输入，公式 \( \max(56-4,0)=52 \mid 56 \mid 60 \)）分别返回 \( 52/16 \)、\( 56/16 \)、\( 60/16 \)。

> 说明：doc 注释的精确验证可结合 4.2.4 的 `cargo doc` 步骤，在还原前顺手查看 `Base07`、`Base56` 的文档页。

#### 4.3.5 小练习与答案

**练习 1**：设计评审想要一个 `Base10` 间距。写出你会往清单里加的输入，并说明为什么另一种形态不合适。

答案：加 `(8, 10, 12)`。因为 `Base10` 要求 Default 档为 10px。若写成 Single `10`，三档会是 `6|10|14`，紧凑档 6px 与 `Base06`（`(3, 6, 8)` 的 Default 档）语义重叠、且宽松档 14px 跨度偏大；用 Tuple 可以精确控制三档比例。当然，如果团队就是想要标准公式的 `6|10|14`，Single `10` 也完全合法——两种写法生成的变体名相同，都是 `Base10`。

**练习 2**：把 `24` 和 `(20, 24, 28)` 同时加进清单会发生什么？为什么？

答案：两个输入都会生成名为 `Base24` 的变体，展开后的枚举里出现两个 `Base24`，rustc 报「枚举变体重复定义」类编译错误。宏本身没有查重或报友好错误的逻辑（它对每个输入独立命名），这个约束靠调用方自查。这也是 u5-l1（健壮性）会回头讨论的改进点。

**练习 3**：为什么新增间距值只需要改 `spacing.rs` 这一个文件，而不需要去改 `dynamic_spacing.rs`？

答案：因为变体集合完全由**宏调用的输入数据**驱动，宏实现是通用的代码生成器。这正是「数据驱动的代码生成」的价值：加值是改数据，不是改逻辑；新变体的命名、文档、`spacing_ratio` 分支全部自动跟上。

## 5. 综合实践

**任务：给 DynamicSpacing 做一次「逆向工程 + 正向验证」。**

第一部分（逆向，纯阅读）：从下面三个真实使用点出发，填出整张表——不许先看 `spacing.rs` 的清单：

| 使用点 | 变体 | 宏输入（含形态） | Compact | Default | Comfortable | doc 注释（默写） |
| --- | --- | --- | --- | --- | --- | --- |
| [button.rs:L465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465) | `Base04` | ？ | ？ | ？ | ？ | ？ |
| [toggle.rs:L518](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/toggle.rs#L518) | `Base32` | ？ | ？ | ？ | ？ | ？ |
| [notebook_ui.rs:L1095](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/repl/src/notebook/notebook_ui.rs#L1095) | `Base16` | ？ | ？ | ？ | ？ | ？ |

填完后打开 [spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) 对答案。

第二部分（正向，临时实验后还原）：按 4.3.4 的步骤把 `(5, 7, 9)` 与 `56` 加入清单，用探针常量或编辑器补全验证 `Base07`、`Base56` 生成，并对照你默写的 doc 格式检查预测；若运行了 4.2.4 的 `cargo doc`，直接在文档页核对。最后还原 `spacing.rs` 并确认 `cargo check -p ui` 通过。

参考答案（第一部分）：

- `Base04` ← `(2, 4, 6)`，三档 `2|4|6`，doc：`` `2px`|`4px`|`6px (@16px/rem)` - Scales with the user's rem size. ``
- `Base32` ← `32`（Single），三档 `28|32|36`，doc：`` `28px`|`32px`|`36px (@16px/rem)` - Scales with the user's rem size. ``
- `Base16` ← `(14, 16, 18)`，三档 `14|16|18`，doc：`` `14px`|`16px`|`18px (@16px/rem)` - Scales with the user's rem size. ``

## 6. 本讲小结

- 变体名规则一句话：`BaseXX` 的 `XX` = **Default 档像素值**——`Single(n)` 取 \( n \)，`Tuple(a,b,c)` 取中间值 \( b \)；`{:02}` 把个位数补成两位（`Base04`），超过两位不截断（`Base100`）。
- Single 与 Tuple 的三档值来源不同：Single 套公式 \( \max(n-4,0) \mid n \mid n+4 \)，Tuple 直接用 \( a \mid b \mid c \)；两种形态可生成同名变体，也因此可能撞名（宏不查重，重复变体由 rustc 报错）。
- doc 注释与 `spacing_ratio` 分支共享同一批计算：`` `Apx`|`Bpx`|`Cpx (@16px/rem)` `` 按 Compact|Default|Comfortable 排列，`@16px/rem` 标明换算基准，随 rem 设置缩放。
- `#[doc = #doc_string]` 是宏注入文档的唯一途径：`///` 无法出现在 `quote!` 模板的动态位置，属性形式与 `///` 对 rustdoc 完全等价。
- 新增间距值 = 只改 `spacing.rs` 清单这一个数据文件：命名、文档、match 分支全部由宏自动生成；验证可用 `cargo check -p ui` 加探针引用或 `cargo doc`。

## 7. 下一步学习建议

本讲搞定了「数字从哪来、名字怎么起、文档怎么写」——全部是**编译期**的事。下一讲 **u3-l2 密度感知间距：spacing_ratio、rems 与 px** 进入**运行时**：`spacing_ratio` 如何通过 `::theme::theme_settings(cx).ui_density(cx)` 在三档间选择、`rems(cx)` 与 `px(cx)` 的区别（为什么 `px` 用 `ui_font_size` 换算），以及组件里该选哪个。建议提前浏览 [crates/theme/src/ui_density.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs) 里的 `UiDensity` 定义，并对照 [button_like.rs:L797-L801](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button_like.rs#L797-L801) 思考：同一处间距，什么时候 `.rems(cx)`、什么时候 `.px(cx)`？
