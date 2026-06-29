# 多文件合并与键扁平化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `merge_value` 是怎样**按 key 深度合并**两个翻译对象的：遇到「对象 + 对象」就递归下钻，遇到叶子值就让后者**整体覆盖**前者（last-write-wins）。
- 说清楚 `flatten_keys` 是怎样把一棵嵌套的 `serde_json::Value` 树**拍平**成 `BTreeMap<String, String>`（即 `a.b.c` 点号键）的，并知道 `Null` / `Array` 会变成空字符串、`Number` / `Bool` 会被字符串化。
- 说清楚 `format_keys` 是怎样用 `.` 把多级键前缀拼起来的，以及它和 `flatten_keys` 在「拼接策略」上的细微差别。
- 能画出「**多个文件先逐个 `merge` 进同一个 locale，最后再统一 `flatten`**」这条整体数据流转，并解释**为什么必须先合并、后扁平化**。

本讲是「编译期代码生成主链路」的第三环：上一讲（u2-l2）讲的是单个翻译文件如何被 `parse_file` 读成 `Translations`（`BTreeMap<Locale, Value>`）；本讲讲的是这些文件读进来之后，**同一 locale 的多份内容如何合并、整棵嵌套树如何拍平成点号键**；下一讲（u2-l4）会讲拍平后的 `BTreeMap<String, BTreeMap<String, String>>` 如何被 `generate_code` 生成成运行时代码。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 为什么需要「合并」

一个真实项目里，同一种语言（比如 `en`）的翻译很少只放在一个文件里。常见的拆法是：基础文案放 `en.yml`、某个模块（如 `view`）的文案单独放 `view.en.yml`、某个第三方库带进来的文案放 `vendor.en.yml`。rust-i18n 在编译期要把它们**拼成一份完整的 `en` 翻译**，这就是 `merge_value` 的工作。

### 2.2 为什么需要「扁平化」

翻译文件为了可读性，通常写成嵌套的：

```yaml
messages:
  count: "%{count} messages"
  title: Welcome
```

但运行时 `t!("messages.count")` 查的是一个**扁平的字符串 key**。所以编译期需要把上面的嵌套结构变成：

```text
"messages.count" -> "%{count} messages"
"messages.title" -> "Welcome"
```

这就是 `flatten_keys` 的工作——把「树」压成「一张扁平表」。

### 2.3 两个中间数据结构

本讲会反复出现这两个类型（见 [crates/support/src/lib.rs:31-36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L31-L36)，它们用 `type` 起了别名）：

| 别名 | 真实类型 | 含义 |
| --- | --- | --- |
| `Translations` | `BTreeMap<Locale, Value>` | 合并阶段用：locale → 一棵嵌套 JSON 树 |
| 最终 `result` | `BTreeMap<String, BTreeMap<String, String>>` | 扁平化后：locale → {点号键 → 译文} |

其中 `Value = serde_json::Value`、`Locale = String`。`BTreeMap` 而非 `HashMap` 的选择会让最终结果**按键名字典序排列**，这点在后面的代码实践里会观察到。

### 2.4 为什么要先合并、后扁平化

这是本讲最关键的一条设计原则。想象两个文件都有 `messages` 这个对象：

- 文件 A 的 `messages`: `{count: "..."}`
- 文件 B 的 `messages`: `{title: "Welcome"}`

如果**先扁平化再合并**，A 会产出 `{"messages.count": "..."}`、B 会产出 `{"messages.title": "Welcome"}`，二者没有同名 key，看起来也能拼。但一旦 A 和 B 都有 `messages.title`，扁平后谁覆盖谁就不直观了。而 `merge_value` 在**嵌套树**层面工作，能清晰地表达「对象就下钻、叶子就覆盖」的语义，行为可预测。所以 rust-i18n 的顺序是：

