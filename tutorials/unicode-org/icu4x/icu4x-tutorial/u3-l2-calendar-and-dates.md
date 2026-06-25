# 日历系统与日期类型

## 1. 本讲目标

上一讲（[u3-l1](u3-l1-datetime-formatting.md)）我们看到了 `DateTimeFormatter` 如何把一个日期格式化成本地化字符串。但那里有一个被刻意略过的前提：**喂给格式化器的「日期」本身是什么？** 一段「2025-06-24」到底是公历的 6 月 24 日，还是儒略历、佛历、回历下的某一天？这背后需要一个完整的「日历抽象」。本讲就来补上这块拼图——`icu_calendar`。

学完后你应该能够：

- 说清楚 `Calendar` 这个 trait **封装了什么**，以及为什么 `Date<A>` 要用一个泛型参数 `A`（而不是直接 `Date<Gregorian>`）。
- 理解 **Rata Die（固定日序）** 作为所有日历互相换算的「中立中介」，并知道 `to_calendar` 何时走「ISO 快速通道」、何时走「Rata Die 通用通道」。
- 会构造 `Gregorian` / `Iso` / `Buddhist` 等具体日历的日期，并能解释为什么佛历年份正好比 ISO 年份大 543。
- 理解 `AnyCalendar` 如何用「类型擦除 + 枚举分发」实现 **运行时多态**，并会从一个带 `-u-ca-...` 扩展的 locale 字符串选出对应日历。

> 本讲承接 [u3-l1](u3-l1-datetime-formatting.md)（格式化器）、[u2-l1](u2-l1-locale-model.md)（`Locale` 与 `-u-ca-` 扩展）和 [u1-l4](u1-l4-metacrate-and-features.md)（`compiled_data` 体系）。`locale!` 宏和「构造期加载 compiled data 故返回 `Result`」这两点会反复用到。

## 2. 前置知识

### 2.1 为什么需要「日历」这个抽象

同一天，在不同日历里「长得不一样」：

| 日历 | 同一个绝对日子（ISO 1992-09-02）|
|---|---|
| ISO / Gregorian（公历） | 1992 年 9 月 2 日 |
| Buddhist（泰国佛历） | 2535 年 9 月 2 日 |
| Indian（印度国历） | 1914 年 6 月 11 日 |

这些「年/月/日」都不是凭空写的，而是同一根「绝对时间轴」上的不同坐标刻度。所以 ICU4X 的设计是：

- 用一个**绝对的中介量**表示「第几天」（这就是 Rata Die）。
- 每个日历只负责回答两件事：**如何把 (年, 月, 日) 翻译成 Rata Die**，以及**如何把 Rata Die 翻译回 (年, 月, 日)**。

于是「日历」就退化成一个可替换的策略对象，`Date` 只是「某个日历下的一组 (年,月,日) + 那个日历本身」。

### 2.2 Rata Die（固定日序）

**Rata Die（RD）** 是一个整数计日法：RD = 1 对应公历（按 proleptic 外推）的 **公元 1 年 1 月 1 日**，每过一天加 1。它是 Dershowitz 与 Reingold 在《Calendrical Calculations》一书中提出的「日历中立」计数，源码注释里也明确标注了这本参考书：

