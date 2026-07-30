# import、路径、包、字体、标签补全

## 1. 本讲目标

在 Typst 里你经常会写出这样一些「需要外部数据」的补全：

- `#import "|"` —— 想补一个文件名，或一个包名 `@preview/example:0.1.0`；
- `#import "lib.typ": |` —— 想补这个模块里导出的名字；
- `#image("|")` —— 想补一张图片路径，且只想要 `.jpg/.png` 这类图片；
- `#text(font: |)` —— 想补一个字体家族名；
- `@|` 或 `(<la|` —— 想补一个文档里定义过的标签；
- ` ```| ` —— 想补 raw 块的语言标签（`rust`、`python`……）。

这些补全与前面几讲的「作用域补全」「字段补全」有一个本质区别：**它们的数据不在语法树里，而来自外部世界**——文件系统里有哪些文件、注册表里有哪些包、系统装了哪些字体、上次编译产出了哪些标签。本讲就讲 typst-ide 如何把这些外部数据接进补全引擎。

学完本讲你应当能够：

1. 说清 `complete_imports` 如何用三段式匹配分别处理「import 路径字符串」「冒号后的导入项」「半成品导入项标识符」，并理解它如何根据字符串是否以 `@` 开头把请求分流给「包补全」或「文件补全」。
2. 解释 `package_completions` 的排序键与「按 `(namespace, name)` 去重保留最新版本」的逻辑，并知道它是 `IdeWorld::packages()` 的唯一消费者。
3. 掌握 `file_completions_with_extensions` 如何以当前文件目录为基准计算相对路径，以及 `path_completion` 如何按「函数名 + 参数名」决定要过滤哪些扩展名；知道它是 `IdeWorld::files()` 的唯一消费者。
4. 理解 `font_completions` 在 `#show math.equation: ...` 这类 equation show 规则下「只补数学字体」的过滤原理。
5. 说清 `label_completions` 如何借助 `analyze_labels` 返回的 `split` 偏移，在 `@`（引用）、`< >`（标签）、`#cite(...)`（参考文献）三种上下文里用 `skip`/`take` 选取不同的标签子集。
6. 知道 `raw_completions` 的语言标签数据来自 `typst::text::RawElem::languages()`，以及它为什么用文本扫描而非语法节点来触发。

本讲承接 u6-l1（补全分发管线）、u6-l2（字段访问补全，分发链第一关），并回用 u1-l2（`IdeWorld` 数据契约）与 u2-l4（`analyze_import` / `analyze_labels`）。

## 2. 前置知识

阅读本讲前，请确认你已了解：

- **`IdeWorld` 数据契约**（u1-l2）：`IdeWorld: World` 在编译器所需的 `World` 之上扩展了三个方法——`upcast()`（必填）、`packages()`（可选，默认空切片）、`files()`（可选，默认空 `Vec`）。本讲会反复用到后两个「可选增强」方法。`World` 自身的 `book()`（字体簿）则是**必填**方法。
- **补全分发链**（u5-l1 / u6-l1）：`autocomplete` 用 `||` 短路依次尝试 `complete_field_accesses → complete_open_labels → complete_imports → complete_rules → complete_params → 通用模式`。本讲的 `complete_imports` 与 `complete_open_labels` 就在这条链上。
- **`CompletionContext`**（u5-l2）：贯穿补全的可变上下文，维护 `leaf`、`cursor`、`from`、`before`/`after`、`world`、`output`，以及候选列表 `completions`。所有补全都以副作用往 `ctx.completions` 里写。
- **`analyze_import` 与 `analyze_labels`**（u2-l4）：前者把一个 import 源节点解析成 `Value::Module`；后者返回 `(labels, split)`，`split` 之前是文档元素标签、之后是参考文献键。
- **`output` 可选参数**：`autocomplete` 的 `output: Option<impl AsOutput>` 是上一次成功编译的产物。标签补全**强依赖**它——没有产物就没有标签可补（优雅降级为空）。

几个本讲专有的术语先统一：

- **专项补全（specialized completion）**：指字体、包、文件、标签、raw 这几类「数据来自外部」的补全。它们的生成器（`font_completions` 等）都是 `CompletionContext` 上的方法，会被多个触发位置复用。
- **`split` 偏移**：`analyze_labels` 返回的 usize，等于「文档标签数量」。它把一个连续的标签数组在逻辑上切成「文档标签」和「参考文献键」两段。
- **触发位置（trigger site）**：调用某个生成器的那段代码，例如 `complete_imports` 和 `param_value_completions` 都会调用文件/字体补全，它们就是两个不同的触发位置。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-ide/src/complete.rs` | 本讲主战场。`complete_imports`/`complete_open_labels` 负责识别触发；`CompletionContext` 上的 `package_completions`/`file_completions`/`file_completions_with_extensions`/`font_completions`/`label_completions`/`raw_completions` 是六个生成器；`path_completion`/`param_value_completions`/`is_in_equation_show_rule` 是关键辅助。 |
| `crates/typst-ide/src/analyze.rs` | `analyze_import`（解析模块以列举导入项）、`analyze_labels`（返回标签与 `split`）。 |
| `crates/typst-ide/src/lib.rs` | `IdeWorld` trait 的定义，`packages()` 与 `files()` 的默认空实现。 |
| `crates/typst-ide/src/tests.rs` | `TestWorld` 对 `IdeWorld::packages()`/`files()` 的实现，以及 `with_source`/`with_asset_at` 构造多文件测试场景的 builder。 |
| `crates/typst-ide/src/utils.rs` | `summarize_font_family`（字体摘要，承接 u2-l5/u3-l4），供 `font_completions` 生成 detail。 |

## 4. 核心概念与源码讲解

在进入各个模块前，先看一张贯穿全讲的「数据来源 → 生成器 → 触发位置」对照表，它是理解本讲的总线索：

| 数据来源 | 必填? | 生成器方法 | 触发位置 |
|------|------|------|------|
| `World::book()` | 必填 | `font_completions` | `param_value_completions`（参数名为 `font`） |
| `IdeWorld::packages()` | **可选** | `package_completions` | `complete_imports`（import/include 路径以 `@` 开头） |
| `IdeWorld::files()` | **可选** | `file_completions` / `file_completions_with_extensions` | `complete_imports`（import/include 路径）+ `param_value_completions`（`path_completion`） |
| `output`（编译产物） | **可选** | `label_completions` | `complete_open_labels`（`<`）+ `complete_markup`（`@`）+ `cast_completions`（`Label` 类型） |
| `RawElem::languages()` | 静态 | `raw_completions` | `complete_markup`（` ``` ` 之后） |

