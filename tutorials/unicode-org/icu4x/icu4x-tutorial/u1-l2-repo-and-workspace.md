# 仓库结构与 Cargo 工作区组织

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 ICU4X 仓库顶层 8 大目录（`components` / `provider` / `utils` / `ffi` / `examples` / `tools` / `tutorials` / `documents`）各自的职责。
- 看懂根目录 `Cargo.toml` 是如何用 **Cargo workspace**（工作区）把近 90 个 crate 组织在一起的，包括 `members`、`exclude`、`[workspace.package]`、`[workspace.dependencies]`、自定义 `profile` 与统一的 `[workspace.lints]`。
- 根据「功能需求」反推到「具体是哪个 crate」，并理解 ICU4X 的三套命名约定（组件 `icu_*`、数据 `icu_provider_*` / `icu_*_data`、工具 crate 用短名）。

这一讲是后续所有讲义的「地图」。有了它，你在面对 `icu_datetime`、`icu_provider_blob`、`zerovec` 这样的名字时，能立刻知道它们属于哪一层、解决什么问题。

## 2. 前置知识

本讲默认你已经在上一讲（u1-l1）了解了 ICU4X 的定位与四大设计目标。这里补充两个 Rust / Cargo 的基础概念，不熟悉的也没关系，我们边看边讲。

- **crate（箱子）**：Rust 的最小编译单元。一个 crate 可以是一个库，也可以是一个二进制。ICU4X 仓库里大部分 crate 都是库。
- **Cargo workspace（工作区）**：当一个大项目由很多互相依赖的 crate 组成时，把它们放进一个 workspace，就能让它们**共享同一套依赖版本、同一份元数据、同一次编译产物目录**，避免每个 crate 各自维护一份 `Cargo.lock` 和重复编译第三方依赖。

ICU4X 正是这种「巨型企业级 monorepo」的典型：它有近 90 个自己的 crate，再加上上百个第三方依赖。没有 workspace 的话，光是让所有 crate 用同一版本的 `serde` 就会很痛苦。

> 补充一句关于 Rust 工具链：仓库根目录的 `rust-toolchain.toml` 指定了 `channel = "1.95"`，而 `[workspace.package]` 里的 `rust-version = "1.88"` 是**最低支持版本（MSRV）**。两者含义不同：前者是「本仓库开发时用的版本」，后者是「用户编译时至少需要的版本」。

## 3. 本讲源码地图

本讲主要围绕两个真实源码文件展开，辅以对仓库目录的直接观察：

| 文件 / 目录 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml)（仓库根） | 定义整个 workspace：`members`、`exclude`、共享元数据、共享依赖、编译 profile、统一 lint 规则。 |
| [`components/icu/src/lib.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs) | `icu` 元 crate（meta-crate）的入口。它本身几乎没有逻辑，只是把各组件 crate 重新导出为模块，是理解「组件层」如何聚合的最佳样本。 |
| [`components/icu/Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml) | 元 crate 的依赖声明，展示了「子 crate 如何用 `workspace = true` 继承共享配置」。 |
| `components/`、`provider/`、`utils/`、`ffi/`、`examples/`、`tools/`、`tutorials/`、`documents/` | 8 个顶层目录，本讲逐一讲解职责。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责速览

#### 4.1.1 概念说明

ICU4X 的设计目标（小而模块化、可插拽数据、跨语言、由专家编写）直接映射成了仓库的目录划分。你可以把仓库理解成一个「分层商店」：

- **`components/`** 是「商品」——每个国际化能力（日期、数字、排序、分段……）都是一个独立 crate，用户按需取用。这就是「小而模块化」。
- **`provider/`** 是「数据供应链」——所有算法需要 locale 数据，但数据从哪来、如何缓存、如何回退、如何生成，都被抽象到这一层，可以替换。这就是「可插拔 locale 数据」。
- **`utils/`** 是「地基材料」——零拷贝容器、生命周期管理、短字符串等通用 Rust 工具，支撑上面两层做到小体积、低内存。这也是「fast、low-memory」的底层来源。
- **`ffi/`** 是「出口包装」——用 Diplomat 把 Rust 实现包装成 C/JS/Dart 等语言的绑定。这就是「跨语言易用」。
- 其余 `examples/`、`tools/`、`tutorials/`、`documents/` 是配套的示例、构建工具、教程和设计文档。

