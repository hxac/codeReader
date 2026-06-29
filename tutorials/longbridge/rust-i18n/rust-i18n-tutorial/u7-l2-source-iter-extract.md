# 源码遍历与 t! 提取

## 1. 本讲目标

本讲紧接 [u7-l1](u7-l1-cli-entry.md) 讲清的 `cargo i18n` 命令行入口，深入提取器内部，回答一个核心问题：

> 当你执行 `cargo i18n` 时，它是**怎么**从成百上千个 `.rs` 源文件里，把每一处 `t!("...")` 调用的文案和所在行号一个不漏地找出来的？

学完本讲，你应当能够：

1. 理解 `iter_crate` 如何用 `ignore` 库遍历源码目录，并且天然尊重 `.gitignore`。
2. 掌握 `Extractor::invoke` 如何把源码切成 token 流，再用 `Ident + "!"` 的模式递归识别出 `t!` / `tr!` 宏调用。
3. 看清 `take_message` 如何只取宏的**第一个字面量参数**，并从 token 的 `span` 里读出行号。
4. 弄懂 `Message` / `Location` / `Results` 这套数据模型，以及提取器如何通过「空白归一化」对重复文案做去重。

本讲只讲「遍历 + 提取」，把提取结果写成 `TODO.yml` 的部分留给 [u7-l3](u7-l3-generator-todo-yml.md)。

## 2. 前置知识

阅读本讲前，你需要先建立以下概念（前序讲义已建立，这里只做一句话回顾）：

- **cargo 子命令与主流程**（[u7-l1](u7-l1-cli-entry.md)）：`cargo i18n` 的 `main` 会先 `iter`（遍历）→ 再 `extract`（提取）→ 最后 `generate`（生成文件）。本讲正是其中「遍历 + 提取」这两步的实现细节。
- **token 流（token stream）**：Rust 源码经过词法分析后，会变成一串 token（标识符 `Ident`、标点 `Punct`、字面量 `Literal`、分组 `Group` 等）。过程宏和本讲的提取器都工作在 token 流层面，而不是原始字符串层面。
- **proc_macro2**：标准库的 `proc_macro` 类型只能在过程宏内部使用；`proc_macro2` 是它的「可分离」复刻版，可以在普通库（比如 `rust-i18n-extract`）里对 token 流做解析。
- **`t!` 与 `tr!`**：用户在源码里写的是 `t!("...")`；`tr!` 是 `_tr!` 的别名形态，提取器把两者同等对待。
- **查找键 vs 译文内容**（[u7-l1](u7-l1-cli-entry.md) 已强调）：提取产出的 `HashMap` 的 key 是「查找键」（开 minify_key 时是哈希短键），而 `Message.key` 字段是「译文内容」。本讲会再次看到这条约定的具体落点。

> 小提示：如果你对「token 流」还比较陌生，可以先把本讲的 4.2 节配合 4.2.3 的源码一起看，遇到陌生术语再回查。

## 3. 本讲源码地图

本讲涉及三个文件，全部位于 `rust-i18n-extract` crate（`crates/extract/`）：

| 文件 | 作用 |
| --- | --- |
| [crates/extract/src/iter.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/iter.rs) | 目录遍历器。用 `ignore::WalkBuilder` 扫描源码树，挑出 `.rs` 文件，把 `(路径, 文件内容)` 逐个喂给回调。 |
| [crates/extract/src/extractor.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs) | 提取器核心。把文件内容解析成 token 流，递归找出 `t!`/`tr!` 调用，取出首参字面量与行号，组装成 `Message`。 |
| [crates/extract/src/example.test.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs) | 测试夹具。一段含多种 `t!` 写法（含 raw 字符串、多空格）的样例源码，被 `test_extract` 用 `include_str!` 引入并断言。 |

此外会顺带引用 [crates/cli/src/main.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs) 中把 `iter` 与 `extract` 串起来的那几行，作为两者的衔接证据。

## 4. 核心概念与源码讲解

### 4.1 源码遍历器 iter_crate：用 ignore 扫描 .rs 文件

#### 4.1.1 概念说明

`iter_crate` 解决的问题是：**给定一个源码根目录（默认是当前目录），怎么把里面所有该翻译的 Rust 源文件找出来？**

这里有一个朴素做法和一个工程做法：

