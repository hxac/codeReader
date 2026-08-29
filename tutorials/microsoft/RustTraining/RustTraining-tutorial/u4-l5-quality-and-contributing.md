# 质量守护与贡献流程

## 1. 本讲目标

本讲是整个学习手册的收官之讲。前面十四讲里我们把仓库拆了个底朝天：构建工具、静态服务器、七本书的内容、部署流水线。这一讲回答两个此前一直悬着的问题：

1. **这个仓库靠什么保证质量？** 它没有单元测试、没有 clippy CI、没有 lint 配置——质量防线完全建立在「部署即验证」的 CI 链路和 mdBook 自身的严格构建行为上。你要能说出这三道防线各自拦什么、漏什么。
2. **我改了东西之后怎么还回去？** 走通 fork → branch → PR → CLA 签署的完整贡献流程，并搞清楚「代码 MIT / 文档 CC-BY-4.0」这条双许可证边界意味着你能怎样复用仓库里的内容。

学完本讲，你应该能独立向 microsoft/RustTraining 提交一个能通过全部 CI 检查的 PR，并且在想搬运书中内容到自己的博客/内部培训材料时，知道自己负有哪些署名义务。

## 2. 前置知识

本讲几乎不涉及新代码，但依赖前面几讲建立的概念。用三段话把需要的背景补齐：

**CI 与 GitHub Actions。** GitHub Actions 是 GitHub 内置的持续集成（Continuous Integration，CI）系统。仓库里 `.github/workflows/` 下的每个 YAML 文件定义一条「workflow」（流水线）；workflow 由 `on:` 块里声明的事件触发（如 push 到 main、pull request、手动 `workflow_dispatch`）；每条 workflow 包含若干 `jobs:`，每个 job 是一台临时虚拟机上的顺序 `steps:`。step 失败（退出码非 0）则整个 job 标红，默认阻断后续依赖它的 job。你在 u4-l1（pages.yml）和 u4-l2（docker.yml）已经精读过两条流水线，本讲把它们当作「质量防线」重新审视，而不是当作部署机制。

**CLA（Contributor License Agreement，贡献者许可协议）。** 开源许可证（MIT、CC-BY 等）管的是「使用者对仓库现有内容有什么权利」；CLA 管的是「贡献者把新内容交给项目时授予项目什么权利」。对微软这样的公司实体，CLA 是它能合法地以自己名义再分发、再许可全部贡献的前提。没有 CLA，你的 PR 在法律上无法被合并。

**双许可证（dual licensing）。** 同一个仓库的不同部分适用不同许可证。本仓库把「软件」（xtask 源码）放在 MIT 下，把「文档」（七本书的 Markdown）放在 CC-BY-4.0 下。这么做的原因是两类内容的复用方式天然不同：代码希望被无摩擦地复制进任何项目，而教学内容希望被署名引用而不是被悄悄搬走。判断「我拿走的东西受哪个许可证管」是本讲要训练的核心能力。

此外 recall 两个此前讲过的关键事实，本讲会反复用到：

- `build_to` 对单本书构建失败只 `eprintln` 告警、不中断循环，最后进程退出码仍是 0（u2-l3、u4-l1）。
- docker.yml 的冒烟测试只断言 async-book 这一本（u4-l2）。

## 3. 本讲源码地图

本讲涉及的文件都不长，但每一份都承担一个明确的治理角色：

| 文件 | 行数 | 作用 |
|---|---|---|
| `.github/workflows/docker.yml` | 61 | 第一道防线：Docker 构建 + 冒烟测试，唯一带断言的 CI |
| `.github/workflows/pages.yml` | 65 | 第二道防线：Pages 部署流水线，构建失败即阻断发布 |
| `xtask/src/main.rs` | — | `build_to` 的失败吞噬行为，是两道防线共同的盲区源头 |
| `CONTRIBUTING.md` | 14 | 贡献入口：CLA 要求与签署方式 |
| `CODE_OF_CONDUCT.md` | 11 | 行为准则声明与联系渠道 |
| `README.md` L1–L11 | 11 | 双许可证与商标条款的「人读侧」声明 |
| `LICENSE` | 21 | MIT 许可证全文（管代码） |
| `LICENSE-DOCS` | 396 | CC-BY-4.0 全文（管文档） |

值得先建立一个事实认知：`.github/workflows/` 目录下**只有这两条流水线**，仓库里没有任何 `tests/` 目录、没有 `#[test]` 函数、没有 rustfmt/clippy 配置。也就是说这个仓库的自动化质量体系是「两条 workflow + mdBook 的严格构建」这三样东西的全部，理解它们的边界就是理解整个仓库的质量模型。

## 4. 核心概念与源码讲解

### 4.1 CI 质量防线

#### 4.1.1 概念说明

大多数 Rust 项目靠 `cargo test` + `cargo clippy` 的 CI 做质量门禁。RustTraining 没有这条路径——它的全部 Rust 代码是一个 500 行左右的构建工具，七本书是 Markdown。所以它的质量模型换了一种形态：

> **把「部署成功」本身当作测试。** 如果落地页能打开、书能被渲染出来、无扩展名链接能解析，那么构建链路就是对的。

这个模型由三层组成，每层拦截不同类型的故障：