记住一句话：**包补全和文件路径补全是仅有的两个消费可选 `IdeWorld` 方法的补全**；字体补全只用必填的 `book()`；标签补全只用可选的 `output`；raw 补全不依赖任何运行时数据。这张表的每一行都会在下面展开。

### 4.1 complete_imports：import 路径与导入项补全

#### 4.1.1 概念说明

`#import` 和 `#include` 是 Typst 里唯一会把「字符串字面量」当成路径或包名来用的语法。围绕这条语句，用户会在三个截然不同的位置请求补全：

1. **路径字符串内部**：`#import "|"`、`#include "|"`。光标停在引号里，用户想补的是「导入什么」——可能是一个本地文件（`lib.typ`），也可能是一个包（`@preview/example:0.1.0`）。
2. **冒号之后、导入列表为空或已有项**：`#import "lib.typ": |`、`#import "lib.typ": a, b, |`。用户想补的是「从这个模块里挑哪些名字」。
3. **导入列表里半成品标识符上**：`#import "lib.typ": thi|`。用户已经打了几个字母，想补全某个导出名。

`complete_imports`（[src/complete.rs:260-309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L260-L309)）就是把这三类位置识别出来、分别派发。它的关键设计是：**位置 1 的分流依据是「字符串是否以 `@` 开头」**——`@` 开头走包补全，否则走文件补全（限定 `.typ`）。

#### 4.1.2 核心流程

```
complete_imports(ctx):
  【位置 1：路径字符串】
    若 leaf 的父节点是 ModuleImport 或 ModuleInclude，且 leaf 本身能 cast 成 Str：
      value = str.get()
      from = leaf.offset()                     # 替换掉整个字符串内容
      若 value 以 '@' 开头：
        all_versions = value 是否包含 ':'       # 已带版本分隔符 → 列全部版本
        package_completions(all_versions)
      否则：
        file_completions_with_extensions(&["typ"])
      返回 true

  【位置 2：冒号后的导入列表】
    若 leaf 的前一个兄弟节点 prev 是 ModuleImport，且其 imports 是 Items：
      source = prev 的子节点里第一个 Expr（即那个路径字符串节点）
      from = cursor
      import_item_completions(items, source)
      返回 true

  【位置 3：导入列表里半成品标识符】
    若 leaf 是 Ident，且沿 parent→ImportItemPath→ImportItems→ModuleImport 一路向上成立：
      source = 该 ModuleImport 子节点里的 Expr
      from = leaf.offset()                     # 替换掉已打的半个名字
      import_item_completions(items, source)
      返回 true

  都不命中 → 返回 false
```

注意位置 2 与位置 3 都委托给同一个 `import_item_completions`，只是 `from`（替换起点）不同：位置 2 从光标处纯插入，位置 3 从标识符起点替换半个名字。两者都需要先在语法树里**重新定位到那个代表路径的 `source` 节点**，因为 `import_item_completions` 要拿它去解析模块。

#### 4.1.3 源码精读

位置 1 的判定与分流（[src/complete.rs:263-276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L263-L276)）：先确认「父节点是 `ModuleImport`/`ModuleInclude` 且自己是 `Str`」，再用 `value.starts_with('@')` 二选一。`all_versions` 由 `value.contains(':')` 决定——一旦用户已经敲到 `@preview/example:` 这种带冒号的形式，就说明他想要具体版本，于是把所有版本都列出来；否则只给每个包的最新版。

```rust
let value = str.get();
ctx.from = ctx.leaf.offset();
if value.starts_with('@') {
    let all_versions = value.contains(':');
    ctx.package_completions(all_versions);
} else {
    ctx.file_completions_with_extensions(&["typ"]);
}
```

位置 2 的判定（[src/complete.rs:281-289](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L281-L289)）：`prev_sibling` 是 `ModuleImport`、`imports()` 是 `Items`（而不是 `*` 通配或裸模块名），再用 `prev.children().find(|child| child.is::<ast::Expr>())` 把那个路径字符串节点挑出来当 `source`。

位置 3 的判定（[src/complete.rs:293-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L293-L306)）：需要连爬四层 `parent()`（`Ident → ImportItemPath → ImportItems → ModuleImport`），确保这个半成品标识符确实身处某个 import 的具名导入列表里，而非普通代码里的标识符。

`import_item_completions`（[src/complete.rs:312-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L312-L329)）本身的逻辑很薄：先用 `analyze_import` 把 `source` 解析成模块值（u2-l4），取其 `scope()` 遍历导出项；若导入列表当前为空，先补一个 `*`「全量导入」snippet；随后对每个导出名，跳过那些已经在 `existing` 列表里的（避免重复导入），其余用 `value_completion` 生成候选。

```rust
let Some(value) = analyze_import(ctx.world, source) else { return };
let Some(scope) = value.scope() else { return };

if existing.iter().next().is_none() {
    ctx.snippet_completion("*", "*", "Import everything.");
}

for (name, binding) in scope.iter() {
    if existing.iter().all(|item| item.original_name().as_str() != name) {
        ctx.value_completion(name.clone(), binding.read());
    }
}
```

#### 4.1.4 代码实践

**目标**：验证位置 1 的 `@` 分流，以及位置 2 的导入项补全。

**操作**：阅读测试 `test_autocomplete_import_items`（[src/complete.rs:1917-1928](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1917-L1928)）。它构造了一个多文件 world：

```rust
let world = TestWorld::new("#import \"other.typ\": ")
    .with_source("second.typ", "#import \"other.typ\": th")
    .with_source("other.typ", "#let this = 1; #let that = 2");
```

**观察**：

- 在 `main.typ` 的偏移 21（冒号后空格处）补全，断言 `must_include(["*", "this", "that"])`——因为列表为空，所以有 `*`；`other.typ` 导出了 `this` 和 `that`。
- 在 `second.typ` 的偏移 23（半成品 `th` 上）补全，断言 `must_include(["this", "that"]).must_exclude(["*", "figure"])`——因为已经打了 `th`，列表非空，所以 `*` 不再出现；也不会混入全局的 `figure`。

**预期结果**：运行 `cargo test -p typst-ide test_autocomplete_import_items` 通过。`@` 分流可参考 `test_autocomplete_packages`（[src/complete.rs:1861-1863](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1861-L1863)）。

#### 4.1.5 小练习与答案

**练习 1**：在 `#import "@preview/example:0.1.0": |` 的光标处，会走 `complete_imports` 的哪个位置？为什么不会触发包补全？

**答案**：走**位置 2**（冒号后的导入列表）。因为此时光标叶子已经不在路径字符串 `Str` 节点里（路径字符串已完整出现在冒号左边），`prev_sibling` 是完整的 `ModuleImport`，于是进入导入项补全。包补全只在「光标仍停在 `@...` 字符串内部」（位置 1）时触发。