\[
\underbrace{\text{每个文件解析成 } Translations}_{\text{u2-l2}} \;\xrightarrow{\text{merge\_value}}\; \underbrace{\text{每 locale 一棵完整嵌套树}}_{\text{本讲 4.1}} \;\xrightarrow{\text{flatten\_keys}}\; \underbrace{\text{每 locale 一张扁平表}}_{\text{交给 u2-l4}}
\]

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它承担了「合并 + 扁平化」的全部逻辑：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | `rust-i18n-support` 的运行时类型与编译期加载逻辑。本讲关注其中带 `#[cfg(feature = "codegen")]` 守卫的 `merge_value`、`flatten_keys`、`format_keys` 三个函数，以及 `try_load_locales` 里调用它们的两个编排点。 |

相关位置速查（均为本文件内行号）：

- 编排点一（逐文件合并）：`try_load_locales` 中的 `merge_value` 调用，约 [L150-L155](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L150-L155)。
- 编排点二（逐 locale 扁平化）：`try_load_locales` 中的 `flatten_keys` 调用，约 [L158-L160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L158-L160)。
- `merge_value`：[L38-L50](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L38-L50)。
- `format_keys`：[L247-L254](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L247-L254)。
- `flatten_keys`：[L256-L290](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L256-L290)。

> 说明：这些函数都被 `#[cfg(feature = "codegen")]` 包住，意味着**只在编译期把翻译文件编进二进制时才存在**；如果你只把 support crate 当纯运行时类型库用（不开 codegen），这些函数不会进二进制。

---

## 4. 核心概念与源码讲解

### 4.1 merge_value：按 key 深度合并两个翻译对象

#### 4.1.1 概念说明

`merge_value` 解决的问题是：把「同一 locale 的第二份内容」合并进「第一份内容」里。它的合并规则非常简洁，只有两条：

1. **如果两边都是「对象（Map）」**：对 b 的每个 key 递归合并——a 里没有这个 key 就先建一个 `Null` 占位，再下钻。
2. **其它任何情况（叶子 vs 叶子、叶子 vs 对象、对象 vs 叶子）**：直接用 b 的值**整体覆盖** a 的值（`*a = b.clone()`）。

换句话说：**对象往下钻，叶子整体换**。这种「深合并对象、浅覆盖叶子」的语义，正好契合翻译文件「同层 key 要合并、但同名文案以后写者为准」的直觉。

#### 4.1.2 核心流程

用伪代码描述 `merge_value(a, b)`（a 可变，b 只读，结果写回 a）：

```text
match (a, b):
  (Object(mapA), Object(mapB)):
      for (k, v) in mapB:               # 只遍历 b 的 key
          slot = mapA.get_or_insert(k, Null)   # a 没有就补 Null
          merge_value(slot, v)          # 递归下钻
  其它:
      *a = b 的副本                      # 叶子/类型不一致：整体覆盖
```

几个要点：

- **只新增 b 有而 a 没有的 key**，a 独有的 key 原样保留。
- **同名叶子值**：b 覆盖 a。注意遍历顺序由 glob 扫描文件决定，所以「同名文案谁覆盖谁」取决于文件被读进来的顺序（通常不稳定，实践中应避免不同文件给出同名键的不同译文）。
- **类型冲突**（如 a 里是字符串、b 里是对象）：走「其它」分支，b 整体覆盖，a 的旧结构被丢弃。

#### 4.1.3 源码精读

函数本体只有 11 行，见 [crates/support/src/lib.rs:38-50](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L38-L50)。这段代码做了上面伪代码描述的两件事：

```rust
fn merge_value(a: &mut Value, b: &Value) {
    match (a, b) {
        (Value::Object(a), Value::Object(b)) => {
            for (k, v) in b {
                merge_value(a.entry(k.clone()).or_insert(Value::Null), v);
            }
        }
        (a, b) => {
            *a = b.clone();
        }
    }
}
```

关键点逐行说明：