记住这条主线：**components 用数据、provider 供数据、utils 当地基、ffi 做包装**。后面所有讲义都在这四层里打转。

#### 4.1.2 核心流程

下面这张「职责表」是本讲最重要的速查表。先用文字版呈现，4.1.4 的实践会让你亲手验证它。

| 顶层目录 | 职责 | 典型成员 | 后续讲义 |
| --- | --- | --- | --- |
| `components/` | 国际化组件库（用户直接调用的算法） | `calendar`、`datetime`、`decimal`、`collator`、`segmenter`、`icu`（元 crate）等 | u3、u4 |
| `provider/` | 可插拔的 locale 数据机制 | `core`、`adapters`、`blob`、`fs`、`baked`、`data/*`、`source`、`export`、`icu4x-datagen` | u5 |
| `utils/` | 通用、零拷贝、低内存工具 | `zerovec`、`yoke`、`databake`、`writeable`、`tinystr`、`litemap`、`fixed_decimal` 等 | u6 |
| `ffi/` | 多语言绑定 | `capi`、`ecma402`、`freertos`、`dart`、`npm`、`mvn` | u7 |
| `examples/` | 多语言示例（**不在 workspace 内**，模拟外部用户） | `rust.md`、`cpp`、`js-tiny`、`dart`、`npm` 等 | u1-l3 |
| `tools/` | 构建、代码生成、性能基准、文档工具 | `make`、`benchmark`、`codegen`、`md-tests`、`noalloctest` 等 | u8-l6 |
| `tutorials/` | 面向用户的 Markdown 教程 | `quickstart.md`、`data-management.md`、`date-picker.md` 等 | u1-l3 |
| `documents/` | 设计提案、流程规范、会议记录 | `process/`、`proposals/`、`design/` 等 | — |

一个特别值得注意的细节：`examples/` 被刻意排除在 workspace 之外（见 4.2.3）。这是为了让示例像「真实的外部用户」那样依赖已发布的 crate，从而验证公开 API 真的可被外部使用。

#### 4.1.3 源码精读

我们用元 crate 的入口文件来印证「components 是聚合用户能力的入口」这一点。`icu` 这个 crate 的文档注释开宗明义：

[`components/icu/src/lib.rs:18-30`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L18-L30) — 说明 `icu` 是 ICU4X 的主 meta-crate，它**不带来任何独有功能**，只是把各组件 crate 重新导出（re-export）为模块，方便用户一处取用。

文件末尾就是这些 re-export，每行把一个 `icu_*` 组件 crate 暴露成 `icu::xxx` 模块：

[`components/icu/src/lib.rs:140-177`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L140-L177) — 把 `icu_calendar` 导出为 `icu::calendar`、`icu_datetime` 导出为 `icu::datetime`，依此类推。这正好印证了「`icu` 只是把 `components/` 下的组件聚合起来」。

注意末尾两个有条件导出的模块：

[`components/icu/src/lib.rs:179-185`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L179-L185) — `experimental` 和 `pattern` 只在启用 `unstable` feature 时才导出，说明它们尚未稳定，是「商品」里的「预览款」。

> 小提示：`components/` 里还有一个特殊的 `icu/` 子目录，它就是上面这个「元 crate」。元 crate 自己也是 workspace 的一个成员（见 4.2.3 的 `components/icu`）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手把上面的职责表和真实目录对应起来。

1. **实践目标**：确认 `components/`、`provider/`、`utils/`、`ffi/` 四大目录下各有哪些成员，建立直观印象。
2. **操作步骤**：在仓库根目录执行 `ls components/ provider/ utils/ ffi/`（或用文件浏览器展开这四个目录）。
3. **需要观察的现象**：你应该看到例如 `components/` 下有 `calendar`、`datetime`、`decimal` 等组件目录；`provider/` 下有 `core`、`blob`、`data` 等；`utils/` 下有 `zerovec`、`yoke`、`tinystr` 等短名工具；`ffi/` 下有 `capi`、`dart`、`npm` 等。
4. **预期结果**：目录列表与本讲 4.1.2 的表格大体一致。如果你在 `components/` 下没看到某个你听过的能力（例如货币格式化），可以去 `components/experimental/` 找——这印证了「未稳定的能力放在 experimental」。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：用户想格式化日期，应该去哪个顶层目录找？想自定义数据的来源呢？