**练习 2**：`import_item_completions` 里为什么要用 `item.original_name()` 而不是某个「重命名后的名字」来判重？

**答案**：因为一个导入项的「原始名」（模块里导出的名字）才是去重的稳定依据。用户可能把 `a` 重命名为 `b`（`a as b`），但只要原始名 `a` 还在列表里，就不该再建议导入它。用 `original_name()` 比对的是「源头」而非「本地别名」，避免漏判或误判。

### 4.2 package_completions：包名补全与版本去重

#### 4.2.1 概念说明

包补全是 typst-ide 里**唯一**消费 `IdeWorld::packages()` 的地方。`packages()` 返回 `&[(PackageSpec, Option<EcoString>)]`——一个「包规格 + 可选描述」的列表。默认实现返回空切片（[src/lib.rs:40-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L40-L42)），所以一个不实现 `packages()` 的 `IdeWorld` 不会有任何包补全——这是「可选增强、优雅降级」的典型例子。

`PackageSpec` 由三部分组成：`namespace`（如 `preview`）、`name`（如 `example`）、`version`（如 `0.1.0`）。一个包在注册表里常有多个版本，而用户大多数时候只想要最新版。于是 `package_completions` 要解决两个问题：**排序**（让结果稳定、可读）与**版本去重**（默认只展示每个包的最新版）。

#### 4.2.2 核心流程

```
package_completions(all_versions):
  1. packages = world.packages().iter().collect()        # 唯一数据来源
  2. 按 (&namespace, &name, Reverse(version)) 排序
       # 同一个 (namespace, name) 下，版本从新到旧
  3. 若非 all_versions：
       按 (&namespace, &name) 去重，保留排序后的第一个（= 最新版）
  4. 对每个 (package, description)：
       str_completion("{package}", kind=Package, detail=description)
```

排序键的设计可以用一个三元组刻画。设每个包规格为 \((ns, name, ver)\)，排序键为：

\[
\text{key}(p) = (ns,\ name,\ -ver)
\]

其中 \(-ver\) 表示「版本降序」。由于先按 `ns`、再按 `name` 字典序排列，最后版本取逆，同一个 \((ns, name)\) 的多个版本会紧挨在一起、且最新版排最前。`dedup_by_key` 在这个有序序列上保留每组的第一项——正是最新版。

#### 4.2.3 源码精读

`package_completions`（[src/complete.rs:1138-1153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1138-L1153)）：

```rust
fn package_completions(&mut self, all_versions: bool) {
    let mut packages: Vec<_> = self.world.packages().iter().collect();
    packages.sort_by_key(|(spec, _)| {
        (&spec.namespace, &spec.name, Reverse(spec.version))
    });
    if !all_versions {
        packages.dedup_by_key(|(spec, _)| (&spec.namespace, &spec.name));
    }
    for (package, description) in packages {
        self.str_completion(
            eco_format!("{package}"),
            Some(CompletionKind::Package),
            description.as_deref(),
        );
    }
}
```

三个要点：

- `Reverse(spec.version)` 让版本大的排前面；`dedup_by_key` 只在「不要全部版本」时启用，它对相邻的同 \((ns, name)\) 项保留首个——因为已按版本降序，首个即最新。
- `all_versions` 的来源（4.1.3 已述）：用户已敲到 `@ns/name:`（带冒号）时为真。
- 生成时把 `PackageSpec` 用 `eco_format!("{package}")` 渲染成 `@preview/example:0.1.0` 这样的完整字符串，再交给 `str_completion`（它会包成 `Value::Str`，见 4.3.3）。

`packages()` 的默认空实现（[src/lib.rs:34-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L34-L42)）的文档明确指出：`@preview` 命名空间的包描述可从 `https://packages.typst.org/preview/index.json` 获取——这是真实 LSP（如 tinymist）实现 `packages()` 时的数据源。

#### 4.2.4 代码实践

**目标**：确认 `package_completions` 是 `packages()` 的唯一消费者，并理解测试里的假数据。

**操作**：阅读 `TestWorld` 对 `packages()` 的实现（[src/tests.rs:127-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L127-L141)）。测试里硬编码了一个包：`namespace=preview, name=example, version=0.1.0`。

**观察**：测试 `test_autocomplete_packages` 用 `#import "@"` 在 `@` 后补全（[src/complete.rs:1861-1863](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1861-L1863)），断言 `must_include([q!("@preview/example:0.1.0")])`。`q!` 宏把字符串包上引号，所以 label 实际是 `"@preview/example:0.1.0"`。

**预期结果**：运行 `cargo test -p typst-ide test_autocomplete_packages` 通过。可以试着在 `tests.rs` 的 `LIST` 里再加一个同 `name` 不同 `version` 的项（如 `0.2.0`），重新跑该测试并观察：默认（不带冒号）只会出现 `0.2.0`，而把测试输入改成 `#import "@preview/example:"` 后两个版本都会出现。**（待本地验证第二个观察。）**

#### 4.2.5 小练习与答案

**练习 1**：如果 `packages()` 返回的列表里同一个包的多个版本**没有预先排序**，`dedup_by_key` 还能正确保留「最新版」吗？

**答案**：不能保证。`dedup_by_key` 只去重「相邻且键相同」的元素，保留的是它遇到的第一个。正因为先做了 `sort_by_key`（版本降序），每组的第一项才是最新版。排序是去重正确性的前提——这是一个容易踩的坑。

**练习 2**：为什么把 `package_completions` 的数据来源设计成可选的 `IdeWorld::packages()`，而不是像字体那样用必填的 `World::book()`？

**答案**：因为「枚举所有可用包」对编译本身毫无意义——编译器只需要解析用户**实际写了**的那个包并下载它（这是 `World` 的职责之外、由 kit 层处理的事）。而字体是排版必需的，`book()` 必须总有。把 `packages()` 设为可选，是为了不让「实现一个能编译的 World」背负「还要能枚举全网包」的负担，体现了解析与枚举的分离（u1-l2）。

### 4.3 file_completions 与扩展名过滤：文件路径补全

#### 4.3.1 概念说明

文件路径补全是 typst-ide 里**唯一**消费 `IdeWorld::files()` 的地方。`files()` 返回 `Vec<FileId>`——所有已知文件的 id。默认实现返回空 `Vec`（[src/lib.rs:44-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L44-L50)），所以不实现它的 `IdeWorld` 不会有路径补全。

它要解决两个问题：

1. **相对路径基准**：用户在 `content/a.typ` 里写 `#image("|")`，应该看到 `../assets/tiger.jpg`（相对当前文件所在目录），而不是绝对路径或相对 main 文件的路径。
2. **扩展名过滤**：`#image(...)` 只该补图片格式，`#csv(...)` 只该补 `.csv`，`#include` 只该补 `.typ`。这个过滤由 `path_completion` 按「函数名 + 参数名」查表决定。

