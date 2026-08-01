# 去重机制 Deduplicator 与 ID 编码

## 1. 本讲目标

本讲拆解 typst-svg 里一个「不渲染任何东西、却决定了输出文件能小多少」的底层基础设施：`Deduplicator`。它是贯穿前几讲的去重主角——字形的 `<symbol>` 复用（u4-l1/u4-l2）、渐变与平铺的「源 + 引用」两层模型（u5-l2 ~ u5-l5）、裁剪路径的集中定义（u2-l2），背后都靠它支撑。

学完后你应当能够：

- 说清 `Deduplicator<T>` 的数据结构，以及为什么它只对**值类型** `T` 泛型、却把任意**键类型** `K` 都归约成 `u128`。
- 解释 `insert_with` / `insert_with_val` 的「按 key 哈希、惰性构造值」语义，以及闭包 `f` 为什么只在首次出现时才被调用。
- 手工推演一个 `DedupId` 如何被编码成「kind 字符 + 大写十六进制哈希」的字符串，理解 `to_be_bytes` 与 `trim_start_matches('0')` 的作用。
- 理解 7 个 `Deduplicator` 用不同 kind 字符（`g/c/f/r/s/t/p`）划分 ID 命名空间，以及为什么用 `typst_utils::hash128` 而非直接把 key 存进 map。

## 2. 前置知识

- **SVG 的 `<defs>` + 引用复用模型**：可复用资源（字形、裁剪路径、渐变、平铺图案）先在 `<defs>` 里定义一次并赋予 `id`，正文里用 `url(#id)`、`<use href="#id">`、`clip-path="url(#id)"` 等方式引用。这就是「去重」能压缩体积的根本原因。
- **哈希（hash）**：把任意长度的数据压缩成定长「指纹」的函数。typst 用 128 位（16 字节）的 SipHasher13，碰撞概率小到可忽略。
- **`IndexMap`**：一个保留插入顺序的哈希表。这里既要按哈希快速查找，又要在 `finalize` 阶段按「登记顺序」写出 `<defs>`，所以选它而不是普通 `HashMap`。
- **RAII / `Drop`**：Rust 中值离开作用域时自动运行析构。typst-svg 用它自动闭合 XML 标签（见 u2-l3 的 `SvgElem`）。

承接 u2-l1：你已经知道 `SVGRenderer` 持有 7 个 `Deduplicator` 字段，渲染期间只「登记 + 写引用」，真正的 `<defs>` 定义在 `finalize` 阶段集中写出。本讲回答这些字段本身是如何工作的。

## 3. 本讲源码地图

本讲全部源码集中在两个文件：

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs) | 定义 `Deduplicator<T>`、`DedupId`、`impl SvgDisplay for DedupId`，以及 `SVGRenderer` 的 7 个去重字段与 `finalize` 写出顺序。 |
| [src/write.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs) | 定义 `SvgWrite` / `SvgDisplay` trait（`DedupId` 就是通过它们流式写入属性值的），以及 `SvgUrl` / `SvgIdRef` 适配器如何把 `DedupId` 包成 `url(#id)`、`#id`。 |

辅助理解：`hash128` 的实现位于 [crates/typst-utils/src/hash.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs)，本讲会引用它解释「为什么哈希、为什么稳定」。

---

## 4. 核心概念与源码讲解

### 4.1 Deduplicator\<T>：去重容器与七个 ID 命名空间

#### 4.1.1 概念说明

一篇 Typst 文档里，同一个字形（如字母 `a`）、同一条裁剪曲线、同一个渐变会被使用成百上千次。如果每次都把完整定义写进 SVG，文件会爆炸式膨胀。`Deduplicator` 就是 typst-svg 的去重账本：**第一次见到某个资源时登记它并生成一个唯一 ID，之后每次再见到同一个资源，只复用那个 ID**。

它的核心设计有两点反直觉之处，理解了这两点就抓住了本讲的灵魂：

1. **键被归约成 `u128`，从不存原始 key。** map 的类型是 `IndexMap<u128, T>`，不是 `IndexMap<K, T>`。因此 `Deduplicator` 只对值类型 `T` 泛型，键类型 `K` 在调用点用泛型约束 `K: Hash` 表达，调用时立刻哈希成 `u128` 再查表。
2. **一个结构定义服务七种完全不同的资源。** 字形、裁剪路径、源渐变、渐变引用、圆锥子渐变、源平铺、平铺引用——它们的键和值类型天差地别，却共用同一个 `Deduplicator<T>` 结构，靠一个 `kind: char` 字段区分 ID 命名空间。

