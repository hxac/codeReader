# init 项目脚手架

## 1. 本讲目标

本讲讲解 `typst init` 命令——它如何从一个「模板包」生成一个可直接写作的新项目目录。学完后你应当能够：

- 说清 `init` 命令从接收命令行参数到生成项目的完整六步流程；
- 理解 `PackageSpec` 的两种解析路径（带版本 / 不带版本）以及版本号是如何被自动推断出来的；
- 读懂 `typst.toml` 清单是如何被读取、反序列化、并经 `validate` 校验的，以及 `TemplateInfo` 在其中扮演的角色；
- 理解 `scaffold_project` 如何用 `fs_extra` 把模板子目录复制成新项目，以及它做了哪些「已存在」保护；
- 能够在本地从 `@preview` 模板或手写的 `@local` 模板创建项目，并把每一步现象对照源码解释清楚。

## 2. 前置知识

本讲承接 [u3-l2 包存储与解析](u3-l2-package-storage.md)，需要你先建立以下认知（本讲不重复，直接使用）：

- **包的三级存储**：`SystemPackages` 按 **data 目录（本地）→ cache 目录（缓存）→ Universe 在线下载** 的优先级短路查找；`obtain(spec)` 是查找入口，每级命中即返回，仅 `@preview` 命名空间会联网。
- **目录约定**：`FsPackages` 约定「namespace/name/version」三层目录结构；`obtain` 只判目录存在性，不校验内容。
- **`packages::system()` 薄壳**：typst-cli 侧只有一个把 `PackageArgs` 装配成 `SystemPackages` 的薄函数，被编译链路（`SystemWorld`）和脚手架链路（`init`）复用。
- **关键类型**：`SystemPackages`、`FsPackages`、`UniversePackages`、`obtain`、`PackageArgs`。

本讲要补充的新类型来自核心库 `typst-syntax::package`：

- **`PackageSpec`**：完整包标识，形如 `@preview/charged-ieee:0.1.0`，含 `namespace`、`name`、`version` 三段。
- **`VersionlessPackageSpec`**：不带版本的包标识，形如 `@preview/charged-ieee`，可经 `.at(version)` 补全成完整 `PackageSpec`。
- **`PackageManifest`**：`typst.toml` 反序列化后的结构，含 `[package]` 信息与可选的 `[template]` 模板信息。
- **`TemplateInfo`**：`typst.toml` 中 `[template]` 段的描述，记录「哪个子目录要被复制」「入口文件是哪个」。

如果你还不熟悉这些，请先阅读 u3-l2。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `crates/typst-cli/src/init.rs` | `init` 命令的全部实现：主流程、清单解析、脚手架复制、成功提示。本讲的主角。 |
| `crates/typst-cli/src/packages.rs` | 薄壳函数 `system()`，把 `PackageArgs` 装配成 `SystemPackages`，供 `init` 复用。 |
| `crates/typst-cli/src/args.rs` | `InitCommand` 参数结构定义（模板名、目录、包参数）。 |
| `crates/typst-syntax/src/package.rs` | `PackageSpec`、`PackageManifest`、`TemplateInfo` 等核心类型的定义与 `FromStr`、`validate` 实现。 |
| `crates/typst-kit/src/packages.rs` | `SystemPackages::obtain` / `latest_version` 的实现（本讲只引用，细节见 u3-l2）。 |

> 本讲对 `typst-kit`、`typst-syntax` 中的类型只做「消费方」视角的讲解，重点是 typst-cli 侧 `init.rs` 如何把它们串起来。

## 4. 核心概念与源码讲解

### 4.1 init 主流程：从命令到新项目

#### 4.1.1 概念说明

`typst init` 是一个「脚手架（scaffold）」命令：它本身不做任何排版编译，而是把一个现成的模板包里的文件**复制**到用户指定的新目录里，让用户立刻得到一个可编译的项目。

模板包和普通包的差别只在于：模板包的 `typst.toml` 里多了一个 `[template]` 段，它告诉 CLI「这个包里哪个子目录是要复制给用户的样板」。因此 `init` 的本质是：**定位包 → 读清单 → 确认它是模板 → 复制模板子目录 → 打印下一步提示**。

