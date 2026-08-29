# GitHub Pages 自动部署流水线

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐段读懂 `.github/workflows/pages.yml`：它何时被触发、申请了哪些权限、如何避免并发部署互相踩踏。
2. 说清 cargo 缓存的键设计（`hashFiles('**/Cargo.lock')`）与 `restore-keys` 前缀回退的原理，以及为什么缓存列表里会出现 `~/.cargo/bin`。
3. 解释「build job 产出 artifact → deploy job 消费 artifact」的两阶段结构，并能回答本讲的核心问题：**为什么 deploy job 不需要重新构建（甚至没有 checkout 代码）**。
4. 独立触发一次部署（push 或手动 `workflow_dispatch`），并在 Actions 页面定位失败时该看哪个 job、哪个 step。
5. 把 CI 里的 `cargo xtask deploy` 与 u2-l3 精读过的 `cmd_deploy` / `build_to` 源码一一对应起来。

## 2. 前置知识

本讲是学习手册的部署篇第一节，第一次接触 GitHub Actions 的读者请先过一遍下面的名词表：

| 术语 | 通俗解释 |
| --- | --- |
| workflow（工作流） | 一个放在 `.github/workflows/` 下的 YAML 文件，描述「什么事件发生时，在什么机器上，按什么步骤做什么」。本仓库只有两个：`pages.yml`（本讲）和 `docker.yml`（下一讲）。 |
| 触发器（`on:`） | 「什么事件启动这条流水线」，例如某分支收到 push、或有人在网页上手动点按钮（`workflow_dispatch`）。 |
| job | 一组步骤的集合，跑在一台**全新的临时虚拟机**（runner，这里是 `ubuntu-latest`）上。job 之间默认并行，用 `needs:` 声明先后依赖。 |
| step | job 里的最小执行单元。`uses:` 引用别人写好的现成动作（action），`run:` 直接执行 shell 命令。 |
| GITHUB_TOKEN | GitHub 为每次运行自动签发的临时令牌，权限由 workflow 的 `permissions:` 块显式声明，任务结束即失效。 |
| artifact（构建产物） | job 之间传递文件的包裹。build job 把 `docs/` 目录打成包裹上传，deploy job 取包裹发布——这是本讲两阶段衔接的关键。 |
| environment | GitHub 的部署环境记录（这里是 `github-pages`），可以挂审批保护规则，也能在 PR 和仓库页面上显示部署历史与站点 URL。 |
| concurrency | 并发控制：把多条流水线归入同一个组，让它们排队或互相取消，避免两次部署同时写 Pages。 |

几个延续自前面讲义的认知，本讲直接使用不再重复：

- `cargo xtask deploy` 是 `.cargo/config.toml` 里别名的展开，等价于 `cargo run --package xtask -- deploy`（u2-l1）。
- `build_to("docs")` 会先删后建 `docs/` 目录，遍历 `BOOKS` 注册表逐书调用 `mdbook build --dest-dir`，收尾写落地页和零字节 `.nojekyll`（u2-l3）。
- `docs/` 是**不入库**的构建产物，磁盘上平时并不存在（u1-l2）——这一点在理解「CI 里它从哪来」时非常关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [.github/workflows/pages.yml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml) | 本讲主角：整条 Pages 部署流水线，共 65 行，分 build 与 deploy 两个 job。 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | CI 第 49 行那条 `cargo xtask deploy` 的落地实现：`main` 分发 → `cmd_deploy` → `build_to`。 |
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 文档侧印证：命令表和「push 到 main 自动部署」的说明。 |
| Cargo.lock | 根目录的 workspace 级锁文件，是缓存键 `hashFiles('**/Cargo.lock')` 的哈希输入。 |

## 4. 核心概念与源码讲解

### 4.1 触发与权限：流水线的「门禁」

#### 4.1.1 概念说明