- `(Value::Object(a), Value::Object(b))`：只有**双方都是对象**才走深合并分支。这里的 `a`、`b` 是绑定到 `serde_json::Map` 的可变/只读引用。
- `a.entry(k.clone()).or_insert(Value::Null)`：在 a 里取 key `k` 的 entry；若不存在就插入一个 `Null` 占位，并返回它的可变引用——这就是「a 没有就补 Null」的实现。注意它用 `Value::Null` 占位而非空对象，是因为真正的结构由 b 里的 `v` 在递归时填进去。
- `merge_value(..., v)`：拿到占位槽后递归。下一层若 `v` 是对象，又会把 `Null` 替换成对象；若 `v` 是叶子，走「其它」分支 `*a = b.clone()` 把 `Null` 覆盖成叶子。
- `(a, b) => *a = b.clone()`：兜底分支——所有非「对象+对象」的组合都整体覆盖。

再看它被谁调用。在 `try_load_locales` 主循环里，每个文件解析出的 `Translations`（可能含多个 locale）会被逐 locale 合并进全局 `translations`，见 [crates/support/src/lib.rs:150-155](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L150-L155)：

```rust
trs.into_iter().for_each(|(k, new_value)| {
    translations
        .entry(k)
        .and_modify(|old_value| merge_value(old_value, &new_value))
        .or_insert(new_value);
});
```

这段做了三件事：取出该文件解析出的每个 `(locale, value)`；若 `translations` 里已有该 locale，就用 `merge_value` 把新值合并进去；否则直接插入。这就是「多个文件 → 同一 locale 嵌套树」的汇聚点。

> 同样的 `merge_value` 还被 `parse_file_v2` 内部复用，用来在**单个 v2 文件内**把不同语言子树合并起来，见 [crates/support/src/lib.rs:210-212](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L210-L212)。一份合并逻辑在两处复用，是本仓库减少重复代码的典型做法。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `merge_value` 的「对象下钻、叶子覆盖」语义，并对照仓库自带的单元测试。

**操作步骤**：

1. 打开 [crates/support/src/lib.rs:296-314](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L296-L314)，阅读 `test_merge_value`。它给了最权威的行为定义。
2. 该测试的内容是：

   ```rust
   // a: {"foo":"Foo", "dar":{"a":"1","b":"2"}}
   // b: {"foo":"Foo1", "bar":"Bar", "dar":{"b":"21"}}
   // 合并后断言：
   assert_eq!(c["foo"], "Foo1");      // 叶子被覆盖
   assert_eq!(c["bar"], "Bar");       // 新增 key
   assert_eq!(c["dar"]["a"], "1");    // 对象下钻后保留 a 独有
   assert_eq!(c["dar"]["b"], "21");   // 对象下钻后叶子被覆盖
   ```

3. 运行这条测试，确认通过：

   ```bash
   cd crates/support
   cargo test --features codegen test_merge_value
   ```

**需要观察的现象**：`foo` 和 `dar.b` 是叶子，被 b 覆盖；`bar` 是 b 新增的；`dar.a` 是 a 独有、被保留。整棵 `dar` 对象没有整体替换，而是逐 key 合并——这正是「对象下钻」的体现。

**预期结果**：测试通过，`c` 等于 `{"foo":"Foo1","bar":"Bar","dar":{"a":"1","b":"21"}}`。

> 如果你的环境无法编译 support crate（缺 codegen 依赖等），可标注「待本地验证」并改为纯阅读：把上面 4 行断言当作「行为契约」记下来即可。

#### 4.1.5 小练习与答案

**练习 1**：`merge_value` 用 `Value::Null` 作为占位插入 a，而不是 `Value::Object(空 map)`。如果改成插入空对象，会对结果有影响吗？