#### 4.1.2 核心流程

一次去重的生命周期分两个阶段：

```text
【渲染阶段：登记 + 写引用】
  对每个出现的资源：
    key = (资源的判等特征)         # 例如 (&font, glyph_id, scale)
    hash = hash128(&key)            # 任意 key → 固定 16 字节
    若 hash 已在 map 中：           # 之前登记过
        返回已有的 DedupId          # 闭包 f 不执行，值不重复构造
    否则：
        val = f()                   # 首次出现，惰性构造值并存入 map
        返回新的 DedupId(kind, hash)
  在正文里写出引用：fill="url(#DedupId)" / <use href="#DedupId">

【finalize 阶段：集中写出定义】
  for (id, val) in dedup.iter():    # 按 map 的插入顺序
      在 <defs> 里写出 val 的完整定义，id 作为元素 id
```

关键不变量：**渲染期只写引用，定义期才写真身**。这正是 u4-l2 中 `assert!(self.glyphs.is_empty())` 能成立的前提——写字形定义本身不会产生新字形，因为登记早在渲染期就完成了。

#### 4.1.3 源码精读

**结构定义**（注意它只对 `T` 泛型，键已被抹平为 `u128`）：

[src/lib.rs:478-482](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L478-L482) —— `Deduplicator` 持有一个 kind 字符和一张 `IndexMap<u128, T, FxBuildHasher>`：

```rust
struct Deduplicator<T> {
    kind: char,
    map: IndexMap<u128, T, FxBuildHasher>,
}
```

> 阅读提示：结构体上方的文档注释（[src/lib.rs:474-477](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L474-L477)）说「`H` is the hash type … `PREFIX` is the prefix」，但实际代码里既没有 `H` 泛型、也没有 `PREFIX` 常量——这是个**过时的注释**，以代码为准：键统一是 `u128`，前缀是运行期字段 `kind`。这是「读代码而非只读注释」的一个真实例子。

**七个字段的声明**，集中体现「同一个结构服务七种资源」：

[src/lib.rs:192-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L192-L224) —— `SVGRenderer` 持有 7 个 `Deduplicator`（值类型各不相同）：

```rust
glyphs:            Deduplicator<Option<RenderedGlyph>>,
clip_paths:        Deduplicator<EcoString>,
gradients:         Deduplicator<(Gradient, Ratio)>,
gradient_refs:     Deduplicator<GradientRef>,
conic_subgradients: Deduplicator<SVGSubGradient>,
tilings:           Deduplicator<Tiling>,
tiling_refs:       Deduplicator<TilingRef>,
```

**kind 字符的赋值**，对应七个 ID 命名空间：

[src/lib.rs:272-283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L272-L283) —— `with_options` 里逐个用不同 kind 字符初始化：

```rust
glyphs:             Deduplicator::new('g'),  // glyph
clip_paths:         Deduplicator::new('c'),  // clip
gradients:          Deduplicator::new('f'),  // gradient 源
gradient_refs:      Deduplicator::new('r'),  // ref
conic_subgradients: Deduplicator::new('s'),  // subgradient
tilings:            Deduplicator::new('t'),  // tiling 源
tiling_refs:        Deduplicator::new('p'),  // pattern ref
```

这 7 个字符就是输出 SVG 里每个 `id` 的首字母。因此你看到 `g00…` 就知道是字形、`c00…` 是裁剪路径、`f00…`/`r00…` 是渐变源/引用、`s00…` 是圆锥子渐变、`t00…`/`p00…` 是平铺源/引用。**不同命名空间共用同一张哈希表也绝不会撞 ID**，因为前缀字符不同。

`iter()` 与 `is_empty()` 是 `finalize` 阶段的辅助方法：

[src/lib.rs:515-522](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L515-L522) —— `iter()` 把每个 `(u128, &T)` 重新组装回 `(DedupId, &T)`，这样写出定义时能拿到和渲染期完全一致的 ID：

```rust
fn iter(&self) -> impl Iterator<Item = (DedupId, &T)> {
    self.map.iter().map(|(hash, v)| (DedupId(self.kind, *hash), v))
}
```

以裁剪路径为例，`write_clip_path_defs` 正是用 `iter()` 按登记顺序集中写出 `<defs>`：

[src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433) —— 渲染期只写 `clip-path="url(#c…)"` 引用，这里才真正写出 `<clipPath id="c…"><path d="…"/></clipPath>` 真身：