- **朴素做法**：用 `std::fs` 自己递归列目录。问题在于要手动处理一堆边界情况：要不要进 `target/`？要不要进 `.git/`？要不要尊重 `.gitignore`？符号链接会不会成环？
- **工程做法**：直接用 [`ignore`](https://docs.rs/ignore) 库（ripgrep 同源的目录遍历库），它开箱即用地尊重 `.gitignore`、`.ignore`、隐藏文件规则，并且能避免符号链接环路。

`rust-i18n` 选择了后者——这也是为什么 `cargo i18n` 默认不会去扫描 `target/` 编译产物。

#### 4.1.2 核心流程

`iter_crate` 是一个泛型函数，接受一个回调 `F`，对每个 `.rs` 文件调用一次：

```text
输入: src_path（源码根目录，如 "./"）, callback
  │
  ▼
1. 去掉 src_path 末尾的 '/'，规范化输入
  │
  ▼
2. 用 ignore::WalkBuilder 构造遍历器
   - .git_ignore(true)   尊重 .gitignore
   - .parents(true)      尊重父目录的 .gitignore/.ignore
   - .follow_links(false) 不跟随符号链接（防环路）
  │
  ▼
3. walker.build() 产出一条条目录条目（DirEntry）
   对每一条:
     ├─ 不是普通文件  → 跳过（目录、符号链接等）
     ├─ 扩展名不是 "rs" → 跳过
     └─ 是 .rs 文件 → 读全部内容，调用 callback(path, content)
  │
  ▼
4. 遍历完毕返回 Ok(())
```

注意：`iter_crate` **只负责「找文件 + 读内容 + 喂回调」**，它完全不知道 `t!` 是什么——所有「识别宏、提取文案」的活儿都在回调（即 `extractor::extract`）里。这是一个干净的「遍历」与「处理」分离。

#### 4.1.3 源码精读

整段实现在 [crates/extract/src/iter.rs:6-45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/iter.rs#L6-L45)，逐段看：

函数签名——泛型回调 `F`，对每个 `.rs` 文件调用 `callback(&PathBuf, &str)`（路径、源码文本）：

```rust
pub fn iter_crate<F>(src_path: &str, mut callback: F) -> Result<(), Error>
where
    F: FnMut(&PathBuf, &str) -> Result<(), Error>,
```

构造遍历器并配置三件套（`git_ignore` 尊重忽略规则、`parents` 让父目录的忽略规则也生效、`follow_links(false)` 防止符号链接成环）——见 [crates/extract/src/iter.rs:12-17](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/iter.rs#L12-L17)：

```rust
let mut walker = ignore::WalkBuilder::new(src_path);
walker
    .skip_stdout(true)
    .parents(true)
    .git_ignore(true)
    .follow_links(false);
```

遍历并对每个条目做两道过滤——「必须是文件」与「扩展名必须是 `rs`」——见 [crates/extract/src/iter.rs:19-37](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/iter.rs#L19-L37)。命中后 `read_to_string` 读全文，再喂给回调：

```rust
if !path.is_file() { continue; }
if path.extension() != Some("rs".as_ref()) { continue; }
// ... File::open + read_to_string ...
callback(&PathBuf::from(filepath), &s)?;
```

> 关键点：`iter_crate` 把整个文件一次性读进 `String` 再交给回调。这意味着提取器拿到的是「文件全文」，行号信息要靠后面 `proc_macro2` 的 `span` 来恢复，而不是靠逐行迭代。

#### 4.1.4 代码实践

**实践目标**：直观感受 `ignore` 对 `.gitignore` 的尊重。

**操作步骤**：

1. 在仓库根目录新建一个临时目录 `tmp_walk/`，里面放两个文件：
   - `tmp_walk/a.rs`，内容随便写一行注释；
   - `tmp_walk/b.rs`，内容随便写一行注释。
2. 在 `tmp_walk/` 下新建 `.gitignore`，写入一行 `b.rs`。
3. 在 `crates/extract/` 下临时加一个测试（本实践只是观察，做完可删掉）：

   ```rust
   #[test]
   fn practice_iter_crate_observes_gitignore() {
       let mut visited = vec![];
       let _ = iter::iter_crate("tmp_walk", |path, _src| {
           visited.push(path.file_name().unwrap().to_string_lossy().to_string());
           Ok(())
       });
       println!("{:?}", visited);
   }
   ```

4. 在仓库根目录运行（因为相对路径从 crate 根算起，建议把 `tmp_walk` 放到仓库根，并把上面的路径改成指向它）：
   ```bash
   cargo test -p rust-i18n-extract practice_iter_crate -- --nocapture
   ```

**需要观察的现象**：打印的 `visited` 里**只出现 `a.rs`，不出现 `b.rs`**——因为 `b.rs` 被 `.gitignore` 命中而被 `ignore` 库过滤。

**预期结果**：`["a.rs"]`（顺序可能不同，但绝不包含 `b.rs`）。

> 待本地验证：不同 `ignore` 版本对 `.gitignore` 的解析细节可能略有差异；若想验证 `target/` 默认不被扫描，可把 `tmp_walk` 换成仓库根目录直接跑现有 `cargo i18n`，观察输出不会包含 `target/` 下的翻译键。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `.follow_links(false)` 改成 `true`，可能引发什么风险？

> **参考答案**：可能跟随符号链接进入循环（例如某目录里有指向自身祖先的软链），导致遍历死循环或重复处理同一文件。`false` 是安全的默认。

**练习 2**：`iter_crate` 为什么不在内部直接调用 `extractor::extract`，而要用回调 `F`？

> **参考答案**：为了解耦。「如何遍历目录」与「如何从单个文件提取翻译」是两件独立的事；用回调让遍历器保持通用，提取器也能被单独测试（事实上 `test_extract` 就是直接喂字符串、完全不经过 `iter_crate`）。

---

### 4.2 token 流递归识别宏调用：Extractor::invoke 与 METHOD_NAMES

#### 4.2.1 概念说明

拿到文件全文后，提取器要回答：**这段源码里，哪些 token 组合表示一次 `t!(...)` 调用？**

注意它**不能**用正则在字符串里找 `t!(`——因为那会误伤注释、字符串字面量里的 `t!(`，也会漏掉跨行、带嵌套括号的情况。正确做法是让 `syn` 先把源码解析成「合法 Rust 语法」，再把语法树展平成 token 流，然后在 token 层面做识别。

识别的核心模式极其朴素：

> 一个**标识符** `t`（或 `tr`），紧跟一个**感叹号标点** `!`，再紧跟一个**分组** `(...)` —— 这就是一个宏调用。

`METHOD_NAMES` 这个常量就是「允许的宏名白名单」。

#### 4.2.2 核心流程

`invoke` 是一个递归函数，消费一个 `TokenStream`，对每个 token 做模式匹配：

```text
invoke(stream):
  for token in stream:
    match token:
      Group(g)        → 递归 invoke(g.stream())   // 钻进 () {} [] 任何分组
      Ident(name):
        若 下一 token 是 Punct("!"):
            is_macro = true，吃掉 "!"
        若 name ∈ METHOD_NAMES 且 is_macro:
            若 下一 token 是 Group(g):
                take_message(g.stream())          // 提取这个宏的参数
      其它 → 忽略
```

这里有一个**容易被忽略但很关键**的细节：识别到 `t ! (group)` 之后，`invoke` 调用 `take_message(group.stream())` 时用的是 **`peek`（窥视）而不是 `next`（消费）**——也就是说那个 `Group` token 并没有从迭代器里拿走。于是下一轮循环还会再遇到这个 `Group`，再次进入递归 `invoke`。这样设计的副作用是：**宏调用本身被 `take_message` 提取一次，其内部又被递归扫一遍**，从而不会漏掉宏参数里嵌套的另一个 `t!`（比如 `t!(t!("nested"))`）。对普通 `t!("hello")` 而言，分组内部只有一个字面量、没有嵌套宏，递归只是空跑，无害。

#### 4.2.3 源码精读

白名单常量——只有名为 `t` 或 `tr` 的宏才会被提取，见 [crates/extract/src/extractor.rs:35](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L35)：

```rust
static METHOD_NAMES: &[&str] = &["t", "tr"];
```

`invoke` 的核心 match，见 [crates/extract/src/extractor.rs:60-87](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L60-L87)。三段分别处理「分组→递归」「标识符→判宏→提取」「其余忽略」：

```rust
TokenTree::Group(group) => self.invoke(group.stream())?,
TokenTree::Ident(ident) => {
    let mut is_macro = false;
    if let Some(TokenTree::Punct(punct)) = token_iter.peek() {
        if punct.to_string() == "!" {
            is_macro = true;
            token_iter.next();   // 消费 "!"
        }
    }
    let ident_str = ident.to_string();
    if METHOD_NAMES.contains(&ident_str.as_str()) && is_macro {
        if let Some(TokenTree::Group(group)) = token_iter.peek() {
            self.take_message(group.stream());   // 注意：peek，不消费
        }
    }
}
_ => {}
```

入口 `extract` 把「文件全文」交给 `syn::parse_file` 做语法解析（这一步会过滤掉注释、保证语法合法），再 `into_token_stream()` 展平成流，交给 `invoke`——见 [crates/extract/src/extractor.rs:38-50](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L38-L50)：

```rust
let mut ex = Extractor { results, path, cfg };
let file = syn::parse_file(source)
    .unwrap_or_else(|_| panic!("Failed to parse file, file: {}", path.display()));
let stream = file.into_token_stream();
ex.invoke(stream)
```

`extract` 在 CLI 中的调用点，正是 `iter_crate` 的回调，见 [crates/cli/src/main.rs:108-110](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L108-L110)——这就是「遍历」与「提取」的衔接：

```rust
iter::iter_crate(&source_path, |path, source| {
    extractor::extract(&mut results, path, source, cfg.clone())
})?;
```

#### 4.2.4 代码实践

**实践目标**：用真实测试夹具，亲眼看到 `invoke` 能从一段多种写法混杂的源码里精确识别出每一处 `t!`。

**操作步骤**：

1. 打开 [crates/extract/src/example.test.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs)，逐行阅读其中 5 类写法（见 4.4 节会有详细对应表）。
2. 运行现有测试：
   ```bash
   cargo test -p rust-i18n-extract test_extract
   ```
3. 阅读测试断言 [crates/extract/src/extractor.rs:216-257](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L216-L257)，对照 `build_messages!` 列出的 5 条期望。

**需要观察的现象**：测试通过；5 条期望里，「带 `locale = ...`、`name = ...` 参数」的 `t!`（第 5、7 行）依然被识别，且 `locale`、`name` 等 ident **不会**被误当成另一次宏调用（因为它们后面跟的是 `=` 不是 `!`）。

**预期结果**：`test_extract` 通过，`messages.len() == 5`。

**待本地验证**：可在 `test_extract` 末尾临时打印 `ex.results`，观察 `Message.key` 与 `Location.line` 的实际取值，再删除打印。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `invoke` 用 `syn::parse_file` 而不是用正则在源码字符串里搜 `t!(`？

> **参考答案**：因为字符串层面的 `t!(` 可能出现在注释（`// 调用 t!(...) 来翻译`）或字符串字面量（`let s = "t!(foo)";`）里，会误报；也会漏掉跨多行、带嵌套的真实调用。`syn::parse_file` 先做语法解析，把注释剔除、把字符串字面量正确归并成一个 token，识别才可靠。

**练习 2**：源码里有一个普通函数 `fn hello()`（见 [example.test.rs:2](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L2)），里面的 `hello` 标识符会不会被误识别成宏？

> **参考答案**：不会。虽然 `hello` 是 `Ident` 且恰好在 `METHOD_NAMES` 里没有（它不是 `t`/`tr`），但即便它是 `t`，函数定义 `fn hello()` 后面跟的是 `()` 而非 `!`，`is_macro` 为 false，也不会触发提取。识别宏必须同时满足「名字在白名单」和「后面紧跟 `!`」两个条件。

---

### 4.3 提取首参字面量并记录行号：take_message

#### 4.3.1 概念说明

`invoke` 找到了「这是一次 `t!(...)` 调用」，但它**只把宏的整个参数分组 `(...)` 丢给 `take_message`**。`take_message` 要从这串参数里取出**真正作为翻译键的那个字面量**。

rust-i18n 的约定是：**宏的第一个参数就是消息**（例如 `t!("hello", name = "Jason")` 里的 `"hello"`，或 `t!("views.title")` 里的 `"views.title"`）。后面的 `name = "Jason"` 是插值参数，不参与提取。

所以 `take_message` 的逻辑非常直接：**只看第一个 token，如果它是字符串字面量就收下，否则放弃**。这同时意味着：动态键 `t!(some_variable)` 不会被提取——它没有字面量首参。这一点与 [u6-l3](u6-l3-minify-key-integration.md) 讲的「提取器只识别字面量首参」完全一致。

拿到字面量后，还有两件事要做：

1. **算出查找键**：决定这个文案在 `Results` 这个 `HashMap` 里用什么 key 存（开启 minify_key 时是哈希短键，否则是空白归一化后的原文）。
2. **记录行号**：从字面量的 `span`（它在源码里的位置信息）读出行号，存进 `Location`。

#### 4.3.2 核心流程

```text
take_message(stream):          # stream 是宏参数分组的内容
  first = stream 的第一个 token
  若 first 不是 Literal: 直接 return（动态键、空调用等）

  s = literal_to_string(first)  # 尝试把字面量解析成字符串
  若 s 为 None: return          # 例如 t!(123) 是数字字面量，放弃

  根据 cfg.minify_key 决定 (查找键, 译文内容):
    开启 → 查找键 = MinifyKey::minify_key(s, ...);  译文内容 = s 原文
    关闭 → 两者都 = format_message_key(s)            # 空白归一化

  message = results.entry(查找键).or_insert(Message::new(译文内容, index, minify_key))
  line = first.span().start().line
  若 line > 0:
    message.locations.push(Location { file, line })
```

`entry().or_insert_with` 是去重的关键：**同一个查找键只对应一个 `Message`**，第二次遇到时不会再新建，而是往已有 `Message` 的 `locations` 里**追加一个新的 `Location`**。所以同一段文案出现在 10 处，最终只有 1 条 `Message`，但带 10 个 `Location`——这正是「TODO.yml 不会把重复文案列 10 遍」的原因。

#### 4.3.3 源码精读

`take_message` 主体在 [crates/extract/src/extractor.rs:89-137](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L89-L137)。只取第一个 token、非字面量则放弃——见 [crates/extract/src/extractor.rs:92-96](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L92-L96)：

```rust
let literal = if let Some(TokenTree::Literal(literal)) = token_iter.next() {
    literal
} else {
    return;
};
```

minify_key 开关决定查找键与译文内容的拆分——见 [crates/extract/src/extractor.rs:107-120](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L107-L120)。开启时查找键是哈希短键、译文内容保留原文；关闭时两者都用归一化结果（承接 [u6-l3](u6-l3-minify-key-integration.md) 的「查找键 vs Message.key」约定）：

```rust
let (message_key, message_content) = if *minify_key {
    let hashed_key = rust_i18n_support::MinifyKey::minify_key(
        &key, *minify_key_len, minify_key_prefix, *minify_key_thresh,
    );
    (hashed_key.to_string(), key.clone())
} else {
    let message_key = format_message_key(&key);
    (message_key.clone(), message_key)
};
```

去重写入与行号记录——见 [crates/extract/src/extractor.rs:121-134](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L121-L134)。`index = self.results.len()` 在插入前取，记录的是「首次出现序号」，CLI 后续用它排序以还原源码顺序；行号来自 `lit.span().start().line`：

```rust
let index = self.results.len();
let message = self
    .results
    .entry(message_key)
    .or_insert_with(|| Message::new(&message_content, index, *minify_key));

let span = lit.span();
let line = span.start().line;
if line > 0 {
    message.locations.push(Location { file: self.path.clone(), line });
}
```

两个辅助函数也值得一读：

- `literal_to_string` 把 `proc_macro2::Literal` 转回字符串——见 [crates/extract/src/extractor.rs:140-145](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L140-L145)。它把字面量 `to_string()` 后再用 `syn::parse_str::<syn::LitStr>` 解析，只有**字符串字面量**才能解析成功，数字、布尔等会返回 `None` 而被丢弃：

  ```rust
  fn literal_to_string(lit: &proc_macro2::Literal) -> Option<String> {
      match syn::parse_str::<syn::LitStr>(&lit.to_string()) {
          Ok(lit) => Some(lit.value()),
          Err(_) => None,
      }
  }
  ```

- `format_message_key` 做空白归一化——见 [crates/extract/src/extractor.rs:147-151](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L147-L151)。把任意连续空白（含换行、缩进）压成单个空格再 `trim`：

  ```rust
  fn format_message_key(key: &str) -> String {
      let re = regex::Regex::new(r"\s+").unwrap();
      let key = re.replace_all(key, " ").into_owned();
      key.trim().into()
  }
  ```

> 关于行号：`span.start().line` 是 `proc_macro2` 提供的源码坐标。当提取器从 `syn::parse_file(source)` 解析时，`source` 的第一行就是 `line = 1`，这与 `example.test.rs` 的行号完全对齐（见 4.4 节）。`if line > 0` 是防御：某些合成 token 的 span 行号可能为 0，这种无法定位的不记录。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `take_message` 只取首参、且动态键不被提取。

**操作步骤**：

1. 在 `crates/extract/src/extractor.rs` 的 `#[cfg(test)] mod tests` 里新增一个测试：

   ```rust
   #[test]
   fn practice_take_message_only_first_literal() {
       // 注意：首参是动态变量 key，不是字面量
       let source = r#"
           fn main() {
               let key = "dynamic";
               t!(key);
               t!("views.title", locale = "en");
           }
       "#;
       let stream = proc_macro2::TokenStream::from_str(source).unwrap();
       let mut results = HashMap::new();
       let mut ex = Extractor {
           results: &mut results,
           path: &"x.rs".to_owned().into(),
           cfg: I18nConfig::default(),
       };
       ex.invoke(stream).unwrap();
       println!("{:?}", ex.results);
   }
   ```

2. 运行：
   ```bash
   cargo test -p rust-i18n-extract practice_take_message -- --nocapture
   ```

**需要观察的现象**：`results` 里**只有 `views.title` 一条**；`t!(key)` 因为首参是标识符（不是 `Literal`）而被 `take_message` 在第一步 `return` 掉，不会出现在结果里。

**预期结果**：`results.len() == 1`，唯一的键是 `"views.title"`，其 `Message.key == "views.title"`，带一个 `Location { line: 4 }`（按上面三引号字符串，`t!("views.title", ...)` 在第 4 行）。

**待本地验证**：行号取决于你三引号字符串里实际的换行位置；若不一致，以你本地打印的 `Location.line` 为准。

#### 4.3.5 小练习与答案

**练习 1**：`t!("hello", name = "Jason")` 里的 `"Jason"` 会不会被当成另一条文案提取？

> **参考答案**：不会。`take_message` 只消费参数流的**第一个** token（`"hello"`），后续的 `name = "Jason"` 完全不看。虽然 `invoke` 后续会递归扫这个分组，但 `name` 后面跟的是 `=` 不是 `!`，`is_macro` 为 false，`"Jason"` 也只是孤立的字面量、不在任何宏调用里，因此不会被提取。

**练习 2**：为什么行号要用 `span.start().line` 而不是在 `iter_crate` 里逐行计数？

> **参考答案**：因为 `iter_crate` 把整个文件读成一个 `String` 整体喂给提取器，提取器内部并不逐行迭代；`proc_macro2` 在解析 token 时已经为每个 token 记录了它来自源码的哪一行（即 span）。直接从字面量的 span 取行号，既准确又无需自己维护行计数器。

---

### 4.4 数据模型与去重验证：Message / Location / Results 与 example.test.rs

#### 4.4.1 概念说明

前面三节用到了 `Message`、`Location`、`Results` 三个类型，本节把它们集中讲清，并用真实测试夹具 `example.test.rs` 验证整个提取流程。

三个类型的关系：

- `Results = HashMap<String, Message>`：**整个提取过程的最终产物**。key 是「查找键」，value 是 `Message`。
- `Message`：一条文案。包含 `key`（译文内容）、`index`（首次出现序号，用于稳定排序）、`minify_key`（是否用了短键）、`locations`（它在源码里出现的所有位置）。
- `Location { file, line }`：一处具体的源码位置。

一条文案可以在源码里出现多次，但 `Results` 里只存一个 `Message`，每次出现追加一个 `Location`——这就是「去重」。

#### 4.4.2 核心流程

`example.test.rs` 是一个精心设计的夹具，覆盖了提取器必须正确处理的几种情况。把它和 `test_extract` 的期望对照，就能验证全部行为：

| 源码位置 | 写法 | 期望 `Message.key`（译文内容） | 期望 `Location.line` |
| --- | --- | --- | --- |
| [example.test.rs:4](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L4) | `t!("hello")` | `hello` | 4 |
| [example.test.rs:5](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L5) | `t!("views.message.title", locale = "en", name = "Jason")` | `views.message.title` | 5 |
| [example.test.rs:7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L7) | `t!("views.message.description", name = "Jason")` | `views.message.description` | 7 |
| [example.test.rs:11-15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L11-L15) | 两段 raw 字符串，缩进/换行不同 | 归一化为同一句（去重成 1 条） | 11、14 两个位置 |
| [example.test.rs:18-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L18-L20) | 一句正常 + 一句多空格 | 归一化后相同（去重成 1 条） | 18、20 两个位置 |

最终 `Results` 里有 **5 条 `Message`**（注意第 4、5 行各算一条，第 11~15 行合并成一条，第 18~20 行合并成一条，共 1+1+1+1+1 = 5）。

#### 4.4.3 源码精读

数据结构定义——见 [crates/extract/src/extractor.rs:8-33](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L8-L33)。注意 `Message::new` 初始化 `locations` 为空 vec，后续由 `take_message` 逐个 `push`：

```rust
pub type Results = HashMap<String, Message>;

pub struct Location { pub file: std::path::PathBuf, pub line: usize }

pub struct Message {
    pub key: String,          // 译文内容
    pub index: usize,         // 首次出现序号，用于排序
    pub minify_key: bool,
    pub locations: Vec<Location>,
}
```

测试夹具本体——见 [crates/extract/src/example.test.rs:1-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L1-L22)。其中第 19 行注释 `// Will remove spaces for avoid duplication.` 正是在解释第 18、20 行两条文案为何会被 `format_message_key` 归一化合并。

测试用例本体——见 [crates/extract/src/extractor.rs:216-257](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L216-L257)。它用 `include_str!("example.test.rs")` 把夹具读成字符串、解析成 token 流、喂给 `invoke`，再用 `build_messages!` 宏列出 5 条期望并逐一比对（比对前把 `index` 归零以忽略顺序差异）：

```rust
let source = include_str!("example.test.rs");
let stream = proc_macro2::TokenStream::from_str(source).unwrap();
// ... build_messages! 列出 5 条期望 ...
let mut ex = Extractor { results: &mut results, path: &"hello.rs".into(), cfg: I18nConfig::default() };
ex.invoke(stream).unwrap();
let mut messages: Vec<_> = ex.results.values().collect();
messages.sort_by_key(|m| m.index);
assert_eq!(expected.len(), messages.len());
```

> 一个细节：第 11~15 行的两段 raw 字符串，一段结尾带「逗号前有空格 + 换行」（`text, \n            and...`），另一段是「逗号后直接换行」（`text,\nand...`）。`format_message_key` 把所有连续空白压成一个空格，于是两者都变成 `Use YAML for mapping localized text, and support mutiple YAML files merging.`，查找键相同，去重成一条 `Message`，带 `line: 11` 和 `line: 14` 两个位置——与 `build_messages!` 的第 4 条期望 `(..., 11, 14)` 完全吻合。

#### 4.4.4 代码实践

**实践目标**：在本讲任务规格指定的最小输入上，验证 `results` 中 `Message.key` 与 `Location.line` 的正确性。

**操作步骤**：

1. 在 `crates/extract/src/extractor.rs` 的 `mod tests` 里新增一个最小测试（这是本讲的核心实践）：

   ```rust
   #[test]
   fn practice_minimal_two_keys() {
       // 一个只含两个 t! 调用的 .rs 文件（以字符串形式）
       let source = r#"fn main() {
    t!("hello");
    t!("views.title");
}
"#;
       let stream = proc_macro2::TokenStream::from_str(source).unwrap();
       let mut results = HashMap::new();
       let mut ex = Extractor {
           results: &mut results,
           path: &"demo.rs".to_owned().into(),
           cfg: I18nConfig::default(),
       };
       ex.invoke(stream).unwrap();

       // 断言：恰好两条文案
       assert_eq!(ex.results.len(), 2);

       let hello = ex.results.get("hello").expect("hello must exist");
       assert_eq!(hello.key, "hello");
       assert_eq!(hello.locations.len(), 1);
       assert_eq!(hello.locations[0].line, 2);   // t!("hello") 在第 2 行

       let title = ex.results.get("views.title").expect("views.title must exist");
       assert_eq!(title.key, "views.title");
       assert_eq!(title.locations[0].line, 3);   // t!("views.title") 在第 3 行
   }
   ```

2. 运行：
   ```bash
   cargo test -p rust-i18n-extract practice_minimal_two_keys -- --nocapture
   ```

**需要观察的现象**：测试通过。`results` 的查找键正好是 `hello` 和 `views.title`（因为 minify_key 关闭，查找键 = 归一化后的原文）；两条 `Message` 各自带一个 `Location`，行号分别是 2 和 3（按上面 `source` 字符串：第 1 行 `fn main`、第 2 行 `t!("hello")`、第 3 行 `t!("views.title")`）。

**预期结果**：`practice_minimal_two_keys` 测试通过，`ex.results.len() == 2`，两条 `Message.key` 与 `Location.line` 均与断言一致。

**待本地验证**：若你调整了 `source` 字符串的换行（例如把 `fn main() {` 与第一个 `t!` 写在同一行），行号会相应变化，请以本地实际为准修改断言里的行号。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `example.test.rs` 的第 11~15 行（两段 raw 字符串）最终只产生**一条** `Message`，却有两个 `Location`？

> **参考答案**：两段字符串的可见文字相同，仅空白/换行不同。`format_message_key` 把连续空白归一化为单个空格，所以它们的**查找键**相同，`entry().or_insert_with` 第二次命中时不再新建 `Message`，而是往已有 `Message` 的 `locations` 里追加第二个 `Location`。结果：1 条 `Message` + 2 个 `Location`（line 11、line 14）。

**练习 2**：`Message.index` 字段有什么用？为什么 `test_extract` 比对前要把它置 0？

> **参考答案**：`index` 记录文案**首次被插入 `Results` 的先后顺序**（取插入前 `self.results.len()`），CLI 用它把文案按源码出现顺序稳定排序后写进 `TODO.yml`。测试里 `HashMap` 的迭代顺序不确定，为了只比较「内容」而不比较「顺序」，比对前把 `index` 统一置 0。

**练习 3**：夹具里有一行注释 `// comment 1`（[example.test.rs:3](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L3)），它会被提取吗？为什么？

> **参考答案**：不会。`syn::parse_file` 在语法解析阶段已经把注释剔除，注释根本不会进入 token 流，因此 `invoke` 永远看不到它。这正是用 `syn` 而非正则的一个直接好处。

## 5. 综合实践

把本讲四个模块串起来，做一个「迷你提取器」端到端验证。

**任务**：构造一个含 4 处 `t!`/`tr!` 调用、刻意制造 1 处去重场景的源码字符串，跑通 `extract`，打印 `Results`，并解释每条结果。

**建议的源码字符串**（写入一个新的 `#[test]`，或一个临时 bin）：

```rust
let source = r#"
fn render() {
    t!("welcome");
    tr!("welcome");            // 与上一行同 key，应去重，location 为 3 和 4
    t!("greeting", name = "Alice");
    // t!("in_comment");       // 注释，不应提取
    let k = "dynamic";
    t!(k);                     // 动态键，首参非字面量，不应提取
}
"#;
```

**要完成的事**：

1. 仿照 4.4.4 的写法，把 `source` 喂给 `Extractor::invoke`。
2. 打印 `ex.results`（用 `{:?}`）。
3. 用本讲学到的知识，回答：
   - `results.len()` 等于几？（**预期 2**：`welcome`、`greeting`）
   - `welcome` 这条 `Message` 的 `locations` 有几个？行号分别是多少？（**预期 2 个：3、4**）
   - `greeting` 的 `Message.key` 是什么？`locations[0].line` 是几？（**预期 `"greeting"`、第 5 行**）
   - 注释里的 `t!("in_comment")` 和动态键 `t!(k)` 为什么没出现？

**运行命令**：

```bash
cargo test -p rust-i18n-extract practice_mini_extractor -- --nocapture
```

**预期结果**：`results.len() == 2`；`welcome` 带 `Location{line:3}` 与 `Location{line:4}`；`greeting` 带 `Location{line:5}`；注释与动态键均不出现。

> 待本地验证：行号取决于三引号字符串实际换行，请以本地打印为准。这个实践同时验证了 `METHOD_NAMES`（`tr!` 也被识别）、`take_message` 只取首参字面量、`syn` 过滤注释、以及 `format_message_key` 去重四个要点。

## 6. 本讲小结

- `iter_crate` 用 `ignore::WalkBuilder` 遍历源码树，天然尊重 `.gitignore`，并只挑出 `.rs` 文件，把 `(路径, 全文)` 喂给回调——它只管「找文件」，不管「找 `t!`」。
- 提取器入口 `extract` 先用 `syn::parse_file` 把源码解析成合法语法树（注释在此被剔除），再展平成 token 流交给 `invoke`。
- `invoke` 用 `Ident + "!"` 的朴素模式递归识别宏调用，名字必须在白名单 `METHOD_NAMES = ["t", "tr"]` 里；遇到 `Group` 就递归下钻，因此能找到任意嵌套深度的调用。
- `take_message` 只取宏的**第一个字面量参数**：非字面量（动态键、数字等）直接放弃；字符串字面量才会被记录，行号取自字面量的 `span`。
- `format_message_key` 把连续空白归一化为单个空格，使「文字相同、空白不同」的多次调用合并成一条 `Message`、收集多个 `Location`——这是提取器去重的核心。
- 数据模型 `Results = HashMap<查找键, Message>`，其中 `Message.key` 是译文内容、`Message.index` 是首次出现序号（CLI 用它排序），「查找键 vs 译文内容」的分离在 minify_key 开关处再次体现。

## 7. 下一步学习建议

本讲拿到了「提取结果 `Results`」，但还没把它落盘成翻译文件。下一讲 [u7-l3 生成 TODO.yml 与合并去重](u7-l3-generator-todo-yml.md) 将讲清 `generator.rs` 如何：

1. 把 `Results` 序列化成 `_version: 2` 的多语言 YAML/JSON/TOML；
2. 与已有翻译对比，跳过已翻译的键、只把新文案写进 `TODO.yml`；
3. 在仍有未翻译文案时返回 `Err`，让 CI 失败。

建议阅读顺序：先把本讲的 `practice_minimal_two_keys` 跑通，再去读 [crates/extract/src/generator.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs) 中 `generate` 函数如何消费 `messages`（即本讲的 `Results`），就能把「提取 → 生成」整条链路彻底打通。

如果你对提取器如何与 `t!` 的 minify_key 短键保持一致感兴趣，可以回顾 [u6-l3 短键在 t! 与提取器中的协作](u6-l3-minify-key-integration.md)，本讲 4.3.3 里 `take_message` 的 minify_key 分支正是那套「键一致性铁律」的提取器侧实现。
