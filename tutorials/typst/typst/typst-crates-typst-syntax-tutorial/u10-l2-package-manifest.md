# 包清单解析

## 1. 本讲目标

学完本讲，你应当能够：

- 说清一份 Typst 包清单文件（`typst.toml`）是如何被解析成 `PackageManifest` 的，以及 `[package]` / `[template]` / `[tool]` 三个 section 各自映射到哪个结构体。
- 理解 `unknown_fields` 这一「收集未知字段」机制的设计动机：为什么 Typst 不直接忽略拼写错误的字段，而是把它们收集起来供前向兼容校验。
- 看懂 `PackageSpec`（`@namespace/name:version`）与 `PackageVersion`、`VersionBound` 的字符串格式、解析规则与版本比较语义。
- 能够自己写代码：用 `toml::from_str` 解析一份清单，从 `[tool.xxx]` 取出自定义工具配置并反序列化。

## 2. 前置知识

本讲只依赖 u1-l1 的全局认知。在进入源码前，先建立三个直觉。

**什么是包（package）。** Typst 允许把可复用的代码、模板打包发布，其他项目用 `#import "@preview/cetz:0.3.0": canvas` 这样的语法引入。这里 `@preview/cetz:0.3.0` 就是一个**包标识**（package spec），`preview` 是命名空间，`cetz` 是名字，`0.3.0` 是版本。每个包根目录下必须有一份 `typst.toml` 清单文件，描述包的元数据。

**什么是 TOML + serde。** TOML 是一种类似 INI 的配置文件格式（用 `[section]` 分节、`key = value` 赋值）。Rust 生态里，`toml` crate 能把 TOML 文本反序列化（deserialize）成结构体，`serde` 框架通过 `#[derive(Deserialize)]` 与各种 `#[serde(...)]` 属性控制字段如何映射。本讲大量出现这些属性，你只需要记住几个关键含义：

| 属性 | 作用 |
|------|------|
| `#[serde(default)]` | 字段缺失时用默认值，不报错 |
| `#[serde(flatten)]` | 把「剩余的/嵌套的」键值平铺进当前结构的一个 Map 字段 |
| `#[serde(skip_serializing_if = "...")]` | 序列化时若条件成立则跳过该字段 |
| `#[derive(Deserialize, Serialize)]` | 自动生成反序列化/序列化代码 |

**为什么清单解析放在 `typst-syntax` 里。** 看似「解析 TOML 配置」与「解析 Typst 源码」无关，但两者共享同一套基础设施：包名/命名空间的合法性校验复用了词法器（lexer）里的 `is_ident`（见 u3-l4）；包标识 `@namespace/name:version` 的解析复用了 `unscanny::Scanner` 游标。所以 `package.rs` 是 `typst-syntax` 的「编外成员」，靠 `pub mod package;` 整体公开（见 u1-l3 模块地图）。

## 3. 本讲源码地图

本讲只涉及一个源文件，外加一个跨 crate 的调用点用于理解真实用法。

| 文件 | 作用 |
|------|------|
| [src/package.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs) | 全部内容：清单结构体、包标识解析、版本号与版本约束、校验逻辑、内联测试 |
| crates/typst-eval/src/import.rs | 真实调用方：读 `typst.toml` → `toml::from_str` → `validate` |

`src/package.rs` 内部的逻辑分层：

```
typst.toml 文本
   │  toml::from_str (serde 反序列化)
   ▼
PackageManifest ──┬── package:  PackageInfo     ← [package]
                  ├── template: Option<TemplateInfo> ← [template]
                  ├── tool:     ToolInfo        ← [tool]
                  └── unknown_fields: UnknownFields ← 顶层未知键

PackageSpec "@preview/cetz:0.3.0"  ←─ FromStr + Scanner + is_ident
   │  manifest.validate(&spec)
   ▼
校验 name / version / compiler 版本要求
```

## 4. 核心概念与源码讲解

### 4.1 PackageManifest：清单的总结构

#### 4.1.1 概念说明

`PackageManifest` 是一份 `typst.toml` 反序列化后的**顶层 Rust 结构**。它把整份清单拆成三大节，外加一个「兜底口袋」收集所有未被识别的字段。设计上有两点值得注意：

