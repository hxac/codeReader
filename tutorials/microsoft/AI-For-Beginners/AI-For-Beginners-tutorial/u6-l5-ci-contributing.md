# CI 安全与贡献流程

## 1. 本讲目标

本讲是「配套工具与维护机制」单元的最后一篇。前几讲我们看了测验应用（u6-l1）、测验生成流水线（u6-l2）、Docsify 文档站点（u6-l3）和多语言翻译机制（u6-l4），它们解决的都是「课程内容怎么被生产、展示和分发」。本讲换个角度，回答一个对开源教育仓库同样关键的问题：**这么大的一个公开仓库，怎么保证它的代码安全、怎么让人安全地往里贡献内容？**

学完本讲你应该能够：

- 说清 GitHub Actions 里的两个安全工作流——**CodeQL** 与 **Scorecard**——分别在扫描什么、什么时候触发、为什么这么配置。
- 看懂 `AGENTS.md` 中关于环境、构建、测试、代码风格的开发约定，理解为什么教育仓库「没有传统测试套件」。
- 掌握面向微软开源教育仓库的贡献流程：CLA、行为准则、PR 检查清单、翻译贡献的特殊规则。
- 自己动手写出一份「为某课 Notebook 修复一个错误」的 PR 检查清单。

## 2. 前置知识

在进入源码前，先解释几个本讲反复出现的术语。它们都来自「供应链安全 / DevSecOps」领域，对纯做学习的同学可能陌生，但概念很简单。

- **CI（Continuous Integration，持续集成）**：每次往仓库推送代码或发起 PR 时，自动在云端跑一套检查（编译、测试、安全扫描），让问题尽早暴露。GitHub 上的 CI 由 `.github/workflows/` 下的 YAML 文件定义，跑在 GitHub 提供的服务器（runner）上。
- **GitHub Actions**：GitHub 内置的 CI/CD 系统。一个 workflow 由若干 **job** 组成，每个 job 又由若干 **step** 组成；step 常用 `uses:` 调用别人写好的 **action**（可复用步骤）。
- **SARIF（Static Analysis Results Interchange Format）**：一种 JSON 格式标准，专门用来描述「静态分析发现了哪些安全问题」。GitHub 的 Security 面板能直接读 SARIF，把漏洞标在代码行上。
- **SHA pinning（哈希钉扎）**：引用一个 action 时，不写 `@v4` 这种会变动的标签，而是写它在 git 里的完整提交哈希 `@b4ffde65f4...`。这样即使作者恶意篡改 `v4` 指向的提交，你的 CI 也不会被牵连——这是供应链防投毒的核心手段。
- **最小权限原则（least privilege）**：workflow 向 GitHub 申请的权限越少越好。本讲的 `permissions: read-all` 就是默认只读，按需再开口子。
- **CLA（Contributor License Agreement，贡献者许可协议）**：贡献者声明「我授权你们使用我的提交」。微软所有开源项目都要求签一次。
- **CodeQL / Scorecard**：本讲两个主角，下面专章展开。

> 一句话定位：本讲讨论的不是「AI 怎么学」，而是「这个 AI 课程仓库本身怎么被安全地维护」。

## 3. 本讲源码地图

本讲涉及 4 个关键文件，分两组：

| 文件 | 作用 | 归属最小模块 |
|------|------|--------------|
| [.github/workflows/codeql.yml](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml) | CodeQL 代码漏洞扫描工作流 | 安全扫描工作流 |
| [.github/workflows/scorecard.yml](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml) | OpenSSF Scorecard 供应链安全评分工作流 | 安全扫描工作流 |
| [AGENTS.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md) | 给 AI 助手与人类开发者的项目开发约定总览 | 开发约定 |
| [etc/CONTRIBUTING.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md) | 贡献流程、CLA、行为准则、待贡献清单 | 贡献流程 |

补充说明：`.github/workflows/` 目录下**只有这两个** YAML 文件——仓库没有为测验应用单独写构建 CI，也没有自动跑 Notebook 的 CI（这一点会在「开发约定」里解释原因）。

---

## 4. 核心概念与源码讲解

### 4.1 安全扫描工作流

#### 4.1.1 概念说明

公开仓库每天都在「被看」也在「被依赖」。维护者要回答两个安全问题：

