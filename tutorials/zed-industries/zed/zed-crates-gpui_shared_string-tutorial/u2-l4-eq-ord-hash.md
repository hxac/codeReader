# 相等、排序与哈希：跨类型比较

## 1. 本讲目标

学完本讲,你应该能够:

1. **掌握为自定义类型实现跨类型 `PartialEq` 的标准写法**——为什么需要手写、写哪几个方向、每个 impl 为什么合法。
2. **解释为什么相等性只比较字符串内容,而与构造路径(静态 / 内联 / 堆)无关**——`new_static("a")` 与 `new(&"a".to_string())` 为何相等。
3. **理解 `Eq` / `Ord` / `Hash` 与 `Borrow<str>` 如何共同保证 `HashMap` 行为正确**——把 u2-l2 里提出的「Borrow 契约」落实到具体的 trait 实现上。

本讲是进阶单元的收官:u2-l1 讲了 derive 委托链的概貌,u2-l2 讲了 `Deref` / `AsRef` / `Borrow` 三个透明代理,u2-l3 讲了 `From` 转换矩阵。本讲把剩下的一块拼图——**相等、排序与哈希语义**——补完整。

## 2. 前置知识

### 2.1 五个 trait 一览

本讲的标题就是五个标准库 trait,先用一个表格建立直觉:

| trait | 回答的问题 | 关键方法 | 一句话理解 |
|---|---|---|---|
| `PartialEq<Rhs>` | `self == other` 吗? | `fn eq(&self, other: &Rhs) -> bool` | 相等判定,默认 `Rhs = Self` |
| `Eq` | 相等关系是不是**等价关系**? | 无(标记 trait) | `PartialEq` 的加强承诺:自反、对称、传递 |
| `PartialOrd<Rhs>` | `self < other` 吗? | `fn partial_cmp(...)` | 偏序,允许「无法比较」 |
| `Ord` | 任意两个值能否排出全序? | `fn cmp(...) -> Ordering` | 全序,`Eq` + 每对元素必有序 |
| `Hash` | 这个值的哈希值是什么? | `fn hash<H: Hasher>(&self, state: &mut H)` | 把字节喂给哈希器 |

几个容易混淆的点:

- `Eq` 没有任何方法,它只是向标准库「承诺」相等关系是等价关系。`f64` 因为 `NaN != NaN` 违反自反性,所以只是 `PartialEq` 而非 `Eq`。字符串没有这个问题,所以 `str` / `String` / `SharedString` 都是 `Eq`。
- **`HashMap` 的键要求 `Eq + Hash`,`BTreeMap` 的键要求 `Ord`**。这就是本讲讲的五个 trait 与容器的直接关系。

### 2.2 derive 与手写 impl 的分工

`#[derive(...)]` 让编译器自动生成 trait 实现。对只有一个字段的 newtype 来说,derive 生成的代码等价于「逐字段委托」:

- `derive(PartialEq)` ⇒ `self.0 == other.0`(调用 `SmolStr` 的 `PartialEq`)
- `derive(Hash)` ⇒ `self.0.hash(state)`(调用 `SmolStr` 的 `Hash`)
- `derive(Ord)` ⇒ `self.0.cmp(&other.0)`(调用 `SmolStr` 的 `Ord`)

但 derive 只会生成 **`SharedString == SharedString`** 这一个方向。要让 `shared == "a"`、`shared == string`、`string == shared` 编译通过,必须手写跨类型 impl——这正是本讲 4.2 的主题。

### 2.3 哈希一致性:HashMap 的隐形合同

`HashMap` 能正确工作的前提是**相等与哈希一致**:

\[ \forall a, b:\quad a = b \;\Rightarrow\; H(a) = H(b) \]

即相等的两个键**必须**产生相同的哈希值(反之不要求,哈希碰撞是允许的)。这条合同 Rust 编译器不检查,全靠类型作者自觉。本讲会验证 `SharedString` 履约,并说明它与 `Borrow<str>` 的配合。

### 2.4 孤儿规则直觉

