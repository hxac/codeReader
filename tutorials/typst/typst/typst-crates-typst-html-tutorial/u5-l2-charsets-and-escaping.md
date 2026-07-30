# 字符集约束与 HTML 转义

## 1. 本讲目标

本讲聚焦 typst-html 在「最后一公里」必须回答的一个最细粒度的问题：**单个字符落盘时，哪些可以原样写进 HTML、哪些必须转义、哪些干脆写不进去？**

学完本讲，你应当能够：

1. 说清 `charsets.rs` 中那一组「字符有效性」判定函数各自的职责，以及它们背后的 WHATWG/W3C 字符分类。
2. 看懂 `write_escape` 对 `& < > " '` 五个特殊字符的命名引用处理，以及对其余字符的「数字字符引用 / 不可编码报错」二分。
3. 区分**普通文本**、**属性值**、**raw 文本元素**（`script`/`style`）、**可转义 raw 文本元素**（`textarea`/`title`）四种语境下完全不同的转义规则。
4. 理解 raw 元素为何要做 `find_closing_tag` 防注入校验，并能从源码预测一段文本是否会触发报错。

本讲承接 [u2-l2（HtmlTag 与驻留）](u2-l2-htmltag-interning.md) 中对 `charsets::is_valid_in_tag_name` 的初步认识，并延续 [u5-l1（DOM 到 HTML 字符串的编码）](u5-l1-dom-to-html-encoding.md) 对 `encode.rs` 整体编码流程的讲解，把镜头推进到「单个字符如何落盘」这一最细粒度。

## 2. 前置知识

在进入源码前，先用三段直觉建立认知。

**直觉一：HTML 文本不是「任意字符串」。** HTML 规范把每个 Unicode 码点分成若干类，其中一部分（控制字符、非字符）根本不允许出现在文档里，另一部分（`&`、`<` 等）虽然允许，但有特殊语法含义，必须转义后才不会被浏览器误解。typst-html 在编码时必须替用户把好这道关，否则生成的 HTML 要么无法解析，要么语义被篡改。

**直觉二：转义规则随「语境」而变。** 同一个 `<` 在普通文本里会开启一个标签、必须转义；但在双引号包裹的属性值里却是普通字符、可以原样保留。同一个 `"` 在属性值里会提前截断属性、必须转义；但在普通文本里毫无威胁。因此 typst-html 没有「一刀切」的转义函数，而是为每种语境准备了一套判定规则。

**直觉三：raw 元素是「不转义」的危险区。** `<script>` 和 `<style>` 里的内容浏览器**原样接收、不解析实体**，所以 `&lt;` 不会被还原成 `<`。这带来一个反直觉的安全问题：如果脚本文本里出现了字面的 `</script>`，浏览器会提前结束脚本——这就是为什么 typst-html 要专门扫描「自闭合序列」。

**关键术语：**

| 术语 | 含义 |
|------|------|
| 字符引用（character reference） | HTML 里以 `&` 开头、`;` 结尾的转义序列，如 `&amp;`、`&#x20;` |
| 命名引用 | 有名字的引用，如 `&amp;`、`&lt;` |
| 数字字符引用 | 用码点编号的引用，`&#xNN;` 为十六进制、`&#NN;` 为十进制 |
| C0 / C1 控制字符 | U+0000–U+001F 与 U+007F–U+009F，多数不可打印 |
| 非字符（non-character） | 规范保留、永远不该出现在文本中的码点，如 U+FDD0、U+FFFF |
| WHATWG | 维护现行 HTML 标准的组织，本讲函数大量引用其章节号 |

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是 typst-html 对外「正确性」的最后防线：

| 文件 | 作用 |
|------|------|
| [src/charsets.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs) | 一组纯函数，回答「某字符在某语境下是否合法」；无副作用、大多为 `const fn` |
| [src/encode.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs) | DOM 树 → HTML 字符串的编码器；本讲聚焦其中 `write_text`/`write_escape`/`write_element`/`find_closing_tag` 等「字符级」函数 |

辅助阅读：

- [src/tag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs)：`is_raw`/`is_escapable_raw` 决定一个标签走哪条编码分支。
- [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs)：`HtmlElement.pre_span` 字段标记编译器生成的空白保护 span，影响转义路径。

> 说明：typst-html 的 HTML 报错行为有配套测试覆盖，但这些测试位于本 crate 之外（仓库根的测试套件），本讲沙箱不可见，故下文一律从**源码逻辑**推导行为，不引用具体测试行号；如需对照真实断言，请到仓库的 HTML 测试套件中检索「cannot be encoded」「closing tag」等关键词。

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**字符有效性规则**、**普通文本的逐字符转义**、**属性值的转义路径**、**raw 文本的防注入**。