一个 workflow 文件的前十几行回答三个问题：**谁能让它跑起来**（触发器）、**它能动仓库里的什么**（权限）、**多条同时触发怎么办**（并发控制）。这三个问题都属于「门禁」：写松了有安全与稳定性风险，写紧了流水线自己会失败。

最小权限（least privilege）原则的含义是：GITHUB_TOKEN 默认可能拿到较宽的权限（取决于仓库设置），显式声明的 `permissions:` 会把它**收窄到恰好够用**。对部署类 workflow 来说，这不仅是防御纵深——`deploy-pages` 这类官方 action 明确要求 `pages: write` 和 `id-token: write`，缺了会直接失败，所以权限块同时也是一份「这条流水线需要什么」的自描述文档。

#### 4.1.2 核心流程

```text
事件发生？
  ├─ push 到 main 分支 ──────────┐
  └─ 网页/CLI 手动触发 dispatch ──┤
                                 ▼
                    检查 concurrency 组 "pages"
                    ├─ 组内无正在进行的运行 → 立即开始
                    └─ 组内有运行中 → 排队等待（不取消对方）
                                 ▼
                    以声明的三向权限运行 build job
```

并发组值得展开一句：`group: "pages"` 把本 workflow 的所有运行归入同一组，同组同时只允许一个「进行中」；`cancel-in-progress: false` 表示后来者**排队而不是取消**前者。这对部署场景是对的——你绝不希望两次部署同时写 Pages 造成站点半新半旧，但也不希望把已经写到一半的部署拦腰砍断。

#### 4.1.3 源码精读