Rust 不允许给「外部类型」实现「外部 trait」(否则两个 crate 可以互相冲突),这叫孤儿规则(E0117)。但有一条重要例外:**trait 的泛型参数里出现了本地类型时,允许为外部类型实现这个外部 trait**。记住这一点,4.2 里 `impl PartialEq<SharedString> for String` 的合法性才讲得通。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [gpui_shared_string.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs) | 全部核心:第 14 行的 derive、第 86–108 行的四个手写跨类型 `PartialEq`、第 68–72 行的 `Borrow<str>` |
| [semantic_tokens.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs) | 下游真实用例:`HashMap<SharedString, u32>` 且用 `&str` 查键 |
| [registry.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs) | 下游用例:`HashMap<SharedString, Arc<Theme>>` 主题表 |
| [thread.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/agent/src/thread.rs) | 下游用例:`BTreeMap<SharedString, ...>`,依赖 `Ord` |

## 4. 核心概念与源码讲解

### 4.1 derive 出的相等、排序与哈希:按内容生效的委托链

#### 4.1.1 概念说明

u2-l1 已经给出过结论:「六个 derive 沿委托内层、按 str 内容生效」。本讲展开其中与相等/排序/哈希相关的五个(`Eq`、`PartialEq`、`PartialOrd`、`Ord`、`Hash`)的具体机制。

要理解的问题只有一个:**`SharedString` 内部是 `SmolStr`,而 `SmolStr` 内部可能是内联缓冲、可能是堆上的 `Arc<str>`、还可能指向静态数据——比较时到底比的是什么?**

答案是:derive 把比较**逐字段**委托给 `SmolStr`,而 `SmolStr`(0.3.6)公开承诺自己的相等、排序、哈希都按**字符串内容**进行,与内部采用哪种存储无关。于是两层委托叠加,`SharedString` 的所有比较语义最终都落在「字节序列」上。

#### 4.1.2 核心流程

三条委托链可以这样画:

```
相等:  shared_a == shared_b
        └─ derive(PartialEq):  self.0 == other.0          ← 逐字段
            └─ SmolStr 的 PartialEq: 按字符串内容比较
                └─ str 的 PartialEq: 逐字节比较

哈希:  shared.hash(&mut hasher)
        └─ derive(Hash):       self.0.hash(state)         ← 逐字段
            └─ SmolStr 的 Hash: 对内容字节做哈希(与 str 的 Hash 结果一致)

排序:  shared_a.cmp(&shared_b)
        └─ derive(Ord):        self.0.cmp(&other.0)        ← 逐字段
            └─ SmolStr 的 Ord: 按字典序
```

由此得到两条对容器至关重要的性质:

\[ \text{哈希一致:}\quad \text{SharedString 与 \&str 的同一文本} \Rightarrow \text{哈希值相同} \]

\[ \text{序一致:}\quad \text{SharedString 的 Ord} \equiv \text{str 的字典序} \]

再加上 derive 把 `Eq` 一并派生(`SharedString` 是普通字符串,不存在 `NaN` 那种病态相等),`HashMap` / `HashSet` / `BTreeMap` 三类容器的键约束(`Eq + Hash` 或 `Ord`)全部满足。

#### 4.1.3 源码精读

derive 集中在结构体定义的这一行:

[gpui_shared_string.rs:L14-L15](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L14-L15) — 在 `pub struct SharedString(SmolStr)` 上派生 `Eq, PartialEq, PartialOrd, Ord, Hash, Clone` 共六个 trait。本讲关注前五个;`Clone` 在 u2-l1 已讲。

对照三个固有构造方法,后面做「同一文本、不同构造路径」实验时会用到:

[gpui_shared_string.rs:L27-L29](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L27-L29) — `new_static`:`const fn`,从 `&'static str` 构造,内部走 `SmolStr::new_static`,不发生堆分配,数据直接指向程序只读段。

[gpui_shared_string.rs:L32-L34](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L32-L34) — `new`:接受 `impl AsRef<str>`,运行时构造;短字符串内联、长字符串进堆。

derive 的下游依赖在 zed 仓库里随处可见,举三个已确认的真实用例:

[semantic_tokens.rs:L537](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L537) — `modifier_mask: HashMap<SharedString, u32>`:键是 `SharedString`,要求 `Eq + Hash`,由本讲的 derive 提供。

