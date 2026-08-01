# PicoStr：编译期友好的字符串内化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `PicoStr` 要解决的问题：为什么 Typst 要把大量短字符串（标签名、HTML 标签/属性名）「内化」成一个 8 字节、可 `Copy` 的整数。
- 解释 `PicoStr` 内部同时存在的**三种表示**——bitcode 内联、exceptions 编译期列表、运行期 interner——以及它们的判定优先级和取值范围。
- 手动推演 bitcode 的 5 位编解码过程，并说出它的字符集与长度限制。
- 理解为什么 exceptions 查找要写成 `const fn` 的二分查找，以及运行期 interner 为什么用 `Box::leak` 故意「泄漏」字符串。
- 独立阅读 `src/pico.rs`，并跑通其中的单元测试。

本讲是专家层的第一篇，默认你已经读过 `u2-l6`（哈希体系）。`PicoStr` 把「同一字符串 → 同一整数」当作一种**跨平台确定性的轻量指纹**，与 `LazyHash`/`hash128` 的动机一脉相承，只是它把指纹**直接当成了值本身**。

## 2. 前置知识

- **字符串内化（string interning）**：把重复出现的字符串只存一份，之后用「代表它的句柄」来比较、哈希。句柄相等 ⇔ 字符串相等，比较从「逐字节」降为「整数比较」。标准库没有通用 interner，Typst 在这里自己实现了一个极轻量的版本。
- **`NonZeroU64` 与 niche 优化**：`NonZeroU64` 保证内部值不为 0，编译器据此把 `Option<NonZeroU64>` 压缩成 8 字节（用 0 当 `None` 的标记）。这正是 `PicoStr` 文档里说的「null-optimized」。
- **`const fn` 与 CTFE**：Rust 的 `const fn` 可以在编译期求值（Compile-Time Function Evaluation）。`PicoStr::constant` 利用这一点，让字符串内化发生在编译期，运行时零开销。但 `const` 上下文里没有 `format!`/`String`/`Vec`，所以拼装错误信息要用很原始的手段——这是本讲会看到的一段「别扭」代码的原因。
- **孤儿规则（orphan rule）**：不能为外部类型在外部 crate 实现 trait。本讲里你会看到 `ResolvedPicoStr` 通过 newtype 把 `&str` 的各类 trait 行为重新挂到自己身上。
- **`LazyLock` / `RwLock`**：全局 interner 用 `LazyLock<RwLock<Interner>>` 实现惰性初始化 + 读写锁并发访问。`LazyLock` 在 `u1-l1` 已讲过。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [src/pico.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs) | `PicoStr`、`ResolvedPicoStr`、bitcode 子模块、exceptions 子模块、全局 `INTERNER`，以及单元测试 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 通过 `pub use self::pico::{PicoStr, ResolvedPicoStr};` 把这两个类型作为公共 API 导出 |

为了说明「为什么需要 `PicoStr`」，本讲还会少量引用它的真实使用方：

| 文件 | 作用 |
| --- | --- |
| crates/typst-library/src/foundations/label.rs | `Label(PicoStr)`——文档里的 `<intro>` 标签内化成 `PicoStr` |
| crates/typst-html/src/dom.rs | `HtmlTag(PicoStr)`、`HtmlAttr(PicoStr)`——HTML 标签名与属性名内化成 `PicoStr` |

---

## 4. 核心概念与源码讲解

### 4.1 PicoStr 的三种表示与 MARKER

#### 4.1.1 概念说明

先看动机。Typst 文档模型里到处都是短字符串标识：标签 `<intro>`、`<fig-1>`，HTML 标签 `h1`、`div`，HTML 属性 `aria-disabled`、`contenteditable`。这些字符串有两个共同点：

1. **数量有限、大量重复**，需要频繁比较、做哈希表 key。
2. **是文档模型里流通的「小值」**，理想情况下应当像 `i64` 一样可以随便 `Copy`，而不是像 `String` 那样每次克隆都分配堆内存。

`PicoStr` 就是为此设计的「内化字符串句柄」。它的全部状态只有一个 `NonZeroU64`，所以：

- `Copy` + 8 字节 → `Label(PicoStr)` 也能 `derive(Copy)`，文档树里到处传递零成本。
- 同一字符串必然得到同一 `PicoStr` → 相等和哈希退化为整数运算。
- `Option<PicoStr>` 仍是 8 字节（`NonZeroU64` 的 niche 优化）。