#### 4.3.2 核心流程

```
file_completions(filter):
  1. current_id  = leaf.span().id()            # 当前文件 id
  2. current_dir = current_id.vpath().parent() # 当前文件所在目录
  3. 遍历 world.files()：
       - 排除 current_id（不补自己）
       - 用 filter(id) 过滤
       - 映射成 vpath().relative_from(current_dir)  # 相对当前目录的路径
  4. 排序
  5. 每个 path → str_completion(path, kind=Path)

file_completions_with_extensions(extensions):
  - 若 extensions 为空：file_completions(|_| true)   # 所有扩展名
  - 否则：file_completions(|id| extensions 包含 id 的扩展名)

path_completion(func, param) -> Option<&'static [&'static str]>:
  按 (func.name(), param.name()) 查表：
    (image, source)   → 图片格式
    (csv, source)     → [csv]
    (bibliography, sources) → [bib, yml, yaml]
    (_, path)         → []（所有文件）
    _                 → None（该参数不做路径补全）
```

#### 4.3.3 源码精读

`file_completions`（[src/complete.rs:1156-1173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1156-L1173)）是核心。注意三个细节：

```rust
fn file_completions(&mut self, mut filter: impl FnMut(FileId) -> bool) {
    let Some(current_id) = self.leaf.span().id() else { return };
    let Some(current_dir) = current_id.vpath().parent() else { return };

    let mut paths: Vec<EcoString> = self
        .world
        .files()
        .iter()
        .filter(|&&id| id != current_id && filter(id))
        .map(|id| id.vpath().relative_from(&current_dir))
        .collect();

    paths.sort();

    for path in paths {
        self.str_completion(path, Some(CompletionKind::Path), None);
    }
}
```

- `leaf.span().id()` 取当前文件的 id——路径补全的基准永远是「光标所在文件」，而不是 main 文件。这正是测试里能在 `content/a.typ` 看到相对路径的原因。
- `relative_from(&current_dir)` 把每个候选文件换算成相对当前目录的路径，自动产生 `../assets/tiger.jpg` 这样的 `..` 前缀。
- 末尾 `str_completion(path, kind=Path, None)` 把路径包成 `Value::Str`（[src/complete.rs:1262-1270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1262-L1270)）。`str_completion` 内部走 `value_completion_full`，于是会命中「引号去重」逻辑（[src/complete.rs:1315-1320](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1315-L1320)）：当 label 形如 `"content/a.typ"` 且光标后已有 `"` 时，`apply` 会去掉首尾引号，避免补出两个引号。

`file_completions_with_extensions`（[src/complete.rs:1178-1191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1178-L1191)）有一个值得注意的结构：

```rust
fn file_completions_with_extensions(&mut self, extensions: &[&str]) {
    if extensions.is_empty() {
        self.file_completions(|_| true);
    }
    self.file_completions(|id| {
        // 用 extensions.contains(...) 过滤
    });
}
```

当 `extensions` 为空时，第一个 `if` 分支补出**所有**文件；随后那条无条件执行的过滤调用，对一个空切片做 `contains` 永远为假，因此不产生任何候选——是一个无害的空操作。当 `extensions` 非空时，只有那条过滤调用生效。换句话说：**空数组 = 放行全部扩展名**，这与文档注释「If the array is empty, all extensions are allowed」一致。

`path_completion`（[src/complete.rs:603-623](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L603-L623)）是一张硬编码的查表，把 `(func.name(), param.name())` 映射到允许的扩展名。`(_, "path")` 这个通配分支返回 `&[]`（空数组，即所有文件）——这就是 `#pdf.attach("|")`、`#read("|")` 这类以 `path` 为参数名的地方会补出任意文件的原因（见 `test_autocomplete_file_path` 里 `content/f.typ` 的 `#read("")` 断言）。匹配不到任何分支时返回 `None`，表示「这个参数根本不做路径补全」。

#### 4.3.4 代码实践

**目标**：验证相对路径基准与扩展名过滤。

**操作**：阅读 `test_autocomplete_file_path`（[src/complete.rs:1866-1905](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1866-L1905)）。它构造了一个含多级目录与多种文件类型/资源的世界：

```rust
let world = TestWorld::new("#include \"\"")
    .with_source("utils.typ", "")
    .with_source("content/a.typ", "#image()")
    .with_source("content/b.typ", "#csv(\"\")")
    .with_source("content/d.typ", "#pdf.attach(\"\")")
    .with_source("content/f.typ", "#read(\"\")")
    .with_asset_at("assets/tiger.jpg", "tiger.jpg")
    .with_asset_at("data/example.csv", "example.csv");
```

**观察**：

- 在 `content/a.typ` 的 `#image("")` 里（偏移 -2），断言 `must_include(["../assets/tiger.jpg", "../assets/rhino.png"]).must_exclude(["../data/example.csv", "b.typ"])`——`image` 的 `source` 参数只要图片格式，且路径相对 `content/` 目录。
- 在 `content/f.typ` 的 `#read("")` 里，断言同时包含 `a.typ`（typ 文件）、`../assets/tiger.jpg`、`../data/example.csv`——因为 `read` 的参数名是 `path`，命中 `(_, "path") => &[]`，放行全部扩展名。
- 在 main 文件的 `#include ""` 里，断言包含 `content/a.typ`、`utils.typ`，排除 `assets/tiger.jpg`——因为 `complete_imports` 走 `file_completions_with_extensions(&["typ"])`。

**预期结果**：运行 `cargo test -p typst-ide test_autocomplete_file_path` 通过。`with_asset_at` 通过 `typst_dev_assets::get_by_name` 取真实二进制资源（u1-l3），所以 `tiger.jpg` 必须是 dev-assets 里真实存在的资源名。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `file_completions` 要先 `filter(|&&id| id != current_id ...)` 排除当前文件？

**答案**：因为让一个文件 `#include` 或 `#import` 自己没有意义（会形成自引用/循环），而且用户站在某个文件里补全路径时，当前文件几乎从不是目标。测试 `test_autocomplete_file_path` 里 `content/c.typ` 的 `#include ""` 断言 `must_exclude(["c.typ"])` 正是验证这一点。

**练习 2**：`#image("|")` 走 `path_completion` 得到 `&["png", "jpg", ...]`，这条调用链是怎样串起来的？

**答案**：`#image(...)` 的光标先被 `complete_params` 命中（参数列表内），deciding 节点是 `(` 或 `,`，进入 `param_completions`；但**值**位置的补全走的是 `named_param_value_completions` → `param_value_completions`（[src/complete.rs:587-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L600)）。在那里，参数名不是 `font`，于是 `path_completion(func, param)` 返回 `Some(图片扩展名)`，进而调用 `file_completions_with_extensions`。注意：路径补全的触发位置是 `param_value_completions`，而不是 `complete_imports`。