[registry.rs:L60-L61](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L60-L61) — 主题注册表用 `HashMap<SharedString, Arc<Theme>>` 与 `HashMap<SharedString, Arc<IconTheme>>` 存全部主题。

[thread.rs:L1263](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/agent/src/thread.rs#L1263) — `tools: BTreeMap<SharedString, Arc<dyn AnyAgentTool>>`:`BTreeMap` 要求 `Ord`,依赖的正是 `derive(Ord)` 委托出的字典序。

#### 4.1.4 代码实践

**实践目标**:用行为侧实验确认 derive 的相等、排序、哈希都按内容生效(不依赖阅读 `SmolStr` 源码)。

**操作步骤**(沿用 u1-l2 建立的方法:新建独立 cargo 项目,以 path 依赖引入本 crate):

1. 新建项目 `eq-lab`,`Cargo.toml` 中添加:

```toml
[dependencies]
gpui_shared_string = { path = "../zed/crates/gpui_shared_string" }
```

2. 在 `src/main.rs` 写入(标注为**示例代码**):

```rust
use gpui_shared_string::SharedString;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

fn hash_of<T: Hash>(value: &T) -> u64 {
    let mut hasher = DefaultHasher::new();
    value.hash(&mut hasher);
    hasher.finish()
}

fn main() {
    // 1) derive 的同类型相等:不同构造路径、同一文本
    let a = SharedString::new_static("hello");          // 静态路径,零分配
    let b = SharedString::new(&"hello".to_string());     // 运行时构造(内联)
    let c: SharedString = "hello".into();                // From<&str>,与 b 同类路径
    assert!(a == b);
    assert!(b == c);

    // 2) derive 的 Ord:字典序
    assert!(SharedString::new("apple") < SharedString::new("banana"));

    // 3) derive 的 Hash:与 &str 的哈希一致(黑盒验证)
    assert_eq!(hash_of(&a), hash_of(&"hello"));
}
```

3. 运行 `cargo run`。

**需要观察的现象**:三条 `assert!` 全部通过,程序无输出正常退出。

**预期结果**:通过。第 1 组说明相等与构造路径无关;第 3 组说明 `SharedString` 与 `&str` 的哈希一致——这正是 `HashMap` 能跨类型查键的前提之一。若你想亲眼看哈希值,可把 `assert_eq!` 换成 `println!("{} {}", hash_of(&a), hash_of(&"hello"))`。

**待本地验证**:以上断言基于 `SmolStr` 0.3.6 公开文档承诺的内容语义;本实验即行为侧验证,请在本地实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**:`derive(Eq)` 为 `SharedString` 生成了什么方法?

<details>
<summary>参考答案</summary>

什么方法都没生成。`Eq` 是标记 trait(marker trait),没有任何方法,它只是向标准库承诺「相等关系满足自反、对称、传递」。它的实际作用是让 `SharedString` 满足 `HashMap` / `HashSet` 对键的 `Eq` 约束——如果去掉 `derive` 列表里的 `Eq`,4.1.3 里那三张 zed 中的哈希表都无法编译。
</details>

**练习 2**:`derive(PartialEq)` 生成的 `eq` 会比较 `SmolStr` 内部是内联存储还是堆分配吗?

<details>
<summary>参考答案</summary>

不会。derive 生成的代码逐字段委托,即 `self.0 == other.0`,调用的是 `SmolStr` 的 `PartialEq`;而 `SmolStr` 的相等按字符串内容(字节序列)判定,存储方式(内联 / 堆 / 静态)不参与比较。4.1.4 实验中 `new_static("hello") == new(&"hello".to_string())` 通过,就是行为侧证据。
</details>

**练习 3**:如果把 `Hash` 从 derive 列表里去掉,zed 中哪些代码会受影响?

<details>
<summary>参考答案</summary>

所有以 `SharedString` 为键的哈希容器都会不再满足约束 `K: Hash`,例如 `semantic_tokens.rs` 的 `modifier_mask: HashMap<SharedString, u32>`、主题注册表的 `themes: HashMap<SharedString, Arc<Theme>>`,以及一切 `HashSet<SharedString>`(如 markdown 解析器中的 `language_names`)。`BTreeMap` 不受影响,因为它只要求 `Ord`。
</details>

### 4.2 四个手写跨类型 PartialEq 实现逐行解读

#### 4.2.1 概念说明

derive 只给了 `SharedString == SharedString`。但真实代码里到处是 `shared == "字面量"`、`shared == string_var` 这样的比较。Rust 的 `==` 运算符会根据**左右操作数的具体类型**查找对应的 `PartialEq` 实现,类型对不上就直接编译错误。所以本 crate 手写了四个跨类型 impl,把最常用的比较方向补齐:

| impl(源码行) | 支持的表达式 |
|---|---|
| `PartialEq<String> for SharedString`(L86) | `shared == string` |
| `PartialEq<SharedString> for String`(L92) | `string == shared` |
| `PartialEq<str> for SharedString`(L98) | `shared == *str_ref`(与 `str` 本体比) |
| `PartialEq<&'a str> for SharedString`(L104) | `shared == "字面量"` |

注意这份清单**并不对称**:没有 `impl PartialEq<SharedString> for str` / `for &str`,所以 `"a" == shared` 编译不过,得写成 `shared == "a"`(见练习 2)。

这四个 impl 与上一讲的透明代理一脉相承:实现体只有一行,全部通过 `as_ref()` 把 `SharedString` 一侧化归为 `&str`,再利用标准库为 `&str` / `str` / `String` 之间预置的成对比较完成判定。

#### 4.2.2 核心流程

四个 impl 的共同套路,可以用伪代码概括:

```
fn eq(&self, other: &Rhs) -> bool {
    把 self 化归为 &str     // self.as_ref() ⇒ 借用内部 SmolStr 的 &str 视图,零拷贝
    把 other 化归为 &str    // 解引用或直接使用标准库的跨类型比较
    两者按字节比较
}
```

为什么这可行而不会错?因为 4.1 已经确立:`SharedString` 的语义就是「内容」。跨类型比较只需要保证**两边都在同一个维度(str 内容)上比较**,不会出现「一个比存储地址、一个比内容」的错位。

另外要理解 `impl PartialEq<SharedString> for String` 为什么合法:按孤儿规则,「为外部类型 `String` 实现外部 trait `PartialEq`」本来禁止,但 trait 的类型参数 `SharedString` 是本 crate 的本地类型,属于 2.4 节提到的例外,编译器放行。

#### 4.2.3 源码精读

逐行解读四个 impl(全部位于 [gpui_shared_string.rs:L86-L108](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L86-L108)):

[gpui_shared_string.rs:L86-L90](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L86-L90) — `impl PartialEq<String> for SharedString`:`self.as_ref()` 借助上一讲的 `AsRef<str>` 实现拿到 `&str`,与 `&String` 比较(标准库为 `&str` 与 `String` 预置了比较)。支持 `shared == string`。

[gpui_shared_string.rs:L92-L96](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L92-L96) — `impl PartialEq<SharedString> for String`:方向反过来,支持 `string == shared`。这是四个 impl 中唯一「`SharedString` 出现在 trait 参数、`String` 作 self 类型」的写法,靠孤儿规则的类型参数例外才合法。

[gpui_shared_string.rs:L98-L102](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L98-L102) — `impl PartialEq<str> for SharedString`:右操作数是 `str` 本体(DST 类型,方法签名里以 `other: &str` 出现),两边直接 `&str == &str` 比较。主要服务于泛型写法(如 `T: PartialEq<str>` 边界)与完备性。

[gpui_shared_string.rs:L104-L108](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L104-L108) — `impl<'a> PartialEq<&'a str> for SharedString`:日常最常用的一个,`other: &&'a str` 是指向 `str` 的引用的引用,所以实现体里先 `*other` 解一层再比。`shared == "Zed"` 这样的字面量比较走的就是它。

一个下游佐证:比较运算按「操作数具体类型」选择 impl,所以在 zed 里凡是 `SharedString` 字段与字面量直接 `==` 的代码,依赖的都是 L104–L108 这组实现——它和 derive 的同类型 `PartialEq`、以及上一讲的 `Borrow`,共同构成 `SharedString` 的「像 `str` 一样比较」体验。

#### 4.2.4 代码实践

**实践目标**:确认每个手写 impl 分别支撑哪种表达式,并亲眼看到一个「反例」编译失败。

**操作步骤**:

1. 在 4.1.4 的 `eq-lab` 项目里新增(标注为**示例代码**):

```rust
fn cross_type_eq() {
    let shared: SharedString = SharedString::new("Zed");
    let string: String = String::from("Zed");

    // L86-L90:shared == string
    assert!(shared == string);
    // L92-L96:string == shared
    assert!(string == shared);
    // L98-L102:shared == *str_ref(与 str 本体比较)
    let str_ref: &str = "Zed";
    assert!(shared == *str_ref);
    // L104-L108:shared == &str(最常用,字面量形态)
    assert!(shared == "Zed");
    // 对照组:derive 生成的同类型比较
    assert!(shared == SharedString::new("Zed"));
}
```

2. 再故意加一行让编译器告诉你缺口在哪:

```rust
// let _ = "Zed" == shared; // 取消注释后编译失败:
// cannot compare `&str` with `SharedString` — 没有 impl PartialEq<SharedString> for &str
```

3. `cargo run` 观察通过版,再取消注释那一行 `cargo check` 观察错误信息。

**需要观察的现象**:五个方向的 `assert!` 全部通过;取消注释后编译器报「不能比较 `&str` 与 `SharedString`」一类错误(E0369)。

**预期结果**:通过 / 编译失败各如预期。这说明跨类型比较是**逐方向手工供给**的,Rust 不做自动换向。

**待本地验证**:请在本地实际运行与编译确认。

#### 4.2.5 小练习与答案

**练习 1**:`hello.text == "Zed"`(`hello.text` 是 `SharedString` 字段)用的是哪个 impl?

<details>
<summary>参考答案</summary>

`impl<'a> PartialEq<&'a str> for SharedString`(L104–L108)。`"Zed"` 的类型是 `&'static str`,左操作数 `SharedString`,`==` 按这对类型查找 impl,命中它;实现体里 `*other` 解一层引用后按内容比较。
</details>

**练习 2**:为什么 `"a" == shared` 写反了就编译不过?

<details>
<summary>参考答案</summary>

`==` 要求存在 `impl PartialEq<Rhs> for Lhs`,其中 `Lhs = &str`、`Rhs = SharedString`。标准库没有、本 crate 也没写 `impl PartialEq<SharedString> for &str`(以及 `for str`),所以报 E0369。解决办法:交换顺序写 `shared == "a"`,或显式 `shared.as_str() == "a"`。本 crate 的选择是把常用方向(`SharedString` 在左)补齐,而不是追求全对称。
</details>

**练习 3**:`impl PartialEq<SharedString> for String` 中 trait 和 self 类型都是外部的,为什么不违反孤儿规则?

<details>
<summary>参考答案</summary>

孤儿规则(E0117)禁止「为外部类型实现外部 trait」,但有一个例外:trait 的泛型参数中出现了本地类型时允许。这里实现的 trait 实际是 `PartialEq<SharedString>`,类型参数 `SharedString` 是本 crate 类型,所以为 `String` 实现它是合法的。同理,如果在外部 crate 里为 `String` 实现 `PartialEq<SomeExternalType>` 则会被拒绝。
</details>

### 4.3 内容语义 vs 存储方式：与 Borrow 的闭环

#### 4.3.1 概念说明

本模块把前两个模块串成完整闭环,回答本讲的核心问题:**为什么比较只看内容、与构造路径无关,以及这为什么重要?**

回顾 u2-l1 的结论:`SharedString` 的同一文本可能存在于三种完全不同的物理位置——

1. `new_static("a")`:数据在程序只读段,`SmolStr` 只存一个指针;
2. `new(&short)`:短文本内联在 `SmolStr` 的 24 字节结构里;
3. `new(&long)`:长文本在堆上,由引用计数共享。

如果相等性依赖存储方式(比如比较指针),这三者构造的 `"a"` / 长文本就会「同文不同命」,`HashMap` 查键、去重、缓存命中全部失效。**本 crate 的设计选择是:相等、排序、哈希一律只由内容决定**——这是 4.1 委托链的自然结果,也是 4.2 跨类型比较能「化归到 str」的前提。

而 u2-l2 讲过,`Borrow<str>` 的契约要求 `SharedString` 的 `Eq` / `Ord` / `Hash` 与 `str` 的对应语义**完全一致**。当时只是引用契约,现在可以逐项验收:

- `Eq` 一致 ⇐ derive 委托 + `SmolStr` 按内容(4.1);
- `Ord` 一致 ⇐ derive 委托出字典序(4.1);
- `Hash` 一致 ⇐ derive 委托,哈希与 `str` 相同(4.1 实验第 3 组);
- 跨类型直接比较可用 ⇐ 四个手写 `PartialEq`(4.2)。

四项全部落位,`Borrow` 契约履约完毕。

#### 4.3.2 核心流程

`HashMap<K, V>::get` 的签名约束是 `K: Borrow<Q>, Q: Hash + Eq`。当 `K = SharedString`、用 `&str` 查询时(`Q = str`),一次 `get("rust")` 背后发生的事:

```
map.get("rust")
  1. Borrow<str>: 把键 SharedString 以 &str 视图交出       ← u2-l2 的 impl
  2. Hash 一致:  对查询 &str 计算哈希,能命中键的哈希桶      ← derive(Hash) 委托内容
  3. Eq 一致:    哈希桶内用 eq 比较查询 &str 与键           ← 内容语义,与存储无关
  ⇒ 三者合力,查键成功
```

写成条件式,即:

\[ \text{get 命中} \iff H(k_{\text{键}}) = H(q) \,\wedge\, k_{\text{键}} = q \quad \text{且判定与二者各自的构造/存储方式无关} \]

`BTreeMap` 同理走 `Ord`:derive 的字典序与 `str` 字典序一致,所以 `BTreeMap<SharedString, V>` 也能用 `&str` 查。

#### 4.3.3 源码精读

契约的「锚点」是这两个 impl:

[gpui_shared_string.rs:L68-L72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L68-L72) — `impl Borrow<str> for SharedString`:把内部 `SmolStr` 以 `&str` 视图交出。u2-l2 已精读过它的机制;本讲的关键是它**必须**与第 14 行的 derive 配合——`Borrow` 文档要求 `Eq` / `Ord` / `Hash` 跨 `Borrow` 一致,而 derive 的内容语义恰好保证了这一点。

[gpui_shared_string.rs:L62-L66](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L62-L66) — `impl AsRef<str> for SharedString`:4.2 的四个手写 `PartialEq` 全部经由 `self.as_ref()` 把 `self` 化归为 `&str`,复用的正是它。

下游最完整的闭环用例在语义高亮模块:

[semantic_tokens.rs:L607-L612](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L607-L612) — `has_modifier(&self, token_modifiers: u32, modifier: &str)`:在 `HashMap<SharedString, u32>` 的 `modifier_mask` 上直接 `self.modifier_mask.get(modifier)`。查询参数是 `&str`,键是 `SharedString`——这条语句能编译且行为正确,同时依赖 `Borrow<str>`(让 `Q = str` 合法)、`Hash` 一致(命中同一个桶)与 `Eq` 一致(桶内判等成立)。键的构造在 [semantic_tokens.rs:L558-L563](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L558-L563),来自 `SharedString::from(modifier.as_str().to_string())`(运行时构造);而查询端传入的 `&str` 来自另一条构造路径——两条路径在 `get` 里相遇仍能命中,这就是内容语义的实战价值。

#### 4.3.4 代码实践(本讲主实践)

**实践目标**:一次性验证「同一文本、不同构造路径彼此相等」与「`SharedString` 与 `&str` 哈希一致」,并在注释里说清与 `Borrow` 的关系。

**操作步骤**:

1. 继续在 `eq-lab` 项目里编写(标注为**示例代码**;注意 Rust 不支持 `a == b == c` 链式比较,`==` 是左结合的且返回 `bool`,所以拆成两两断言):

```rust
use gpui_shared_string::SharedString;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

#[test]
fn content_semantics() {
    // 三种构造路径 + 与裸类型比较。
    // 注意:Rust 没有 a == b == c 语法,必须两两断言。
    let static_path = SharedString::new_static("a");        // 静态数据,零分配
    let runtime_path = SharedString::new(&"a".to_string()); // 运行时构造
    let from_str_path: SharedString = "a".into();           // From<&str>
    let string_form = String::from("a");

    // derive 的同类型相等:与构造路径无关(4.1 的委托链,按内容判定)
    assert!(static_path == runtime_path);
    assert!(runtime_path == from_str_path);

    // 手写跨类型比较:SharedString 与 &str / String(4.2 的四个 impl)
    assert!(static_path == "a");
    assert!(static_path == string_form);
    assert!(string_form == static_path);
}

#[test]
fn hash_agrees_with_str() {
    let shared = SharedString::new("rust");
    let bare: &str = "rust";

    // 分别用 DefaultHasher 计算两个值的哈希。
    let mut h1 = DefaultHasher::new();
    shared.hash(&mut h1);            // derive(Hash) → SmolStr::hash → 内容
    let mut h2 = DefaultHasher::new();
    bare.hash(&mut h2);              // str::hash → 内容

    // 断言一致。
    //
    // 与 Borrow 的关系:Borrow<str> 只负责「让 HashMap<SharedString, _>
    // 接受 &str 查询」这个类型层面的通道;而 get 真正能命中,还要求
    // ① 两边哈希一致(本断言验证)② 两边 eq 语义一致(上一个测试验证)。
    // Borrow 的契约正是要求 Eq/Ord/Hash 跨 Borrow 一致——本 crate 用
    // 「按内容」的 derive 实现履约,所以下面这个跨类型 get 才能成功:
    assert_eq!(h1.finish(), h2.finish());

    use std::collections::HashMap;
    let mut map = HashMap::new();
    map.insert(SharedString::new_static("rust"), 1);
    assert_eq!(map.get("rust"), Some(&1)); // Borrow<str> + Hash 一致 + Eq 一致
}
```

2. 运行 `cargo test`。

**需要观察的现象**:两个测试全部通过;`hash_agrees_with_str` 中 `map.get("rust")` 用 `&str` 命中了以 `new_static` 构造的键。

**预期结果**:通过。若把 `map.insert` 的键换成 `SharedString::new(&"rust".to_string())`(换构造路径),`get("rust")` 依然命中——这就是「内容语义 vs 存储方式」的直接证据。

**待本地验证**:请在本地运行 `cargo test` 确认(本讲义未代跑)。

#### 4.3.5 小练习与答案

**练习 1**:`HashMap<SharedString, V>` 能用 `&str` 查键,需要哪几个条件同时成立?

<details>
<summary>参考答案</summary>

三个:① `impl Borrow<str> for SharedString`,让 `get` 的 `Q = str` 满足 `K: Borrow<Q>`(类型通道);② `Hash` 一致——`SharedString` 与 `&str` 的同一文本哈希相同,才能落进同一个桶;③ `Eq` 一致——桶内用 `eq` 判等时内容相同即命中。②③ 由「按内容」的 derive 委托链保证,①是 u2-l2 的手写 impl;三者缺一不可,合起来即 `Borrow` 契约的完整履约。
</details>

**练习 2**:`new_static("a") == new(&"a".to_string())` 为什么为真?

<details>
<summary>参考答案</summary>

`==` 走 derive 的 `PartialEq`,它逐字段委托给 `SmolStr` 的 `PartialEq`,后者按字符串内容(字节)比较。前者数据在程序只读段、后者内联在 `SmolStr` 结构里,存储位置完全不同,但存储方式不参与比较,内容同为 `"a"` 即相等。同理两者的哈希、字典序也完全一致。
</details>

**练习 3**:如果把 4.3.4 里 `map.insert` 的键换成 `format!("{}{}",'r',"ust").into()` 生成的 `SharedString`,`map.get("rust")` 还能命中吗?为什么?

<details>
<summary>参考答案</summary>

能。`format!` 产物经 `From<String>` 转入,只是又一条构造路径(运行时构造,内容 `"rust"`);哈希与相等都只由内容决定,与键从哪条路径来无关。这正是 `SharedString` 适合做缓存/注册表键的原因:生产端(可能来自配置、网络、格式化)与查询端(往往是 `&str` 字面量)不需要共享任何构造细节。
</details>

## 5. 综合实践

设计一个「词频索引器」,把本讲三个模块全部串起来:

**任务**:给定一段文本,统计单词出现次数,并按字典序输出。

1. 建 `HashMap<SharedString, u32>` 做词频表;把文本 `split_whitespace` 后逐词 `insert`(**键用 `SharedString`**:插入时统一 `.into()`,体会 u2-l3 的 `From<&str>` 与本讲容器约束 `Eq + Hash` 的衔接)。
2. 特意让同一个单词 `"gpui"` 通过两条路径各出现一次——一次来自 `"gpui".into()`,一次来自 `format!("{}","gpui").to_string().into()`——验证计数是 2 而不是 1、1(内容语义 vs 存储方式)。
3. 用 `map.get("gpui")`(裸 `&str`)查询计数(`Borrow` + `Hash` 一致 + `Eq` 一致的闭环)。
4. 把整个表搬进 `BTreeMap<SharedString, u32>` 再遍历输出,确认按键的**字典序**排列(derive `Ord` 委托出的内容排序)。
5. 收尾:自己写一个 `struct Word(SharedString)`,为它手写 `impl PartialEq<&str> for Word`(模仿 4.2 的 L104–L108 写法,化归到 `str` 维度),并加一条 `assert!(Word("gpui".into()) == "gpui")`。

**验收标准**:程序输出按字典序排列的词频;`"gpui"` 计数为 2;最后一条断言通过。整个任务不需要运行 Zed 本体,独立 cargo 项目即可完成(依赖方式同 4.1.4)。

## 6. 本讲小结

- **derive 委托链**:`derive(Eq, PartialEq, PartialOrd, Ord, Hash)` 对单字段 newtype 等价于逐字段委托给 `SmolStr`,而 `SmolStr` 按字符串内容实现这些 trait,于是 `SharedString` 的相等、排序、哈希全部由**字节内容**决定。
- **容器约束**:`HashMap` / `HashSet` 键需要 `Eq + Hash`,`BTreeMap` 键需要 `Ord`;zed 中主题注册表、语义 token 修饰符表、agent 工具表都在直接消费这些 derive。
- **四个手写跨类型 `PartialEq`**(`String`↔`SharedString`、`str`、`&str`)把常用比较方向补齐,套路统一:经 `as_ref()` 把 `SharedString` 化归为 `&str` 再比;方向不对称,`"a" == shared` 不可用,需写成 `shared == "a"`。
- **孤儿规则例外**:`impl PartialEq<SharedString> for String` 合法,因为 trait 的类型参数含本地类型。
- **内容语义 vs 存储方式**:`new_static` / 内联 / 堆三种构造路径的同一文本相等、同哈希、同序——这是 `get("rust")` 能命中任意路径构造的键的根本原因。
- **`Borrow` 契约闭环**:u2-l2 立下的「`Eq`/`Ord`/`Hash` 跨 `Borrow` 一致」要求,由本讲的按内容 derive 全部兑现;`semantic_tokens.rs` 的 `has_modifier` 是完整闭环的现成范例。

## 7. 下一步学习建议

至此进阶单元(u2)完结,你已经精读了 `gpui_shared_string.rs` 中除序列化外的全部源码。下一讲 **u3-l1《serde 与 schemars:让字符串类型透明序列化》**将补上最后一块源码:第 194–211 行的 `Serialize` / `Deserialize` 手工实现(在 JSON 中表现为普通字符串)与第 42–54 行委托 `String` 的 `JsonSchema`——你会再次看到「化归到 `str`/`String` 维度」的同款套路。建议在进入下一讲前,先完成本讲第 5 节的综合实践,确保跨类型 `PartialEq` 的写法已经亲手写过一遍;之后可以顺带浏览 [semantic_tokens.rs:L607-L612](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L607-L612) 与 [registry.rs:L60-L61](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L60-L61),体会 `SharedString` 作为容器键在真实代码中的密度。
