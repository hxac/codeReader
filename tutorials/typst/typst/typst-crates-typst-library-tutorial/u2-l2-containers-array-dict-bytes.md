# 容器类型 Array、Dict、Bytes 与 Label

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `Array`、`Dict`、`Bytes`、`Label` 四种容器各自的内部表示（`EcoVec` / `Arc<IndexMap>` / `Arc<LazyHash<dyn Bytelike>>` / `PicoStr`）。
- 理解 Typst 为什么为这四种容器选择「引用计数 + 写时复制」的存储策略，从而让**克隆几乎免费、按需深拷贝**。
- 对比四种容器的**哈希与相等成本**，看懂 Typst 如何用「惰性哈希（LazyHash）」和「字符串驻留（PicoStr）」把代价逐步降到 \(O(1)\)。
- 读懂 `at` 的负数下标、`Bytes` 的零拷贝转换、`Label` 的驻留机制等关键实现细节。

本讲承接上一讲 [`Value` 枚举与标量类型](u2-l1-value-and-scalars.md)：`Value` 是「单个值」的容器，而本讲讲的是「装多个值 / 装字节 / 装名字」的容器——它们都是 `Value` 枚举里的变体类型。

## 2. 前置知识

在进入源码前，先用三段白话解释几个反复出现的关键词。

**引用计数（reference counting, RC）与写时复制（copy-on-write）。**
Rust 里 `Vec<T>` 每次克隆都要把整段内存复制一遍，代价是 \(O(n)\)。这对一个排版引擎来说太贵了——文档里动辄有成千上万个 `Array`、`Dict`，如果每做一次求值就全量复制，性能会崩塌。解决办法是把真正存储数据的堆对象包进一个「计数器」里：克隆时只把计数器 +1（\(O(1)\)），并不复制数据；只有当某个所有者真的要**修改**数据时，才检查计数器，若计数大于 1 就先深拷贝一份再改（这叫写时复制）。本讲的 `Array`（`EcoVec`）、`Dict`（`Arc`）、`Bytes`（`Arc`）用的都是这套思路。

**`EcoVec`、`IndexMap`、`LazyHash`、`PicoStr` 是什么。**

| 名字 | 来自 | 一句话作用 |
|------|------|-----------|
| `EcoVec<T>` | `ecow` crate | 一个引用计数的、写时复制的可增长数组。克隆是 \(O(1)\)。 |
| `IndexMap<K,V>` | `indexmap` crate | 一个**保留插入顺序**的哈希表（普通 `HashMap` 的迭代顺序是不确定的）。 |
| `FxBuildHasher` | `rustc-hash` crate | 一个非加密的快速哈希函数，比默认 `SipHash` 快很多，Typst 不需要抗碰撞安全性。 |
| `LazyHash<T>` | `typst-utils` | 包住一个值，**第一次算哈希时缓存结果**，之后直接复用。 |
| `PicoStr` | `typst-utils` | 一个**字符串驻留器（interner）**：相同字符串在全局只存一份，用一个 8 字节整数 ID 代替它。 |

**为什么「哈希」很重要。**
Typst 用 `comemo` 做增量记忆化（上一讲与后续编译环境讲义会展开），缓存是否命中取决于参数的哈希值是否变化。因此一个类型「算哈希有多贵」直接影响增量编译的速度。这正是本讲反复讨论四种容器哈希成本的动机。

## 3. 本讲源码地图

| 文件 | 定义的类型 | 本讲关注点 |
|------|-----------|-----------|
| [src/foundations/array.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs) | `Array` | `EcoVec<Value>` 表示、负数下标、`range`/`sorted`/`dedup`/`to_dict` 等方法 |
| [src/foundations/dict.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs) | `Dict` | `Arc<IndexMap<...>>` 表示、插入序、`make_mut` 写时复制、`finish` 校验未知键 |
| [src/foundations/bytes.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs) | `Bytes` | `Arc<LazyHash<dyn Bytelike>>` 表示、从字符串零拷贝、`into_vec`/`into_string` 复用分配 |
| [src/foundations/label.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs) | `Label` | `PicoStr` 驻留句柄、`Unlabellable` 标记 trait |