### 4.1 字符有效性规则：charsets 函数集

#### 4.1.1 概念说明

[charsets.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs) 是一个只有 82 行的小文件，但它定义了 typst-html 全部「字符能否通过」的判据。它的设计有两个鲜明特点：

1. **纯函数 + `const fn`**：所有函数都不依赖运行时状态，且大多标注为 `const fn`，因此能在编译期常量（如 `HtmlTag::constant`，见 u2-l2）和运行期校验（如 `HtmlTag::intern`）中**共用同一套规则**，避免「两套标准」。
2. **按语境分函数**：不写一个万能的 `is_valid(c)`，而是为标签名、属性名、属性值、普通文本各写一个函数，因为每种语境的合法字符集不同。

底层是两个「原子分类函数」：`is_whatwg_non_char`（是否非字符）与 `is_whatwg_control_char`（是否控制字符），其余函数都在它们之上组合。

#### 4.1.2 核心流程

字符有效性判定的依赖关系如下（上层调用下层）：

```
is_valid_in_tag_name        ── 独立白名单（仅 ASCII 字母数字与 '-'）
is_valid_in_attribute_name  ── 黑名单 + 非字符 + 控制字符
is_valid_in_attribute_value ── is_w3c_text_char + 额外禁 '&' '"'
is_valid_in_normal_element_text ── is_w3c_text_char + 额外禁 '&' '<'
        │
        └── is_w3c_text_char ── 非字符? 控制字符?
                                  ├── is_whatwg_non_char
                                  └── is_whatwg_control_char
```

注意两条不同的策略：

- **标签名 / 属性名**用的是「语法分隔符」视角：关心的是字符会不会破坏 HTML **解析结构**（如 `>`、`/`、`=`）。
- **属性值 / 普通文本**用的是「文本字符」视角：以 `is_w3c_text_char` 为底，再叠加该语境特有的危险字符。

非字符的判定用了一个巧妙的位运算。WHATWG 规定每个平面末尾的两个码点（xxFFFE、xxFFFF）都是非字符，即低 16 位等于 `0xFFFE` 或 `0xFFFF`：

\[
\text{non-char}(c) \;\Longleftrightarrow\; (c \mathbin{\&} \texttt{0xFFFE}) = \texttt{0xFFFE} \;\wedge\; c \le \texttt{0x10FFFF}
\]

掩码 `0xFFFE` 的最低位恒为 0，因此它同时匹配 `0xFFFE` 与 `0xFFFF` 两种尾数，一条表达式覆盖整个非字符族。除此之外，`U+FDD0..=U+FDEF` 这一段也被单独列为非字符。

#### 4.1.3 源码精读

先看两个「原子分类函数」：

[is_whatwg_non_char 与 is_whatwg_control_char — charsets.rs:L64-L81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L64-L81)

这段代码刻画了两类「永远不合法」的码点：C0 控制字符（`U+0000–U+001F`）、其余控制字符（`U+007F–U+009F`），以及非字符族。注意 `0x10ffff` 上界对 Rust 的 `char` 其实是冗余的（`char` 本就不超过该值），这里属于防御性写法。

再看文本字符的总判据：

[is_w3c_text_char — charsets.rs:L53-L62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L53-L62)

它的逻辑是「非字符一律拒；控制字符里只放行 ASCII 空白（含 `\t\n\v\f\r` 与空格），其余控制字符拒；其它全放行」。也就是说 `is_w3c_text_char` 是「**可作为文本的字符**」的并集，是属性值与普通文本判定的共同底座。

最后是两个「带语境」的判定：

[is_valid_in_normal_element_text 与 is_valid_in_attribute_value — charsets.rs:L25-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L25-L50)

二者结构完全对称：以 `is_w3c_text_char` 为底，再叠加各自语境的危险字符——普通文本额外禁 `&` 与 `<`（会开启实体或标签），属性值额外禁 `&` 与 `"`（`"` 会截断双引号属性值）。注意代码注释里特别说明：`&` 其实只在「有歧义」时才危险，但精确判定「歧义 & 号」成本太高，索性全部转义。

> 补充：标签名与属性名的判定（`is_valid_in_tag_name`、`is_valid_in_attribute_name`）已在 u2-l2、u2-l3 讲过，本讲不重复，只需记住它们是「语法分隔符」黑/白名单视角，与文本视角不可混用。