- **`template` 是可选的**：只有当这个包是一个「模板包」（用 `typst init` 初始化新项目时拷贝文件用的）才需要 `[template]` 节，所以它是 `Option`。
- **`tool` 永远存在但常为空**：`#[serde(default)]` 让缺省时得到一个空的 `ToolInfo`，方便第三方工具挂载自己的配置。

#### 4.1.2 核心流程

一份清单的解析流程是：

1. `toml::from_str::<PackageManifest>(text)` 触发 serde 反序列化。
2. serde 读到 `[package]` 节 → 反序列化成 `PackageInfo`，填进 `package` 字段。
3. 读到 `[template]` 节（若有）→ 反序列化成 `TemplateInfo`，包进 `Some(...)` 填进 `template`。
4. 读到 `[tool]` 节（若有）→ `#[serde(flatten)]` 把其下所有子表收进 `tool.sections`。
5. 读到任何**顶层**未知节（如 `[unknown]`）→ 被 `#[serde(flatten)]` 收进 `unknown_fields`。
6. 任何**必需**字段缺失或类型不符 → 返回 `Err`。

#### 4.1.3 源码精读

`PackageManifest` 的定义只有四个字段，但每个字段的 serde 属性都承担了一项职责：

[文件路径:L21-L34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L21-L34) —— `PackageManifest` 结构体：`package` 是必需节；`template` 带 `default` + `skip_serializing_if` 故缺省即为 `None`；`tool` 带 `default` 故缺省即空；`unknown_fields` 带 `flatten` + `skip_serializing`，前者把顶层未知键收进 Map，后者保证序列化时不回吐这些键（不破坏「未知字段」的语义）。

`UnknownFields` 是这一切的关键类型别名：

[文件路径:L14-L16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L14-L16) —— `UnknownFields = BTreeMap<EcoString, IgnoredAny>`。`IgnoredAny` 是 serde 内置类型：它能匹配任意值但在反序列化时**完全丢弃**值内容，只保留键名。配合 `#[serde(flatten)]`，效果就是「把所有没对应字段名的键，连同它们的名字一起记下来，但不管值是什么」。

#### 4.1.4 代码实践

**实践目标**：亲手跑通最小清单解析，确认字段映射。

**操作步骤**：

1. 在本仓库运行已有测试，确认环境正常：
   ```bash
   cargo test -p typst-syntax package::
   ```
2. 阅读内联测试 `minimal_manifest`，它是最小合法清单的「黄金样本」：
   [文件路径:L575-L597](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L575-L597) —— 用只含 `[package]` 三字段的 TOML 反序列化，断言结果与手写的 `PackageManifest { package: PackageInfo::new(...), template: None, tool: ToolInfo { sections: ... }, unknown_fields: ... }` 相等。

**需要观察的现象**：测试通过，说明缺省的 `template`/`tool`/`unknown_fields` 都被 serde 的 `default` 正确填上了空值。

**预期结果**：`cargo test` 报告 `minimal_manifest ... ok`。

### 4.2 PackageInfo / TemplateInfo / ToolInfo：三个 section

#### 4.2.1 概念说明

三个子结构体分别承载清单的三个节，职责分明：

- **`PackageInfo`**（`[package]`）：包的核心元数据——名字、版本、入口文件是必需的；作者、许可证、描述、主页、仓库、关键词、分类、学科、最低编译器版本、排除文件列表都是可选的。
- **`TemplateInfo`**（`[template]`）：模板专用——模板目录路径、编译入口、缩略图。
- **`ToolInfo`**（`[tool]`）：**第三方工具配置区**。这是最特别的一个：Typst 自己不解释 `[tool]` 下面的内容，而是把每个子表原样存起来，交给具体工具自己去反序列化。

#### 4.2.2 核心流程

`ToolInfo` 的设计是本节重点。它不用具名字段，而用一个扁平的 Map：

```
[tool.my-tool]      ┐
key = "value"       ├─  tool.sections: BTreeMap {
[tool.another]      │       "my-tool" => Table{ key: "value" },
foo = 1             │       "another" => Table{ foo: 1 },
                    ┘    }
```