触发器：push 限定在 `main` 分支，另开一个手动入口 [`workflow_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_dispatch)：

- [.github/workflows/pages.yml:3-6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L3-L6) — `on:` 块：`push.branches: [main]` 与 `workflow_dispatch`。前者意味着日常每次合并到 main 都会触发部署；后者让你不 push 任何提交也能重跑整条流水线，是排查问题的入口。

权限三连：

- [.github/workflows/pages.yml:8-11](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L8-L11) — `contents: read` 供 checkout 拉代码；`pages: write` 供部署 action 写 Pages 服务；`id-token: write` 允许 action 领取 OIDC 令牌向 Pages 证明「我真的是这条流水线」。三者都是官方 `deploy-pages` 模式的硬性要求。

并发控制：

- [.github/workflows/pages.yml:13-15](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L13-L15) — `concurrency` 组名为 `"pages"`，`cancel-in-progress: false`：同组串行、后来者排队。

文档侧的印证（README 明确说不需要任何手动步骤）：

- [README.md:108](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L108) — 「The site auto-deploys to GitHub Pages on push to `main` via `.github/workflows/pages.yml`. No manual steps needed.」与触发器配置互为表里。
- [README.md:59](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L59) — 部署的最终出口是 <https://microsoft.github.io/RustTraining/>。

#### 4.1.4 代码实践

1. **实践目标**：确认自己能区分两种触发路径，并理解排队行为。
2. **操作步骤**：
   - 在仓库网页进入 Actions → 左侧选「Deploy to GitHub Pages」→ 右上角 Run workflow 按钮，这就是 `workflow_dispatch` 入口（只读浏览即可，不必真的点击）；
   - 连续快速触发两次（或阅读下述思考题），对照 `concurrency` 配置推演行为。
3. **需要观察的现象**：Actions 列表里第二次运行的状态标记（排队 Queued / 进行中 In progress / 被取消 Cancelled）。
4. **预期结果**：第二次运行应显示排队，直到第一次的整条 workflow 结束才开始——因为组内 `cancel-in-progress: false`。（待本地验证：具体 UI 文案以 Actions 页面实际显示为准。）

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `permissions:` 里的 `id-token: write`，流水线会在哪一步、以什么方式失败？

**答案**：构建本身不会失败——`cargo xtask deploy` 只需要 `contents: read`（checkout）和磁盘写权限。失败发生在 deploy job 的 `deploy-pages` 步骤：该 action 需要 OIDC 令牌才能向 Pages 服务认证，缺少 `id-token: write` 会报权限不足错误。这也说明权限块精确对应到「哪个 job 需要什么」。

**练习 2**：把 `cancel-in-progress` 从 `false` 改成 `true` 会对部署行为产生什么变化？对这个仓库是好是坏？

**答案**：改成 `true` 后，连续两次 push 到 main 时，前一次运行会被立即取消，只留最新的。对「每次运行产出完整且独立」的构建来说这通常能省资源；但对本仓库这种**部署类**任务，中途取消可能留下一次没有完成的发布记录，且旧部署可能停在半途。仓库选择 `false`（排队）是更稳妥的部署语义。

**练习 3**：为什么触发器只写 `branches: [main]` 而不是所有分支？

**答案**：一是省资源——特性分支的 PR 不需要反复发布站点；二是权限约束——让拥有写 Pages 权限的运行只发生在受保护的主分支上，任意分支 push 都能触发部署会扩大攻击面。

### 4.2 cargo 缓存策略：让第二次构建快起来

#### 4.2.1 概念说明

GitHub 的 runner 是一次性虚拟机：每次运行都是全新环境，上次装的 Rust、编译的 xtask、`cargo install` 的 mdbook 全部消失。不缓存的话，每次部署都要重新下载 crates.io 索引、重编依赖、重编 xtask、重编 mdbook 本体（`cargo install` 是从源码编译，mdbook 连同依赖要编几十个 crate，通常是最慢的一步）。

`actions/cache` 的核心是一个键值对游戏：**命中则整目录恢复，未命中则跑完任务后把列表里的目录存进去**。键的设计要保证「依赖变了就换新键，没变就复用旧缓存」。本仓库的输入是根目录的 `Cargo.lock`——它是整个 workspace（xtask 及其依赖 ctrlc）的精确版本快照，锁文件一变，缓存就该失效重建。

#### 4.2.2 核心流程

缓存键的构成是一个字符串拼接：

\[
\text{key} = \text{runner.os} + \text{"-cargo-"} + \operatorname{hash}(\texttt{**/Cargo.lock})
\]

匹配时按两级查找：

```text
计算 key = "Linux-cargo-<hash(Cargo.lock)>"
  ├─ 精确命中 key → 恢复对应缓存（完美命中）
  ├─ 未精确命中 → 按 restore-keys 前缀 "Linux-cargo-" 找最近一条
  │                恢复它，任务结束后以新 key 重存（增量重建）
  └─ 前缀也没有 → 全空起步，任务结束后新建缓存
```

恢复的路径分四类，各有用途：`~/.cargo/bin` 存着 `cargo install` 装好的 **mdbook 与 mdbook-mermaid 二进制**；`registry/index`、`registry/cache`、`git/db` 是依赖的元数据与源码包；`target` 是本 workspace 的编译中间产物（xtask 的增量编译）。

#### 4.2.3 源码精读

- [.github/workflows/pages.yml:30-41](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L30-L41) — 完整的缓存步骤：`path` 列出五个目录（`|` 折叠的多行字符串），`key` 用 `hashFiles('**/Cargo.lock')` 把根目录锁文件的哈希编进键名，`restore-keys` 给出前缀回退。注意 `~/.cargo/bin` 也在列表里——它属于 `cargo install` 的产物，不受 Cargo.lock 管理，缓存它纯粹是为了跳过重编 mdbook。
- [.github/workflows/pages.yml:43-46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L43-L46) — 安装命令 `which mdbook || cargo install mdbook`：`which` 探测成功（缓存命中恢复了二进制）就短路跳过安装；失败才从源码编译。这个「先探测后安装」的守卫与 u2-l2 讲过的 `check_mdbook`「先检查后行动」是同一种模式。
- [xtask/src/main.rs:118-126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126) — 对照：xtask 自己的 `check_mdbook` 用子进程探测 `mdbook --version`。CI 的 `which` 与它在「二进制是否在 PATH 上」这个判定上等价，只是一个在 shell 层、一个在 Rust 层。

两个值得咀嚼的细节：其一，`cargo install mdbook` **不钉版本**，缓存未命中时装的是当天的最新版——这意味着不同年份的构建产物可能来自不同版本的 mdbook，存在轻微漂移（维护者本地安装路径则钉了版本，见 u1-l3）。其二，mdbook 版本变了缓存键却不变（Cargo.lock 只锁 workspace 依赖），所以「缓存里是哪个版本的 mdbook」取决于缓存是何时建立的。

#### 4.2.4 代码实践

1. **实践目标**：亲眼确认「缓存命中 → 安装步骤秒过」的因果链。
2. **操作步骤**：
   - fork 仓库后随便触发一次部署，记下 build job 里「Install mdbook and dependencies」这步的耗时；
   - 到仓库 Actions 页面右侧的 Caches 管理页，删除 `Linux-cargo-...` 缓存；
   - 再次触发部署，比较同一步的耗时。
3. **需要观察的现象**：两次运行中该步骤的耗时差异；第二次（缓存已删）应出现 `cargo install` 的完整编译日志。
4. **预期结果**：缓存命中时 `which mdbook` 成功、步骤几乎瞬间完成；缓存被删后需要数分钟编译 mdbook 与 mdbook-mermaid。（待本地验证：具体耗时取决于 runner 状态。）

没有 fork 条件的读者可以做本地版：在终端执行 `which mdbook && echo "缓存命中等价场景：会跳过安装" || echo "缓存未命中等价场景：将执行 cargo install"`，验证 `||` 守卫的短路逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么缓存键用 `hashFiles('**/Cargo.lock')` 而不是 `hashFiles('**/Cargo.toml')`？

**答案**：`Cargo.toml` 声明的是**版本范围**（如 `ctrlc = "3"`），同一条目可以解析到不同实际版本；`Cargo.lock` 才是解析结果的**精确快照**。用 Cargo.toml 做键可能出现「键没变、实际依赖变了」（缓存陈旧）或「键变了、依赖没变」（白白重建）。锁文件变了则必然要重新解析依赖，以它为键语义最准确。

**练习 2**：`restore-keys` 的前缀回退在什么场景下真正发挥作用？

**答案**：当 Cargo.lock 更新（比如依赖升级）导致精确键失配时，前缀 `Linux-cargo-` 仍能找回上一次的缓存，恢复出大部分未变化的 registry 源码和 `target` 中间产物，之后只重建变化的部分并以新键存回。没有它，每次锁文件一变就要从零全量下载编译。

**练习 3**：`target` 目录被缓存了，但 CI 里似乎只编译一个很小的 xtask——这值得吗？

**答案**：值得但收益有限。xtask 只有一个外部依赖 ctrlc，全量编译也不慢；缓存的收益主要是跳过 rustc 对 xtask 自身和依赖的重编。真正的大头是 `~/.cargo/bin` 里的 mdbook——这是本仓库缓存列表里最划算的一项。

### 4.3 构建与上传 artifact：docs/ 目录在 CI 中的诞生

#### 4.3.1 概念说明

build job 的职责是：在一台干净虚拟机上，把仓库源码变成「可发布的静态站点目录」。它复用了本地开发用的同一条命令 `cargo xtask deploy`——**CI 与本地共用一个构建入口**是本仓库部署设计的核心优点：本地能构建成功，CI 几乎必然也能，不会出现「两套构建脚本各自漂移」的经典问题。

产出物是 `docs/` 目录：七本书的 HTML、一个手写的落地页 `index.html`、一个零字节 `.nojekyll`。随后 `upload-pages-artifact` 把整个目录打成名为 `github-pages` 的 artifact（这是该 action 的固定约定），交给平台保管，供下一个 job 取用。

#### 4.3.2 核心流程

```text
build job（ubuntu-latest 全新虚拟机）
  1. checkout            拉源码
  2. rust-toolchain      装 stable 工具链
  3. configure-pages     启用 Pages、写入站点元数据
  4. cache               恢复上一节讲的五目录缓存
  5. which||install      确保 mdbook/mdbook-mermaid 可用
  6. cargo xtask deploy  ↓ 展开为下面的 xtask 调用链
        main() --match--> cmd_deploy()
                          ├─ check_mdbook()（探测失败则以码 1 退出）
                          └─ build_to("docs")
                              ├─ 删旧建新 docs/
                              ├─ 遍历 BOOKS：mdbook build --dest-dir docs/<slug>
                              ├─ write_landing_page → docs/index.html
                              └─ 写零字节 docs/.nojekyll
  7. upload-pages-artifact  把 ./docs 打包上传为 artifact
```

#### 4.3.3 源码精读

前置三步与上传：

- [.github/workflows/pages.yml:21-28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L21-L28) — checkout 拉代码、安装 stable Rust 工具链、`configure-pages` 准备 Pages 环境（官方两阶段模式的必备前置）。
- [.github/workflows/pages.yml:48-54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L48-L54) — 构建与上传：`run: cargo xtask deploy` 之后，`upload-pages-artifact@v4` 以 `path: ./docs` 打包上传。

构建命令在 Rust 侧的落地：

- [xtask/src/main.rs:61-77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77) — `main` 的 match 分发，`Some("deploy") => cmd_deploy()`（第 69 行）。CI 传入的唯一参数就是 `deploy`。
- [xtask/src/main.rs:109-116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116) — `cmd_deploy`：先 `check_mdbook` 把关（CI 中这一步几乎必过，因为上一步刚装好），再 `build_to("docs")`，最后打印一句发布提示。注意这句提示描述的是**手动模式**（提交 docs/ 并把 Pages 源设为分支的 `/docs` 目录）——而本 workflow 走的是更新的 **artifact 模式**，并不会提交 docs/；这句文案是给本地手动执行 `deploy` 的人看的，CI 里它只是被打印出来的一行日志。
- [xtask/src/main.rs:128-168](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L128-L168) — `build_to` 全流程（u2-l3 已精读）：删旧建新、遍历 `BOOKS`、逐书 `mdbook build --dest-dir`、落地页、`.nojekyll`。其中 [.github/workflows/pages.yml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml) 的第 49 行一行命令，最终执行的就是这里面的几十行逻辑。
- [xtask/src/main.rs:165-166](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L165-L166) — 写零字节 `.nojekyll`：告诉 GitHub Pages 别用 Jekyll 再加工产物（以 `_` 开头的资源目录会被 Jekyll 默认忽略，加此文件可豁免）。它随着 `docs/` 一起被打进 artifact，是「源码行为影响线上行为」的一个隐蔽细节。
- [README.md:96](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L96) — README 命令表对 `cargo xtask deploy` 的一行注释「Build all books into docs/ (for GitHub Pages)」，与 CI 步骤名「Build documentation」互为印证。

还有一个贯穿前面讲义的事实在此兑现：`build_to` 里单本书构建失败只打告警、循环继续，**进程退出码仍是 0**（u2-l3）。因此 CI 中某本书 mdbook 构建失败时，build job 会「绿色地」产出一个缺书站点并照常部署——这是当前流水线的一个盲区，排查线上缺书时要先去 build 日志里搜 `FAILED`。

#### 4.3.4 代码实践

1. **实践目标**：在本地复现 CI 第 6 步的产物，亲眼看到 `docs/` 里有什么。
2. **操作步骤**：
   - 确认 `mdbook --version` 可用（否则先安装）；
   - 在仓库根目录执行 `cargo xtask deploy`；
   - 用 `ls docs/` 与 `ls -la docs/ | head` 查看内容。
3. **需要观察的现象**：逐书打印的 `✓ <slug>` 列表、`7/7 books built`、`✓ index.html`，以及 `docs/` 下的目录结构。
4. **预期结果**：`docs/` 含七个书的子目录、`index.html`、以及 `ls -la` 才能看见的零字节 `.nojekyll`；`cargo xtask clean` 可将其删除。同时对照 `git status`，确认 `docs/` 不被追踪——这解释了为什么 CI 必须自己构建而不能「用仓库里现成的 docs/」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CI 选择调用 `cargo xtask deploy`，而不是在 YAML 里直接写一段循环调用 `mdbook build`？

**答案**：单一事实来源。构建逻辑（书的清单、输出目录、落地页、.nojekyll）只存在于 xtask 一处，本地与 CI 执行的是同一份代码，接受编译期检查；若写进 YAML，七本书的清单就要在 shell 里再维护一份，与 `BOOKS` 注册表漂移只是时间问题。这也正是 u2-l1 介绍的 xtask 模式的核心卖点。

**练习 2**：`upload-pages-artifact` 上传的路径是 `./docs`，这个 `docs/` 是从磁盘哪里来的？仓库里本来有吗？

**答案**：是同一次运行中第 6 步 `cargo xtask deploy` 刚刚生成的；仓库里本来没有（docs/ 在 .gitignore 语义下不入库）。runner 是一次性虚拟机，artifact 是这个目录能到达 deploy job 的唯一途径。

**练习 3**：如果某个 PR 意外改坏了 python-book 的一章导致 mdbook 构建失败，这次部署会发生什么？

**答案**：`build_to` 对失败的书打印 `✗ python-book FAILED` 后继续，其余六本正常产出，落地页仍生成（落地页不检查目录/构建结果，u2-l4），退出码为 0，build job 绿色通过，deploy 照常发布一个**缺 python-book 内容（或含旧版失效页）**的站点。要发现它必须去 build 日志里找 FAILED 行——这也是后面质量守护一讲会回到的话题。

### 4.4 deploy job 衔接：为什么不重新构建

#### 4.4.1 概念说明

deploy job 只做一件事：把 build job 上传的 artifact 发布到 GitHub Pages。它是 GitHub 官方推荐的「build + deploy 两阶段」模式的下半场，这种拆分换来三个好处：

1. **职责与权限隔离**——重活（编译、装工具链）都在低权限的 build job 完成；deploy job 极薄，只持有发布动作所需的 Pages 写权限，攻击面最小。
2. **环境治理**——`environment: github-pages` 让每次发布进入部署历史，仓库管理员还可以在这个环境上挂人工审批或限定分支。
3. **产物即审计对象**——被发布的是 build 产出的确切字节的快照。同一次构建的产物可以被反复重试部署而不会「重试时重新构建出不一样的站点」。

「为什么不需要重新构建」的完整答案：**构建结果已经以 artifact 形式持久化，deploy job 消费的是这份字节快照而非源码**。证据就写在 YAML 里——deploy job 的 steps 只有一步 `deploy-pages`，连 `actions/checkout` 都没有：它根本没有源码，也无从构建。

#### 4.4.2 核心流程

```text
build job 成功结束
        │  needs: build（失败则 deploy 不启动）
        ▼
deploy job（另一台全新虚拟机，无源码）
  1. 绑定 environment "github-pages"
  2. deploy-pages 动作：
     ├─ 读取 build job 上传的 "github-pages" artifact
     ├─ 解包为静态站点
     └─ 通过 Pages API 发布（用 id-token 换取的 OIDC 凭证）
  3. 把 page_url 写入 step 输出
        ▼
environment 的 url 字段引用该输出
→ 仓库 Environments 页 / PR 页显示可点击的站点链接
→ https://microsoft.github.io/RustTraining/ 更新
```

#### 4.4.3 源码精读

- [.github/workflows/pages.yml:56-65](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L56-L65) — deploy job 全部 10 行。逐字段读：
  - `environment.name: github-pages` + `url: ${{ steps.deployment.outputs.page_url }}`（57-59 行）：把发布后的站点 URL 回填到环境记录，Actions 页面上这个 job 旁边会直接出现站点链接；
  - `needs: build`（61 行）：声明依赖，build 失败则 deploy 根本不会启动——「坏构建不发布」的第一道闸门；
  - 唯一的 step（63-65 行）：`deploy-pages@v5`，`id: deployment` 让它的输出能被上面的 `steps.deployment.outputs.page_url` 引用。
- [.github/workflows/pages.yml:51-54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L51-L54) — 再看一眼 build 侧的上传：`upload-pages-artifact@v4` 与 deploy 侧的 `deploy-pages@v5` 是一对**约定好的接口**——前者把 `./docs` 打成名叫 `github-pages` 的 artifact，后者默认消费同名 artifact。两阶段之间传递的合同就是这个固定名字，YAML 里甚至不需要显式写出 artifact 名。
- [.github/workflows/pages.yml:17-19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L17-L19) — 两个 job 平级声明、各自 `runs-on: ubuntu-latest`：它们跑在**两台不同的虚拟机**上，进一步说明文件必须靠 artifact 而非磁盘传递。

顺带回收一个前面埋下的线头：u2-l4 讲过落地页卡片的 `href="{slug}/"` 带尾斜杠，是为配合本地服务器 301 补斜杠重定向。这条链接在 GitHub Pages 上同样成立——nginx/Pages 服务器对目录 URL 的 index 解析行为与尾斜杠约定兼容，所以同一份 `docs/` 产物可以无差别地伺服本地、Pages 和 Docker 三种运行时。

#### 4.4.4 代码实践

1. **实践目标**：在 Actions 页面验证两阶段的执行顺序与交接物，并用自己的话回答「为什么 deploy 不重新构建」。
2. **操作步骤**：
   - fork 仓库（fork 后 Pages 默认可能未启用：Settings → Pages → Source 选 **GitHub Actions**）；
   - 触发一次运行（push 一个空提交 `git commit --allow-empty -m "trigger" && git push`，或用 Run workflow 按钮手动触发）；
   - 打开这次运行，先看 build job 完整跑完、状态变绿后 deploy job 才开始；
   - 点开 deploy job 的日志，与 build job 的 steps 列表对比；
   - 展开运行摘要页的 Artifacts 区域，找到上传的产物。
3. **需要观察的现象**：
   - 两个 job 在可视化图上呈一条先后箭头（`needs` 的体现）；
   - deploy job 的 steps 里**没有 Checkout、没有 Install Rust、没有 cargo xtask**，只有 Deploy to GitHub Pages 一步；
   - build job 页面（或运行摘要）能看到名为 `github-pages` 的 artifact；
   - deploy job 旁边出现的站点 URL（fork 下的 `<用户名>.github.io/RustTraining/`）。
4. **预期结果**：deploy 步骤耗时通常以秒计（只是解包发布），而 build job 首次运行要数分钟——耗时差本身就是「没重新构建」的直接证据。最后写下你的解释：构建产物已作为 artifact 持久化，deploy job 无源码也无工具链，它发布的正是 build 阶段的字节快照。（待本地验证：fork 环境的 Pages 设置与 UI 细节可能略有差异。）

#### 4.4.5 小练习与答案

**练习 1**：deploy job 里没有 checkout。如果它需要知道「这次部署对应哪个 commit 的内容」，这个信息从哪来？

**答案**：隐式来自运行上下文与 artifact 本身。每次 workflow 运行绑定一个确切的 commit（fork 后 push 触发时就是那个提交），build job 在这次运行内构建该提交并上传 artifact；deploy job 消费的 artifact 与运行一一对应，因此发布的必然是该 commit 的产物。环境记录（environment）会把部署与这次运行/commit 关联起来留档。

**练习 2**：假如把两个 job 合并成一个（build 完直接在同 job 里调 `deploy-pages`），会失去什么？

**答案**：至少失去三点——无法给「发布」这一步单独挂 environment 审批规则；权限边界变粗（同一个 job 的 GITHUB_TOKEN 要同时覆盖构建与发布所需）；重试语义变差（失败重试会把构建也重跑一遍，可能产出与上次不同的字节）。两阶段拆分用 artifact 换来了这三项治理能力。

**练习 3**：deploy job 的 `url:` 引用了 `steps.deployment.outputs.page_url`。如果删掉 `id: deployment` 这一行会怎样？

**答案**：`deploy-pages` 步骤照常执行、照常发布成功，但 `steps.deployment.outputs.page_url` 这个引用失去了指向（没有 id 就没有 `steps.deployment` 这个键），环境记录里拿不到站点 URL，Actions 页面上该部署旁边不会显示可点击的链接。发布本身不受影响，受影响的只是 UI 呈现。

## 5. 综合实践

**任务：把一条部署流水线从触发到上线完整走一遍，并做一次「故障注入」推演。**

1. fork 仓库，Settings → Pages → Source 选 GitHub Actions。
2. 触发一次部署，按顺序记录四个检查点：
   - concurrency 是否生效（若连续触发两次，第二次应排队）；
   - build job 各 step 的耗时分布（哪一步最慢？缓存命中与否的差别在哪次运行里体现？）；
   - build 与 deploy 的先后关系及 deploy 的极短耗时；
   - deploy job 关联的站点 URL 是否可访问、落地页七张卡片是否齐全。
3. 故障注入推演（不必真的做，写出你的预判再对照源码验证）：
   - 若把某本书目录从磁盘删除但仍保留在 `BOOKS` 里，流水线会失败吗？线上会怎样？（提示：回到 4.3.3 的盲区讨论）
   - 若 `cargo xtask deploy` 因 PATH 里没有 mdbook 而退出码 1，deploy job 会执行吗？（提示：`needs: build`）
4. 用一段话回答本讲的核心问题：为什么 deploy job 不需要重新构建？答案里应包含 artifact、`needs`、无 checkout 三个关键词。

预期结果：一次绿色部署 + 一张四检查点记录表 + 三条有源码依据的故障预判。全部观察类结论标注了待本地验证的以实际运行为准。

## 6. 本讲小结

- `pages.yml` 由触发器（push 到 main + 手动 dispatch）、最小权限三连（contents read / pages write / id-token write）和 `pages` 并发组（排队不取消）三道门禁开头。
- 缓存键 \( \text{OS} + \text{hash(Cargo.lock)} \) 配合 `restore-keys` 前缀回退；缓存 `~/.cargo/bin` 是为了跳过 `cargo install` 重编 mdbook，`which ||` 守卫让安装幂等。
- build job 在一次性虚拟机上执行与本地完全相同的 `cargo xtask deploy`，落地为 `docs/`（七本书 + 落地页 + `.nojekyll`），再由 `upload-pages-artifact` 打成固定名字 `github-pages` 的 artifact。
- deploy job 通过 `needs: build` 串联，无 checkout、无工具链，仅消费 artifact 完成发布，并以 `environment` 回填站点 URL——「不重新构建」的本质是发布构建阶段的字节快照。
- 已知盲区：单本书构建失败时 `build_to` 退出码仍为 0，流水线会「绿色地」部署一个缺书站点；排查要看 build 日志里的 `FAILED`。

## 7. 下一步学习建议

本讲把「源码 → docs/ → 线上」这条链走完了 Pages 分支。下一讲 **u4-l2 Docker 多阶段构建与 CI 冒烟测试** 会看同一批内容的另一条交付路径：`docker/Dockerfile` 如何用 builder/runtime 两阶段让运行时镜像不含 Rust 工具链、`docker.yml` 如何用 curl 冒烟断言弥补本讲提到的「绿色地部署坏站点」盲区。若想先巩固本讲，建议重读 [xtask/src/main.rs:128-168](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L128-L168) 中 `build_to` 的容错分支，并思考：如果要让 CI 在「7 本书没有全部构建成功」时失败，最小改动应该落在 xtask、pages.yml 还是两者之一？