**答案**：不会有影响。因为紧接着的 `merge_value(slot, v)` 递归：若 `v` 是对象，递归会在空对象里逐 key 填充（结果一致）；若 `v` 是叶子，`*a = b.clone()` 会把空对象整个覆盖掉（结果也一致）。用 `Null` 只是表达「这里暂时什么都不是」的语义，更省一次 map 分配。

**练习 2**：若 a 里 `foo` 是字符串 `"Foo"`、b 里 `foo` 是对象 `{"x":1}`，合并后 `foo` 会是什么？

**答案**：走「其它」分支，`*a = b.clone()`，`foo` 变成对象 `{"x":1}`，原来的字符串 `"Foo"` 被丢弃。类型冲突一律以后者整体覆盖收场。

---

### 4.2 flatten_keys：把嵌套树拍平成点号键

#### 4.2.1 概念说明

`flatten_keys` 把一棵嵌套的 `serde_json::Value` 转成一张扁平的 `BTreeMap<String, String>`。它的工作方式是**深度优先递归**：每下钻一层，就把当前层的 key 用 `.` 拼到前缀后面；遇到叶子（字符串/数字/布尔）就把「完整前缀」作为 key、叶子的字符串形式作为 value 写入结果。

它还顺带处理了非字符串叶子：

- `String` → 原样。
- `Number` / `Bool` → 用 `format!("{}", x)` 转成字符串。
- `Null` → 空字符串 `""`。
- `Array` → **空字符串 `""`**（数组不被支持，被压成一个空值占位）。

#### 4.2.2 核心流程

伪代码描述 `flatten_keys(prefix, trs) -> BTreeMap<String, String>`：

```text
result = {}
match trs:
  String(s):    result[prefix] = s
  Object(o):    for (k, vv) in o:
                   key = prefix 为空 ? k : prefix + "." + k
                   result.extend( flatten_keys(key, vv) )   # 递归
  Null:         result[prefix] = ""
  Bool(b):      result[prefix] = format!("{}", b)
  Number(n):    result[prefix] = format!("{}", n)
  Array(_):     result[prefix] = ""
return result
```

要点：

- **前缀拼接规则**：`prefix` 为空时直接用当前 key，否则用 `"."` 连接——所以最外层不会多出一个前导点。
- **Object 才递归**：只有对象会继续下钻并把子结果 `extend` 进来；叶子直接落盘。
- **空键保护**：理论上 `prefix` 为空且当前是叶子时，`result[""]` 会出现一个空字符串 key——这对应「文件顶层直接是一个标量」的退化情况，实践中翻译文件顶层总是对象，不会触发。

#### 4.2.3 源码精读

函数见 [crates/support/src/lib.rs:256-290](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L256-L290)，核心结构如下：

```rust
fn flatten_keys(prefix: &str, trs: &Value) -> BTreeMap<String, String> {
    let mut v = BTreeMap::<String, String>::new();
    let prefix = prefix.to_string();

    match &trs {
        serde_json::Value::String(s) => { v.insert(prefix, s.to_string()); }
        serde_json::Value::Object(o) => {
            for (k, vv) in o {
                let key = if prefix.is_empty() {
                    k.clone()
                } else {
                    format!("{}.{}", prefix, k)
                };
                v.extend(flatten_keys(key.as_str(), vv));
            }
        }
        serde_json::Value::Null        => { v.insert(prefix, "".into()); }
        serde_json::Value::Bool(s)     => { v.insert(prefix, format!("{}", s)); }
        serde_json::Value::Number(s)   => { v.insert(prefix, format!("{}", s)); }
        serde_json::Value::Array(_)    => { v.insert(prefix, "".into()); }
    }
    v
}
```

逐段说明：

- 返回类型是 `BTreeMap<String, String>`：**键和值都是字符串**。这意味着不管原文是数字还是布尔，进表后都变成字符串。
- `Object` 分支里的 `key` 构造，是「点号键」诞生的地方：`prefix.is_empty()` 判空避免前导点，`format!("{}.{}", prefix, k)` 完成拼接。
- `v.extend(flatten_keys(...))`：把子树的扁平结果整张并入当前结果——递归的「汇总」动作。
- `Null` 和 `Array` 都映射成空字符串 `""`，是对「无法表达成译文」的兜底处理。