模板包可以来自两个地方：

- `@preview/<name>`：发布到 Typst Universe 的在线模板（联网下载）；
- `@local/<name>`：本地 data 目录里的模板（不联网）。

#### 4.1.2 核心流程

`init` 的主函数是一个线性的六步流程，任何一步失败都用 `?` 冒泡成 `StrResult`：

```
1. 装配包存储      packages = packages::system(&command.package)
2. 解析包标识      spec = parse(command.template)        # 含版本自动推断
3. 定位/下载包     root = packages.obtain(&spec)         # 返回 FsRoot
                   package_path = root.path()
4. 读清单并校验    manifest = parse_manifest(package_path)
                   manifest.validate(&spec)
5. 确认是模板      template = manifest.template          # 缺失则 bail!
6. 脚手架 + 提示   scaffold_project(project_dir, package_path, template)
                   print_summary(spec, project_dir, template)
```

其中「项目目录」`project_dir` 的确定规则是：优先用命令行 `--dir`，否则默认用**清单里 `[package].name` 的值**（即包名）。

#### 4.1.3 源码精读

主函数 `init` 全部位于 [init.rs:16-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L16-L53)。下面按步骤解读。

**第 1 步：装配包存储**——复用 u3-l2 讲过的薄壳：

[init.rs:17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L17) 把 `PackageArgs` 转成 `SystemPackages`，这一步确定「去哪里找包、是否允许联网」。对应实现见 [packages.rs:6-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs#L6-L18)。

**第 2 步：解析包标识（含版本推断）**——本讲的重点之一：

```rust
let spec: PackageSpec = command.template.parse().or_else(|err| {
    let spec: VersionlessPackageSpec = command.template.parse().map_err(|_| err)?;
    let version = packages.latest_version(&spec)?;
    StrResult::Ok(spec.at(version))
})?;
```

这段 [init.rs:22-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L22-L28) 的逻辑是「先试完整、再试无版本」：

1. 先把 `command.template`（一个字符串，如 `@preview/charged-ieee:0.1.0`）按完整 `PackageSpec` 解析；若用户带了 `:版本号`，这一步直接成功。
2. 若失败（用户没给版本号），退而求其次按 `VersionlessPackageSpec` 解析，再调 `packages.latest_version(&spec)` 查最新版本，最后 `.at(version)` 补全。
3. 一个细节：`map_err(|_| err)?` 刻意丢弃「无版本解析」自身的错误，转而返回第 1 步「完整解析」的错误信息——因为后者对用户更有意义（例如「缺 `@`」「缺命名空间」）。

**第 3 步：定位/下载包**：

[init.rs:31-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L31-L32) 调 `packages.obtain(&spec)`，返回一个 `FsRoot`；`root.path()` 即该包在磁盘上的根目录（里面应当有 `typst.toml`）。`obtain` 内部的「data → cache → 联网下载」三级短路逻辑见 [typst-kit/src/packages.rs:95-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L124)（u3-l2 已详解）。

**第 4 步：读清单并校验**：

[init.rs:35-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L35-L36) 读取并解析 `typst.toml`，随后用 `manifest.validate(&spec)` 校验清单与命令行 spec 是否一致。详见 4.3。

**第 5 步：确认是模板**：

```rust
let Some(template) = &manifest.template else {
    bail!("package {spec} is not a template");
};
```

[init.rs:39-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L39-L41) 用 `let-else` 取出 `[template]` 段；若该包根本不是模板（没有 `[template]`），立即报错退出。这是 `init` 区别于普通包引用的关键守卫。

**第 6 步：确定项目目录、复制、提示**：

[init.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L44) 确定 `project_dir`（命令行优先，否则用 `manifest.package.name`），然后 [init.rs:47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L47) 复制文件、[init.rs:50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L50) 打印提示。这两步分别在 4.4、4.5 详解。

`init` 在 `main.rs` 的分发入口是 [main.rs:73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L73)：`Command::Init(command) => crate::init::init(command)?`。

#### 4.1.4 代码实践

**实践目标**：验证「项目目录默认名 = 包名」这条规则，并对照源码走一遍版本推断。

**操作步骤**（源码阅读型）：

1. 读 [args.rs:137-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L137-L152)，确认 `InitCommand` 有 `template`（必填）、`dir`（可选）、`package`（扁平化的 `PackageArgs`）三个字段。
2. 读 [init.rs:22-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L22-L28)，回答：用户输入 `@preview/charged-ieee`（不带版本）时，代码走哪条分支？输入 `@preview/charged-ieee:0.1.0`（带版本）时呢？

**需要观察的现象**：

- 带 `:0.1.0` 时，`command.template.parse()` 直接成功，`or_else` 闭包不执行。
- 不带版本时，`or_else` 闭包执行，先按无版本解析，再联网（或查本地）拿 `latest_version`。

**预期结果**：你能画出两条输入对应的代码路径。运行结果「待本地验证」（依赖联网或本地包）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户传了一个既不是合法 `PackageSpec`、也不是合法 `VersionlessPackageSpec` 的字符串（例如 `charged-ieee`，缺了 `@`），最终报的错误来自哪一步？为什么？

> **答案**：报的是「完整 `PackageSpec` 解析」的错误（`@` 缺失之类）。因为 `or_else` 闭包里 `.map_err(|_| err)?` 主动丢弃了无版本解析的错误，把第 1 步的 `err` 重新抛出——注释里明确说「prefer the error message of the normal package spec parsing」。

**练习 2**：`init` 流程中，哪几步可能触发**网络访问**？

> **答案**：两处可能联网——第 2 步 `latest_version`（仅 `@preview` 命名空间会下载 index），以及第 3 步 `obtain`（仅 `@preview` 且本地未命中时下载包本体）。`@local` 模板两步都不联网。

---

### 4.2 清单解析与校验：parse_manifest 与 validate

#### 4.2.1 概念说明

`typst.toml` 是每个包的「身份证 + 说明书」。`init` 在拿到包的根目录后，必须读取并解析它，原因有三：

1. 要从 `[package].name` 取出默认项目目录名；
2. 要确认 `[template]` 段存在（是不是模板）；
3. 要从 `[template].path` / `[template].entrypoint` 知道「复制哪个子目录」「入口文件叫什么」。

`parse_manifest` 负责「读文件 + 反序列化」，`PackageManifest::validate` 负责「反序列化之后的语义校验」。二者一前一后，分别处理「格式问题」和「内容问题」。

#### 4.2.2 核心流程

```
parse_manifest(package_path):
  toml_path = package_path/typst.toml
  string    = read_to_string(toml_path)         # 读失败 → FileError 包装
  manifest  = toml::from_str(string)            # 格式错 → malformed
  return manifest

manifest.validate(spec):
  校验 manifest.package.name    == spec.name     # 名字要一致
  校验 manifest.package.version == spec.version  # 版本要一致
  若声明了 compiler 下限 → 校验当前编译器版本 >= 下限
```

`PackageManifest` 的核心结构（来自核心库）：

```rust
struct PackageManifest {
    package: PackageInfo,              // [package] 段
    template: Option<TemplateInfo>,    // [template] 段（模板才有）
    tool: ToolInfo,                    // [tool] 段（第三方工具配置）
    unknown_fields: UnknownFields,     // 未知字段（用于校验/容错）
}

struct TemplateInfo {
    path: EcoString,            // 要复制的子目录（相对包根）
    entrypoint: EcoString,      // 入口文件（相对 template.path）
    thumbnail: Option<EcoString>,
    unknown_fields: UnknownFields,
}
```

#### 4.2.3 源码精读

**`parse_manifest`**：[init.rs:56-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L56-L67)。

```rust
let toml_path = package_path.join("typst.toml");
let string = std::fs::read_to_string(&toml_path).map_err(|err| {
    eco_format!("failed to read package manifest ({})",
        FileError::from_io(err, &toml_path))
})?;
toml::from_str(&string)
    .map_err(|err| eco_format!("package manifest is malformed ({})", err.message()))
```

两个错误分支都精心包装过：读失败用 `FileError::from_io` 转成项目通用的文件错误再拼进字符串；解析失败用 `err.message()` 只取核心信息。注意它读的是**包根目录下的 `typst.toml`**（`package_path` 是 `obtain` 返回的包根），而不是模板子目录里的文件。

**`PackageManifest` 类型定义**：[package.rs:21-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L21-L34)，`template` 字段是 `Option<TemplateInfo>`，且标了 `#[serde(default, skip_serializing_if = "Option::is_none")]`——没有 `[template]` 段时就是 `None`，这正是 4.1 第 5 步 `let-else` 判断的依据。

**`TemplateInfo` 类型定义**：[package.rs:79-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L79-L94)。`path` 是「相对包根、要复制给用户的子目录」，`entrypoint` 是「相对 `template.path` 的入口文件」。理解这两段的相对基准不同，是看懂 4.4 复制逻辑和 4.5 提示命令的关键。

**`validate`**：[package.rs:157-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L157-L183)，三重校验：

```rust
if self.package.name != spec.name { ... }       // 名字
if self.package.version != spec.version { ... } // 版本
if let Some(required) = self.package.compiler { // 编译器下限
    let current = PackageVersion::compiler();
    if !current.matches_ge(&required) { ... }
}
```

前两步保证「磁盘上这个包确实是你命令行要的那个」（防止目录被错放/改名），第三步保证「当前 typst 版本够新，能跑这个模板」。`init.rs:36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L36) 调用 `manifest.validate(&spec)?`，任何一项不满足都会冒泡成错误并终止。

#### 4.2.4 代码实践

**实践目标**：用一个手写的 `typst.toml` 验证 `validate` 的三重校验。

**操作步骤**：

1. 在某个临时目录建 `typst.toml`，故意把 `[package].version` 写成 `0.2.0`，而目录结构按 `local/<name>/0.1.0` 排列（即让 spec 版本与清单版本不一致）。
2. 用 `typst init @local/<name>:0.1.0` 尝试初始化。

**需要观察的现象**：终端报「package manifest contains mismatched version」。

**预期结果**：印证 [package.rs:165-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L165-L170) 的版本一致性校验生效。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`parse_manifest` 读的是哪个目录的 `typst.toml`？为什么不是模板子目录里的？

> **答案**：读的是包根目录（`package_path`，即 `obtain` 返回的 `FsRoot.path()`）的 `typst.toml`。因为清单是「整个包」的身份证，必须放在包根；模板子目录（`[template].path`）里只是给用户复制的样板文件，不负责描述包元信息。

**练习 2**：`validate` 里 `self.package.compiler` 是 `Option<VersionBound>`。一个模板若没在 `typst.toml` 里写 `compiler` 字段，会怎样？

> **答案**：跳过编译器版本校验（`if let Some(required)` 不匹配），任何 typst 版本都放行。`compiler` 是可选字段，只在你显式声明最低版本要求时才生效。

---

### 4.3 脚手架复制：scaffold_project

#### 4.3.1 概念说明

拿到 `TemplateInfo` 后，`init` 的核心动作就是把模板的「样板子目录」原样复制成用户新项目。这件事看起来简单，但源码做了两个重要的**前置保护**：

1. 目标项目目录**不能已存在**（防止误覆盖用户已有项目）；
2. 清单里声明的模板子目录**必须真实存在**（防止清单撒谎）。

只有这两个检查通过，才用第三方 crate `fs_extra` 做递归目录复制。

#### 4.3.2 核心流程

```
scaffold_project(project_dir, package_path, template):
  若 project_dir 已存在       → bail! "project directory already exists"
  template_dir = package_path / template.path
  若 template_dir 不存在      → bail! "template directory does not exist"
  fs_extra::dir::copy(template_dir → project_dir, content_only=true)
```

注意 `content_only(true)` 这个选项：它表示「只复制源目录的**内容**，不把源目录本身作为一个子目录套进去」。也就是说 `template_dir` 里的文件会直接落到 `project_dir` 下，而不是 `project_dir/<template.path 的最后一段>/` 下。

#### 4.3.3 源码精读

[scaffold_project](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L71-L93)（init.rs:71-93）：

```rust
if project_dir.exists() {
    bail!("project directory already exists (at {})", project_dir.display());
}

let template_dir = package_path.join(template.path.as_str());
if !template_dir.exists() {
    bail!("template directory does not exist (at {})", template_dir.display());
}

fs_extra::dir::copy(
    &template_dir,
    project_dir,
    &CopyOptions::new().content_only(true),
).map_err(|err| eco_format!("failed to create project directory ({err})"))?;
```

逐行看：

- [init.rs:76-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L76-L78)：第一道保护——目标目录已存在就拒绝。这是 `init` 最容易触发也最「防呆」的检查。
- [init.rs:80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L80)：`template_dir` 由「包根」拼接「`[template].path`」得到。例如模板 `path = "template"`，包根是 `…/preview/charged-ieee/0.1.0`，则 `template_dir = …/preview/charged-ieee/0.1.0/template`。
- [init.rs:81-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L81-L83)：第二道保护——清单声明的子目录不存在就拒绝（清单与实际内容不一致）。
- [init.rs:85-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L85-L90)：`fs_extra::dir::copy` 执行递归复制。`fs_extra` 是 workspace 依赖，声明于 [Cargo.toml:40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L40)（`fs_extra = { workspace = true }`）。失败时包装成 `failed to create project directory`。

> 术语：`content_only` 决定了「复制源目录本身 vs 复制源目录里的东西」。设为 `true` 后，`template_dir` 这个壳被去掉，其内部文件直接成为 `project_dir` 的直接内容——这正是「把样板铺平成新项目」所需。

#### 4.3.4 代码实践

**实践目标**：理解 `content_only(true)` 的效果，以及两道前置保护的触发条件。

**操作步骤**（源码阅读型 + 本地可选验证）：

1. 阅读 [init.rs:76-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L76-L90)，回答：若 `template.path = "template"`，复制后新项目里会不会出现一个叫 `template` 的子目录？
2. 若想本地验证：对同一个模板连续运行两次 `typst init @preview/<模板>`（不指定 `--dir`，让它默认同名），观察第二次的现象。

**需要观察的现象**：

- `content_only(true)` 下，新项目里**不会**多一层 `template/` 目录，样板文件直接出现在项目根。
- 第二次运行会因为目录已存在而被 [init.rs:76-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L76-L78) 拦截，报 `project directory already exists`。

**预期结果**：与源码两道保护一一对应。第二次运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `scaffold_project` 要在复制前检查 `template_dir.exists()`？`validate` 不是已经校验过清单了吗？

> **答案**：`validate` 只校验清单的**元信息**（名字、版本、编译器版本），不检查 `[template].path` 指向的目录在磁盘上是否真的存在。一个包可能写出合法的清单却漏放模板目录（打包错误），所以复制前必须再验一次磁盘真实性。

**练习 2**：如果把 `CopyOptions::new().content_only(true)` 改成 `content_only(false)`，新项目结构会怎样变化？

> **答案**：会把 `template_dir` **连同目录名一起**复制进去，导致新项目里多一层「模板子目录名」的目录，样板文件被埋深一层，与清单里 `entrypoint`（相对 `template.path`）的预期路径错位。

---

### 4.4 成功提示：print_summary

#### 4.4.1 概念说明

复制完成后，`init` 会打印一段「成功 + 下一步命令」的提示，告诉用户如何进入新目录并开始写作。这一步看似只是 `println`，但有两个值得学习的工程细节：

1. 它复用了 typst-cli 的统一终端输出出口 `crate::terminal::out()`，并支持**彩色/暗淡样式**（灰色提示前缀 `> `），与诊断输出、watch 状态共用同一条 stderr 通道；
2. 它对要拼进命令行的路径做 **shell 转义**（`shell_escape`），避免路径含空格或特殊字符时复制粘贴的命令出错。

#### 4.4.2 核心流程

```
print_summary(spec, project_dir, template):
  out = terminal::out()
  打印 "Successfully created new project from <spec> 🎉"
  打印 "To start writing, run:"
  灰色 "> "  +  "cd <转义后的 project_dir>"
  灰色 "> "  +  "typst watch <转义后的 template.entrypoint>"
  空行收尾
```

#### 4.4.3 源码精读

[print_summary](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L96-L126)（init.rs:96-126）：

```rust
let mut gray = ColorSpec::new();
gray.set_fg(Some(Color::White));
gray.set_dimmed(true);

let mut out = crate::terminal::out();
writeln!(out, "Successfully created new project from {spec} 🎉")?;
writeln!(out, "To start writing, run:")?;
out.set_color(&gray)?;
write!(out, "> ")?;
out.reset()?;
writeln!(out, "cd {}", shell_escape::escape(project_dir.display().to_string().into()))?;
// …同上模式打印 "typst watch <entrypoint>"
```

关键点：

- [init.rs:101-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L101-L103)：构造一个「白色 + dimmed（暗淡）」的 `ColorSpec`，用于 `> ` 前缀。
- [init.rs:105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L105)：`crate::terminal::out()` 是 typst-cli 的终端输出 singleton（u2-l4 讲过），实现 `WriteColor`，非 TTY 时自动不输出颜色。
- [init.rs:106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L106)：`{spec}` 直接用 `PackageSpec` 的 `Display`（形如 `@preview/charged-ieee:0.1.0`，定义于 [package.rs:262-266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L262-L266)）。
- [init.rs:114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L114) 与 [init.rs:122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L122)：`shell_escape::escape` 对路径转义。`shell-escape` 声明于 [Cargo.toml:50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L50)。
- [init.rs:121-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L121-L123)：提示的「下一步」命令是 `typst watch <entrypoint>`，其中 `entrypoint` 来自 `TemplateInfo`（相对 `template.path`），即新项目里那个主文件的名字。

注意返回类型是 `std::io::Result<()>`，与 `init` 主体的 `StrResult` 不同，所以调用处 [init.rs:50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L50) 用 `.unwrap()`——打印失败属于「无关紧要的 IO 错误」（例如终端被关闭），不值得让整个 init 失败。

#### 4.4.4 代码实践

**实践目标**：观察彩色与 shell 转义的实际效果。

**操作步骤**：

1. 在一个**名字带空格**的目录里运行 `typst init`，并让默认项目名也带特殊字符（例如用 `--dir` 指定 `my project`）。
2. 观察打印出的 `cd` 命令是否被正确加引号/转义。

**需要观察的现象**：提示中的 `cd 'my project'`（或对应平台的转义形式），可直接复制到 shell 执行。

**预期结果**：印证 `shell_escape::escape` 的作用。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`print_summary` 用 `.unwrap()` 而不是 `?`，为什么这样设计是安全的？

> **答案**：它是 `init` 的最后一步（项目已成功创建），打印失败只意味着「终端 IO 异常」（如 stdout 已关闭），对用户毫无可挽救的价值；此时 panic/终止与返回成功没有实质区别，所以用 `unwrap` 简化代码、避免把 `io::Error` 与主流程的 `StrResult` 混在一起。

**练习 2**：提示里建议用户运行 `typst watch`，而不是 `typst compile`。这反映了 typst 推崇怎样的写作方式？

> **答案**：反映了 typst 的「写一边看一边」实时预览理念——`watch` 模式会在文件变更时增量重编译（见 u2-l5），比单次 `compile` 更适合持续写作的场景，所以被作为默认推荐的下一步。

---

## 5. 综合实践

把本讲的全部知识串起来：**手写一个本地 `@local` 模板包，并用 `typst init` 把它脚手架成一个新项目**。这个任务能让你从「消费模板」走到「提供模板」，完整理解 `init` 的每个判定。

### 准备：构建 typst 二进制

```bash
# 在仓库根目录（workspace 根）
cargo build -p typst-cli
# 产物在 target/debug/typst
```

### 第 1 步：搭出 `@local` 模板包的目录结构

按 `FsPackages` 的「namespace/name/version」三层约定，把包放进本地 data 目录，并用 `--package-path` 指向它（避免污染系统目录）。例如在工作目录下建：

```
my-packages/local/demo/0.1.0/
├── typst.toml
└── template/            ← 这就是要被复制的样板子目录
    └── main.typ         ← 入口文件
```

`typst.toml` 内容（示例，非项目原有代码）：

```toml
[package]
name = "demo"
version = "0.1.0"
entrypoint = "lib.typ"     # 包本身的入口（脚手架时不重要，但 validate 要求存在该字段）

[template]
path = "template"          # 要复制的子目录（相对包根）
entrypoint = "main.typ"    # 新项目的入口（相对 template.path）
```

`template/main.typ` 写任意可编译内容，例如 `Hello from my template.`。

### 第 2 步：用 `init` 创建项目

```bash
target/debug/typst init @local/demo \
  --package-path ./my-packages
```

### 第 3 步：对照源码逐项验证

执行后，依次核对：

1. **版本推断**：你没写 `:0.1.0`，命令仍成功——说明 [init.rs:22-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L22-L28) 走了 `latest_version` 分支。注意：`@local` 不联网，`latest_version` 改为在 **data 目录**扫描版本子目录取最大值（见 [typst-kit/src/packages.rs:127-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L127-L142)）。
2. **项目目录名**：默认生成了 `demo/` 目录——印证 `project_dir = manifest.package.name`（[init.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L44)）。
3. **复制效果**：`demo/` 里直接是 `main.typ`，**没有**多一层 `template/`——印证 `content_only(true)`（[init.rs:85-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L85-L90)）。
4. **成功提示**：屏幕打印 `Successfully created new project from @local/demo:0.1.0 🎉` 并给出 `cd demo` 与 `typst watch main.typ`（[init.rs:106-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L106-L123)）。

### 第 4 步：故意触发两道保护（可选）

- 在已有 `demo/` 上再跑一次 `typst init @local/demo --package-path ./my-packages` → 应报 `project directory already exists`（[init.rs:76-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L76-L78)）。
- 把 `typst.toml` 里的 `path = "template"` 改成 `path = "nonexistent"` 再初始化（换个 `--dir`）→ 应报 `template directory does not exist`（[init.rs:81-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L81-L83)）。

> 全流程的具体输出「待本地验证」，取决于你的 typst 二进制版本与终端环境。

## 6. 本讲小结

- `typst init` 是「脚手架」命令，不做编译，只做「定位包 → 读清单 → 确认模板 → 复制文件 → 打印提示」六步线性流程，任何一步失败都用 `?` 冒泡。
- 包标识解析支持「带版本」与「不带版本」两条路径：后者先按 `VersionlessPackageSpec` 解析，再靠 `latest_version` 自动补全——`@preview` 联网查 index，`@local` 在 data 目录扫描；错误信息优先用完整解析的版本。
- `parse_manifest` 读包根的 `typst.toml` 并反序列化成 `PackageManifest`；`validate` 再做名字、版本、编译器版本三重一致性校验；`[template]` 段缺失则判定「不是模板」。
- `scaffold_project` 用 `fs_extra::dir::copy` + `content_only(true)` 把模板子目录铺平复制成新项目，复制前有「目标已存在」「模板目录不存在」两道保护。
- `print_summary` 复用 `terminal::out()` 统一出口、对路径做 `shell_escape` 转义，返回 `io::Result` 并用 `unwrap()` 收尾；`init` 复用 u3-l2 的 `packages::system()`，体现了「脚手架链路」与「编译链路」共享同一套包基础设施。

## 7. 下一步学习建议

- 若想了解模板被复制后「如何被真正编译」，请回到 [u2-l1 SystemWorld](u2-l1-system-world.md) 与 [u2-l2 编译配置](u2-l2-compile-config.md)，看 `SystemWorld::new` 如何用同一个 `packages::system()` 把项目里的 `#import "@preview/…"` 解析到包根目录。
- 想理解 `@preview` 在线下载那条路径（含进度条、TLS 证书），继续阅读 [u3-l3 网络下载与进度报告](u3-l3-download-progress.md)。
- 想看 CLI 如何端到端测试「包解析」这类场景，阅读 [u4-l7 测试与 smoke 测试](u4-l7-smoke-tests.md) 中的 `test_package_resolved`。
- 推荐继续通读 `crates/typst-syntax/src/package.rs` 全文，它还包含 `VersionBound` 的 `matches_ge/lt/gt` 比较语义，是理解 `validate` 里编译器版本校验的钥匙。
