# 一本书的解剖：book.toml 与 SUMMARY.md

## 1. 本讲目标

在前三讲里，我们已经把整个仓库当作一个「七本书的出版工厂」看了一遍：知道了 `BOOKS` 注册表是工厂的总目录（u1-l1），摸清了 `*-book/` 目录的排列方式（u1-l2），也跑通了 `cargo xtask build / serve / deploy` 流水线（u1-l3）。

本讲把镜头推进到「一本书的内部」，以 `async-book/`（异步 Rust 深潜）为解剖标本，学完本讲你应该能够：

1. 逐行读懂 [async-book/book.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L1-L21) 中每个配置节（`[book]`、`[build]`、`[output.html]`、`[preprocessor.mermaid]`、`[output.html.playground]`）的作用，并能回答「改了这一行，页面会发生什么变化」。
2. 掌握 [async-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L1-L41) 的语法：前缀章、Part 分部标题、编号章、分隔线、草稿章，理解侧边栏导航是如何从这份文件生成的。
3. 能独立用 `mdbook serve --open` 预览单本书，并通过「修改 → 观察 → 还原」的循环验证自己对配置的理解——这也是后续给七本书中任何一本贡献内容时的标准工作流。

## 2. 前置知识

本讲只需要非常基础的背景，不熟悉的的概念都在这里补齐：

- **mdBook 是什么**：一个用 Rust 写的命令行工具，把一组 Markdown 文件渲染成一个带侧边栏、搜索框、主题切换的静态 HTML 书站。本仓库七本书全部由它生成（版本钉在 `mdbook@0.4.52`，见 [README.md:L81](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L81)）。
- **一本书的三层输入**：
  1. `book.toml` —— 配置层，决定「这本书叫什么、输出到哪、开哪些功能」；
  2. `src/SUMMARY.md` —— 结构层，决定「有哪些章、什么顺序、什么层级」；
  3. `src/chXX-*.md` —— 内容层，每章一个 Markdown 文件。
  本讲聚焦前两层。
