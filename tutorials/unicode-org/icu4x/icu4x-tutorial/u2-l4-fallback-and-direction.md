# Locale 回退链与文本方向性

## 1. 本讲目标

本讲承接 u2-l3（规范化与 likely subtags），讲解 `icu_locale` 里两个「把 locale 翻译成可用信息」的工具：

- **`LocaleFallbacker`**：当用户给的 locale（如 `de-CH`）在数据里没有精确条目时，按规则退回到最近的有数据的 locale（`de-CH → de → und`）。
- **`LocaleDirectionality`**：判断一个 locale 的文字是「从左到右」（LTR）还是「从右到左」（RTL），用于 UI 镜像布局。

学完本讲你应该能够：

1. 说清楚 locale 回退链的优先级（language / script / region）和逐步剥离的顺序。
2. 用 `LocaleFallbacker` 为任意 locale 生成并逐项遍历回退链。
3. 用 `LocaleDirectionality` 判断 `ar`、`he` 等语言的方向，并理解「方向是 script 的属性，不是 language 的属性」。

---

## 2. 前置知识

### 2.1 回顾：locale 子标签与 `und`

在 u2-l2 中我们学过 BCP-47 locale 由 `language-script-region-variant` 等子标签组成。其中语言为空时是特殊值 `und`（undetermined，未确定）。在 u2-l3 中我们学过 likely subtags 会把 `zh` 补全成 `zh-Hans-CN`。本讲会大量用到这两个概念。

### 2.2 为什么要回退

CLDR 不会为地球上每个 locale 都准备一份数据——那份数据会爆炸式增长。它的做法是：只存储「根数据」和有差异的分支，其余靠**回退（fallback）**推导。例如日期格式化数据可能有 `de`（标准德语），但没有 `de-CH`（瑞士德语）的独立条目。当用户请求 `de-CH` 时，ICU4X 必须能自动退到 `de`，再退到 `und`（根 locale）。

> 直觉：回退就是「如果这一层没有，就去问更通用的一层，直到根」。它让 ICU4X 用有限的数据覆盖无限的 locale 组合。

### 2.3 `DataLocale` 是什么

回退迭代器操作的不是 `Locale`，而是 `DataLocale`——一个 `Copy` 的、专门给数据管道用的精简 locale（见 u2-l1）。`Locale` 可以通过 `.into()` 转成 `DataLocale`。本讲示例里你会看到 `locale!("zh-TW").into()` 这样的写法。

---

## 3. 本讲源码地图

本讲涉及的关键文件分三层：配置、算法、适配器，再加上方向性组件。

| 文件 | 作用 |
| --- | --- |
| [provider/core/src/fallback.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/core/src/fallback.rs) | 回退**配置**类型 `LocaleFallbackConfig` / `LocaleFallbackPriority`。放在 provider/core 是因为数据 marker 元信息也要用到它。 |
| [components/locale/src/fallback/mod.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs) | `LocaleFallbacker` 核心 API：构造、绑定配置、创建迭代器。 |
| [components/locale/src/fallback/algorithms.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs) | 回退**算法**本体：`normalize` 与三种 `step_*` 步进逻辑。 |
| [provider/adapters/src/fallback/mod.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/adapters/src/fallback/mod.rs) | `LocaleFallbackProvider<P>`：把回退算法嵌入数据加载过程的「适配器」。 |
| [components/locale/src/directionality.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs) | `LocaleDirectionality` 与 `Direction`：判断 LTR/RTL。 |
| [components/locale/src/provider.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/provider.rs) | `ScriptDirection` 数据结构（LTR/RTL 两个 script 列表）。 |

> 小提示：`LocaleFallbacker` 在源码里标了 `#[doc(hidden)]`，因为它在 `icu_locale`/`icu::locale` 顶层重新导出。你写代码时直接用 `icu::locale::fallback::LocaleFallbacker` 或 `icu::locale::LocaleFallbacker` 即可。

---

## 4. 核心概念与源码讲解

### 4.1 LocaleFallbacker 与回退优先级

#### 4.1.1 概念说明