### 4.4 font_completions：字体补全与 equation 过滤

#### 4.4.1 概念说明

字体补全的数据来源是 `World::book()`——一个**必填**的 `World` 方法，返回字体簿 `FontBook`。所以即使 `IdeWorld` 的可选方法都不实现，字体补全照常工作（和包/文件补全的「可选」形成对比）。

它的典型触发位置是 `#text(font: |)`、`#show link: set text(font: |)` 这种「名为 `font` 的参数的值」。一个特别的设计是：**当光标位于 `#show math.equation: ...` 这类针对方程的 show 规则里时，只补数学字体**。理由是：数学公式只能用带数学字形表的字体（如 New Computer Modern Math）来排，给用户列出全部字体会干扰选择。

#### 4.4.2 核心流程

```
font_completions():
  book = world.book()                          # 必填 World 方法
  equation = is_in_equation_show_rule(leaf)    # 是否在 equation show 规则里
  对 book.families() 的每个 (family, iter):
    variants = 收集该家族所有 FontInfo
    is_math  = 任一 variant 的 flags 含 FontFlags::MATH
    detail   = summarize_font_family(variants) # 摘要（字重/字宽/斜体…）
    若 !equation 或 is_math：                  # 过滤
      str_completion(family, kind=Font, detail)

is_in_equation_show_rule(leaf):
  沿祖先向上，若某个 ShowRule 的 selector 是 FieldAccess 且字段名为 "equation" → true
```

过滤条件可以写成布尔式。设 \(E\) 为「在 equation show 规则内」、\(M_f\) 为「家族 \(f\) 含数学字体」，则家族 \(f\) 进入候选当且仅当：

