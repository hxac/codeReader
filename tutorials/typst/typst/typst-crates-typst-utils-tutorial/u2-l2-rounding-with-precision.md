# 高精度数值舍入 round.rs

## 1. 本讲目标

本讲带你吃透 `typst-utils` 里一个不到 120 行却「边界条件极多」的小函数模块 `src/round.rs`。学完后你应该能够：

- 说清 `round_with_precision(value, precision)` 在 **正精度**（保留小数位）和 **负精度**（保留整数位）两种语义下分别怎么算。
- 看懂函数为何要在入口处做一连串「无效舍入短路」，以及这些守卫各自防的是什么。
- 解释 `round_int_with_precision` 为什么返回 `Option<i64>`，以及在 `i64::MAX` 上以 `-1` 精度舍入时为何返回 `None`。
- 理解为什么这里要用 `libm::exp10` 而不是标准库的浮点运算，它和上一讲 `Scalar` 的「跨平台确定性」目标如何呼应。

本讲是上一讲 [u2-l1 Scalar：可哈希可排序的确定性浮点](u2-l1-scalar-deterministic-float.md) 的延续：那一讲解决了「浮点能不能当集合键」的问题，本讲解决「浮点怎么按指定精度安全舍入」的问题，二者都把「确定性」放在第一位。

## 2. 前置知识

阅读本讲前，你需要了解：

- **Rust 的 `f64::round()`**：把浮点数舍入到最近的整数，采用「半数远离零」（half away from zero）规则——也就是 `0.5 → 1.0`、`-0.5 → -1.0`。这是本讲所有舍入判断的「底层原语」。
- **`i64` 整数除法 `/` 与取模 `%`**：在 Rust 中都向零截断（truncate toward zero），例如 `-154 / 10 == -15`、`-15 % 10 == -5`。这一点对理解 `round_int_with_precision` 至关重要。
- **`Option` 与 `?` 运算符**：函数中多处用 `checked_xxx().?` 在溢出时提前返回 `None`。
- **浮点常量**：`f64::MANTISSA_DIGITS`（=53，有效二进制位数）、`f64::DIGITS`（=15，约等于有效十进制位数）、`f64::MAX_10_EXP`（=308）。
- 上一讲提到的 **跨平台确定性** 概念：同一份输入在 32 位 / 64 位 / 不同操作系统上要产生逐位相同的结果。

一个直觉：所谓「按精度 n 舍入」，本质就是「先把小数点搬到要保留的那一位之后，调用一次 `round()`，再把小数点搬回去」。本讲两个函数就是把这句直觉翻译成代码，并堵住所有会溢出 / 产生 `inf` / 产生 `NaN` 的口子。

## 3. 本讲源码地图

本讲只涉及一个源文件，外加 `lib.rs` 里的导出行：

| 文件 | 作用 |
| --- | --- |
| [src/round.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs) | 全部实现：两个公开函数 `round_with_precision` / `round_int_with_precision`，以及一组覆盖正负精度、无穷、NaN、最大最小值的测试。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | `mod round;` 声明私有模块，再 `pub use` 把两个函数重新导出为 crate 顶层 API。 |

模块声明与导出在 [src/lib.rs:L14](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L14) 与 [src/lib.rs:L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L26)，是 u1-l1 讲过的「私有 `mod` + 选择性 `pub use`」公开 API 模式。用户实际调用时写的是 `typst_utils::round_with_precision(...)`，而不是 `typst_utils::round::round_with_precision(...)`。

`libm` 依赖登记在 [Cargo.toml:L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L25)（`libm = { workspace = true }`）。

## 4. 核心概念与源码讲解

### 4.1 round_with_precision 的正负精度语义

#### 4.1.1 概念说明

`round_with_precision(value: f64, precision: i16) -> f64` 的目标是：把 `value` 舍入到「某一位」，而那一位由 `precision` 指定。

- **precision > 0（正精度）**：保留 `precision` 位小数。例如 `precision = 2` 表示「保留 2 位小数」，和日常生活中「保留两位小数」一致。
- **precision == 0**：舍入到整数。
- **precision < 0（负精度）**：把小数点**左边**的若干位也清零，保留到「十位 / 百位 / 千位…」。例如 `precision = -3` 表示「保留到千位」。

