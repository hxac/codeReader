# 项目全景：RustTraining 是什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清楚 microsoft/RustTraining 这个仓库的定位：**七本 mdBook 格式的 Rust 培训书 + 一个叫 xtask 的构建工具 + GitHub Pages / Docker 部署体系**。
2. 理解仓库的五级难度分类（Bridge / Deep Dive / Advanced / Expert / Practices），知道每本书为哪类读者准备。
3. 说清楚仓库的「双许可证」结构：代码走 MIT、文档走 CC-BY-4.0，并理解这对复用内容的实际影响。
4. 知道从哪里开始读源码：这个仓库里唯一的 Rust 代码是 `xtask/`，其余主体是 Markdown 内容。

本讲是整本学习手册的第一篇，不要求你写过 Rust，只要求会用命令行和浏览器。

## 2. 前置知识

本讲需要的背景概念不多，逐个用通俗语言解释：

- **培训书库（training books）**：不是一个可安装的软件产品，而是一套「用 Markdown 写成、用工具渲染成网站」的技术图书集合。读这类仓库时，你要区分「内容」（书本身）和「基础设施」（把书构建、部署成网站的工具链）。
- **mdBook**：Rust 官方生态中类似 GitBook 的静态文档生成器。你把章节写成 Markdown、在 `SUMMARY.md` 里列好目录，mdBook 就能生成一个带侧边栏导航和全文搜索的网站。本仓库七本书全部用 mdBook 组织。
- **Cargo workspace（工作空间）**：Cargo 是 Rust 的构建工具。一个 workspace 可以把多个 crate（Rust 的编译单元，类似「包」）放在一个树上统一管理。本仓库的根 `Cargo.toml` 就是一个 workspace，但它只包含一个成员——`xtask`。
- **xtask 模式**：Rust 社区流行的一种做法——把项目里的自动化脚本（构建、清理、起本地服务器）写成一个小的 Rust 程序，而不是写 Makefile 或 shell 脚本。好处是脚本本身也有类型检查。本仓库的 `xtask/` 就是这个角色，它负责批量构建七本书、生成落地页、起本地静态服务器。
- **开源许可证（License）**：规定「别人能怎样使用、修改、再分发你的成果」的法律文本。本仓库用了两份不同的许可证分别覆盖代码和文档，这是本讲 4.3 节的主题。
- **CLA（Contributor License Agreement，贡献者许可协议）**：向微软这样的组织贡献代码前要签署的协议，声明你有权授予对方使用你的贡献。本仓库由微软维护，因此有 CLA 门槛。

## 3. 本讲源码地图

本讲涉及的文件都不大，但它们是理解整个仓库的「目录页」：

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 仓库门面：许可证声明、七本书清单与五级分类、本地构建指引 | 项目定位的权威来源 |
| [Cargo.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/Cargo.toml) | workspace 定义，只有一个成员 `xtask` | 证明「仓库里唯一的 Rust 代码是构建工具」 |
| [CONTRIBUTING.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CONTRIBUTING.md) | 贡献流程：CLA 与行为准则 | 参与贡献的门槛 |
| [LICENSE](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE) | MIT 许可证全文（覆盖代码） | 双许可证的「代码」一侧 |
| [LICENSE-DOCS](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS) | CC-BY-4.0 许可证全文（覆盖文档） | 双许可证的「文档」一侧 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | 构建工具的全部源码（单文件） | 其中的 `BOOKS` 常量是七本书的「注册表」 |

阅读建议：先把 README 从头到尾读一遍（只有 111 行），再对照本讲第 4 节逐段精读。

## 4. 核心概念与源码讲解

### 4.1 README 项目定位

#### 4.1.1 概念说明

很多人第一次看到这个仓库会疑惑：它是个「Rust 项目」，为什么几乎没有 `.rs` 文件？

答案藏在 README 的第一句正文里——这是一个**文档工程**：主体内容是七本培训书，Rust 代码只服务于「把书变成网站」这一件事。理解这一定位非常重要，因为它决定了后续所有讲义的读法：

- 想学 **Rust 语言本身** → 阅读对象是七本书的内容（单元三）。
- 想学 **工程实践** → 阅读对象是 xtask 源码、CI 流水线、Docker 配置（单元二、四）。

README 同时明确了这套材料的性质：它是培训教材，**不是权威参考**，关键细节要以官方文档为准。这个「免责声明」值得注意——它告诉你本仓库的教学定位是「体系化入门与进阶」，而不是语言律师手册。