但「字符串→整数」的映射从哪来？`PicoStr` 给出了**三种来源**，巧妙地按「能否在编译期确定」分档，尽量减少运行期分配：

| 表示 | 适用字符串 | 何时确定 | 是否占运行期内存 |
| --- | --- | --- | --- |
| **bitcode 内联** | 长度 ≤ 12，字符集仅 `a-z`/`1-4`/`-` | 编译期或运行期都行 | 否（编码进整数本身） |
| **exceptions 列表** | 一批写死在源码里的常见长串（如 `aria-disabled`） | 编译期 | 否（嵌在二进制只读数据里） |
| **运行期 interner** | 其它一切字符串 | 运行期 | 是（`Box::leak` 泄漏，进程级常驻） |

三种表示共用一个 `u64`，靠**取值范围**互相区分，而不靠 tag 位浪费空间。区分规则的核心是一个标记位 `MARKER`。

#### 4.1.2 核心流程

整个判定的「路由」逻辑可以画成下面这张图（对应 `resolve` 的解码路径）：

```
                 PicoStr 内部的 u64 value
                          │
                 value & MARKER != 0 ?
                  ┌───────┴────────┐
                是(bitcode)         否(编号)
                  │                  │
        decode(value & !MARKER)   index = value - 1
                                  ┌──┴──────────────────┐
                          index < LIST.len() ?          │
                           (exception)          (runtime interner)
                                  │                      │
                          LIST[index]          INTERNER.strings[index - LIST.len()]
```

- **bitcode**：第 63 位（`MARKER`）置 1，真实编码在低 60 位。低 60 位足以容纳 12 × 5 = 60 bit。
- **exception**：值为「在 `LIST` 中的下标 + 1」，范围 `1..=LIST.len()`。
- **runtime**：值从 `LIST.len() + 1` 开始往后排，每新增一个就 +1。

三类取值不重叠：bitcode 一定有高位标记，后两类都是小整数且不含高位标记，且都非零（满足 `NonZeroU64`）。

#### 4.1.3 源码精读

类型定义与文档注释（注意 doc 里就写明了三种 flavor）：

[pico.rs:25-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L25-L40) — `PicoStr(NonZeroU64)`，`derive(Copy, Clone, Eq, PartialEq, Hash)`。文档明确说它「8 字节、可拷贝、null 优化」，并预告了两种编译期内化方式。

标记位定义：

[pico.rs:11-12](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L11-L12) — `const MARKER: u64 = 1 << 63;`。注释「Marks a number as a bitcode encoded PicoStr」点明它只是 bitcode 的「类型标签」。

把三种来源统一成 `u64` 的关键函数是 `try_constant`（4.2、4.3 会细看 bitcode 与 exception 两条分支）：

[pico.rs:99-118](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L99-L118) — 先试 bitcode，成功就 `v | MARKER`；失败再试 exception，成功就用 `i + 1`（`+1` 是为了避开 0，保住 `NonZeroU64` 不变式）。两条路都走不通就返回 `Err`，交给运行期 interner。

反向解码 `resolve` 完整对应 4.1.2 那张图：

[pico.rs:121-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L121-L136) — 高位置位 → bitcode 解码；否则用 `checked_sub(LIST.len())` 一句同时完成「是 exception 还是 runtime」的判断：`Some(runtime)` 走运行期表，`None` 走 exception 表。

最后看一眼「为什么」的现实证据——下游怎么用。`Label` 把 `PicoStr` 当成自己的唯一字段：