两种情况统一遵循「半数远离零」规则：被丢掉的最高位 ≥ 5 就进位（远离零），否则舍去。

#### 4.1.2 核心流程

把上面那句直觉写成公式。设 \( o = 10^{|precision|} \)：

- 正精度（\( p > 0 \)）：

\[
\text{result} = \frac{\text{round}(value \cdot o)}{o}, \qquad o = 10^{p}
\]

- 负精度（\( p < 0 \)，令 \( n = -p > 0 \)）：

\[
\text{result} = \text{round}\!\left(\frac{value}{o}\right) \cdot o, \qquad o = 10^{n} = 10^{-p}
\]

用例子验证：

- `round_with_precision(-0.56553, 2)`：\( o = 10^2 = 100 \)，\( -0.56553 \times 100 = -56.553 \)，`round` 得 \( -57 \)，再 \( /100 = -0.57 \)。✓
- `round_with_precision(823543.0, -3)`：\( o = 10^3 = 1000 \)，\( 823543 / 1000 = 823.543 \)，`round` 得 \( 824 \)，再 \( \times 1000 = 824000 \)。✓

> 为什么负精度要用「除」而不是「乘一个负指数」？因为 `f64::MAX_10_EXP = 308` 的绝对值比 `f64::MIN_10_EXP = -307` 大一点点。除法用的是正指数 \( 10^{n} \)，能让 \( n = 308 \) 这个精度也用上；如果用乘法需要 \( 10^{-308} \) 这种负指数，反而会受限。源码注释里专门解释了这一点。

#### 4.1.3 源码精读

两个分支的实现非常短，关键是 [src/round.rs:L48-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L48-L58)：

```rust
if precision > 0 {
    let offset = libm::exp10(precision.into());
    assert!((value * offset).is_finite(), "{value} * {offset} is not finite!");
    (value * offset).round() / offset
} else {
    // 负精度：用除法，理由见正文
    let offset = libm::exp10(-f64::from(precision));
    (value / offset).round() * offset
}
```

注意几个要点：

1. `libm::exp10(x)` 计算 \( 10^x \)，返回 `f64`。关于为何用它而非别的，见 4.4 节。
2. 正精度分支里那行 `assert!` 是一道「双保险」：在 4.2 节的短路守卫保护下，理论上 `value * offset` 永远不会变成 `inf`，这行 assert 只会在守卫被改坏时触发，帮开发者立刻定位问题。
3. 正负两个分支结构对称：都是「搬小数点 → `round()` → 搬回去」，只是搬运方向（乘 / 除）和 offset 取指数的方式不同。

函数的完整签名和文档注释见 [src/round.rs:L26-L59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L26-L59)，文档里直接给了和上面一样的两个例子，可作为权威参考。

#### 4.1.4 代码实践

**目标**：亲手验证正负精度两条公式。

**步骤**：

1. 在一个依赖了 `typst-utils` 的临时项目里（参考 u1-l1 的方式添加依赖），写一段小程序：

```rust
// 示例代码：用户自建工程中运行
use typst_utils::round_with_precision;

fn main() {
    // 正精度：保留 2 位小数
    assert_eq!(round_with_precision(-0.56553, 2), -0.57);
    // 负精度：保留到千位
    assert_eq!(round_with_precision(823543.0, -3), 824000.0);
    // 半数远离零
    assert_eq!(round_with_precision(1245.232, -1), 1250.0);
    assert_eq!(round_with_precision(-1245.232, -1), -1250.0);
    println!("全部断言通过");
}
```

2. `cargo run` 运行。

**需要观察的现象**：四条断言全部通过，打印「全部断言通过」。

**预期结果**：无 panic，程序正常退出。如果你把 `-0.56553, 2` 改成 `-0.56553, 3`，应得到 `-0.566`；改成 `-1` 应得到 `-0.0`（向零舍入的负零，符号被保留）。

> 本地若未配置依赖，运行结果为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：计算 `round_with_precision(0.99553, 2)` 的结果，并说明进位方向。

**答案**：\( o = 100 \)，\( 0.99553 \times 100 = 99.553 \)，`round` 得 \( 100 \)，\( /100 = 1.0 \)。第三位小数是 5，按「远离零」向上进位，最终跨过整数位变成 `1.0`。源码测试在 [src/round.rs:L162-L163](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L162-L163) 用 `0.99553` 和 `-0.99553` 同时验证了这一点。