四个类型都标注了 `#[ty(scope, cast, ...)]`，意味着它们都是上一讲介绍的「一等类型」：都会作为 `Value` 枚举里的一个变体存在，并各自注册了一个 `Type`，可在脚本中用名字引用（`array`、`dictionary`、`bytes`、`label`）。

---

## 4.1 Array

### 4.1.1 概念说明

`Array` 是 Typst 里「一串值」的容器，对应脚本里的 `(1, 2, 3)`。它的元素类型可以各不相同（`Value` 已经抹平了类型），所以 `(1, "a", (2, 3))` 是合法的。

它要解决两个问题：

1. **频繁克隆的性能**：求值过程中一个数组可能被复制很多次，必须克隆廉价。
2. **丰富的脚本方法**：用户要能 `.at()`、`.map()`、`.filter()`、`.sorted()`、`.split()`、`.zip()`、`.chunks()`……这套 API 比普通数组大得多。

### 4.1.2 核心流程

`Array` 的定义只有一行：

[src/foundations/array.rs:73-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L73-L75) —— 这里把 `EcoVec<Value>` 用 `#[derive(..., Clone, PartialEq, Hash, ...)]` 包装成 `Array`。

派生的语义决定了它的成本模型：

- **克隆**：`EcoVec` 是引用计数的，克隆只是把内部计数 +1 → \(O(1)\)。
- **相等 `==`**：派生的 `PartialEq` 会逐元素比较 → \(O(n)\)。
- **哈希 `Hash`**：派生的 `Hash` 会逐元素喂入哈希器 → \(O(n)\)。

> 对比预告：`Bytes` 会用 `LazyHash` 把「\(O(n)\) 哈希」变成「算一次后缓存」，`Label` 会用驻留把它变成「恒 \(O(1)\)」。本模块先看最朴素的「全量 \(O(n)\)」基线。

数组的「负数下标从尾部数」是脚本里的常用特性，由内部方法 `locate_opt` 实现。

[src/foundations/array.rs:130-L137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L130-L137) —— 当 `index < 0` 时，用 `len + index` 把负数折回正数下标（`(1,2,3).at(-1)` 取到 3）。`end_ok` 控制是否允许 `index == len`（插入/切片时允许越界一格）。

### 4.1.3 源码精读

**用户可见的 `at` 方法**（带 `default` 兜底）：

[src/foundations/array.rs:207-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L207-L221) —— 注意三段式取值链：`locate_opt` 解析下标 → 取值并 `cloned` → 失败时回落到 `default` 参数 → 都没有才报错。`#[func]` 宏（见下一讲）会把 `default: Option<Value>` 暴露成脚本的命名参数 `default:`。

**`range`：构造数列。** 这是规格任务里点名的「数列构造」能力（注意源码里**没有** `range_data` 方法，真正存在的是 `range` 和 `to_dict`）。

[src/foundations/array.rs:383-L443](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L383-L443) —— 它支持 `range(5)` / `range(2,5)` / `range(20, step:4)` / `inclusive:`，核心是一个 `while in_bounds(x)` 循环，逐个 `push(x.into_value())`。`step_dir = 0.cmp(&step)` 记录步长方向，避免用户写 `range(5, step:-1)` 时陷入死循环。

**`sorted`：为何不用标准库排序。**

[src/foundations/array.rs:939-L942](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L939-L942) —— 注释解释：当用户的 `by` 比较函数不能定义合法全序时，标准库排序可能 panic，所以 Typst 改用 `glidesort`。这是「外部输入可能导致比较函数异常」的典型防御。

**`dedup`：为何是 \(O(n^2)\)。**

[src/foundations/array.rs:1083-L1085](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1083-L1085) —— 注释说明去重不能用 `HashSet`：一要保序，二是因为「任意 `Value` 不能被哈希」（上一讲提到不是所有 `Value` 都能哈希）。所以用了双层循环的朴素算法。这正好呼应了「`Array` 的哈希是 \(O(n)\)、但更上层的去重宁可 \(O(n^2)\) 也不用哈希」这个工程权衡。