#### 4.1.4 代码实践

**实践目标**：用源码规则手工分类一批字符，建立对「四套判据」的肌肉记忆。这是一个**源码阅读型实践**，无需运行。

**操作步骤**：

1. 准备下表，先**不看答案**，根据 4.1.2 的依赖关系逐个推断每个字符在四套规则下的取值（✓ 合法 / ✗ 非法）。

   | 字符 | 码点 | `tag_name` | `attr_name` | `attr_value` | `normal_text` |
   |------|------|-----------|-------------|--------------|---------------|
   | `a` | U+0061 |  |  |  |  |
   | `-` | U+002D |  |  |  |  |
   | `<` | U+003C |  |  |  |  |
   | `&` | U+0026 |  |  |  |  |
   | `"` | U+0022 |  |  |  |  |
   | `\t` | U+0009 |  |  |  |  |
   | `\u{1}` (SOH) | U+0001 |  |  |  |  |
   | `\u{fdd0}` | U+FDD0 |  |  |  |  |
   | `\u{200d}` (ZWJ) | U+200D |  |  |  |  |

2. 推断完后，对照源码逐行核对。提醒两个易错点：`\t` 是控制字符但属于 ASCII 空白，故在 `attr_value`/`normal_text` 下**合法**；`\u{200d}`（零宽连接符）既非非字符也非控制字符，两处文本类判据均**合法**，但 `tag_name` 只认 ASCII 字母数字与 `-`，会拒收它。

**需要观察的现象**：你能解释「为什么 `<` 在普通文本非法、在属性值却合法」——因为二者共用 `is_w3c_text_char` 底座，但叠加了不同的危险字符集。

**预期结果**（参考答案）：

| 字符 | `tag_name` | `attr_name` | `attr_value` | `normal_text` |
|------|-----------|-------------|--------------|---------------|
| `a` | ✓ | ✓ | ✓ | ✓ |
| `-` | ✓ | ✓ | ✓ | ✓ |
| `<` | ✗（非字母数字/`-`） | ✓ | ✓ | ✗（普通文本禁 `<`） |
| `&` | ✗ | ✓ | ✗（属性值禁 `&`） | ✗（普通文本禁 `&`） |
| `"` | ✗ | ✗（黑名单） | ✗（属性值禁 `"`） | ✓ |
| `\t` | ✗ | ✗（控制字符） | ✓（ASCII 空白放行） | ✓ |
| `\u{1}` | ✗ | ✗（控制字符） | ✗ | ✗ |
| `\u{fdd0}` | ✗ | ✗（非字符） | ✗ | ✗ |
| `\u{200d}` | ✗ | ✓ | ✓ | ✓ |

#### 4.1.5 小练习与答案

**练习 1**：`is_valid_in_attribute_name(' ')` 与 `is_valid_in_attribute_name('\u{2028}')`（行分隔符）分别返回什么？为什么？

> **答案**：前者返回 `false`（空格在黑名单里），后者返回 `true`。属性名采用黑名单策略，只禁少数语法分隔符与非字符/控制字符，注释里特意写「_Everything_ else is allowed, including U+2029 paragraph separator」，所以连段落分隔符都放行。

**练习 2**：若把 `is_w3c_text_char` 里的 `c.is_ascii_whitespace()` 条件去掉，会对哪类字符的判定产生影响？

> **答案**：`\t`、`\n`、`\r` 等本是控制字符但属于 ASCII 空白的码点，会从「合法文本」变为「非法」，进而导致它们在普通文本与属性值中被判为不可编码、触发报错。这会破坏正常的换行与制表符输出。

---

### 4.2 普通文本的逐字符转义：write_text 与 write_escape

#### 4.2.1 概念说明

知道了「某字符是否合法」，下一步就是「不合法时怎么办」。`write_text` 负责把一个文本节点逐字符写出，`write_escape` 负责把单个字符翻译成字符引用。二者是「调度」与「执行」的关系。

这里有一个关键的 `escape` 开关：当 `escape = true` 时，**所有字符**（哪怕本来合法）都强制走 `write_escape`。这个开关由 `HtmlElement.pre_span` 字段传入——它是编译器为防止空白被折叠而生成的 `<span style="white-space: pre-wrap">` 元素（详见 u4-l1）。在这种 span 里，空格和制表符会被写成数字字符引用，目的不是给浏览器看（CSS 已经够了），而是**防止 HTML 格式化工具把连续空白压扁**而破坏保护。