调用点在 `try_load_locales` 的末尾，见 [crates/support/src/lib.rs:158-160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L158-L160)：

```rust
translations.iter().for_each(|(locale, trs)| {
    result.insert(locale.to_string(), flatten_keys("", trs));
});
```

注意三个细节：第一，它对**每个 locale 各调用一次**，初始 `prefix` 都是空字符串 `""`；第二，此时 `translations` 已经是合并完的完整嵌套树，所以拍平的是「最终态」；第三，产出的 `result` 就是 `try_load_locales` 的返回值，会被 u2-l4 的 `generate_code` 拿去生成运行时代码。

#### 4.2.4 代码实践

**实践目标**：手工跟踪一棵嵌套树经过 `flatten_keys` 后的扁平结果，确认点号键拼接与叶子字符串化行为。

**操作步骤**：

1. 设想 en locale 合并后的嵌套树为：

   ```json
   {
     "greeting": "Hello",
     "messages": { "count": "3 messages", "title": "Welcome" },
     "count": 42,
     "tags": ["a", "b"]
   }
   ```

2. 从 `prefix = ""` 开始，逐层下钻，按下表推演：

   | 步骤 | 当前 prefix / key | 叶子类型 | 产出的扁平条目 |
   | --- | --- | --- | --- |
   | 1 | `greeting` | String | `"greeting" -> "Hello"` |
   | 2 | `messages` | Object → 下钻 | （无叶子，递归） |
   | 2.1 | `messages.count` | String | `"messages.count" -> "3 messages"` |
   | 2.2 | `messages.title` | String | `"messages.title" -> "Welcome"` |
   | 3 | `count` | Number | `"count" -> "42"`（字符串化） |
   | 4 | `tags` | Array | `"tags" -> ""`（数组压成空串） |

3. 因为结果是 `BTreeMap`（按键名字典序），最终顺序是：

   ```text
   {
     "count": "42",
     "greeting": "Hello",
     "messages.count": "3 messages",
     "messages.title": "Welcome",
     "tags": ""
   }
   ```

**需要观察的现象**：数字 `42` 变成了字符串 `"42"`；数组 `["a","b"]` 变成了空串 `""`；嵌套对象被拆成 `messages.count` / `messages.title` 两个点号键；最终结果按 key 字典序排列。

**预期结果**：得到上表那张 5 条目的扁平 map。**待本地验证**：仓库没有为 `flatten_keys` 写专门的单元测试（只有 `test_merge_value` 和 `test_parse_file_*`），若想跑通可参考 4.3.4 的「示例代码」复现该算法做断言。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `flatten_keys` 对 `Array` 返回空字符串而不是报错？

**答案**：因为 `t!` 的查找表是 `String -> String`，没有结构可以承载数组。把数组压成空串是一种「安静兜底」——不 panic、不阻断编译，但运行时 `t!("tags")` 只会拿到空串。实践中翻译文件不应放数组。

**练习 2**：若嵌套树里某层 key 自身含点号（如 YAML 里 `"a.b": "x"`），拍平后会怎样？

**答案**：会与多层嵌套产生的点号混淆——`flatten_keys` 不区分「key 名里的点」和「层级间的点」，二者都变成 `.`。这是点号键方案的固有局限，实践中应避免在单个 key 名里使用点号。

---

### 4.3 format_keys：v2 多级键前缀拼接

#### 4.3.1 概念说明

`format_keys` 是一个很小的工具函数：把若干段字符串用 `.` 拼成一个完整键，**并自动跳过空串**。它和 `flatten_keys` 里的 `format!("{}.{}", prefix, k)` 看似都在「拼点号」，但用途不同：