**`to_dict`：数组转字典的桥梁。**

[src/foundations/array.rs:1117-L1135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1117-L1135) —— 把「长度为 2 的数组」视为 `(key, value)` 对，其中 `key` 必须能 `cast::<Str>()`。这是 `Array` 与 `Dict` 两个容器的交汇点。

### 4.1.4 代码实践

> **实践目标**：验证 `Array` 的克隆/哈希成本，并理解负数下标。

1. 打开 [src/foundations/array.rs:73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L73)，确认 derive 是 `Clone, PartialEq, Hash`。
2. **阅读跟踪**：在 `locate_opt`（[L130-L137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L130-L137)）处脑内代入 `(1,7,4).at(-1)`：`len=3, index=-1 → 3+(-1)=2 → arr[2]=4`。
3. **观察现象（待本地验证）**：若你装了 Rust 工具链，可在仓库内 `cargo build -p typst-library`；用 `cargo expand` 查看 `#[derive(Hash)]` 展开后会发现它对每个 `Value` 元素调用 `.hash(state)`——这就是「\(O(n)\) 哈希」的来源。
4. **预期结果**：你能用自己的话说出「`Array` 克隆 \(O(1)\)、但 `==` 和哈希都是 \(O(n)\)」，并能解释 `dedup` 为何宁可 \(O(n^2)\) 也不哈希。

> 本实践为「源码阅读型」：因为 `Array` 是库内内部类型，无法脱离编译器单独运行，故以阅读 + 推理为主。

### 4.1.5 小练习与答案

**练习 1**：`(1,2,3).at(-2)` 的值是什么？请用 `locate_opt` 的逻辑推导。
**答案**：`len=3, index=-2 → 3+(-2)=1 → arr[1]=2`，结果是 `2`。

**练习 2**：为什么 `Array` 的 `sorted` 用 `glidesort` 而不是标准库 `sort_by`？
**答案**：用户的 `by` 比较函数可能不构成合法全序（例如对 `(x,y)` 和 `(y,x)` 都返回 false），标准库排序在此情况下可能 panic，而 `glidesort` 不会（见 [L939-L942](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L939-L942) 的注释）。

---

## 4.2 Dict

### 4.2.1 概念说明

`Dict` 是「字符串键 → 任意值」的映射，对应脚本里的 `(name: "Typst", born: 2019)`。它与 `Array` 最大的区别是：键不是整数而是字符串，并且**必须保留插入顺序**（这样用户遍历、`.keys()`、序列化输出才是可预期的）。

两个设计要点：

1. 用 `IndexMap`（保留顺序）而非 `HashMap`。
2. 用快速哈希 `FxBuildHasher`，因为字典的键是字符串、不涉及安全场景。

### 4.2.2 核心流程

定义同样只有一行：

[src/foundations/dict.rs:78-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L78-L80) —— `Dict` 是 `Arc<IndexMap<Str, Value, FxBuildHasher>>` 的包装。

它的成本模型：

- **克隆**：`Arc` 计数 +1 → \(O(1)\)。
- **修改**：通过 `Arc::make_mut(&mut self.0)`。当 `strong_count > 1`（即有别人共享）时，`make_mut` 会先深拷贝一份 `IndexMap`，这正是写时复制。
- **哈希**：注意 `Dict` **没有**派生 `Hash`，而是手写了实现（见 4.2.3），原因是 `IndexMap` 默认派生的哈希会包含内部布局（桶顺序等），不稳定；手写实现只哈希「长度 + 每个键值对」，保证相同内容哈希相同。

### 4.2.3 源码精读

**手写的 `Hash`（重点）**：

[src/foundations/dict.rs:406-L413](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L406-L413) —— 先写长度，再逐对哈希 `item.hash(state)`。`item` 是 `(&Str, &Value)`，两者都能哈希。这个手写实现保证「内容相同则哈希相同」，代价是 \(O(n)\)。

**写时复制的修改方法**：以 `insert`、`at_mut`、`clear` 为例。