**练习 2**：`round_with_precision(0.4, 2)` 的结果是多少？为什么不是 `0.40`？

**答案**：结果是 `0.4`。`f64` 是数值而非字符串，没有「末尾补零」的概念；`0.4` 和 `0.40` 作为 `f64` 是同一个比特模式。补零是**格式化显示**时才需要关心的事，与舍入函数无关。

---

### 4.2 边界与无效输入短路

#### 4.2.1 概念说明

如果直接套用 4.1 的公式，下面这些输入会出问题：

- `value` 是 `∞` / `-∞` / `NaN`：乘除之后还是特殊值，`round()` 对它们无意义。
- `value` 很大、`precision` 也很大：`value * offset` 可能溢出成 `inf`，破坏结果。
- `precision` 是一个极其负的数：理论上结果应该是 0，但要小心保留正负零符号。

`round_with_precision` 的设计哲学是：**「舍入后结果不变的输入，就原样返回」**——既省一次运算，又避免触发上面的坑。它用一连串 `||` 条件把这些「无效 / 无意义」的情况一次性挡在门外，称为「短路」（short-circuit）。

#### 4.2.2 核心流程

入口的判断顺序（伪代码）：

```
if value 是无穷 或 NaN                    → 原样返回 value
if precision >= 0 且 |value| >= 2^53      → 原样返回 value（已是整数，舍不掉小数）
if precision >= 15                        → 原样返回 value（f64 表示不出这么多有效位）
if precision < -308                       → 返回 value * 0.0（结果是 ±0.0，保留符号）
否则                                       → 进入 4.1 的正常计算
```

几个常量为何这样取？

- `2^53 = f64::MANTISSA_DIGITS 位`：任何 \( |value| \ge 2^{53} \) 的 `f64` 的小数部分必然为 0（它已经把全部 53 位精度用在了整数部分），所以正精度舍入是 no-op。
- `f64::DIGITS = 15`：`f64` 最多只有约 15 位有效十进制精度，要求保留 ≥ 15 位小数毫无意义。
- `-f64::MAX_10_EXP = -308`：精度比这更负时，`value / 10^308` 必然趋向 0，结果是 0；但代码特意写成 `value * 0.0` 而不是直接 `0.0`，目的是**保留正负零的符号**（`-1.2 * 0.0 == -0.0`）。

#### 4.2.3 源码精读

短路守卫集中在 [src/round.rs:L36-L47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L36-L47)：

```rust
if value.is_infinite()
    || value.is_nan()
    || precision >= 0 && value.abs() >= (1_i64 << f64::MANTISSA_DIGITS) as f64
    || precision >= f64::DIGITS as i16
{
    return value;
}
if precision < -(f64::MAX_10_EXP as i16) {
    // Multiply by zero to ensure sign is kept.
    return value * 0.0;
}
```

代码上方的注释（[src/round.rs:L27-L35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L27-L35)）特别指出一个精妙的连带效果：因为 `2^53 * 10^15 ≈ 9e30`，远远小于 `f64::MAX ≈ 1.8e308`，所以守卫一旦放行，后面的 `value * offset` 就**必然不会溢出成 inf**——这正是 4.1 里那行 `assert!` 永远不该触发的原因。

文档里还提醒了负精度的一个固有行为（[src/round.rs:L12-L14](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L12-L14)）：舍回去时如果 `result` 超出 `f64` 范围，会返回 `±∞`，这是「可接受的、有文档的」结果，不是 bug。

#### 4.2.4 代码实践

**目标**：用源码自带的模糊测试（fuzzy test）理解边界行为，不自己造数据。

**步骤**：

1. 打开 [src/round.rs:L192-L265](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L192-L265)，阅读 `test_round_with_precision_fuzzy` 和 `test_round_with_precision_fuzzy_negative`。
2. 在 `crates/typst-utils` 目录下运行测试：

```bash
cargo test -p typst-utils --lib round::tests::test_round_with_precision_fuzzy
cargo test -p typst-utils --lib round::tests::test_round_with_precision_fuzzy_negative
```

**需要观察的现象**：关注这两条断言（[src/round.rs:L228-L229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L228-L229)）：

