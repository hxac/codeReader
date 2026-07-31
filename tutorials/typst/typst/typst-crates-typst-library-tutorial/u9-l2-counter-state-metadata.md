# Counter、State 与 Metadata

## 1. 本讲目标

本讲承接 u9-l1（Location / Locator / Tag / query / Context），进入 Typst 内省系统的「可变状态」部分。学完后你应该能够：

- 说清 `Counter` 为什么总是返回**多级数组**，以及它的值是如何被算出来的；
- 读懂 `Counter` / `State` 共用的「整条轨迹一次算完、按位置取偏移」的设计，并理解它为何把计算量从平方降到线性；
- 区分 `Counter`（只数多级整数、带 `step`、能自动编号显示）与 `State`（值类型任意、只有 `set/func` 两种更新）的边界；
- 解释 `MetadataElem` 这样一个极小元素如何成为「向 query 系统投放任意值」的通道；
- 把这三者统一到 u9-l1 建立的内省模型上：插入不可见元素 → 内省器索引 → 经 `engine.introspect(...)` 读值 → 收敛循环验证。

## 2. 前置知识

在读本讲前，请先确认你已掌握 u9-l1 中的几个关键概念：

- **Location / Tag**：locatable 元素的「身份证号」；`TagElem` 把位置盖章进帧树，内省器据此建索引。
- **Introspector**：在排版完成后提供统一查询接口（`query` / `query_count_before` / `page` 等）的对象。
- **Context 门禁**：`get` / `at` / `final` / `display` 这类「读当前值」的函数都要先从 `Context` 取到当前位置，没有 context 就会报「can only be used when context is known」。
- **收敛迭代**：第 N 轮排版时用户代码读到的是第 N-1 轮建好的索引，因此内省需要反复迭代才稳定（收敛循环本身在 u9-l3 详讲）。

还要回忆 u2-l1 的 `Value` 枚举：`State` 的值可以是任意 `Value`，而 `Counter` 的值永远是整数数组。另外 u3-l2/u3-l3 的「元素 + 能力（capability）」是本讲反复出现的工具——`CounterUpdateElem`、`StateUpdateElem`、`MetadataElem` 都是元素，且都带 `Locatable` 能力。