[label.rs:49-65](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/label.rs#L49-L65) — `#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)] pub struct Label(PicoStr);`。因为 `PicoStr` 是 `Copy`，`Label` 才能也是 `Copy`；`Label::new` 用 `const EMPTY: PicoStr = PicoStr::constant("")` 在编译期就把「空串」内化好，可见 null 优化让 `Option<Label>` 与 `Label` 一样便宜。

#### 4.1.4 代码实践

实践目标：直观感受「同一字符串 → 同一 `PicoStr`」，以及 `Copy` 的便利。

下面的代码是**示例代码**（项目本身没有 examples 目录，你可以新建一个临时 crate，或在 `crates/typst-utils/tests/` 下临时加一个集成测试来运行）：

```rust
use typst_utils::PicoStr;

fn main() {
    let a = PicoStr::constant("h1");   // 编译期内化
    let b = PicoStr::intern("h1");     // 运行期内化
    assert_eq!(a, b, "编译期与运行期内化的结果必须相等");
    println!("{a:?} == {b:?}");        // Debug 会自动 resolve 成 "h1"

    // 因为 Copy，可以直接按值传来传去，无需克隆
    let copied = a;
    assert_eq!(a, copied);
}
```

操作步骤：

1. 在仓库根目录运行 `cargo test -p typst-utils test_pico_str -- --nocapture`（这条测试断言了上面 `a == b`，见 4.1.5）。
2. 想自己跑 `main`：在 `crates/typst-utils/examples/` 下新建 `pico_demo.rs`（目录不存在就创建），粘贴上面代码，再 `cargo run -p typst-utils --example pico_demo`。

需要观察的现象：两次断言都通过，`{a:?}` 打印出 `h1`（说明 `Debug` 会自动 `resolve`）。**待本地验证**：示例运行取决于工作区的 dev-dependencies 配置，若无法编译示例，直接用单元测试即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PicoStr` 选 `NonZeroU64` 而不是普通 `u64`？

> 答案：为了 niche 优化——`NonZeroU64` 保证非零，编译器把 `Option<PicoStr>` 压成 8 字节。`Label::new` 返回 `Option<Label>`，这个压缩很有价值。

**练习 2**：exception 表示存的是「下标 + 1」，为什么要 `+1`？

> 答案：下标 0 对应的 `u64` 会是 0，违反 `NonZeroU64` 不变式；`+1` 把范围挪到 `1..=N`，既非零又不含 `MARKER` 高位，安全。

---

### 4.2 bitcode：5 位编解码

#### 4.2.1 概念说明

「bitcode」是 `PicoStr` 最有意思的一档：**把字符串直接编码进一个 `u64` 里，完全不分配、不查表、不需要全局状态**。能做到这一点，靠的是对字符集和长度下狠手限制：

- 长度最多 12（`12 × 5 = 60` bit，恰好塞进 `u64` 低 60 位，留下高位给 `MARKER`）。
- 每个字符用 5 bit（32 种可能）编码，可用字符恰好 31 个：`a-z`、`-`、`1-4`。

为什么是这几个字符？因为它们覆盖了最常见的「机器友好的短标识」：HTML 标签 `h1`/`div`、CSS-like 的 kebab-case 名（`abc-def`）。注意 `0` 和 `5-9` **不在**字符集里——这是一个很重要的伏笔（见 4.3.1）。

#### 4.2.2 核心流程

编码把字符串看作一串字节 \(b_0 b_1 \dots b_{L-1}\)（\(L \le 12\)），每个字节通过查表得到 5 位码 \(c_k = \text{ENCODE}[b_k] \in [1,31]\)（0 表示「非法字符」）。最终整数（**第 0 个字符在最低 5 位**）：

\[
\text{num} = \sum_{k=0}^{L-1} c_k \cdot 32^{k}, \qquad c_k \in [1,31]
\]

由于 \(c_k \le 31 < 32\)，最大值严格小于 \(32^{12} = 2^{60}\)，所以 num 占用不超过 60 bit，与 `MARKER`（bit 63）不冲突。

解码就是反复「取低 5 位 → 查 `DECODE` 表 → 整体右移 5 位」，因为第 0 个字符本来就在最低位，所以解码天然按正序还原。

**手工推演 `"hi"`**：`code('h') = 8`（h 是第 8 个字母）、`code('i') = 9`。

\[
\text{num} = 8 \cdot 32^{0} + 9 \cdot 32^{1} = 8 + 288 = 296
\]

解码：`296 % 32 = 8 → 'h'`，`296 / 32 = 9`，`9 % 32 = 9 → 'i'`，得到 `"hi"`。✓

边界：空串 `""` 的 `num = 0`，`0 | MARKER = MARKER`；解码 `MARKER & !MARKER = 0` 时循环不进入，得到长度为 0 的内联串——所以**空串也是 bitcode 可编码的**。

#### 4.2.3 源码精读

`bitcode` 是 `pico.rs` 内部的一个私有 `mod`。核心是两张互逆的表：

[pico.rs:150-162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L150-L162) — `DECODE` 是「码 → 字节」：下标 0 是 `\0`（非法/占位），1–26 是 `a-z`，27 是 `-`，28–31 是 `1-4`。`ENCODE` 是 256 项的「字节 → 码」反查表，用一个 `const` 块在编译期生成（`while i < DECODE.len()` 那段循环就是 CTFE）。注意所有合法码都 ≥ 1，于是 `ENCODE[b] == 0` 可统一表示「这个字节不在字符集里」。

编码函数（`const fn`，可在编译期跑）：

[pico.rs:165-186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L165-L186) — 先挡长度 `> 12`（`Err(TooLong)`）；再**从后往前**遍历字节，对每个字节查 `ENCODE`，若为 0 则 `Err(BadChar)`；否则 `num <<= 5; num |= v`。由于每处理一个新字节都先左移再或到低位，最终「字符串第一个字节」落在最低 5 位，正好对应 4.2.2 的公式。

解码函数：

[pico.rs:189-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L189-L201) — 一个 `[0; 12]` 栈缓冲 + 长度计数。`value & 0b11111` 取低 5 位查 `DECODE` 写入 `buf[len]`，再 `value >>= 5`。结果以 `ResolvedPicoStrInner::Inline(buf, len)` 返回（`Inline` 的细节见 4.4）。

错误类型与可读信息：

[pico.rs:203-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L203-L219) — `EncodingError::{TooLong, BadChar}`，各自配一句静态错误信息，供编译失败时提示。

#### 4.2.4 代码实践

实践目标：亲手验证字符集限制与编码方向。

操作步骤（阅读型实践，无需新建文件）：

1. 打开 [pico.rs:165-186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L165-L186)。
2. 在脑中（或纸面）按 4.2.2 的公式手算 `"abc"` 与 `"h1-4"` 的 `num`：
   - `"abc"`：`code(a)=1, code(b)=2, code(c)=3` → \(1 + 2\cdot32 + 3\cdot32^{2} = 1 + 64 + 3072 = 3137\)。
   - `"h1-4"`：`code(h)=8, code(1)=28, code(-)=27, code(4)=31` → \(8 + 28\cdot32 + 27\cdot1024 + 31\cdot32768\)。
3. 再解释为什么 `"h5"` **不能**用 bitcode 编码（提示：查 `DECODE` 里有没有 `5`）。

需要观察的现象 / 预期结果：`"abc"` 算得 3137；`"h5"` 因 `5` 不在 `DECODE`（`ENCODE['5'] == 0`）会触发 `BadChar`，必须走 exception。这两点都可由单元测试佐证（见 4.2.5 与 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：bitcode 为什么限制长度为 12 而不是 13？

> 答案：每字符 5 bit，12 × 5 = 60 bit，刚好放进 `u64` 低 60 位，留出 bit 60–63（其中 bit 63 是 `MARKER`）。13 × 5 = 65 bit，超过 64 位装不下。

**练习 2**：`ENCODE` 为什么用 256 项数组而不是 `HashMap`？

> 答案：它要在 `const fn` 里构造、在编译期使用；`const` 上下文不支持 `HashMap`（需要堆分配）。定长数组 `[u8; 256]` 是纯栈/静态数据，可 CTFE。

**练习 3**：`DECODE[0] = '\0'`，而 `ENCODE['\0'] = 0`，这会冲突吗？

> 答案：不会。合法字符串里不会出现 `\0` 字节；而 `encode` 把 `ENCODE[b] == 0` 一律视为「非法字符」直接报错，永远不会把 `\0` 当成合法码写进 num。

---

### 4.3 exceptions：编译期列表与二分查找

#### 4.3.1 概念说明

有些常见字符串虽长、字符集又超范围，但 Typst 仍希望在**编译期**就把它们内化好（比如所有 `aria-*` 属性）。bitcode 编不了它们，运行期 interner 又只能在运行时确定——折中方案就是 `exceptions::LIST`：一张**写死在源码里、按字典序排好**的字符串列表，编译期即可通过二分查找定位。

回到 4.2 埋下的伏笔：因为 `5`、`6` 不在 bitcode 字符集里，而 HTML 标题 `h5`、`h6` 又必须能在编译期内化，所以你会在这张表里看到：

[pico.rs:268-269](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L268-L269) — `"h5"`、`"h6"` 作为 exception 出现。这是一个非常具体的「字符集限制 → 必须人工兜底」的例子。

整张表（节选头尾）：

[pico.rs:229-295](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L229-L295) — 注释明确要求 `Must be sorted.`，内容全是 HTML/ARIA/MathML 里的属性名，外加少数 Typst 自己的标识（如 `transparentize`）。

#### 4.3.2 核心流程

由于 `LIST` 恒有序，查找用经典二分：

```
lo = 0, hi = LIST.len()
while lo < hi:
    mid = (lo + hi) / 2
    match strcmp(string, LIST[mid]):
        Less    -> hi = mid
        Greater -> lo = mid + 1
        Equal   -> return Some(mid)
return None
```

两个细节决定了它必须手写、且只能用 `const fn`：

1. **标准库 `str::cmp` / `slice::binary_search` 在 `const fn` 里曾不可用**（且 `binary_search` 的 `usize` 溢出防护与泛型边界对 const 不友好），所以作者手写了一个返回 `Option<usize>` 的二分。
2. **`const` 上下文里也没有 `strcmp`**，得自己写一个逐字节比较的 `strcmp`，连 `min` 都要自己实现（标准库 `std::cmp::min` 当年非 const）。

#### 4.3.3 源码精读

二分查找主体：

[pico.rs:298-310](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L298-L310) — 标准「左闭右开」二分模板，`pub const fn get(string: &str) -> Option<usize>`。返回的下标会被 `try_constant` 转成 `i + 1` 存进 `PicoStr`。

手写的逐字节比较：

[pico.rs:313-336](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L313-L336) — 先比公共前缀，再按「谁更长谁更大」收尾，语义与 `str::cmp`（按字节序）一致。注意它按**字节**比较，对纯 ASCII 的 exceptions 列表来说与字典序等价。

手写的 `min`：

[pico.rs:339-341](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L339-L341) — `const fn min(a, b)`，纯粹是因为当年 `std::cmp::min` 不能在 `const fn` 里调。

保障这张表「永远合法」的三条单元测试：

[pico.rs:489-517](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L489-L517) — `test_exceptions_not_bitcode_encodable`（每个 exception 都不应能被 bitcode 编码，否则它就不该出现在表里，属于浪费）、`test_exceptions_sorted`（保证有序，二分才正确）、`test_exception_find`（每个元素都能被 `get` 查到正确下标）。这三条测试是 exceptions 机制的「不变式守护者」。

#### 4.3.4 代码实践

实践目标：体会「加一个 exception」的完整流程，以及为什么排序很重要。

操作步骤（源码阅读 + 推理，**不要真的去改源码**，以免影响仓库）：

1. 阅读 [pico.rs:489-498](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L489-L498)。假设你要新增 `"aria-live"`（不在表里、且含 `-` 与字母，确实编不进 bitcode——但实际它字符集合法且 ≤12，能不能 bitcode？请判断）。
2. 思考：如果有人往 `LIST` 里乱序插入一项，哪条测试会红？为什么二分会得到错误的下标？
3. 再思考：`"h5"` 既是 exception 又能被 `intern` 调用，`try_constant("h5")` 在 `try_constant` 内部走的是 bitcode 分支还是 exception 分支？（提示：看 [pico.rs:100-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L100-L115) 的顺序）。

需要观察的现象 / 预期结果：`"aria-live"` 由 `a-r-i-a-(-)-l-i-v-e` 共 9 个字符，全部在字符集内且长度 ≤ 12，所以它**其实能** bitcode 编码——按规则它不该进 exception 表（会被 `test_exceptions_not_bitcode_encodable` 拒绝）。乱序项会让 `test_exceptions_sorted` 失败，并使二分返回错误下标、`resolve` 解出错的字符串。`"h5"` 因 `5` 非 bitcode 字符，`bitcode::encode` 先返回 `Err`，才走到 exception 分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 exceptions 的查找用二分，而不是改用编译期的完美哈希？

> 答案：二分实现简单、无依赖、可在纯 `const fn` 里手写；列表只有几十项，\(O(\log n)\) 在编译期足够快。完美哈希会引入额外复杂度与构建步骤，收益不大。

**练习 2**：如果某个字符串同时满足 bitcode 编码条件和出现在 `LIST` 里，`try_constant` 会选哪种？

> 答案：选 bitcode。`try_constant` 先试 bitcode，成功就直接返回（`v | MARKER`），根本不会查 exception。`test_exceptions_not_bitcode_encodable` 正是为了保证 `LIST` 里不会出现这种「浪费」的项。

---

### 4.4 运行期 INTERNER 与 resolve

#### 4.4.1 概念说明

bitcode 和 exceptions 都要求字符串「提前可知」。但用户文档里的标签 `<我的-标签>`、运行期动态生成的 HTML 属性等，编译期根本见不到。这类字符串由**运行期 interner** 兜底：第一次见到就分配一份、泄漏成 `&'static str`、记进全局表，下次再见直接复用。

为什么敢用 `Box::leak` 故意「泄漏」内存？源码注释说得很直白：`PicoStr is only used for strings that aren't created en masse, so it is okay.`——它只用于数量有限的标识符字符串，不会在循环里海量生成，所以进程级常驻是可接受的取舍。一旦内化，`PicoStr` 就成了这个字符串的永久身份证。

#### 4.4.2 核心流程

`intern(s)` 的决策树：

```
intern(s):
  1. try_constant(s) 成功?  -> 直接返回（bitcode 或 exception）
  2. write lock INTERNER:
       seen 里已有 s?        -> 返回已记录的 id
       否则:
         num = LIST.len() + strings.len() + 1
         leaked = Box::leak(s.to_string().into_boxed_str())   // &'static str
         seen.insert(leaked, num)
         strings.push(leaked)
         返回 num
```

注意第 1 步：**运行期也先试编译期那两档**，能不分配就不分配。第 2 步直接拿写锁（注释解释：先读锁查、未命中再写锁复查的双检锁在此处「probably not worth it」，索性一步到位）。

`get(s)` 是 `intern` 的「只读版本」：同样先 `try_constant`，再**只读锁**查 `seen`，未命中返回 `None`，绝不新建条目。它用于「我只想比较，不想为比较而分配」的场景。

`resolve(value)` 在 4.1.2 已画出。补充运行期分支：`checked_sub(LIST.len())` 得到 `Some(runtime)` 时，从 `INTERNER.strings[runtime]` 取回那个 `&'static str`。

#### 4.4.3 源码精读

全局 interner 的定义：

[pico.rs:14-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L14-L23) — `INTERNER: LazyLock<RwLock<Interner>>`，`Interner { seen: FxHashMap<&'static str, PicoStr>, strings: Vec<&'static str> }`。`seen` 做「串→id」反查、`strings` 按 id 顺序存串供 `resolve` 正查。

`intern`：

[pico.rs:43-68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L43-L68) — 先 `try_constant`（4.1.3），再写锁查 `seen`，最后 `Box::leak` 新建。`num = exceptions::LIST.len() + interner.strings.len() + 1` 紧接 exception 编号之后，保证全局唯一。

`get`（只读）：

[pico.rs:70-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L70-L85) — 与 `intern` 同形，但拿 `read()` 锁、返回 `Option`、不新建。

`resolve`：

[pico.rs:120-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L120-L136) — 已在 4.1.3 详述；运行期分支读 `INTERNER`。

`resolve` 返回的不是 `&str`，而是 `ResolvedPicoStr`——一个同时能承载「bitcode 解出的内联字节」和「指向 `&'static str` 的指针」的小类型：

[pico.rs:344-355](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L344-L355) — `ResolvedPicoStrInner::{ Inline([u8; 12], u8), Static(&'static str) }`。`Inline` 持有 bitcode 解码出的 12 字节缓冲和长度；`Static` 直接指向 exception 或 interner 里的静态串。

[pico.rs:357-367](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L357-L367) — `as_str` 对 `Inline` 用 `str::from_utf8_unchecked`。这里**安全的前提**是 `DECODE` 里只有合法 ASCII 单字节，所以拼出的缓冲一定是合法 UTF-8；对 `Static` 直接返回内层 `&'static str`。

`ResolvedPicoStr` 还实现了 `Deref<Target=str>`、`Borrow<str>`、`Eq`/`Ord`/`Hash`（全部基于 `as_str()` 的内容），所以它可以像 `&str` 一样用于格式化、当 `HashMap` 的 key 等：

[pico.rs:381-425](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L381-L425) — 这一组 trait 让「解码结果」用起来和普通字符串引用几乎无差别。

最后是一段「别扭但必要」的代码——编译失败时的错误信息拼装。`constant` 在 `try_constant` 失败时会调 `failed_to_compile_time_intern`，而它必须是个 `const fn`（因为 `constant` 是 `const fn`，CTFE 中触发的 panic 会变成**编译错误**）：

[pico.rs:427-458](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L427-L458) — 因为 `const` 上下文没有 `format!`/`String`，作者用一个 `[u8; 512]` 缓冲 + 内部 `push` 函数手动把前缀、出错的字符串、错误说明、`file!()` 给出的「去哪里加 exception」提示一段段粘起来，最后 `panic!`。这段代码的存在理由就是：**给「在 const 上下文误用了不可内化的字符串」的人一条可操作的报错**——告诉你去 `pico.rs` 加 exception。

#### 4.4.4 代码实践

实践目标：验证运行期 interner 对「无法内联编码」的字符串也能正确工作，并观察 `Debug` 自动 `resolve`。

这是本讲的主实践（对应任务要求）。**示例代码**（同 4.1.4，可放 `examples/pico_demo.rs` 或临时测试）：

```rust
use typst_utils::PicoStr;

fn main() {
    // 1) 编译期 vs 运行期，同串必相等
    let a = PicoStr::constant("h1");
    let b = PicoStr::intern("h1");
    assert_eq!(a, b);

    // 2) 一个 bitcode 编不了、也不在 exceptions 里的字符串
    //    注意 ∆/@/</_/0 都不在 bitcode 字符集，且它不在 exceptions::LIST
    let weird = "∆@<hi-10_";
    let interned = PicoStr::intern(weird);
    let resolved = interned.resolve();
    println!("{}", resolved);            // 走 Display -> "∆@<hi-10_"
    assert_eq!(resolved.as_str(), weird);
}
```

操作步骤：

1. 直接运行现成测试验证整条链路：`cargo test -p typst-utils test_pico_str -- --nocapture`。该测试（[pico.rs:469-487](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L469-L487)）已经包含 `roundtrip("∆@<hi-10_")`，即 `intern` 后 `resolve` 应得回原串。
2. 想看打印：按 4.1.4 在 `examples/` 下加文件后 `cargo run -p typst-utils --example pico_demo`。

需要观察的现象：`println!("{}", resolved)` 输出 `∆@<hi-10_`，证明运行期 interner 把它存进 `INTERNER.strings`、`resolve` 经运行期分支正确取回。**待本地验证**：示例能否编译取决于工作区是否允许 examples；若不行，单元测试是权威来源。

#### 4.4.5 小练习与答案

**练习 1**：`intern` 为什么不先拿读锁查、未命中再升级写锁（双检锁）？

> 答案：源码注释说这么做的话，未命中后还得再拿写锁复查，多一次锁操作「probably not worth it」。直接写锁实现最简单，且 `PicoStr` 字符串量小、竞争低，写锁开销可接受。

**练习 2**：`ResolvedPicoStr::as_str` 对 `Inline` 分支用了 `from_utf8_unchecked`，凭什么安全？

> 答案：`Inline` 的字节全部来自 `DECODE` 表，而表里只有合法的 ASCII 单字节字符（`a-z`/`-`/`1-4`），任意组合都是合法 UTF-8，所以免检是安全的。

**练习 3**：如果两次 `intern` 同一个运行期字符串，会分配两次吗？

> 答案：不会。第一次 `Box::leak` 并写入 `seen`；第二次在写锁里命中 `seen.get(string)`，直接返回已记录的 `id`，不再分配。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「标签注册表」小任务，模拟 `Label` 的真实用法。

**任务**：实现一个函数，它接收一组字符串，把它们都内化成 `PicoStr` 放进 `HashSet`，然后回答若干查询——给定字符串，判断它是否在集合里，并打印出「这个 `PicoStr` 是三种表示中的哪一种」。

**示例代码**（可在临时 crate 或测试里编写）：

```rust
use std::collections::HashSet;
use typst_utils::PicoStr;

/// 判断一个 PicoStr 走的是哪条表示路径（仅用于教学观察）。
fn which_kind(p: PicoStr) -> &'static str {
    // 借用一个内部值的 trick：用 Debug 无法直接拿到 u64，这里改用「来源推断」
    // ——对教学而言，我们改为：重新尝试 try_constant 来推断。
    match typst_utils::PicoStr::try_constant("") {
        _ => "（无法直接读取内部 u64；请阅读 pico.rs::resolve 的判定逻辑）",
    }
}

fn main() {
    let names = ["h1", "aria-disabled", "∆@<hi-10_", "h1", "intro"];
    let mut set: HashSet<PicoStr> = HashSet::new();
    for n in names {
        set.insert(PicoStr::intern(n)); // 重复的 "h1" 因同 id 不会真正重复
    }
    println!("集合大小 = {}", set.len()); // 期望 4（"h1" 去重）

    for q in ["h1", "missing", "∆@<hi-10_"] {
        let p = PicoStr::get(q);
        println!("{q:?} 在集合里? {}", matches!(p, Some(x) if set.contains(&x)));
    }
}
```

进阶要求（思考，可不写码）：

1. 解释为什么 `HashSet<PicoStr>` 比直接用 `HashSet<String>` 更省内存、比较更快。
2. 对照 [pico.rs:121-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L121-L136) 的 `resolve`，分别说出 `"h1"`、`"aria-disabled"`、`"∆@<hi-10_"` 这三个 `PicoStr` 各自会命中哪条分支。
3. 说明 `PicoStr::get` 相对 `PicoStr::intern` 在「查询是否已存在」时为什么更合适。

**预期结果**：集合大小为 4；三个查询分别打印 `true`、`false`、`true`。三个字符串分别命中 bitcode、exception、运行期 interner 分支。**待本地验证**：`which_kind` 的占位实现提示读者——内部 `u64` 不对外暴露，要判断表示种类必须回到 `resolve`/`try_constant` 的源码逻辑，这本身就是一个引导阅读源码的练习。

## 6. 本讲小结

- `PicoStr(NonZeroU64)` 是一个 8 字节、`Copy`、null 优化的「内化字符串句柄」，目的是让标签名、HTML 标签/属性名等短标识能像整数一样便宜地比较、哈希、传递。
- 它内部统一用 `u64` 承载**三种表示**：bitcode（高位 `MARKER` 置位）、exception（`1..=LIST.len()`）、runtime（从 `LIST.len()+1` 起递增），靠取值范围而非 tag 互相区分。
- **bitcode** 用 5 bit/字符把长度 ≤ 12、字符集为 `a-z`/`1-4`/`-` 的字符串直接编码进整数，完全不分配；首字符落在最低 5 位。
- **exceptions** 是一张编译期有序列表，兜底 bitcode 编不了的常见长串（如 `aria-*`、以及因 `5/6` 不在字符集而进表的 `h5`/`h6`），用纯 `const fn` 手写二分查找。
- **运行期 interner** 用全局 `LazyLock<RwLock<_>>` + `Box::leak` 兜底一切其它字符串，先 `try_constant` 再查表，`intern` 会新建、`get` 只读不建。
- `resolve` 把 `PicoStr` 解码成 `ResolvedPicoStr`（内联字节或 `&'static str`），后者实现了一组字符串 trait，用起来与 `&str` 几无差别；编译失败时由手写的 `const fn` 拼出可操作错误信息。

## 7. 下一步学习建议

- **横向对比哈希体系**：回看 `u2-l6` 的 `LazyHash`，思考「`PicoStr` 把指纹当值」与「`LazyHash` 把指纹当判等捷径」两者在前提与风险上的异同。
- **进入 u3-l2**：下一篇讲 `fat.rs` 的胖指针拆解与 `protected.rs` 的访问守卫，那是另一类「用类型系统封装 `unsafe` / 谨慎访问」的低层技巧。
- **继续阅读源码**：
  - [pico.rs:460-518](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/pico.rs#L460-L518) 的完整测试，是理解各种边界（空串、exception、乱序检测）的最佳材料。
  - [label.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/label.rs) 与 [dom.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/dom.rs)，看 `PicoStr` 如何被 `Label`、`HtmlTag`、`HtmlAttr` 当成唯一字段，体会「`Copy` 句柄」在文档模型里的实际收益。