- `flatten_keys` 的拼接发生在**拍平嵌套树**时，前缀由递归层层累积。
- `format_keys` 的拼接发生在 **v2 文件解析**（`parse_file_v2`）时，用来把「上层 key 前缀 + 当前 key」拼成点号键，而且要处理「前缀可能是空串」（顶层）的情况。

之所以单独抽一个函数，是因为 v2 解析时要**显式地、一次性地**拼前缀，而不是靠递归自然累积。

#### 4.3.2 核心流程

伪代码：

```text
format_keys(keys: &[&str]) -> String:
    收集 keys 中所有「非空」的段
    用 "." 连接它们
    返回结果
```

要点：

- **跳过空串**：这样调用 `format_keys(&["", "messages"])`（顶层，前缀为空）和 `format_keys(&["a", "messages"])` 都能得到正确结果——前者是 `"messages"`，后者是 `"a.messages"`。
- 因此它**不会产生前导或尾随的点号**。

#### 4.3.3 源码精读

函数见 [crates/support/src/lib.rs:247-254](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L247-L254)：

```rust
fn format_keys(keys: &[&str]) -> String {
    keys.iter()
        .filter(|k| !k.is_empty())
        .map(|k| k.to_string())
        .collect::<Vec<String>>()
        .join(".")
}
```

逐行说明：

- `.filter(|k| !k.is_empty())`：过滤掉空串段，这是「跳过空前缀」的关键。
- `.map(|k| k.to_string())`：把 `&&str` 转成 `String`，因为后面 `join` 需要拥有所有权的字符串切片。
- `.collect::<Vec<String>>().join(".")`：先收集成 `Vec` 再用 `.` 连接。

它的唯一调用者是 `parse_file_v2`，见 [crates/support/src/lib.rs:198-236](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L198-L236)。在 v2 文件里，每遇到一个语言子键，就用 `format_keys(&[key_prefix, key])` 拼出该条目的完整点号键；遇到嵌套对象则带着新前缀 `parse_file_v2(&key, value)` 递归，并用 `merge_value` 把不同语言的结果合并回 `trs`。典型调用见 [L206](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L206) 与 [L217](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L217)。

> 对比 `flatten_keys` 的拼接：`flatten_keys` 用 `if prefix.is_empty()` 判空后二选一，本质等价于 `format_keys(&[prefix, k])`。两者实现不同但语义一致——都是「跳过空段、用点连接」。

#### 4.3.4 代码实践

**实践目标**：用一份自包含的「示例代码」复现 `merge_value + flatten_keys` 算法，跑通后验证本讲 4.1.4、4.2.4 的手工推演。

> 说明：`merge_value`、`flatten_keys`、`format_keys` 在仓库里都是**私有函数**（没有 `pub`），外部无法直接调用。下面的示例代码是把仓库算法原样搬到一份独立测试里，仅供验证手工推演，**不是项目原有代码**。

**操作步骤**：

1. 新建一个临时 crate 或在任意可跑 `cargo test` 的项目里，加入依赖 `serde_json = "1"`。