```rust
let mut defs = svg.elem("defs");
for (id, path) in self.clip_paths.iter() {
    defs.elem("clipPath").attr("id", id).with(|svg| {
        svg.elem("path").attr("d", path);
    });
}
```

#### 4.1.4 代码实践

**实践目标**：跟踪一处真实的 `insert_with` 调用，验证「渲染期只登记 + 写引用」。

**操作步骤**：

1. 打开 [src/lib.rs:351-357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L351-L357)，这是 `render_group` 里处理裁剪的代码：

   ```rust
   if let Some(clip_curve) = &group.clip {
       let offset = Point::new(state.transform.tx, state.transform.ty);
       let id = self.clip_paths.insert_with((clip_curve, offset), || {
           shape::convert_curve(offset, clip_curve)
       });
       svg.init().attr("clip-path", SvgUrl(id));
   }
   ```

2. 回答两个问题：
   - 这里的 key 是什么？值（闭包返回值）是什么？
   - 同一条裁剪曲线出现在同一偏移时，`convert_curve` 会被调用几次？

**预期结果**：

- key 是 `(clip_curve, offset)`——曲线本身 + 它在页面上的平移偏移。为什么要把 `offset` 算进 key？因为同一个 curve 在不同位移下生成的 path 数据 `d` 不同，必须分别登记。
- 值是 `convert_curve(offset, clip_curve)` 产生的 `EcoString`（path 数据）。由于 `insert_with` 内部用 `or_insert_with`（见 4.2.3），同一个 `(curve, offset)` 第二次出现时闭包**不执行**，`convert_curve` 只在首次被调用一次。这正是去重的意义。

**待本地验证**：用 `cargo build -p typst-svg` 确认编译通过；若想观察运行时行为，可在 `insert_with_val` 的 `or_insert_with` 分支后临时加一行日志（仅用于学习，勿提交），统计某文档里 `convert_curve` 的实际调用次数是否远小于裁剪 group 的总数。

#### 4.1.5 小练习与答案

**练习 1**：七个 `Deduplicator` 的 kind 字符分别是哪七个？为什么渐变和平铺各占两个字符（`f`/`r` 与 `t`/`p`）？

> **答案**：`g/c/f/r/s/t/p`。渐变和平铺各占两个，是因为它们采用「源 + 引用」两层去重（见 u5-l2）：源不带变换（`f`/`t`），引用只记「源 ID + 使用变换」（`r`/`p`）。两层用不同命名空间，避免带变换的引用和不带变换的源挤在同一张表里互相污染。

**练习 2**：为什么 `Deduplicator` 用 `IndexMap` 而非标准库 `HashMap`？

> **答案**：`IndexMap` 保留插入顺序。`finalize` 用 `iter()` 按登记顺序写出 `<defs>`，这让输出更确定、更易 diff，也让相关资源在文件中聚拢。`HashMap` 的迭代序不确定，会破坏输出的可复现性。

---

### 4.2 insert_with / insert_with_val：按 key 哈希、惰性构造值

#### 4.2.1 概念说明

`insert_with` 与 `insert_with_val` 是 `Deduplicator` 的核心 API，它们把「去重」和「按需构造」绑定在一起。设计要点有三个：

1. **键泛型 `K: Hash`，不进入结构。** 调用点决定 key 的形状（元组、字符串、引用都行），方法内部立刻 `hash128(&key)` 归约成 `u128` 再查表。这正是上一节「只对 `T` 泛型」的实现机制。
2. **惰性构造值。** 值通过闭包 `F: FnOnce() -> T` 提供。闭包只在 key **首次**出现时执行（靠 `IndexMap::entry().or_insert_with` 实现），已存在则直接跳过。这对昂贵的值（字形轮廓提取、整段 SVG 字符串）至关重要——重复出现时零开销。
3. **`#[must_use]` 强制使用返回的 ID。** 两个方法都标了 `#[must_use]`，因为去重的全部价值就在于拿到 ID 去写引用；丢弃返回值等于白做。

两者唯一区别：`insert_with` 只返回 `DedupId`；`insert_with_val` 额外返回 `&mut T`，供调用方读取或改写缓存值。

#### 4.2.2 核心流程

`insert_with` 是 `insert_with_val` 的薄包装：

```text
fn insert_with(key, f) -> DedupId:
    return insert_with_val(key, f).0   # 取二元组的第一个元素

fn insert_with_val(key, f) -> (DedupId, &mut T):
    hash = typst_utils::hash128(&key)          # K → u128
    val = map.entry(hash).or_insert_with(f)    # 已存在则用旧值、f 不执行；否则插入 f()
    return (DedupId(kind, hash), val)
```