#### 4.1.2 核心流程

一个新访客消费这个仓库的典型路径：

```text
看到 README
   │
   ├─ 在线阅读 ──→ GitHub Pages 站点 ──→ 按「背景」选书 ──→ 进入某本书
   │
   └─ 本地克隆 ──→ cargo install mdbook mdbook-mermaid
                     ──→ cargo xtask serve ──→ http://localhost:3000
```

维护者的路径则是（README「For Maintainers」一节）：

```text
cargo xtask build    # 把七本书构建到 site/（本地预览用）
cargo xtask serve    # 构建并在 3000 端口起静态服务器
cargo xtask deploy   # 构建到 docs/（GitHub Pages 用）
cargo xtask clean    # 删除 site/ 与 docs/
```

这四个子命令的具体实现在单元二精读；本讲只需要记住「仓库自带一套用 Rust 写的构建工具」。

#### 4.1.3 源码精读

**定位句**。[README.md:L13-L15](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L13-L15) 给出仓库的一句话定义：七门培训课程，覆盖从不同编程背景进入 Rust 的路径，外加 async、高级模式与工程实践的深潜。

**内容性质声明**。[README.md:L17-L19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L17-L19) 说明这套材料融合了原创内容与社区最佳资源的思路，并明确「是培训材料而非权威参考，关键细节请对照官方文档验证」。

**内容来源致谢**。[README.md:L21-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L21-L33) 列出了《The Rust Programming Language》、Jon Gjengset、withoutboats、Mara Bos 等社区资源。这份清单本身就是一份高质量的 Rust 进阶阅读地图，值得收藏。

**在线与本地两条入口**。[README.md:L59-L67](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L59-L67) 给出 GitHub Pages 在线地址和四行命令的本地预览流程；[README.md:L80-L82](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L80-L82) 在维护者章节把工具版本钉死为 `mdbook@0.4.52` 和 `mdbook-mermaid@0.14.0`——钉版本是为了避免预处理器行为漂移导致构建不一致。