2. 把下面这段「示例代码」放进一个测试文件（例如 `tests/flatten_demo.rs`），它复刻了 `merge_value`、`flatten_keys`、`format_keys` 三个函数：

   ```rust
   // 示例代码：复刻 crates/support/src/lib.rs 中的算法，用于验证手工推演。
   use serde_json::{json, Value};
   use std::collections::BTreeMap;

   fn merge_value(a: &mut Value, b: &Value) {
       match (a, b) {
           (Value::Object(a), Value::Object(b)) => {
               for (k, v) in b {
                   merge_value(a.entry(k.clone()).or_insert(Value::Null), v);
               }
           }
           (a, b) => *a = b.clone(),
       }
   }

   fn flatten_keys(prefix: &str, trs: &Value) -> BTreeMap<String, String> {
       let mut v = BTreeMap::<String, String>::new();
       let prefix = prefix.to_string();
       match &trs {
           Value::String(s) => { v.insert(prefix, s.to_string()); }
           Value::Object(o) => {
               for (k, vv) in o {
                   let key = if prefix.is_empty() { k.clone() }
                             else { format!("{}.{}", prefix, k) };
                   v.extend(flatten_keys(&key, vv));
               }
           }
           Value::Null => { v.insert(prefix, "".into()); }
           Value::Bool(b) => { v.insert(prefix, format!("{}", b)); }
           Value::Number(n) => { v.insert(prefix, format!("{}", n)); }
           Value::Array(_) => { v.insert(prefix, "".into()); }
       }
       v
   }

   #[test]
   fn merge_then_flatten_demo() {
       // 模拟两个 en 文件解析后的值
       let mut a = json!({
           "greeting": "Hello",
           "messages": { "count": "3 messages" }
       });
       let b = json!({
           "messages": { "title": "Welcome" },
           "farewell": "Bye"
       });

       merge_value(&mut a, &b);              // 先合并
       let flat = flatten_keys("", &a);      // 后扁平化

       // 断言：messages 两边的子键被合并，点号键正确拼接，BTreeMap 自动排序
       let keys: Vec<_> = flat.keys().collect();
       assert_eq!(keys, vec!["farewell", "greeting", "messages.count", "messages.title"]);
       assert_eq!(flat["messages.count"], "3 messages");
       assert_eq!(flat["messages.title"], "Welcome");
       assert_eq!(flat["farewell"], "Bye");
   }
   ```

3. 运行 `cargo test merge_then_flatten_demo`。

**需要观察的现象**：

- 两个文件的 `messages` 子树被**深度合并**（`count` 来自 A、`title` 来自 B），而非整体替换。
- 拍平后 `messages.count` / `messages.title` 两个点号键同时存在。
- 因为用的是 `BTreeMap`，`keys()` 返回的顺序是字典序：`farewell` < `greeting` < `messages.count` < `messages.title`。

**预期结果**：测试通过。这同时验证了「先 merge 后 flatten」能正确保留两边嵌套子树的键——如果改成「先各自 flatten 再合并」，你需要自己处理 `messages.count` 这类点号键的合并，逻辑会复杂得多。

#### 4.3.5 小练习与答案

**练习 1**：`format_keys(&["", "title"])` 和 `format_keys(&["view", "title"])` 分别返回什么？为什么需要 `filter` 掉空串？

**答案**：分别返回 `"title"` 和 `"view.title"`。需要 `filter` 掉空串，是因为 v2 解析在顶层调用时 `key_prefix` 为空串 `""`，若不过滤会拼出 `".title"` 这样的前导点号，导致 key 错误。

**练习 2**：能否用 `format_keys(&[prefix, k])` 替换 `flatten_keys` 里的 `if prefix.is_empty() { ... } else { ... }`？替换后行为一致吗？

**答案**：可以，行为一致。两者都是「跳过空段、用点连接」。仓库里 `flatten_keys` 选择手写 `if/else` 大概是出于少一次 `Vec` 分配的考虑（递归调用频繁），但语义上与 `format_keys` 完全等价。

---

## 5. 综合实践

把本讲三个函数串起来，完成一次「从两份原始翻译文件到最终扁平表」的全程推演。

**任务**：假设 `locales/` 下有两个 v1 文件（关于 v1/v2 见 u1-l4，关于 `file_stem` 如何推出 locale 见 u2-l2）：

`locales/en.yml`：

```yaml
app:
  name: My App
  version: 1.2
user:
  greet: Hello %{name}
```

`locales/view.en.yml`：

```yaml
user:
  greet: Hi %{name}
  logout: Sign out
nav:
  home: Home
```

请按下列步骤完成：