> **答案**：格式化日期属于「算法」，去 `components/`（具体是 `components/datetime`）；自定义数据来源属于「数据机制」，去 `provider/`。

**练习 2**：为什么 `zerovec`、`yoke` 这些工具放在 `utils/` 而不是 `components/`？

> **答案**：它们是**通用的 Rust 基础工具**（零拷贝容器、生命周期绑定），不依赖任何 i18n 语义，也不直接面向终端用户，所以放在 `utils/`，并且独立发布到 crates.io 供非 ICU4X 项目也能使用。`components/` 只放面向用户的国际化能力。

---

### 4.2 Cargo 工作区与共享依赖

#### 4.2.1 概念说明

一个 workspace 通过根 `Cargo.toml` 的 `[workspace]` 段定义。ICU4X 的根 `Cargo.toml` 用它做了五件事：

1. 用 `members` 列出所有参与工作区的 crate。
2. 用 `exclude` 把某些子目录排除（例如 `examples`）。
3. 用 `[workspace.package]` 声明**全工作区共享的包元数据**（版本号、license、edition 等），子 crate 用 `xxx.workspace = true` 继承。
4. 用 `[workspace.dependencies]` 声明**全工作区共享的依赖版本**，子 crate 写 `dep = { workspace = true }` 即可复用，避免每个 crate 各写一遍版本号。
5. 用自定义 `[profile.*]` 和 `[workspace.lints]` 统一编译优化选项与 lint 标准。

这种「集中声明、各处继承」的写法是大型 Rust 项目的通行做法，ICU4X 把它用得很彻底——近 90 个 crate 的版本号、license、依赖版本都被收敛到了一个文件里。

#### 4.2.2 核心流程

理解 ICU4X workspace 的工作流，可以分成「声明 → 继承 → 特殊处理」三步：

```text
根 Cargo.toml 声明                 子 crate 继承
─────────────────────              ─────────────────────────
[workspace]                        [package]
 members = [ ... ]      ─┐         version.workspace = true
                         │         license.workspace = true
[workspace.package]      │         edition.workspace = true
 version = "2.2.0"      ─┤
 license = "Unicode-3.0"│         [dependencies]
 edition = "2024"        │         icu_datetime = { workspace = true }
                         │
[workspace.dependencies]│
 icu_datetime = {       ─┘
   version = "~2.2.0",
   path = "components/datetime",
   default-features = false
 }
```

要点：

- `members` 里的每一项都是**相对于根目录的路径**，指向一个含 `Cargo.toml` 的子目录。
- 共享依赖既写了 `version = "..."`（给外部用户从 crates.io 拉取），又写了 `path = "..."`（给仓库内部直接走本地路径）。这样**仓库内部改代码立刻生效，发布后又自动用 crates.io 版本**，一举两得。
- 几乎所有共享依赖都带 `default-features = false`，再按需打开 feature——这是 ICU4X 控制「小体积」和 `no_std` 兼容的关键习惯。

#### 4.2.3 源码精读

**① `members` 数组——整个工作区的「点名册」**

[`Cargo.toml:7-98`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L7-L98) — 这是 `members` 的完整列表。注意 ICU4X 用**注释把成员分成 7 组**，和我们的顶层目录一一对应：`ICU4X core`、`Components`、`FFI`、`Provider`、`Baked data`、`Utils`、`Tools`。这种「分组 + 注释」的写法让近百个成员仍然清晰可读。

举几个关键分组：

[`Cargo.toml:10-12`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L10-L12) — `ICU4X core` 组，只有两个最底层的 crate：`components/locale_core`（Locale 数据模型）和 `provider/core`（DataProvider 抽象）。它们是整个仓库被依赖最多的两个 crate。

[`Cargo.toml:48-60`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L48-L60) — `Baked data` 组，是 12 个「编译期内嵌数据」的 crate（如 `provider/data/calendar`）。它们就是上一讲提到的「compiled data」的实物来源，每个组件对应一个数据 crate。

