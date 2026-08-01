# 人类可读的时长格式化 duration.rs

## 1. 本讲目标

本讲聚焦 `typst-utils` 中一个「小而完整」的工程函数 `format_duration`。学完后你应当能够：

- 说清楚 `format_duration` 如何把一个 `std::time::Duration` 转成 `4 min 24.78 s`、`294.82 ms`、`11 d 13 h 46 min` 这样人类可读的字符串。
- 理解它「按量级分级显示」的策略：长时长显示到「天/时/分」，中等时长显示到「秒」，短时长再下探到「毫/微/纳秒」。
- 读懂它用 `piece!` 宏实现「空格分隔拼接」的精简写法。
- 看懂它如何复用上一讲的 `round_with_precision` 来保留两位小数，以及在「超长时间」时主动丢弃小数部分。
- 体会到「延迟格式化（返回 `impl Display`）」「精度随量级降级」这两个常见工程手法。

## 2. 前置知识

在进入源码前，先用通俗语言铺好几个基础概念。

- **`Duration` 是什么**：`std::time::Duration` 表示「一段时间长度」，内部由「整秒数 `as_secs()`」+「不足 1 秒的纳秒部分 `subsec_nanos()`」两部分组成。比如 `264.776 秒` 会被存成「264 秒 + 776_000_000 纳秒」。本讲要做的，就是把这堆「秒+纳秒」换算成更直觉的单位。
- **`Display` trait 与延迟格式化**：实现了 `Display` 的类型可以用 `{}` 打印、或用 `.to_string()` 转成字符串。`format_duration` 并不立刻生成字符串，而是返回一个「实现了 `Display` 的包装类型」——只有当你真正去打印它时，格式化逻辑才会执行。这种「先返回一个描述，按需才求值」的做法叫**延迟格式化**，好处是零成本、且能直接配合 `format!("{}", x)` 之类惯用法。
- **`Formatter` 与 `fmt::Result`**：`Display::fmt(&self, f: &mut Formatter)` 是真正干活的函数，往 `f` 里写字符可能失败（比如底层缓冲区出错），所以每步都返回 `fmt::Result`，用 `?` 传播错误。
- **`round_with_precision`（上一讲 u2-l2）**：本讲会复用它。你只需记住：`round_with_precision(value, 2)` 把 `value` 舍入到「小数点后 2 位」，遵循「半数远离零」。例如 `round_with_precision(24.776, 2) == 24.78`。它的内部短路守卫与 `libm` 细节已在上一讲讲过，这里不再重复。
- **`macro_rules!` 基础（u1-l3）**：本讲会现场定义一个局部宏 `piece!`，你需要知道 `$($tts:tt)*` 表示「任意 token 序列」，`$()*` 表示重复展开。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，并少量引用它的「上下游」：

| 文件 | 作用 |
| --- | --- |
| [src/duration.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs) | 本讲主角。定义 `format_duration` 及其私有包装类型 `DurationDisplay`，约 70 行核心逻辑。 |
| [src/round.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs) | 提供 `round_with_precision`，被 `duration.rs` 复用来保留两位小数（u2-l2 已精读）。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 通过 `mod duration;`（第 9 行）声明私有模块，再用 `pub use`（第 21 行）把 `format_duration` 暴露为公开 API。 |

调用关系非常简单：

```text
用户: format_duration(Duration) -> impl Display
            │  （返回一个 DurationDisplay 包装，不立即求值）
            ▼
打印时: DurationDisplay::fmt()
            │  （进位拆解 + 按量级选单位）
            ├── piece! 宏：往 Formatter 写「空格 + 片段」
            └── round_with_precision(.., 2)：保留两位小数
```

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：入口与 `piece!` 拼接宏、量级分级显示（天/时/分进位）、秒以下量级与复用 `round_with_precision`、超长时间丢弃小数。

### 4.1 入口设计：延迟格式化与 piece! 空格拼接宏