```rust
assert_eq!(rp(f64::MAX, -max_digits), f64::INFINITY);
assert_eq!(rp(f64::MIN, -max_digits), f64::NEG_INFINITY);
```

以及（[src/round.rs:L238-L239](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L238-L239)）：

```rust
assert_eq!(rp(f64::MAX, -max_up), 0.0);
assert_eq!(rp(f64::MIN, -max_up), -0.0);
```

**预期结果**：测试通过。这说明：精度刚好 `-308` 时 `f64::MAX` 被放大到 `+∞`；精度更负一档（`-309`）时则被压成 `0.0` / `-0.0`（符号被保留）。若环境无法编译，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`round_with_precision(f64::INFINITY, 2)` 返回什么？走的是哪条分支？

**答案**：返回 `f64::INFINITY`。命中第一个短路条件 `value.is_infinite()`，直接原样返回，根本不会进入乘除计算。测试见 [src/round.rs:L198-L199](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L198-L199)。

**练习 2**：为什么短路条件里要用 `precision >= 0 && value.abs() >= 2^53`，而**不**对负精度也加 `value.abs() >= 2^53` 这个判断？

**答案**：负精度的舍入目标是「整数的高位」，即使 `value` 本身是个超大整数（小数部分本就为 0），它仍然可能需要被舍入到十位 / 百位（例如把 `823543.0` 舍入到千位）。所以「`|value| >= 2^53` 就 no-op」这个结论只对**正精度**成立，不能套用到负精度。

---

### 4.3 round_int_with_precision 与溢出处理

#### 4.3.1 概念说明

`round_int_with_precision(value: i64, precision: i16) -> Option<i64>` 是给**整数**用的版本。它的语义是：

- `precision >= 0`：直接原样返回（整数没有小数位可舍）。
- `precision < 0`：把整数舍入到「十位 / 百位…」，规则同样是「半数远离零」。

返回值是 `Option<i64>` 而不是 `i64`，因为**整数舍入可能溢出**：把 `i64::MAX`（以 7 结尾）按 `-1` 精度往上进位，会得到一个比 `i64::MAX` 还大的数，这就装不下了。函数用 `None` 表示「这次舍入会溢出，我拒绝给出错误答案」。

#### 4.3.2 核心流程

算法分四步（设 `n = -precision`，即要清掉的位数）：

```
1. ten_to_digits = 10^(n-1)          // 用 checked_pow，溢出则结果视为 0
2. truncated = value / ten_to_digits // 先砍掉 n-1 位，留 1 位做进位判断
3. 看 truncated 的最后一位：
     若 |最后一位| >= 5  → 远离零进位（可能溢出 → checked_add，失败返回 None）
     否则                → 直接把最后一位清零
4. result = rounded * ten_to_digits   // 乘回去（可能溢出 → checked_mul，失败返回 None）
```

关键技巧在第 2 步：它除以的是 `10^(n-1)` 而不是 `10^n`，故意多保留 1 位——这一位就是用来判断「该不该进位」的。第 3 步用 `% 10` 取出它。

用 `i64::MAX` 在 `-1` 精度上走一遍（这是本讲规格要求解释的例子）：

- `n = 1`，`ten_to_digits = 10^0 = 1`
- `truncated = i64::MAX / 1 = i64::MAX`（以 `7` 结尾）
- `truncated % 10 = 7`，`|7| >= 5`，需要进位
- 进位计算：`truncated + signum * (10 - 7) = i64::MAX + 1*3 = i64::MAX + 3` → **溢出**
- `checked_add` 返回 `None`，`?` 让整个函数返回 `None`

所以「为何溢出返回 `None`」的答案有两层：(1) `i64::MAX` 以 7 结尾，按远离零规则本应向上进位到下一个 10 的倍数；(2) 而那个倍数 `...810` 超出了 `i64` 能表示的范围，`checked_add` 安全地探测到溢出并返回 `None`，避免静默给出错误结果。

#### 4.3.3 源码精读

函数主体在 [src/round.rs:L82-L117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L82-L117)。几个关键点：

正精度直接 no-op（[src/round.rs:L83-L85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L83-L85)）：

```rust
if precision >= 0 {
    return Some(value);
}
```