| 层 | 拦什么 | 漏什么 |
|---|---|---|
| ① mdBook 严格构建 | SUMMARY 引用不存在的文件、非法配置、预处理器崩溃 | 单书失败被 `build_to` 吞掉退出码 |
| ② pages.yml 部署门禁 | `check_mdbook` 失败、xttask panic、`expect` 崩溃 | 全部「退出码仍为 0」的失败 |
| ③ docker.yml 冒烟断言 | 落地页缺失、async-book 缺失、try_files 失效 | 其余六本书、页面内容正确性 |

关键洞察是：**这三层的防护力依次增强，但覆盖面依次收窄**。第①层覆盖所有书但只产生日志；第②层能阻断发布但被退出码语义欺骗；第③层有真正的 HTTP 断言却只盯一本标本书。理解这个「防护力—覆盖面」的反比关系，比记住任何一条具体规则都重要。

#### 4.1.2 核心流程

一次 push 到 main 之后，两条流水线并行启动。把它们各自遭遇失败时的行为画成对照：

```text
pages.yml                                    docker.yml
─────────                                    ──────────
push main ──► build job                      push main(路径匹配) ──► build job
              │                                                     │
              ├─ checkout / rust / cache                            ├─ checkout / buildx
              ├─ install mdbook+mermaid                             ├─ docker build (push:false)
              ├─ cargo xtask deploy                                 └─ Smoke test
              │      │                                                  ├─ 就绪轮询(最多60s)
              │      ├─ 退出码≠0 ──► job 红色 ──► deploy 不执行        ├─ grep <title> 断言
              │      └─ 退出码=0 ──► 上传 docs/ artifact              ├─ /async-book/ =200 断言
              │                                                     ├─ 无扩展名链接 =200 断言
              └─ deploy job（仅消费 artifact）                        └─ 任一断言失败 ──► job 红色
```

注意两条流水线对「失败」的敏感度不同：pages.yml 只看 `cargo xtask deploy` 一个命令的退出码；docker.yml 里的冒烟测试是真正的**行为断言**——它不问「构建命令成功了吗」，而问「用户能不能真的读到这一页」。这两种验证思路的差别，就是「构建正确性」和「运行正确性」的差别。

#### 4.1.3 源码精读

**盲区的源头：`build_to` 吞掉失败。** 这是全仓库最重要的一个质量语义，值得整段读一遍：

[xtask/src/main.rs:139-161](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L139-L161)

这段代码做的事：遍历 `BOOKS` 注册表，逐书调用 `mdbook build --dest-dir`；`status.success()` 为真就 `ok += 1` 并打 `✓`，为假就走 `else` 分支打 `✗ {slug} FAILED` 到 stderr——然后**什么都不做**，继续下一轮。函数末尾没有任何基于 `ok` 的判断，`build_to` 正常返回，`cmd_build` 正常返回，`main` 正常返回，进程退出码 0。

把这个行为接到 pages.yml 上，后果是：**一本书的 Markdown 写坏了（比如 SUMMARY.md 引用了不存在的文件），流水线依然全绿，站点照常部署，读者点进那本书得到 404。** 唯一的线索藏在 build job 的日志里（`✗ xxx-book FAILED` 和 `6/7 books built`）。这就是 u4-l1 反复强调的「绿色地部署缺书站点」盲区。

顺带一提：`ok` 计数器打印的 `{ok}/{} books built` 是给**人**看的信号，CI 不会读它。设计者显然知道失败不致命（其余书还能读），所以选择了「尽力而为」而不是「一票否决」——这是一个刻意的取舍，不是一个 bug，但它决定了后面两道防线为什么长成那样。

**第二道防线：pages.yml 的退出码门禁。**

[.github/workflows/pages.yml:3-11](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L3-L11)

触发器（`push` 到 main + 手动 `workflow_dispatch`）和最小权限三连（contents 读、pages 写、id-token 写）。注意这里**没有 `pull_request` 触发**——你的 PR 不会跑这条流水线，它只在合并进 main 之后才执行。

[.github/workflows/pages.yml:43-49](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L43-L49)

这两步是防线本体。`cargo xtask deploy` 以非 0 退出时，"Build documentation" step 直接标红，job 失败，`needs: build` 的 deploy job 不会运行——**坏产物到不了线上**。它能拦住的故障是：`mdbook` 完全缺失（u2-l2 讲过 `check_mdbook` 在任何副作用前以码 1 退出）、`project_root` 找不到目录触发 `expect` panic、`fs::remove_dir_all` / `create_dir_all` 失败。它拦不住的，就是上面那段 `build_to` 语义。

**第三道防线：docker.yml 的行为断言。**

[.github/workflows/docker.yml:5-21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L5-L21)

这段配置信息量很大。L3-L4 的注释直接说明了这条流水线的设计哲学：**只构建、不发布**（`push: false`），所以不需要引入任何 registry 凭证，不存在「CI 凭证泄漏导致恶意镜像被推到官方仓库」的攻击面；它存在的唯一理由是「让 Dockerfile 不能悄悄烂掉」。L6-L20 的路径过滤意味着：只有当改动触及 `docker/**`、`.dockerignore`、`xtask/**`、任何 `book.toml` 或这条 workflow 自身时才触发。**特别注意 `pull_request` 也在触发器里**——这是你作为外部贡献者唯一会在自己 PR 上看到的检查。