1. **我们自己的代码里有没有已知漏洞模式？**（比如不安全的反序列化、SQL 注入、硬编码密钥）
2. **我们依赖的 GitHub Actions、我们的分支保护配置、我们的维护活跃度，够不够安全？**（即「这个仓库本身的卫生状况如何」）

本仓库用两套互补的工具分别回答这两个问题：

- **CodeQL**：GitHub 自研的**代码语义分析引擎**。它先把代码编译/解析成一个可查询的数据库，再用一套用 SQL 风格语言（QL）写的查询去搜漏洞模式。所以它擅长发现「代码本身」的缺陷，是**白盒**分析。
- **OpenSSF Scorecard**：由开源安全基金会（OpenSSF）维护的**供应链健康评分工具**。它不看代码细节，而是检查十几项「卫生指标」——分支有没有保护、依赖有没有钉扎、仓库是否还在活跃维护、CI 配置权限是否最小化等，最后给一个 0~10 的综合分。所以它衡量的是「仓库的运维姿态」，是**姿态（posture）**评估。

一句话区分：**CodeQL 找代码里的洞，Scorecard 找仓库配置的坑。**

#### 4.1.2 核心流程

两个 workflow 都遵循 GitHub Actions 的标准结构：`name` → `on`（触发器）→ `jobs`（任务）→ `permissions`（权限）→ `steps`（步骤）。

**CodeQL 工作流的执行流程**：

```text
触发（push到main / PR到main / 每周一17:37定时）
   │
   ▼
按语言矩阵展开（actions / javascript-typescript / python）
   │  （三种语言并行，互不阻塞：fail-fast: false）
   ▼
checkout 拉代码
   │
   ▼
Initialize CodeQL（按语言+构建模式初始化数据库）
   │
   ▼
Perform CodeQL Analysis（跑查询，产出 SARIF，上传到 Security 面板）
```

**Scorecard 工作流的执行流程**：

```text
触发（分支保护规则变更 / 每周五15:44定时 / push到main）
   │
   ▼
checkout（钉扎版本 + 关闭凭证持久化）
   │
   ▼
ossf/scorecard-action 跑十几项检查 → results.sarif
   │
   ▼
publish_results: true（公开仓库才发布，可生成徽章）
   │
   ▼
上传 SARIF 到 code-scanning 面板 + 作为 artifact 留存 5 天
```

两者最终都把结果汇成 **SARIF** 文件并塞进 GitHub 的 **Code scanning** 面板，这样维护者在同一个地方就能看到「代码漏洞」和「供应链风险」两类告警。

#### 4.1.3 源码精读

**（a）CodeQL：触发器与语言矩阵**

[.github/workflows/codeql.yml:12-21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L12-L21) 定义了工作流名称与三种触发方式：

```yaml
name: "CodeQL Advanced"
on:
  push:
    branches: [ "main" ]
  pull_request:
    branches: [ "main" ]
  schedule:
    - cron: '37 17 * * 1'
```

- `push` / `pull_request` 都限定在 `main` 分支：意味着每次合并到主线、以及每个针对主线的 PR 都会被扫描——把好「进门关」。
- `schedule` 用 cron 表达式 `'37 17 * * 1'`，标准 5 字段「分 时 日 月 周」(UTC)，即**每周一 17:37 UTC** 定时全量扫一次，用来捕获新发布的 CodeQL 查询规则能查出的旧漏洞（即便近期没人提交代码）。

[.github/workflows/codeql.yml:42-51](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L42-L51) 是**语言矩阵**：

```yaml
strategy:
  fail-fast: false
  matrix:
    include:
    - language: actions
      build-mode: none
    - language: javascript-typescript
      build-mode: none
    - language: python
      build-mode: none
```

三种语言 `actions`（指 workflow YAML 本身）、`javascript-typescript`（测验应用）、`python`（课程脚本与 Notebook 抽出的代码）正好覆盖了本仓库所有「会被当作代码」的部分；`build-mode: none` 表示这三种都是解释型语言，不需要先编译就能分析。`fail-fast: false` 很关键：一种语言扫描失败不会立即取消其他语言，三种结果都拿到，避免一个语言的偶发问题掩盖其他语言的真问题。

**（b）CodeQL：权限与扫描步骤**