[src/foundations/dict.rs:99-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L99-L111) —— `at_mut` 和 `take` 都先 `Arc::make_mut(&mut self.0)` 拿到可变的 `IndexMap`，再操作。`take` 用的是 `shift_remove`（保持序，把后续元素左移），而不是 `swap_remove`（会打乱顺序）。

[src/foundations/dict.rs:119-L125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L119-L125) —— `clear` 有个优化：若引用计数为 1（独占），就地 `clear`；否则干脆把整个 `Dict` 替换成一个新空字典（避免无谓深拷贝）。

**「未知键」校验 `finish` / `unexpected_keys`**：

[src/foundations/dict.rs:134-L169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L134-L169) —— 这套方法供「带固定字段集合的元素构造」使用：从脚本收来的 `Dict` 应该只含预期键，多出来的键要报「unexpected key ..., valid keys are ...」。后续讲义讲到 `#[elem]` 字段解析时会反复用到。

**`Module` 也能转成 `Dict`**：

[src/foundations/dict.rs:345-L353](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L345-L353) —— `cast!` 规则把一个模块（命名空间）迭代成键值对。这是脚本里 `dictionary(sys).at("version")` 能成立的底层原因。

### 4.2.4 代码实践

> **实践目标**：理解 `Dict` 的取值与类型转换；写出从 `Dict` 取值并做类型转换的伪代码。

这是本讲的主干实践。`Dict` 暴露给脚本，所以可以同时用「脚本侧」和「Rust 侧」两种视角看。

**Rust 侧（库内部视角）**——这是 Typst 标准库自身取值 + 转换的惯用法：

```rust
// 示例代码：库内部从 Dict 取值并类型转换的典型写法
use crate::foundations::{Dict, FromValue, Str, Value};

// 1. 拿到一个来自脚本的 Dict（键值都已抹平成 Value）
fn handle_config(dict: &Dict) -> StrResult<()> {
    // 2. 借取：get 返回 &Value，cloned 得到 Value
    let raw: Value = dict.get("count")?.clone();
    // 3. 类型转换：FromValue::from_value / Value::cast::<T>
    let count: i64 = raw.cast::<i64>()?;   // 失败时返回带提示的 StrResult
    let name: Str = dict.get("name")?.clone().cast::<Str>()?;
    Ok(())
}
```

关键点：
- `Dict::get` 返回 `StrResult<&Value>`（[L94-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L94-L96)），缺键时是 `dictionary does not contain key ...`。
- `.cast::<T>()` 委托 `FromValue`（上一讲讲过），把 `Value` 还原成具体 Rust 类型；类型不符也会产出可读错误。

**脚本侧（用户视角）**——可直接放进 `.typ` 文件验证：

```typ
#let cfg = (name: "Typst", count: 3, tags: ("a", "b"))
#cfg.name              // 字段访问：取 "Typst"
#cfg.at("count")       // 动态键访问：取 3
#cfg.at("missing", default: 0)   // 兜底：取 0
#("name" in cfg)       // 成员判断：true
#cfg.keys()            // 插入序：("name", "count", "tags")
```

**操作步骤**：
1. 阅读 [src/foundations/dict.rs:94-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L94-L96)（`get`）与 [L207-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L207-L221)（脚本 `at`），对比「库内 `get` 借取引用」与「脚本 `at` 克隆值」。
2. 在仓库里 `cargo run --bin typst compile -` 等命令行工具（若可用）或 typst CLI 中，把上面脚本片段跑一遍。
3. **需要观察的现象**：`at` 带与不带 `default:` 时，缺键分别报错 / 返回默认值；`keys()` 严格按声明顺序。
4. **预期结果**：能解释 `Dict::get` 返回引用而脚本 `.at()` 返回克隆值的原因（脚本值必须脱离字典独立存在），并说清「取值 + `cast::<T>`」的两步式类型转换。
5. 若无本地运行环境，记为「待本地验证」，仅完成源码阅读与推导。

### 4.2.5 小练习与答案