[.github/workflows/docker.yml:45-61](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L45-L61)

冒烟测试的四个断言，逐条对应它能拯救的场景：

- L48-L54 就绪轮询：起容器后最多重试 30 次 × 2 秒，全部失败则用 `::error::` 注解报错并 dump 容器日志。守护的是「nginx 根本没起来」这类最粗的故障。
- L57 `grep -q "<title>Rust Training Books</title>"`：落地页由 `write_landing_page` 生成，这条断言守护的是 xtask 的 HTML 拼接逻辑没有被改坏。L56 的注释还贴心地解释了为什么 grep `<title>` 而不是 `<h1>`——`<h1>` 被 `<span>` 标签切开了。这是把「怎么写断言」的经验写进 CI 的好例子。
- L58 `/async-book/` 返回 200：**这条部分堵住了 `build_to` 盲区**。如果 mdbook 在镜像里构建 async-book 失败，`site/async-book/` 目录不存在，nginx 返回 404，`grep -q 200` 失败，job 标红。
- L60 无扩展名链接 `/async-book/ch00-introduction` 返回 200：守护 u4-l3 讲过的 `try_files` 三级候选规则，保证 Docker 自托管站与 GitHub Pages 行为一致。

把这四条断言和 4.1.1 的表格对上，你会发现它的覆盖是刻意的「抽样」而非「普查」：用一本标本书代表七本，用一条无扩展名链接代表全站路由。这是 CI 成本与覆盖率的典型折中。

#### 4.1.4 代码实践

**实践：亲手制造一次「绿色的失败」，看清盲区长什么样。**

这个实践不真的提 PR，只在你自己的 fork 里做，目的是让你对「退出码 0 但书没构建出来」有一个身体层面的记忆。

1. **实践目标**：观察 `build_to` 吞掉单书失败后，CI 与站点各自呈现什么状态。

2. **操作步骤**：

   a. Fork 仓库到你的账号，克隆你的 fork，并在仓库根目录安装工具链（参照 u1-l3）。

   b. 人为弄坏一本书——最可靠的方式是让 mdbook 找不到一个章节文件。编辑 `async-book/src/SUMMARY.md`，把最后一章的文件名改成一个不存在的路径，例如把某个 `chXX-....md` 条目改成 `ch99-does-not-exist.md`。保存。

   c. 本地先验证故障形态：`cargo xtask build`。观察输出里的 `✗ async-book FAILED` 和末尾的 `6/7 books built`，然后 `echo $?` 确认退出码。

   d. 还原 `SUMMARY.md`（`git checkout -- async-book/src/SUMMARY.md`），确认 `cargo xtask build` 恢复 `7/7`。

   e. 再用另一种方式弄坏它——把 `book.toml` 里的 `[preprocessor.mermaid]` 节临时删掉（或者卸载 mdbook-mermaid），重复 c 步，观察七本书是全部失败还是部分失败。

   f. 把 c 步的破坏状态提交并推到你 fork 的 main 分支（注意这是你自己的 fork，不影响上游），在 Actions 页面看 pages.yml 的颜色，再看部署出来的站点上 async-book 的卡片是不是死链。

3. **需要观察的现象**：本地 `cargo xtask build` 打印 `✗ async-book FAILED`，但 `echo $?` 输出 `0`；你 fork 上的 pages.yml 全绿；线上站点点进那本书是 404。

4. **预期结果**：你会亲眼看到「日志里有一行 ✗、流水线全绿、线上死链」三者同时成立。这就是 4.1.1 表格里第②层防线的盲区。

5. docker.yml 在这个实验里**不会**变红——它没有被触发，因为你只改了 `SUMMARY.md`，不在它的路径过滤列表里；即便被触发，断言盯的 `/async-book/` 目录是否 200 也取决于 mdbook 失败时是否留下部分产物，行为需要实际观察，**待本地验证**。

清理：还原所有文件，删掉你 fork 上的实验分支，避免后续实践误用它。

#### 4.1.5 小练习与答案

**练习 1**：`build_to` 里 `ok` 计数器的唯一消费者是谁？如果要把它变成硬门禁，最小改动是什么？

**答案**：唯一消费者是 `println!("\n  {ok}/{} books built", BOOKS.len())` 这行日志，人和 CI 都不读它。最小改动是在 `build_to` 末尾（或 `cmd_build`/`cmd_deploy` 里）加一句 `if ok < BOOKS.len() as u32 { std::process::exit(1); }`。但要意识到代价：这会把「一本书坏掉、六本书还能读」变成「全站拒绝部署」，对一个培训书库来说未必是更好的取舍——这正是原设计选择尽力而为的原因。

**练习 2**：为什么 docker.yml 的冒烟测试选择 `grep "<title>..."` 而不是 `grep "<h1>..."`？

**答案**：因为落地页 HTML 里 `<h1>` 标签内部被 `<span>` 标签分隔（比如标题文字和副标题分属不同节点），`grep` 是纯文本匹配，跨标签的字符串匹配不到。`<title>` 是完整连续的文本节点，匹配稳定。这是 docker.yml L56 注释里明说的。它教给我们的通用规则是：对 HTML 做断言时，优先选天然连续的文本节点，或者改用真正的 HTML 解析器。