时间复杂度：`entry` + `or_insert_with` 是 `IndexMap` 的摊还 O(1) 操作（一次哈希 + 一次查找/插入）。

#### 4.2.3 源码精读

**`insert_with`**——委托给 `insert_with_val`，只丢掉值的引用：

[src/lib.rs:492-499](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L492-L499)：

```rust
#[must_use = "returns the id of the inserted value"]
fn insert_with<K, F>(&mut self, key: K, f: F) -> DedupId
where
    K: Hash,
    F: FnOnce() -> T,
{
    self.insert_with_val(key, f).0
}
```

**`insert_with_val`**——本讲的核心，三行说尽一切：

[src/lib.rs:503-512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L503-L512)：

```rust
#[must_use]
fn insert_with_val<K, F>(&mut self, key: K, f: F) -> (DedupId, &mut T)
where
    K: Hash,
    F: FnOnce() -> T,
{
    let hash = typst_utils::hash128(&key);
    let val = self.map.entry(hash).or_insert_with(f);
    (DedupId(self.kind, hash), val)
}
```

注意三个细节：

- `key: K` 没有任何 trait 约束要求它 `Clone` 或 `Eq`——因为它从不会被存入 map，只是被借用一下就哈希掉。调用方传入的 key（哪怕是借用 `&Font`）无需付出克隆代价。
- `or_insert_with(f)` 是惰性的关键：`f` 作为闭包传进去，**仅当 `hash` 不存在时**才被调用求值。
- 返回的 `DedupId(self.kind, hash)` 直接用刚算出的 `hash` 和该表的 `kind` 拼装，保证渲染期与 `finalize` 期 `iter()` 重建出的 ID 完全一致。

**典型调用：字形的「键含 scale」**（承接 u4-l1 的取舍）：

[src/text.rs:64-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L73)——轮廓字形的 key 是 `(&text.font, glyph_id, scale)`：

```rust
let scale = Ratio::new(text.size.to_pt() / text.font.units_per_em());
let key = (&text.font, glyph_id, scale);
let (id, path) = self.glyphs.insert_with_val(key, || {
    let mut builder = SvgPathBuilder::with_scale(scale);
    text.font.ttf().outline_glyph(glyph_id, &mut builder)?;
    Some(RenderedGlyph::Path(builder.finsish()))
});
```

同一个字形在 12pt、14pt 下是不同的 key（scale 不同），因此各登记一份；但 100 个 12pt 的 `a` 共享同一个 key，`outline_glyph` 只执行一次。惰性构造 + 哈希查表让这两件事同时成立。

**渐变的「源 + 引用」分层调用**（承接 u5-l2）：

[src/paint.rs:66-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L66-L85)——先登记源（键不含变换 `ts`），再用 `(gradient_id, ts)` 登记引用：

```rust
let gradient_id = self
    .gradients
    .insert_with((gradient, aspect_ratio), || (gradient.clone(), aspect_ratio));
if ts.is_identity() { return gradient_id; }
self.gradient_refs.insert_with(&(gradient_id, ts), || GradientRef { /* … */ })
```

这里 `insert_with` 的键是一个引用元组 `&(DedupId, Transform)`——又一次体现「键不进结构、只被哈希」。

#### 4.2.4 代码实践

**实践目标**：解释「为什么用 `typst_utils::hash128(&key)` 而非直接把 key 存进 map」。这是本讲的核心思考题。

**操作步骤**：先阅读 `hash128` 的实现：

[crates/typst-utils/src/hash.rs:13-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L13-L33)——它用一个 **固定密钥** 的 `SipHasher13`，并把 `usize` 当 `u64` 哈希：

```rust
pub fn hash128<T: Hash + ?Sized>(value: &T) -> u128 {
    /* StableHasher = SipHasher13::new()（固定密钥），usize → u64 */
    let mut state = StableHasher(SipHasher13::new());
    value.hash(&mut state);
    state.0.finish128().as_u128()
}
```

然后从三个角度组织你的答案（参考要点如下）：