`checked_pow` 溢出兜底（[src/round.rs:L88-L91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L88-L91)）——位数大得离谱时直接返回 `Some(0)`：

```rust
let Some(ten_to_digits) = 10_i64.checked_pow(digits - 1) else {
    return Some(0);
};
```

进位分支里的 `?` 就是溢出逃逸口（[src/round.rs:L102-L111](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L102-L111)）：

```rust
let rounded = if (truncated % 10).abs() >= 5 {
    // Round away from zero ... 此处对 MAX/MIN 配 -1 可能溢出
    truncated.checked_add(truncated.signum() * (10 - (truncated % 10).abs()))?
} else {
    truncated - (truncated % 10)
};
rounded.checked_mul(ten_to_digits)   // ← 乘回去时也可能溢出
```

注意末尾的 `rounded.checked_mul(ten_to_digits)` **没有** `?`，因为它直接是函数的最后一个表达式，`Option` 就是返回值。源码测试 `test_round_int_with_precision_negative_1` 在 [src/round.rs:L286-L287](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L286-L287) 明确断言了 `round(i64::MAX)` 和 `round(i64::MIN)` 在 `-1` 精度下都返回 `None`。

#### 4.3.4 代码实践

**目标**：本讲规格指定的验证任务——确认 `i64::MAX` 在 `-1` 精度下返回 `None`，并对比 `-2` 精度下能正常返回。

**步骤**：

```rust
// 示例代码
use typst_utils::round_int_with_precision;

fn main() {
    // 以 7 结尾，向上进位会溢出
    assert_eq!(round_int_with_precision(i64::MAX, -1), None);
    // 同理，i64::MIN 以 8 结尾，向下（远离零）进位也溢出
    assert_eq!(round_int_with_precision(i64::MIN, -1), None);

    // 但 -2 精度下不会溢出（见练习）
    assert_eq!(round_int_with_precision(i64::MAX, -2), Some(i64::MAX - 7));

    // 普通常规用例
    assert_eq!(round_int_with_precision(-154, -2), Some(-200));
    assert_eq!(round_int_with_precision(823543, -3), Some(824000));
    println!("整数舍入断言通过");
}
```

**需要观察的现象**：所有断言通过。重点体会 `i64::MAX` 在 `-1` 和 `-2` 两种精度下的差异——同一输入，精度不同，一个溢出一个不溢出。

**预期结果**：打印「整数舍入断言通过」。结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `round_int_with_precision(i64::MAX, -2)` 返回的是 `Some(i64::MAX - 7)` 而不是 `None`？

**答案**：`-2` 精度下 `ten_to_digits = 10`，`truncated = i64::MAX / 10 = 922337203685477580`，末位是 `0`，`|0| < 5` 走「清零」分支，`rounded = truncated - 0` 不需要 `checked_add`；最后 `rounded * 10 = 9223372036854775800`，恰好等于 `i64::MAX - 7`，不溢出。测试见 [src/round.rs:L301-L302](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L301-L302)。

**练习 2**：手算 `round_int_with_precision(-154, -2)` 的全过程。

**答案**：`n=2`，`ten_to_digits = 10`；`truncated = -154 / 10 = -15`；`-15 % 10 = -5`，`|-5| >= 5` 走进位分支；`checked_add(signum * (10 - 5)) = -15 + (-1)*5 = -20`；`-20 * 10 = -200`，返回 `Some(-200)`。测试见 [src/round.rs:L297-L298](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L297-L298)（用 `-1245` 验证了同类进位）。

---

### 4.4 libm::exp10 与跨平台确定性

#### 4.4.1 概念说明

回头看 4.1 的公式，核心运算是「乘以 / 除以 \( 10^{k} \)」，所以需要一个算 \( 10^k \) 的函数。标准库并没有 `f64::exp10`，常见的替代有：

- `10f64.powi(k)`：用标准库的 `powi`，但它内部依赖平台的浮点指令 / 数学库。
- `libm::exp10(k)`：来自 `libm` crate，是**纯 Rust** 实现的数学函数。

`typst-utils` 选了后者。原因和上一讲 `Scalar::powi` 选用 LLVM 算法如出一辙：**Typst 要求编译结果在所有平台上逐位确定**。标准库的浮点运算最终会落到操作系统的系统数学库（glibc / musl / macOS libm / Windows），这些实现对 `pow`、`exp10` 等函数的最低有效位可能不同，于是在 Linux 上算出 `0.57`、在 Windows 上可能算出 `0.5700000000000001`。`libm` crate 把同一份纯 Rust 实现编译进程序，彻底消除这个差异。