**练习 1**：`Dict` 为什么手写 `Hash` 而不直接 `#[derive(Hash)]`？
**答案**：`IndexMap` 派生的哈希可能随内部桶布局变化，不能保证「内容相同则哈希相同」；手写实现只哈希「长度 + 每个键值对」，保证内容确定性（见 [L406-L413](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L406-L413)）。

**练习 2**：`take` / `remove` 用 `shift_remove` 而非 `swap_remove`，为什么？
**答案**：`shift_remove` 删除后把后续元素左移、**保持插入顺序**；`swap_remove` 用最后一个元素填补空位会打乱顺序，违背 `Dict` 保序的承诺（见 [L107-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L107-L111)）。

---

## 4.3 Bytes

### 4.3.1 概念说明

`Bytes` 是「字节序列」，对应脚本里的 `bytes("Hi")` 或 `bytes((72, 105))`。它和「`Array` of 整数」语义相近，但**专门为二进制数据优化**：图片、字体、原始文件内容都很大，绝不能用每个元素一个 `Value` 的 `Array` 来存。

它要解决三个问题：

1. **大缓冲区的廉价克隆**：一个几 MB 的图片可能被多次引用。
2. **避免无谓拷贝**：从字符串创建字节时，应直接复用其 UTF-8 内存，而不是先转 `Vec<u8>`。
3. **避免重复算哈希**：字节缓冲区很大，每次都全量哈希太贵。

### 4.3.2 核心流程

定义：

[src/foundations/bytes.rs:44-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L44-L46) —— `Bytes(Arc<LazyHash<dyn Bytelike>>)`。这里有两层巧思：

- 外层 `Arc`：引用计数，克隆 \(O(1)\)，可被多个 `Bytes` 共享。
- 内层 `LazyHash<dyn Bytelike>`：用 **trait 对象** `dyn Bytelike` 擦除「真实存储类型」，把不同来源（`&'static [u8]`、`Vec<u8>`、`String`、`Source`……）统一成一种；`LazyHash` 则把「算一次哈希」缓存起来。

成本模型（与 `Array`/`Dict` 对照）：

- **克隆**：\(O(1)\)（`Arc` +1）。
- **哈希**：第一次 \(O(n)\)，之后被 `LazyHash` 缓存为 \(O(1)\)——这是 `Bytes` 相对 `Array`/`Dict` 的关键进步。
- **相等**：逐字节比较 \(O(n)\)。

### 4.3.3 源码精读

**两种构造器：`new`（字节来源）与 `from_string`（字符串来源）。**

[src/foundations/bytes.rs:60-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L60-L77) —— `new` 直接把原始数据 `T: AsRef<[u8]>` 包进 `LazyHash`，注释强调「**直接背靠**（directly back）原始数据，传 `&'static [u8]` 或 `[u8; 8]` 不额外分配」。`from_string` 则包一层 `StrWrapper`。

**`StrWrapper`：让字符串成为字节缓冲且跳过 UTF-8 校验。**

[src/foundations/bytes.rs:408-L421](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L408-L421) —— `StrWrapper<T>` 实现了 `Bytelike`，它的 `as_str` 直接返回 `Ok(self.0.as_ref())`，因为字符串本身一定是合法 UTF-8。于是 `as_str` 不必再校验一次（见 [L89-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L89-L95) 的注释）。

**`into_vec` / `into_string`：独占时复用底层分配。**

[src/foundations/bytes.rs:103-L137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L103-L137) —— 当 `Bytes` 是唯一所有者（引用计数为 1）时，`into_vec`/`into_string` 能把底层 `Vec<u8>`/`String` 直接「掏出来」复用，零拷贝；否则只能 `to_vec()` 全新分配。源码里的单元测试（见下）正是验证「指针相同」。

**为什么 `slice` 选择拷贝而不是持有视图。**

[src/foundations/bytes.rs:296-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L296-L301) —— 注释解释：本可以持有一个「指向原缓冲的视图」而不拷贝，但 Java 当年对 `String` 这么做后来又放弃了，因为「在很大的缓冲里切一个很小的视图」会导致大缓冲无法被回收，形同内存泄漏。所以 Typst 选择了拷贝。

**手写的 `PartialEq` 与派生的 `Hash`。**