**练习 3**：你的 PR 只改了某本书的一个章节正文，CI 会跑哪些检查？

**答案**：一条都不会跑。pages.yml 没有 `pull_request` 触发器（只在 push 到 main 后执行）；docker.yml 有 `pull_request` 触发，但路径过滤列表是 `docker/**`、`.dockerignore`、`xtask/**`、`**/book.toml`、workflow 自身——`.md` 章节文件不在其中。所以纯内容 PR 的质量完全依赖你的本地 `mdbook serve` 预览和维护者的人工评审。

### 4.2 CLA 与贡献流程

#### 4.2.1 概念说明

这一模块讲「怎么把改动还回去」。仓库的贡献入口是 `CONTRIBUTING.md`，全文只有 14 行——这在开源项目里属于极简风，但它把两件法律层面必需的事说清楚了：

1. **贡献前需要签 CLA**，因为你提交的内容将被微软以仓库许可证（MIT/CC-BY-4.0）再分发，法律上需要你先授权。
2. **CLA 的执行是自动化的**：PR 提交后由 bot 判定并装饰，你跟着指引点链接签署即可，且一次签署对使用同一 CLA 的所有微软仓库永久有效。

与之配套的 `CODE_OF_CONDUCT.md` 是行为准则（Code of Conduct，社区规范），它不约束代码质量，约束的是参与者在 issue 和 PR 里的行为。这两份文件加上双许可证，构成了仓库完整的「治理四件套」。

一个值得注意的观察：仓库**没有**贡献模板（`.github/PULL_REQUEST_TEMPLATE.md` 不存在）、没有 issue 模板、没有 styleguide。这意味着「什么样的 PR 会被接受」没有成文标准，事实上的标准就是 u3-l2 拆解过的章节写作范式——目标框、Mermaid 图、自包含代码、Key Takeaways。

#### 4.2.2 核心流程

一个完整贡献的生命周期：

```text
fork ──► git clone 你的fork ──► 新建分支
  ──► 修改内容 ──► 本地验证（cargo xtask build / mdbook serve --open）
  ──► commit & push 到你的fork
  ──► 在 GitHub 上开 PR（base = microsoft/RustTraining main）
  ──► CLA bot 检查：
        ├─ 已签过 ──► 通过，PR 打上绿色标记
        └─ 没签过 ──► bot 留评论 + 标签，给出 cla.microsoft.com 链接
                        └─ 你点链接完成签署 ──► bot 自动更新状态
  ──► CI（视改动路径，可能触发 docker.yml 的 pull_request 检查）
  ──► 维护者评审 ──► 合并
  ──► push 到 main 自动触发 pages.yml ──► 几分钟后线上可见
```

流程里有两个容易被外部贡献者忽略的点。第一，**合并才是验证的起点**：你的纯内容 PR 在 PR 阶段没有任何 CI 跑，pages.yml 要等合并进 main 才执行，所以本地 `cargo xtask build` 确认 `7/7 books built` 不是可选项，而是你唯一的门禁。第二，**改 `book.toml` 会触发 docker.yml**：如果你新增一本书或动了配置，PR 上会出现 Docker 构建检查，这是你提前发现自己是否漏了 mermaid 资产的机会（u4-l4 讲过 `additional-js` 相对书根解析的坑）。

#### 4.2.3 源码精读

**CONTRIBUTING.md：CLA 要求。**