- **TOML 语法**：`book.toml` 用的格式。`[book]` 这样的方括号行开启一个「表」（table），下面的 `key = value` 都属于这个表，直到下一个 `[...]` 出现。可以理解成「分节的键值对」。
- **预处理器（preprocessor）与渲染器（renderer）**：mdBook 的构建流水线是「读入 Markdown → 预处理器逐个改写 → 渲染器输出」。预处理器在内容变成 HTML *之前*动手脚；渲染器决定最终产物（默认的 `html` 渲染器生成网站）。`mdbook-mermaid` 就是一个预处理器，负责处理 Mermaid 图表代码块。
- **Mermaid**：一种「用文字描述图表」的语法，比如用 `sequenceDiagram` 写几行文字就能生成一张时序图。浏览器里由一段 JavaScript（`mermaid.min.js`）负责把文字画成图。
- **Rust Playground**：[play.rust-lang.org](https://play.rust-lang.org)，官方在线运行 Rust 代码的沙箱。mdBook 页面上的「运行」按钮就是把代码片段发给它执行。
- **书根目录（book root）**：包含 `book.toml` 的那个目录（如 `async-book/`）。这个概念在读 `additional-js` 路径时会用到——它的路径是相对书根解析的，而不是相对 `src/`。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [async-book/book.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L1-L21) | **本讲主角一**：async-book 的全部配置，仅 22 行，却定义了输出目录、主题、mermaid、playground 四大块行为 |
| [async-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L1-L41) | **本讲主角二**：全书骨架，17 章 + 1 个前缀章，分成 3 个 Part + 附录 |
| [async-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L1-L96) | 前缀章正文，内部还有一份**手写的目录**，与 SUMMARY.md 形成「双源」，是很好的对照材料 |
| [python-book/book.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/book.toml#L1-L22) | 对照样本：与 async-book 的配置逐行同构（仅 title 不同），证明七本书共用一套配置模板 |
| [xtask/src/main.rs:L146-L152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L146-L152) | 构建任务的 mdbook 调用点：用 `--dest-dir` 覆盖 `build-dir`，解释「单书预览」与「批量构建」输出位置为何不同 |
| [async-book/src/ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24) | 真实的 ` ```rust ` 代码块样本，用来观察 playground 行为 |
| [async-book/src/ch02-the-future-trait.md:L30-L61](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L30-L61) | 真实的 ` ```mermaid ` 代码块样本，用来观察 mermaid 预处理器的作用对象 |

## 4. 核心概念与源码讲解

### 4.1 book 配置节：`[book]`、`[build]` 与 `[output.html]`

#### 4.1.1 概念说明

`book.toml` 是 mdBook 对一本书的唯一配置入口。它回答三类问题：

- **这本书是什么**（`[book]`）：标题、作者、语言、源文件目录；
- **构建产物放哪**（`[build]`）：输出目录；
- **网站长什么样、带什么功能**（`[output.html]`）：GitHub 链接、默认主题、额外加载的 JS。

读者不需要记全 mdBook 的几十个配置键，只需要建立「配置键 → 页面可见效果」的映射直觉。下面逐节精读。

#### 4.1.2 核心流程

mdbook 单书构建时的读取顺序（简化）：

1. 在**书根目录**（含 `book.toml` 的目录）发现 `book.toml`，解析各配置表并与默认值合并；
2. `[book].src`（这里是 `src`）确定 Markdown 源码目录，从中读 `SUMMARY.md` 得到章节树；
3. 预处理器流水线处理各章内容（见 4.2）；
4. `[build].build-dir`（这里是 `book`）确定输出目录，HTML 渲染器把整站写进去；
5. 渲染时按 `[output.html]` 的键注入对应的行为：菜单栏的 GitHub 图标、主题、额外 JS 等。

注意第 4 步的一个关键分叉：**这条默认路径只在「直接在书目录里跑 mdbook」时生效**。u1-l3 讲过的 `cargo xtask build` 走的是另一条路——xtask 显式传了 `--dest-dir`，把输出强制改到 `site/<slug>/` 或 `docs/<slug>/`：

[xtask/src/main.rs:L146-L152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L146-L152) —— 构建每一本书时，xtask 以书目录为工作目录启动 `mdbook build --dest-dir <统一输出目录>/<书名>`，命令行参数覆盖了 `book.toml` 里的 `build-dir`。

所以：**`build-dir = "book"` 管的是你自己在 `async-book/` 里跑 `mdbook build/serve` 时的输出位置；批量构建时它被 `--dest-dir` 覆盖**。这就是为什么 u1-l3 里 `cargo xtask clean` 只删 `site/` 和 `docs/`，而单书实验还会留下 `async-book/book/` 目录。

#### 4.1.3 源码精读

**第一节：`[book]` 元数据**

[async-book/book.toml:L1-L5](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L1-L5) —— 定义书的身份：

```toml
[book]
title = "Async Rust: From Futures to Production"
authors = ["Rust Training Team"]
language = "en"
src = "src"
```

- `title`：出现在浏览器标签页标题和侧边栏顶部，也是搜索结果的书籍名；
- `language`：写入 HTML 的 `lang` 属性（本仓库所有书都是 `en`）；
- `src = "src"`：源文件目录，相对书根解析。SUMMARY.md 必须位于 `<src>/SUMMARY.md`。

**第二节：`[build]` 输出目录**

[async-book/book.toml:L7-L8](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L7-L8) —— `build-dir = "book"` 把单书构建输出定位到 `async-book/book/`。这恰好也是 mdBook 的默认值，写出来属于「显式文档化默认行为」。

**第三节：`[output.html]` 网站外观**

[async-book/book.toml:L10-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L10-L14) —— HTML 渲染器的选项：

```toml
[output.html]
git-repository-url = "https://github.com/microsoft/RustTraining"
default-theme = "light"
preferred-dark-theme = "ayu"
additional-js = ["mermaid.min.js", "mermaid-init.js"]
```

- `git-repository-url`：页面右上角菜单出现 GitHub 图标，点击跳到仓库主页；
- `default-theme = "light"`：访客默认看到浅色主题；
- `preferred-dark-theme = "ayu"`：当浏览器通过 `prefers-color-scheme` 请求暗色时使用的主题。两个键搭配的含义是「默认浅色，但尊重系统的暗色偏好」；
- `additional-js`：在每页默认脚本之外额外加载的两个 JS 文件。**关键细节：这两个路径是相对「书根目录」（`async-book/`）解析的，不是相对 `src/`**。证据链：`git ls-files 'async-book/*'` 显示 `mermaid.min.js` 与 `mermaid-init.js` 被 git 跟踪在 `async-book/` 书根下（而非 `src/` 内），且 mdBook 0.4.52 渲染器在复制额外 JS 资产时以书根为基准目录拼接路径——两边严丝合缝。这个细节在 4.2 会再展开。

**横看：七本书共用一个模板**

[python-book/book.toml:L1-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/book.toml#L1-L22) —— python-book 的配置与 async-book 逐行同构，唯一实质差异是 `title = "Rust for Python Programmers"`。也就是说，仓库维护者用的是「一套配置模板 + 每本书换标题」的策略。这解释了 u1-l1 的观察：七本书的站点外观和行为完全一致。**学会读这一份 book.toml，等于学会了读全部七份。**

#### 4.1.4 代码实践

**实践：改主题，观察外观**

1. **实践目标**：验证 `default-theme` 的作用，建立「改配置 → 页面变化」的直接体感。
2. **操作步骤**：
   - 打开 `async-book/book.toml`，把第 12 行 `default-theme = "light"` 临时改为 `default-theme = "coal"`（mdBook 内置暗色主题）；
   - 在 `async-book/` 目录下运行 `mdbook serve --open`（依赖安装见 u1-l3 / [README.md:L81](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L81)）；
   - 浏览器打开后（默认 `http://localhost:3000`），按 `Ctrl+Shift+R` 强制刷新避免缓存干扰；
   - 观察完把配置改回 `light`，保存后页面会自动热重建回浅色；
   - 结束后 `Ctrl+C` 停止 serve，并用 `git checkout -- async-book/book.toml`（在仓库根目录执行）确保还原干净。
3. **需要观察的现象**：首次加载即为暗色（coal）主题，无需手动切换；改回 `light` 保存后，serve 的增量构建日志滚动，页面自动恢复浅色。
4. **预期结果**：`default-theme` 决定「首屏主题」，而页面菜单里的主题切换器仍可手动换。**本实践结果待本地验证**（本次讲义编写环境未安装 mdbook，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `[build]` 一节，单书 `mdbook build` 的输出会去哪里？

> **答案**：仍然输出到 `async-book/book/`。`build-dir` 的默认值就是 `book`，删掉配置只是回到默认行为；写出来是让读者一眼看到输出位置。

**练习 2**：`additional-js` 里写的是 `mermaid.min.js`，为什么不需要写成 `src/mermaid.min.js`？文件实际在哪？

> **答案**：mdBook 以**书根目录**为基准解析 `additional-js` 的相对路径。这两个文件被 git 跟踪在 `async-book/`（书根）下，所以直接写文件名即可；若挪进 `src/` 反而要改配置且和约定不符。

**练习 3**：`default-theme` 与 `preferred-dark-theme` 有什么区别？

> **答案**：前者是所有访客的首屏默认主题；后者仅当浏览器通过 `prefers-color-scheme` 媒体查询声明「偏好暗色」时才生效。本书设为 light + ayu，即「默认浅色、尊重系统暗色偏好」。

### 4.2 preprocessor.mermaid：让书里的图表活起来

#### 4.2.1 概念说明

本仓库每本书都有大量 Mermaid 图（README 明确说 "Each book has 15–16 chapters with Mermaid diagrams…"，见 [README.md:L57](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L57)）。但 mdBook 本体不认识 Mermaid——它只会把 ` ```mermaid ` 当成普通代码块渲染成一坨等宽文字。

`mdbook-mermaid` 这个预处理器补上了缺口，它的工作分**构建期**和**浏览器期**两半：

- **构建期**：mdbook 启动时发现 `[preprocessor.mermaid]` 表，就按 `command` 指定的程序名拉起外部进程，把「整本书的结构 + 配置」以 JSON 喂给它；该进程把 mermaid 代码块改写成页面可用的形式后交还；
- **浏览器期**：每个页面通过 `additional-js` 加载 `mermaid.min.js`（绘图引擎）和 `mermaid-init.js`（初始化脚本），在访客浏览器里把图渲染成 SVG。

为什么拆成两半？因为画图是纯前端工作，构建期只需要「把代码块变成前端认识的标记 + 确保页面带上了绘图脚本」。

#### 4.2.2 核心流程

```
mdbook build
   │
   ├─ 读取 book.toml，发现 [preprocessor.mermaid]
   │    command = "mdbook-mermaid"  ──► 启动 mdbook-mermaid 子进程（stdin/stdout 交换 JSON）
   │                                     └─ 改写所有 ```mermaid 块
   ├─ HTML 渲染器接收改写后的章节
   │    └─ 按 [output.html].additional-js 从书根复制 mermaid.min.js / mermaid-init.js
   │       并在每页 <script> 引用它们
   └─ 输出到 build-dir（单书）或 --dest-dir（xtask 批量）

访客打开页面 ──► mermaid-init.js 初始化 ──► mermaid.min.js 把图源码画成 SVG
```

两个容易踩的坑：

1. **没装 `mdbook-mermaid` 会怎样**：配置表存在而可执行文件不在 `PATH` 上时，mdbook 直接报错终止（预处理器默认是必需的）。这正是 u1-l3 强调安装命令要把两个工具一起装的原因（[README.md:L81](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L81)）。
2. **JS 资产缺失会怎样**：预处理器照常改写，但页面没有绘图引擎，图表位置只会显示图的文字源码。

#### 4.2.3 源码精读

**配置声明**

[async-book/book.toml:L16-L17](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L16-L17) —— 声明名为 `mermaid` 的预处理器，并指定要执行的命令：

```toml
[preprocessor.mermaid]
command = "mdbook-mermaid"
```

按 mdBook 的约定，名为 `foo` 的预处理器默认会去找 `mdbook-foo` 可执行文件；`command` 键用来显式指定（也可以换成别的程序名或带参数）。这里两者恰好一致，属于显式写明的默认行为。

**与 additional-js 的配合**

[async-book/book.toml:L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L14) —— `additional-js = ["mermaid.min.js", "mermaid-init.js"]` 是这套机制的另一半：预处理器负责构建期改写，这两个脚本负责浏览器期绘图。它们躺在书根 `async-book/` 下并被 git 跟踪（用 `git ls-files 'async-book/*'` 可验证），所以构建不依赖网络下载；这个「两文件放书根 + book.toml 两段配置」的布局正是 `mdbook-mermaid install` 工具生成的标准形态。

**预处理器的原料：真实的 mermaid 代码块**

[async-book/src/ch02-the-future-trait.md:L30-L61](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L30-L61) —— 第 2 章里一张完整的时序图，从 Executor 调用 `poll(cx)` 开始，到 Waker 唤醒、任务重回队列，完整讲了一遍异步轮询循环。这就是 `mdbook-mermaid` 在构建期要处理的输入：

```mermaid
sequenceDiagram
    participant E as Executor
    participant F as Future (Task)
    ...
    E->>F: Calls poll(cx)
    F-->>E: Returns Poll::Pending
    R->>E: Calls Waker::wake()
    E->>F: Calls poll(cx) again
```

同一章里还混着普通 Rust 块（[ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24)，`Future` trait 定义）——预处理器只动 ` ```mermaid ` 块，` ```rust ` 块留给 playground 机制（4.3）。

#### 4.2.4 代码实践

**实践：从产物反推预处理器做了什么**

1. **实践目标**：用构建产物验证「mermaid 块在构建期被改写、JS 资产被复制」。
2. **操作步骤**：
   - 在仓库根目录运行 `git ls-files 'async-book/*'`，确认 `async-book/mermaid.min.js` 和 `async-book/mermaid-init.js` 在 git 跟踪列表里、且位于书根；
   - 在 `async-book/` 下运行 `mdbook build`（输出进入 `async-book/book/`）；
   - 打开 `book/ch02_the_future_trait.html`，搜索字符串 `mermaid`：预期既能在页面里找到被改写的图表标记，也能在 `<script>` 引用中看到 `mermaid.min.js`；
   - 再看 `book/` 根部，预期有两个被复制过来的 mermaid JS 文件；
   - 完成后 `rm -rf async-book/book` 清理产物（该目录不应提交）。
3. **需要观察的现象**：HTML 中不再有裸露的 ` ```mermaid ` 围栏，取而代之的是前端绘图脚本认得的标记；JS 资产出现在输出目录。
4. **预期结果**：构建期改写 + 资产复制两件事都能在产物里看到实证。**本实践结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 的安装命令必须同时装 `mdbook` 和 `mdbook-mermaid`，只装前者会怎样？

> **答案**：`book.toml` 声明了 `[preprocessor.mermaid]`，mdbook 构建时必须能拉起 `mdbook-mermaid` 子进程。只装 mdbook 的话，构建在预处理器阶段就会报错退出（预处理器默认必需，除非标 `optional = true`，本书没标）。

**练习 2**：如果把 `additional-js` 那行删掉，页面会发生什么？

> **答案**：构建仍能成功（预处理器还在），但页面不再加载绘图引擎，mermaid 图表位置显示的是图的文字源码而不是 SVG——构建期与浏览器期两半缺一不可。

**练习 3**：为什么不把 `mermaid.min.js` 放进 `src/` 和章节放一起？

> **答案**：`additional-js` 以书根为基准解析路径，且 `src/` 语义上是「章节 Markdown 源码」；放书根既符合路径解析规则，也保持 `src/` 纯净。这是 mdBook 生态的既定约定（`mdbook-mermaid install` 也生成在这个位置）。

### 4.3 playground 设置：页面上的代码为什么能直接跑

#### 4.3.1 概念说明

mdBook 对 ` ```rust ` 代码块有一套内置增强：默认给每个 Rust 块加一个**运行按钮**，点击把代码发到官方 Rust Playground 在线执行。`[output.html.playground]` 表用来调整这套行为，本仓库的配置开了两项：

- `editable = true`：允许在页面上直接编辑源码（默认 false）；
- `line-numbers = true`：可编辑代码块显示行号（默认 false）。

README 对外的宣传语 "editable Rust playgrounds"（[README.md:L57](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L57)）指的就是这两个开关的效果。这会让教程类内容体验大幅提升：读者改一行代码立刻能跑，不用本地建工程。

#### 4.3.2 核心流程

mdBook 官方文档对 `[output.html.playground]` 各键的语义定义（本仓库只显式设了前两个，其余为默认值）：

| 键 | 本书的值 | 默认值 | 含义 |
|----|---------|--------|------|
| `editable` | `true` | `false` | 允许编辑页面上的源码 |
| `line-numbers` | `true` | `false` | 可编辑代码区显示行号；**生效前提是 `editable` 与 `copy-js` 同时为 true** |
| `copy-js` | 未设（true） | `true` | 把编辑器所需 JS 复制进输出目录 |
| `runnable` | 未设（true） | `true` | Rust 代码块显示「运行」按钮 |
| `copyable` | 未设（true） | `true` | 代码块显示复制按钮 |

渲染器处理 Rust 块时还有两条默认规则（mdBook 0.4.52 源码行为）：

- 代码里没有 `fn main` 时，渲染器会自动注入一段隐藏的 `# fn main() { … # }` 包装，让片段作为一个完整程序发去 Playground；
- 代码围栏标注了 `ignore`、`noplayground` 或 `noplaypen` 时，该块不参与 playground 增强（只做语法高亮）。

#### 4.3.3 源码精读

**配置**

[async-book/book.toml:L19-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L19-L21) —— 三行开启全书可编辑 + 行号：

```toml
[output.html.playground]
editable = true
line-numbers = true
```

注意 `copy-js` 没写——默认即 true，恰好满足 `line-numbers` 的生效前提（`editable` && `copy-js`）。如果哪天有人显式加一行 `copy-js = false`，行号会静默失效，这是个隐蔽的配置陷阱。

**作用对象：真实的 Rust 代码块**

[async-book/src/ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24) —— `Future` trait 的定义，就是一个普通的 ` ```rust ` 围栏，没有任何注解：

```rust
pub trait Future {
    type Output;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
}
```

这段代码没有 `fn main`，按上面的规则，渲染时会被注入隐藏的 main 包装再挂运行按钮——读者在页面上点一下就能看到 trait 定义通过编译。全书章节用的都是这种无注解写法，行为统一由 `book.toml` 全局控制。

#### 4.3.4 代码实践

**实践：用 `ignore` 注解摘掉运行按钮**

1. **实践目标**：直观验证「playground 增强按代码块逐个生效，注解可以排除个别块」。
2. **操作步骤**：
   - 把 `async-book/src/ch02-the-future-trait.md` 第 13 行的 ` ```rust ` 临时改成 ` ```rust,ignore `；
   - `async-book/` 下运行 `mdbook serve --open`，打开第 2 章；
   - 对比该代码块与其他 Rust 块（比如第 65、92 行附近的块）的按钮差异；
   - 还原：把 `,ignore` 删掉，保存后 serve 自动重建；结束后 `Ctrl+C`，并在仓库根目录 `git checkout -- async-book/src/ch02-the-future-trait.md` 双保险还原。
3. **需要观察的现象**：被标注 `ignore` 的块没有运行按钮（只剩高亮和复制按钮），其余 Rust 块不受影响。
4. **预期结果**：确认注解是块级开关、配置是全局开关，两层叠加决定单个代码块的最终行为。**本实践结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `line-numbers` 要依赖 `editable` 和 `copy-js` 同时为 true？

> **答案**：行号不是 HTML 画出来的，而是页面内嵌代码编辑器渲染的。编辑器脚本只有在 `editable` 开启且 `copy-js = true`（把编辑器 JS 复制进产物）时才会就位——没有编辑器，行号无从谈起。

**练习 2**：一个 ` ```rust ` 块里没有 `fn main`，直接发到 Playground 能编译吗？mdBook 怎么解决？

> **答案**：不能，Playground 需要完整程序。mdBook 渲染时注入一段隐藏的 `# fn main() { … # }` 包装（`#` 开头的行在页面上不显示），把片段补成完整程序再交给运行按钮。

**练习 3**：想让全书所有 Rust 块都不出现运行按钮，改哪里？只想让某一章的一个块不出现呢？

> **答案**：全局改 `[output.html.playground]` 加 `runnable = false`；单个块在围栏信息字符串里加 `ignore` / `noplayground` / `noplaypen`。

### 4.4 SUMMARY 语法：一本书的骨架如何变成侧边栏

#### 4.4.1 概念说明

mdBook 官方文档的第一句话就把 SUMMARY.md 的地位说尽了：*没有这个文件，就没有这本书*（"Without this file, there is no book"）。它决定**包含哪些章、顺序如何、层级如何、源文件在哪**，侧边栏导航、页面先后跳转、上一章/下一章按钮全部由它生成。

SUMMARY.md 是**格式严格的** Markdown 子集——解析器只认下面六种元素，其他写法轻则忽略、重则构建报错：

| 语法 | 名称 | 侧边栏效果 |
|------|------|-----------|
| `[标题](文件.md)`（位于编号章之前/之后的独立链接） | 前缀章 / 后缀章 | **不编号**的可点击条目 |
| `# 标题`（一级标题） | Part 分部标题 | 不可点击的分组文字 |
| `- [标题](文件.md)`（列表项，可嵌套） | 编号章 | 自动编号（1.、2.、2.1…）的章节 |
| `- [标题]()` | 草稿章 | 灰色不可点击的占位链接 |
| `---`（三个及以上短横线） | 分隔线 | 一条水平细线 |
| `# Summary`（首行标题） | 文件标题 | 被解析器忽略 |

两条重要规则：前缀章必须在所有编号章之前、不可嵌套；编号章编号是**跨 Part 连续**的（Part II 的第一章接着 Part I 的最后一章编号，不会每个 Part 从 1 重来）。

#### 4.4.2 核心流程

解析器自上而下扫描 SUMMARY.md，把每行映射成一个书条目，得到一棵「书树」：

```
# Summary                      ← 忽略
[Introduction](ch00-…)         ← 前缀章（无编号）
---                            ← 分隔线
# Part I: How Async Works      ← 分部标题（不可点击）
- [1. Why Async…](ch01-…)      ← 编号章 #1
- [2. The Future Trait](ch02-…)← 编号章 #2
  …                            ← （编号跨 Part 连续累加到 17）
# Appendices                   ← 分部标题
- [Summary and Reference Card] ← 编号章 #16
- [Capstone Project…]          ← 编号章 #17
```

渲染侧边栏时按这棵树逐项输出：分部标题变成分组文字、编号章变成链接、分隔线变成水平线。还有一个默认行为值得注意：mdBook 默认（`no-section-label = false`）会在侧边栏条目前加**自动序号**（如「1.」「2.1」）。本书标题里已经手写了序号（如「1. Why Async is Different in Rust」），两者叠加，侧边栏预计显示成「1. 1. Why Async is Different in Rust」式的双序号——你可以在 4.4.4 的实践中观察验证这一点。

#### 4.4.3 源码精读

**整体骨架**

[async-book/src/SUMMARY.md:L1-L41](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L1-L41) —— 全文 41 行定义了「1 前缀章 + 3 个 Part + 附录、共 17 个编号章」的结构。逐段拆开：

**前缀章与第一条分隔线**

[async-book/src/SUMMARY.md:L1-L5](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L1-L5) —— 第 1 行 `# Summary` 被解析器忽略；第 3 行 `[Introduction](ch00-introduction.md)` 是前缀章，**不参与编号**（所以它的文件叫 ch00）；第 5 行 `---` 在侧边栏画一条分隔线，把引言与正文隔开。

**Part 与编号章**

[async-book/src/SUMMARY.md:L7-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L7-L13) —— `# Part I: How Async Works` 是不可点击的分部标题；下面五条列表项是编号章：

```markdown
- [1. Why Async is Different in Rust](ch01-why-async-is-different-in-rust.md)
- [2. The Future Trait](ch02-the-future-trait.md)
- [3. How Poll Works](ch03-how-poll-works.md)
- [4. Pin and Unpin](ch04-pin-and-unpin.md)
- [5. The State Machine Reveal](ch05-the-state-machine-reveal.md)
```

注意列表项用 `-` 或 `*` 皆可但不能混用；这里全部用 `-`。文件名 `ch01…ch05` 与自动编号 1…5 一一对应——这是维护者用**文件名前缀固定编号**的约定，防止重排章节时链接失效。

**后续 Part 与附录**

[async-book/src/SUMMARY.md:L15-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L15-L40) —— 同样的「`---` + `# Part N` + 编号章列表」模式再重复三次：Part II（第 6–10 章）、Part III（第 11–15 章，其中 [L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L33) 是第 15 章 Exercises）、Appendices（第 16 参考卡、第 17 capstone 项目）。本书没有用到草稿章语法 `- [标题]()`。

**双源目录的漂移实例（重点）**

[ch00-introduction.md:L64-L92](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L64-L92) —— 引言章里还有一份**手写的目录**（带 🟢🟡🔴 难度标记和每章一句话摘要），比侧边栏信息更丰富，但它不会自动跟随 SUMMARY.md 更新。对照之下能看到两处真实漂移：

1. 手写目录的 Part III 只列了第 11–14 章（[ch00-introduction.md:L82-L87](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L82-L87)），**漏掉了 SUMMARY.md 第 33 行的第 15 章 Exercises**；
2. [ch00-introduction.md:L58](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L58) 写着 "The capstone (Ch 16)"，而 SUMMARY.md 里 capstone 是第 17 章（[SUMMARY.md:L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L40)）。

这与 u1-l1 讲过的「README 表格 / `BOOKS` 注册表双源」是同一种维护模式的风险：**两份人工维护的清单迟早分叉**。给这本书加章或改章名时，记得 SUMMARY.md 与 ch00 手写目录要一起改。

#### 4.4.4 代码实践

**实践：加一个草稿章，看侧边栏如何呈现**

1. **实践目标**：亲眼看到 SUMMARY.md 语法元素（草稿章）如何映射成侧边栏 UI，并体会「serve 热重建」。
2. **操作步骤**：
   - 编辑 `async-book/src/SUMMARY.md`，在第 40 行（capstone 一行）之后新增一行：`- [Future Expansion Ideas]()`；
   - 保存（如果 `mdbook serve` 正在跑，直接看浏览器；没跑就先在 `async-book/` 下 `mdbook serve --open`）；
   - 展开侧边栏 Appendices 分组观察新条目的样式；试着点击它；
   - 还原：删掉新增行并保存，在仓库根目录执行 `git checkout -- async-book/src/SUMMARY.md` 确认还原。
3. **需要观察的现象**：新条目以灰色/禁用链接样式出现，点击不跳转；同时留意侧边栏编号章前面的自动序号与标题手写序号叠加的样子（验证 4.4.2 提到的双序号现象）。
4. **预期结果**：草稿章呈现为不可用的占位条目——这正是「先搭结构后填内容」的工作流工具。**本实践结果待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：前缀章和后缀章有什么共同点？本质区别是什么？

> **答案**：共同点：都不编号、都必须位于列表外的根层级、都不能嵌套。区别只在位置——前缀章必须在所有编号章之前，后缀章在其后。本书的 Introduction 是前缀章，没有后缀章。

**练习 2**：如果 Part II 的第一章会显示自动序号「6.」而不是「1.」，说明编号规则是什么？这对全书 URL 有影响吗？

> **答案**：编号规则是跨 Part 全局连续累加。URL 不受自动序号影响——页面文件名由链接目标（`ch06-building-futures-by-hand.md` → `ch06-building-futures-by-hand.html`）决定，序号只出现在侧边栏标签里。

**练习 3**：`---` 和 `# Part X` 都能在侧边栏形成视觉分组，差别在哪？

> **答案**：`---` 只画一条水平分隔线，不带文字；`# Part X` 是一级标题转换来的不可点击文字标题，还参与逻辑分组（像 Appendices 这样的语义分部）。本书两者叠用：每个 Part 前先一条线再一个标题。

## 5. 综合实践

把本讲四个模块串起来的两步实验（全程在本地做，做完务必还原）：

**准备**：确认已按 [README.md:L81](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L81) 安装 `mdbook@0.4.52` 与 `mdbook-mermaid@0.14.0`，然后在 `async-book/` 目录启动：

```bash
cd async-book
mdbook serve --open     # 浏览器打开 http://localhost:3000
```

（此命令形式与 README 维护者指南的单书预览示例一致，见 [README.md:L100-L104](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L100-L104)。）

**第一步：改 SUMMARY.md 章节标题，观察侧边栏**

1. 编辑 `async-book/src/SUMMARY.md` 第 9 行，把标题改成明显不同的文字，例如：
   `- [1. Why Async is DIFFERENT Now](ch01-why-async-is-different-in-rust.md)`；
2. 保存，回到浏览器——serve 检测到文件变化会自动重建；
3. **观察**：左侧边栏 Part I 下第一条链接文字立即变为新标题；点进第 1 章，页面正文的大标题（来自章节 Markdown 内的 `# …`）**不会**跟着变——标题文字在两处各存一份；
4. 顺手检查第 1 章页面顶部的面包屑/上级导航是否也随侧边栏更新。

**第二步：关掉 line-numbers，观察 playground 差异**

1. 保持 serve 运行，编辑 `async-book/book.toml` 第 21 行：`line-numbers = true` → `line-numbers = false`；
2. 保存后切到第 2 章（The Future Trait），找到开头的 `Future` trait 代码块（源自 [ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24)）；
3. **观察**：可编辑代码区的行号消失，编辑/运行能力仍在（`editable = true` 未动）；
4. 想更进一步可以把第 20 行 `editable` 也改成 `false` 再保存，观察编辑能力一并消失——这验证了 4.3 讲的依赖关系：行号挂在「可编辑」之下。

**收尾（必做）**：`Ctrl+C` 停止 serve，然后在仓库根目录执行：

```bash
git checkout -- async-book/src/SUMMARY.md async-book/book.toml
```

用 `git status` 确认工作区干净（`RustTraining-tutorial/` 等未跟踪目录除外）。这个「改 → 看 → 还原」的循环就是给这套书做内容实验的标准姿势：**源码永远干净，认知留在脑子里**。

**本综合实践的全部运行结果待本地验证**（编写环境未安装 mdbook，未实际执行）。

## 6. 本讲小结

- `book.toml` 的三个基础节各司其职：`[book]` 定身份（标题/作者/src 目录）、`[build]` 定输出（`build-dir` 会被 xtask 的 `--dest-dir` 覆盖）、`[output.html]` 定外观（GitHub 图标、默认 light + 暗色 ayu、额外 JS）；七本书共用同一套模板，只换 `title`。
- `additional-js` 的路径相对**书根**（含 book.toml 的目录）解析，`mermaid.min.js` / `mermaid-init.js` 就躺在 `async-book/` 根下并被 git 跟踪——这是「书根放资产」约定的实证。
- mermaid 是构建期与浏览器期的接力：`[preprocessor.mermaid]` 在构建时改写 ` ```mermaid ` 块，两个 JS 文件在浏览器里画图；缺了 `mdbook-mermaid` 可执行文件，构建直接失败。
- playground 由 `[output.html.playground]` 全局控制：`editable` + `line-numbers` 开启全书可编辑带行号的代码块（行号依赖 editable 与默认为 true 的 copy-js）；`ignore` 等围栏注解可按块排除；无 `fn main` 的片段会被注入隐藏 main 包装再发往 Rust Playground。
- SUMMARY.md 是格式严格的书骨架：前缀章（ch00 Introduction，不编号）、`# Part` 分部标题（不可点击）、`---` 分隔线、跨 Part 连续编号的列表章（1–17 与文件名 ch01–ch17 对齐）、草稿章 `- [标题]()` 占位。
- 引言章里那份手写目录与 SUMMARY.md 是双源，已经出现漂移（漏了第 15 章、capstone 写成 Ch 16）——改章节结构时两处都要同步。

## 7. 下一步学习建议

- **进入书籍内容单元**：配置和骨架已经看透，下一站是 `src/` 里的正文章节。建议从 [ch02-the-future-trait.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L1-L1) 开始通读，留意每章固定的五要素结构（"What you'll learn" 块、Mermaid 图、内嵌练习、Key Takeaways、交叉引用，见 [ch00-introduction.md:L38-L43](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L38-L43)）——这是七本书共用的章节写作模板。
- **横向对比另一本书**：用本讲的方法解剖 `rust-patterns-book/` 或 `type-driven-correctness-book/` 的 `book.toml` 与 `SUMMARY.md`，验证「一套模板走天下」，并观察它们的 Part 划分与本书有何不同。
- **想深挖构建系统**：u2 单元会逐行读 `xtask/src/main.rs`——包括本讲引用过的 `--dest-dir` 覆盖逻辑（[L146-L152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L146-L152)）、`BOOKS` 注册表和落地页生成。
- **延伸阅读（官方文档）**：mdBook 的 SUMMARY.md 格式文档与 HTML 渲染器配置文档（rust-lang.github.io/mdBook 的 *Format › SUMMARY.md* 与 *Configuration › Renderers* 两页）覆盖了本讲未用到的语法（嵌套子章、`edit-url-template`、`[output.html.fold]` 折叠侧边栏等），遇到没见过的配置键时优先查它。
