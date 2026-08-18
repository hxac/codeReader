# 仓库与 Crate 组织:243 个盒子如何协作

## 1. 本讲目标

学完本讲,你应该能够:

1. 看懂根 `Cargo.toml` 中 `members` 列表的三块结构(crates / extensions / tooling),并能说出 `default-members` 的作用(承接 u1-l2 的结论)。
2. 按功能领域给 243 个 crate 大致归类(UI、编辑器、语言、AI、协作、终端、Git 等),拿到一个陌生 crate 名字时能猜到它属于哪一层。
3. 通过阅读某个 crate 的 `Cargo.toml` 依赖段,判断它在整个依赖图中处于底层、中层还是顶层。
4. 会用 `cargo metadata` 与 `cargo tree` 两个命令自己探索这个庞大的 workspace。

## 2. 前置知识

本讲不写业务代码,但需要你先建立三个概念。

**概念一:crate 是什么?**

在 Rust 世界里,crate 是编译的基本单位。一个 crate 可以是一个「库」(被别人依赖,比如 `rope`),也可以是一个「二进制」(自己能跑起来,比如 `zed` 和 `collab`)。每个 crate 由一个目录加一个 `Cargo.toml`(清单文件)描述:我叫什么名字、版本是多少、我依赖谁。可以把 crate 理解成乐高盒子——每个盒子独立封装,盒子之间只能通过「依赖」关系拼装。

**概念二:依赖图是有方向的。**

如果 A 的 `Cargo.toml` 里写了依赖 B,就说「A 依赖 B」,画一条从 A 指向 B 的箭头。Rust 明确禁止循环依赖(A 依赖 B、B 又依赖 A 是编译不过的),所以整个 workspace 的依赖关系构成一张**有向无环图**(DAG)。方向决定了分层:被很多人依赖、自己几乎不依赖别人的,是底层;依赖一大堆别人、被很少人依赖的,是顶层。

**概念三:workspace 是 crate 的「宿舍楼」。**

u1-l2 已经讲过:Zed 用一个 Cargo workspace 把 250 个成员(crate)放进同一栋楼,共用一份依赖版本表和编译配置。本讲从「怎么构建」的视角切换到「地图」的视角——这栋楼里有哪些房间、房间怎么分组、谁住在楼上谁住在楼下。