#### 4.1.1 概念说明

`format_duration` 要解决两个问题：**算出该显示什么**，以及**把若干片段拼成一个字符串**。

- 对「算什么」，它返回一个轻量包装类型 `DurationDisplay`，把 `Duration` 藏在里头，**先不算**。
- 对「拼什么」，最朴素的写法是把各片段收集进 `Vec<String>` 再 `join(" ")`，但那样会分配堆内存。这里改用一个**局部宏 `piece!`** 配合一个 `space` 布尔标记：第一次写片段前不加空格，之后每次写片段前先补一个空格。这样不分配、不收集，边算边写进 `Formatter`。

#### 4.1.2 核心流程

`piece!` 宏的执行逻辑（伪代码）：

```text
维护一个标记 space（初始 false）

每当调用 piece!("片段"):
    old = 取出 space 的当前值，并把 space 置为 true
    若 old == true:            # 说明前面已经写过片段
        先写一个空格 ' '
    再写入 "片段"
```

关键点是 `std::mem::replace(&mut space, true)`：它「返回旧值、同时写入新值 true」一步完成。于是「是否是第一个片段」的判断被天然地编码进了这个标记——第一个片段时 `space` 是 `false`，所以不加空格；从第二个片段起 `space` 已是 `true`，于是加空格。

#### 4.1.3 源码精读

入口函数返回 `impl Display`，实际返回私有包装类型 `DurationDisplay`（[src/duration.rs:L7-L12](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L7-L12)）：这里把 `Duration` 装进 `DurationDisplay`，**不立刻格式化**。

`piece!` 宏定义在 `Display::fmt` 内部（[src/duration.rs:L16-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L16-L24)）：

```rust
let mut space = false;
macro_rules! piece {
    ($($tts:tt)*) => {
        if std::mem::replace(&mut space, true) {
            f.write_char(' ')?;
        }
        write!(f, $($tts)*)?;
    };
}
```

`$($tts:tt)*` 把传给宏的全部内容（如 `"{days} d"`）原样转发给标准库的 `write!` 宏。这样 `piece!("{hours} h")` 就等价于「必要时先写空格，再写 `{hours} h`」。

公开导出在 [src/lib.rs:L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L21)，模块声明在 [src/lib.rs:L9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L9)。

#### 4.1.4 代码实践

**目标**：体会「延迟格式化」与 `piece!` 的拼接行为。

1. 在 `typst-utils` crate 内新增一个临时 example（或在自己的小项目里用 path 依赖引用本 crate），写：
   ```rust
   use std::time::Duration;
   use typst_utils::format_duration;

   fn main() {
       let d = format_duration(Duration::from_secs(90));
       // 还没真正格式化，d 只是一个 impl Display
       println!("{}", d);      // 现在才求值
       println!("{}", format_duration(Duration::from_secs(3665)));
   }
   ```
2. 观察输出：第一行是单片段（`1 min 30 s` 一类），第二行是多片段用空格连接。
3. **待本地验证**：确切的输出字符串请对照第 5 节的综合实践断言；若暂无法编译，可改用「源码阅读型实践」——直接阅读 `Display::fmt`，确认每段都经 `piece!` 写出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `std::mem::replace(&mut space, true)` 改成 `let old = space; space = true;`，行为会变吗？
**答案**：不会。两者语义等价（都是「取旧值、置新值 true」），`mem::replace` 只是更紧凑、更不易写错的写法。

**练习 2**：为什么 `piece!` 要在写内容**之前**先判断要不要写空格，而不是在每段**之后**补空格？
**答案**：之后补空格会导致字符串**末尾多出一个空格**（如 `"4 min 24.78 s "`）。之前补空格则只有「非首段」才补，天然没有尾随空格。

---

### 4.2 量级分级显示：天/时/分的进位拆解

#### 4.2.1 概念说明