#### 4.2.2 核心流程

`write_text` 的判定逻辑（伪代码）：

```
for c in text:
    if escape 或 not is_valid_in_normal_element_text(c):
        write_escape(c)      # 可能成功写出引用，也可能返回「不可编码」错误
    else:
        直接 push c
```

`write_escape` 的分派逻辑（伪代码）：

```
match c:
    '&'  → "&amp;"
    '<'  → "&lt;"
    '>'  → "&gt;"
    '"'  → "&quot;"
    '\'' → "&apos;"
    若 is_w3c_text_char(c) 且 c != '\r':
        → "&#x{十六进制};"     # 数字字符引用
    否则:
        → Err(unencodable(c)) # 不可编码，报错
```

注意三个细节：

1. **命名引用只有五个**：`& < > " '`。其中 `>`、`"`、`'` 在普通文本里其实合法、不会被走到；它们出现在这里，是给 `escape = true`（pre_span）这种「强制转义」场景兜底的。
2. **数字引用用小写十六进制**：格式串为 `&#x{:x};`，例如空格写成 `&#x20;`、制表符写成 `&#x9;`、零宽连接符写成 `&#x200d;`。
3. **`\r` 被排除在数字字符引用之外**：回车符既非命名字符，又被 `c != '\r'` 挡掉，于是落入最后的兜底分支——在强制转义路径下遇到 `\r` 会报「不可编码」。普通文本里 `\r` 合法（属 ASCII 空白），会被直接 push，不走这里。

#### 4.2.3 源码精读

[write_text — encode.rs:L100-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L100-L109)

这段代码做了两件事：逐字符遍历；按 `escape || !is_valid_in_normal_element_text(c)` 决定走转义还是原样输出。注意 `.at(span)` 把 `write_escape` 返回的 `StrResult`（本身无源码位置）绑上文本节点的 `Span`，从而让「不可编码」报错能精确定位到用户源码里的那个字符。

[write_escape 与 unencodable — encode.rs:L368-L388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L368-L388)

`write_escape` 是本讲的「核心翻译表」。`unencodable` 用 `#[cold]` 标注提示编译器这是冷路径（报错很少发生），并用 `c.repr()`（来自 `typst_library::foundations::Repr` trait）把字符格式化成可读形式塞进错误消息。

`escape` 开关的来源——`pre_span` 字段：

[pre_span 字段及其文档注释 — dom.rs:L198-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L198-L205)

文档注释明确说明：这类 span 里的空格与制表符会被写成转义序列，**目的是防格式化工具破坏**，而非影响浏览器渲染。它在编码时的传播路径是 `write_children` 把 `element.pre_span` 作为 `escape_text` 传给 `write_node`，再传给 `write_text`：

[write_node 按 HtmlNode 变体分派 — encode.rs:L89-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L89-L97)

（`write_children` 内部对每个孩子调用 `write_node(w, c, element.pre_span)?`，即把整段子树的转义模式统一交给父元素的 `pre_span` 决定，详见 [encode.rs:L193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L193)。）

#### 4.2.4 代码实践

**实践目标**：预测 `&`、`<` 被转义、而不可编码字符触发报错的输出形态。下面是一段**示例代码**（非项目原有文件），用于演示。

**操作步骤**：

1. 新建 `escape.typ`（示例代码）：

   ```typst
   // 普通文本里的特殊字符
   价格 < 5 元 & 库存充足

   // 一个 WHATWG 非字符
   #"\u{fdd0}"
   ```

2. 编译为 HTML：`typst compile escape.typ escape.html`（`.html` 后缀会自动触发 HTML 导出，见 u1-l3）。

3. 用文本编辑器打开 `escape.html`，定位到正文那一段。

**需要观察的现象**（基于源码推导的预测）：

- `价格 < 5 元 & 库存充足` 中的 `<` 应变成 `&lt;`，`&` 应变成 `&amp;`；文本里若出现 `>` 会原样保留（`is_valid_in_normal_element_text('>')` 为真）。
- 非字符 `\u{fdd0}` 应让编译**报错**，消息形如 `the character "\u{fdd0}" cannot be encoded in HTML`，并指向源码中该字符所在位置（由 `write_escape` 返回 `Err(unencodable(c))`、经 `.at(span)` 定位）。

**预期结果**：转义只发生在不合法字符上；非字符直接让编译失败而非悄悄丢失——这正是 typst-html「不静默吞字符」的设计取向。