1. **确定 locale**：两个文件的 `file_stem` 分别是 `en` 和 `view.en`，经 `split('.').last()` 都得到 `en`。所以二者都汇入 `en` 这个 locale。
2. **写出各自 parse 后的 Value**（`parse_file_v1` 整文件挂到 locale 下）。
3. **手工执行 `merge_value`**：注意 `user.greet` 是同名叶子，后者（`view.en.yml` 的 `"Hi %{name}"`）覆盖前者；`user.logout`、`nav.home` 是新增；`app.*` 保留。
4. **手工执行 `flatten_keys`**：把合并后的嵌套树拍平成点号键，并注意 `version: 1.2` 这种数字会被字符串化（YAML 里 `1.2` 解析成浮点数 → `"1.2"`）。
5. **写出最终 `en` 的扁平 `BTreeMap`**（按字典序）。

**参考答案**（合并后 `en` 的扁平表）：

```text
{
  "app.name": "My App",
  "app.version": "1.2",
  "nav.home": "Home",
  "user.greet": "Hi %{name}",
  "user.logout": "Sign out"
}
```

关键观察：

- `user.greet` 被 `view.en.yml` 覆盖（last-write-wins，取决于 glob 扫描顺序，实践中不要依赖此覆盖关系，应保证同名键译文一致）。
- `app.version` 的 `1.2` 被字符串化为 `"1.2"`。
- 所有键按字典序排列（`app.*` < `nav.*` < `user.*`）。

**进阶**：用 4.3.4 的「示例代码」把上面两个 Value 喂给 `merge_value + flatten_keys`，断言结果与参考答案一致。

---

## 6. 本讲小结

- rust-i18n 在编译期遵循「**先按 locale 合并、再统一扁平化**」的两阶段数据流转：合并阶段产物是 `Translations`（`BTreeMap<Locale, 嵌套 Value>`），扁平化阶段产物是 `BTreeMap<Locale, BTreeMap<点号键, 译文>>`。
- `merge_value` 只有两条规则：**双方都是对象就逐 key 递归下钻**（a 没有的 key 用 `Null` 占位再填），**其它情况就用 b 整体覆盖 a**。它被「逐文件合并」的编排点（[L150-L155](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L150-L155)）和 v2 解析内部两处复用。
- `flatten_keys` 深度优先地把嵌套树压成 `BTreeMap<String,String>`：对象下钻并 `extend` 子结果，叶子（含 `Number`/`Bool`）字符串化，`Null`/`Array` 压成空串。它由「逐 locale 扁平化」的编排点（[L158-L160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L158-L160)）驱动，初始前缀为空。
- `format_keys` 是 v2 解析专用的「跳过空段、用点连接」工具，与 `flatten_keys` 内部的 `if/else` 拼接语义等价。
- 最终的扁平表按键名字典序排列（因为用了 `BTreeMap`），这正是下一讲 u2-l4 中 `generate_code` 生成 `SimpleBackend` 静态数据的输入。

## 7. 下一步学习建议

- **紧接着读 u2-l4**：看 `generate_code` 如何把本讲产出的 `BTreeMap<String, BTreeMap<String, String>>` 用 `quote!` 编译成 `_RUST_I18N_BACKEND`（一个 `SimpleBackend`）以及 `_rust_i18n_translate` 查找函数。你会看到本讲的扁平表是如何「变」成运行时代码的。
- **回头验证 u2-l2**：本讲的合并/扁平化是 `try_load_locales` 的后半段；建议对照 u2-l2 讲义里的 `parse_file` / `parse_file_v1` / `parse_file_v2`，把「glob → parse → merge → flatten」整条链路在脑中走一遍。
- **为 u3 做铺垫**：理解了「点号键」之后，u3 讲运行时 `t!("messages.count")` 时，你会发现查找的 key 正是本讲 `flatten_keys` 产出的点号键——编译期的键形态和运行时的查询形态是完全对齐的。