`#[serde(flatten)]` 让 serde 把 `[tool]` 下的每个**子表**当作一个键值对塞进 `sections`。注意：值类型是 `toml::Table`（即 `BTreeMap<String, toml::Value>`），所以 `[tool]` 下的内容**必须是表**（即必须写成 `[tool.xxx]` 形式），直接写 `key = "str"` 会反序列化失败——这正是内联测试 `tool_section` 验证的行为。

#### 4.2.3 源码精读

`PackageInfo` 是典型的「少数必需 + 多数可选」结构，靠 `#[serde(default, skip_serializing_if = ...)]` 区分：

[文件路径:L99-L143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L99-L143) —— `PackageInfo`：`name`/`version`/`entrypoint` 无 `default`（必需）；`authors`/`keywords`/`categories`/`disciplines`/`exclude` 是 `Vec`，缺省空且序列化时空则跳过；`license`/`description`/`homepage`/`repository`/`compiler` 是 `Option`，缺省 `None`。末尾同样挂一个 `unknown_fields`，意味着**`[package]` 节内部的**未知字段也会被收集。

`TemplateInfo` 结构更简单：

[文件路径:L79-L94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L79-L94) —— `TemplateInfo`：`path`/`entrypoint` 必需，`thumbnail` 可选，再挂 `unknown_fields`。

`ToolInfo` 是设计亮点，它的文档注释里直接嵌了一个**可运行的 doctest**，示范第三方工具如何取自己的配置：

[文件路径:L36-L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L36-L74) —— `ToolInfo`：`sections: BTreeMap<EcoString, toml::Table>` 带 `#[serde(flatten)]`。文档示例展示了两步取值法：先 `manifest.tool.sections.remove("my-tool")` 取出原始 `toml::Table`，再用 `MyTool::deserialize(my_tool)` 反序列化成自己的结构体。这种「先收全、后各自解释」的设计让 `[tool]` 成为安全的第三方扩展点，互不干扰。

注意三个结构体（`PackageManifest`、`PackageInfo`、`TemplateInfo`）**都**带 `unknown_fields`，所以未知字段在三个层级都会被收集，不只在顶层。

#### 4.2.4 代码实践

**实践目标**：解析一份含 `[tool.my-tool]` 的清单，取出并反序列化自定义工具配置。

**操作步骤**：

1. 阅读内联测试 `tool_section`，它正是本实践的标准答案：
   [文件路径:L599-L640](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L599-L640) —— 先断言 `[tool]` 下放非表值（`not-table = "str"`）会报错；再示范合法用法：解析含 `[tool.my-tool] key = "value"` 的清单，`remove("my-tool")` 取出后用 `MyTool::deserialize` 反序列化，断言得到 `MyTool { key: "value" }`。
2. 在仓库外新建一个小项目（或在一个临时测试里）仿照写一份：

   ```rust
   // 示例代码（仿照 package.rs 的 doctest）
   use serde::{Deserialize, Serialize};
   use ecow::EcoString;
   use typst_syntax::package::PackageManifest;

   #[derive(Debug, PartialEq, Serialize, Deserialize)]
   struct MyTool { key: EcoString }

   let mut manifest: PackageManifest = toml::from_str(r#"
       [package]
       name = "package"
       version = "0.1.0"
       entrypoint = "src/lib.typ"

       [tool.my-tool]
       key = "value"
   "#).unwrap();

   let raw = manifest.tool.sections.remove("my-tool").unwrap();
   let my_tool = MyTool::deserialize(raw).unwrap();
   assert_eq!(my_tool, MyTool { key: "value".into() });
   ```

   > 说明：`typst_syntax::package` 是 `pub mod`，故 `PackageManifest` 等类型可被外部直接引用（见 u1-l3）。

**需要观察的现象**：`sections.remove("my-tool")` 返回 `Some(...)`；`MyTool::deserialize` 成功。

**预期结果**：断言通过，证明 `[tool.my-tool]` 的内容被原样保留并可二次反序列化。