> 待本地验证：不同 Typst 版本对源码内联 `\u{fdd0}` 的解析可能有差异；若行为不一致，可改用字符串表达式注入。重点是把预测与源码分支对上，而非记结论。

#### 4.2.5 小练习与答案

**练习 1**：普通文本中出现 `>` 时，输出是 `&gt;` 还是 `>`？为什么？

> **答案**：输出 `>`（原样）。因为 `is_valid_in_normal_element_text('>')` 为真（`is_w3c_text_char('>')` 为真，且 `>` 既不是 `&` 也不是 `<`），`write_text` 直接 push，不进 `write_escape`。`write_escape` 里的 `>` → `&gt;` 分支只在 `escape = true`（pre_span 强制转义）时才会被走到。

**练习 2**：为什么 `write_escape` 要把 `\r` 单独排除在数字字符引用之外？

> **答案**：回车符在 HTML 里有特殊的规范化规则（Newline normalization），浏览器对裸 `\r` 与对字符引用 `&#xd;` 的处理并不等价；为了不产生语义偏差，typst-html 选择在强制转义路径下对 `\r` 报「不可编码」而非悄悄转义。普通文本里的 `\r` 仍合法、原样输出，不会走到这个分支。

---

### 4.3 属性值的转义路径

#### 4.3.1 概念说明

属性值的转义和普通文本**形似而神不同**：同样是「逐字符判定 + 不合法则转义」，但危险字符集不同——属性值要禁的是 `"`（会截断双引号定界）和 `&`，而 `<` 反而是安全的。这条路径不在 `write_text` 里，而是直接写在 `write_element` 输出属性的循环中。

另外，属性值有一个「空值简写」优化：当属性值为空字符串时，写成 `attr` 而非 `attr=""`，二者在 HTML 里等价。

#### 4.3.2 核心流程

```
对每个 (attr, value):
    写出属性名
    若 value 为空:    跳过 '=' 与引号（用简写语法 attr）
    否则:
        写 '=' 与开引号 '"'
        for c in value:
            if is_valid_in_attribute_value(c): 直接 push
            else: write_escape(c)   # & → &amp;，" → &quot;，不可编码则报错
        写闭引号 '"'
```

#### 4.3.3 源码精读

[write_element 中属性值的写出与转义 — encode.rs:L116-L134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L116-L134)

注释 `// <elem attr="">..</elem> 等价于 <elem attr>..</elem>` 解释了空值简写。循环体里用 `is_valid_in_attribute_value(c)` 判定，不合法时复用同一个 `write_escape`（注意它对 `"` 命中 `&quot;` 分支），并用 `.at(element.span)` 把错误绑到元素本身的位置。

注意这里与普通文本的**对照**：`<` 在属性值里 `is_valid_in_attribute_value('<')` 为真（`is_w3c_text_char('<')` 为真），因此原样保留；`"` 则被转义。这就是「同函数、不同语境判据」带来的差异。

#### 4.3.4 代码实践

**实践目标**：验证属性值中 `<` 原样、`"` 与 `&` 被转义。下面是一段**示例代码**。

**操作步骤**：

1. 新建 `attr.typ`（示例代码）：

   ```typst
   #html.elem("a", attrs: (
     "href": "/search?q=a < b & c",
     "title": "say \"hi\"",
   ))[链接]
   ```

2. 编译：`typst compile attr.typ attr.html`。

3. 打开 `attr.html` 查看 `<a>` 标签。

**需要观察的现象**（基于源码推导的预测）：

- `href` 值里的 `<` 应**原样**出现（`a < b`），而 `&` 应变成 `&amp;`。
- `title` 值里的 `"` 应变成 `&quot;`（因为整个属性值用双引号包裹，内部的双引号必须转义）。

**预期结果**：属性值的转义集合是 `{&, "}`（外加不可编码字符报错），与普通文本的 `{&, <}` 不同。如果你在属性里放进 `\u{fdd0}`，同样会触发 `cannot be encoded in HTML` 报错（路径相同，只是绑定到 `element.span`）。

> 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：若把属性值的外层引号从 `"` 换成 `'`（单引号），转义规则需要怎么变？typst-html 目前是怎么做的？

> **答案**：理论上单引号定界的属性值里，`"` 可以原样、而 `'` 需转义。但 typst-html **总是用双引号**包裹属性值（见源码 `w.buf.push('"')`），因此只需转义 `"`，`'` 在属性值里原样保留、无需特殊处理。`write_escape` 里的 `&apos;` 分支主要服务于强制转义场景。