`LocaleFallbacker` 实现的是 [UTS #35: Locale Inheritance and Matching](https://www.unicode.org/reports/tr35/#Locale_Inheritance)。它的职责只有一个：给定一个输入 locale，按既定规则产出一个**从最具体到最通用**的 locale 序列，直到 `und`（根）。调用方（数据加载器）只需依次去数据里查这个序列，命中第一个就算成功。

回退行为受一个枚举控制——**优先级（priority）**，决定「先剥离哪个子标签」：

| 优先级 | 含义 | 典型用途 |
| --- | --- | --- |
| `Language`（默认） | 优先保住语言，先剥 region 再剥 script | 大多数本地化文本（日期、数字、消息） |
| `Script` | 优先保住 script，剥到 `und-Latn` 这种 | 与书写文字强相关的数据 |
| `Region` | 优先保住 region，剥到 `und-US` | 区域性数据（如行政区划、电话格式） |

例如 `en-US`：

- `Language` 优先级：`en-US → en → und`
- `Region` 优先级：`en-US → und-US → und`

#### 4.1.2 核心流程

`LocaleFallbacker` 的用法是一条流式调用链，分四步：

```
LocaleFallbacker::new()          // ① 加载 compiled 回退数据，得到 borrowed 句柄
    .for_config(config)          // ② 绑定一个 LocaleFallbackConfig
    .fallback_for(locale.into()) // ③ 以某个 DataLocale 起步，得到迭代器
    // ④ 循环：.get() 读当前 → .step() 前进一步
```

迭代器**不实现标准 `Iterator` trait**（因为 trait 不允许 item 从迭代器自身借用），所以用手动的 `.get()` / `.step()`。终止条件是 `it.get().is_unknown()`（当前等于 `und`）。

#### 4.1.3 源码精读

先看优先级枚举定义在 provider/core，因为它要被数据 marker 元信息共享：

[provider/core/src/fallback.rs:L14-L31](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/core/src/fallback.rs#L14-L31) — 定义 `LocaleFallbackPriority` 三个变体，注释直接说明了 `en-US` 在不同优先级下回退到何处。默认值是 `Language`（见 [fallback.rs:L33-L44](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/core/src/fallback.rs#L33-L44) 的 `default()`）。

接着看 `LocaleFallbacker` 本体持有的数据：

[components/locale/src/fallback/mod.rs:L78-L83](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L78-L83) — `LocaleFallbacker` 只有两块数据：`likely_subtags`（likely subtags 表，用于推断默认 script）和 `parents`（显式父级映射表，如 `es-AR → es-419`）。这说明回退算法是**数据驱动**的，规则表换了行为就变。

`for_config` 把配置绑到句柄上：

[components/locale/src/fallback/mod.rs:L184-L187](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L184-L187) — 一行转发到 borrowed 版本的 `for_config`。

`fallback_for` 创建迭代器，并在创建时立刻做一次 `normalize`：

[components/locale/src/fallback/mod.rs:L246-L262](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L246-L262) — 注意它在构造迭代器前先调用 `self.normalize(&mut locale, &mut default_script)`，并把 `max_script`（最大化后的 script）存进迭代器内部。`max_script` 在后续剥离 region 时用来决定是否要把 script「加回来」（见 4.2）。

迭代器的三个核心方法：

[components/locale/src/fallback/mod.rs:L265-L283](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L265-L283) — `get()` 借当前 locale，`take()` 拿走所有权，`step()` 推进一步（内部委托给 `algorithms.rs`）。`step` 的文档明确说：当前 locale 变成 `DataLocale::default()`（即 `und`）时回退结束。

#### 4.1.4 代码实践

**实践目标**：亲手为 `zh-TW` 生成回退链并逐项打印，验证「`zh-TW` 会先变成 `zh-Hant`（因为繁体的默认 script 是 Hant，需要显式保留）」。

**操作步骤**（承接 u1-l3 的 `cargo new --bin` 流程）：

1. 新建二进制项目并加依赖：`cargo add icu writeable`（`writeable` 用来把 `DataLocale` 转成字符串打印，见下方说明）。
2. 在 `src/main.rs` 写入：

```rust
// 示例代码：打印 zh-TW 的语言优先级回退链
use icu::locale::fallback::LocaleFallbacker;
use icu::locale::locale;
use writeable::Writeable; // DataLocale 实现的是 Writeable，不是 Display

fn main() {
    // ① 用 compiled 数据构造 fallbacker（返回 borrowed 句柄）
    let fallbacker = LocaleFallbacker::new();
    // ② 绑定默认配置（Language 优先级），③ 从 zh-TW 起步
    let mut it = fallbacker
        .for_config(Default::default())
        .fallback_for(locale!("zh-TW").into());

    // ④ 循环打印，直到 und（is_unknown）为止
    loop {
        let current = it.get();
        if current.is_unknown() {
            break;
        }
        println!("{}", current.write_to_string());
        it.step();
    }
}
```

> 说明：`DataLocale` 没有实现 `std::fmt::Display`，而是通过宏实现了 `Writeable`（见 [data.rs:L138](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs#L138)），所以打印需借助 `writeable::Writeable::write_to_string()`，这也正是 ICU4X 源码测试自己用的方式（见 [algorithms.rs:L534](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L534)）。

**预期结果**：

```
zh-TW
zh-Hant
```

（迭代器随后还会产出终止符 `und`，但被 `is_unknown()` 拦截不打印。）

**需要观察的现象**：`zh-TW` 没有直接退到 `zh`，而是退到 `zh-Hant`——因为 `zh` 的默认 script 是 `Hans`（简体），而台湾用的是 `Hant`（繁体），所以回退算法必须把 `Hant` 显式加回来，否则会错误地落到简体数据。这与 `zh-CN` 直接退到 `zh` 形成鲜明对比（见 4.2 的测试用例）。

#### 4.1.5 小练习与答案

**练习 1**：把上面的配置改成 `Region` 优先级（`config.priority = LocaleFallbackPriority::Region`），`zh-TW` 的回退链会变成什么？

**参考答案**：`zh-TW → und-TW → und`。Region 优先级会先剥离语言保住地区，变成 `und-TW`，再剥离地区到 `und`。这与 [algorithms.rs:L441](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L441) 的 `expected_region_chain: &["zh-TW", "und-TW"]` 一致。

**练习 2**：`LocaleFallbacker::new_without_data()`（[mod.rs:L166-L181](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L166-L181)）构造的 fallbacker 没有数据，它对 `zh-TW` 会产出什么回退链？为什么？

**参考答案**：只会产出 `zh-TW`（然后直接到 `und`）。因为没有 likely subtags 数据，算法无法知道 `zh` 的默认 script、也无法判断要不要加回 `Hant`，所以只能做最朴素的「剥 region → 剥 language」。源码注释也警告它在多 script 语言下行为「surprising」。

---

### 4.2 回退算法与数据驱动

#### 4.2.1 概念说明

回退分两个阶段：

1. **归一化（normalize）**：在迭代器**创建时**跑一次。它会剥掉「由其他子标签隐含」的冗余 script。例如 `zh-Hans` 会被归一化成 `zh`，因为 `Hans` 是 `zh` 的默认 script，写出来是多余的。所以**迭代器产出的第一个元素不一定等于输入 locale**。
2. **步进（step）**：每调一次 `.step()`，按优先级剥离一个子标签，产出一个更通用的 locale，直到 `und`。

步进的剥离顺序由优先级决定，且会被两张数据表影响：

- **likely subtags 表**：用于推断「这个语言的默认 script 是什么」，从而决定剥离 region 后要不要把 script 加回来。
- **parents 表（显式父级）**：CLDR 显式声明的「父子」关系，如 `es-AR → es-419`（阿根廷西语先退到拉美西语，再退到西语根）、`en-001 → en`。它能让回退走更贴近真实语言分群的中间层。

#### 4.2.2 核心流程

以默认的 `Language` 优先级 `step_language` 为例，剥离顺序自上而下（命中一条就 return，等下次 `step`）：

```
1. 剥掉 subdivision 关键字（u-sd-... 的行政区划）  → 备份
2. 剥掉 variant（如 valencia、fonipa）             → 备份
3. 查 parents 表，若有显式父级则跳到父级（如 es-AR→es-419）
4. 剥掉 region（剥之前先决定要不要把 script 加回来）
5. 剥掉 language + script → und（终止）
```

第 4 步「要不要加回 script」是关键：如果当前语言的默认 script（如 `zh`→`Hans`）不等于最大化时记下的 `max_script`（如 `Hant`），说明这是个「非默认 script」的语言社区，必须把 `Hant` 显式写出来，否则数据会落到错误的 script。这正是 `zh-TW → zh-Hant` 的由来。

#### 4.2.3 源码精读

先看归一化函数：

[components/locale/src/fallback/algorithms.rs:L11-L68](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L11-L68) — `normalize` 的第 2 步（[L46-L67](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L46-L67)）查 likely subtags 算出 `default_script`，若当前 script 等于它就置空——这就是 `zh-Hans` 归一化成 `zh` 的地方。

再看步进派发：

[components/locale/src/fallback/algorithms.rs:L72-L88](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L72-L88) — `step` 按 `priority` 派发到 `step_language` / `step_script` / `step_region`，对未知值兜底直接清成 `und`。

`step_language` 的核心（剥 region 前决定 script 去留）：

[components/locale/src/fallback/algorithms.rs:L109-L126](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L109-L126) — 比较 `language_implied_script`（语言默认 script）与 `self.max_script`：不相等就 `locale.script = self.max_script`（加回），相等就置空。然后 `locale.region = None`。

显式父级查询：

[components/locale/src/fallback/algorithms.rs:L232-L239](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L232-L239) — `get_explicit_parent` 用 `locale.strict_cmp` 在 `parents` 表里反查，命中则跳到父级 locale。这就是 `es-AR → es-419` 这类「走中间层」的实现。

这套算法如何被「免费」接到数据加载里？看适配器 `LocaleFallbackProvider`：

[provider/adapters/src/fallback/mod.rs:L57-L61](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/adapters/src/fallback/mod.rs#L57-L61) — 它只是一个「内层 provider + fallbacker」的包装。

核心循环在 `run_fallback`：

[provider/adapters/src/fallback/mod.rs:L139-L188](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/adapters/src/fallback/mod.rs#L139-L188) — 逻辑是：拿到 marker 的 `fallback_config`，生成回退迭代器；循环里把迭代器当前的 locale 塞进 `DataRequest` 去问内层 provider；若内层返回「标识未找到」（`allow_identifier_not_found` 的 `Ok(None)` 分支，[L172-L178](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/adapters/src/fallback/mod.rs#L172-L178)），就 `step()` 继续问下一个；命中则把命中的 locale 写进响应元数据返回（[L168-L171](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/adapters/src/fallback/mod.rs#L168-L171)）。

> 也就是说：上层组件（如 `DateTimeFormatter`）只要构造时用了 `LocaleFallbackProvider` 包裹的 provider，就自动获得「任意 locale 都能查到最近数据」的能力，完全不用自己写回退逻辑。这是 u5（数据提供器）会展开的主题，这里先建立直觉。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——阅读 `algorithms.rs` 的测试用例表，对照算法预言 `es-AR` 和 `hi-Latn-IN` 的语言优先级回退链，再理解为什么。

**操作步骤**：

1. 打开 [algorithms.rs:L259-L492](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L259-L492) 的 `TEST_CASES` 数组。
2. 找到 `es-AR` 用例（[L398-L404](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L398-L404)）：`expected_language_chain: &["es-AR", "es-419", "es"]`。
3. 找到 `hi-Latn-IN` 用例（[L412-L418](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L412-L418)）：`["hi-Latn-IN", "hi-Latn", "en-IN", "en-001", "en"]`。

**需要观察的现象 / 思考题**：

- `es-AR` 为什么中间多了一层 `es-419`？（答：parents 表显式声明了 `es-AR → es-419`，拉美西语有共享数据。）
- `hi-Latn-IN`（天城文转写的印地语）为什么会跳到 `en-IN`？（答：`hi-Latn` 这种「非默认 script」的语言社区没有独立数据，likely subtags 把它关联到了同样用拉丁字母、同地区的英语，于是回退跨语言到 `en-IN → en-001 → en`。这是 ICU4X 在 UTS #35 之外做的额外步骤，源码模块文档 [mod.rs:L17-L21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L17-L21) 有说明。）

**预期结果**：你能在不运行的情况下，凭算法和数据表推断出这些链，并与测试断言一致。如果想验证，可在 `components/locale` 目录下跑 `cargo test --lib test_fallback`（待本地验证具体命令）。

#### 4.2.5 小练习与答案

**练习 1**：`sr-ME`（黑山塞尔维亚语）的语言优先级链是什么？为什么不是 `sr-ME → sr`？

**参考答案**：`sr-ME → sr-Latn`（见 [algorithms.rs:L322-L327](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/algorithms.rs#L322-L327)）。因为塞尔维亚语默认 script 是 `Cyrl`（西里尔），但黑山用拉丁字母 `Latn`，属于非默认 script，所以剥 region 后必须加回 `Latn`，得到 `sr-Latn`，而不是直接 `sr`（那会落到西里尔数据）。

**练习 2**：为什么 `ca-ES-valencia`（瓦伦西亚加泰罗尼亚语）的回退链是 `ca-ES-valencia → ca-ES → ca-valencia → ca`，而不是剥一次 variant 就完事？

**参考答案**：见 [fallback.rs:L70-L80](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/provider/core/src/fallback.rs#L70-L80) 的官方文档示例。算法会先把 variant 备份，剥 region 得 `ca-valencia`（让「带 variant 但无 region」的共享数据有机会命中），再剥 variant 得 `ca`。这种「反复把备份的 variant 重新挂回」的设计，是为了让 `valencia` 这种有独立数据的 variant 在不同层级都能被查到。

---

### 4.3 LocaleDirectionality 判断 LTR/RTL

#### 4.3.1 概念说明

文本方向（LTR / RTL）决定了 UI 该不该镜像布局（按钮左右翻转、图标反向等）。一个关键直觉是：**方向是 script（书写文字）的属性，不是 language（语言）的属性**。

- `ar`（阿拉伯语）→ RTL，因为它用 `Arab` 文字。
- `en`（英语）→ LTR，因为 `Latn`。
- `en-Arab`（用阿拉伯字母写的英语）→ **RTL**，尽管语言是英语——决定方向的是 `Arab` 这个 script。

所以判断方向分两步：先从 locale 推出它的 likely script，再查这个 script 是 LTR 还是 RTL。

#### 4.3.2 核心流程

```
LocaleDirectionality::new_common()   // 加载 script 方向数据 + likely subtags
    .get(&langid!("ar"))             // 返回 Option<Direction>
            │
            ▼
   1. get_likely_script(langid)      // 推断 likely script（承接 u2-l3 的 expander）
   2. 在「RTL script 排序列表」二分查找
      → 命中：RightToLeft
      否则在「LTR script 排序列表」二分查找
      → 命中：LeftToRight
      否则：None（未知 script，如编造的 "foo"）
```

两个列表都是**已排序**的 `ZeroVec<UnvalidatedScript>`，所以查找复杂度为 \(O(\log n)\)（二分查找）。RTL script 数量很少（约十几个），LTR 占绝大多数。

#### 4.3.3 源码精读

方向枚举：

[components/locale/src/directionality.rs:L14-L21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L14-L21) — `Direction` 目前只有 `LeftToRight` 和 `RightToLeft` 两个变体，且标了 `#[non_exhaustive]`，未来可能扩展（如自上而下书写的文字）。

`LocaleDirectionality` 的字段：

[components/locale/src/directionality.rs:L36-L40](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L36-L40) — 它由两部分组成：`script_direction`（方向数据）和一个泛型 `expander`（默认 `LocaleExpander`，用来推断 likely script）。这正好对应「先推 script，再查方向」两步。

核心方法 `get`：

[components/locale/src/directionality.rs:L198-L208](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L198-L208) — 先 `get_likely_script`，再用 `script_in_ltr` / `script_in_rtl` 判定。文档注释（[L155-L165](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L155-L165)）明确点出「direction 是 script 的属性，不是 language 的属性」。

二分查找的实现：

[components/locale/src/directionality.rs:L240-L254](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L240-L254) — `script_in_rtl` / `script_in_ltr` 把 script 转成未校验形式，在排序列表上做 `binary_search`，命中即属于该方向。

数据结构：

[components/locale/src/provider.rs:L458-L465](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/provider.rs#L458-L465) — `ScriptDirection` 就两个字段：`rtl` 和 `ltr`，都是 `ZeroVec<UnvalidatedScript>`（零拷贝、紧凑存储，承接 u6 的 zerovec 主题）。

likely script 推断（复用 expander）：

[components/locale/src/expander.rs:L542-L546](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L542-L546) — `get_likely_script`：locale 已带 script 就直接用，否则按 language+region / language 推断。这和 u2-l3 的 `maximize` 是同一套 likely subtags 数据。

便捷方法 `is_right_to_left` / `is_left_to_right`：

[components/locale/src/directionality.rs:L217-L223](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L217-L223) — 返回 `bool`，比 `get` 更省事，但文档提醒：返回 `false` 可能是 LTR，也可能是「无数据」，二义性场景仍需用 `get`。

#### 4.3.4 代码实践

**实践目标**：用 `LocaleDirectionality` 判断 `ar`（阿拉伯语）、`he`（希伯来语）是否为 RTL，并验证 `en-Arab`（英语+阿拉伯字母）是 RTL。

**操作步骤**：在同一个 `cargo new --bin` 项目里（依赖 `icu`）：

```rust
// 示例代码：判断文本方向
use icu::locale::{langid, Direction, LocaleDirectionality};

fn main() {
    // 用 compiled 数据构造（common 版 likely subtags 已够用）
    let ld = LocaleDirectionality::new_common();

    for lid in [langid!("ar"), langid!("he"), langid!("en"), langid!("en-Arab")] {
        match ld.get(&lid) {
            Some(Direction::RightToLeft) => println!("{lid} 是 RTL"),
            Some(Direction::LeftToRight) => println!("{lid} 是 LTR"),
            None => println!("{lid} 方向未知"),
        }
    }
}
```

**预期结果**：

```
ar 是 RTL
he 是 RTL
en 是 LTR
en-Arab 是 RTL
```

**需要观察的现象**：`en-Arab` 虽然语言是英语（`en` 本身是 LTR），但因为用了 `Arab` 字母，结果是 RTL。这验证了「方向取决于 script」。若把 `new_common()` 换成 `new_extended()`（[directionality.rs:L77-L80](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L77-L80)），还能覆盖更多生僻语言（如 `jbn`，见 [L118-L127](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L118-L127) 的文档示例）。

#### 4.3.5 小练习与答案

**练习 1**：`ld.is_right_to_left(&langid!("foo"))`（`foo` 是编造的语言）返回什么？和 `ld.get(&langid!("foo"))` 的区别是什么？

**参考答案**：`is_right_to_left` 返回 `false`（推不出 likely script，`unwrap_or(false)`，见 [L217-L223](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/directionality.rs#L217-L223)）；而 `get` 返回 `None`。`false` 无法区分「LTR」和「未知」，所以需要区分时要用 `get`。

**练习 2**：为什么 `LocaleDirectionality` 内部要持有一个 `LocaleExpander`，而不是直接用 language 查方向？

**参考答案**：因为绝大多数 locale 不显式写 script（人们写 `ar` 而非 `ar-Arab`）。要查方向必须先知道 script，而 script 要靠 likely subtags 推断，这件事正是 `LocaleExpander::get_likely_script` 做的（[expander.rs:L542-L546](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L542-L546)）。所以方向性组件天然复用了 expander，而非重新实现一份 script 推断。

---

## 5. 综合实践

**任务**：写一个小程序，输入任意 locale 字符串，先打印它的**语言优先级回退链**，再打印它的**文本方向**，把本讲两个工具串起来。

**操作步骤**：

1. `cargo new --bin locale-inspect && cd locale-inspect && cargo add icu writeable`。
2. 写入下面的「示例代码」并 `cargo run`：

```rust
// 示例代码：回退链 + 方向 综合检查
use icu::locale::fallback::LocaleFallbacker;
use icu::locale::{Direction, LocaleDirectionality};
use writeable::Writeable;

fn inspect(input: &str) {
    // 解析输入 locale
    let locale: icu::locale::Locale = input.parse().expect("解析失败");

    // —— 第一部分：回退链 ——
    let fallbacker = LocaleFallbacker::new();
    let mut it = fallbacker
        .for_config(Default::default())
        .fallback_for(locale.into());
    print!("回退链: ");
    loop {
        if it.get().is_unknown() {
            break;
        }
        print!("{} ", it.get().write_to_string());
        it.step();
    }
    println!();

    // —— 第二部分：方向 ——
    // LocaleDirectionality 接受 &LanguageIdentifier。Locale 内嵌 id: LanguageIdentifier
    // （承接 u2-l1），所以直接取 locale.id 即可判断。
    let ld = LocaleDirectionality::new_common();
    let dir = match ld.get(&locale.id) {
        Some(Direction::RightToLeft) => "RTL",
        Some(Direction::LeftToRight) => "LTR",
        None => "未知",
    };
    println!("方向: {dir}");
}

fn main() {
    inspect("zh-TW");
    inspect("ar-EG");
}
```

> 第二部分用 `locale.id` 取出内嵌的 `LanguageIdentifier`（承接 u2-l1：`Locale { id: LanguageIdentifier, .. }`）交给 `LocaleDirectionality::get`。若你手头只有静态字符串而非解析得到的 `Locale`，也可直接用 `langid!("ar")` 调用 `get`。

**预期结果**（待本地验证）：

```
回退链: zh-TW zh-Hant
方向: LTR       # zh-Hant 用 Han 系文字，整体排版仍按 LTR 主轴；若实际为 TTB 请以本地输出为准
回退链: ar-EG ar
方向: RTL
```

> 说明：`zh-Hant` 的「方向」在 CLDR 里属于 LTR（中文整体行进方向是从左到右排版的现代用法）；若你的本地数据版本判定不同，以实际输出为准——重点是练习「回退链 + 方向」两条 API 链路的串联，而不是记住某个具体判定。

**进阶思考**：尝试把 `ar-EG` 的回退配置改成 `Region` 优先级，观察回退链如何从 `ar-EG → ar` 变成 `ar-EG → und-EG`，并体会优先级如何改变「最近数据」的含义。

---

## 6. 本讲小结

- **回退（fallback）** 是 ICU4X 用有限 CLDR 数据覆盖无限 locale 组合的核心机制：从最具体逐层退到 `und`（根）。
- `LocaleFallbacker` 的用法是 `new() → for_config() → fallback_for() → 循环 get()/step()`；迭代器不实现 `Iterator`，终止条件是 `is_unknown()`。
- **优先级（Language / Script / Region）** 决定先剥哪个子标签；算法是数据驱动的，依赖 likely subtags（推默认 script）和 parents（显式父级如 `es-AR → es-419`）两张表。
- 关键细节：**非默认 script 必须显式保留**（`zh-TW → zh-Hant`、`sr-ME → sr-Latn`），否则会落到错误 script 的数据；归一化会先剥掉冗余 script（`zh-Hans → zh`）。
- `LocaleFallbackProvider` 适配器把回退算法自动嵌入数据加载循环，上层组件无需手写回退。
- **`LocaleDirectionality`** 判断 LTR/RTL，但方向是 **script 的属性**：先 `get_likely_script`，再在已排序的 LTR/RTL script 列表上二分查找。

---

## 7. 下一步学习建议

- **向数据层深入**：本讲的 `LocaleFallbackProvider` 只是适配器之一。建议进入 **u5（数据提供器系统）**，系统学习 `DataProvider` / `DataMarker` 抽象，以及 `LocaleFallbackProvider`、`ForkByMarkerProvider`、`FilterDataProvider` 等如何像积木一样组合数据管道。
- **理解零拷贝地基**：回退数据和方向数据用的 `ZeroVec`、`DataPayload` 背后的 `Yoke`，将在 **u6（零拷贝基础工具）** 讲透。
- **回到组件实战**：掌握回退后，再读 **u3-l1（DateTime 格式化）**，你会更清楚「为什么传入 `de-CH` 也能格式化成功」——它正是默默走了回退链。
- **延伸阅读**：[UTS #35 Locale Inheritance](https://www.unicode.org/reports/tr35/#Locale_Inheritance) 与 ICU4X 的[回退设计文档](https://docs.google.com/document/d/1Mp7EUyl-sFh_HZYgyeVwj88vJGpCBIWxzlCwGgLCDwM/edit)（源码 [mod.rs:L20-L21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/fallback/mod.rs#L20-L21) 有链接），可了解 ICU4X 在 UTS #35 之外做的额外步骤（如 `hi-Latn` 跨语言回退）。