1. **存储与克隆开销**：key 类型千差万别且常常很大——字形键含 `&Font` 引用背后的字体表、平铺键含整段渲染出的 SVG 字符串（见 u5-l5 的 `rendered.as_str()`）、渐变键含 `Gradient` 结构。若把 key 直接存进 map，就必须为每个 key 克隆一份常驻内存；哈希成 16 字节的 `u128` 后，map 里只存定长小键，内存占用骤降。
2. **统一结构定义**：键归约成 `u128` 后，`Deduplicator<T>` 只需对值类型 `T` 泛型，一个结构定义就能服务 7 种键形状迥异的资源（见 4.1.3）。否则得为每种 key 写一个专门的结构。
3. **可复现性 / 跨架构稳定**：`DedupId` 会被**原样写进 SVG 文件**作为 `id`。这意味着同一份文档在 32 位/64 位机器上、在不同进程里，必须产出**字节级相同**的 ID，否则破坏可复现构建（reproducible build）和缓存。`hash128` 用固定密钥的 `SipHasher13::new()`、并把 `usize` 哈希成 `u64`（消除指针宽度差异），正是为此而稳定。若改用标准库 `HashMap` 默认的 `RandomState`（每次进程启动注入随机种子），ID 会逐次漂移，输出文件无法复现。

**预期结果**：能说出「省内存 + 统一泛型 + 输出可复现」这三条，并指出第三条是 typst 选择**固定密钥**哈希（而非随机化）的决定性理由。

**代价（辩证思考）**：哈希存在碰撞风险——两个不同 key 理论上可能映射到同一 `u128`，导致错误去重。128 位 SipHasher13 的碰撞概率约 \(2^{-128}\)，在实践中可忽略；typst 接受这个权衡换取体积与可复现性。

#### 4.2.5 小练习与答案

**练习 1**：`insert_with` 和 `insert_with_val` 的区别是什么？什么场景下必须用后者？

> **答案**：前者只返回 `DedupId`；后者额外返回 `&mut T`，允许调用方读取/改写缓存值。必须用后者的场景是「登记之后还要用到值本身」，例如 u4-l1 中字形登记后要判断 `path.is_some()` 决定是否写出 `<use>`（[src/text.rs:69-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L69-L79)）。若只需 ID 写引用（如裁剪路径 [src/lib.rs:353-356](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L353-L356)），用前者即可。

**练习 2**：为什么两个方法都标注了 `#[must_use]`？如果调用者忽略返回值会发生什么？

> **答案**：去重的全部意义在于拿到 `DedupId` 去写 `url(#id)` / `href="#id"` 引用。忽略返回值意味着资源被登记了却没人引用它——`<defs>` 里会多出一个无用的定义（白白增加体积），且闭包 `f` 的构造工作也白做了。`#[must_use]` 把这种「逻辑错误」升级为编译期警告。

---

### 4.3 DedupId 与 SvgDisplay：kind 字符 + 大写十六进制编码

#### 4.3.1 概念说明

`DedupId` 是去重资源的「身份证」，它最终会变成 SVG 文件里 `id="…"`、`url(#…)`、`href="#…"` 中的那段字符串。它的设计极简：一个 `char`（命名空间前缀）加一个 `u128`（哈希值）。

```rust
struct DedupId(char, u128);
```

把它变成字符串靠的是 `impl SvgDisplay for DedupId`（见 u2-l3 对 `SvgDisplay` trait 的介绍）。编码规则用一句话概括：

> **kind 字符 + 把 u128 的 16 个字节展开成 32 个大写十六进制位、再去掉所有前导零。**

三个细节值得记住：

- **大端序（big-endian）**：用 `u128::to_be_bytes()`，最高位字节在前。这保证「字典序」与「数值序」一致，ID 在文件中天然有序。
- **大写 A–F**：`to_hex_digit` 对 10–15 输出 `b'A' + (nibble - 10)`，故 hex 是大写。
- **去掉前导零**：`trim_start_matches('0')` 只删**前导**零，不删内部和尾部的零。这让小哈希值的 ID 很短；对真实的大哈希值（通常 32 位全满），前导零罕见，ID 一般是 33 个字符（1 前缀 + 32 hex）。

#### 4.3.2 核心流程

```text
fn fmt(DedupId(kind, hash)) -> String:
    输出 kind 字符                            # 例如 'c'
    bytes = hash.to_be_bytes()               # [u8; 16]，大端
    for 每个 byte (高半字节, 低半字节):
        digits += to_hex_digit(高半字节)       # 0..9 → '0'..'9'，10..15 → 'A'..'F'
        digits += to_hex_digit(低半字节)
    # digits 现在是 32 个大写 hex 字符
    输出 digits.trim_start_matches('0')       # 删除所有前导 '0'
```

输出形如 `c30F5AB`（见 4.3.4 的手工推演）。