> 提示：若在独立项目里运行，需在 `Cargo.toml` 加 `typst-syntax`、`toml`、`serde`、`ecow` 依赖；在本仓库内可直接放进 `package.rs` 的 `#[cfg(test)] mod tests` 临时验证。

#### 4.2.5 小练习与答案

**练习 1**：若把上面清单里的 `[tool.my-tool]` 改成 `[tool]` 节下直接写 `my-tool = "not-a-table"`，反序列化会怎样？为什么？

**参考答案**：会报错。`ToolInfo.sections` 的值类型是 `toml::Table`，serde 要求 `[tool]` 下每个键的值都是表；字符串 `"not-a-table"` 无法反序列化成 `Table`，故返回 `Err`。这正是 `tool_section` 测试前半段验证的。

**练习 2**：`PackageInfo` 有三个**必需**字段，少了任意一个都会反序列化失败。指出它们是哪三个。

**参考答案**：`name`、`version`、`entrypoint`（这三个字段没有 `#[serde(default)]`）。

### 4.3 UnknownFields：前向兼容的设计

#### 4.3.1 概念说明

「未知字段」机制解决的是**前向兼容与拼写检查**的矛盾。考虑两种朴素做法的缺陷：

- **严格模式**（遇到未知字段就报错）：Typst 升级新增字段后，旧版本无法读取新清单——前向兼容差。
- **完全忽略**（serde 默认对未知字段静默丢弃）：用户把 `entrypoint` 拼成 `entrypiont`（拼写错误）时，解析「成功」但行为错误，且没有任何提示。

`UnknownFields` 取了折中：**不报错（保证前向兼容），但把未知键的名字记下来**，让上层（CLI、LSP）可以选择性地检查并给出「是否拼写错误」的警告。`IgnoredAny` 只丢值、留键，正是为了这个目的——不需要值，只需要知道「有哪些键没被认出」。

#### 4.3.2 核心流程

```
TOML 解析中遇到一个键 K
   │
   ├─ K 匹配某具名字段 → 正常反序列化
   └─ K 不匹配任何字段
         │  (该结构体有 #[serde(flatten)] unknown_fields)
         ▼
      插入 BTreeMap: unknown_fields[K] = IgnoredAny
      （值被丢弃，键被保留，按 EcoString 字典序排序）
```

由于用的是 `BTreeMap`，键自动按字典序排列，便于稳定地遍历与展示。三个层级（顶层、`[package]` 内、`[template]` 内）各自独立收集。

#### 4.3.3 源码精读

类型定义与挂载方式：

[文件路径:L14-L16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L14-L16) —— `UnknownFields = BTreeMap<EcoString, IgnoredAny>`。

[文件路径:L31-L33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L31-L33) —— `PackageManifest` 上 `#[serde(flatten, skip_serializing)]` 的 `unknown_fields` 字段。`flatten` 负责「收」，`skip_serializing` 负责「不回吐」——保证序列化输出里不会出现这些未知键，维持语义一致。

内联测试 `unknown_keys` 验证了收集行为：

[文件路径:L642-L657](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L642-L657) —— 一份含 `[unknown]` 空节的清单解析后，`manifest.unknown_fields.contains_key("unknown")` 为真。

**一个重要细节**：本文件里的 `validate()` 方法（见 4.4 节）**并不**检查 `unknown_fields`。也就是说，`typst-syntax` 只负责「收集」，是否对未知字段发出警告由调用方决定。`unknown_fields` 字段的文档注释明确写着「this can be used for validation」（可用于校验），把检查权留给上层。

#### 4.3.4 代码实践

**实践目标**：观察未知字段被收集、已知字段被正常解析，二者互不影响。

**操作步骤**：

1. 在临时测试中加入一份「合法字段 + 拼错的字段 + 完全未知的节」混合的清单：
   ```toml
   [package]
   name = "demo"
   version = "0.1.0"
   entrypoint = "lib.typ"
   licence = "MIT"      # 拼错：应为 license
   [my-extra-section]
   ```
2. 用 `toml::from_str::<PackageManifest>` 解析，打印 `manifest.package.unknown_fields` 与 `manifest.unknown_fields`。