人类读时长时，不会把「100 万秒」原样甩出来，而是想看到「11 天 13 小时 46 分」。`duration.rs` 用一段连续的**进位拆解（carry decomposition）**把「总秒数」层层拆成「天/时/分/秒」四个分量，再用「非零才显示」的规则拼出大单位。

这里有一个关键的设计取舍：**精度随量级降级**。一旦时长够长（出现了「天」或「时」），就**只显示到分钟**，秒及其以下细节直接丢弃——因为对一个长达十几天的时长，秒级精度对人毫无意义，反而让字符串变长。

#### 4.2.2 核心流程

进位拆解流程（每一步都把「高位的余数」回填到「低位」）：

```text
secs   = duration.as_secs()              # 总秒数（整数）
mins, secs = secs / 60 , secs % 60       # 拆出分钟，留下不足 1 分钟的秒
hours, mins = mins / 60 , mins % 60      # 拆出小时，留下不足 1 小时的分钟
days, hours = hours / 24 , hours % 24    # 拆出天，  留下不足 1 天的小时

# 仅当分量 > 0 才输出对应片段：
days  > 0 → piece!("{days} d")
hours > 0 → piece!("{hours} h")
mins  > 0 → piece!("{mins} min")

# 关键：出现了天或小时，就到此为止，不再显示秒
if days > 0 或 hours > 0:
    return   # 提前结束
```

注意这里大量使用**变量遮蔽（shadowing）**：`secs`、`mins`、`hours` 都在解构赋值时被同名重绑定。初读时容易看花眼，但好处是「同一个名字始终代表当前最细的单位」——拆完分钟后，`secs` 就专指「不足 1 分钟的剩余秒数」。

#### 4.2.3 源码精读

进位拆解与三个大单位的输出（[src/duration.rs:L26-L41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L26-L41)）：

```rust
let secs = self.0.as_secs();
let (mins, secs) = (secs / 60, (secs % 60));
let (hours, mins) = (mins / 60, (mins % 60));
let (days, hours) = ((hours / 24), (hours % 24));

if days > 0 { piece!("{days} d"); }
if hours > 0 { piece!("{hours} h"); }
if mins > 0 { piece!("{mins} min"); }
```

「出现大单位就停」的提前返回（[src/duration.rs:L43-L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L43-L46)）：

```rust
// No need to display anything more than minutes at this point.
if days > 0 || hours > 0 {
    return Ok(());
}
```

手算验证 `Duration::from_secs(1_000_000)`：

```text
1_000_000 秒
→ (mins, secs) = (16_666, 40)
→ (hours, mins) = (277, 46)
→ (days, hours) = (11, 13)
```

于是输出 `days=11`、`hours=13`、`mins=46` 三个片段，得到 `11 d 13 h 46 min`；剩余的 `secs=40` 因为 `days>0` 被提前返回丢弃。这与源码测试 `test(Duration::from_secs(1000000), "11 d 13 h 46 min")` 一致。

#### 4.2.4 代码实践

**目标**：用纸笔（或断点）复现进位拆解，验证「大单位提前返回」。

1. 阅读 [src/duration.rs:L80-L93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L80-L93) 的测试用例。
2. 对 `Duration::from_secs(3600 * 24)`（一整天）手算拆解：应得 `days=1, hours=0, mins=0`，因此只输出 `1 d`。
3. 对 `Duration::from_secs(3600)`（1 小时整）手算：应得 `hours=1`，输出 `1 h`。
4. **待本地验证**：运行 `cargo test -p typst-utils test_format_duration`，确认这些断言通过（本实践只读运行测试，不修改源码）。

#### 4.2.5 小练习与答案

**练习 1**：`Duration::from_secs(3600 + 240)`（即 1 小时 4 分钟）会输出什么？
**答案**：拆解得 `hours=1, mins=4, secs=0`。输出 `hours` 片段 `1 h`、`mins` 片段 `4 min`，随后因 `hours>0` 提前返回（`secs=0` 本来也不会再显示秒）。结果：`1 h 4 min`。