[.github/workflows/codeql.yml:31-40](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L31-L40) 体现了最小权限——只开「写安全事件」（用于上传 SARIF）和「读包/读动作/读内容」：

```yaml
permissions:
  security-events: write   # 上传扫描结果必需
  packages: read           # 拉取内部 CodeQL 包
  actions: read
  contents: read
```

扫描本身分两步：[.github/workflows/codeql.yml:71-75](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L71-L75) 初始化数据库，[.github/workflows/codeql.yml:99-102](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L99-L102) 执行分析。注意 [.github/workflows/codeql.yml:89-97](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L89-L97) 有一段「manual 构建模式」的占位代码，它用 `if: matrix.build-mode == 'manual'` 守卫——本仓库三种语言都是 `none`，所以这段永远不会执行，只留作日后加 C/C++ 等编译型语言时的模板。

**（c）Scorecard：触发器与「默认只读」**

[.github/workflows/scorecard.yml:5-18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml#L5-L18)：

```yaml
name: Scorecard supply-chain security
on:
  branch_protection_rule:          # 分支保护规则变化时（Branch-Protection 检查需要）
  schedule:
    - cron: '44 15 * * 5'          # 每周五 15:44 UTC
  push:
    branches: [ "main" ]
permissions: read-all              # 默认全部只读
```

`branch_protection_rule` 这个触发器很特别：它只在「仓库的分支保护设置被改」时触发，因为 Scorecard 有一项检查就是「main 分支有没有保护规则」，设置变了要立刻重评。顶层 `permissions: read-all` 是供应链安全的**默认姿态**——能给只读就绝不多给。

**（d）Scorecard：SHA 钉扎 + 关闭凭证持久化**

这是本工作流最值得学的两行。[.github/workflows/scorecard.yml:34-37](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml#L34-L37)：

```yaml
- name: "Checkout code"
  uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1
  with:
    persist-credentials: false
```

- `@b4ffde65f4...` 是 `actions/checkout` 这个 action 在 v4.1.1 版本的**完整提交哈希**，注释 `# v4.1.1` 只是给人看的可读标记。Scorecard 自己就有一项「Pinned-Dependencies」检查会去验证你的 action 有没有钉扎——所以这个文件自己就是「合规示范」。
- `persist-credentials: false` 阻止 checkout 步骤把 GitHub token 留在 `.git/config` 里。否则后续步骤若被注入恶意命令，可能从 git 配置里偷走 token。

分析、上传两步同样钉扎哈希：[.github/workflows/scorecard.yml:39-57](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml#L39-L57) 用 `ossf/scorecard-action@0864cf19...` 跑出 `results.sarif` 并设 `publish_results: true`（公开仓库可发布并生成徽章），[.github/workflows/scorecard.yml:70-73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml#L70-L73) 用 `github/codeql-action/upload-sarif@1b1aada4...` 把结果送进 code-scanning 面板。中间 [.github/workflows/scorecard.yml:61-66](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/scorecard.yml#L61-L66) 还把 SARIF 作为 artifact 留存 5 天，方便回溯。

#### 4.1.4 代码实践

**实践目标**：通过阅读两个 workflow 文件，理解 CI 安全工作流的触发与配置，掌握 cron 与 SHA 钉扎的读法。

**操作步骤**：

1. 打开本地 `.github/workflows/codeql.yml`，找到 `schedule` 那行，确认 cron 是 `'37 17 * * 1'`。
2. 打开 `.github/workflows/scorecard.yml`，找到三处 `uses:` 开头的步骤，抄下每处的哈希与可读注释。
3. 在 GitHub 上访问本仓库的 **Security → Code scanning alerts** 页面（若你有权限），观察告警条目。
4. 在你的本地副本里，把 codeql.yml 的 cron 改成 `'0 9 * * *`（每天 09:00 UTC），**不要提交**，只为理解 cron 字段含义。

**需要观察的现象**：

- CodeQL 与 Scorecard 的 cron 时间不同（周一 vs 周五），是有意错峰，避免同一时刻抢 runner。
- 三个 SHA 钉扎的 action 后面都跟着形如 `# v4.1.1` 的人类可读版本注释。
- Scorecard 顶层 `permissions: read-all`，而 job 内部又单独提权了 `security-events: write` 和 `id-token: write`——后者用于 OIDC 发布结果。

**预期结果**：你能用一句话说清「CodeQL 三种语言为什么都设 `build-mode: none`」（因为 Python/JS/Actions 都是解释型，无需编译即可分析），并能解释为什么 `persist-credentials: false` 能减少 token 泄露风险。

> 注：实际触发扫描需要推送到 GitHub，本地无法直接运行 CI。以上为源码阅读型实践，cron 修改请勿提交，否则会改变仓库 CI 行为。

#### 4.1.5 小练习与答案

**练习 1**：CodeQL 的语言矩阵里为什么没有 `c-cpp`？

**参考答案**：本仓库的可执行代码只有 Python（课程脚本/Notebook）、JavaScript/TypeScript（测验应用）和 Actions（workflow YAML），没有任何 C/C++ 代码，所以矩阵里不需要 `c-cpp`。这也意味着 [.github/workflows/codeql.yml:89-97](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.github/workflows/codeql.yml#L89-L97) 那段 `manual` 构建占位代码永远不会触发。

**练习 2**：Scorecard 工作流里的 `branch_protection_rule` 触发器，为什么 CodeQL 工作流里没有？

**参考答案**：Scorecard 有一项「Branch-Protection」检查专门评估 main 分支的保护规则（是否要求 PR、是否要求评审等），所以当保护规则发生变化时需要立刻重评；而 CodeQL 只看代码内容，分支保护配置与代码漏洞无关，故不需要这个触发器。

**练习 3**：把 `uses: actions/checkout@v4` 换成 `uses: actions/checkout@b4ffde65f4...` 有什么安全收益？

**参考答案**：`@v4` 是一个**可变标签**，仓库维护者可以把 `v4` 重新指向任意新提交；一旦该 action 被入侵或作者恶意，你的 CI 就会拉到被篡改的版本。钉扎到完整 SHA 后，拉取的内容被固定为那个提交，攻击者无法在不改动你这个 YAML 文件的前提下替换它，从而阻断供应链投毒。

---

### 4.2 开发约定

#### 4.2.1 概念说明

`AGENTS.md` 是一种约定俗成的文件名：它专门写给「AI 编程助手和参与开发的人类」看，用结构化的方式说清「这个项目是什么、怎么搭环境、怎么构建、怎么测、代码风格如何、怎么贡献」。它和 `README.md`（写给最终用户/学习者）的区别在于：`AGENTS.md` 面向**参与建设仓库的人**，侧重工程约定。

对教育仓库而言，`AGENTS.md` 里最反直觉、也最重要的一条约定是：**「这是一个教学内容仓库，没有传统意义上的测试套件」**。理解这一点，才能理解为什么前几讲我们看到的所有 Notebook 都强调「能从头跑到尾」而不是「有断言」。

#### 4.2.2 核心流程

`AGENTS.md` 的内容按「从上手到深水区」组织，构成一条开发主线：

```text
项目概述（是什么、技术栈）
   ▼
环境搭建（conda ai4beg / 测验应用 npm）
   ▼
开发工作流（本地 Jupyter / VS Code / 云端 / GPU）
   ▼
测试说明（无传统测试套件，靠 Notebook 端到端跑通 + 测验 lint）
   ▼
代码风格（Python 教学优先 / Vue ESLint）
   ▼
构建与部署（Notebook 无构建 / 测验应用 npm build / Docsify）
   ▼
贡献指南 + 环境依赖 + 常见排错
```

#### 4.2.3 源码精读

**（a）环境搭建的「唯一正名」**

[AGENTS.md:24-37](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L24-L37) 把环境名钉死为 `ai4beg`，并指定从 `environment.yml` 创建：

```bash
conda env create --name ai4beg --file environment.yml
conda activate ai4beg
jupyter notebook
```

这条约定承接了 u1-l3：所有课程 Notebook 都默认跑在 `ai4beg` 内核里。把它写进 `AGENTS.md`，意味着任何 AI 助手或新贡献者打开仓库，第一件事就是按这个名字建环境，避免「每个人起一个不同环境名」的混乱。

**（b）测验应用的两套命令**

[AGENTS.md:46-56](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L46-L56) 把测验应用（u6-l1 详讲过的 Vue 应用）的命令列清楚：

```bash
cd etc/quiz-app
npm install
npm run serve   # 开发服务器
npm run build   # 生产构建
npm run lint    # 检查并自动修复
```

注意 `npm run lint` 在这里不仅是开发命令，更是**提交前的强制检查**（见 4.3.3）。

**（c）「没有传统测试套件」——本仓库最关键的约定**

[AGENTS.md:94-104](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L94-L104) 明说：

> "This is an educational repository focused on learning content rather than software testing. There is no traditional test suite."

它给出的替代验证方式有四种：①逐 cell 跑 Notebook 验证示例可用；②测验应用靠开发服务器手动测；③检查 `translations/` 翻译内容；④测验应用跑 `npm run lint`。这解释了本课程的一个全局事实——**「测试」在这里等于「Notebook 能端到端跑完且结果合理」**，而不是 `pytest` 断言。这也是为什么 `AGENTS.md` 在贡献指南里反复强调「Ensure notebooks run end-to-end」（确保 Notebook 能从头跑到尾）。

**（d）代码风格：教学优先**

[AGENTS.md:120-127](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L120-L127) 对 Python 的要求是「Standard Python conventions for educational code… prioritizing learning over optimization」「No strict linting requirements for lesson content」——课程内容不强求严格 lint，可读性和教学性优先；而测验应用这一「真正的软件」则要求 ESLint 与 Vue 2.x 规范（[AGENTS.md:129-134](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L129-L134)）。两套标准并存，对应「教学内容」与「工程产物」两种性质的代码。

#### 4.2.4 代码实践

**实践目标**：核对 `AGENTS.md` 中给出的命令与真实仓库结构是否一致，建立「文档可信但要核对」的习惯。

**操作步骤**：

1. 打开 [AGENTS.md:106-116](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L106-L116)，找到它给出的「直接运行 Python 脚本」示例。
2. 用 `ls` 确认其中两个路径真实存在：`lessons/4-ComputerVision/07-ConvNets/pytorchcv.py` 与 `lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb`（本讲已确认二者均存在）。
3. 对照 [AGENTS.md:217-229](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L217-L229) 列出的依赖版本，打开本地 `requirements.txt` 与 `environment.yml`，核对 `tensorflow`、`numpy`、`keras` 等版本是否与文档一致。

**需要观察的现象**：文档示例路径是否都能在磁盘上找到；依赖版本号是否与实际文件吻合。

**预期结果**：两个示例路径都存在；如果某些版本号在 `requirements.txt` 与 `AGENTS.md` 间有出入，记下来——这正是「文档可能滞后于源码」的典型情况，也是 update 类贡献的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AGENTS.md` 说教育仓库「没有传统测试套件」，却仍然在贡献流程里强调「测试」？

**参考答案**：这里的「测试」指的不是 `pytest`/单元测试，而是**「让 Notebook 从第一个 cell 顺序执行到最后一个 cell 都不报错、输出合理」**。对教学仓库而言，Notebook 跑通就是「测试通过」。`AGENTS.md` 在 Pull Request Process 里写「Ensure notebooks run end-to-end」就是这个意思。

**练习 2**：`AGENTS.md` 对 Python 课程代码和测验应用 Vue 代码的风格要求有何不同？为什么？

**参考答案**：课程 Python 代码不强求严格 lint、教学可读性优先（因为读者要读懂每一行学概念）；测验应用 Vue 代码要求 ESLint + Vue 2.x 规范、组件化架构（因为它是真正部署的软件工程产物，需要可维护、风格统一）。两套标准反映了两类代码的不同性质。

**练习 3**：一个贡献者按 `AGENTS.md` 建好环境却仍报 `ModuleNotFoundError`，最可能漏了哪一步？

**参考答案**：最可能漏了「在 Jupyter 里选对 `ai4beg` 内核」。建好环境不等于 Notebook 会用它——必须把 `ai4beg` 注册为 Jupyter 内核并在 Notebook 右上角选中它（u1-l3 详述）。`AGENTS.md` 的 Debugging 段也给了 `python -m ipykernel install --user --name ai4beg` 的修复命令。

---

### 4.3 贡献流程

#### 4.3.1 概念说明

贡献流程回答「我想往这个仓库提交一点东西，要走哪些门」。对微软这种大型开源项目，门主要分三道：

- **法律门（CLA）**：你必须签一份贡献者许可协议，声明你授权微软使用你的提交。一次签署、全微软仓库通用。
- **社区门（Code of Conduct）**：你必须遵守开源行为准则，保持友善、专业。
- **质量门（PR 检查清单）**：你的 PR 要满足内容质量要求，且会被 CI（CodeQL 等）自动检查。

对**教育仓库**，质量门还有两个特色：一是没有单元测试要过（如上节所述），二是「内容」类贡献（写一节新课、改一处说明）远多于「代码」类贡献，所以清单里强调的是「教学友好、Notebook 跑通、更新翻译」而非测试覆盖率。

`etc/CONTRIBUTING.md` 还专门维护一份「**Looking for Contributions**」（求贡献）清单，列出维护者当下最想让人帮忙的具体主题——对新人来说，这是最友好的上手入口。

#### 4.3.2 核心流程

一个典型贡献的生命周期：

```text
1. 看 CONTRIBUTING.md 的「求贡献」清单，挑一个主题
   ▼
2. 签 CLA（首次，全程一次）
   ▼
3. Fork → 新分支 → 改内容 / 改 Notebook
   ▼
4. 自检：Notebook 端到端跑通；改了测验应用就 npm run lint；改了英文就更新翻译
   ▼
5. 提 PR（标题清晰、描述充分）
   ▼
6. 自动检查：CLA-bot → CodeQL → （Scorecard 评仓库姿态）
   ▼
7. 维护者评审 → 合并 → co-op-translator 自动同步翻译（u6-l4）
```

注意第 6 步：本讲的两个安全工作流正是在这里发挥作用的——CodeQL 会扫你 PR 引入的代码，Scorecard 会评估合并后仓库的整体姿态是否下降。

#### 4.3.3 源码精读

**（a）CLA 与 CLA-bot**

[etc/CONTRIBUTING.md:3-10](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md#L3-L10) 说明法律门：

> "Most contributions require you to agree to a Contributor License Agreement (CLA)… When you submit a pull request, a CLA-bot will automatically determine whether you need to provide a CLA and decorate the PR appropriately."

关键词是 **CLA-bot 自动判定**：你不用自己去想要不要签，机器人会看你的提交账号，没签就在 PR 上贴标签、留评论、给你链接，你跟着点完即可；且「只需在所有微软仓库里签一次」。

**（b）行为准则**

[etc/CONTRIBUTING.md:12-14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md#L12-L14) 指向《Microsoft Open Source Code of Conduct》及 FAQ，违规可邮件举报。这是社区门。

**（c）求贡献清单——新人最佳入口**

[etc/CONTRIBUTING.md:16-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md#L16-L24) 是一份带 `- [ ]` 复选框的待办：

- 深度强化学习（Deep Reinforcement Learning）相关章节
- 目标检测（Object Detection）章节与 Notebook 改进
- PyTorch Lightning 内容
- 命名实体识别（NER）章节与示例
- 自训练词嵌入（own embeddings）示例

这些主题对应 u5-l2、u3-l6、u4-l7、u4-l3 等讲义——也就是说，贡献者可以拿着这份清单，挑一个你学过的课去补内容，方向明确、不会跑偏。

**（d）PR 质量门（来自 AGENTS.md）**

`AGENTS.md` 把 PR 检查清单写得比 `CONTRIBUTING.md` 更细。[AGENTS.md:189-196](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L189-L196) 的四条 Pull Request Process：

1. 标题清晰、描述改动；
2. **CLA 必须签署**（自动检查）；
3. 内容准则：保持教学友好与对初学者友好、所有 Notebook 示例都要测过、**确保 Notebook 端到端跑通**、若改了英文内容就要同步更新翻译；
4. 改了测验应用，**提交前必须 `npm run lint`**。

**（e）翻译贡献的特殊规则**

[AGENTS.md:198-203](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L198-L203) 区分两种翻译贡献：英文源内容改了之后，多语言翻译由 GitHub Actions 里的 `co-op-translator` **自动同步**（u6-l4 详述，由 `localizeflow[bot]` 发同步 PR）；**手工翻译**则放进 `translations/<语言码>/`，测验翻译放 `etc/quiz-app/src/assets/translations/`。也就是说：普通人一般不用手动翻译正文，只在你确实要提供高质量人工译文时才动手。

#### 4.3.4 代码实践

**实践目标**：把本节的三道门（法律、社区、质量）落成一份可勾选的 PR 检查清单，针对一个具体场景。

**操作步骤**：

1. 选定一个场景：假设你要为 `lessons/5-NLP/19-NER/NER-TF.ipynb`（u4-l7 讲过的 NER Notebook）修复一个 import 错误。
2. 打开 [etc/CONTRIBUTING.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md) 与 [AGENTS.md:185-211](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L185-L211)。
3. 基于这两份文档，写出本场景的 PR 检查清单（参考第 5 节「综合实践」的模板，但本次只列「与 NER Notebook 修复直接相关」的条目）。

**需要观察的现象**：你的清单是否覆盖了 CLA、Notebook 跑通、是否动了英文内容（要不要触发翻译）、是否碰了测验应用（要不要 lint）这四个判断点。

**预期结果**：得到一份 6~8 条、可直接逐条打勾的检查清单。这个练习的真正价值在于训练「从两份散落的文档里抽取出与具体改动相关的质量门」的能力。

> 注：是否真的发起 PR 取决于你是否愿意签 CLA 并公开提交；本练习只要求产出清单文档，不强制推送。

#### 4.3.5 小练习与答案

**练习 1**：你修了某个 Notebook 里的一处英文注释错字，要不要手动去 `translations/` 改翻译？

**参考答案**：**不需要**手动改。按 [AGENTS.md:200](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L200) 的约定，英文源内容一旦合并，`co-op-translator` 会通过 GitHub Action 自动重译（且只重译源哈希变化的文件，见 u6-l4）。你手动改反而会和机器人冲突。

**练习 2**：CLA-bot 判定你需要签 CLA，但你已经在另一个微软仓库签过了，怎么办？

**参考答案**：按 [etc/CONTRIBUTING.md:10](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md#L10)，CLA「只需在所有仓库里签一次」。如果 bot 仍提示，通常是用不同邮箱提交导致的，按 bot 评论里的指引，用已签 CLA 的同一邮箱重新提交（或在 PR 里说明），bot 会重新判定通过。

**练习 3**：你的 PR 只改了测验应用的一行 Vue 代码，CI 会跑哪些自动检查？为什么 Notebook 不需要过测试？

**参考答案**：会跑 CodeQL（扫 `javascript-typescript` 语言）和 Scorecard（评仓库姿态）；按 `AGENTS.md` 贡献约定，你提交前还应自己跑 `npm run lint`。Notebook 不需要过测试，是因为本仓库「没有传统测试套件」，对 Notebook 的要求是「端到端跑通」而非自动化断言——这条改动没碰 Notebook，所以也不涉及「跑通」检查。

---

## 5. 综合实践

把本讲三个最小模块串起来，设计一个贯穿任务：**写一份「为某课 Notebook 修复一个错误」的完整 PR 检查清单**，并指出每个条目分别对应三道门里的哪一道、会被哪个 CI 工作流检查。

**实践目标**：综合运用「安全扫描工作流 + 开发约定 + 贡献流程」三部分知识，产出一份能直接指导真实贡献的文档。

**场景设定**：你在 `lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb`（u5-l2 讲过的强化学习 Notebook）里发现一个 API 调用已过时导致报错，打算修好并提 PR。

**操作步骤**：

1. **读规约**：通读 [etc/CONTRIBUTING.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/CONTRIBUTING.md) 全文与 [AGENTS.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md) 的 Setup / Testing / Contributing 三节。
2. **写清单**：按下表把每一项归类到「法律门 / 社区门 / 质量门」并标注对应 CI：

   | 序号 | 检查项 | 归属门 | 自动检查方 |
   |------|--------|--------|-----------|
   | 1 | 已签 CLA（或按 bot 指引补签） | 法律门 | CLA-bot |
   | 2 | 行为符合 Code of Conduct | 社区门 | 人工评审 |
   | 3 | 修复后 CartPole Notebook 在 `ai4beg` 内核端到端跑通 | 质量门 | 人工 / Notebook 执行（无自动 CI） |
   | 4 | 改动保持教学友好、对初学者可读 | 质量门 | 人工评审 |
   | 5 | 若改动了英文源文本，确认依赖 co-op-translator 自动重译 | 质量门 | localizeflow[bot]（u6-l4） |
   | 6 | 本 PR 未触碰 `etc/quiz-app/`，故无需 `npm run lint` | 质量门 | —（不适用） |
   | 7 | PR 引入的代码通过 CodeQL 扫描 | 质量门 | CodeQL workflow（`python` 语言） |
   | 8 | 合并后仓库 Scorecard 姿态不下降 | 质量门 | Scorecard workflow |

3. **补风险点**：在清单末尾写 1~2 条「本仓库特有」的提醒，例如「本仓库无传统测试套件，故第 3 项只能靠手动执行 Notebook 验证，不能用 CI 绿勾替代」。
4. **存档**：把清单保存为本地笔记（不要写进仓库）。

**预期结果**：一份 8 条左右、每条都标注了归属门与检查方的清单，且能说清「为什么第 3 项没有对应 CI」（因为教育仓库无测试套件）。这份清单同时证明了你对三个最小模块的理解：法律/社区/质量三道门（贡献流程）、ai4beg 内核与无测试套件（开发约定）、CodeQL/Scorecard 两个工作流（安全扫描）。

## 6. 本讲小结

- 本仓库 `.github/workflows/` 下有两个安全工作流：**CodeQL** 扫代码漏洞（白盒，覆盖 actions / javascript-typescript / python 三种语言、均为 `build-mode: none`），**Scorecard** 评供应链姿态（分支保护、依赖钉扎、维护活跃度等）。
- 两个工作流最终都产出 **SARIF** 并汇入 GitHub **Code scanning** 面板；触发器有 `push`/`pull_request`/`schedule`（cron 错峰：周一 17:37、周五 15:44 UTC），Scorecard 还多了 `branch_protection_rule`。
- Scorecard 工作流是供应链安全的**示范配置**：顶层 `permissions: read-all`（默认只读）、action 用**完整 SHA 钉扎**（防标签被篡改）、`persist-credentials: false`（防 token 留在 git 配置）。
- `AGENTS.md` 是写给开发者的工程约定总览：环境唯一命名为 `ai4beg`、测验应用用 npm、Python 教学代码不强求 lint 而 Vue 工程代码要求 ESLint；最关键的约定是**「没有传统测试套件」——「测试」=「Notebook 端到端跑通」**。
- 贡献流程有三道门：法律门（CLA，由 CLA-bot 自动判定、一次签署全仓通用）、社区门（Microsoft Code of Conduct）、质量门（PR 清单：标题、跑通 Notebook、改英文要同步翻译、改测验应用要 `npm run lint`）。
- `etc/CONTRIBUTING.md` 的「Looking for Contributions」清单列出深度强化学习、目标检测、NER、自定义嵌入等待贡献主题，是新贡献者最佳上手入口；正文翻译由 `co-op-translator` 自动同步，无需手动。

## 7. 下一步学习建议

本讲是「配套工具与维护机制」单元（u6）的收尾，也是整套学习手册的最后一篇。建议：

- **想动手贡献**：从 `etc/CONTRIBUTING.md` 的求贡献清单里挑一个你学过的主题（比如学过 u4-l7 就去做 NER 示例），按本讲第 5 节的清单走一遍完整 PR 流程，体验从 CLA 到 CodeQL 的全部门。
- **想深入 CI/安全**：把本讲的两个 workflow 当模板，对照 [OpenSSF Scorecard 文档](https://github.com/ossf/scorecard) 逐条理解它检查的十几项指标；进一步可学习 Dependabot、Renovate 等依赖更新机器人（本仓库未启用，是潜在的改进方向）。
- **回看整个单元**：u6-l1（测验应用）→ u6-l2（测验生成）→ u6-l3（Docsify 站点）→ u6-l4（翻译机制）→ 本讲（CI 与贡献），合起来回答了「这个课程仓库如何被生产、分发、翻译、安全维护」的全链路；建议把五篇串读一遍，画出从「写一行课程内容」到「全球多语言线上可学」的完整数据流。
- **通读收束**：至此你已完成从 u1（项目概览）到 u6（维护机制）的全部 34 篇讲义，覆盖了 AI-For-Beginners 的「学课程 / 跑示例 / 看测验 / 要中文 / 懂维护」五个维度。下一步的最佳实践是挑一个最小改动（如修正一处文档错字）真正提一个 PR，把书本知识变成一次开源贡献。