**需要观察的现象**：解析成功（不报错）；`package.unknown_fields` 含 `licence`（拼错的键），顶层 `unknown_fields` 含 `my-extra-section`；而 `package.license` 是 `None`（因为正确字段名没出现）。

**预期结果**：拼写错误不会导致解析失败，但会被记录在对应层级的 `unknown_fields` 里，可供上层提示用户。

### 4.4 PackageSpec / PackageVersion / VersionBound：包标识与版本

#### 4.4.1 概念说明

这一组类型解决两个不同的问题，不要混淆：

- **`PackageSpec`**：标识「哪一个包」，字符串格式 `@namespace/name:version`。它来自源码里的 import 语法（如 `#import "@preview/cetz:0.3.0"`），需要从字符串解析。`VersionlessPackageSpec` 是去掉版本号的变体。
- **`PackageVersion` / `VersionBound`**：版本号本身。`PackageVersion` 是精确的三段版本 `major.minor.patch`（如 `0.3.0`）；`VersionBound` 是「版本约束」，minor/patch 可省略（如 `1`、`1.2`、`1.2.3` 都合法），用于清单里声明「最低编译器版本」。

`PackageSpec` 不直接出现在 `typst.toml` 里，而是出现在 import 语句里；`PackageManifest::validate(&spec)` 用来核对「清单声明的 name/version 是否与 import 时请求的 spec 一致」。

#### 4.4.2 核心流程

`PackageSpec` 的字符串解析把 `@preview/cetz:0.3.0` 拆成三段，用 `unscanny::Scanner` 游标逐步吃掉分隔符：

```
"@preview/cetz:0.3.0"
   │
   ├─ parse_namespace: 吃 '@' → eat_until('/') 得 "preview" → 校验 is_ident
   ├─ parse_name:      吃 '/' → eat_until(':') 得 "cetz"    → 校验 is_ident
   └─ parse_version:   吃 ':' → 剩余 "0.3.0" → PackageVersion::from_str
```

`PackageVersion::from_str` 用 `split('.')` 切成三段，每段必须是合法 `u32`，且**恰好三段**（多一段也报错）。`VersionBound::from_str` 结构相同，但 minor/patch 可缺省。

版本比较（`matches_*` 系列）有一个容易踩坑的语义：`matches_gt` / `matches_lt` 只有在「约束里出现的某一段确实被超过」时才返回真。例如版本 `1.1.1` 对约束 `1` 做 `matches_gt` 返回 **false**——因为约束只到 major 段，而 major 相等，没有「约束里出现的段」被超过。这一点在 doc 注释里写明。

#### 4.4.3 源码精读

`PackageSpec` 结构体只有三个公开字段，靠手写的 `FromStr` 解析字符串：

[文件路径:L224-L233](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L224-L233) —— `PackageSpec { namespace, name, version }`。

[文件路径:L244-L254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L244-L254) —— `FromStr for PackageSpec`：依次调用 `parse_namespace`/`parse_name`/`parse_version` 三个助手。注意它**复用** `crate::is_ident`（来自 lexer.rs，u3-l4）校验命名空间与名字的合法性——包名规则与 Typst 标识符规则一致（首字符 `is_id_start`、续字符 `is_id_continue`，允许 `-`）。

[文件路径:L314-L327](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L314-L327) —— `parse_namespace`：必须以 `@` 开头（否则报「must start with '@'」），`eat_until('/')` 取命名空间，空或非合法标识符都报错。

[文件路径:L329-L351](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L329-L351) —— `parse_name` 与 `parse_version`：前者 `eat_until(':')` 取名字并校验；后者吃掉 `:` 后把剩余字符串交给 `PackageVersion::from_str`。

`PackageVersion` 用自定义的 serde 实现把字符串 `"0.3.0"` 转成三个 `u32`：

[文件路径:L353-L362](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L353-L362) —— `PackageVersion { major, minor, patch }`，三个 `u32`。

[文件路径:L432-L455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L432-L455) —— `FromStr for PackageVersion`：闭包 `next` 取每一段并 `parse::<u32>`，缺段报「missing {kind} version」，多了第四段报「unexpected fourth component」。