#### 4.3.3 源码精读

**`DedupId` 的定义**——一个普通元组结构体，派生了全套比较与哈希 trait：

[src/lib.rs:525-527](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L525-L527)：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct DedupId(char, u128);
```

**`SvgDisplay` 实现**——本节的编码主体：

[src/lib.rs:529-551](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L529-L551)：

```rust
impl SvgDisplay for DedupId {
    fn fmt(&self, f: &mut impl SvgWrite) {
        let Self(kind, hash) = *self;
        f.push_char(kind);

        let mut digits = [0; 32];
        for (i, byte) in hash.to_be_bytes().into_iter().enumerate() {
            digits[2 * i]     = to_hex_digit((byte >> 4) & 0x0F);  // 高半字节
            digits[2 * i + 1] = to_hex_digit( byte       & 0x0F);  // 低半字节
        }
        let str = std::str::from_utf8(&digits).unwrap();
        f.push_str(str.trim_start_matches('0'));

        fn to_hex_digit(nibble: u8) -> u8 {
            match nibble {
                0..10 => b'0' + nibble,
                _     => b'A' + (nibble - 10),
            }
        }
    }
}
```

逐行解读：

- `f.push_char(kind)`：先写命名空间前缀字符（来自 [src/write.rs:99-101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L99-L101) 的 `SvgWrite::push_char` 默认实现，把 char 编码成 UTF-8 再 `push_str`）。
- `digits = [0; 32]`：固定 32 字节缓冲区（16 字节 × 每字节 2 个 hex 位），栈上分配、零拷贝。
- `(byte >> 4) & 0x0F` 取高 4 位、`byte & 0x0F` 取低 4 位，分别转一个 hex 字符。
- `std::str::from_utf8(&digits).unwrap()`：因为这些字节全是 ASCII（`0..9`、`A..F`），UTF-8 转换必定成功，故 `unwrap` 安全。
- `trim_start_matches('0')`：删除**所有前导** `'0'`。

**两个边界情况**：

1. 若哈希值很小，前导零很多，ID 会很短（见 4.3.4）。
2. 极端地，若 `hash == 0`，32 位全是 `'0'`，`trim_start_matches('0')` 会得到**空串**，最终 ID 只剩孤零零的 kind 字符（如 `"g"`）。这在 128 位哈希下几乎不可能发生，但代码层面是合法的——单字母仍是合法 XML id（以字母开头）。

**`DedupId` 如何流进 SVG 属性**：靠 `write.rs` 的两个适配器包装：

[src/write.rs:295-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L295-L311)——`SvgUrl<DedupId>` 和 `SvgIdRef<DedupId>` 分别产出 `url(#…)` 与 `#…`，内部都调 `f.push(self.0)` 触发上面的 `fmt`：

```rust
impl SvgDisplay for SvgUrl<DedupId> {
    fn fmt(&self, f: &mut impl SvgWrite) {
        f.push_str("url(#");
        f.push(self.0);   // ← 触发 impl SvgDisplay for DedupId
        f.push_str(")");
    }
}
// SvgIdRef<DedupId> 同理产出 "#<id>"
```

所以 `render_group` 里 `svg.attr("clip-path", SvgUrl(id))`（[src/lib.rs:356](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L356)）最终写出 `clip-path="url(#c30F5AB)"` 这样的文本。

#### 4.3.4 代码实践

**实践目标**：手工推演一个 `u128` 哈希值经 `to_be_bytes` + `to_hex_digit` + `trim_start_matches('0')` 后得到的最终 ID 字符串。

**给定**：假设某裁剪曲线的 key 经 `hash128` 得到 `hash = 0x0000_0000_0000_0000_0000_0000_0030_F5AB`（为便于演示，取一个有大量前导零的小值；真实哈希通常 32 位全满）。kind 字符为 `'c'`（裁剪路径）。

**步骤 1：`to_be_bytes()`**——大端序，最高位字节在前，得到 16 字节：

```text
索引: 0    1    2    3    4    5    6    7    8    9   10   11   12   13   14   15
字节: 00   00   00   00   00   00   00   00   00   00   00   00   00   30   F5   AB
```

前 13 个字节（索引 0–12）都是 `0x00`，只有最后 3 个字节非零：`0x30, 0xF5, 0xAB`。

**步骤 2：每个字节拆成两个 hex 字符**（高半字节在前）：

| 字节 | 高半字节 → 字符 | 低半字节 → 字符 | 结果 |
|------|----------------|----------------|------|
| `0x00`（×13） | `0x0` → `'0'` | `0x0` → `'0'` | `"00"`（×13 = 26 个 `'0'`） |
| `0x30` | `(0x30>>4)=0x3` → `'3'` | `0x30&0x0F=0x0` → `'0'` | `"30"` |
| `0xF5` | `0xF` → `'F'` | `0x5` → `'5'` | `"F5"` |
| `0xAB` | `0xA` → `'A'` | `0xB` → `'B'` | `"AB"` |

校验 `to_hex_digit`：`0x3=3 < 10` → `b'0'+3 = '3'`；`0xF=15 ≥ 10` → `b'A'+(15-10) = 'A'+5 = 'F'`；`0xA=10` → `b'A'+0 = 'A'`。✓

拼成 32 字符的 `digits`：

```text
"00000000000000000000000000" + "30" + "F5" + "AB"
 = "0000000000000000000000000030F5AB"
   └── 26 个前导 '0' ──┘ └─ 非零部分 ─┘
```

（26 + 6 = 32 字符 ✓）

**步骤 3：`trim_start_matches('0')`**——删除**所有前导** `'0'`，遇到第一个非零字符 `'3'` 停止：

```text
"0000000000000000000000000030F5AB"  →  "30F5AB"
```

注意：`"30F5AB"` 里的 `'0'`（在 `'3'` 和 `'F'` 之间）是**内部**零，不被删除——`trim_start_matches` 只看前导。

**步骤 4：拼上 kind 字符 `'c'`**，得到最终 ID：

```text
"c30F5AB"
```

**步骤 5：进入 SVG 的最终形态**——经 `SvgUrl(id)` 包装后：

```text
clip-path="url(#c30F5AB)"
```

**需要观察的现象**：

- 真实的 128 位哈希几乎没有前导零，所以 `trim_start_matches('0')` 通常一个字符都删不掉，ID 一般是 **33 个字符**（1 kind + 32 hex）。
- 由于用大端序 + 大写 hex，多个 ID 在 `<defs>` 里会按字典序自然聚拢，diff 友好。

**待本地验证**：上述推演可用一段最小 Rust 程序核对（仅作学习参考，不属于项目代码）：

```rust
// 示例代码：验证 DedupId 编码逻辑（非项目原有代码）
let hash: u128 = 0x0000_0000_0000_0000_0000_0000_0030_F5AB;
let mut s = String::from("c");
for byte in hash.to_be_bytes() {
    s.push_str(&format!("{:02X}", byte)); // 大写、两位、补零
}
let trimmed = s.trim_start_matches('0');
// 注意：上面 format! 会把 "c" 之后的前导零也补齐，需手动只 trim hex 部分
```

更准确的做法是直接对照 [src/lib.rs:529-551](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L529-L551) 的逐字节逻辑写一个小测试，断言 `0x0030F5AB` 在 kind `'c'` 下编码为 `"c30F5AB"`。

#### 4.3.5 小练习与答案

**练习 1**：若 `hash = 0x1000_0000_0000_0000_0000_0000_0000_0000`（最高位字节是 `0x10`），kind 为 `'f'`，最终 ID 是什么？`trim_start_matches('0')` 会删掉任何字符吗？

> **答案**：最高字节 `0x10` → 高半字节 `'1'`、低半字节 `'0'`，所以 `digits` 以 `"10…"` 开头，**没有前导零**（第一个字符是 `'1'`）。`trim_start_matches('0')` 一个字符都不删，最终 ID 是 `"f10"` + 后 30 个 hex 字符 = `"f10" + "000000000000000000000000000000"`（共 32 个 hex 位），即 `"f1000000000000000000000000000000"`（33 字符）。

**练习 2**：为什么用 `to_be_bytes()`（大端序）而不是 `to_le_bytes()`（小端序）？

> **答案**：大端序让哈希的「数值高位」对应字符串的「前导字符」，于是字典序与数值序一致。这让 ID 在 `<defs>` 中天然有序、可读性更好，且若两 ID 仅末尾不同，diff 时差异集中在行尾。小端序会把低位字节放前面，破坏这种有序性。

**练习 3**：`trim_start_matches('0')` 与 `trim_start('0')` 行为有区别吗？如果哈希全为零会怎样？

> **答案**：对单字符参数 `'0'`，两者等价（都删除所有前导 `'0'`）。若 `hash == 0`，32 位全是 `'0'`，会被删成**空串**，ID 只剩 kind 字符（如 `"g"`）。这是代码层面合法但概率近乎为零的边界情况。

---

## 5. 综合实践

**任务**：跟踪一个裁剪路径从「登记」到「写出定义」的完整去重生命周期，并手工计算它的 ID。

**背景**：假设一个文档里有 3 个 group 都带裁剪，其中前两个的裁剪曲线和偏移完全相同，第三个不同。

**步骤**：

1. **定位登记点**：在 [src/lib.rs:351-357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L351-L357)，确认渲染期对每个裁剪 group 调用 `self.clip_paths.insert_with((clip_curve, offset), || …)`，并把返回的 `id` 写进 `clip-path="url(#id)"`。
2. **推演去重次数**：回答——`convert_curve` 总共被调用几次？map 里最终有几条记录？3 个 group 写出的 `clip-path` 引用各是什么？
3. **定位定义点**：在 [src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433)，确认 `finalize` 阶段用 `iter()` 把 map 里的记录按登记顺序写成 `<clipPath id="c…"><path d="…"/></clipPath>`。
4. **手工算 ID**：假设第 1 个裁剪 key 的 `hash128` 结果为 `0x0000_0000_0000_0000_0000_0000_0030_F5AB`（kind `'c'`），按 4.3.4 的方法推出它的 `id` 字符串和完整 `clip-path` 属性值。
5. **画出时序**：用一张图表示「渲染期（3 次 insert_with、2 次命中）→ finalize（2 条定义写出）」的资源与 ID 流。

**预期结果**：

- `convert_curve` 被调用 **2 次**（第 1、3 个 group 首次出现时；第 2 个 group 命中已登记记录，闭包不执行）。
- map 里有 **2 条**记录（两条不同的 `(curve, offset)`）。
- 第 1、2 个 group 写同一个引用 `url(#c30F5AB)`，第 3 个 group 写另一个引用 `url(#c<另一哈希>)`。
- `finalize` 在 `<defs>` 里写出 **2 个** `<clipPath>` 定义，与渲染期引用通过 ID 一一对应。
- 第 1 条裁剪路径的 `id = "c30F5AB"`，`clip-path="url(#c30F5AB)"`。

这个练习把「渲染期登记 + 引用」与「finalize 期集中定义」两阶段、以及 ID 编码串联起来，覆盖了本讲的全部核心。

## 6. 本讲小结

- `Deduplicator<T>` 是 typst-svg 的去重账本：只对值类型 `T` 泛型，键在调用点被 `hash128` 归约成 `u128`，因此一个结构定义能服务 7 种键形状迥异的资源。
- `insert_with` / `insert_with_val` 把「按 key 哈希查重」与「闭包惰性构造值」绑定：闭包 `f` 仅在 key 首次出现时执行（`or_insert_with`），重复出现零开销；两者都 `#[must_use]` 强制使用返回的 `DedupId`。
- 用 `hash128`（固定密钥 SipHasher13、usize→u64）而非直接存 key，换来三重收益：**省内存**（不克隆大 key）、**统一泛型**（一个结构服务七种资源）、**输出可复现**（ID 原样写进文件，必须跨进程跨架构稳定）。
- `DedupId(char, u128)` 的编码为「kind 字符 + 大端 32 位大写 hex + 去前导零」，kind 字符 `g/c/f/r/s/t/p` 划分七个 ID 命名空间，互不碰撞。
- 渲染期只写引用（`url(#id)`、`href="#id"`），`finalize` 期才用 `iter()` 按登记顺序集中写出 `<defs>` 真身——这是 u4-l2 那条 `assert!(self.glyphs.is_empty())` 不变量成立的根基。

## 7. 下一步学习建议

本讲讲完了去重机制的「底层容器」。建议接下来：

- **回到应用层印证**：重读 u5-l2（填充/描边入口与去重引用模型），现在你能从 `write_fill` → `push_gradient` → `insert_with` 一路看懂「源 + 引用」两层去重是如何落在 `Deduplicator` 上的。
- **阅读 u6-l4（链接、锚点与 HTML/Bundle 集成）**：看 `finalize` 如何按固定顺序调用 `write_glyph_defs` / `write_gradients` / `write_tilings` 等八个写出方法（[src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)），把本讲的 7 个 `Deduplicator` 串成完整的 `<defs>` 输出。
- **动手实验**：挑一篇含重复字形和渐变的 Typst 文档，导出 SVG 后用浏览器开发者工具搜索 `id="g`、`id="f`、`id="r`，亲眼数一数不同 kind 前缀的 ID 各有多少个，验证「去重」的实际体积收益。