**「唯一的 Rust 代码」证据**。根 [Cargo.toml:L1-L3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/Cargo.toml#L1-L3) 定义了一个 workspace，`members = ["xtask"]` 说明整个 Rust 工作空间只有 xtask 一个 crate：

```toml
[workspace]
resolver = "2"
members = ["xtask"]
```

配合磁盘目录也能印证：仓库顶层是七个书籍目录（`c-cpp-book/`、`csharp-book/`、`python-book/`、`async-book/`、`rust-patterns-book/`、`type-driven-correctness-book/`、`engineering-book/`）加上 `xtask/`、`docker/`、`.github/`，没有任何 `src/`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 README 的两个说法——「每本书有 15–16 章」以及「仓库唯一的 Rust 代码在 xtask」。

**操作步骤**：

1. 如果尚未克隆仓库：

   ```bash
   git clone https://github.com/microsoft/RustTraining.git
   cd RustTraining
   ```

2. 统计某本书的章节数（以 async-book 为例）：

   ```bash
   ls async-book/src/ch*.md | wc -l
   ls async-book/src/
   ```

3. 确认 Rust 代码的位置：

   ```bash
   find . -name "*.rs" -not -path "./target/*" -not -path "./RustTraining-tutorial/*"
   ```

**需要观察的现象**：

- 第 2 步会列出 `ch00-introduction.md` 到 `ch17-capstone-project.md`。实际数出来是 **18 个** `ch*.md` 文件——比 README 说的「15–16 章」略多，因为编号中还包含导言（ch00）、练习（ch15）、参考卡（ch16）和 capstone 项目（ch17）这类非正文章节。README 的说法是约数。
- 第 3 步的结果应只有一个文件：`./xtask/src/main.rs`（`site/`、`docs/` 若已构建会被 `-not -path "./target/*"` 之外的规则排除在源码之外；若你还没构建过，它们根本不存在）。

**预期结果**：你会直观确认「这是一个以 Markdown 内容为主体、以单个 Rust 文件为构建工具」的仓库结构。命令输出与上述描述如有出入（例如未来仓库新增了 Rust 代码），以实际输出为准。

**待本地验证**：以上命令未在本讲义中代跑，请在本地执行并记录你的实际输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个仓库要用 Cargo workspace，而不是把 xtask 写成 shell 脚本？

**参考答案**：xtask 模式让构建脚本本身享受 Rust 的类型检查、包管理和错误处理，避免 shell 脚本随规模膨胀后的脆弱性；同时 workspace 让 `cargo xtask` 这样的别名可以无缝融入 Cargo 工作流（详见讲义 u2-l1）。对这个仓库而言，构建逻辑包括批量调用子进程、生成 HTML、手写 HTTP 服务器，用 Rust 写远比 shell 可靠。

**练习 2**：README 为什么强调「这些书是培训材料，不是权威参考」？

**参考答案**：这是对内容边界的诚实声明。Rust 语言演进快，教程成文后细节可能过时；作者希望读者把这套书当作体系化学习的骨架，而在生产决策时回到官方文档和语言参考确认关键细节。这同时降低了内容维护的责任边界。

**练习 3**：README 把 mdbook 版本钉在 `0.4.52`，不钉版本会怎样？

**参考答案**：mdbook 与 mdbook-mermaid 是「预处理器 + 主程序」的插件关系，两者跨版本可能不兼容；不钉版本时，不同时间安装的贡献者会得到不同行为，出现「我这能构建你那不能」的问题。钉版本保证了所有人和 CI 在同一套工具链下构建。

### 4.2 七本书清单与五级分类

#### 4.2.1 概念说明

仓库的核心资产是七本书。它们不是随意罗列，而是被组织成一个**五级难度体系**，让不同背景的读者能定位自己的起点：

| 级别 | 含义 | 适合谁 |
|------|------|--------|
| 🟢 **Bridge（桥梁）** | 从其他语言迁移到 Rust 的入门通道 | 刚接触 Rust 的人，按原语言选书 |
| 🔵 **Deep Dive（深潜）** | 对某个子系统做聚焦式深入 | 已入门、想吃透一个主题的人 |
| 🟡 **Advanced（进阶）** | 面向有经验 Rustacean 的模式与技巧 | 写了一段时间 Rust 的人 |
| 🟣 **Expert（专家）** | 前沿的类型层正确性技术 | 深度使用者、库作者 |
| 🟤 **Practices（实践）** | 工程化、工具链与生产就绪 | 要把 Rust 推向生产的人 |

七本书中有**三本 Bridge 书**，分别对应 C/C++、C#（兼顾 Swift/Java）、Python 三类背景——这是整个体系的设计重心：先解决「从哪来」的问题，再谈「到哪去」。

#### 4.2.2 核心流程

五级分类不只出现在 README 的表格里，它同时被**编码进了构建系统**。xtask 源码里有一个 `BOOKS` 常量，每个条目的第四个字段就是级别（category）：

```text
README 表格（人类阅读的说明）
        ▲
        │ 同步维护
        ▼
xtask 的 BOOKS 常量（机器消费的数据）
        │
        ├─ category_label() 把 "bridge" 映射为展示标签
        └─ 落地页生成时按级别渲染不同颜色的卡片
```

这形成了一个值得学习的工程习惯：**同一份元数据，文档侧用表格给人看，代码侧用常量给机器用**。新增一本书时两处都要更新（讲义 u4-l4 的实战主题）。

#### 4.2.3 源码精读

**README 的五级定义表**。[README.md:L39-L45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L39-L45) 定义了 Bridge/Deep Dive/Advanced/Expert/Practices 五个级别的含义。

**README 的七本书总表**。[README.md:L47-L55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55) 给出每本书的名称、级别与目标读者，整理如下（加入磁盘目录名方便对照）：

| 书名 | 目录 | 级别 | 目标读者/主题 |
|------|------|------|---------------|
| Rust for C/C++ Programmers | `c-cpp-book/` | 🟢 Bridge | 移动语义、RAII、FFI、嵌入式、no_std |
| Rust for C# Programmers | `csharp-book/` | 🟢 Bridge | Swift / C# / Java 背景 → 所有权与类型系统 |
| Rust for Python Programmers | `python-book/` | 🟢 Bridge | 动态类型 → 静态类型、无 GIL 并发 |
| Async Rust | `async-book/` | 🔵 Deep Dive | Tokio、流、取消安全 |
| Rust Patterns | `rust-patterns-book/` | 🟡 Advanced | Pin、分配器、无锁结构、unsafe |
| Type-Driven Correctness | `type-driven-correctness-book/` | 🟣 Expert | 类型状态、幻影类型、能力令牌 |
| Rust Engineering Practices | `engineering-book/` | 🟤 Practices | 构建脚本、交叉编译、CI/CD、Miri |

**每本书的标准配置**。[README.md:L57](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L57) 说明每本书都带 Mermaid 图、可编辑的 Rust playground、练习和全文搜索——这是七本书统一的写作范式（讲义 u3-l2 深入拆解）。

**构建系统侧的注册表**。[xtask/src/main.rs:L8-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) 的 `BOOKS` 常量以 `(slug, title, description, category)` 四元组注册了同样的七本书：

```rust
/// (slug, title, description, category)
const BOOKS: &[(&str, &str, &str, &str)] = &[
    (
        "c-cpp-book",
        "Rust for C/C++ Programmers",
        "Move semantics, RAII, FFI, embedded, no_std",
        "bridge",
    ),
    // ……共七条，与 README 表格一一对应
];
```

slug 字段就是磁盘上的目录名——这就是「目录与构建系统之间的契约」：只新建目录而不注册到 `BOOKS`，书就不会被构建和展示（讲义 u1-l2、u4-l4 展开）。

#### 4.2.4 代码实践

**实践目标**：交叉验证 README 表格与 `BOOKS` 常量的一致性，体验「文档元数据 vs 代码元数据」的双源维护。

**操作步骤**：

1. 打开 [README.md:L47-L55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55) 的书籍总表。
2. 打开 [xtask/src/main.rs:L9-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L9-L52) 的 `BOOKS` 常量。
3. 逐行比对七本书的：slug ↔ 目录名、title ↔ 书名、description ↔ 目标读者描述、category ↔ 级别。
4. 验证磁盘真实目录：`ls -d */ | grep book`。

**需要观察的现象**：

- 七个 slug 与七个磁盘目录一一对应，无多余、无缺失。
- 你会发现**细微不一致**：例如 README 表格里 async-book 的书名写作 "Async Rust"，而 `BOOKS` 常量里的 title 是 `"Async Rust: From Futures to Production"`（见 [xtask/src/main.rs:L28-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L28-L33)）；csharp-book 在 README 中的读者描述与常量里的 description（"Best for Swift / C# / Java developers"）措辞也不完全相同。落地页显示的是常量里的版本。

**预期结果**：你会得出结论——两处数据「语义一致、字面有出入」，这正是双源维护的典型风险点，也是单元四实战中要留意的细节。

**待本地验证**：请以你本地检出的 HEAD 为准重新比对一次，确认上述不一致在你的版本中仍然存在。

#### 4.2.5 小练习与答案

**练习 1**：一位写了 8 年 Python、从没碰过静态类型语言的数据工程师应该从哪本书开始？之后的合理路线是什么？

**参考答案**：从 Rust for Python Programmers（python-book）开始——它专为「动态类型 → 静态类型」的迁移设计，会借助 Python 概念解释所有权和类型系统。之后按需选择：要做网络服务就读 Async Rust；要写命令行工具并发布可交给 Rust Engineering Practices；对类型系统特别感兴趣再进 Type-Driven Correctness。

**练习 2**：为什么仓库提供三本 Bridge 书而不是一本通用的「Rust 入门」？

**参考答案**：因为学习者最大的障碍是「已有心智模型的迁移」：C++ 程序员需要的是「移动语义在 Rust 里如何变得安全」，Python 程序员需要的是「为什么编译器要跟我过不去」。按背景分书能让每本书大胆使用源语言的类比（如用 RAII 解释 Drop、用 GIL 对比解释 Rust 并发模型），比一本泛泛的入门书教学效率高得多。

**练习 3**：`BOOKS` 常量的四个字段中，哪个字段决定了落地页卡片的颜色分类？

**参考答案**：第四个字段 category（取值 `bridge` / `deep-dive` / `advanced` / `expert` / `practices`）。xtask 中的 `category_label()` 函数（[xtask/src/main.rs:L170](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L170)）把它映射为展示标签，落地页再按级别渲染不同的分类颜色（讲义 u2-l4 详解）。

### 4.3 双许可证结构与贡献门槛

#### 4.3.1 概念说明

这个仓库最容易被忽视但最重要的设计之一，是**双许可证（dual licensing）**：

- **代码（xtask 等）** → [MIT License](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE)：极其宽松，保留版权声明即可自由使用、修改、再分发，包括商用。
- **文档（七本书的内容）** → [CC-BY-4.0](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS)：同样宽松，但核心义务是**署名（Attribution）**——复制或改编内容时必须注明来源和原作者。

为什么要分开？因为代码和文档的「复用方式」不同：代码会被拷进别人的项目编译，MIT 的简短条款最合适；文档会被摘抄、翻译、改编成课程，CC-BY-4.0 是内容领域的事实标准，署名义务能保证学术与教学传播中的来源可追溯。

另外两个相关概念：

- **商标保留**：微软的商标和 logo 不随这两份许可证授予，使用需遵循微软商标指南。
- **CLA**：向仓库提交 PR 时需要签署的贡献者协议（见 4.3.3）。

#### 4.3.2 核心流程

一个想**复用内容**的人的合规判断流程：

```text
我想用仓库里的东西
   │
   ├─ 是 xtask 源码 / 配置文件？
   │      └─ 是 → 按 MIT：保留 LICENSE 版权声明即可
   │
   ├─ 是书里的章节文字 / 图 / 示例讲解？
   │      └─ 是 → 按 CC-BY-4.0：注明出处与作者
   │
   └─ 涉及微软 logo / 商标？
          └─ 是 → 不在许可范围内，另循商标指南
```

一个想**贡献代码**的人的流程：

```text
fork 仓库 → 改动 → 提 PR
   │
   └─ CLA-bot 自动检查是否已签 CLA
          ├─ 未签 → 按 bot 提示到 cla.microsoft.com 签署（一次签署，全仓库通用）
          └─ 已签 → PR 进入正常评审
```

#### 4.3.3 源码精读

**README 顶部的许可证声明**。[README.md:L1-L5](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L1-L5) 用醒目的样式块声明：本项目在 MIT 与 CC-BY-4.0 下双重许可，并分别链接到两份许可证文件。紧随其后的 [README.md:L7-L11](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L7-L11) 是商标声明——微软商标的使用须遵循微软商标与品牌指南，修改后的版本不得造成混淆或暗示微软赞助。

**MIT 侧**。[LICENSE:L1-L3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE#L1-L3) 开头即「MIT License / Copyright (c) Microsoft Corporation.」，正文是标准 MIT 条款：免费授予使用、复制、修改、合并、发布、分发、再许可、销售的权利，条件是保留版权与许可声明。

**CC-BY-4.0 侧**。[LICENSE-DOCS:L1](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L1) 标题「Attribution 4.0 International」——「BY」就是署名义务。这是知识共享组织维护的标准全文，核心授予条件是：分发许可材料时须给出适当署名、标注许可协议链接、注明是否做过修改。

**贡献门槛**。[CONTRIBUTING.md:L3-L10](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CONTRIBUTING.md#L3-L10) 说明：多数贡献需要同意 CLA；提交 PR 后 CLA-bot 会自动判断并给出指引，只需按提示操作一次。[CONTRIBUTING.md:L12-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CONTRIBUTING.md#L12-L14) 指明项目采用微软开源行为准则。

#### 4.3.4 代码实践

**实践目标**：通过阅读两份许可证的原文关键段落，建立「复用前先查许可证」的肌肉记忆。

**操作步骤**：

1. `cat LICENSE | head -10`——确认这是标准 MIT，版权主体是 Microsoft Corporation。
2. `head -5 LICENSE-DOCS`——确认标题是「Attribution 4.0 International」。
3. 做一个思想实验并写下答案：假如你想把 python-book 的某一章翻译成中文发到自己的博客，需要做什么才合规？假如你想把 xtask 的静态服务器代码抄进自己的项目呢？

**需要观察的现象**：

- LICENSE 全文只有 22 行左右，没有附加条款；LICENSE-DOCS 是知识共享的标准长文本。
- 思想实验的答案应能从许可证文本中直接推导，而不是靠猜。

**预期结果**：

- 翻译章节（文档，CC-BY-4.0）：注明原出处（microsoft/RustTraining 与原作者）、标注 CC-BY-4.0 协议链接、注明「由原文翻译/修改」。CC-BY-4.0 不限制演绎作品使用更宽松的分享方式，但署名义务不可省。
- 抄 xtask 代码（MIT）：在你的项目里保留那份 MIT 版权与许可声明（通常放进 THIRD_PARTY_NOTICES 或随附 LICENSE 副本）。

以上是按许可证文本的一般性解读，不构成法律意见；正式使用前请阅读原文。

**待本地验证**：无（纯阅读实践），但请确保实际打开过两份许可证文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么代码用 MIT、文档用 CC-BY-4.0，而不是两边都用 MIT？

**参考答案**：MIT 是为软件设计的简短许可，法律社区对它覆盖「文档/图文内容」的惯例不如 CC 系列成熟；CC-BY-4.0 是内容分享的事实标准，其署名机制天然适合「被摘抄、改编、翻译」的教学材料。分开授权让两类资产各自处在最被广泛理解的法律框架下，降低复用者的合规不确定性。

**练习 2**：你把仓库 fork 后大改了书的内容并自建站点发布，README 顶部的商标声明对你有什么约束？

**参考答案**：你的修改版本中不得以会造成混淆或暗示微软赞助的方式使用微软商标与 logo。也就是说内容按 CC-BY-4.0 可以自由改编发布，但品牌标识不在授权范围内。

**练习 3**：签署 CLA 时「只需要做一次」是什么意思？

**参考答案**：微软的 CLA 是按贡献者（自然人或组织）签署一次即在其所有适用仓库生效的协议，不是每个仓库、每个 PR 都要重签。CLA-bot 会自动识别你在其他仓库的签署状态。

## 5. 综合实践

**任务**：为「未来的你」制定一份基于本仓库的 Rust 学习入口方案。这是贯穿本讲三个模块（项目定位、书籍体系、许可证）的收尾任务。

**要求**：

1. 克隆仓库并完整通读一遍 [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md)（如果还没做，先完成 4.1.4 的实践）。
2. 制作一张「七书速查表」，包含四列：**书名、级别（用五级体系标注）、目标读者、一句话内容概括**。数据来源以 [README.md:L47-L55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55) 为主，并与 `BOOKS` 常量交叉核对（提示：留意 4.2.4 发现的字面差异，在表下加一行备注）。
3. 根据你自己的编程背景，从七本书中选出你的**入口书**，用 3–5 句话写出选择理由。理由必须引用该书的「目标读者/主题」描述（例如 Python 背景就引用 "Dynamic → static typing, GIL-free concurrency" 并解释这为什么匹配你）。
4. 再选出你的**第二本书**（入口书读完后的去向），说明从入口到第二本的递进关系（例如 Bridge → Deep Dive 是「入门后专攻子系统」）。
5. 最后写一句「合规备忘」：如果学习过程中你想把某章笔记整理后公开发表，依据 4.3 的结论你需要保留什么。

**预期产出**：一份 Markdown 文档（可以就叫 `my-learning-plan.md`，放在仓库外或 `RustTraining-tutorial/` 之外的任何地方，不要提交到仓库），包含速查表、入口书理由、第二本书理由和合规备忘。

**检验标准**：速查表有 7 行且级别与 README 一致；入口书理由确实结合了自身背景与该书定位；合规备忘提到了署名/保留声明义务。

## 6. 本讲小结

- **RustTraining 是一个文档工程仓库**：七本 mdBook 培训书是主体，唯一的 Rust 代码是 `xtask/` 构建工具（根 Cargo.toml 的 workspace 只有一个成员），外加 GitHub Pages 与 Docker 两套部署。
- **五级分类构成学习路线的骨架**：三本 Bridge 书按 C/C++、C#、Python 背景分流初学者，Deep Dive / Advanced / Expert / Practices 四本书覆盖专攻方向；每本书带 Mermaid 图、可运行 playground 和练习。
- **书籍元数据有两份**：README 的表格给人读，`BOOKS` 常量给机器读，二者语义一致但存在字面差异——新增书籍时两处都要同步。
- **双许可证**：代码走 MIT（保留声明即可自由使用），文档走 CC-BY-4.0（复用需署名），微软商标不在授权范围内。
- **贡献有 CLA 门槛**：提 PR 会被 CLA-bot 检查，首次贡献按提示到 cla.microsoft.com 签署一次即可。

## 7. 下一步学习建议

下一讲（u1-l2「目录结构：一个文档工程仓库的布局」）会逐个打开顶层目录，讲清楚 `xtask/`、`docker/`、`.github/`、`.cargo/` 各自的职责，并深入 `BOOKS` 常量与磁盘目录的契约关系。

在进入下一讲之前，建议你先自己做两件事：

1. 用浏览器打开 [GitHub Pages 在线站点](https://microsoft.github.io/RustTraining/)，点进你的入口书随便翻两章，对「这套书长什么样」建立直观印象。
2. 通读 [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) 的前 60 行，不要求看懂，只数一数 `BOOKS` 里的七个条目——单元二将从这个文件开始逐函数精读。