\[
\text{show}(f) = \neg E \lor M_f
\`

即「不在 equation 规则内（全部放行），或者虽在内但本身是数学字体」。

#### 4.4.3 源码精读

`font_completions`（[src/complete.rs:1120-1135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1120-L1135)）：

```rust
fn font_completions(&mut self) {
    let book = self.world.book();
    let equation = is_in_equation_show_rule(self.leaf);
    for (family, iter) in book.families() {
        let variants: Vec<_> = iter.filter_map(|id| book.info(id)).collect();
        let is_math = variants.iter().any(|f| f.flags.contains(FontFlags::MATH));
        let detail = summarize_font_family(variants);
        if !equation || is_math {
            self.str_completion(
                family,
                Some(CompletionKind::Font),
                Some(detail.as_str()),
            );
        }
    }
}
```

`book.families()` 按「家族名」分组迭代，每组给出家族名和它下属字体 id 的迭代器。`is_math` 检查家族里是否**任一**变体带 `FontFlags::MATH` 标志——这是 OpenType 数学表的标记，由字体本身决定。`summarize_font_family`（utils.rs，承接 u2-l5/u3-l4）把多个变体的字重、字宽、斜体等压成一行 detail。

`is_in_equation_show_rule`（[src/complete.rs:1035-1048](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1035-L1048)）沿祖先链寻找：某个祖先是 `ShowRule`，且它的 `selector()` 是一个 `FieldAccess`、字段名为 `"equation"`（即 `math.equation`）。这与 `#show math.equation: set text(font: ...)` 的语法结构对应。

触发位置在 `param_value_completions`（[src/complete.rs:587-589](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L589)）：

```rust
if param.name() == Some("font") {
    ctx.font_completions();
}
```

也就是说，**任何一个名为 `font` 的具名参数的值都会触发字体补全**，不限于 `text` 函数。

#### 4.4.4 代码实践

**目标**：验证 equation 过滤。

**操作**：阅读 `test_autocomplete_fonts`（[src/complete.rs:1953-1967](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1953-L1967)）。

**观察**：

- `#text(font:)` 与 `#show link: set text(font: )`（非 equation）都同时包含 `"Libertinus Serif"`（普通字体）与 `"New Computer Modern Math"`（数学字体）。
- `#show math.equation: set text(font: )` 只包含 `"New Computer Modern Math"`、排除了 `"Libertinus Serif"`——因为现在 `equation` 为真，只有 `is_math` 的家族通过。
- 嵌套写法 `#show math.equation: it => { set text(font: ) ... }` 同样只补数学字体，证明 `is_in_equation_show_rule` 的祖先遍历能穿透 `it => { ... }` 的闭包结构。

**预期结果**：运行 `cargo test -p typst-ide test_autocomplete_fonts` 通过。注意 `New Computer Modern Math` 之所以是数学字体，是因为它的字体文件带 `FontFlags::MATH`；这是测试字体的固有属性，不是补全代码硬编码的。

#### 4.4.5 小练习与答案

**练习 1**：为什么字体补全用必填的 `book()` 而不是像包/文件那样用可选方法？

**答案**：因为字体是排版的必需资源，`World` 本来就必须提供字体簿（否则连一个字都排不出来）。既然 `book()` 总在，字体补全就直接复用它，无需再定义一个可选的 `IdeWorld` 方法。这与「补全是编译所需」 vs 「补全是 IDE 专属增强」的分工一致。

**练习 2**：若一个家族既有 Regular 又有 Math 变体（两份字体信息挂在同一家族名下），`is_math` 会怎样？

**答案**：`is_math` 用 `variants.iter().any(|f| f.flags.contains(FontFlags::MATH))`，只要**任一**变体带数学标志即为真。所以这个家族在 equation 规则里也会出现。这是合理的：用户可能用同一个家族名的数学版本来排方程。

### 4.5 label_completions 与 complete_open_labels：标签与参考文献

#### 4.5.1 概念说明

Typst 里有三种「引用标签」的写法，对应三种补全上下文：

1. **引用简写 `@x`**：在正文里用 `@` 引用一个标签，可指向文档元素标签（`<fig1>`），也可指向参考文献键（来自 `.bib`）。
2. **标签字面量 `<x>`**：用尖括号定义或选取一个标签。在代码里写 `(<la|` 这种半成品时也需要补全。这里只该补**文档元素标签**，不该补参考文献键（参考文献键不是用户用 `<>` 定义的）。
3. **参考文献 `#cite(<key>)` 或 `#bibliography`**：明确引用文献，只该补**参考文献键**。

`label_completions` 要在这三种上下文里给出**不同**的标签子集。它的关键工具是 `analyze_labels` 返回的 `split` 偏移——把一个连续数组切成「文档标签（下标 < split）」和「参考文献键（下标 ≥ split）」两段，再用 `skip`/`take` 选取。

`label_completions` 还**强依赖 `output`**（编译产物）：没有上次编译结果，就没有标签可补，直接返回（优雅降级）。这是它和字体/包/文件补全最大的不同。

#### 4.5.2 核心流程

```
complete_open_labels(ctx):               # 位于分发链第 2 关
  若 leaf 是错误节点且文本以 '<' 开头（半成品标签 "(<la|"）：
    from = leaf.offset() + 1             # 跳过 '<'
    label_completions()
    返回 true

label_completions():
  output = ctx.output?                   # 无编译产物 → 直接返回（降级）
  (labels, split) = analyze_labels(output)
  head   = text[..from]
  at     = head 以 '@' 结尾              # 引用简写
  open   = 非 at 且 非 以 '<' 结尾        # 需要补开尖括号
  close  = 非 at 且 after 不以 '>' 开头   # 需要补闭尖括号
  citation = 非 at 且 before_window(15) 含 "cite"

  (skip, take) =
      at        → (0, MAX)        # 全部（文档标签 + 参考文献）
    | citation  → (split, MAX)    # 仅参考文献键
    | otherwise → (0, split)      # 仅文档标签

  对 labels.skip(skip).take(take) 的每个 (label, detail)：
    apply = 按需拼 "<label>"（开/闭尖括号）
    推入 Completion { kind=Label, apply, label=label.resolve(), detail }
```

`skip`/`take` 的三种取值是本模块的核心。用区间记号表示，设标签全集下标域为 \([0, N)\)、文档标签为 \([0, split)\)、参考文献键为 \([split, N)\)，则：

\[
\text{选取区间} =
\begin{cases}
[0, N) & \text{上下文 }=\ \text{@ 引用} \\
[split, N) & \text{上下文 }=\ \text{citation} \\
[0, split) & \text{上下文 }=\ \text{< > 标签}
\end{cases}
\]

#### 4.5.3 源码精读

`complete_open_labels`（[src/complete.rs:248-257](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L248-L257)）只处理「代码里的半成品标签」：当 `leaf.kind().is_error()` 且文本以 `<` 开头时触发（语法分析器把未闭合的 `<la` 解析成错误节点）。它把 `from` 设为 `<` 之后，让补全只替换标签名本身。

`label_completions`（[src/complete.rs:1216-1249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1216-L1249)）：

```rust
fn label_completions(&mut self) {
    let Some(output) = self.output else { return };
    let (labels, split) = analyze_labels(output);

    let head = &self.text[..self.from];
    let at = head.ends_with('@');
    let open = !at && !head.ends_with('<');
    let close = !at && !self.after.starts_with('>');
    let citation = !at && self.before_window(15).contains("cite");

    let (skip, take) = if at {
        (0, usize::MAX)
    } else if citation {
        (split, usize::MAX)
    } else {
        (0, split)
    };

    for (label, detail) in labels.into_iter().skip(skip).take(take) {
        self.completions.push(Completion {
            kind: CompletionKind::Label,
            apply: (open || close).then(|| {
                eco_format!(
                    "{}{}{}",
                    if open { "<" } else { "" },
                    label.resolve(),
                    if close { ">" } else { "" }
                )
            }),
            label: label.resolve().as_str().into(),
            detail,
        });
    }
}
```

几个要点：

- **第一行就是降级**：`let Some(output) = self.output else { return };`。没有编译产物，整个函数什么都不做。
- **上下文判定**靠对 `text` 的字符串检查：`@` 看光标前缀、`<`/`>` 看是否需要补尖括号、`cite` 看光标前 15 个字符的窗口（`before_window(15)`，定义在 [src/complete.rs:1092-1094](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1092-L1094)）。
- **`apply` 的尖括号拼接**：`<` 标签上下文里，若用户还没打 `<`（`open`）就补开尖括号，若后面还没打 `>`（`close`）就补闭尖括号。`@` 引用不需要尖括号，`apply` 为 `None`（直接用 label）。
- `analyze_labels`（[src/analyze.rs:104-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104-L142)）里 `let split = output.len();` 紧跟在「文档元素标签」循环之后、`output.extend(BibliographyElem::keys(...))` 之前——所以 `split` 恰好等于文档标签的数量，把参考文献键隔在后面。

`label_completions` 还有一个重要的**第三处触发**：`cast_completions` 在类型为 `Label` 时也会调用它（[src/complete.rs:1397-1398](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1397-L1398)）。此外 `complete_markup` 里的 `@` 引用起点和已有 `RefMarker`（`@he|`）也调用它（[src/complete.rs:629-641](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L629-L641)）。

#### 4.5.4 代码实践

**目标**：验证 citation 场景用 `split` 只取参考文献键。

**操作**：阅读 `test_autocomplete_cite_function`（[src/complete.rs:1771-1786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1771-L1786)）。它先编译一个合法文件拿到 `doc`，再往末尾追加非法的 `#cite()`：

```rust
let mut world =
    TestWorld::new("#bibliography(\"works.bib\") <bib>").with_asset("works.bib");
let doc = typst::compile::<PagedDocument>(&world).output.ok();
let end = world.main.text().len();
world.main.edit(end..end, " #cite()");
test_with_doc(&world, -2, doc.as_ref(), true)
    .must_include(["netwok", "glacier-melt", "supplement"])
    .must_exclude(["bib"]);
```

**观察**：

- `#cite(` 的光标处，`before_window(15)` 含 `"cite"`，故 `citation` 为真，`skip = split`——只取参考文献键 `netwok`/`glacier-melt`/`supplement`（来自 `works.bib`）。
- 文档自身的标签 `<bib>`（一个文档元素标签，下标 < split）被排除，验证了 `split` 的切割作用。
- 注意它**先用合法文件编译出 `doc`，再追加非法的 `#cite()`**。注释解释了原因：若文件一开始就非法，`doc` 会是 `None`，`label_completions` 第一行就降级返回，拿不到任何标签。这模拟了真实编辑器「用上一次成功编译的产物来服务当前正在编辑（可能暂时非法）的代码」的场景。

**预期结果**：运行 `cargo test -p typst-ide test_autocomplete_cite_function` 通过。可对照 `test_autocomplete_ref_shorthand`（`@` 取全部）与 `test_autocomplete_ref_function`（`#ref(<)` 取文档标签）体会三种上下文的差异。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `@` 引用要取「文档标签 + 参考文献」全部，而 `<>` 标签只取文档标签？

**答案**：`@x` 是「引用」语义，在 Typst 里既可以引用文档元素标签（`<fig>`）也可以引用参考文献（来自 `.bib`），所以两种都要给。而 `<x>` 是「定义/选取标签字面量」语义，参考文献键不是用户用 `<>` 在源码里定义的东西，不应当作可定义的标签出现，所以只给文档标签。`split` 正是为了把这两类区分开而存在的。

**练习 2**：若 `autocomplete` 调用时没传 `output`（为 `None`），在 `@|` 处会发生什么？

**答案**：`complete_markup` 仍会匹配到 `@` 触发并调用 `label_completions`，但 `label_completions` 第一行 `let Some(output) = self.output else { return };` 直接返回，不产生任何标签候选。整个 `autocomplete` 最终返回一个空候选列表（`from` 已设好，但 `completions` 为空）。这就是「可选 output、优雅降级」。

### 4.6 raw_completions：raw 块语言标签补全

#### 4.6.1 概念说明

Typst 的 raw 块用 ` ```lang ` 开头来指定语法高亮语言，如 ` ```rust `、` ```python `。用户在敲完三个反引号后，自然想补全支持哪些语言。这就是 `raw_completions` 的职责。

它有几个与众不同的特点：

- **数据来源是静态的**：`typst::text::RawElem::languages()` 返回一个编译期已知的语法名注册表，不依赖 `world`、`output`、`packages`、`files` 中的任何一个。所以它是最「自给自足」的专项补全。
- **触发靠文本扫描而非语法节点**：raw 块的语言标签在用户还没敲完时往往是「半成品」，语法树难以稳定表达，于是 typst-ide 用 `Scanner` 直接扫文本里的 ` ``` `。

#### 4.6.2 核心流程

```
complete_markup(ctx):                     # 在 ``` 之后
  s = Scanner::new(text); s.jump(leaf.offset())
  若 s.eat_if("```")：                    # 当前位置起是三个反引号
    s.eat_while('`')                      # 吃掉更多反引号（如 ````）
    start = s.cursor()
    若 s.eat_if(is_id_start)：s.eat_while(is_id_continue)   # 吃掉已敲的语言名
    若 s.cursor() == cursor：             # 光标恰好在语言名位置
      from = start
      raw_completions()
    返回 true

raw_completions():
  对 RawElem::languages() 的每个 (name, tags)：
    lower = name.to_lowercase()
    若 tags 不含 lower：把 lower 加进 tags         # 总是允许小写形式
    tags.retain(|t| is_ident(t))                   # 只保留合法标识符
    若 tags 为空：跳过
    推入 Completion { kind=Constant, label=name, apply=tags[0],
                      detail=separated_list(tags, " or ") }
```

#### 4.6.3 源码精读

触发判定在 `complete_markup` 里（[src/complete.rs:662-676](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L662-L676)）：

```rust
let mut s = Scanner::new(ctx.text);
s.jump(ctx.leaf.offset());
if s.eat_if("```") {
    s.eat_while('`');
    let start = s.cursor();
    if s.eat_if(is_id_start) {
        s.eat_while(is_id_continue);
    }
    if s.cursor() == ctx.cursor {
        ctx.from = start;
        ctx.raw_completions();
    }
    return true;
}
```

注意它**不依赖 `explicit`**——只要光标在 ` ``` ` 之后、还在语言名位置（`s.cursor() == cursor`，即没有越过语言名继续打别的东西），就触发。`from = start` 把替换起点定在语言名开始处。

`raw_completions`（[src/complete.rs:1194-1213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1194-L1213)）：

```rust
fn raw_completions(&mut self) {
    for (name, mut tags) in RawElem::languages() {
        let lower = name.to_lowercase();
        if !tags.contains(&lower.as_str()) {
            tags.push(lower.as_str());
        }

        tags.retain(|tag| is_ident(tag));
        if tags.is_empty() {
            continue;
        }

        self.completions.push(Completion {
            kind: CompletionKind::Constant,
            label: name.into(),
            apply: Some(tags[0].into()),
            detail: Some(repr::separated_list(&tags, " or ").into()),
        });
    }
}
```

三个细节：

- **总是加入小写形式**：`name` 可能是 `C++` 这种含非标识符字符的名字，但 raw 块的语言标签必须是合法标识符。把 `lower`（小写名）加进 `tags`，保证至少有一个可用的标识符形式。
- **`is_ident` 过滤**：`retain` 丢掉所有不是合法标识符的 tag，比如含 `+`/`#` 的别名。过滤后若空则跳过该语言。
- **`apply` 用第一个 tag**：选中后实际写入的是 `tags[0]`（通常是规范的小写名），而 `label` 显示原始 `name`、`detail` 用 `separated_list` 列出所有可用别名（如 `rust or rs`）。这是 label/apply 分离的又一实例（u5-l2）。

#### 4.6.4 代码实践

**目标**：理解 raw 补全的数据来源与触发方式。

**操作**：在本地准备一个最小 Typst 文件 `main.typ`，内容为一行 ```` ``` ```` 加换行，把光标放在反引号后。若你集成了 typst-ide，调用 `autocomplete(world, output, &source, cursor, false)`。

**观察**：候选列表里应出现 `Constant` 类型的语言名（如 `rust`、`python`、`json`），每个的 `detail` 形如 `rust or rs`。注意它**不需要** `explicit=true` 也不需要 `output`。

**预期结果**：待本地验证（本讲不假设你已运行编辑器集成）。源码层面可确认：`RawElem::languages()` 的返回值决定了候选数量，typst-ide 只是做小写化、`is_ident` 过滤与别名拼接。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `raw_completions` 要 `tags.retain(|tag| is_ident(tag))`？

**答案**：因为 raw 块的语言标签在语法上必须是合法标识符（如 `rust`、`python`），而 `RawElem::languages()` 返回的某些别名可能含 `+`、`#` 等字符（如 `C++`、`C#`）。这些不能直接作为 raw 标签写入，所以要先过滤。过滤后若该语言没有任何合法标识符形式，就跳过不补。

**练习 2**：raw 补全的触发为什么用 `Scanner` 扫文本，而不是像字段补全那样靠 `ctx.leaf.kind()`？

**答案**：因为用户敲到 ` ```ru` 时，`ru` 这部分在语法树里往往还不是一个稳定的节点（raw 块可能尚未闭合、语言名是半成品），难以靠 `SyntaxKind` 可靠判定。而 ` ``` ` 这个前缀在纯文本层面很容易识别，所以用 `Scanner` 直接扫文本、再要求 `s.cursor() == cursor` 来锚定光标位置。这是一种「文本触发」与「语法触发」的权衡。

## 5. 综合实践

本综合实践把六类专项补全串起来，核心是亲手构造一个多文件、多资源的测试世界，并验证各补全的数据来源与触发位置。

### 实践目标

用 `TestWorld` 搭建一个含跨文件 import、图片资源、参考文献的场景，编写一个新测试，一次性观察 import 项、文件路径、字体、标签四类补全，并据此回答两个总问题：

1. `IdeWorld::packages()` 与 `files()` 分别被哪些补全消费？
2. `label_completions` 如何用 `split` 区分文档标签与参考文献（citation 场景）？

### 操作步骤

1. **搭建世界**（仿照 `test_autocomplete_value_filter` 与 `test_autocomplete_cite_function` 的写法）。在 `src/complete.rs` 的 `tests` 模块里新增：

   ```rust
   #[test]
   fn test_my_special_completions() {
       let world = TestWorld::new(
           "#import \"lib.typ\": \n#image(\"\")\n#text(font: )\nx<myfig>\n#bibliography(\"refs.bib\")",
       )
       .with_source("lib.typ", "#let alpha = 1; #let beta = 2")
       .with_asset_at("img/photo.jpg", "tiger.jpg")   // 复用已知 dev-asset
       .with_asset("refs.bib");
       let doc = typst::compile::<PagedDocument>(&world).output.ok();

       // (a) import 项：冒号后
       test_with_doc(&world, ("main.typ", 18), doc.as_ref(), true)
           .must_include(["alpha", "beta"]);

       // (b) image 路径：只要图片
       //    自行定位 #image("") 的引号内光标偏移，断言含 "img/photo.jpg"

       // (c) font 值：字体补全（非 equation，应含普通字体）
       //    自行定位 font: 后空格的光标偏移，断言含 "Libertinus Serif"
   }
   ```

2. **定位光标偏移**：用 `("main.typ", 正偏移)` 或负偏移（`-1` 为末尾）。负偏移的含义见 `cursor`（[src/tests.rs:238-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L238-L244)）。

3. **回答问题 1**：用 `Grep` 在 `src/complete.rs` 里搜索 `world.packages()` 与 `world.files()`，确认它们各自只出现在 `package_completions` 与 `file_completions` 里。再顺着调用链确认：`package_completions` 仅由 `complete_imports`（`@` 路径）调用；`file_completions_with_extensions` 由 `complete_imports`（`.typ`）与 `param_value_completions`（`path_completion`）调用。

4. **回答问题 2**：在 `test_autocomplete_cite_function` 的断言基础上，把 `#cite()` 改成 `@`，断言既含参考文献键（`netwok` 等）又含文档标签（`bib`）；再改成 `#ref(<)`，断言只含文档标签。三组对照即可说明 `split` 如何把 `[0, split)` 给 `<>`、把 `[split, N)` 给 citation、把 `[0, N)` 给 `@`。

### 需要观察的现象

- `with_source` 注册的 `lib.typ` 会出现在 `files()` 里，因此 `#image`/`#include` 能补到它；而它的导出项 `alpha`/`beta` 出现在 import 项补全里（说明 import 项走 `analyze_import`，与 `files()` 是两条独立路径）。
- `packages()` 没被这个测试用到（没有 `@` 路径），但你可以加一条 `#import "@"` 断言 `@preview/example:0.1.0`，确认它来自 `TestWorld::packages` 的硬编码（[src/tests.rs:127-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L127-L141)）。

### 预期结果

- 运行 `cargo test -p typst-ide test_my_special_completions` 通过。
- 问题 1 答案：`packages()` 仅被 `package_completions`（进而仅被 `complete_imports` 的 `@` 分支）消费；`files()` 仅被 `file_completions`（进而被 `complete_imports` 的文件分支与 `param_value_completions` 经 `path_completion`）消费。两者均为可选 `IdeWorld` 方法，缺失则相应补全为空。
- 问题 2 答案：`analyze_labels` 返回的 `split` 等于文档标签数量；`label_completions` 用 `skip`/`take` 三选一——`@` 取 `[0, N)`、citation 取 `[split, N)`（`skip = split`）、`<>` 取 `[0, split)`（`take = split`）——从而在三种上下文给出不同子集。

## 6. 本讲小结

- typst-ide 的专项补全按「数据来源」可分为五类：包（可选 `IdeWorld::packages()`）、文件路径（可选 `files()`）、字体（必填 `World::book()`）、标签（可选 `output`）、raw 语言（静态 `RawElem::languages()`）。
- `complete_imports` 用三段式匹配覆盖「路径字符串 / 冒号后导入项 / 半成品导入项」三种位置；路径字符串再按是否以 `@` 开头分流给包补全或（限 `.typ` 的）文件补全。
- `package_completions` 是 `packages()` 的唯一消费者，按 `(namespace, name, Reverse(version))` 排序，并在非「全版本」模式下按 `(namespace, name)` 去重保留最新版——排序是去重正确性的前提。
- `file_completions` 以**当前文件目录**为基准计算相对路径、排除自身；`path_completion` 按 `(func.name(), param.name())` 查表决定扩展名过滤，`(_, "path") => &[]` 表示放行全部。
- `font_completions` 用必填的 `book()`，在 equation show 规则内用 `FontFlags::MATH` 过滤为「仅数学字体」；触发位置是任何名为 `font` 的具名参数值。
- `label_completions` 强依赖 `output`（无则降级），用 `analyze_labels` 的 `split` 把文档标签与参考文献键切开，靠 `@`/`citation`/`<>` 三种上下文的 `skip`/`take` 选取不同子集；`complete_open_labels` 处理代码里 `(<la|` 半成品标签。
- `raw_completions` 是唯一不依赖任何运行时数据的专项补全，数据来自 `RawElem::languages()`，用 `Scanner` 文本扫描触发，并对语言名做小写化与 `is_ident` 过滤。
- 同一个生成器（如文件/字体/标签补全）会被**多个触发位置**复用：文件补全兼由 import 路径与参数值触发；标签补全兼由 `<`、`@`、`Label` 类型 cast 触发。

## 7. 下一步学习建议

本讲把「外部数据驱动的专项补全」讲完了。接下来建议：

- **u6-l4（scope_completions 与类型驱动补全）**：补全引擎里最「智能」的一环——`scope_completions` 如何合并局部命名与全局作用域、`cast_completions` 如何按 `CastInfo` 递归展开、`check_value_recursively` 如何让「含目标类型的容器」也参与补全。它会用到本讲提到的 `label_completions`（作为 `Label` 类型 cast 的下游）。
- **u6-l5（apply 片段与 BracketMode）**：本讲多次出现的 `value_completion_full` / `str_completion` 的「下半段」——`apply` 文本如何根据值类型与上下文生成括号形式，以及 `BracketMode` 如何决定 `()`/`[]` 与换行缩进。
- **回看 u8-l2（集成实践）**：把本讲的「可选增强 + 优雅降级」放到真实 LSP 集成里——为支持补全，你需要实现 `IdeWorld` 的哪些方法、缓存哪一类编译产物（`output`）。
- **源码延伸**：若想了解 `RawElem::languages()` 这张语言表如何被过滤与重排，可结合仓库根目录近期关于 raw syntaxes 的提交（如 `ad8e9bcec`、`9a1d84e94`）阅读 typst 主 crate 的 raw 实现。