**② `exclude`——为什么 examples 不在工作区里**

[`Cargo.toml:101-106`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L101-L106) — `examples` 被显式排除，注释写明「Examples are tested outside the workspace to simulate external users」（示例在工作区外测试，以模拟外部用户）。这一点很重要：ICU4X 想确保示例代码真的能像普通用户那样从 crates.io 拉依赖、能编译，而不是偷偷走工作区内部路径。`examples/.cargo/config.toml` 里的 `[patch.crates-io]` 再把发布版替换回本地路径用于测试，做到了「外部视角 + 本地代码」的兼顾。

**③ `[workspace.package]`——共享元数据**

[`Cargo.toml:108-128`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L108-L128) — 声明 `version = "2.2.0"`、`rust-version = "1.88"`、`edition = "2024"`、`license = "Unicode-3.0"`、`categories` 等。所有子 crate 只需写 `version.workspace = true` 就能继承，保证全仓库版本一致。

元 crate `components/icu/Cargo.toml` 就是这么继承的：

[`components/icu/Cargo.toml:8-18`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L8-L18) — 一连串 `xxx.workspace = true`，把版本、作者、edition、license 等全部从工作区继承下来。这就是「集中声明、各处继承」的真实样子。

**④ `[workspace.dependencies]`——共享依赖版本**

[`Cargo.toml:130-211`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L130-L211) — 完整的共享依赖表。注意每个 ICU4X 自己的 crate 都同时给了 `version` 和 `path`：

[`Cargo.toml:141-142`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L141-L142) — `icu` 指向 `path = "components/icu"`，`icu_calendar` 指向 `path = "components/calendar"`，版本用 `~2.2.0`（兼容 2.2.x 的补丁更新）。

元 crate 通过 `workspace = true` 复用这些声明：

[`components/icu/Cargo.toml:23-36`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L23-L36) — 元 crate 把 `icu_calendar`、`icu_datetime` 等组件作为依赖引入，全部用 `{ workspace = true }`，部分还按需加 `features = ["alloc"]`。

**⑤ 自定义 profile 与统一 lint**

[`Cargo.toml:322-348`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L322-L348) — 定义了 `release-opt-size`（体积优化：LTO + `opt-level = "s"` + `panic = "abort"`）、`dev-without-assertions`、`release-with-assertions`、`bench`、`bench-memory` 等专用编译配置。这些 profile 服务于 ICU4X 对「体积 / 内存」的极致追求，后续 u8 单元会专门讲。

[`Cargo.toml:350-393`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L350-L393) — `[workspace.lints]` 把一组严格的 clippy / rust lint（如禁止 `alloc-instead-of-core`、`deny` 掉 `exhaustive_enums` 等）声明在工作区，子 crate 再用 `[lints] workspace = true` 统一启用。这就是为什么 ICU4X 全仓库代码风格高度一致。

最后提一句外部工具依赖：生成多语言绑定的 Diplomat 工具是用 git rev 锁定的：

[`Cargo.toml:221-224`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L221-L224) — `diplomat` 系列依赖指向一个固定的 git commit，注释说明发布前才需发布 Diplomat、开发期可用 git 版本。

#### 4.2.4 代码实践

这是本讲的核心实践，**直接来自本讲规格**：在 `members` 里定位 4 个 crate 并找到它们的真实目录。