[文件路径:L469-L480](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L469-L480) —— 自定义 `Deserialize`：先把值当 `EcoString` 反序列化（即 TOML 里版本写成**字符串** `"0.1.0"`），再 `string.parse()`。这就是为什么清单里版本要带引号。

版本约束 `VersionBound` 与比较方法：

[文件路径:L483-L491](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L483-L491) —— `VersionBound { major, minor: Option<u32>, patch: Option<u32> }`，patch 只有在 minor 也存在时才有意义。

[文件路径:L375-L399](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L375-L399) —— `matches_eq` 用 `is_none_or` 处理可缺省段；`matches_gt` 在某段相等时下钻到更细段，约束段耗尽（`None`）即返回 false（即「没有约束段被超过」）。这套语义对应内联测试 `version_version_match`：

[文件路径:L558-L573](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L558-L573) —— 用 `1.1.1` 对各种约束验证 eq/gt/lt 的真假，正好覆盖了上述语义边界。

最后，`validate` 把清单与 spec 关联起来：

[文件路径:L156-L183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L156-L183) —— `PackageManifest::validate(&spec)`：依次校验 name 一致、version 一致；若清单声明了 `compiler`（一个 `VersionBound`），则用 `PackageVersion::compiler()` 取当前编译器版本，要求 `matches_ge(&required)`，否则报「requires Typst {required} or newer」。

真实的调用链在 `typst-eval`：读 `typst.toml` → `toml::from_str` → `manifest.validate(&spec)`：