[src/foundations/bytes.rs:325-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L325-L331) —— `eq` 委托到内层 `self.0.eq(&other.0)`（逐字节比较）；而 `Hash` 是在 [L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L45) 派生的，但派生会经过 `LazyHash`，后者负责「算一次缓存」。

**可读的单元测试佐证以上设计**：

[src/foundations/bytes.rs:456-L505](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L456-L505) —— `test_bytes_into_string_lone` 用 `std::ptr::eq` 断言「独占时指针不变（零拷贝）」；`test_bytes_into_string_shared` 断言「共享时第一个会拷贝、最后一个能拿回原始分配」。读测试是理解行为最快的方式。

### 4.3.4 代码实践

> **实践目标**：理解 `Bytes` 的零拷贝转换与 `LazyHash` 缓存。

1. 阅读 [src/foundations/bytes.rs:60-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L60-L77) 与 `Bytelike` trait 定义 [L383-L399](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L383-L399)。注意泛型实现 `impl<T: AsRef<[u8]>> Bytelike for T` 让几乎所有字节容器自动成为 `Bytelike`。
2. 跟踪一条链路：`bytes("Hello")` → `cast!` 的 `Str` 分支（[L426-L439](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L426-L439)）调用 `Bytes::from_string` → `StrWrapper` → 不发生任何字节复制。
3. 运行（待本地验证）：`cargo test -p typst-library bytes::` 可看到 4.3.3 提到的指针相等测试通过，验证零拷贝复用。
4. **预期结果**：你能解释「为什么从字符串造 `Bytes` 不需要复制、也不需要重新做 UTF-8 校验」，并说出 `LazyHash` 如何让大缓冲的重复哈希变廉价。

### 4.3.5 小练习与答案

**练习 1**：为什么 `Bytes` 用 `dyn Bytelike`（trait 对象）而不是固定存 `Vec<u8>`？
**答案**：为了「零拷贝复用来源」。不同来源（`&'static [u8]`、`Vec<u8>`、`String`、`Source`）各有自己的内存表示，用 trait 对象擦除类型后，`Bytes::new` / `from_string` 就能直接背靠原始数据而不必统一转成 `Vec<u8>`（见 [L60-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L60-L77) 注释）。

**练习 2**：`Bytes::slice` 为什么宁可拷贝也不持有「视图」？
**答案**：在超大缓冲里切极小视图会让大缓冲无法释放，形成内存泄漏（Java `String` 的历史教训，见 [L296-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L296-L301) 注释）。

---

## 4.4 Label

### 4.4.1 概念说明

`Label` 是文档里的「标签」，对应脚本里的 `<my-label>` 语法或 `label("b")` 构造器。标签依附到最近的前一个非空白元素上，被它标记的元素随后可以被 `@ref` 引用、被 `query` 查询、或被 `show <name>:` 样式规则选中。

它与前三种容器的本质区别：它**装的是「名字」而不是数据**。一个文档里可能重复使用同一个标签名（错误用法，但语法允许），且标签会被频繁地比较、哈希（内省系统大量用到）。因此 Typst 对它的优化最为彻底——用**字符串驻留**把它压成一个 8 字节整数。

### 4.4.2 核心流程

定义：

[src/foundations/label.rs:49-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L49-L51) —— `Label(PicoStr)`，并派生 `Copy, Clone, Eq, PartialEq, Hash`。

`PicoStr` 来自 `typst-utils`，其定义是 `pub struct PicoStr(NonZeroU64)`（见 [crates/typst-utils/src/pico.rs:40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L40)），即一个 8 字节整数。它的工作方式：

- `PicoStr::intern("name")` 把字符串「驻留」到全局表里，返回一个 ID；**相同字符串永远返回相同 ID**。
- `.resolve()` 用 ID 反查回字符串（用于显示）。

于是 `Label` 的成本模型是本讲最优的：

| 操作 | 成本 |
|------|------|
| 克隆 | `Copy`，8 字节按位复制 → \(O(1)\) |
| 相等 `==` | 比较两个 `u64` ID → \(O(1)\)（无需比字符串内容） |
| 哈希 `Hash` | 哈希一个 `u64` → \(O(1)\) |