1. **实践目标**：掌握「`members` 条目 → 仓库目录 → crate 名」的对应关系。
2. **操作步骤**：
   - 打开根 [`Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml)，在 `members` 数组里搜索下面 4 项，记下它们的成员路径。
   - 再到仓库里实际进入这些目录，打开各自的 `Cargo.toml` 看 `[package] name`。
3. **需要观察的现象与预期结果**（这是答案）：

   | 要找的能力 | members 中的条目（行号） | 真实目录 | crate 名（`[package] name`） |
   | --- | --- | --- | --- |
   | 日历 | `"components/calendar"`（[第 15 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L15)） | `components/calendar/` | `icu_calendar` |
   | 日期时间格式化 | `"components/datetime"`（[第 20 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L20)） | `components/datetime/` | `icu_datetime` |
   | 数据机制核心 | `"provider/core"`（[第 12 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L12)） | `provider/core/` | `icu_provider` |
   | 零拷贝容器工具 | `"utils/zerovec"`（[第 82 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L82)） | `utils/zerovec/` | `zerovec` |

   注意一个关键反差：前三项的**目录名**（`calendar`/`datetime`/`core`）和 **crate 名**（`icu_calendar`/`icu_datetime`/`icu_provider`）并不相同；而 `zerovec` 的目录名和 crate 名一致。这正是 4.3 要讲的「命名约定」差异。
4. 待本地验证（用 `grep 'name =' components/calendar/Cargo.toml` 等命令确认 crate 名）。

#### 4.2.5 小练习与答案

**练习 1**：为什么共享依赖要同时写 `version` 和 `path`？

> **答案**：`path` 让仓库内部开发时直接用本地源码、改了立刻生效；`version` 让该 crate 发布到 crates.io 后，外部用户（以及被别的项目依赖时）能从 crates.io 拉取正确版本。两条信息缺一不可：只有 `path` 则无法发布给外部，只有 `version` 则内部改代码不会立即反映。

**练习 2**：`exclude = ["examples"]` 有什么实际意义？

> **答案**：让 `examples/` 下的代码像真实外部用户一样，从 crates.io 解析依赖（而非走 workspace 内部），从而在 CI 中验证公开 API 确实可被外部正常使用；测试时再用 `examples/.cargo/config.toml` 的 `[patch.crates-io]` 把发布版指回本地路径。

---

### 4.3 crate 命名约定（icu_\* / icu_provider_\* / 工具 crate）

#### 4.3.1 概念说明

ICU4X 有近 90 个 crate，如果命名混乱会很难找。好在我们已经看到仓库用了**三套清晰的命名约定**，一旦掌握，看到名字就能猜出它属于哪一层、是否稳定、是否面向用户：

1. **组件 crate：`icu_<功能>`** —— 面向用户的国际化能力，如 `icu_calendar`、`icu_datetime`、`icu_decimal`。元 crate 直接叫 `icu`。
2. **数据 / provider crate：`icu_provider_*` 和 `icu_<组件>_data`** —— 与「可插拽数据」相关。`icu_provider_blob`、`icu_provider_fs` 是不同存储后端；`icu_calendar_data`、`icu_datetime_data` 是编译期内嵌的 baked 数据。
3. **工具 crate：短名，不带 `icu_` 前缀** —— 如 `zerovec`、`yoke`、`databake`、`writeable`、`tinystr`、`litemap`、`fixed_decimal`。它们是通用 Rust 工具，独立发布，非 ICU4X 项目也能用，所以名字里不绑「icu」。

还有一个稳定性信号：名字带 `experimental` 的（`icu_experimental`、`icu_experimental_data`）版本号是 `0.6.0-dev`，明显低于稳定组件的 `2.2.0`，表示尚未稳定。

#### 4.3.2 核心流程

判断一个 crate 属于哪一层，可以按下面这个决策树（伪代码）：

```text
看到一个 crate 名 / 目录路径：
├─ 目录在 components/ 下？
│   ├─ 目录是 icu/          → 元 crate，名字 = "icu"，聚合所有组件
│   └─ 其他                 → 组件 crate，名字 = "icu_" + 目录名
│                             （experimental 子目录里是未稳定能力）
├─ 目录在 provider/ 下？
│   ├─ 目录是 data/<x>      → baked 数据 crate，名字 = "icu_" + x + "_data"
│   └─ 其他（core/blob/fs…）→ provider 机制 crate，名字 = "icu_provider_" + 目录名
│                             （provider/core 特殊：名字是 "icu_provider"）
├─ 目录在 utils/ 下？        → 通用工具 crate，名字通常 = 目录名（短名，无前缀）
└─ 目录在 ffi/ 下？          → 多语言绑定，名字 = "icu_capi" / "icu4x_ecma402" / "icu_freertos" 等
```

#### 4.3.3 源码精读

**① 组件命名：目录名 `calendar` → crate 名 `icu_calendar`**

[`Cargo.toml:142-147`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L142-L147) — 共享依赖里，`icu_calendar = { ..., path = "components/calendar" }`、`icu_casemap = { ..., path = "components/casemap" }`、`icu_collator = { ..., path = "components/collator" }`、`icu_datetime = { ..., path = "components/datetime" }`。规律一目了然：**目录名前加 `icu_` 就是 crate 名**。元 crate 本身则是 `icu = { ..., path = "components/icu" }`（见 [第 141 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L141)）。

**② provider 命名：`icu_provider_*` 与特殊的 `icu_provider`**

[`Cargo.toml:163-169`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L163-L169) — `icu_provider_export`、`icu_provider_source`、`icu_provider_adapters`、`icu_provider_baked`、`icu_provider_blob`、`icu_provider_fs`、`icu_provider_registry`，全部以 `icu_provider_` 开头，目录在 `provider/` 下。唯一的例外是核心：

[`Cargo.toml:136`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L136) — `icu_provider = { ..., path = "provider/core" }`。`provider/core` 这个目录对应的 crate 名是 `icu_provider`（不带后缀），因为它是整个数据机制的「核心」，地位特殊，所以名字最短最基础。

**③ baked 数据命名：`icu_<组件>_data`**

[`Cargo.toml:172-184`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L172-L184) — `icu_calendar_data`、`icu_datetime_data`、`icu_decimal_data`……每个组件对应一个 `_data` crate，路径在 `provider/data/<组件>` 下。它们就是 compiled data 的实物。注意最后一行 `icu_experimental_data` 的版本是 `0.6.0-dev`，是实验性数据。

**④ 工具命名：短名、无前缀**

[`Cargo.toml:191-211`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L191-L211) — `bies`、`calendrical_calculations`、`crlify`、`databake`、`fixed_decimal`、`ixdtf`、`litemap`、`tinystr`、`writeable`、`yoke`、`zerofrom`、`zerovec` 等。这些名字**不带 `icu_` 前缀**，目录名和 crate 名基本一致，体现它们是「通用工具」，可以脱离 ICU4X 独立使用和发布。其中带 `-derive` 后缀的（如 `databake-derive`、`zerovec-derive`，见 [第 195、210 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L195-L210)）是配套的过程宏 crate。

> 小知识：过程宏（proc-macro）必须放在独立的 crate 里（Rust 的硬性规定），所以 `yoke` 的派生宏要单独成 `yoke-derive`（目录 `utils/yoke/derive`），这也是为什么 `utils/` 下会出现 `xxx/derive` 子目录。

#### 4.3.4 代码实践

1. **实践目标**：用命名约定反推 crate 所属层级，验证决策树。
2. **操作步骤**：对下面 5 个 crate 名，先用决策树判断它属于哪一层、目录大概在哪、是否面向用户；再用 `grep` 在根 `Cargo.toml` 里确认 `path`。
3. **需要观察的现象与预期结果**（答案）：

   | crate 名 | 你的判断 | 实际 path（确认） |
   | --- | --- | --- |
   | `icu_collator` | 组件（`components/`），面向用户 | `components/collator`（[第 144 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L144)） |
   | `icu_provider_blob` | provider 机制（`provider/`），存储后端 | `provider/blob`（[第 167 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L167)） |
   | `icu_datetime_data` | baked 数据（`provider/data/`），compiled data 来源 | `provider/data/datetime`（[第 175 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L175)） |
   | `zerovec` | 通用工具（`utils/`），无 `icu_` 前缀 | `utils/zerovec`（[第 209 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L209)） |
   | `icu_experimental` | 未稳定组件，版本 0.x | `components/experimental`（[第 148 行](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L148)） |
4. 待本地验证（命令示例：`grep 'icu_collator' Cargo.toml`）。

#### 4.3.5 小练习与答案

**练习 1**：看到 `icu_provider_fs` 和 `icu_fs` 两个候选名，哪个更符合 ICU4X 命名约定？为什么？

> **答案**：`icu_provider_fs`。因为它是 provider 机制的一部分（文件系统存储后端），按约定应以 `icu_provider_` 开头。ICU4X 中没有 `icu_fs` 这种写法。

**练习 2**：为什么 `tinystr`、`litemap` 不叫 `icu_tinystr`、`icu_litemap`？

> **答案**：它们是通用 Rust 工具，不依赖国际化语义，设计上就允许被非 ICU4X 项目使用，并独立发布到 crates.io。不带 `icu_` 前缀是在向用户表达「这是一个通用库，不是 ICU4X 专有部件」。这也是为什么它们被放在 `utils/` 而非 `components/`。

**练习 3**：`icu_provider`（没有后缀）和 `icu_provider_blob` 有什么区别？

> **答案**：`icu_provider`（目录 `provider/core`）是整个数据机制的**核心抽象**（定义 `DataProvider` trait 等），几乎所有 crate 都依赖它；`icu_provider_blob`（目录 `provider/blob`）是一种**具体的存储后端**实现，从内存 blob 反序列化数据，是可插拔的「插件」之一。前者是地基，后者是建在地基上的一种实现。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**只看名字，定位一切**」的小任务，帮助你在脑子里建立完整的仓库地图。

任务：假设你正在做一个 App，需要：(a) 用西班牙语格式化日期；(b) 自定义日期数据的来源（从网络下载一个数据 blob）；(c) 想理解 ICU4X 是怎么做到「数据零拷贝加载」的。请完成下表，并给出每个 crate 在仓库里的真实目录（用根 `Cargo.toml` 验证）。

| 需求 | 该用哪个 crate（名字） | 属于哪一层 / 目录 | 在 `members` 或 `workspace.dependencies` 中的验证行 |
| --- | --- | --- | --- |
| (a) 日期格式化 | `icu_datetime`（或直接用元 crate `icu`） | 组件层 / `components/datetime` | [`Cargo.toml:146`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L146) |
| (b) 从内存 blob 加载数据 | `icu_provider_blob` | provider 层 / `provider/blob` | [`Cargo.toml:167`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L167) |
| (c) 零拷贝加载的底层原理 | `zerovec`（以及 `yoke`） | 工具层 / `utils/zerovec` | [`Cargo.toml:209`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L209) |

完成后，你应该能体会到：本讲的「目录职责 + workspace 组织 + 命名约定」三条线，足以让你面对任意一个 ICU4X crate 名字时，立刻判断它属于哪一层、解决什么问题、去仓库哪个目录读它的源码。这正是后续讲义反复用到的导航能力。

## 6. 本讲小结

- ICU4X 仓库顶层分为 `components`（组件算法）、`provider`（可插拽数据）、`utils`（零拷贝/低内存工具）、`ffi`（多语言绑定）四大核心目录，外加 `examples`、`tools`、`tutorials`、`documents` 配套目录。
- 整个仓库是一个 **Cargo workspace**：根 `Cargo.toml` 用 `members` 点名、`[workspace.package]` 共享元数据、`[workspace.dependencies]` 共享依赖版本，子 crate 用 `xxx.workspace = true` 继承。
- `examples/` 被 `exclude` 在工作区外，目的是模拟真实外部用户、验证公开 API；测试时再用 `examples/.cargo/config.toml` 的 `[patch.crates-io]` 指回本地路径。
- 共享依赖同时写 `version`（供 crates.io）和 `path`（供仓库内部），并普遍 `default-features = false`，这是 ICU4X 控制 `no_std` 与小体积的关键习惯。
- 三套命名约定：组件 `icu_*`、数据 `icu_provider_*` / `icu_*_data`（核心特例 `icu_provider`）、工具用短名无前缀；名字带 `experimental` 或版本为 0.x 表示未稳定。
- `icu` 是**元 crate**，自身无逻辑，只是把各组件 re-export 为 `icu::*` 模块，是理解「组件聚合」的最佳样本。

## 7. 下一步学习建议

下一讲 **u1-l3（搭建环境与运行第一个 ICU4X 应用）** 会带你真正动手：用 `cargo new` 创建项目、添加 `icu` 依赖、跑通一段日期格式化代码。建议在动手前：

- 先扫一眼 [`tutorials/quickstart.md`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/quickstart.md)，这是官方快速上手文档。
- 留意本讲提到的 `compiled_data` feature 与 `icu_provider_blob` 等 provider——它们会在 u1-l4（元 crate 与 feature 体系）和 u5（数据提供器系统）里被深入讲解。

如果你想现在就深入源码，推荐先读 [`components/icu/src/lib.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs) 的顶部文档（Compiled data vs Explicit data 两种数据使用方式的对比），它是理解整个 ICU4X 数据哲学的最好入口。