**练习 2**：`html.elem("input", attrs: ("disabled": ""))` 生成的 HTML 是 `disabled="disabled"`、`disabled=""` 还是 `disabled`？

> **答案**：生成 `disabled`（简写）。因为 `value.is_empty()` 为真，代码跳过了 `='""'` 部分，直接进入下一个属性或闭标签。这正是布尔属性的标准简写写法。

---

### 4.4 raw 文本元素的防注入：collect_raw_text、find_closing_tag 与 write_raw

#### 4.4.1 概念说明

`<script>` 与 `<style>` 是 **raw 文本元素**：浏览器对其内容**不做实体解析、原样接收**。这意味着 typst-html 不能、也不该对它们做转义——否则 `&amp;` 会被脚本当成字面六字符。但这也带来两个必须由 typst-html 自己兜底的问题：

1. **内容必须是合法文本**：raw 元素里若混入非字符或控制字符，浏览器也救不了，要在编码期就报错。
2. **内容不能含「自己的闭合标签」**：一旦脚本文本里出现字面的 `</script>`（哪怕大小写不同、后面跟空格或 `>`），浏览器会提前结束脚本，造成注入/截断。这是 raw 元素**唯一无法靠转义解决**的安全问题，必须显式扫描。

与 raw 相对的还有一类**可转义 raw 文本元素** `<textarea>`/`<title>`：它们会解析实体，所以走 `write_text(escape=false)`，即 `&`/`<` 照常转义——因此 textarea 里写 `</textarea>` 是安全的（`<` 会被转成 `&lt;`，不再像闭合标签）。

#### 4.4.2 核心流程

raw 文本的编码分四步：

```
write_element 发现 tag 是 raw (script/style):
  ├─ collect_raw_text(element)
  │    ├─ 遍历孩子：只允许 Text；遇到 Element/Frame → 报错「不能有非文本孩子」
  │    └─ 逐片段检查 is_w3c_text_char；任一字符非法 → 报「不可编码」
  ├─ find_closing_tag(text, tag)
  │    └─ 在文本里搜 `</` + 标签名(忽略大小写) + 尾随空白/`>`/`/`
  │       命中 → 报错「raw 元素不能包含自己的闭合标签」(附 hint)
  ├─ 选 RawMode（pretty 时对 script/style 做缩进/换行策略）
  └─ 原样写出文本（不转义）
```

可转义 raw（textarea/title）则简单得多：直接 `write_text(escape=false)`，按普通文本规则转义 `&`/`<`。

闭合标签的判定遵循 HTML 规范 §13.1.2.6：序列 `</` 之后、紧跟（忽略大小写）标签名、再跟 `\t \n \f \r 空格 > /` 之一，即视为闭合标签。

#### 4.4.3 源码精读

先看 raw 元素的入口分发（在 `write_element` 末尾）：

[write_element 的 raw/escapable-raw/普通三分派 — encode.rs:L154-L167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L154-L167)

`is_raw` 与 `is_escapable_raw` 的定义在 tag.rs，分别只覆盖两组标签：

[is_raw 与 is_escapable_raw — tag.rs:L144-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L144-L152)

可以看到分类极其精确：`is_raw` 只匹配 `script | style`，`is_escapable_raw` 只匹配 `textarea | title`，正是 HTML 规范 §13.1.2 的「raw text / escapable raw text」两类。

raw 文本的收集与校验：

[collect_raw_text 与 walk_raw_text — encode.rs:L258-L286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L258-L286)

`walk_raw_text` 是共用遍历器：把 `Tag` 节点跳过、`Text` 节点交给闭包、遇到 `Element`/`Frame` 直接 `bail!`（报 `HTML raw text element cannot have non-text children`）。`collect_raw_text` 用它做两件事——逐片段用 `is_w3c_text_char` 找首个非法字符（找到即 `Err(unencodable(c))`），否则拼成完整字符串。注意这里用的是 **`is_w3c_text_char`**（最宽松的文本判据），而不是 `is_valid_in_normal_element_text`——因为 raw 元素里 `<`/`&` 本来就是合法的字面字符，只需挡掉非字符与非法控制字符。

防注入核心：

[find_closing_tag — encode.rs:L291-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L291-L301)

它用 `match_indices("</")` 找到每个 `</` 起始位置，然后检查其后是否「忽略大小写等于标签名」且「再后一位是 `\t \n \f \r 空格 > /` 之一」。`eq_ignore_ascii_case` 解释了为什么 `</SCRiPT` 也会被命中。命中时返回截取的 `&text[i..i + 2 + len]`（即 `</` 加标签名，保留用户输入的原始大小写），供错误 hint 展示。