对比小结（本讲的总线索）：

| 类型 | 克隆 | 哈希 | 关键机制 |
|------|------|------|----------|
| `Array` | \(O(1)\) | \(O(n)\) 每次都算 | `EcoVec` 引用计数 |
| `Dict` | \(O(1)\) | \(O(n)\) 每次都算 | `Arc<IndexMap>` + 手写 Hash |
| `Bytes` | \(O(1)\) | \(O(n)\) 一次，之后 \(O(1)\) | `Arc<LazyHash>` 缓存 |
| `Label` | \(O(1)\) | \(O(1)\) | `PicoStr` 字符串驻留 |

### 4.4.3 源码精读

**构造：禁止空名 + 驻留。**

[src/foundations/label.rs:76-L90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L76-L90) —— 脚本构造器 `label("...")` 接受任意非空字符串（包括含特殊字符的名字），空名则 `bail!` 报错，否则 `PicoStr::intern` 驻留。

[src/foundations/label.rs:57-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L57-L60) —— 另一个内部 `new` 接受已驻留的 `PicoStr`，对空串常量返回 `None`（保证任何 `Label` 都非空）。

**显示（repr）：决定用尖括号语法还是函数语法。**

[src/foundations/label.rs:92-L101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L92-L101) —— 若名字是合法的尖括号标识符（如 `my-label`），打印成 `<my-label>`；含特殊字符时回退到 `label("...")` 函数形式。这保证了往返（repr → 重新解析）的一致性。

**`Unlabellable` 标记 trait。**

[src/foundations/label.rs:109-L110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L109-L110) —— 一个空 trait，给「不允许被打标签」的元素类型做标记（如纯空格内容）。实现它的元素会被 `<label>` 跳过、不接收标签——这正是文件顶部文档注释所说「标签依附到最近的非空白元素」的实现基础。

### 4.4.4 代码实践

> **实践目标**：理解 `Label` 的驻留与 `Copy` 语义。

1. 阅读 [src/foundations/label.rs:49-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L49-L51)，确认 `Label` 派生了 `Copy`——这意味着 `Label` 永远在栈上按值传递，**永远不会**进入堆/引用计数世界。
2. 打开驻留实现 [crates/typst-utils/src/pico.rs:40-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L40-L44)，确认 `PicoStr` 只是 `NonZeroU64`，`intern` 是「字符串 → ID」的查表插入。
3. **思考验证**：既然 `Label` 内部只是一个 `u64`，那么 `query(<a>)` 在内省系统中对海量元素做标签比较时，每次比较就是比一个整数——这是内省/`query` 能高效工作的基础（下一单元内省会展开）。
4. **预期结果**：你能解释「为什么 `Label` 不像 `Str`/`Dict` 那样需要引用计数」，并说清驻留如何让相等与哈希都变成 \(O(1)\)。

> 待本地验证项：`cargo expand -p typst-utils` 查看 `PicoStr` 的内部全局表实现，确认「同字符串同 ID」。

### 4.4.5 小练习与答案

**练习 1**：为什么 `Label` 可以派生 `Copy`，而 `Array`/`Dict`/`Bytes` 都不行？
**答案**：`Label` 内部是 `PicoStr(NonZeroU64)`，8 字节、无堆指针，按位复制即正确且廉价；而另三者内部有堆分配（`EcoVec`/`IndexMap`/`dyn Bytelike`），若 `Copy` 会引发所有权混乱，必须用引用计数的 `Clone`。

**练习 2**：`Unlabellable` trait 解决什么问题？
**答案**：标记某些元素（如空白）「不可被打标签」，让 `<label>` 语法跳过它们、把标签附给真正的前一个内容元素，从而实现「标签依附最近非空白元素」的规则（见 [L109-L110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L109-L110)）。

---

## 5. 综合实践

设计一个把四个容器串起来的小任务：**在源码里追踪一次「从字节到字典」的完整转换链**。

场景：用户写了下面这段 Typst 脚本，你作为源码阅读者要回答每一步分别用到哪个容器的哪个能力。