[components/calendar/src/lib.rs:32-34](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/lib.rs#L32-L34) —— 说明算法源自 Dershowitz & Reingold《Calendrical Calculations》。

为什么重要？因为只要每个日历都能在「自己的日期」与 RD 之间互转，那么 **任意两个日历 A、B 之间的转换** 就退化成：

\[ \text{date}_A \xrightarrow{\text{to\_rata\_die}} \text{RD} \xrightarrow{\text{from\_rata\_die}} \text{date}_B \]

这正是本讲后半段 `to_calendar` 的核心逻辑。RD 的有效范围由一个常量界定，对应 ISO 年大约 `-999_999..=999_999`（见 [components/calendar/src/calendar_arithmetic.rs:31-33](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/calendar_arithmetic.rs#L31-L33)）。

### 2.3 extended year（扩展年）

不同日历的「年」含义千差万别（有的有纪元、有的是循环干支年）。为了能做算术和比较，ICU4X 给每个日期算出一个**扩展年（extended year）**：它是一个连续的、可加减的整数，通常把「最重要的纪元的第 1 年」锚定为 1。对公历族日历而言，extended year 就是我们熟悉的公历年份数字（1992 就是 1992）。`EraYear` / `CyclicYear` 等结构都带着这个字段，稍后细看。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `components/calendar/src/lib.rs` | crate 根：模块总览、跨日历转换示例、导出 `Calendar`/`Date`/`AnyCalendar` 等。 |
| `components/calendar/src/calendar.rs` | 定义 `Calendar` trait——日历的策略接口（含 `from_rata_die`/`to_rata_die`/`year_info` 等方法）。 |
| `components/calendar/src/date.rs` | 定义 `Date<A>`、`AsCalendar`、`Ref`，以及用户最常用的 `try_new` / `to_calendar` / `to_any` 等方法。 |
| `components/calendar/src/any_calendar.rs` | `AnyCalendar` 枚举、`AnyCalendarKind`、`IntoAnyCalendar` trait——运行时多态的日历。 |
| `components/calendar/src/cal/abstract_gregorian.rs` | 公历族共享算术（`Gregorian`/`Iso`/`Buddhist`/`Roc` 都建立在它之上），含「ISO 快速通道」实现。 |
| `components/calendar/src/cal/{gregorian,iso,buddhist}.rs` | 三个具体日历：只各自定义「纪元与年份换算」，其余逻辑复用 `AbstractGregorian`。 |
| `components/time/src/types.rs` | `DateTime<A>` 的定义（`Date<A>` + `Time` 的组合）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`Calendar` trait 与 `Date<A>` 类型**——策略接口长什么样、`Date` 为何带泛型、用户 API 如何转发到具体日历。
2. **具体日历实现：从 `AbstractGregorian` 理解 Gregorian / Iso / Buddhist**——`to_calendar` 的两条转换通道，以及「佛历 +543」从何而来。
3. **`AnyCalendar` 与运行时日历转换**——类型擦除、枚举分发、按 locale 字符串选日历。

---

### 4.1 Calendar trait 与 Date\<A\> 类型

#### 4.1.1 概念说明

ICU4X 把「日历」抽象成一个被 **密封（sealed）** 的 trait `Calendar`。普通用户**不需要**（也基本**不应该**）实现它——库内置了十几种日历，你要么直接用，要么通过 `AnyCalendar` 在运行时挑。`Calendar` 的职责是：给定一个内部日期表示，告诉我它的年/月/日/星期、它在不在闰年、把它加减一段时间后变成什么、以及——最关键的——它与 Rata Die / ISO 日期如何互转。

而真正给用户用的类型是 `Date<A>`，其中 `A: AsCalendar`。`A` 通常是某个具体日历类型（如 `Iso`、`Gregorian`），但也可以是 `AnyCalendar`、甚至包了 `Rc`/`Arc` 的日历。`Date` 把「日历策略」和「在该日历下的内部日期」打包在一起，所有用户 API（`month()`、`year()`、`to_calendar()`…）都只是**转发**给内部那个日历。

为什么不用 `Date<Gregorian>` 写死？因为同一个 `Date` 要能在不同日历间流转，而日历对象本身（如 `Japanese`，含纪元数据）可能不是 `Copy` 的零成本类型——用泛型 `A` 允许把日历放进 `Rc`/`Arc` 共享，避免反复克隆数据。

#### 4.1.2 核心流程

用户创建并使用一个 `Date` 的典型路径：

```
Date::try_new_iso(y, m, d)            # ① 用具体日历的便捷构造器（或通用 try_new）
        │
        ▼
   Date<Iso> { inner: IsoDateInner, calendar: Iso }
        │
        │  date.month() / date.year() / date.weekday()
        ▼
   转发给 calendar 对应的 Calendar trait 方法
        │
        │  date.to_calendar(Buddhist)
        ▼
   Date<Buddhist> { inner: BuddhistDateInner, calendar: Buddhist }
```

`Calendar` trait 的关键方法可以分成三组：

- **构造**：`new_date`（从年月日构造内部日期）、`from_fields`（从字段袋构造）。
- **换算**：`from_rata_die` / `to_rata_die`（通用通道）、`from_iso` / `to_iso`（仅当 `has_cheap_iso_conversion()` 为真时的快速通道）。
- **查询**：`year_info`、`month`、`day_of_month`、`days_in_year`、`is_in_leap_year` 等。

#### 4.1.3 源码精读

先看 `Calendar` trait 本身——注意它的三个关联类型，其中 `DateInner` 是「该日历专属的内部日期表示」：

[components/calendar/src/calendar.rs:33-45](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/calendar.rs#L33-L45) —— `Calendar` trait 的开头：`type DateInner` 是日历专属的内部日期类型；trait 被密封，不建议用户自行实现。

最核心的两个换算方法定义在这里——它们就是上一节「中立中介」思想在代码里的落点：

[components/calendar/src/calendar.rs:133-141](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/calendar.rs#L133-L141) —— `from_rata_die` 与 `to_rata_die`：每个日历必须实现的「与绝对日序互转」。

而 `from_iso` / `to_iso` 有默认实现，**只有当日历声明 `has_cheap_iso_conversion() == true` 时才会被调用**，否则走 RD 通道：

[components/calendar/src/calendar.rs:113-131](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/calendar.rs#L113-L131) —— `has_cheap_iso_conversion` 与 ISO 快速通道的默认实现（默认实现本身就是「转成 RD 再转」）。

再看用户侧的 `Date<A>`。它只是一个把 `inner` 和 `calendar` 装在一起的结构体：

[components/calendar/src/date.rs:138-141](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L138-L141) —— `Date<A>` 的定义：`inner` 是 `<A::Calendar as Calendar>::DateInner`，`calendar` 是 `A`。

通用的构造入口是 `try_new`，它把「年/月/日」交给内部日历的 `new_date`：

[components/calendar/src/date.rs:192-200](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L192-L200) —— `Date::try_new`：转发给 `calendar.as_calendar().new_date(...)`，故返回 `Result`（年月日可能非法）。

所有查询方法都是同一套「转发」模式，例如 `month()`：

[components/calendar/src/date.rs:325-328](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L325-L328) —— `Date::month()` 直接委托给日历的 `month(&self.inner)`。

注意 `try_new_from_codes`（基于字符串纪元/月份码的旧构造器）已被标记为 `#[deprecated]`，新代码用 `try_new`（见 [components/calendar/src/date.rs:150-164](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L150-L164)）。这是阅读时容易踩的坑。

> **关于 `DateTime<A>`**：ICU4X 2.x 里 `DateTime` 不在 `icu_calendar`，而在 `icu::time`。它只是 `Date<A>` 外面再套一个 `Time`，同样以 `A: AsCalendar` 参数化、不带算术与全序比较，仅用于「展示给用户」。[components/time/src/types.rs:207-212](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/time/src/types.rs#L207-L212) 是它的定义。理解了 `Date<A>`，`DateTime<A>` 就自然懂了。

#### 4.1.4 代码实践

**目标**：亲手构造一个 ISO 日期，验证几个查询方法，并确认 `weekday()` 是从 Rata Die 推导出来的（与具体日历无关）。

**操作步骤**（新建一个 `cargo` 二进制项目，`cargo add icu` 后写入 `src/main.rs`，这是**示例代码**）：

```rust
use icu::calendar::{types::Weekday, Date};

fn main() {
    // 1992-09-02（ISO）
    let date = Date::try_new_iso(1992, 9, 2)
        .expect("合法日期");

    println!("weekday     = {:?}", date.weekday());        // Wednesday
    println!("era_year    = {:?}", date.era_year().year);  // 1992
    println!("month       = {}",  date.month().ordinal);   // 9
    println!("day         = {}",  date.day_of_month().0);  // 2
    println!("days_in_year= {}",  date.days_in_year());    // 1992 是闰年 -> 366
    println!("days_in_month={}",  date.days_in_month());   // 9 月 -> 30
}
```

**需要观察的现象**：`weekday()` 返回 `Weekday::Wednesday`，`days_in_year()` 因为 1992 是闰年而为 `366`。

**预期结果**：与 `lib.rs` 顶部的文档示例完全一致（见 [components/calendar/src/lib.rs:46-57](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/lib.rs#L46-L57)）。若运行环境 compiled data 正常，应能直接打印上述结果。

> 提示：`weekday()` 的实现是 `self.to_rata_die().into()`（[components/calendar/src/date.rs:310-313](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L310-L313)）——星期的本质是「绝对日序 mod 7」，所以它和你在哪个日历下看无关。如果你愿意本地验证，可以把同一个 ISO 日期转成 `Buddhist` 后再打印 `weekday()`，结果应当相同。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Date<A>` 里的泛型 `A` 为什么不直接写成具体日历类型 `C: Calendar`，而要引入 `AsCalendar` 这个中间 trait？

**参考答案**：因为日历对象可能不是零成本的（如 `Japanese` 带纪元数据），用户常常想用 `Rc<C>` / `Arc<C>` 共享同一个日历、避免克隆。`AsCalendar` 为 `Rc`/`Arc`/`Ref` 都提供了实现（见 [components/calendar/src/date.rs:39-93](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L39-L93)），让 `Date<Rc<Japanese>>` 成为可能；若写死 `C: Calendar`，就无法把这些包装类型塞进 `Date`。

**练习 2**：为什么 `try_new` 返回 `Result`，而 `from_rata_die` 不返回 `Result`？

**参考答案**：`try_new` 接收的是用户给的年/月/日，可能非法（如 2 月 30 日），需要返回 `DateNewError`；而 `from_rata_die` 接收的 RD 若超出有效范围，会被**钳制（clamp）**到 `VALID_RD_RANGE` 内部，而不是报错（见 [components/calendar/src/date.rs:254-258](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L254-L258)）。

---

### 4.2 具体日历实现：从 AbstractGregorian 理解 Gregorian / Iso / Buddhist

#### 4.2.1 概念说明

ICU4X 内置十几种日历（`Gregorian`、`Iso`、`Buddhist`、`Japanese`、`Hijri`、`Hebrew`、`Coptic`、`Ethiopian`、`Indian`、`Persian`、`Julian`、`Roc`、中式/韩式传统历……）。它们都实现了 `Calendar` trait，但写法分两类：

- **公历族**：`Gregorian`、`Iso`、`Buddhist`、`Roc` 等「月日规则与公历完全相同、只是年份/纪元不同」的日历。它们共享同一套「算术骨架」`AbstractGregorian`，各自只提供一个 `GregorianYears` 实现来描述「纪元码、extended year 偏移、era 换算」。由于它们与 ISO 的「(年,月,日)↔RD」映射完全一致，`has_cheap_iso_conversion()` 恒为 `true`。
- **非公历族**：`Hebrew`、`Hijri`、`Coptic` 等。它们的月份/闰年规则与公历不同，必须自己实现 RD 互转，`has_cheap_iso_conversion()` 为 `false`。

这一分类直接决定了 `to_calendar` 走哪条通道。

#### 4.2.2 核心流程

把一个日期从日历 A 转到日历 B 的决策（见 [components/calendar/src/date.rs:282-295](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L282-L295)）：

```
date.to_calendar(B)
   │
   ├─ 若 A、B 都 has_cheap_iso_conversion():
   │      B.from_iso(A.to_iso(inner))      # 快速通道：直接换 ISO 内部表示
   │
   └─ 否则:
          B.from_rata_die(A.to_rata_die(inner))  # 通用通道：经 RD 中转
```

两个公历族日历互转（如 `Gregorian → Buddhist`）走快速通道——这只是一次结构体字段搬运，几乎零成本。公历族与非公历族互转（如 `Gregorian → Hebrew`）则必须算 RD。

至于「佛历年 = ISO 年 + 543」，来源是 `BuddhistEra` 里的一个常量：

\[ \text{extended\_year} = \text{era\_year} + \text{EXTENDED\_YEAR\_OFFSET} = \text{be\_year} - 543 \]

由于公历族的 extended year 就是 ISO 年，所以 \(\text{be\_year} = \text{ISO\_year} + 543\)。ISO 1992 → 佛历 2535，与库文档示例吻合。

#### 4.2.3 源码精读

先看公历族共享骨架如何声明「ISO 快速通道」并实现 RD 互转：

[components/calendar/src/cal/abstract_gregorian.rs:165-188](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/abstract_gregorian.rs#L165-L188) —— `AbstractGregorian` 的 `from_rata_die`/`to_rata_die`/`has_cheap_iso_conversion`(=true)/`from_iso`/`to_iso`。`from_iso` 直接把 `IsoDateInner` 当作自己的内部表示，这就是「快速通道」的物理基础。

`Gregorian` 与 `Buddhist` 都只是用宏 `impl_with_abstract_gregorian!` 把上面这套实现套到自己身上，再各自提供「纪元换算」。先看 `Gregorian` 的纪元逻辑（CE/BCE）：

[components/calendar/src/cal/gregorian.rs:19-56](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/gregorian.rs#L19-L56) —— `CeBce` 实现 `GregorianYears`：`ad`/`ce` 视为正年，`bc`/`bce` 映射为 `1 - year`（没有 0 年），并按年份区间标注歧义等级。

再看 `Buddhist`——注意那个 `-543` 偏移：

[components/calendar/src/cal/buddhist.rs:34-61](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/buddhist.rs#L34-L61) —— `BuddhistEra` 实现 `GregorianYears`，`EXTENDED_YEAR_OFFSET = -543`，单一纪元码 `be`。

`Iso` 则最简单：单一 `default` 纪元、无偏移（[components/calendar/src/cal/iso.rs:25-57](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/iso.rs#L25-L57)）。

最后看 `to_calendar` 的两条通道决策，这是本模块的「枢纽」代码：

[components/calendar/src/date.rs:282-295](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L282-L295) —— `to_calendar`：双方都便宜转 ISO 时走 `from_iso(to_iso(..))`，否则走 `from_rata_die(to_rata_die(..))`。

库根的示例正好同时演示了这两条通道，可作为权威参照：

[components/calendar/src/lib.rs:59-84](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/lib.rs#L59-L84) —— ISO 日期转 `Indian`（1914-06-11）和 `Buddhist`（2535-09-02）。

#### 4.2.4 代码实践

**目标**：把一个 ISO 日期分别转成 `Buddhist`（快速通道）和 `Hebrew`（RD 通道），观察年份差异，验证两条路径殊途同归。

**操作步骤**（**示例代码**，接 4.1.4 的项目，补充依赖：在 `Cargo.toml` 的 `[dependencies]` 里 `icu = { version = "2.0.0", features = ["unstable"] }`，因为 `Hebrew` 等日历类型在 `cal` 下需要 `unstable` feature；若不愿开 feature，可改用同为公历族的 `Roc`）：

```rust
use icu::calendar::cal::{Buddhist, Hebrew};
use icu::calendar::Date;

fn main() {
    let iso = Date::try_new_iso(1992, 9, 2).unwrap();

    // 快速通道：公历族 -> 公历族
    let be = iso.to_calendar(Buddhist);
    println!("Buddhist year = {}", be.era_year().year); // 2535

    // RD 通道：公历族 -> 非公历族
    let he = iso.to_calendar(Hebrew);
    println!("Hebrew extended_year = {}", he.year().extended_year());

    // 殊途同归：转一圈回来应与原值相等
    assert_eq!(iso, be.to_calendar(icu::calendar::Iso).to_calendar(icu::calendar::cal::Indian)
                   .to_calendar(icu::calendar::Iso));
}
```

**需要观察的现象**：`Buddhist year` 打印 `2535`（= 1992 + 543）；`Hebrew` 的 extended_year 是一个与公历差异很大的数字；最后那条「转一圈」的断言通过。

**预期结果**：`assert_eq!` 不 panic，证明两条转换通道结果一致。这正是 [components/calendar/src/date.rs:742-750](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/date.rs#L742-L750) 里 `test_to_calendar` 测试所验证的性质。若未开启 `unstable` feature 无法编译 `Hebrew`，可用 `Roc` 替代观察快速通道，RD 通道部分标注**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Buddhist` 不需要自己写 `from_rata_die`/`to_rata_die`，却能正确换算？

**参考答案**：因为它通过 `impl_with_abstract_gregorian!(Buddhist, …)` 复用了 `AbstractGregorian` 的实现（[components/calendar/src/cal/buddhist.rs:34](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/buddhist.rs#L34)）。公历族的「(年,月,日)↔RD」映射对所有成员都一样，唯一不同的是「年份怎么标」——这部分由各自的 `GregorianYears`（`EXTENDED_YEAR_OFFSET` 与 era 换算）提供。

**练习 2**：`Gregorian` 把 `bc`/`bce` 映射成 `1 - year`（见 [components/calendar/src/cal/gregorian.rs:28](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/cal/gregorian.rs#L28)）。为什么是 `1 - year` 而不是 `-year`？

**参考答案**：因为传统公元纪元**没有 0 年**——公元前 1 年（BCE 1）紧接着公元 1 年（CE 1）。而 extended year 是带 0 年的算术序列（CE 1 = extended 1，CE 1 的前一年 = extended 0）。所以 BCE 的 `year`（1, 2, 3…）要映成 extended 的 `0, -1, -2…`，公式正是 `1 - year`。

---

### 4.3 AnyCalendar 与运行时日历转换

#### 4.3.1 概念说明

到目前为止，`Date<Iso>`、`Date<Buddhist>` 的日历类型都写死在**编译期**。但很多场景下，日历要等运行时才能确定——典型例子是解析一个带 `-u-ca-japanese` 扩展的 locale，或从用户配置里读到一个日历名字符串。这时你既不想用一个巨大的 `match` 在每种 `Date<X>` 之间切换，也无法把它们放进同一个容器（类型不同）。

`AnyCalendar` 解决的就是这个问题：它是一个**枚举**，每个变体包一种具体日历；它自己实现了 `Calendar`，把所有方法**分发**到内部那个具体日历上。于是 `Date<AnyCalendar>` 成为一个「运行时多态」的日期容器。配套的 `IntoAnyCalendar` trait 让任意具体日历能 `.to_any()` 擦除成 `AnyCalendar`，`AnyCalendarKind` 则是一个轻量枚举，用来「按名字」挑选日历。

代价是什么？`AnyCalendar` 是个较大的枚举（含数据，如 `Japanese` 带纪元表），不再像 `Iso` 那样是零字节零成本类型；并且不同变体之间**没有全序**（无法对「一个佛历日期」和「一个回历日期」比大小）。

#### 4.3.2 核心流程

「从一个 locale 字符串得到一个运行时日历」的典型路径：

```
locale!("en-u-ca-japanese")
        │  .into()   // Locale -> CalendarPreferences
        ▼
AnyCalendarKind::new(prefs)        # 解析 -u-ca- 扩展 -> AnyCalendarKind::Japanese
        │
        ▼
AnyCalendar::new(kind)             # 由 kind 构造具体日历（compiled_data）
        │
        ▼
gregorian_date.to_calendar(any)    # 把一个具体 Date 转成 Date<AnyCalendar>
        .to_any()
```

`AnyCalendarKind` 与 CLDR 的 [Unicode calendar identifier](https://unicode.org/reports/tr35/#UnicodeCalendarIdentifier) 一一对应（`buddhist`、`gregory`、`iso8601`、`japanese`、`hebrew`、`persian`…）。遇到无法识别的算法时，`AnyCalendarKind::new` 会回退到 `Gregorian`。

#### 4.3.3 源码精读

`AnyCalendar` 与其内部日期 `AnyDateInner` 都由一个宏批量生成——每个变体对应一种具体日历：

[components/calendar/src/any_calendar.rs:342-409](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L342-L409) —— `make_any_calendar!` 宏的调用点：列出全部日历变体（`Buddhist(Buddhist)`、`Gregorian(Gregorian)`、`HijriUmmAlQura(...)` 等），生成 `AnyCalendar` 枚举。

`AnyCalendar` 实现 `Calendar` 时，每个方法都是一个大 `match`，把请求分发给内部具体日历——以 `to_rata_die` 为例（其余方法同理）：

[components/calendar/src/any_calendar.rs:129-137](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L129-L137) —— `to_rata_die`：按 `(self, date)` 两个枚举配对分发；若日历类型与日期内部类型不匹配（仅 misuse `from_raw` 才会发生），则 panic。

由 `kind` 构造 `AnyCalendar` 的入口（默认走 compiled data）：

[components/calendar/src/any_calendar.rs:430-480](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L430-L480) —— `AnyCalendar::new(kind)`：对每个 `AnyCalendarKind` 变体返回对应具体日历（如 `Ethiopian` 还要选 era style，`Hijri` 要选算法）。

`AnyCalendarKind` 枚举本身及其「从偏好解析」的构造器：

[components/calendar/src/any_calendar.rs:669-756](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L669-L756) —— `AnyCalendarKind` 枚举（每个变体注明对应的 CLDR calendar id）与 `AnyCalendarKind::new(prefs)`，无法识别时回退 `Gregorian`。

「字符串名 → kind」的实际映射发生在 `TryFrom<CalendarAlgorithm>`，它把 CLDR 算法名（如 `gregory`、`buddhist`、`islamic-umalqura`）翻译成 kind：

[components/calendar/src/any_calendar.rs:758-791](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L758-L791) —— `TryFrom<CalendarAlgorithm> for AnyCalendarKind`：注意 `Hijri(None)`（只写了 `islamic` 没指定子算法）会返回 `Err(())`，从而在 `new()` 里回退到 `Gregorian`。

最后，把一个具体 `Date` 类型擦除成 `Date<AnyCalendar>`：

[components/calendar/src/any_calendar.rs:849-857](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L849-L857) —— `Date<C>::to_any()`：把内部日期与日历都转成 `AnyDateInner` / `AnyCalendar`。

#### 4.3.4 代码实践

**目标**：从一个 **字符串**（locale 扩展）在运行时选出日历，构造 `AnyCalendar`，并把一个 Gregorian 日期转成该日历下的 `Date<AnyCalendar>`。

**操作步骤**（**示例代码**）：

```rust
use icu::calendar::{AnyCalendar, AnyCalendarKind, Date, Gregorian};
use icu::locale::locale;

fn main() {
    // 1) 从字符串（带 -u-ca- 扩展）在运行时确定日历
    let cal_name = "buddhist"; // 假装这是运行时读到的名字
    let loc = format!("en-u-ca-{cal_name}");
    let loc: icu::locale::Locale = loc.parse().expect("合法 locale");
    let kind = AnyCalendarKind::new(loc.into()); // -> Buddhist
    let any_cal = AnyCalendar::new(kind);

    // 2) 构造一个 Gregorian 日期，转到运行时日历下
    let g = Date::try_new_gregorian(1992, 9, 2).unwrap();
    let any_date = g.to_calendar(any_cal).to_any(); // Date<AnyCalendar>

    println!("{any_date:?}");          // 调试输出会显示日历类型
    println!("year = {}", any_date.year().extended_year()); // 2535

    // 3) 直接用 kind 变体也行（编译期就知道名字，但走同一个 AnyCalendar）
    let direct = AnyCalendar::new(AnyCalendarKind::Buddhist);
    let any_date2 = g.to_calendar(direct).to_any();
    assert_eq!(any_date, any_date2);
}
```

**需要观察的现象**：`{any_date:?}` 打印的调试串里带有 `AnyCalendar (Buddhist)` 字样（来自 `debug_name`，见 [components/calendar/src/any_calendar.rs:270-276](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L270-L276)）；`extended_year()` 为 `2535`。

**预期结果**：两条构造路径得到的 `any_date` 与 `any_date2` 相等，断言通过。把 `cal_name` 改成 `"japanese"`、`"hebrew"`、`"persian"` 等，应得到对应日历下的不同年份。若某日历（如某些 `islamic-*`）数据缺失或解析失败，`AnyCalendarKind::new` 会回退到 Gregorian，年份会保持 1992——这是一个值得观察的回退现象。**待本地验证**具体各日历的输出数字。

#### 4.3.5 小练习与答案

**练习 1**：`Date<AnyCalendar>` 为什么没有实现 `Ord`（全序）？

**参考答案**：因为 `AnyDateInner` 的不同变体之间无法比较（一个佛历内部日期和一个回历内部日期没有可比性）。源码里 `AnyDateInner` 的 `PartialOrd` 在变体不匹配时返回 `None`（见 [components/calendar/src/any_calendar.rs:51-61](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L51-L61)），因此只能 `PartialOrd` 而无法 `Ord`。这其实是有用的：它告诉你「跨日历比大小没有意义」。

**练习 2**：`AnyCalendarKind::new` 在解析失败时回退到 `Gregorian`。这个回退点在源码哪里？为什么这样设计而非报错？

**参考答案**：在 [components/calendar/src/any_calendar.rs:750-755](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L750-L755)，`resolved_algorithm().try_into().unwrap_or(Self::Gregorian)`。设计成回退而非报错，是因为 `AnyCalendarKind::new` 常被用在「给定任意 locale 都要能拿出一个可用日历」的流水线里（如日期格式化），让数据缺失不至于让整个调用链失败；调用方若需要严格区分，可自行先检查 `resolved_algorithm()`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个迷你任务（**示例代码**）：

> **任务**：写一个函数 `describe(iso_year, iso_month, iso_day, cal_name: &str)`，它在内部用一个 `Date<Iso>`，依次完成：
>
> 1. 用 `try_new_iso` 构造日期，打印它的 `weekday()` 和公历 `era_year().year`。
> 2. 把它转成 **Buddhist** 日历，打印佛历年份（验证 = ISO + 543）。
> 3. 根据传入的 `cal_name`（如 `"japanese"`、`"hebrew"`、`"persian"`）用 `AnyCalendarKind::new` 选出运行时日历，把日期转成 `Date<AnyCalendar>`，打印它的 `debug_name`（通过 `calendar()`）和 `extended_year()`。
> 4. 最后把 `Date<AnyCalendar>` 再 `.to_calendar(Iso)` 转回 ISO，断言它等于第 1 步的原始日期（验证往返一致）。

参考实现骨架：

```rust
use icu::calendar::{AnyCalendar, AnyCalendarKind, Date, Iso};
use icu::locale::Locale;

fn describe(y: i32, m: u8, d: u8, cal_name: &str) {
    let iso = Date::try_new_iso(y, m, d).unwrap();
    println!("ISO: weekday={:?}, year={}", iso.weekday(), iso.era_year().year);

    let be = iso.to_calendar(icu::calendar::cal::Buddhist);
    println!("Buddhist year={} (期望 {})", be.era_year().year, y + 543);

    let loc: Locale = format!("en-u-ca-{cal_name}").parse().unwrap();
    let any_cal = AnyCalendar::new(AnyCalendarKind::new(loc.into()));
    let any_date = iso.to_calendar(any_cal).to_any();
    println!("AnyCalendar kind={:?}, extended_year={}",
             any_date.calendar().kind(),
             any_date.year().extended_year());

    let back = any_date.to_calendar(Iso);
    assert_eq!(iso, back, "往返应保持一致");
}
```

**验收标准**：
- 对 `describe(1992, 9, 2, "buddhist")`，Buddhist 年份打印 `2535`。
- 断言 `iso == back` 始终成立（这是 `to_rata_die`/`from_rata_die` 往返性质保证的）。
- 能解释：为什么 `cal_name = "islamic"`（不带子算法）时，`kind` 会回退成 `Gregorian`、extended_year 仍是 1992。

**待本地验证**：各日历的具体 extended_year 数值；若运行环境缺少某日历的 compiled data，相关构造可能回退，请以本地实际输出为准。

## 6. 本讲小结

- **`Calendar` 是密封的策略 trait**，每个日历只负责「(年,月,日) ↔ Rata Die / ISO」的互转与若干查询；`Date<A>` 只是把「某日历下的内部日期」与「日历对象」打包，所有用户 API 都是转发。
- **Rata Die 是日历换算的中立中介**：`to_calendar` 在双方都 `has_cheap_iso_conversion()` 时走「ISO 快速通道」（公历族互转，近乎零成本），否则走「RD 通用通道」。
- **公历族共享 `AbstractGregorian` 骨架**：`Gregorian`/`Iso`/`Buddhist`/`Roc` 的月日规则相同，差异只在纪元与年份换算；佛历的 `EXTENDED_YEAR_OFFSET = -543` 直接造就了「佛历年 = ISO 年 + 543」。
- **`AnyCalendar` 用枚举分发实现运行时多态**：`to_any()` 做类型擦除，`AnyCalendarKind`（对应 CLDR calendar id）按名字挑日历，`AnyCalendarKind::new(locale)` 解析 `-u-ca-` 扩展并在失败时回退 `Gregorian`。
- **代价**：`Date<AnyCalendar>` 跨变体不可全序比较（只能 `PartialOrd`），且枚举比 `Iso` 这类零成本类型更重——运行时多态并非免费。
- **`DateTime<A>` 不在 `icu_calendar`** 而在 `icu::time`，是 `Date<A>` + `Time` 的组合，仅用于展示，不带算术/全序。

## 7. 下一步学习建议

- **深入换算的底层算法**：RD 互转真正用到的是 `utils/calendrical_calculations` crate（Reingold-Dershowitz 算法）。这正是 [u8-l4 时间与日历算法工具链](u8-l4-time-and-calendar-utils.md) 的主题，读完会对 `fixed_from_gregorian` 等函数有完整认识。
- **回到格式化**：现在你已经理解了 `Date<A>`，可以回头重读 [u3-l1](u3-l1-datetime-formatting.md)，体会 `DateTimeFormatter` 为何要按「日历 + fieldset」两个维度组织，以及 `FixedCalendarDateTimeFormatter` 如何把日历类型编进签名。
- **数据视角**：`Japanese` 这类日历需要纪元数据，`AnyCalendar::try_new_unstable` 需要一个 `DataProvider`（见 [components/calendar/src/any_calendar.rs:535-582](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/calendar/src/any_calendar.rs#L535-L582)）。学完 [u5 数据提供器系统](u5-l1-dataprovider-core.md) 后，你会明白这背后那套「compiled data vs 显式 provider」的机制。
- **解析入口**：现实里日期常来自字符串。建议接着看 [u8-l4](u8-l4-time-and-calendar-utils.md) 提到的 `ixdtf`（RFC 9557 解析），它能把 `"2025-06-24T10:00:00[America/New_York][u-ca=gregory]"` 直接解析成带日历/时区的日期，是连接本讲与真实数据源的桥梁。