报错与原样输出：

[write_raw — encode.rs:L217-L250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L217-L250)

`find_closing_tag` 命中即 `bail!` 并附 hint（展示命中的闭合序列）；否则按 `RawMode`（Keep/Wrap/Indent）原样写出，**全程不做字符转义**。注意 script 的 `RawMode::of` 有个微妙分支：若文本含反引号（模板字符串可能跨行），只 Wrap 不 Indent，以免改写 JS 语义。

可转义 raw 的处理只有一行，复用 `write_text`：

[write_escapable_raw — encode.rs:L253-L255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L253-L255)

`escape=false` 表示「不强制全部转义」，只按普通文本规则处理 `&`/`<`。它内部也先经过 `walk_raw_text`（由 `write_text` 间接调用？不——`write_escapable_raw` 直接调 `walk_raw_text` 把每个文本片段交给 `write_text`），因此可转义 raw 同样**不允许非文本孩子**，遇到 `Element`/`Frame` 也会报 `cannot have non-text children`。

> 行为小结（从源码推导，非测试引用）：普通文本或可转义 raw 里出现非字符 → `cannot be encoded in HTML`；raw 元素里出现自身闭合序列 → `HTML raw text element cannot contain its own closing tag`（hint 指出命中的 `</tag`）；raw/可转义 raw 里塞入非文本孩子 → `HTML raw text element cannot have non-text children`。注意：`html.script` 这类类型化函数的 body 类型本身就是 `Str`（见 u2-l5），传错类型通常在 `construct` 阶段就以类型错误拦下，未必走到编码期的 `walk_raw_text`。

#### 4.4.4 代码实践

**实践目标**：对比 raw 与可转义 raw 对「自身闭合标签」的截然不同处理。下面是一段**示例代码**。

**操作步骤**：

1. 新建 `raw.typ`，分别写两个用例（建议**分开编译**，因为第一个会报错）：

   ```typst
   // 用例 A：script 里出现了 </script> —— 应当报错
   #html.script("console.log('</script>')")

   // 用例 B：textarea 里出现了 </textarea> —— 应当通过（被转义）
   #html.textarea("粘贴 </textarea> 到这里")
   ```

2. 先只保留用例 A，编译：`typst compile raw.typ raw.html`，观察报错与 hint。
3. 注释掉 A、只保留用例 B，重新编译，打开 `raw.html` 查看。

**需要观察的现象**（基于源码推导的预测）：

- 用例 A 报错：`HTML raw text element cannot contain its own closing tag`，hint 形如 `the sequence '</script' appears in the raw text`。
- 用例 B 成功，输出的 textarea 内容里 `<` 变成了 `&lt;`，因此浏览器不会把它误判为闭合标签。

**预期结果**：raw 元素靠「拒绝闭合序列」保安全，可转义 raw 元素靠「转义 `<`」保安全——两条不同的路通往同一个目的（不让内容意外终止元素）。

> 待本地验证：hint 里展示的标签名大小写是否与输入完全一致（`</SCRiPT` vs `</script>`）。从源码看，`find_closing_tag` 返回的是 `&text[i..i+2+len]`，截取自**原始文本片段**，故应保留用户输入的大小写。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `find_closing_tag` 在比较标签名时用 `eq_ignore_ascii_case`，而尾随字符判定却用精确匹配？

> **答案**：HTML 标签名本身是大小写不敏感的（`</SCRIPT>`、`</script>` 都能闭合 `<script>`），所以名称比较必须忽略大小写；而尾随的「空白或 `>`/`/`」是具体字符集，没有大小写问题，用精确匹配即可。混用这两种策略正好贴合规范 §13.1.2.6。

**练习 2**：为什么 `collect_raw_text` 用 `is_w3c_text_char` 校验，而不是更严格的 `is_valid_in_normal_element_text`？

> **答案**：raw 元素的内容浏览器原样接收、不解析实体，因此 `<` 和 `&` 在其中是完全合法的字面字符，不能按普通文本规则转义或拒绝。`is_w3c_text_char` 恰好只挡掉「任何语境都不该出现」的非字符与非法控制字符，是 raw 元素能用的最宽（也最合适）的判据。

---

## 5. 综合实践

把四个模块串起来，做一个「预测—验证」的综合练习。

**任务**：下面这份 `final.typ`（**示例代码**）混合了普通文本、属性值、raw 元素和可转义 raw 元素，每处都埋了「陷阱字符」。请**先填好预测表**，再分块编译验证。