```typ
// 读一段原始字节（Bytes），转成整数数组（Array），再转成字典（Dict），
// 最后给结果打上标签（Label）以便后续查询。
#let raw = bytes((1, 2, 3))            // 1. Bytes
#let nums = array(raw)                  // 2. Array（从 Bytes 转）
#let pairs = nums.enumerate()           // 3. Array 的方法：((0,1),(1,2),(2,3))
#let mapping = pairs.to-dict()          // 4. Array → Dict
#show <result>: set text(red)
#metadata(mapping) <result>             // 5. 打上 Label
```

请完成：

1. 在 [bytes.rs 的 `cast!`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L426-L439)（`ToBytes`）与 [array.rs 的 `cast!`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1175-L1180)（`ToArray` 的 `Bytes` 分支）中找到 `bytes`↔`array` 的互转规则，确认它们彼此对称。
2. 跟踪第 4 步 `to_dict`：阅读 [Array::to_dict](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1117-L1135)，说明它如何把「长度 2 的数组」拆成 `(Str, Value)` 对。
3. 跟踪第 5 步：阅读 [label.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L76-L90) 的 `construct` 与 `Unlabellable`，说明 `<result>` 如何被驻留为一个 `PicoStr` 并被附加到 `metadata` 元素。
4. **综合结论**：用一张图或一段话总结「字节→整数→数组→键值对→字典→被标签标记」这条链上，每一段的克隆/哈希成本（参考 4.4.2 的对照表）。

这个任务把四种容器用一条真实的数据流串起来，并让你亲手验证「转换规则的对称性」「标签的驻留」两个核心点。

## 6. 本讲小结

- 四种容器都用**引用计数 + 写时复制**实现 \(O(1)\) 克隆：`Array` 用 `EcoVec`，`Dict` 用 `Arc<IndexMap>`（`make_mut` 触发按需深拷贝），`Bytes` 用 `Arc<LazyHash<dyn Bytelike>>`。
- `Array` 的 `==`/哈希是 \(O(n)\)（派生），且因「任意 `Value` 不可哈希」，`dedup` 宁可 \(O(n^2)\) 也不用 `HashSet`；负数下标由 `locate_opt` 用 `len + index` 折算。
- `Dict` 手写 `Hash` 以保证「内容相同则哈希相同」，用 `shift_remove` 保序，`finish`/`unexpected_keys` 为带固定字段的元素构造做未知键校验。
- `Bytes` 用 `dyn Bytelike` trait 对象零拷贝复用不同来源，`from_string` 借 `StrWrapper` 跳过 UTF-8 校验，`into_vec`/`into_string` 在独占时复用底层分配（单元测试用指针相等佐证）；`LazyHash` 让哈希「算一次后缓存」。
- `Label` 最彻底：内部就是 `PicoStr(NonZeroU64)`，`Copy` + 驻留让克隆/相等/哈希全部 \(O(1)\)；`Unlabellable` 标记 trait 实现「标签附给最近非空白元素」。
- 一条贯穿线索：克隆都是 \(O(1)\)，但哈希成本沿 `Array`/`Dict`（每次 \(O(n)\)）→ `Bytes`（缓存后 \(O(1)\)）→ `Label`（恒 \(O(1)\)）逐步下降。

## 7. 下一步学习建议

下一讲是 [类型转换系统：cast、Type、Module 与 Scope](u2-l3-cast-type-module-scope.md)。本讲反复出现的 `cast!` 宏（如 `ToDict`、`ToBytes`、`ToArray`）正是下一讲的主题：你会看到 `Reflect`/`IntoValue`/`FromValue`/`CastInfo` 如何把 Rust 类型与 `Value` 双向桥接。建议阅读：

- [src/foundations/cast.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs) —— `cast!` 宏与三段式转换。
- [src/foundations/scope.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs) —— `Dict` 的 `get` 与 `Module→Dict` 转换背后真正的命名空间实现。
- 若对 `Label` 如何被 `query` 使用感兴趣，可提前跳读 [src/introspection/query.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs)（内省单元会系统讲解）。