[文件路径:L271-L274](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/import.rs#L271-L274) —— `resolve_package` 函数里，解析失败报「package manifest is malformed」，再 `manifest.validate(&spec)` 核对一致性。

#### 4.4.4 代码实践

**实践目标**：验证 `PackageSpec` 字符串解析与版本比较的边界语义。

**操作步骤**：

1. 阅读并运行测试：
   ```bash
   cargo test -p typst-syntax package::tests
   ```
2. 在临时测试里手写几例 `PackageSpec::from_str` 与 `PackageVersion` 比较：

   ```rust
   // 示例代码
   use std::str::FromStr;
   use typst_syntax::package::{PackageSpec, PackageVersion, VersionBound};

   let spec = PackageSpec::from_str("@preview/cetz:0.3.0").unwrap();
   assert_eq!(spec.namespace, "preview");
   assert_eq!(spec.name, "cetz");
   assert_eq!(format!("{}", spec.version), "0.3.0");

   // 边界语义：1.1.1 对约束 "1" 做 gt 为 false
   let v = PackageVersion::from_str("1.1.1").unwrap();
   assert!(!v.matches_gt(&VersionBound::from_str("1").unwrap()));
   // 但对约束 "1.0" 做 gt 为 true
   assert!(v.matches_gt(&VersionBound::from_str("1.0").unwrap()));

   // 非法 spec：缺少 '@'
   assert!(PackageSpec::from_str("preview/cetz:0.3.0").is_err());
   ```

**需要观察的现象**：合法 spec 三段被正确拆分；版本比较的边界与 doc 注释一致；缺 `@` 的字符串解析失败。

**预期结果**：所有断言通过，与内联测试 `version_version_match` 的结论吻合。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `PackageVersion` 在清单里要写成带引号的字符串 `version = "0.1.0"`，而不是 `version = 0.1.0`？

**参考答案**：因为 TOML 里 `0.1.0` 不是合法字面量（数字不能有两个点），且 `PackageVersion` 的自定义 `Deserialize` 先把值当 `EcoString` 读再 `parse`（见 L475-L480）。必须写成字符串 `"0.1.0"` 才能被当作字符串反序列化成功。

**练习 2**：`PackageVersion::from_str("1.2")` 会成功吗？为什么？

**参考答案**：会失败。`PackageVersion` 要求恰好三段 `major.minor.patch`，`next("patch")` 在 `parts.next()` 返回 `None` 时报「version number is missing patch version」。只有 `VersionBound` 才允许省略 minor/patch。

**练习 3**：`PackageSpec` 的 `namespace` 和 `name` 用什么规则校验合法性？这条规则与词法器有什么关系？

**参考答案**：用 `crate::is_ident`（来自 lexer.rs，见 u3-l4）校验——首字符满足 `is_id_start`、其余满足 `is_id_continue`（允许 `-` 续接）。这说明包名/命名空间与 Typst 标识符共用同一套字符口径，体现了 `package.rs` 复用 `typst-syntax` 基础设施的设计。

## 5. 综合实践

把本讲全部知识串起来，模拟一次「迷你包加载校验」：

1. 准备两份数据：
   - 一个 import 请求字符串 `"@preview/my-pkg:1.2.0"`，用 `PackageSpec::from_str` 解析成 `spec`。
   - 一份 `typst.toml` 文本（写成字符串字面量），包含：完整的 `[package]`（name 设为 `my-pkg`、version 设为 `"1.2.0"`、entrypoint、再加一个拼错的 `licence` 字段）、一个 `[tool.linter]` 子表（带一两个自定义键）、一个未知的 `[extra]` 顶层节。
2. 用 `toml::from_str::<PackageManifest>` 解析清单文本。
3. 检查：
   - `manifest.validate(&spec)` 成功（name/version 都对得上）。
   - `manifest.package.unknown_fields` 含 `licence`（拼写错误的字段被收集）。
   - `manifest.unknown_fields` 含 `extra`（顶层未知节被收集）。
   - `manifest.tool.sections["linter"]` 能被你自定义的 `Linter` 结构体 `deserialize` 成功。
4. 故意把清单里的 `version` 改成 `"1.2.1"`，再调用 `validate(&spec)`，观察它返回「mismatched version」错误。

这个任务同时用到：清单结构（4.1）、三 section 映射（4.2）、unknown_fields 收集（4.3）、PackageSpec 解析与 validate（4.4）。若某些步骤无法在独立项目跑通，可在本仓库 `package.rs` 的 `#[cfg(test)] mod tests` 里临时添加测试函数验证，标注「待本地验证」。

## 6. 本讲小结

- `PackageManifest` 是 `typst.toml` 的顶层 Rust 映射，含 `package`（必需）、`template`（可选）、`tool`（默认空）、`unknown_fields`（收集顶层未知键）四个字段。
- 三个 section 各有分工：`PackageInfo` 承载包元数据、`TemplateInfo` 描述模板、`ToolInfo` 用 `#[serde(flatten)]` 的 `BTreeMap<EcoString, toml::Table>` 作为第三方工具的安全扩展点。
- `UnknownFields = BTreeMap<EcoString, IgnoredAny>` 通过 `IgnoredAny`「丢值留键」，在前向兼容（不报错）与拼写检查（留痕供上层提示）间取折中，且在顶层、`[package]`、`[template]` 三个层级各自收集。
- `PackageSpec` 用 `@namespace/name:version` 格式，靠 `unscanny::Scanner` + `is_ident` 解析；`PackageVersion` 是精确三段版本，`VersionBound` 允许省略 minor/patch。
- `PackageVersion` 的自定义 serde 把版本当字符串读再 parse，故清单里版本必须带引号；`matches_gt`/`matches_lt` 只有「约束里出现的段被超过」才返回真。
- `validate(&spec)` 核对 name/version 一致性与最低编译器版本要求，是 `typst-eval` 加载包时的真实校验步骤。

## 7. 下一步学习建议

- **顺读 u10-l3《语法高亮》**：本系列最后一篇讲 `highlight.rs`，与 `package.rs` 一样是 `typst-syntax` 的「消费侧」模块，看它如何把 CST 映射成 TextMate 标签。
- **跨 crate 阅读 `typst-eval/src/import.rs` 的 `resolve_package`**：完整跟踪「import `@preview/...` → 找包 → 读 `typst.toml` → `validate` → 求值入口」全链路，理解本讲的清单解析在编译器里的位置。
- **回顾 u3-l4《字符判定工具函数》**：本讲多次出现的 `is_ident` 正来自那里，对照阅读可加深「包名与 Typst 标识符共用同一字符口径」的理解。