```typst
#html.elem("p", attrs: ("data-q": "a < b & c"))[
  单价 < 10 元 & 数量 > 0
]

#html.script("if (a < b && c > d) { /* </script> */ }")

#html.textarea("备注: < 与 & 与 \" 三种")

#"\u{fdd0}"  #"A\tB"
```

**预测表（先填，再编译对照）**：

| 位置 | 你预测的结果（转义？原样？报错？） | 实际结果 |
|------|-----------------------------------|----------|
| `data-q` 属性值里的 `<` |  |  |
| `data-q` 属性值里的 `&` |  |  |
| `<p>` 正文里的 `<` |  |  |
| `<p>` 正文里的 `&` |  |  |
| `<p>` 正文里的 `>` |  |  |
| `#html.script(...)` 整行 |  |  |
| `textarea` 里的 `<` |  |  |
| `textarea` 里的 `&` |  |  |
| `textarea` 里的 `"` |  |  |
| `\u{fdd0}` |  |  |
| `"A\tB"` 中的 `\t` |  |  |

**参考答案要点**（从源码推导）：

- 属性值 `<` 原样、`&` 转 `&amp;`；正文 `<` 转 `&lt;`、`&` 转 `&amp;`、`>` 原样（`>` 在普通文本合法）。
- `#html.script(...)` 整行**报错**（含 `</script>`，命中 `find_closing_tag`，hint 指出 `</script`）。建议把这行注释掉再编译其余部分。
- textarea 里 `<` 转 `&lt;`、`&` 转 `&amp;`、`"` **原样**（可转义 raw 只按普通文本规则转义 `&`/`<`）。
- `\u{fdd0}` **报错**（非字符不可编码）。
- `"A\tB"` 经空白保护后会落在 pre_span 里（详见 u4-l1），制表符被写成 `&#x9;`；若未被包进 pre_span 则原样输出。

**验证方式**：由于 script 行与非字符行会触发报错，建议**逐块启用**——每启用一块就编译一次，把实际输出/报错填进表格右列，与左列预测对照。重点不是记结论，而是确认你能从 `charsets` 判据与 `write_escape` 分派推导出每一格。

> 待本地验证。

## 6. 本讲小结

- `charsets.rs` 用一组「按语境分」的纯 `const fn` 给出字符合法性判据：标签名/属性名走语法分隔符视角，属性值/普通文本以 `is_w3c_text_char` 为共同底座再叠加各自危险字符。
- 两类码点「永远不合法」：WHATWG 非字符（位运算 `(c & 0xFFFE) == 0xFFFE` 匹配 xxFFFE/xxFFFF，外加 `U+FDD0..=U+FDEF`）与控制字符（仅放行其中的 ASCII 空白）。
- `write_escape` 是核心翻译表：五个命名引用 `& < > " '`、其余合法文本字符走小写十六进制数字引用 `&#x{:hex:};`、真正不可编码的字符返回 `Err(unencodable)` 并经 `.at(span)` 定位到源码。
- 普通文本转义 `{&, <}`，属性值转义 `{&, "}`——同一张翻译表、不同语境判据；`escape = true`（pre_span）会强制全部字符走转义，把空格/制表符写成数字引用以防格式化工具破坏空白保护。
- raw 元素（`script`/`style`）**不转义**，但靠 `collect_raw_text`（用 `is_w3c_text_char`）拒绝非法字符、靠 `find_closing_tag` 拒绝「自身闭合序列」保安全；可转义 raw（`textarea`/`title`）复用普通文本转义，故其内部出现闭合标签是安全的。
- typst-html 的取向是「不静默吞字符」：宁可报错让用户知道，也不产出错误或被篡改的 HTML。

## 7. 下一步学习建议

- 阅读 [u5-l3（HTML 内省器 HtmlIntrospector）](u5-l3-html-introspector.md)，看 DOM 在被编码前如何被建立索引——本讲的 `Span` 定位与内省的位置编码一脉相承。
- 回到 [u5-l1](u5-l1-dom-to-html-encoding.md)，把本讲的字符级细节放回 `write_element`/`write_children` 的整体编码流程中，你会对「pretty 打印与转义如何互不干扰」有更完整的认识。
- 进阶可阅读 HTML 规范 §13.1.2（语法）与 §13.1.2.6（闭合标签判定），对照 `charsets.rs` 与 `find_closing_tag` 的实现，体会「规范条文 → Rust 代码」的逐句翻译。