#### 4.4.2 核心流程

`libm` 的调用点只有两处，但都至关重要：

```
正精度分支：offset = libm::exp10(precision as f64)          // 10^precision
负精度分支：offset = libm::exp10((-precision) as f64)        // 10^(-precision)
```

两处算出的 `offset` 都是「10 的正整数次幂」，随后用于 4.1 的乘除。由于 `offset` 必须精确（哪怕差一个 ULP 都会让舍入结果错位），所以它**必须**用确定性的 `libm`，而不是「大概正确」的平台实现。

#### 4.4.3 源码精读

`libm` 依赖在 [Cargo.toml:L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L25) 引入。两处调用分别是：

- 正精度：[src/round.rs:L49](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L49) — `let offset = libm::exp10(precision.into());`
- 负精度：[src/round.rs:L56](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L56) — `let offset = libm::exp10(-f64::from(precision));`

注意 `precision` 是 `i16`，`libm::exp10` 接收 `f64`，所以两处都做了整数到浮点的转换（`precision.into()` 和 `-f64::from(precision)`）。负精度分支里特意写成 `-f64::from(precision)`（先把负的 `precision` 转成 `f64` 再取负），等价于 `(-precision) as f64`，得到正指数。

测试代码 [src/round.rs:L217](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L217) 也直接用 `libm::exp10` 来构造边界数据（`let exp10 = |exponent: i16| libm::exp10(exponent.into());`），说明项目把 `libm::exp10` 视作「可信的真值来源」，连测试基准都建立在它之上。

#### 4.4.4 代码实践

**目标**：直观对比 `libm::exp10` 与标准库 `powi` 的差异（在大多数平台上它们相同，但体会「确定性优先」的设计意图）。

**步骤**（源码阅读型实践，无需运行）：

1. 在 [crates.typst-utils](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/) 目录下检索 `libm::` 的所有使用点，观察除了 `round.rs`，还有哪些文件依赖它（提示：`scalar.rs` 的 `powi` 之外，`round.rs` 是另一个集中点）。
2. 阅读上一讲 `Scalar` 里关于 `powi` 的描述，对照本讲的 `exp10`，归纳 Typst 处理浮点确定性的两条共同策略：
   - 算法层面：移植经过验证的实现（LLVM 的 `powi`）或用纯 Rust 库（`libm`）。
   - 工程层面：用 `assert!` 和短路守卫堵住一切可能产生 `inf` / `NaN` 的路径。

**需要观察的现象**：所有需要逐位确定的浮点运算，Typst 都不直接信任平台数学库。

**预期结果**：你能用一句话向别人解释「为什么 Typst 连算个 10 的幂都要单独引一个 crate」——为了跨平台逐位一致。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `libm::exp10(precision.into())` 换成 `10f64.powi(precision.into())`，在功能上（不考虑跨平台）还能算对吗？为什么仍然不这么做？

**答案**：在大多数平台上结果数值正确，`powi` 也能算出 \( 10^k \)。但不这么做是因为 `powi` 依赖平台浮点行为，无法保证 32/64 位、不同操作系统上逐位一致，违背 Typst 的确定性目标。`libm` 是纯 Rust 实现，才是确定性场景的正确选择。

**练习 2**：负精度分支为什么写成 `libm::exp10(-f64::from(precision))`，而不是 `libm::exp10(f64::from(-precision))`？两者有区别吗？

**答案**：两者数值结果相同，都是把负的 `precision` 转成一个正指数。区别只是「先转 `f64` 再取负」还是「先取负再转 `f64`」。这里 `precision` 是 `i16`，对它能表示的负数范围来说两种写法都不会有溢出或精度差异，属于等价写法。源码选前者，可读性上更贴近「对 `f64` 做取负」这一意图。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个「mini 舍入调试器」。

**任务**：写一个小程序，对同一批数据分别调用 `round_with_precision`（浮点版）和 `round_int_with_precision`（整数版），打印对照表，并故意包含会触发各种边界分支的输入。