[CONTRIBUTING.md:3-10](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CONTRIBUTING.md#L3-L10)

这段把 CLA 的「是什么、为什么、怎么办」压缩进八个句子。L3-L6 是定义与动机：CLA 声明你**有权**且**实际**授予仓库使用你贡献的权利（"have the right to, and actually do, grant us the rights"）。L8-L10 描述自动化流程：CLA-bot 会在 PR 上自动判定（"automatically determine"）并用标签和评论装饰 PR（"label, comment"），你只需按 bot 的指引操作。最关键的信息在 L10——"You will only need to do this once across all repositories using our CLA"，签署一次全仓库通用，这不是每个仓库重复一遍的流程。

[CONTRIBUTING.md:12-14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CONTRIBUTING.md#L12-L14)

行为准则的引用与联系方式。注意这两行同时出现在 `CODE_OF_CONDUCT.md` 里——仓库治理文件的常见模式是「一处全文、多处引用」。

**CODE_OF_CONDUCT.md：行为准则。**

[CODE_OF_CONDUCT.md:3-10](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/CODE_OF_CONDUCT.md#L3-L10)

L7-L9 给出三个对外渠道：行为准则原文、FAQ、以及举报邮箱 `opencode@microsoft.com`。L10 是给微软内部员工的额外渠道。作为贡献者你需要知道的核心义务是：在 issue 和 PR 里保持专业与尊重，具体细则在外部链接里，仓库本地文件只是一份指针。这种「引用而不复制」的写法让准则更新时不需要同步几百个仓库。

#### 4.2.4 代码实践

**实践：从零走通一次 CLA 签署 + PR 提交（不改任何内容也能做）。**

1. **实践目标**：体验 CLA bot 的判定流程，熟悉贡献链路，为 4.3 和综合实践做演练。

2. **操作步骤**：

   a. 如果你还没有 GitHub 账号，注册一个。Fork `microsoft/RustTraining` 到你的账号下。

   b. 本地克隆你的 fork：`git clone https://github.com/<你的用户名>/RustTraining.git && cd RustTraining`。

   c. 建分支：`git checkout -b docs/cla-rehearsal`（分支名习惯上用 `docs/`、`fix/`、`feat/` 前缀标明改动类型）。

   d. 做一个最小改动。推荐修一个真实存在的瑕疵来练手：用 u1-l4 讲过的「引言章手写目录与 SUMMARY.md 双源漂移」现象——对照 `async-book/src/SUMMARY.md` 和 `async-book/src/ch00-introduction.md` 里手写的章节列表，把不一致处修正。找不到就改一处错别字或补一个断链。`git add` + `git commit`。

   e. 推送并开 PR：`git push -u origin docs/cla-rehearsal`，然后 `gh pr create --repo microsoft/RustTraining --fill`（或用网页界面）。

   f. 观察 PR 页面：一两分钟内会出现 CLA bot 的检查项。如果你从未签过，它会留一条带 `https://cla.microsoft.com` 链接的评论。

   g. 点链接完成签署（通常是电子签署，几分钟内生效），回到 PR 看 bot 状态自动转绿。

3. **需要观察的现象**：CLA bot 检查项从「待处理/失败」变为「通过」；PR 的 Checks 区域列出哪些 workflow 运行了。

4. **预期结果**：签署完成后 CLA 门禁通过。若你改的只是 `.md` 文件，Checks 里应该没有 docker.yml（路径不匹配）；若你动了 `book.toml` 或 `xtask/`，会看到 "Docker image" workflow 出现。

5. 如果这个 PR 不打算真的提交给维护者评审，签署验证完成后可以自行关闭它（CLA 签署保留在你账号上，不随 PR 关闭失效）。**待本地验证**：bot 评论出现的时机和文案可能随 GitHub App 版本变化。

#### 4.2.5 小练习与答案

**练习 1**：CLA 和开源许可证（MIT）各自解决什么问题？一个 PR 同时受两者约束吗？

**答案**：许可证解决「公众对仓库现有内容有什么权利」——它面向使用者，授权方向是仓库→公众。CLA 解决「贡献者交给项目什么权利」——它面向贡献者，授权方向是贡献者→项目，让微软能合法地把你的改动纳入仓库并以 MIT/CC-BY-4.0 再分发。是的，你的 PR 同时受两者约束：先经 CLA 把权利交给项目，合并后你的内容成为仓库的一部分，下游使用者按许可证取得权利。

**练习 2**：为什么仓库本地只有 14 行的 CONTRIBUTING.md 和 11 行的CODE_OF_CONDUCT.md，而不是把全文写进仓库？

**答案**：因为两者都采用「指针」模式。行为准则全文托管在 `opensource.microsoft.com/codeofconduct`，更新时所有引用仓库自动跟进，不需要逐一改几百个仓库；CLA 流程完全由 `cla.microsoft.com` 的服务端和 bot 驱动，仓库里只需要说明去向。这也是为什么这两份文件几乎全由链接构成——它们的本体是服务，不是文本。

**练习 3**：你提交了一个只改 `python-book/src/` 下章节的 PR，哪些 CI 会跑？如果合并后那本书构建失败，谁最先发现？

**答案**：没有 CI 会跑（pages.yml 无 pull_request 触发；docker.yml 的路径过滤不含 `python-book/src/**`）。合并后那本书构建失败时，pages.yml 依然全绿（`build_to` 退出码 0），最先发现的只能是点开死链的读者，或在 Actions 日志里翻到 `✗ python-book FAILED` 的维护者。这正是 4.1 讲的盲区在真实贡献场景中的落点。

### 4.3 双许可证边界

#### 4.3.1 概念说明

这是本讲最需要精确性的模块：**判断你想复用的东西落在哪一侧的许可证下**。

仓库的双许可证声明写在 README 顶部的两个灰色框里。边界不是按目录硬切的（没有「docs/ 走 CC-BY、src/ 走 MIT」的显式清单），而是按**内容的性质**划分：

| 内容 | 适用许可证 | 关键义务 |
|---|---|---|
| `xtask/` 的 Rust 源码 | MIT | 保留版权与许可声明 |
| `docker/`、`.github/` 的配置 | MIT（视作软件的一部分） | 保留版权与许可声明 |
| 七本书的 Markdown（`*-book/src/`） | CC-BY-4.0 | 署名 + 标明修改 + 保留许可链接 |
| `mermaid.min.js` 等第三方资产 | 各自的 MIT（文件头注明 mermaid-js 项目） | 保留其文件头声明 |

两个许可证的气质差异值得先建立直觉：**MIT 是「拿走别告我」**——义务极轻（保留一段声明），允许商用、修改、再许可、闭源。**CC-BY-4.0 是「拿走要署名」**——同样允许商用和改编，但署名义务具体得多，且明确禁止对下游加额外限制。Creative Commons 官方也不推荐用 CC 许可证管软件，本仓库把两者分开正是遵循了这个惯例。

还要补一个 README 里单独声明、容易被忽略的边界：**商标不在任何许可证授权范围内**。

#### 4.3.2 核心流程

判断「我能怎么用这段内容」的决策树：

```text
我想复用仓库里的东西
  │
  ├─ 是代码（xtask 源码 / Dockerfile / nginx.conf / workflow YAML）？
  │    └─ MIT：
  │         ├─ 可以：复制进任何项目、修改、商用、再许可、闭源
  │         └─ 义务：在你的发行物里保留 LICENSE 的版权与许可声明
  │
  ├─ 是书的内容（章节 Markdown、Mermaid 图、示例代码讲解文字）？
  │    └─ CC-BY-4.0：
  │         ├─ 可以：全文转载、翻译、改写、编入内部培训材料、商用
  │         ├─ 义务（LICENSE-DOCS §3(a)(1)）：
  │         │    ① 标注创作者与版权声明
  │         │    ② 保留指向本许可证的链接
  │         │    ③ 标明你做了修改
  │         │    ④ 尽可能给出原文链接
  │         └─ 禁止（§2(a)(5)(b)）：对下游施加额外限制或技术措施
  │
  └─ 涉及 Microsoft 商标 / 名称 / Logo？
       └─ 两个许可证都不授权，须另循微软商标与品牌准则
```

一条容易踩的灰色地带：**书里的 Rust 示例代码**算「文档」还是「软件」？保守做法是按 CC-BY-4.0 处理（因为它以 Markdown 正文形式存在），并在你无法确定时同时满足两侧义务——保留 MIT 声明 + 给出署名，成本极低而风险归零。

#### 4.3.3 源码精读

**README：双许可证与商标声明。**

[README.md:1-11](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L1-L11)

L3 是双许可证的「人读侧」总声明：项目在 MIT 和 CC-BY-4.0 下双许可，并链接到 `LICENSE` 与 `LICENSE-DOCS` 两个文件。L9 是商标条款，三个要点：授权使用的微软商标须遵循微软的商标与品牌准则；**在修改版中使用微软商标不得造成混淆或暗示微软背书**（"must not cause confusion or imply Microsoft sponsorship"）；第三方商标归第三方政策管。这句话对你的实际影响是：fork 这个仓库做公司内训站完全没问题，但别在改过的版本上保留会让人以为是微软官方出品的标识。

**LICENSE（MIT）：轻义务许可。**

[LICENSE:5-13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE#L5-L13)

这是 MIT 的核心条款。L5-L10 授权：无偿获得本软件及文档副本的人可以**不受限制地**处置（"without restriction"）——使用、复制、修改、合并、发布、分发、再许可（sublicense）、销售。L12-L13 是唯一条件：在**所有副本或实质部分**中保留版权声明与本许可声明。对 xtask 源码来说，这意味着你把 `build_to` 抄进自己的构建工具时，只需要在你的发行物某处带上这段 21 行的 LICENSE 文本。

[LICENSE:15-21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE#L15-L21)

免责与责任限制：软件按「现状」提供，不作任何明示或默示担保，作者不对任何索赔负责。通俗说：代码出了问题别来找作者。

**LICENSE-DOCS（CC-BY-4.0）：署名是核心条件。**

[LICENSE-DOCS:135-146](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L135-L146)

授权范围（Section 2(a)(1)）：全球范围内、免版税、不可再许可（non-sublicensable）、非独占、**不可撤销**（irrevocable）的许可，允许 (a) 复制与分享全部或部分许可材料，(b) 生产、复制与分享**改编材料**（Adapted Material）。也就是说你不仅可以转载，还可以翻译、删节、改写、重新组织成课件——这与 MIT 的差别在于授权的对象是「作品」而非「软件」，Creative Commons 体系天然面向创作物。

[LICENSE-DOCS:167-180](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L167-L180)

**反下游限制条款**（Section 2(a)(5)）：每个接收者自动获得来自许可人的同等要约（L169-L173）；但你**不得**对接收者施加任何额外或不同的条款件，也不得施加有效技术措施，如果这会限制他们行使许可权利（L175-L180）。实务含义：你不能把书的内容装进一个「禁止再转发」的内部课件里分发，也不能用 DRM 锁死它——CC-BY 的自由必须能一路传递下去。

[LICENSE-DOCS:215-244](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L215-L244)

**署名条件**（Section 3(a)(1)），这是你复用文档时真正要执行的清单。若你分享许可材料（包括修改形式），必须：保留创作者标识与版权声明（L220-L229）；保留指回本许可证的声明与免责声明链接（L231-L234）；尽可能给出原文 URI（L235-L236）；**标明你做了修改并保留先前修改的标记**（L239-L240）；注明材料按本许可证授权并附许可证文本或链接（L242-L244）。L246-L249 补了一个友好条款：你可以按媒介合理选择满足方式，比如直接放一个包含这些信息的超链接就够——所以你的课件末尾放一行「内容改编自 microsoft/RustTraining（CC-BY-4.0），有修改」加链接，通常即合规。

[LICENSE-DOCS:313-339](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L313-L339)

**期限与终止**（Section 6）：许可证随版权存续，但若你违反条款，权利**自动终止**（L315-L318）。救济通道有两条：发现违规后 30 天内改正则自动恢复（L323-L325），或由许可人明示恢复（L327）。这条对运维场景很有用——一次疏忽不是永久拉黑，及时补救即可。

[LICENSE-DOCS:199-201](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/LICENSE-DOCS#L199-L201)

明确不授权的内容（Section 2(b)(2)）：**专利权与商标权不在本许可证授权范围内**。这呼应了 README 的商标条款——CC-BY-4.0 给你的是著作权层面的自由，名称和标识的使用另算。

**第三方资产的一个实例。**

[async-book/mermaid.min.js:1-2](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/mermaid.min.js#L1-L2)

每本书目录里携带的 mermaid.min.js 文件头声明它自身是 MIT 许可、版权归 Knut Sveidqvist，并指向 mermaid-js 仓库的 LICENSE。这提醒我们一个容易被双许可证叙述掩盖的事实：仓库里还嵌着第三方资产，它们带着各自的许可条款。你复用书的 HTML 产物时，这个 JS 文件遵循的是它自己的 MIT 声明，而不是仓库的两份许可证——好在两者义务都轻，但概念上要分清。

#### 4.3.4 代码实践

**实践：为一次「搬书」做合规检查。**

1. **实践目标**：把抽象的许可条款落成一次具体的合规操作，训练「先判断边界再动手」的习惯。

2. **操作步骤**：

   a. 设定场景：你想把 async-book 第 2 章（Future trait）翻译成中文，发到自己团队的知识库。

   b. 先跑一遍 4.3.2 的决策树，写下你的判断：这是「书的内容」→ CC-BY-4.0。

   c. 逐条核对 Section 3(a)(1) 的五项要求，为你的译文起草一段署名声明，至少包含：原作者归属（microsoft/RustTraining）、原文链接（指向 GitHub 上的 ch02 源文件或线上页面）、许可证名称与链接（CC-BY-4.0）、「本译文有修改」的说明。用 `gh api repos/microsoft/RustTraining` 或网页确认仓库的规范归属写法。

   d. 再检查反下游限制：确认你的知识库**没有**对这页内容设置「禁止转载」或访问技术锁。如果你们平台默认给所有页面加了这类限制，说明这条路径不合规，需要换平台或另行授权。

   e. 反向练习：再为「把 xtask 的 `percent_decode_path` 函数抄进公司内部 CLI」起草一段 MIT 合规声明。对比两份声明的长度差异，体会两个许可证义务量的落差。

   f. 边界判断练习：如果你搬的不是译文，而是把第 2 章里的 Rust 示例代码原样抽出来放进自己的库，该按哪个许可处理？写下你的判断和理由（参考 4.3.2 末段的保守策略）。

3. **需要观察的现象**：CC-BY 声明需要四到五个要素才能合规，而 MIT 声明只需要一段固定文本。

4. **预期结果**：得到两份可直接使用的署名模板，以及一套可复用的「先分类、再查条款、最后落成声明」流程。

5. 本实践是法律素养训练，不构成法律意见；涉及商业用途或大规模分发时，**建议咨询法务**。

#### 4.3.5 小练习与答案

**练习 1**：同事说「这仓库是 MIT 的，所以我可以把书整本搬进我们的闭源收费课程」。哪里错了？

**答案**：两处。第一，MIT 只覆盖代码侧（xtask 等），书的内容在 CC-BY-4.0 下——而且 CC-BY-4.0 恰恰**允许**商用和改编，所以「收费」本身不是问题。真正的错误在第二处：闭源收费课程如果对接收者施加了限制（禁止再分发、技术锁），就违反了 LICENSE-DOCS §2(a)(5)(b) 的反下游限制条款；同时如果省掉了署名与许可声明，也违反 §3(a)(1)。正确做法是：收费可以，但课程中来自本书的部分必须附带署名、原文链接、修改声明，且不得限制学员对这些部分行使 CC-BY 权利。

**练习 2**：仓库里 `docker/nginx.conf` 应该按哪个许可证处理？依据是什么？

**答案**：按 MIT 处理。它是对软件运行方式的配置描述，属于仓库的「软件侧」基础设施，与 `xtask/`、`.github/` 同类。严格地说，README L3 的双许可证声明没有逐文件划分边界，此时适用 4.3.2 的按内容性质判断：不是文学/教学创作物，就走 MIT。对应的义务只是在你发行物里保留那段 MIT 声明。

**练习 3**：你违反了 CC-BY-4.0 的署名要求，三个月后发现了。你的权利状态是什么？能补救吗？

**答案**：按 §6(a)，你违反条款的瞬间权利**自动终止**——不需要许可人起诉或通知。但 §6(b)(1) 给了补救通道：在发现违规后 30 天内完成改正（补上署名），权利自改正之日起自动恢复；超过 30 天或不想等，则只能由许可人明示恢复（§6(b)(2)）。所以答案是：当下你可能处于无权使用状态，但立即补救通常能自动恢复。

## 5. 综合实践

**综合实战：把 u3-l2 的章节草稿变成一个通过全部检查的真实 PR。**

这是整本学习手册的终点任务，它把你在这个仓库学到的所有东西——写作范式、构建链路、CI 行为、许可证义务——串成一条完整的交付线。

**背景**：u3-l2 的实践让你写过一个 300–500 字的小节草稿（含目标框、Mermaid 图、可运行 rust 块）。现在把它真正交出去。

**步骤**：

1. **选题定位**。在七本书里找一个你的草稿最适合安放的位置。优先选择「补充现有章节的缺角」而不是「新增一章」——后者要动 SUMMARY.md 与 Part 划分，评审阻力大得多。一个可靠的切入点是给某本书的练习章（如 `async-book/src/ch15-exercises.md`）增加一道你设计的练习。

2. **对齐范式**。重读 u3-l2 的四要素（难度 emoji、What you'll learn 目标框、正文、Key Takeaways），逐项检查你的草稿。确认代码块自包含（不依赖前文定义）、Mermaid 语法能被 mdbook-mermaid 渲染、`rust` 代码块能通过 playground 运行或正确标注 `rust,ignore`。

3. **本地门禁**。这是纯内容 PR 唯一的门禁（4.2.2 讲过 PR 上没有 CI 跑）：
   ```bash
   cd <你的书目录> && mdbook serve --open   # 逐项目检视渲染效果
   cd .. && cargo xtask build               # 确认输出 "7/7 books built"
   ```
   如果看到任何 `✗ ... FAILED`，修复后再继续——你不想让 4.1 讲的那个盲区由你的 PR 兑现。

4. **走贡献流程**。fork → branch（建议 `docs/add-xxx-exercise`）→ commit → push → `gh pr create --repo microsoft/RustTraining`。PR 描述里写清楚：改了哪本书哪个位置、为什么、本地验证做过什么。

5. **处理 CLA**。按 4.2.4 的流程完成签署，确认 bot 状态转绿。

6. **观察 CI**。如果你的 PR 只含 `.md`，Checks 应该是空的；如果你被迫动了 `book.toml`（比如新书），"Docker image" workflow 会出现——这时盯紧它，那是你提前发现自己漏了 mermaid 资产的最后机会（u4-l4 的坑）。

7. **许可自查**。你的 PR 内容是你原创的，签署 CLA 后交给仓库以双许可证发布，这一步通常无额外动作；但如果你在草稿里引用了别处看来的表述，先按 4.3 的决策树确认你能合法地提交它。

**验收标准**：PR 上 CLA 通过、无红色 CI（或有且已修复）、维护者评审意见得到响应。无论最终是否被合并，你已经完整走过一次「内容→范式→本地验证→流程→法律」的全链路——这正是这套学习手册想交付的能力。

## 6. 本讲小结

- 仓库的自动化质量体系只有三样东西：mdBook 的严格构建、pages.yml 的退出码门禁、docker.yml 的冒烟断言——没有单元测试、没有 clippy/fmt CI、没有 PR 模板。
- 三道防线呈「防护力递增、覆盖面递减」的反比关系：mdBook 覆盖所有书但只产日志；pages.yml 能阻断发布却被 `build_to` 的退出码语义欺骗；docker.yml 有真正的 HTTP 行为断言却只盯 async-book 一本标本。
- 核心盲区在 [xtask/src/main.rs:154-159](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L154-L159)：单书失败只告警不阻断，`7/7` 变 `6/7` 的信号只给人看，CI 不读——一次纯内容的坏 PR 可以全绿合并并部署出死链。
- 贡献流程的法定前提是 CLA：CONTRIBUTING.md 说明 bot 自动判定、一次签署全仓库通用；纯内容 PR 在 PR 阶段没有任何 CI，本地 `cargo xtask build` 确认 `7/7` 是你唯一的门禁。
- 双许可证按内容性质划界：代码走 MIT（义务＝保留一段声明），书的内容走 CC-BY-4.0（义务＝署名＋标明修改＋许可链接，且禁止对下游加限制）；微软商标在两个许可证之外，修改版不得暗示官方背书。
- 仓库里还嵌着带自身 MIT 声明的第三方资产（mermaid.min.js），复用 HTML 产物时这类文件的条款要单独看。

## 7. 下一步学习建议

本讲是 u4 的最后一讲，也是整条依赖链的终点。到这里你已经完整读过仓库的全部基础设施代码和一条内容深潜路线。三个方向可以继续：

1. **补齐内容层的深潜**。本手册的内容层只精读了 async-book（u3-l4）与两本高阶书的概览（u3-l5、u3-l6）。如果你在做 Rust 后端，按 u3-l3 选一座自己的桥，再走 engineering-book 的 ch11 生产 CI/CD 章，把 u4-l1 讲的缓存与并行策略对照你们团队的流水线读一遍。

2. **把这个仓库当作「小型基础设施」的参照系**。你在 u2 精读的零依赖静态服务器（路径安全、MIME、优雅退出）和 u4 的 nginx 加固，是一套可以直接搬到自己项目里的对照实现。下次写内部工具时，问自己：这里用 xtask 模式能不能替代那个脆弱的 shell 脚本？

3. **回馈上游**。本讲的盲区分析本身就是一张贡献清单：给 `build_to` 加 `ok` 门禁、给 docker.yml 补一条其他书的断言、修 README 与 BOOKS 的元数据漂移（u1-l1 讲过）、补齐引言章手写目录的漂移（u1-l4 讲过）。这些都是小而真实的 PR，正好用综合实战的流程交出去。

如果只能带走一件事：**退出码是给机器读的契约，日志是给人读的线索，一个健康的质量体系不能只靠后者。**