一条贯穿全讲的认知（与 u6/u7/u8 各讲一致）：**typst-library 只负责「定义元素 + 把数据归一化」，真正排版、回填 location、建索引的行为住在行为 crate，运行期经 `Routines` 回调。** 本讲的 `Counter`/`State`/`Metadata` 也遵守这条线——它们定义「状态如何被表示与查询」，而把状态算出来这件事，靠的是内省器（行为 crate 提供）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/introspection/counter.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L1-L996) | 计数器全部实现：`Counter`/`CounterKey`/`CounterState`/`CounterUpdate`/`Count` trait、`CounterUpdateElem`、`sequence` 求值、三种 `Introspect` 实现、页码计数器。 |
| [src/introspection/state.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L1-L525) | 通用状态机：`State`/`StateUpdate`/`StateUpdateElem`、`sequence` 求值、两种 `Introspect` 实现。结构与 counter 几乎对称。 |
| [src/introspection/metadata.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L1-L29) | 极小的 `MetadataElem`：把一个 `Value` 投放到文档里，供 `query` 检索。 |
| [src/introspection/convergence.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L1-L282) | `Introspect` trait 与 `History`，是 counter/state 的统一底座，也是 u9-l3 的入口。 |
| [src/introspection/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/mod.rs#L35-L45) | `define(global)` 把三者注册进标准库作用域。 |
| [src/model/heading.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L318) | 标题如何实现 `Count` 能力、如何在 `Synthesize` 里调 `display_at` 把编号回填——这是实践任务的核心调用链。 |

注册侧（[src/introspection/mod.rs:35-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/mod.rs#L35-L45)）把 `Counter`、`State` 注册为类型（`define_type`，因此它们有构造器 `counter(...)` / `state(...)`），把 `MetadataElem` 注册为元素（`define_elem`）。`here`/`query`/`locate` 三个函数同在此处注册，已在 u9-l1 讲过。

## 4. 核心概念与源码讲解

### 4.1 Counter：可多级编号的计数器

#### 4.1.1 概念说明

`Counter` 是 Typst 对「会随文档推进而变化的整数编号」的抽象：页码、标题号、图号、脚注号，以及用户自定义的计数器，都归它管。它的特点有三个：

1. **多级**：标题号可能是 `1.1.3` 这样多层，所以计数器值不是单个数，而是一个**整数数组**，每一层一个元素。
2. **上下文相关（contextual）**：值随位置变化，读值需要 context。
3. **按排版顺序更新**：更新发生在内容被排进文档的位置，而非代码求值的位置。

#### 4.1.2 核心流程

读一个计数器在某位置的值，背后是统一的「整条轨迹 + 偏移」模型：

1. 用「键」构造一个 `Counter`。键有三种：页（`page`）、匹配某选择器的元素（如 `heading`）、自定义字符串键（如 `"mycounter"`）。
2. 用户调用 `step` / `update` 时，编译器在文档里插入一个不可见的 `CounterUpdateElem`（带 `Locatable` 能力），它记下「这次更新是什么」。
3. 排版完成后，内省器给所有 locatable 元素（包括这些更新元素，以及被计数的标题等）建好索引。
4. 读值时，`CounterAtIntrospection` 先算出**整条状态轨迹** `sequence`（从初值开始，按排版顺序遍历所有匹配元素、逐个套用更新），再用 `query_count_before(selector, loc)` 算出当前位置之前发生了几次更新，得到偏移，从轨迹里取那一项。

用公式表达，位置 `loc` 处的计数器值为：

\[
v(\text{loc}) = \text{fold}\bigl(u_1 \circ u_2 \circ \dots \circ u_{k}\bigr)(\text{init}), \qquad k = \text{count\_before}(\text{loc})
\]

其中 \(u_i\) 是按排版顺序的第 \(i\) 次更新。关键优化：`sequence` 把整条轨迹一次性算完并被 comemo 记忆，于是「在不同位置读同一个计数器」从「每次重算前缀」的 \(O(n^2)\) 降为「算一次 + 各点 \(O(1)\) 查表」的 \(O(n)\)。

#### 4.1.3 源码精读

**Counter 只是一个键的包装**。[src/introspection/counter.rs:217-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L217-L219)：

```rust
#[derive(Debug, Clone, PartialEq, Hash)]
pub struct Counter(CounterKey);
```

`CounterKey` 是三种键的枚举（[counter.rs:526-536](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L526-L536)）：`Page` / `Selector(Selector)` / `Str(Str)`。它的 `cast!`（[counter.rs:538-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L538-L555)）让用户能写成 `counter(page)`、`counter(heading)`（元素被转成 `Selector::Elem`，特判 `page` 走 `Page`）、`counter(<label>)`、`counter("mycounter")`。

**为什么返回多级数组**：值类型是 `CounterState`（[counter.rs:591-592](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L591-L592)）：

```rust
pub struct CounterState(pub SmallVec<[u64; 3]>);
```

`SmallVec<[u64; 3]>` 是「≤3 层在栈上、超出才堆分配」的数组——三层标题（章/节/小节）正好是常见情况，故常态零分配。它经 `cast!`（[counter.rs:649-657](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L649-L657)）双向转成 Typst 的 `Array`，所以用户看到的永远是数组。

**初值与步进**：`init` 与 `step`（[counter.rs:596-630](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L596-L630)）。页计数器初值为 1，其余为 0（文档解释：「计数器总是在被计元素之前步进，所以从 0 开始，首次显示时正好是 1」）：

```rust
pub fn init(page: bool) -> Self {
    Self(smallvec![u64::from(page)]) // 页=1，其余=0
}

pub fn step(&mut self, level: NonZeroUsize, by: u64) {
    let level = level.get();
    while self.0.len() < level { self.0.push(0); }   // 补齐到所需层级
    self.0[level - 1] = self.0[level - 1].saturating_add(by);
    self.0.truncate(level);                            // 关键：截断更深层
}
```

`truncate(level)` 是多级编号的核心：当从 `1.2.3`（level=1 的新标题）继续时，深层会被清零，于是编号从 `1.2.3` 正确回到 `2`。

**更新的载体 `CounterUpdateElem`**（[counter.rs:660-682](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L660-L682)）：`step`/`update` 不会直接改值，而是返回一段内容，内容里塞了这个元素（[counter.rs:506-516](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L506-L516)）。它带 `Locatable`、`Count`、`#[internal]`，且 `Construct` 直接 bail（用户不能手写它）：

```rust
#[elem(Construct, Locatable, Count)]
pub struct CounterUpdateElem {
    #[required] key: CounterKey,
    #[required] #[internal] update: CounterUpdate,
}
```

**`Count` 能力**（[counter.rs:585-588](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L585-L588)）：被计数的元素实现它来声明「自己触发什么更新」。标题的实现（[heading.rs:311-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L311-L318)）在有 `numbering` 时返回 `Step(level)`：

```rust
impl Count for Packed<HeadingElem> {
    fn update(&self) -> Option<CounterUpdate> {
        self.numbering.get_ref(StyleChain::default())
            .is_some()
            .then(|| CounterUpdate::Step(self.resolve_level(StyleChain::default())))
    }
}
```

**`sequence_impl`：算整条轨迹**（[counter.rs:913-962](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L913-L962)，带 `#[comemo::memoize]`）。核心循环遍历 `introspector.query(selector)` 的全部命中元素（排版顺序），逐个套用更新（[counter.rs:938-959](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L938-L959)）：

```rust
for elem in introspector.query(selector) {
    // （页计数器：按真实页码差补步进，见下文）
    if let Some(update) = match elem.with::<dyn Count>() {
        Some(countable) => countable.update(),            // 元素自带更新（如标题）
        None => Some(CounterUpdate::Step(NonZeroUsize::ONE)), // 默认 +1
    } {
        current.update(&mut engine, update)?;
    }
    stops.push((current.clone(), page));
}
```

注意两件事：一是「能用 `Count` 能力的就用、不能的就默认 `Step(1)`」——所以 `counter(heading)` 既数标题本身（标题 impl 了 `Count`），也数手动 `counter(heading).step()` 插入的 `CounterUpdateElem`（它 impl `Count` 返回自己的 update，见 [counter.rs:678-682](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L678-L682)）。二是轨迹每一站同时记下当时的页码 `page`，这是页计数器专用。

**`Counter::select`：拼出最终选择器**（[counter.rs:238-263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L238-L263)）：基础是「所有 key 等于本计数器的 `CounterUpdateElem`」；对 `Selector` 键再 `Or` 上元素选择器本身；对 `Page` 键在 bundle 导出时用 `Within` 限定到当前文档。

**按位置取值 `CounterAtIntrospection`**（[counter.rs:787-814](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L787-L814)）：算轨迹 → `query_count_before` 取偏移 → 取那一站；页计数器还要把「真实页码 − 记录页码」的差补成一次 `step`（因为页计数器会在每个分页自动 +1，差值需要补回）。

```rust
let sequence = sequence(counter, &selector, engine, introspector)?;
let offset = introspector.query_count_before(&selector, *loc);
let (mut state, page) = sequence[offset].clone();
if counter.is_page() {
    let delta = introspector.page(*loc).unwrap_or(NonZeroUsize::ONE).get()
        .saturating_sub(page.get());
    state.step(NonZeroUsize::ONE, delta as u64);
}
Ok(state)
```

**用户层 `get` / `display`**（[counter.rs:364-373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L364-L373)、[counter.rs:382-447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L382-L447)）：`get` 从 context 取位置后 `engine.introspect(...)`；`display` 多一步——若没给 numbering，用 `matching_numbering`（[counter.rs:290-333](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L290-L333)）按被计元素推断（标题用标题的 numbering，否则默认 `"1.1"`），再把 `CounterState` 喂给 `Numbering::apply` 产出格式化文字。`both: true` 走 `CounterBothIntrospection`（[counter.rs:849](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L849)）产出 `[当前, 总数]`，正是页码 `"1 / 1"` 的来源。

**内部 API `display_at`**（[counter.rs:270-284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L270-L284)）：供 `Synthesize`/`Refable`/`Outline`/`Figure`/`Footnote`/`Link` 在**已知 location** 时格式化编号。标题在合成期调它把编号写回 `numbers` 字段（[heading.rs:269-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L269-L277)）——这就是「标题号、`@ref` 引用号、`#outline()` 目录号永不冲突」的实现基础（三者在 u8 讲过，都走同一个 `display_at` / 同一个计数器）。

**未收敛诊断**：每个 `Introspect` 实现都带 `diagnose`（如 [counter.rs:811-813](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L811-L813)），调 `format_convergence_warning`（[counter.rs:965-976](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L965-L976)）产出 `value of counter(...) did not converge` 警告，并附历次迭代观察到的值。这是通往 u9-l3 收敛循环的桥。

#### 4.1.4 代码实践

**实践目标**：弄清 `Counter` 为何返回多级数组，并追踪 `counter(heading).display()` 在文档不同位置如何给出不同结果。

**操作步骤（源码阅读型 + 本地验证型结合）**：

1. 打开 [counter.rs:591-630](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L591-L630)，确认 `CounterState` 是 `SmallVec<[u64;3]>`，读 `step` 里的 `truncate(level)`，在纸上推演：从 `smallvec![1,2,3]` 调 `step(level=1, by=1)` 后数组变成 `[2]`。
2. 跟踪 `counter(heading).display()` 的链路（不带 context 的展示版）：
   - `Counter::construct`（[counter.rs:340-358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L340-L358)）把 `heading` 转成 `CounterKey::Selector`；
   - 用户写 `#context counter(heading).display()` 时走 `Counter::display`（[counter.rs:382-447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L382-L447)）→ `CounterAtIntrospection`（[counter.rs:787-809](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L787-L809)）→ `sequence_impl`（[counter.rs:938-959](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L938-L959)）；
   - 而标题自己的编号是 `Synthesize` 调 `display_at`（[heading.rs:269-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L269-L277)）回填的。两条路读同一个计数器、同一套 `sequence`。
3. 本地写一个最小 Typst 文档验证（待本地验证）：

   ```typst
   #set heading(numbering: "1.1")

   = A
   这里是 #context counter(heading).display()  // 预期：1

   == A.b
   这里是 #context counter(heading).display()  // 预期：1.1

   = B
   这里是 #context counter(heading).display()  // 预期：2
   ```

**需要观察的现象**：三处 `display()` 处于文档不同位置，输出分别是 `1` / `1.1` / `2`。这正是 `query_count_before(selector, loc)` 在不同 `loc` 给出不同偏移、从而在 `sequence` 轨迹里取到不同 `CounterState` 的结果。

**预期结果**：理解了「同一计数器、不同位置 → 不同数组」的根因是偏移查询，而非计数器本身可变；并且因为读的是「上一轮」的内省器，首次编译这些值可能不准，需要收敛迭代才稳定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Counter::get` 的返回类型是 `CounterState`，而用户在 Typst 里看到的是数组？  
**答案**：`CounterState` 经 [counter.rs:649-657](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L649-L657) 的 `cast!` 在输出方向把 `SmallVec` 转成 `Array`。Rust 侧用 `CounterState` 保留多级语义与 `step`/`display` 方法，用户侧统一看到数组。

**练习 2**：若把 `counter(heading).update(3)` 的输出赋给变量而不放进文档（`let _ = ...`），会发生什么？  
**答案**：什么都不会发生。`update` 返回的是含 `CounterUpdateElem` 的内容（[counter.rs:506-516](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L506-L516)），只有被插入文档才有 location、才会被内省器索引并参与 `sequence`。

---

### 4.2 State：通用文档状态机

#### 4.2.1 概念说明

`State` 是 `Counter` 的「泛化版」：它持有的值可以是**任意 `Value`**（字符串、布尔、字典……），而不限于整数数组。它没有 `step`、没有自动编号，只有两种更新——直接设值、或用函数从旧值算新值。文档（[state.rs:15-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L15-L188)）用一个「`star` 计算器」的例子说明：普通 Typst 变量是不可变的（求值顺序 ≠ 排版顺序），而 `state` 把更新绑定到「内容被插入文档的位置」，从而在排版顺序下生效。

#### 4.2.2 核心流程

State 的读值模型与 Counter 完全同构，只是值不是整数数组而是 `Value`：

1. 用字符串 `key` 构造 `State`，附带初值 `init`。
2. `update` 在文档里插入不可见的 `StateUpdateElem`（`Locatable`），记下 `Set(值)` 或 `Func(函数)`。
3. 排版后内省器建索引。
4. `StateAtIntrospection::introspect` 算整条轨迹 `sequence`（从 `init` 起，按排版顺序逐个套用更新），用 `query_count_before` 取偏移。

定位处的状态值即：

\[
v(\text{loc}) = u_k \circ \dots \circ u_2 \circ u_1(\text{init}), \qquad k = \text{count\_before}(\text{loc})
\]

一个重要区别于 Counter：State 的更新函数 `Func` 接收**前一个值**并返回新值，因此「依赖前值」的更新无需 context，编译器能高效地把轨迹一次性算完；反之若写成「先 `context` 读再设值」，每次传播要多一轮迭代，可能无法在 5 次内收敛（见 [state.rs:338-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L338-L350) 的对比说明）。

#### 4.2.3 源码精读

**`State` 结构**（[state.rs:189-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L189-L196)）：只有 `key` 与 `init` 两个字段：

```rust
pub struct State {
    key: Str,
    init: Value,
}
```

构造器（[state.rs:218-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L218-L250)）：注意 `init` 有 `#[default]`，缺省时为 `Value::None`。文档里特别说明：同一 `key`、不同 `init` 的多个 state 会**共享更新**、各自用自己的初值计算——所以 [state.rs:234-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L234-L245) 那个 🍎/🍌/🥦 例子中三者结果不同。

**`StateUpdate`**（[state.rs:377-389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L377-L389)）：只有两种，没有 `Step`：

```rust
pub enum StateUpdate {
    Set(Value),    // 直接设值
    Func(Func),    // 函数：旧值 → 新值
}
cast! {
    StateUpdate,
    v: Func => Self::Func(v),
    v: Value => Self::Set(v),
}
```

`cast!` 的分支顺序很关键：先判 `Func`，故传函数走 `Func`、传别的值走 `Set`。

**`update` 不需要 context**（[state.rs:329-367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L329-L367)）：它标注的是 `#[func(since = "forever")]`，**没有** `contextual`。理由（文档 [state.rs:324-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L324-L329)）：构造一次更新无需知道当前位置，只有**读值**才需要位置。它返回内容（内含 `StateUpdateElem`）：

```rust
pub fn update(self, span: Span, update: StateUpdate) -> Content {
    StateUpdateElem::new(self.key, update).pack().spanned(span)
}
```

对照之下，`get` / `at` / `final_` 都标了 `#[func(contextual, ...)]`（[state.rs:256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L256)、[state.rs:273](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L273)、[state.rs:287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L287)）。`get` 取位置后调内省（[state.rs:257-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L257-L265)）：

```rust
pub fn get(&self, engine: &mut Engine, context: Tracked<Context>, span: Span) -> SourceResult<Value> {
    let loc = context.location().at(span)?;
    engine.introspect(StateAtIntrospection(self.clone(), loc, span))
}
```

**`StateUpdateElem`**（[state.rs:391-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L391-L402)）：与 `CounterUpdateElem` 同构，带 `Locatable`、`#[internal]`、`Construct` bail。

**整条轨迹 `sequence_impl`**（[state.rs:459-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L459-L510)，同样 `#[comemo::memoize]`）。与 Counter 版相比它更简单：值是 `Value`、不跟踪页码：

```rust
let mut current = state.init.clone();
let mut stops = eco_vec![current.clone()];
for elem in introspector.query(&state.select()) {
    let elem = elem.to_packed::<StateUpdateElem>().unwrap();
    match &elem.update {
        StateUpdate::Set(value) => current = value.clone(),
        StateUpdate::Func(func) => {
            current = func.call(&mut engine, Context::none().track(), [current])?;
        }
    }
    stops.push(current.clone());
}
```

**按位置取值 `StateAtIntrospection`**（[state.rs:414-431](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L414-L431)）：算轨迹 → `query_count_before` 取偏移 → `sequence[offset]`，与 Counter 完全平行：

```rust
let sequence = sequence(state, engine, introspector)?;
let offset = introspector.query_count_before(&state.select(), *loc);
Ok(sequence[offset].clone())
```

`final_` 走 `StateFinalIntrospection`（[state.rs:437-452](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L437-L452)），取 `sequence.last()`——这就是文档说的「time travel」：在任何位置都能读到文档末尾的终值。

#### 4.2.4 代码实践

**实践目标**：对比「函数式更新」与「context 式更新」对收敛迭代次数的影响，亲手体会 state.rs 文档强调的最佳实践。

**操作步骤**：

1. 阅读 [state.rs:459-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L459-L510)，确认 `Func` 分支调 `func.call(..., [current])` 把旧值喂给函数——这是「无需 context 即可算出整条轨迹」的关键。
2. 本地写两版斑马纹列表（改编自 [state.rs:351-363](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L351-L363) 的例子，待本地验证）：

   ```typst
   // 版本 A：函数式更新（推荐）
   #let fill = state("fill", false)
   #show list.item: it => {
     fill.update(f => not f)
     context { set text(fill: fuchsia) if fill.get(); it }
   }
   #lorem(5).split().map(list.item).join()
   ```

   ```typst
   // 版本 B：context 式更新（不推荐，可能不收敛）
   #let fill = state("fill", false)
   #show list.item: it => {
     context fill.update(not fill.get())
     context { set text(fill: fuchsia) if fill.get(); it }
   }
   #lorem(5).split().map(list.item).join()
   ```

**需要观察的现象**：版本 A 正常交替上色；版本 B 在条目较多时可能触发 `value of state(fill) did not converge` 警告（见 [state.rs:513-524](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L513-L524) 的诊断），因为每次更新要多一轮迭代才能传播。

**预期结果**：体会「依赖前值时优先用函数式 `update(f => ...)`」这条经验背后的源码原因——它让 `sequence_impl` 能在一轮里算完整条轨迹。

#### 4.2.5 小练习与答案

**练习 1**：`State` 与 `Counter` 都有 `get/at/final`，结构几乎一样。它们最大的语义差异是什么？  
**答案**：值类型与更新能力。`Counter` 的值恒为多级整数数组、有 `step` 和自动 `display`/`display_at`、并特判页码；`State` 的值是任意 `Value`、只有 `Set`/`Func` 两种更新、不自动格式化。可以说 `Counter` 是「受约束的、带编号显示的 State」。

**练习 2**：为什么 `state.update(...)` 不需要写在 `context { ... }` 里，而 `state.get()` 需要？  
**答案**：构造更新不需要知道当前位置（[state.rs:324-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L324-L329)），它只生成一个待插入的元素；而读值必须知道「现在在轨迹的哪一站」，故 `get` 要 context 来取 location（[state.rs:263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L263)）。

---

### 4.3 MetadataElem：向 query 系统投放值

#### 4.3.1 概念说明

`MetadataElem` 是本讲最小的模块——整个文件不到 30 行（[metadata.rs:1-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L1-L29)）。它的作用很纯粹：**把一个任意 `Value` 嵌进文档，但不产生任何可见内容**，让这个值能被 `query` 检索到。它解决的问题是「我想在文档里藏一些数据，事后（或从命令行）把它们取出来」。

它和 Counter/State 的关系：三者都靠「插入一个 locatable 元素」参与内省，但目的不同——Counter/State 维护的是「会沿文档变化的可计算状态」，而 Metadata 维护的是「静态标记点」。Metadata 不需要 `sequence`/偏移那套 machinery，它就是一个能被 `query` 命中的普通 locatable 元素。

#### 4.3.2 核心流程

1. 用户写 `#metadata("某值") <label>`，编译器创建一个 `MetadataElem`，其 `value` 字段是 `"某值"`，并因 `#[label]` 附上 `<label>`。
2. 排版后，内省器把所有 locatable 元素（含 MetadataElem）建索引。
3. 用户用 `#context query(<label>)`（u9-l1 讲过的 `query` 函数）取回命中元素，再 `.first().value` 读出藏起来的值。
4. 命令行也可用 `typst query` 直接拿到（文档 [metadata.rs:10-13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L10-L13)）。

#### 4.3.3 源码精读

整个元素定义（[metadata.rs:23-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L23-L28)）：

```rust
#[elem(since = "0.7.0", Locatable)]
pub struct MetadataElem {
    /// The value to embed into the document.
    #[required]
    pub value: Value,
}
```

要点：

- **`Locatable` 能力**：这是它能被 `query`/`locate` 检索的前提（u3-l2 讲过 `can::<dyn Locatable>()`）。有了它，编译器才会在排版后给每个 `MetadataElem` 回填 `location`，内省器才会把它收进索引。
- **唯一字段 `value: Value`**：`#[required]` 且 `pub`，所以 Typst 侧可直接 `it.value` 访问（见文档示例 [metadata.rs:18-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L18-L21) 的 `query(<note>).first().value`）。
- **不产生可见内容**：它没有 `Show` 实现、没有可渲染字段，realization 阶段它就是「隐形」的——这与 `CounterUpdateElem`/`StateUpdateElem` 的「不可见但参与内省」一致。
- **配合标签**：要在一堆 metadata 里分辨某一个，标准做法是给它附 `<label>`（`Label` 在 u2-l2 讲过），再 `query(<label>)` 精确定位。

#### 4.3.4 代码实践

**实践目标**：用 `metadata` + `query` 实现「在文档里藏数据、在别处取回」的最小闭环。

**操作步骤**：

1. 阅读 [metadata.rs:14-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/metadata.rs#L14-L22) 的文档示例，确认 `query` 返回的是数组、`.first().value` 才是藏的值。
2. 本地写一个用 metadata 收集「关键术语」再统一列出的例子（待本地验证）：

   ```typst
   // 在文档各处「埋点」
   #metadata(("term", "_renderer")) <t1>
   一些正文……
   #metadata(("term", "_locator")) <t2>
   更多正文……

   // 在文档末尾把所有埋点收集起来
   #context {
     let ms = query(Selector.or(<t1>, <t2>))
     for m in ms { linebreak() ; strong(m.value.at(1)) }
   }
   ```

   （`Selector.or` 是 u4-l2 讲过的选择器组合；若你的 Typst 版本写法不同，也可分别 query 再拼接。）

**需要观察的现象**：末尾的列表正确显示 `_renderer` 与 `_locator`，说明 `query` 跨越文档位置取回了这些不可见元素。

**预期结果**：理解 MetadataElem = 「带 location 的、可被 query 命中的隐形 Value 容器」，它是 Typst 与外部世界（尤其 `typst query` 命令行）交换任意数据的官方通道。

#### 4.3.5 小练习与答案

**练习 1**：`MetadataElem` 只有 `Locatable` 能力，没有实现 `Count`。如果有人误写 `counter(<sometag>)` 去数一个挂在 `MetadataElem` 上的标签，会发生什么？  
**答案**：`CounterKey::Selector` 会匹配到这些 MetadataElem；在 `sequence_impl`（[counter.rs:951-956](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L951-L956)）里，因为它不 impl `Count`，会走默认 `Step(1)` 分支——即每出现一次就 +1。所以它「能数」，只是数的是出现次数。

**练习 2**：为什么说 MetadataElem 是「静态」的，而 State 是「动态」的？  
**答案**：MetadataElem 投放后就固定不变，`query` 只是按 location 取回它携带的常量 `Value`；State 的值随位置变化，读值要沿排版顺序折叠一串更新。前者是「标记点」，后者是「随位置变化的可计算状态」。

---

## 5. 综合实践

把本讲三块串起来，做一个「带自动编号与状态追踪的自定义定理环境」小任务：

1. **用 Counter 自动编号**：定义 `#let c = counter("theorem")`，在每个定理开头 `c.step()` 再 `#context c.display()`（参考 [counter.rs:159-177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L159-L177) 的「How to step」节）。
2. **用 State 记录难度**：再定义 `#let diff = state("difficulty", 0)`，每个定理前 `diff.update(d => d + 1)` 表示难度递增，正文里 `#context diff.get()` 显示当前难度。
3. **用 Metadata 暴露给命令行**：在每个定理里放 `#metadata((counter: c, difficulty: diff)) <thm-i>`，使得 `typst query '<thm-*>'` 能一次性导出所有定理的编号与难度。
4. **验证收敛**：故意把 State 改成 context 式更新（4.2.4 的版本 B），观察是否触发 `did not converge` 警告，再改回函数式更新。

完成后，你应该能解释：为什么 step/update/metadata 插入的内容都「不可见却生效」——因为它们都是 locatable 元素，被内省器索引后参与计数器的 `sequence`、state 的 `sequence` 或 `query` 的命中集合；而一切读值都经 `engine.introspect(...)`，读的是上一轮的索引，故需收敛循环。

## 6. 本讲小结

- `Counter` 的值恒为多级数组（`CounterState` 即 `SmallVec<[u64;3]>`），因为编号可能多层；`step` 的 `truncate(level)` 负责回高层时清零深层。
- Counter 与 State 共用「整条轨迹 + `query_count_before` 偏移」模型，且 `sequence_impl` 都标 `#[comemo::memoize]`，把多点读值从 \(O(n^2)\) 降到 \(O(n)\)。
- `Counter` 是受约束的（只数整数、带 `step`/`display`/`display_at`、特判页码）；`State` 是泛化的（任意 `Value`、只有 `Set`/`Func`）。二者结构几乎对称。
- `step`/`update` 不需要 context（只生成待插入元素），`get`/`at`/`final`/`display` 需要 context（要取位置才能算偏移）；依赖前值时优先用函数式更新以利于收敛。
- `MetadataElem` 是极小的「带 location 的隐形 `Value` 容器」，靠 `Locatable` 能力被 `query`/`typst query` 检索，是 Typst 与外部交换任意数据的通道。
- 三者都靠「插入不可见 locatable 元素 → 内省器索引 → `engine.introspect` 读值」运作，未收敛时各自的 `diagnose` 产出带历史值的警告，通向 u9-l3 的收敛循环。

## 7. 下一步学习建议

- 下一讲 **u9-l3 Introspector 与收敛循环**：本讲反复出现的 `engine.introspect`、`Introspect` trait、`History`、`MAX_ITERS` 与 `did not converge` 都在 [convergence.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L1-L282) 收口，建议精读它把「为什么需要反复编译」彻底讲清。
- 回看 u8 的标题/引用/图表/目录（[model/](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L318)），用本讲的 `display_at` + `Count` 视角重新理解「三号同源」。
- 若对性能感兴趣，可带着本讲的 `#[comemo::memoize] sequence_impl` 与 `SmallVec`，去 u12-l2（性能与并发）看 comemo tracked 与惰性哈希的全貌。