```rust
// 示例代码：综合实践
use typst_utils::{round_int_with_precision, round_with_precision};

fn main() {
    // 浮点舍入：覆盖正/零/负精度、无穷、超大整数
    let float_cases: &[(f64, i16)] = &[
        (-0.56553, 2),   // 正精度
        (823543.0, -3),  // 负精度
        (0.56453, 0),    // 舍入到整数
        (f64::INFINITY, 2),          // 短路：无穷
        ((1_i64 << f64::MANTISSA_DIGITS) as f64, 2), // 短路：≥2^53 的整数
        (1234.5678, -309),           // 短路：精度过负 → ±0.0
    ];
    for (v, p) in float_cases {
        println!("round_with_precision({v}, {p}) = {}", round_with_precision(*v, *p));
    }

    // 整数舍入：覆盖溢出与正常情况
    let int_cases: &[(i64, i16)] = &[
        (-154, -2),
        (823543, -3),
        (i64::MAX, -1),  // 进位溢出 → None
        (i64::MAX, -2),  // 不溢出
        (i64::MIN, -1),  // 进位溢出 → None
    ];
    for (v, p) in int_cases {
        println!("round_int_with_precision({v}, {p}) = {:?}",
            round_int_with_precision(*v, *p));
    }
}
```

**你要回答的问题**（把答案写在注释里）：

1. 为什么 `f64::INFINITY` 那一行原样打印 `inf`，而不是 `NaN` 或 panic？
2. 为什么 `i64::MAX` 配 `-1` 是 `None`，配 `-2` 却能给出一个具体数字？
3. 把任意一行的 `precision` 改成 `15` 或更大，浮点结果会怎样？为什么？

**预期结果**：程序打印出 11 行结果，前 6 行是浮点、后 5 行是 `Option<i64>` 的 `Debug` 输出（`None` 或 `Some(...)`）。通过这张表，你能一眼看清「正/负精度」「短路」「溢出」四类行为。结果待本地验证。

## 6. 本讲小结

- `round_with_precision(value, precision)` 用「搬小数点 → `round()` → 搬回去」实现：正精度乘 `10^p` 再除、负精度除 `10^{|p|}` 再乘，统一遵循「半数远离零」。
- 入口的一串 `||` 短路守卫把无穷、NaN、`|value| ≥ 2^53` 的整数、`precision ≥ 15`、`precision < -308` 这些「舍了等于没舍」或「会出特殊值」的输入原样挡回，并连带保证后续乘法不会溢出成 `inf`。
- 负精度里用「除」而非「乘负指数」，是为了利用 `|MAX_10_EXP|=308 > |MIN_10_EXP|=307` 这个微小不对称，多支持一档精度。
- `round_int_with_precision` 返回 `Option<i64>`，用 `checked_add` / `checked_mul` + `?` 在整数进位溢出时安全返回 `None`，而不是静默回绕。
- `i64::MAX` 以 7 结尾，在 `-1` 精度下要向上进位到超出 `i64` 范围，所以返回 `None`；同样输入在 `-2` 精度下末位变 0 不需进位，故能正常返回。
- 两处 `10^k` 都用 `libm::exp10`（纯 Rust 实现），与上一讲 `Scalar::powi` 选用 LLVM 算法同理：保证浮点运算跨平台逐位确定。

## 7. 下一步学习建议

本讲搞定了「数值怎么安全舍入」，其中的「按量级选择单位 + 复用 `round_with_precision`」思路在下一篇 [u2-l3 人类可读的时长格式化 duration.rs](u2-l3-duration-formatting.md) 会立刻被用到——`format_duration` 把 `Duration` 转成「4 min 24.78 s」时，正是调用本讲的 `round_with_precision` 来保留两位小数。建议：

- 继续阅读 [src/duration.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs)，看它如何复用本讲的 `round_with_precision`，体会「小工具被串成大功能」的工程美感。
- 想深入「确定性浮点」主题，可重读上一讲 `Scalar` 的 `powi`（[src/scalar.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs)），与本讲的 `libm::exp10` 对照，归纳 Typst 的浮点确定性策略。
- 想练习「读测试理解行为」，本讲 [src/round.rs:L119-L304](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L119-L304) 的测试模块是最完整的边界用例集，逐条读懂它，胜过自己猜测边界行为。
