# 搭建环境与运行第一个 ICU4X 应用

## 1. 本讲目标

学完本讲后，你应该能够：

- 检查并确认本机的 Rust 工具链满足 ICU4X 的要求，理解 `rust-toolchain.toml` 与 MSRV 的含义。
- 用 `cargo new` 创建一个二进制应用，并通过 `cargo add icu` 引入 ICU4X 元 crate。
- 独立写出并跑通一个「用 `DateTimeFormatter` 把日期格式化成指定 locale 文本」的示例程序。
- 看懂这段示例背后的最小数据流：locale 输入 → 格式化器构造（加载 compiled data）→ `Date` 构造 → `format` 输出。

本讲是「动手」讲义：阅读完成后，你手里应当有一个真正能 `cargo run` 的 ICU4X 小程序。

## 2. 前置知识

在继续之前，请确认你具备以下基础（本讲不会从零教 Rust 语法）：

- **Rust 基础语法**：能看懂 `use`、`fn main()`、`let`、`Result` 与 `.expect()`。官方 [Rust Book](https://doc.rust-lang.org/book/) 是最好的入门材料。
- **终端与 cargo**：会在命令行执行 `cargo --version`、`cargo new`、`cargo run`。
- **承接 u1-l2 的认知**：你已经知道 ICU4X 是一个 Cargo **工作区（workspace）**，`icu` 是一个「元 crate」——它本身没有逻辑，只是把 `icu_calendar`、`icu_datetime`、`icu_locale` 等组件 re-export 成 `icu::*` 模块，方便外部用户一行依赖就能用上全部组件。本讲会反复用到这一点。

还需要一个新概念：**MSRV（Minimum Supported Rust Version，最低支持 Rust 版本）**。它指的是「能编译通过这个 crate 的最低 Rust 版本」。ICU4X 在 `Cargo.toml` 里用 `rust-version` 字段声明 MSRV，同时在仓库根目录用 `rust-toolchain.toml` 固定「开发者推荐使用的工具链」。两者的区别是本讲的一个重点。

> 名词速查：**compiled data**（编译期内置数据）指 ICU4X 默认把一批 CLDR locale 数据直接「编译进二进制」，这样构造格式化器时无需额外提供数据文件。它是 `icu` 元 crate 的默认行为，本讲的示例就依赖它。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [tutorials/quickstart.md](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md) | 官方入门教程，本讲主线依据。从环境检查一路带到日期格式化。 |
| [rust-toolchain.toml](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/rust-toolchain.toml) | 仓库根目录的工具链固定文件，声明本仓库开发用的 Rust channel。 |
| [Cargo.toml](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml)（根） | 工作区根清单，声明 `version` 与 `rust-version`（MSRV）。 |
| [components/icu/Cargo.toml](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml) | `icu` 元 crate 的清单，展示它如何依赖并聚合各组件。 |
| [components/icu/examples/work_log.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/examples/work_log.rs) | 仓库内真实示例：用 `FixedCalendarDateTimeFormatter` 格式化一批日期。 |
| [examples/rust.md](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/examples/rust.md) | 仅一行，把读者重定向到 `components/icu/examples/` 目录。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① 环境与工具链要求 → ② cargo new 与依赖添加 → ③ 第一个日期格式化示例解析**。三者构成一条从「环境就绪」到「程序跑通」的流水线。

### 4.1 环境与工具链要求

#### 4.1.1 概念说明

要跑 ICU4X，你首先得有一份合适的 Rust 工具链。这里有两套互相独立的「版本约束」，初学者很容易混淆：

1. **MSRV（最低支持版本）**：ICU4X 承诺「不低于这个版本就能编译」。它是给**使用者**的承诺，写在 `Cargo.toml` 的 `rust-version` 字段里。你的工具链只要 ≥ MSRV 即可。
2. **仓库开发工具链**：ICU4X 的**贡献者**在该仓库里开发、跑 CI 时统一使用的版本，由仓库根的 `rust-toolchain.toml` 固定。`rustup` 进入该目录会自动切换到这个 channel。

一句话区分：MSRV 是「下限」，`rust-toolchain.toml` 是「仓库内部默认值」。作为外部用户，你通常只需关心 MSRV；只有当你 clone 整个仓库来开发时，`rust-toolchain.toml` 才会自动生效。

#### 4.1.2 核心流程

```text
打开终端
   │
   ▼
cargo --version            ← 确认 cargo 已安装、看版本号
   │
   ▼
进入 ICU4X 仓库目录(可选)    ← rustup 读 rust-toolchain.toml，自动切到 channel=1.95
   │
   ▼
若只在普通项目里用 icu       ← 只要你的 Rust ≥ MSRV(1.88) 即可，无需匹配 1.95
```

#### 4.1.3 源码精读

先看仓库开发用的工具链固定文件：[rust-toolchain.toml:5-6](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/rust-toolchain.toml#L5-L6) 声明 `channel = "1.95"`。它告诉 rustup：在本仓库里统一使用 Rust 1.95 工具链。

```toml
[toolchain]
channel = "1.95"
```

再看 MSRV，它写在工作区根清单里：[Cargo.toml:108-110](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L108-L110)。`rust-version = "1.88"` 就是 MSRV，`version = "2.2.0"` 是当前 `icu` 系列组件的工作区版本号（所有子 crate 通过 `version.workspace = true` 继承它）。

```toml
[workspace.package]
version = "2.2.0"
rust-version = "1.88"
```

官方 quickstart 的「Requirements」一节也是从检查 `cargo` 开始的：[tutorials/quickstart.md:11-21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md#L11-L21)。它假设你已安装 rust 与 cargo，并给出了验证命令的样例输出：

```console
cargo --version
# cargo 1.86.0 (adf9b6ad1 2025-02-28)
```

注意 quickstart 给的 `1.86.0` 只是「样例输出」，不是硬性要求——真正的要求是上面的 MSRV 1.88。

#### 4.1.4 代码实践

1. **实践目标**：确认本机工具链可用，并理解两套版本约束的差异。
2. **操作步骤**：
   - 在终端执行 `cargo --version`，记下版本号。
   - 用 `rustc --version` 看 Rust 编译器版本。
3. **需要观察的现象**：两条命令都能正常打印版本，没有「command not found」。
4. **预期结果**：版本号 ≥ 1.88（MSRV）。如果你在本讲后面 `cargo add icu` 拉取的发布版与上述工作区版本不一致，以 crates.io 上实际拉到的版本号为准。
5. 若你的版本低于 MSRV，用 `rustup update stable` 升级。

#### 4.1.5 小练习与答案

**练习 1**：MSRV 和 `rust-toolchain.toml` 里的 channel，哪一个是给库使用者的承诺？

**参考答案**：MSRV（`Cargo.toml` 的 `rust-version`）是给使用者的承诺，表示「≥ 这个版本就能编译」；`rust-toolchain.toml` 的 channel 是仓库贡献者开发时统一使用的版本，rustup 进入目录会自动切换，但它对纯使用者不是硬性要求。

**练习 2**：本仓库根 `rust-toolchain.toml` 固定的 channel 是多少？MSRV 又是多少？

**参考答案**：channel = `1.95`；MSRV = `1.88`（见根 `Cargo.toml` 的 `rust-version`）。

---

### 4.2 cargo new 与依赖添加

#### 4.2.1 概念说明

ICU4X 对外暴露的「门面」是元 crate `icu`。回忆 u1-l2：`icu` 自身不含算法，它只是把 `icu_datetime`、`icu_calendar`、`icu_locale` 等组件 re-export 成 `icu::datetime`、`icu::calendar`、`icu::locale` 模块。因此「使用 ICU4X」在最简情况下等价于「依赖 `icu` 这一个 crate」。

引入它的标准方式不是手编 `Cargo.toml`，而是用 cargo 自带的 `cargo add` 子命令——它会自动查询 crates.io、写入正确的版本与来源。这一步也是 quickstart 第二节的核心。

#### 4.2.2 核心流程

```text
cargo new --bin myapp     # 生成 src/main.rs 与 Cargo.toml 的二进制骨架
cd myapp
cargo add icu             # 自动把 icu = "<version>" 写入 [dependencies]
# 之后的代码里即可 use icu::datetime::...
```

#### 4.2.3 源码精读

quickstart 第二节给出完整步骤：[tutorials/quickstart.md:23-36](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md#L23-L36)。

```console
cargo new --bin myapp
cd myapp
cargo add icu
```

为什么一行 `cargo add icu` 就够？看元 crate 自己的清单 [components/icu/Cargo.toml:23-45](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L23-L45)：它的 `[dependencies]` 里几乎把所有组件都拉了进来——

```toml
[dependencies]
icu_calendar = { workspace = true, features = ["alloc"] }
icu_casemap = { workspace = true }
icu_collator = { workspace = true }
icu_collections = { workspace = true }
icu_datetime = { workspace = true }
icu_decimal = { workspace = true, features = ["alloc"]  }
icu_list = { workspace = true, features = ["alloc"] }
icu_locale = { workspace = true }
icu_normalizer = { workspace = true }
icu_plurals = { workspace = true }
icu_properties = { workspace = true, features = ["alloc"] }
icu_segmenter = { workspace = true }
icu_time = { workspace = true, features = ["alloc"] }
icu_experimental = { workspace = true, optional = true }
icu_pattern = { workspace = true, optional = true }
```

也就是说，依赖 `icu` 之后，`icu::datetime`、`icu::calendar`、`icu::locale` 等模块就都在作用域里了（`icu_experimental`、`icu_pattern` 是 `optional`，需要额外开启 feature 才会启用，本讲用不到）。这正是「元 crate」的价值：**一行依赖，换回全套组件的统一命名空间**。

#### 4.2.4 代码实践

1. **实践目标**：亲手创建一个依赖 `icu` 的空项目，观察生成的 `Cargo.toml`。
2. **操作步骤**：
   - 选一个空目录，执行 `cargo new --bin myapp` 然后 `cd myapp`。
   - 执行 `cargo add icu`。
   - 打开生成的 `Cargo.toml`，查看 `[dependencies]` 段。
3. **需要观察的现象**：`Cargo.toml` 的 `[dependencies]` 下出现一行形如 `icu = "2.x"` 的条目。
4. **预期结果**：`cargo add` 会拉取 crates.io 上 `icu` 的最新发布版（仓库工作区版本为 2.2.0，crates.io 实际版本以拉到的为准）。运行 `cargo build` 应能成功下载并编译依赖。
5. 如果处于离线环境或 crates.io 不可达，编译会卡在下载阶段——这属于环境问题，不是代码问题。

#### 4.2.5 小练习与答案

**练习 1**：为什么不建议直接 `use icu_datetime::...`，而是 `use icu::datetime::...`？

**参考答案**：因为 `icu` 元 crate 已经把 `icu_datetime` 等组件 re-export 成 `icu::datetime` 模块。用元 crate 的命名空间更省心——一次依赖就能拿到全部组件，且路径统一；直接依赖单个 `icu_datetime` 只在「只要这一个组件、想极致瘦身」时才有意义。

**练习 2**：`cargo add icu` 这条命令背后，cargo 帮你修改了哪个文件的哪个段落？

**参考答案**：它把 `icu = "<版本>"` 写入了项目 `Cargo.toml` 的 `[dependencies]` 段。

---

### 4.3 第一个日期格式化示例解析

#### 4.3.1 概念说明

这是本讲的高潮：跑通一个真正的国际化输出。示例会用到三个东西：

- **`Locale`**：locale 标识符（语言-文字-地区…），是几乎所有 ICU4X 组件的输入。它来自 `icu::locale`（底层是 `icu_locale_core` 组件）。
- **`DateTimeFormatter`**：日期时间格式化器。构造它需要一个 locale 和一个 **fieldset**（字段集合，声明要显示哪些字段，例如只显示年月日的 `YMD`）。
- **`Date`**：一个具体日期值，来自 `icu::calendar`。

构造格式化器需要**数据**（不同语言对「年月日」的写法、月份名等）。本讲依赖 `icu` 元 crate 默认开启的 **compiled data**——数据已编译进二进制，因此构造函数 `try_new` 对大部分常用 locale 都能成功，我们用 `.expect()` 直接断言它可用。

#### 4.3.2 核心流程

```text
1. 定义 Locale（用 locale! 宏在编译期解析，免运行期错误处理）
        │
        ▼
2. DateTimeFormatter::try_new(locale, YMD::long())
   └─ 从 compiled data 加载该 locale 的格式化数据 → 返回格式化器
        │
        ▼
3. Date::try_new_iso(year, month, day)   ← 构造一个 ISO 日历的日期值
        │
        ▼
4. dtf.format(&date)                     ← 得到「已格式化、待输出」的中间对象
        │
        ▼
5. println!("{}", formatted)             ← 触发 Writeable，产出本地化字符串
```

注意第 4 步返回的不是 `String`，而是一个「惰性」的格式化对象——它实现了 ICU4X 自定义的 `Writeable`（见 u6-l5），`println!` 时才真正生成文本，从而避免多余分配。初学阶段你只需把它当成「可以被 `{}` 打印的东西」。

#### 4.3.3 源码精读

quickstart 第四节的最终示例：[tutorials/quickstart.md:107-135](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md#L107-L135)。

```rust
use icu::locale::{Locale, locale};
use icu::calendar::Date;
use icu::datetime::{DateTimeFormatter, fieldsets::YMD};

const LOCALE: Locale = locale!("ja"); // let's try some other language

fn main() {
    let dtf = DateTimeFormatter::try_new(
        LOCALE.into(),
        YMD::long(),
    )
    .expect("ja data should be available");

    let date = Date::try_new_iso(2020, 10, 14)
        .expect("date should be valid");

    let formatted_date = dtf.format(&date);

    println!("📅: {}", formatted_date);
}
```

逐行解读关键点：

- `locale!("ja")`：编译期宏，把字符串解析成 `Locale` 常量，无需处理 `Result`。
- `DateTimeFormatter::try_new(LOCALE.into(), YMD::long())`：第一个参数是 locale（`.into()` 把 `Locale` 转成格式化器所需的 `DataLocale`），第二个参数 `YMD::long()` 是「年-月-日 + 长格式」的 fieldset。
- `Date::try_new_iso(2020, 10, 14)`：构造 ISO 日历日期。
- `dtf.format(&date)`：得到惰性格式化对象，打印即输出 `📅: 2020年10月14日`。

quickstart 对应的预期输出见 [tutorials/quickstart.md:131-135](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md#L131-L135)：

```text
📅: 2020年10月14日
```

除了 quickstart，仓库里还有真实示例可对照。`components/icu/examples/work_log.rs` 用 `FixedCalendarDateTimeFormatter` + `YMDT`（带时分）格式化一批日期：[work_log.rs:30-44](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/examples/work_log.rs#L30-L44)。

```rust
fn main() {
    let dtf = FixedCalendarDateTimeFormatter::try_new(locale!("en").into(), YMDT::medium())
        .expect("Failed to create FixedCalendarDateTimeFormatter instance.");
    ...
    for (idx, &(year, month, day, hour, minute, second)) in DATES_ISO.iter().enumerate() {
        let date = DateTime {
            date: Date::try_new_gregorian(year, month, day).expect("date should parse"),
            time: Time::try_new(hour, minute, second, 0).expect("time should parse"),
        };
        let fdt = dtf.format(&date);
        println!("{idx}) {}", fdt);
    }
}
```

> 提示：`DateTimeFormatter`（日历已擦除，能接受任意日历的日期，quickstart 用它）与 `FixedCalendarDateTimeFormatter<C, F>`（日历在类型上固定，work_log 示例用它）是两条平行的 API，本讲先用前者。它们的区别会在 u3-l1 / u3-l2 详细讲。

顺带一提，`examples/rust.md` 本身只有一行重定向：[examples/rust.md:1](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/examples/rust.md#L1)，把读者指向 `components/icu/examples/` 目录——上面这些真实示例都集中在那里。

#### 4.3.4 代码实践

这是本讲的主实践任务，对应官方 quickstart 的扩展。

1. **实践目标**：把 ISO 日期 `2025-06-24` 用 `es`（西班牙语）locale 格式化并打印，确认输出里含有 `junio`（西班牙语的「六月」）。
2. **操作步骤**：
   - 在 4.2 节创建好的 `myapp` 项目里，编辑 `src/main.rs` 为下面的代码（示例代码，基于 quickstart 模式改写）：

     ```rust
     use icu::locale::locale;
     use icu::calendar::Date;
     use icu::datetime::{DateTimeFormatter, fieldsets::YMD};

     const LOCALE: Locale_placeholder = locale!("es");

     fn main() {
         let dtf = DateTimeFormatter::try_new(
             LOCALE.into(),
             YMD::long(),
         )
         .expect("es data should be available");

         let date = Date::try_new_iso(2025, 6, 24)
             .expect("date should be valid");

         println!("📅: {}", dtf.format(&date));
     }
     ```

     ⚠️ 上面为了聚焦改动，把常量类型简写成了占位名 `Locale_placeholder`——**请把它改成正确的导入**：在第一行 `use` 里加上 `Locale`，即 `use icu::locale::{Locale, locale};`，并写 `const LOCALE: Locale = locale!("es");`。这是故意留给你的一处动手修正。
   - 执行 `cargo run`。
3. **需要观察的现象**：程序无 panic，打印出一行带 emoji 的本地化日期。
4. **预期结果**：`es` 的 `YMD::long()` 长格式下，`2025-06-24` 应输出形如 `📅: 24 de junio de 2025` 的文本——其中包含 `junio`。**输出文本以本地实际运行为准（待本地验证）**，只要出现 `junio` 即算成功。
5. 若想观察 `debug` 与 `release` 的差别，可追加一次 `cargo run --release`（quickstart 末尾也建议这样做以评估性能/体积）。

#### 4.3.5 小练习与答案

**练习 1**：把示例里的 `locale!("es")` 改成 `locale!("ja")` 并重新格式化 `2025-06-24`，输出会变成什么风格？（提示：对照 quickstart 的日语示例。）

**参考答案**：日语不区分大小写、用 `年/月/日` 汉字分隔，输出大致形如 `2025年6月24日`（具体文本以本地运行为准）。这印证了同一个 fieldset + 不同 locale 会产出完全不同的本地化文本。

**练习 2**：`try_new` 为什么返回 `Result`（要用 `.expect()`），而 `locale!` 宏不返回 `Result`？

**参考答案**：`locale!` 在**编译期**解析字符串，非法输入会直接编译失败，所以运行期无需 `Result`；而 `DateTimeFormatter::try_new` 要在**运行期**根据 locale 去加载 compiled data，万一数据缺失就会失败，所以返回 `Result`，需要 `expect`/`unwrap` 或 `match` 处理。

**练习 3**：`YMD::long()` 里的 `YMD` 和 `long` 分别控制什么？

**参考答案**：`YMD` 是 **fieldset**（字段集合），声明只显示「年-月-日」三个字段（不带时分）；`long` 是 **length** 选项，控制呈现长度（short/medium/long/full 之一），`long` 会用完整的月份名（如 `junio`）而非数字缩写。

## 5. 综合实践

把本讲三个模块串起来，完成一个「迷你 locale 探索器」：

1. 用 `cargo new --bin locale_explorer` 创建项目，`cargo add icu`。
2. 编写 `main.rs`：定义一个 locale（自选，例如 `es`、`ja`、`de`），用 `DateTimeFormatter` + `YMD::long()` 格式化 `2025-06-24` 并打印。
3. 把 locale 换成另外两种语言，各跑一次，把三行输出放在一起对比。
4. 在 `Cargo.toml` 里确认 `icu` 的版本号，并在仓库根 `Cargo.toml` 核对 `rust-version`（MSRV），写下「我用的 icu 版本 / MSRV / 本机 cargo 版本」三行记录。
5. （进阶）把 `YMD::long()` 改成 `YMD::short()`，观察输出如何从 `24 de junio de 2025` 这类长文本变成数字缩写形式。

这个任务把「工具链认知 → 依赖引入 → 格式化 API → fieldset/length 选项」整条链走了一遍。注意：所有输出文本均以本地实际运行为准。

## 6. 本讲小结

- ICU4X 有两套版本约束：**MSRV（`Cargo.toml` 的 `rust-version = "1.88"`）** 是给使用者的下限承诺；**`rust-toolchain.toml` 的 `channel = "1.95"`** 是仓库贡献者的默认工具链。
- 用 `cargo new --bin` + `cargo add icu` 即可引入 ICU4X；依赖一个元 crate `icu` 就能拿到 `icu::datetime`、`icu::calendar`、`icu::locale` 等全部组件命名空间。
- 第一个示例的最小数据流是：`locale!` → `DateTimeFormatter::try_new(locale, fieldset)` → `Date::try_new_iso(...)` → `dtf.format(&date)` → `println!`。
- `try_new` 返回 `Result` 是因为它要在运行期加载 compiled data；默认 compiled data 覆盖大部分常用 locale，所以常用 `.expect()` 断言成功。
- `YMD` 是 fieldset（决定显示哪些字段），`long` 是 length（决定呈现长度），二者共同决定输出形态。
- 评估性能/体积时别忘了 `cargo run --release`。

## 7. 下一步学习建议

本讲你跑通了「默认 compiled data」的示例。接下来：

- **想理解「数据从哪来、能否裁剪」**：继续读 quickstart 引用的 [data-management 教程](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/data-management.md) 与 `examples/cargo` 配置示例；这会直接衔接 **u5（数据提供器系统）** 单元。
- **想深入 Locale 模型**：进入 **u2-l1（Locale 与 LanguageIdentifier 数据模型）**，弄清 `locale!`、`Locale`、`DataLocale` 背后的类型。
- **想深入日期格式化**：进入 **u3-l1（DateTime 格式化旗舰组件）**，系统了解 `DateTimeFormatter` / `FixedCalendarDateTimeFormatter`、全部 fieldset 与 length 的组合，以及格式化流水线内部结构。
- 建议同时浏览 `components/icu/examples/` 目录（如 `work_log.rs`、`date_try_from_fields.rs`），它们是比 quickstart 更贴近真实用法的可运行范例。