> 阅读前提:你已经完成 u1-l1(知道仓库顶层有哪些文件)和 u1-l2(知道 `default-members = ["crates/zed"]` 意味着裸 `cargo run` 只构建编辑器主程序)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`(仓库根) | workspace 总清单:成员列表、共享依赖表、补丁、编译 profile、lints |
| `crates/zed/Cargo.toml` | 编辑器主程序 crate 的清单,是观察「顶层如何组装底层」的最佳窗口 |
| `docs/src/development/glossary.md` | 官方术语表,把 crate 名和工作中的概念(Worktree、Project、Buffer)对应起来 |
| `crates/{rope,language,worktree,project,editor,vim,terminal,agent,collab}/Cargo.toml` | 10 个代表性 crate 的清单,用于本讲的分层练习 |

## 4. 核心概念与源码讲解

### 4.1 workspace 成员浏览:三块大陆

#### 4.1.1 概念说明

workspace 的 `members` 字段是一份**手工维护的名单**,列出了所有参与统一构建的 crate 目录。Zed 的这份名单分成三块「大陆」:

1. **crates 大陆**:编辑器产品的全部代码,243 个条目(对应 `crates/` 目录下的 243 个子目录)。
2. **extensions 大陆**:以独立扩展形式开发的语言支持(glsl、html、proto 等),4 个条目。
3. **tooling 大陆**:开发工具(compliance 合规检查、perf 性能采集、xtask 构建辅助脚本),3 个条目。

注意:members 名单**不会自动生长**。新建一个 `crates/my_thing/` 目录并不会让它变成成员,必须手动把 `"crates/my_thing"` 加进名单——这是给 Zed 贡献新 crate 时的必要步骤之一。

#### 4.1.2 核心流程

Cargo 在解析 workspace 时的逻辑可以概括为:

1. 读根 `Cargo.toml` 的 `[workspace]` 表。
2. 收集 `members` 列出的所有目录,每个目录下的 `Cargo.toml` 就是一个成员 crate。
3. 此外,凡是被成员以 path 方式依赖、且位于 workspace 目录内的 crate,也会被纳入构建图(即使没写进名单)。
4. 遇到 `cargo build` 这类不指定包名的命令时,只对 `default-members` 里列出的 crate 操作——Zed 只列了 `crates/zed` 一个。

#### 4.1.3 源码精读

先看 workspace 表的开头和结尾:

- [Cargo.toml:L1-L3](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1-L3) — `[workspace]` 表从这里开始,`resolver = "2"` 声明使用第二版特性解析器(更严格的 target 相关特性合并规则)。
- [Cargo.toml:L4-L246](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L4-L246) — members 的第一大块:按**字母序**排列的 243 条 `crates/*` 路径。字母序意味着相邻行之间没有领域含义,分类要靠后文的方法(见 4.2),而不是靠列表分段。
- [Cargo.toml:L248-L255](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L248-L255) — 注释 `# Extensions` 之下是 4 个扩展成员:`extensions/glsl`、`extensions/html`、`extensions/proto`、`extensions/test-extension`。`extensions/` 目录下其实还有一个 `workflows` 目录,但它**没有**列进 members——它不参与这个 workspace 的构建。
- [Cargo.toml:L257-L263](https://github.com/zed-industries-zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L257-L263) — 注释 `# Tooling` 之下的三个工具成员:`tooling/compliance`、`tooling/perf`、`tooling/xtask`。
- [Cargo.toml:L265](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L265) — `default-members = ["crates/zed"]`,u1-l2 已详细讲过:裸 `cargo run` / `cargo build` 只构建编辑器主程序。

两个值得注意的「名单外的成员」:

- [Cargo.toml:L317](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L317) — `copilot_ui = { path = "crates/copilot_ui" }`。`crates/copilot_ui` 目录真实存在,却没有出现在 members 名单里,只登记在共享依赖表中;它通过 path 依赖被 `zed` 引用,从而进入构建图。
- [Cargo.toml:L1127-L1132](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1127-L1132) — 注释解释了 `tooling/lints`(自定义 dylint 规则包)为什么**故意**留在 workspace 之外:它需要钉住自己的 nightly 工具链。

#### 4.1.4 代码实践

**实践目标**:不用肉眼扫描,用 Cargo 自己的元数据接口拿到成员清单。

**操作步骤**:

1. 在仓库根目录执行:

   ```bash
   cargo metadata --no-deps --format-version 1 | jq -r '.workspace_members | length'
   ```

2. 再执行下面这条,把成员按目录前缀分组计数:

   ```bash
   cargo metadata --no-deps --format-version 1 \
     | jq -r '.workspace_members[]' \
     | sed 's/ .*//' \
     | awk -F/ '{print $1"/"$2}' | sort | uniq -c | sort -rn | head
   ```

3. 对照根 `Cargo.toml` 手数一遍 members 条目数。

**需要观察的现象**:第一条命令输出一个数字(预期在 250 上下:crates 区 243 条 + extensions 4 条 + tooling 3 条)。

**预期结果**:命令输出的数字应与你手数的条目数一致。具体数值「待本地验证」——如果略有出入,想一想 4.1.2 提到的「path 依赖隐式入图」规则,再检查 `copilot_ui` 这类目录是否被计入。

#### 4.1.5 小练习与答案

**练习 1**:你想给 Zed 新增一个 crate `crates/my_widget`,只创建目录和 `Cargo.toml` 就够了吗?

> **答案**:不够。还需要 (1) 在根 `Cargo.toml` 的 `members` 里加一行 `"crates/my_widget"`;(2) 在 `[workspace.dependencies]` 里登记 `my_widget = { path = "crates/my_widget" }`,这样别的 crate 才能用 `my_widget.workspace = true` 引用它。

**练习 2**:`cargo build`(不带 `-p`)会编译 `tooling/xtask` 吗?会编译 `crates/agent` 吗?

> **答案**:都不会。`default-members` 只有 `crates/zed`,`cargo build` 只构建它(及它的依赖闭包)。`xtask` 不在 zed 的依赖闭包里,要显式 `cargo build -p xtask`;`agent` 虽然在闭包里会被顺带编译,但构建目标(默认目标)是 `zed` 这个二进制。

### 4.2 crate 分类:给 243 个盒子贴标签

#### 4.2.1 概念说明

243 个 crate 若不加分类,就是一锅字母粥。好消息是:Zed 的 crate 命名相当规律,后缀和前缀往往直接暴露领域:

- `_ui` 后缀 → 某功能的界面层(`git_ui`、`agent_ui`、`extensions_ui`)。
- `_store` 后缀 → 状态仓储(glossary 的命名约定:`NameStore` 抽象「本地还是远程」的差异)。
- 提供商名 → 某家 LLM/服务商的接入(`anthropic`、`open_ai`、`ollama`、`google_ai`…)。
- `gpui*` 前缀 → UI 框架家族(`gpui_macros`、`gpui_platform`、`gpui_linux`…)。

分类的目的是建立**检索索引**:当你想改「项目树面板」,能立刻猜到去 `project_panel`;想看「补全请求怎么发出」,能定位到 `editor` + `project`。

#### 4.2.2 核心流程

给陌生 crate 归类的操作流程:

1. 看名字猜领域(用上面的前后缀规律)。
2. 打开它的 `Cargo.toml`,看它依赖谁、被谁依赖 → 判断层次(4.3 的方法)。
3. 打开它的库根文件(如 `crates/rope/src/rope.rs`)看模块列表 → 确认职责。
4. 拿不准时查 glossary 或在仓库里 Grep 该 crate 名,看谁在 `use` 它。

#### 4.2.3 源码精读

**第一份材料:共享依赖表就是「内部电话簿」。**

- [Cargo.toml:L271-L275](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L271-L275) — `[workspace.dependencies]` 表开头,注释把它明确分成「Workspace member crates」和「External crates」两节。
- [Cargo.toml:L277-L508](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L277-L508) — 内部成员节:每个 crate 一行 `名字 = { path = "crates/名字" }`。想快速浏览「这个仓库自己造了哪些轮子」,扫这一节比扫 243 个目录快得多。
- [Cargo.toml:L510-L904](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L510-L904) — 外部依赖节,版本全部钉死在这里,各成员 crate 通过 `foo.workspace = true` 继承,保证全仓版本一致(u1-l2 已讲)。

**第二份材料:顶层采购清单。**

- [crates/zed/Cargo.toml:L1-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/Cargo.toml#L1-L9) — `zed` crate 的包描述:"The fast, collaborative code editor."。它就是产品本体。
- [crates/zed/Cargo.toml:L56-L59](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/Cargo.toml#L56-L59) — `[[bin]]` 段声明二进制名 `zed`、入口 `src/main.rs`(u1-l4 将逐行读它)。
- [crates/zed/Cargo.toml:L65-L231](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/Cargo.toml#L65-L231) — 依赖段:几乎全是 `xxx.workspace = true` 形式的内部依赖,数量超过 140 个。**这份列表就是一张功能清单**——编辑器、项目、git_ui、agent、vim、terminal_view、command_palette……产品有什么能力,这里就引用什么 crate。

**第三份材料:官方术语表把 crate 和概念对上号。**

- [docs/src/development/glossary.md:L21-L27](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L21-L27) — 命名约定:`AnyName` 是类型擦除版本,`NameStore` 抽象本地/远程差异。读 crate 名字时的解码器。
- [docs/src/development/glossary.md:L89-L91](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L89-L91) — `Project` = 一个或多个 `Worktree`;`Worktree` = 本地或远程文件的表示。这两个概念分别对应 `project` 和 `worktree` 两个 crate(本手册 u4 展开)。
- [docs/src/development/glossary.md:L100-L103](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L100-L103) — `Buffer` 是「文件」的内存表示,含语法树、git 状态、诊断,对应 `language` crate 中的核心类型(u3 展开)。

**按领域的分类速查表**(节选代表性 crate,按本讲阅读的成员名单整理):

| 领域 | 代表 crate |
| --- | --- |
| 框架底座 | `gpui` 及 `gpui_*` 家族、`collections`、`util`、`path`、`paths`、`fs`、`clock` |
| 数据结构原语 | `rope`、`sum_tree`、`text`、`refineable` |
| 语言与语法 | `language`、`language_core`、`languages`、`grammars`、`lsp`、`syntax_theme` |
| 项目与工作区 | `worktree`、`project`、`workspace`、`buffer_diff`、`multi_buffer` |
| 编辑器本体 | `editor`、`editor_benchmarks`、`search`、`snippet` |
| UI 组件与外观 | `ui`、`component`、`theme`、`icons`、`file_icons`、`picker`、`panel`、`sidebar` |
| 功能面板 | `project_panel`、`outline_panel`、`git_ui`、`extensions_ui`、`tasks_ui`、`settings_ui` |
| 命令与键位 | `command_palette`、`command_palette_hooks`、`keymap_editor`、`which_key` |
| Git | `git`、`git_ui`、`git_ui_core`、`git_hosting_providers` |
| 终端 | `terminal`、`terminal_view` |
| Vim 模式 | `vim`、`vim_mode_setting` |
| AI / Agent | `agent`、`agent_ui`、`agent_servers`、`acp_thread`、`language_models`、`edit_prediction`、各家提供商(`anthropic`、`open_ai`、`ollama`…) |
| 协作与网络 | `rpc`、`proto`、`client`、`collab`、`collab_ui`、`call`、`channel`、`remote`、`remote_server`、`cloud_api_client` |
| 扩展系统 | `extension`、`extension_api`、`extension_host`、`extension_cli`、`theme_extension`、`language_extension` |
| 设置与基础服务 | `settings`、`settings_json`、`settings_macros`、`db`、`sqlez`、`session`、`telemetry`、`release_channel`、`feature_flags` |
| 调试器 | `dap`、`dap_adapters`、`debugger_ui`、`debugger_tools`、`repl` |
| 工具与基准 | `benchmarks`、`*_benchmarks`、`eval_cli`、`migrator`、`schema_generator`、`tooling/{compliance,perf,xtask}` |

#### 4.2.4 代码实践

**实践目标**:用命令量化「顶层 crate 的扇入扇出」,体会 `zed` 作为组装层的地位。

**操作步骤**:

1. 统计 `zed` 清单里出现 `workspace = true` 的次数(含全部依赖段):

   ```bash
   grep -c 'workspace = true' crates/zed/Cargo.toml
   ```

2. 对比一个底层 crate:

   ```bash
   grep -c 'workspace = true' crates/rope/Cargo.toml
   ```

3. 用 cargo 观察从 `zed` 出发往下一层的内部依赖:

   ```bash
   cargo tree -p zed --depth 1
   ```

**需要观察的现象**:第 1 步应输出三位数(本讲写作时实测为 194,涵盖主依赖、开发依赖与平台条件依赖),第 2 步是个位数(rope 的内部依赖极少)。

**预期结果**:顶层与底层的依赖条目数相差两个数量级。第 3 步的完整输出与机器平台有关(Windows/Linux 条件依赖不同),「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**:`settings_ui`、`settings_json`、`settings_macros` 三个 crate 各自大概做什么?

> **答案**:按命名规律推断——`settings_ui` 是设置界面(`_ui` 后缀);`settings_json` 处理 settings.json 文件模型;`settings_macros` 提供 `settings!` 这类过程宏(`_macros` 后缀,配合 `derive` 使用)。三者同属「设置与基础服务」族,具体分工在 u7-l1 展开。

**练习 2**:想找「命令面板」的实现代码,应该去哪个 crate?依据是什么?

> **答案**:`command_palette`。依据:成员名单里直接有这个名字;同时 `zed` 的依赖列表里有 `command_palette.workspace = true`,说明它被产品直接引用。也可以从 `crates/command_palette/src/` 入手确认。

**练习 3**:为什么 `anthropic`、`ollama`、`google_ai` 这些要各自独立成 crate,而不是合并在 `agent` 里?

> **答案**:它们是同一抽象(语言模型提供商)的平行实现,彼此独立、互不依赖;拆成独立 crate 可以让 `language_models` 统一按需引用,避免单一巨型 crate,也让每家的迭代互不干扰。这是「按变化方向切分」的典型做法。

### 4.3 依赖方向:判断一个 crate 的层次

#### 4.3.1 概念说明

依赖图是 DAG,而「分层」就是给 DAG 里的节点定高度。一个直觉化的定义: crate 的层数等于它到最底层依赖的最长路径长度:

\[ L(c) = \begin{cases} 0, & c \text{ 没有内部依赖} \\ 1 + \max_{d \in \mathrm{deps}(c)} L(d), & \text{否则} \end{cases} \]

层数低 = 被所有人踩在脚下(改动它影响面巨大,但它自己很稳定);层数高 = 站在很多盒子之上组装功能(改动通常只影响自己)。**读源码时自底向上爬依赖图,是最不容易迷路的路线**——这正是本手册 u2(GPUI)→ u3(Rope/Buffer)→ u4(Project)→ u5(Editor)排序的依据。

#### 4.3.2 核心流程

判断一个 crate 层次的四步法:

1. 打开它的 `Cargo.toml`,列出 `[dependencies]` 里的**内部**依赖(形如 `foo.workspace = true` 且 `foo` 是成员名单里的名字)。
2. 内部依赖越少、越通用 → 越靠底层。
3. 看它的 `[lib]`/`[[bin]]`:有 `[[bin]]` 的是可执行程序的终点;纯 `[lib]` 是被别人引用的库。
4. 反向验证:Grep 其他 crate 的清单看谁依赖它(扇入大 → 底层)。

#### 4.3.3 源码精读

**底层样本:`rope`,内部依赖只有两个。**

- [crates/rope/Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/rope/Cargo.toml#L11-L12) — `[lib] path = "src/rope.rs"`:遵循仓库「库根与 crate 同名,不用 lib.rs」的约定。
- [crates/rope/Cargo.toml:L14-L22](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/rope/Cargo.toml#L14-L22) — 依赖段里内部依赖只有 `sum_tree` 和 `util`,其余是 `rayon`、`unicode-segmentation` 等通用外部库。这就是典型第 0 层的样子:文本树结构,连 gpui 都不依赖(注意它的 `gpui` 只出现在 `[dev-dependencies]`,用于测试)。

**中层样本:`worktree`,依赖 language 而不被 editor 依赖。**

- [crates/worktree/Cargo.toml:L32-L60](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/worktree/Cargo.toml#L32-L60) — 内部依赖包括 `language`、`git`、`gpui`、`sum_tree`、`text`、`rpc`、`fs` 等:它在语言层之上、项目层之下,把文件系统封装成可查询的快照。
- 对照 [crates/language/Cargo.toml:L31-L80](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/language/Cargo.toml#L31-L80) — `language` 依赖 `gpui`、`sum_tree`、`text`、`lsp`、`rpc`、`settings`、`theme` 等,但**不依赖** `worktree`,所以 `language` 在 `worktree` 之下。一个反直觉的细节:语言层会依赖网络层的 `rpc`——这与远程协作时 buffer 操作的同步有关,细节留到 u8-l1。

**聚合层样本:`project`,把 worktree/language/terminal 拧在一起。**

- [crates/project/Cargo.toml:L36-L109](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/project/Cargo.toml#L36-L109) — 内部依赖密密麻麻:`worktree`、`language`、`terminal`、`client`、`rpc`、`lsp`、`git`、`dap`、`buffer_diff`、`prettier`、`settings`……它是「项目的中心枢纽」这个定位在依赖图上的直接体现。

**顶层样本:`editor` 与 `vim`。**

- [crates/editor/Cargo.toml:L33-L107](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/editor/Cargo.toml#L33-L107) — `editor` 依赖 `project`、`multi_buffer`、`gpui`、`rope`、`workspace`、`ui`、`theme` 等。注意这里的 `workspace` 指的是**窗口组织 crate**(`crates/workspace`),与 Cargo workspace 概念重名,读代码时要靠上下文区分。
- [crates/vim/Cargo.toml:L18-L55](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/vim/Cargo.toml#L18-L55) — `vim` 依赖 `editor`、`project`、`multi_buffer`、`workspace`、`ui`、`search`、`command_palette`:它站在 `editor` 肩上改写按键行为,是依赖链接近顶端的一环。

**平行世界样本:`collab`,一个不依赖 editor 的独立二进制。**

- [crates/collab/Cargo.toml:L13-L14](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/collab/Cargo.toml#L13-L14) — `[[bin]] name = "collab"`:它是协作服务端,一个与 `zed` 平行的可执行程序。
- [crates/collab/Cargo.toml:L28-L74](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/collab/Cargo.toml#L28-L74) — 它的依赖是 `rpc`、`text`、`gpui`、`clock`、`livekit_api`、`cloud_api_types` 等,**没有** `editor`/`project`。这说明「顶层」不止 `zed` 一个:服务端复用底层协议与数据结构,但完全不碰编辑器 UI。它是理解「分层让平行产品成为可能」的最佳例子。

同理可读:[crates/terminal/Cargo.toml:L22-L48](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/terminal/Cargo.toml#L22-L48)(终端模拟,依赖 `gpui`、`theme`、`settings`,不依赖 language/project)与 [crates/agent/Cargo.toml:L19-L80](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/agent/Cargo.toml#L19-L80)(AI 智能体,依赖 `project`、`language_models`、`db`、`sandbox` 等,是高层的功能域 crate)。

#### 4.3.4 代码实践

**实践目标**:沿一条真实依赖链从顶爬到底,验证「分层」不是纸上谈兵。

**操作步骤**:

1. 写下这条链:`vim → editor → project → worktree → language → text`。
2. 逐段打开对应 `Cargo.toml` 的 `[dependencies]`,找到下一环的名字,记下所在行号:
   - `vim` 清单中的 `editor.workspace = true`([crates/vim/Cargo.toml:L26](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/vim/Cargo.toml#L26))
   - `editor` 清单中的 `project.workspace = true`([crates/editor/Cargo.toml:L68](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/editor/Cargo.toml#L68))
   - `project` 清单中的 `worktree.workspace = true`([crates/project/Cargo.toml:L105](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/project/Cargo.toml#L105))
   - `worktree` 清单中的 `language.workspace = true`([crates/worktree/Cargo.toml:L45](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/worktree/Cargo.toml#L45))
   - `language` 清单中的 `text.workspace = true`([crates/language/Cargo.toml:L66](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/language/Cargo.toml#L66))
3. 用 cargo 验证整条链(它会打印完整路径):

   ```bash
   cargo tree -p vim -i text
   ```

   (`-i text` 表示「反向」查看谁依赖 `text`。)

**需要观察的现象**:每一段依赖都能在清单里找到实名对应;`cargo tree -i` 的输出里出现 `vim` 到 `text` 的多条路径。

**预期结果**:你亲手验证了「按键行为(vim)→ 编辑器(editor)→ 项目(project)→ 工作树(worktree)→ 语言(language)→ 文本原语(text)」这条纵贯线。`cargo tree` 的具体输出格式随 Cargo 版本略有差异,「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**:`rope` 的 `Cargo.toml` 里出现了 `gpui`,能说明 rope 依赖 UI 框架吗?

> **答案**:不能。`gpui` 位于 `[dev-dependencies]`(见 [crates/rope/Cargo.toml:L24-L30](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/rope/Cargo.toml#L24-L30)),只在编译测试时生效,不进入正式依赖闭包。判断层次时务必看清楚依赖落在哪个表里。

**练习 2**:为什么 `collab` 服务端能完全不依赖 `editor`,却依然能和编辑器协作编辑?

> **答案**:因为双方共同依赖更底层的协议与数据结构(`rpc`、`proto`、`text` 等)。服务端同步的是**操作与版本**,不是屏幕上的 UI;只要底层 crate 对「文本与编辑」的建模一致,顶层各自实现即可。这正是分层的价值:换一个顶层(服务端),底层原语原样复用。

**练习 3**:如果要给 `sum_tree` 加一个新 API,和给 `agent_ui` 加一个新面板相比,测试范围有什么不同?

> **答案**:`sum_tree` 处于第 0 层、扇入极大(rope、language、editor、multi_buffer……都直接或间接依赖它),改动需要跑大面积相关测试;`agent_ui` 是顶层功能 crate,扇入基本只有 `zed`,回归范围小得多。层次决定了改动的「爆炸半径」。

## 5. 综合实践

**任务**:浏览 `crates/` 目录,把 `gpui`、`editor`、`language`、`project`、`worktree`、`rope`、`agent`、`collab`、`vim`、`terminal` 这 10 个 crate 按底层/中层/顶层画一张依赖草图。

**操作步骤**:

1. 对每个 crate 打开其 `Cargo.toml`,只记录**内部依赖**(在成员名单或共享依赖表里能对上号的)。
2. 按内部依赖多寡和方向初步排层。
3. 用 `cargo tree -p <crate> --depth 1` 抽查两三个 crate 核对你的清单阅读结果。
4. 画图(纸笔或任何画图工具),箭头从依赖者指向被依赖者,标出两个特殊节点:`zed`(扇入为零的组装点)与 `collab`(平行二进制)。

**参考答案草图**(依据本讲引用的真实清单绘制,箭头读作「依赖」):

```
第 5 层  vim ──────┐
第 4 层  editor ───┤→ (被 zed 组装)
        agent ────┤