**练习 2**：为什么「天/时/分」片段用整数 `{days}`，而秒以下却可能带小数？
**答案**：大单位已经通过整数除法 `//`、`%` 取整，天然无小数；而秒以下为了表达「24.78 秒」这种亚秒精度，需要带小数（见 4.3）。两类单位的展示精度策略不同。

---

### 4.3 秒以下量级选择与复用 round_with_precision

#### 4.3.1 概念说明

当时长不足 1 小时（即上一节没有提前返回），代码进入「秒及以下」的处理。这里把**所有不足 1 分钟的时间**统一换算成**纳秒整数 `nanos`**，再根据时长量级选择一个合适的「展示单位」（秒/毫秒/微秒/纳秒），用 `nanos / 1000^exp` 换算到该单位，最后复用上一讲的 `round_with_precision(.., 2)` 保留两位小数。

之所以统一用纳秒做中间量，是因为 `Duration` 本身就是「秒 + 纳秒」结构，纳秒是它能精确表达的最细粒度；用一个公倍数当中间量，就能用同一个公式 `nanos / 1000^exp` 切换到任意「千进制」单位。

#### 4.3.2 核心流程

换算与量级选择（伪代码）：

```text
order(exp) = 1000^exp                      # 闭包：千进制幂
nanos = secs * 1000^3 + subsec_nanos        # 把「剩余秒」和「亚秒纳秒」合成总纳秒
fract(exp) = round_with_precision(nanos / 1000^exp, 2)   # 换算到目标单位并保留 2 位

# 按量级从大到小选择单位（exp 越大单位越大）：
if 时长 == 0 或 时长 > 1 秒:
    展示「秒」  fract(3) = nanos / 1e9
elif 时长 > 1 毫秒:
    展示「毫秒」fract(2) = nanos / 1e6
elif 时长 > 1 微秒:
    展示「微秒」fract(1) = nanos / 1e3
else:
    展示「纳秒」fract(0) = nanos / 1
```

单位与 `exp` 的对应关系：

| exp | `1000^exp` | 展示单位 | 典型时长区间 |
| --- | --- | --- | --- |
| 3 | \(10^9\) | 秒 `s` | 时长为 0 或 > 1 秒 |
| 2 | \(10^6\) | 毫秒 `ms` | 1 毫秒 ~ 1 秒 |
| 1 | \(10^3\) | 微秒 `µs` | 1 微秒 ~ 1 毫秒 |
| 0 | \(1\) | 纳秒 `ns` | < 1 微秒 |

复用 `round_with_precision(.., 2)` 的意义：把 `24.776` 干净地舍成 `24.78`，把 `294.816` 舍成 `294.82`，且因为 `round_with_precision` 走 `libm` 实现，结果跨平台逐位确定（这是 u2-l1、u2-l2 一脉相承的「确定性」主题）。

#### 4.3.3 源码精读

两个闭包与总纳秒的合成（[src/duration.rs:L48-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L48-L50)）：

```rust
let order = |exp| 1000_u64.pow(exp);
let nanos = secs * order(3) + self.0.subsec_nanos() as u64;
let fract = |exp| round_with_precision(nanos as f64 / order(exp) as f64, 2);
```

量级选择链（[src/duration.rs:L52-L65](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L52-L65)）：

```rust
if nanos == 0 || self.0 > Duration::from_secs(1) {
    // 秒（>5 分钟时丢弃小数，见 4.4）
    if self.0 > Duration::from_secs(300) { piece!("{secs} s"); }
    else { piece!("{} s", fract(3)); }
} else if self.0 > Duration::from_millis(1) {
    piece!("{} ms", fract(2));
} else if self.0 > Duration::from_micros(1) {
    piece!("{} µs", fract(1));
} else {
    piece!("{} ns", fract(0));
}
```

> 说明：`nanos == 0` 这个条件专门兜底「时长恰好为 0」——否则零时长会一路掉进纳秒分支打印 `0 ns`，作者认为打印 `0 s` 更自然。