第 3 层  project ──┤
第 2 层  worktree ─┤
第 1 层  language ─┤   terminal(独立分支,不依赖 language)
第 0 层  gpui / rope(→ sum_tree)

平行二进制:
  collab ─→ rpc / text / gpui(不依赖 editor、project)
  zed    ─→ 上述几乎所有 crate(组装成编辑器应用)
```

各层依据(均为本讲 4.3.3 引用的清单事实):

| crate | 关键内部依赖 | 层次判定 |
| --- | --- | --- |
| `gpui` | 基本无内部依赖 | 第 0 层:UI 与并发框架 |
| `rope` | `sum_tree`、`util` | 第 0 层:文本数据结构 |
| `language` | `gpui`、`sum_tree`、`text`、`lsp`、`rpc` | 第 1 层:语言核心 |
| `terminal` | `gpui`、`theme`、`settings`(无 language/project) | 第 1 层旁支:终端模拟 |
| `worktree` | `language`、`git`、`gpui` | 第 2 层:文件系统抽象 |
| `project` | `worktree`、`language`、`terminal`、`client`、`lsp`… | 第 3 层:项目枢纽 |
| `editor` | `project`、`multi_buffer`、`gpui`、`rope`、`workspace`、`ui` | 第 4 层:编辑器 UI |
| `agent` | `project`、`language_models`、`db`、`sandbox`… | 第 4 层:AI 功能域 |
| `vim` | `editor`、`project`、`workspace`、`ui` | 第 5 层:编辑器包装 |
| `collab` | `rpc`、`text`、`gpui`、`livekit_api` | 平行二进制:协作服务端 |

**需要观察的现象**:画完后你会发现两点——(1) 越往下箭头越密(大家都指向 `gpui`);(2) `zed` 不在任何人 的依赖里(它是终点)。

**预期结果**:草图与上表一致即为完成;若你的图中出现环,说明读错了某个 `[[bin]]`/`[dev-dependencies]` 条目,回到 4.3.4 的方法逐段核对。

## 6. 本讲小结

- workspace 成员分三块大陆:`crates/`(243 个)、`extensions/`(4 个)、`tooling/`(3 个),合计约 250 个成员,名单在根 `Cargo.toml` 手工维护。
- `default-members = ["crates/zed"]` 决定了裸 cargo 命令只构建编辑器主程序;`zed` 通过 140 余个内部依赖把整个产品组装起来。
- crate 命名有规律(`_ui`、`_store`、提供商名、`gpui*` 前缀),配合 `[workspace.dependencies]` 这本「电话簿」和 glossary,可以快速给陌生 crate 归类。
- 依赖图是 DAG,分层 \( L(c) = 1 + \max L(d) \);判断层次看内部依赖的数量与方向,并区分 `[dependencies]` 与 `[dev-dependencies]`。
- `collab` 是与 `zed` 平行的独立二进制,复用 `rpc`/`text` 等底层而不依赖 `editor`——分层让平行产品成为可能。

## 7. 下一步学习建议

本讲建立了静态地图,下一讲 **u1-l4「应用启动流程」** 将让地图动起来:从 `crates/zed/src/main.rs` 进入,看这个组装了 140 多个 crate 的二进制如何按顺序把 GPUI、设置、主题、扩展一个个初始化,直到窗口出现。

之后进入第 2 单元(GPUI)时,请带着本讲的分层直觉:你将从第 0 层的 `gpui` 开始自底向上阅读——这是理解它上层一切 UI 代码的最短路径。探索习惯上,建议从现在开始把 `cargo tree -p <crate>` 和 `cargo metadata --no-deps` 当作常备工具。