复用的 `round_with_precision` 本体在 [src/round.rs:L26-L59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/round.rs#L26-L59)，并经 [src/lib.rs:L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L26) 的 `pub use` 暴露。`duration.rs` 顶部用 `use super::round_with_precision;`（[src/duration.rs:L4](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L4)）把它引入。

手算 `Duration::from_micros(294_816)`：

```text
as_secs()=0, subsec_nanos()=294_816_000
secs(剩余)=0, 大单位全为 0，不提前返回
nanos = 0 + 294_816_000 = 294_816_000
时长 0.295 秒 > 1 毫秒 → 走 ms 分支
fract(2) = round_with_precision(294_816_000 / 1e6, 2)
         = round_with_precision(294.816, 2) = 294.82
→ 输出 "294.82 ms"
```

与测试 `test(Duration::from_micros(294816), "294.82 ms")` 一致。

#### 4.3.4 代码实践

**目标**：复现「纳秒中间量 + 千进制换算」的思路。

1. 取 `Duration::from_nanos(1)` 手算：`as_secs=0, subsec_nanos=1, nanos=1`。时长既不 >1 秒、也不 >1 毫秒、也不 >1 微秒，掉进 `else` 纳秒分支：`fract(0)=round_with_precision(1/1, 2)=1`，输出 `1 ns`。
2. 取 `Duration::from_micros(734)` 手算：`nanos=734_000`，时长 >1 微秒但 <1 毫秒，走 `µs` 分支：`fract(1)=round_with_precision(734_000/1000, 2)=round(734.0,2)=734`，输出 `734 µs`。
3. **待本地验证**：运行测试 `cargo test -p typst-utils test_format_duration`，确认 `1 ns`、`734 µs` 两条断言通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `order` 用 `1000_u64.pow(exp)`，而 `fract` 里又把 `nanos` 转成 `f64`？
**答案**：`order` 计算 \(1000^{exp}\) 用于**整数换算**（`nanos` 是 `u64`），保持精度且无浮点误差；`fract` 最终要交给 `round_with_precision`（它只接收 `f64`）做小数舍入，所以在「除法 + 舍入」这一步才转 `f64`。整数部分尽量用整数算，只在最后舍入时进浮点，是兼顾精度与需求的常见写法。

**练习 2**：若把 `round_with_precision(.., 2)` 的精度改成 `1`，`294.82 ms` 会变成什么？
**答案**：`round_with_precision(294.816, 1) = 294.8`，故输出 `294.8 ms`。精度参数直接控制小数位数。

---

### 4.4 超长时间丢弃小数：精度降级策略

#### 4.4.1 概念说明

「秒」分支里藏着一个容易忽略的细节：当时长**超过 5 分钟**（但不足 1 小时，否则上一节已提前返回）时，代码不再用 `fract(3)` 显示带小数的秒，而是直接显示**整数秒** `{secs}`。这又是一次「精度随量级降级」：6 分钟上下的时长，零点几秒的小数对人眼已无价值，整秒就够了；而 1~5 分钟区间的时长才值得保留两位小数。

把三处降级策略串起来看，整个函数的「精度梯度」是连贯的：

```text
出现 天/时    → 显示到「分」为止，丢弃秒
5 分钟 ~ 1 小时 → 显示整数「秒」，丢弃小数
1 秒 ~ 5 分钟  → 显示「秒」+ 两位小数
< 1 秒        → 下探到 ms/µs/ns + 两位小数
```

量级越大，展示越粗；量级越小，展示越细。这与人类对时长的直觉感知一致。

#### 4.4.2 核心流程

「秒」分支内部的二次判断：

```text
已确定走「秒」单位（时长==0 或 时长>1秒）：
    if 时长 > 5 分钟(300 秒):
        piece!("{secs} s")        # 整数秒，丢弃小数
    else:
        piece!("{} s", fract(3))  # 带两位小数
```

注意 `secs` 在此处（经 4.2 的遮蔽）专指「不足 1 分钟的剩余秒数」，范围 0..60，所以「整数秒」不会有歧义。

#### 4.4.3 源码精读

降级判断（[src/duration.rs:L52-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L52-L58)）：

```rust
if nanos == 0 || self.0 > Duration::from_secs(1) {
    // For durations > 5 min, we drop the fractional part.
    if self.0 > Duration::from_secs(300) {
        piece!("{secs} s");
    } else {
        piece!("{} s", fract(3));
    }
}
```

注释 `For durations > 5 min, we drop the fractional part.` 直接点明了意图。

手算 `Duration::from_secs_f64(264.776)` 验证「带小数」路径：

```text
as_secs()=264, subsec_nanos()=776_000_000
mins=4, secs(剩余)=24, hours=0, days=0
→ 先输出 mins 片段 "4 min"
→ days/hours 都为 0，不提前返回
→ nanos = 24*1e9 + 776_000_000 = 24_776_000_000
→ 时长 264.776 秒：>1 秒（走秒分支），但 ≤ 300 秒（不丢弃小数）
→ fract(3) = round_with_precision(24_776_000_000 / 1e9, 2)
           = round_with_precision(24.776, 2) = 24.78
→ 输出 "4 min 24.78 s"
```

与测试 `test(Duration::from_secs_f64(264.776), "4 min 24.78 s")` 一致。

对比 `Duration::from_secs_f64(364.77)`（>5 分钟）走「丢弃小数」路径：`mins=6, secs(剩余)=4`，输出 `mins` 片段 `6 min`，随后 `364.77 > 300` 故 `piece!("{secs} s")` 输出整数 `4 s`，最终 `6 min 4 s`（无小数），与测试 `test(Duration::from_secs_f64(364.77), "6 min 4 s")` 一致。

#### 4.4.4 代码实践

**目标**：观察「5 分钟」这条临界线两侧的输出差异。

1. 构造两个相近的时长：`Duration::from_secs_f64(299.999)` 与 `Duration::from_secs_f64(300.001)`。
2. 手算预测：
   - `299.999`：`>1秒` 且 `≤300秒` → 带小数，`mins=4, secs≈60`… 实际拆解 `299/60=4 余 59`，输出形如 `4 min 59.xx s`（带两位小数）。
   - `300.001`：`>300秒` → 整数秒，`mins=5, secs≈0`，输出形如 `5 min 0 s`。
3. **待本地验证**：在 example 里打印 `format_duration(Duration::from_secs_f64(299.999))` 与 `format_duration(Duration::from_secs_f64(300.001))`，确认前者带小数、后者为整数秒。

#### 4.4.5 小练习与答案

**练习 1**：「超过 5 分钟就丢弃小数」的分支，为什么只可能出现在「不足 1 小时」的时长里？
**答案**：因为 4.2 节里 `if days > 0 || hours > 0 { return; }` 已经把「≥1 小时」的时长提前返回了。能走到「秒」分支的时长必然 `hours == 0`，即不足 1 小时。所以这个降级分支实际覆盖的是「5 分钟 ~ 1 小时」区间。

**练习 2**：注释说 `> 5 min` 才丢弃小数，但代码写的是 `> Duration::from_secs(300)`，二者等价吗？
**答案**：等价。300 秒正好是 5 分钟。代码用 `from_secs(300)` 是为了避免「魔法数字 5」与「分钟换算」混写，直接以秒为统一单位比较，可读性更好且与同文件其它 `Duration::from_secs(...)` 判断风格一致。

---

## 5. 综合实践

把本讲三个要点（量级分级、`piece!` 拼接、复用 `round_with_precision` 与丢弃小数）串起来，完成下面这个**贯穿性任务**：对三个典型时长调用 `format_duration`，并断言输出。

**任务**：编写一段调用，验证下列三组输入/输出（它们正是源码测试 [src/duration.rs:L82,L87,L91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L82) 中的断言）：

| 输入 `Duration` | 期望输出 | 命中的量级路径 |
| --- | --- | --- |
| `from_secs_f64(264.776)` | `4 min 24.78 s` | 秒分支 + 保留两位小数 |
| `from_micros(294816)` | `294.82 ms` | 毫秒分支 + 保留两位小数 |
| `from_secs(1_000_000)` | `11 d 13 h 46 min` | 天/时/分，提前返回（秒被丢弃） |

**操作步骤**（示例代码，需自行确保 `typst-utils` 可作为依赖被引入，例如在本工作区内用 path 依赖）：

```rust
// 示例代码（非项目原有）
use std::time::Duration;
use typst_utils::format_duration;

fn main() {
    let cases = [
        (Duration::from_secs_f64(264.776),   "4 min 24.78 s"),
        (Duration::from_micros(294_816),     "294.82 ms"),
        (Duration::from_secs(1_000_000),     "11 d 13 h 46 min"),
    ];
    for (d, expected) in cases {
        let got = format_duration(d).to_string();
        assert_eq!(got, expected, "for {:?}", d);
        println!("ok: {got}");
    }
}
```

**需要观察的现象**：

1. 第一行：`264.776` 秒被拆成 `4 min` 加 `24.78 s`（注意 `24.776` 被舍成 `24.78`），证明「秒分支保留两位小数」与 `piece!` 空格拼接。
2. 第二行：不足 1 秒的时长下探到毫秒，`294.816 ms` 被舍成 `294.82 ms`，证明「量级下探 + 复用 `round_with_precision`」。
3. 第三行：百万秒被拆成 `11 d 13 h 46 min`，**没有**秒，证明「出现天/时即提前返回、丢弃秒」。

**预期结果**：三行断言全部通过。

**若无法本地编译**（例如不方便建立对工作区 crate 的依赖）：直接运行项目自带的测试即可验证同样的事实——

```bash
cargo test -p typst-utils test_format_duration
```

该测试（[src/duration.rs:L80-L93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/duration.rs#L80-L93)）已包含上述三组断言，跑通即等价于验证了本讲的核心行为。此命令只读运行测试，不修改任何源码。

## 6. 本讲小结

- `format_duration` 返回 `impl Display`（私有包装 `DurationDisplay`），**延迟格式化**——只在真正打印时才求值。
- 局部宏 `piece!` 配合一个 `space` 布尔标记，用 `mem::replace` 实现「无尾随空格、无堆分配」的空格分隔拼接。
- 「天/时/分」用一段**进位拆解**（连续的 `/ 60`、`% 60`、`/ 24`）求得，非零才输出；一旦出现天或小时就**提前返回**，不再显示秒。
- 秒以下统一换算成**纳秒中间量**，再按量级选「秒/毫/微/纳秒」单位（`nanos / 1000^exp`），并复用上一讲的 `round_with_precision(.., 2)` 保留两位小数。
- 贯穿全局的是**精度随量级降级**：天/时级显示到分、5~60 分钟级显示整数秒、更短时长才显示两位小数。
- 本讲是 u2-l2 `round_with_precision` 的直接应用场景，二者共同延续「跨平台逐位确定性」主题。

## 7. 下一步学习建议

- 本讲涉及的 `round_with_precision` 已在 u2-l2 精读；若还想看「整数版带溢出保护」的舍入，可回顾 `round_int_with_precision`。
- 接下来进入 **u2-l4 位压缩集合 BitSet 与 SmallBitSet**，从「数值/时长」主题切换到「用位运算表示集合」的另一种工程套路，继续体会 typst-utils 中「小而精」的数据结构设计。
- 若对「延迟求值 / 包装类型实现 trait」的手法感兴趣，可在后续 u2-l6（`LazyHash`）中再次看到类似「包一层、按需才算」的设计。
