# 覆盖率分析、问题分析与质量徽章

## 1. 本讲目标

Open Logic 把「可信代码（Trustable Code）」作为第一大设计哲学（见 u1-l1）。但「可信」不能只靠口头声明——它需要**可量化、可阻止、可看见**三件事同时成立。本讲就讲解仓库里负责这三件事的三段 Python 脚本：

- **可量化**：`sim/AnalyzeCoverage.py` 把仿真覆盖率按文件解析出来。
- **可阻止**：覆盖率或（issue 数）不达标时，脚本以非零退出码让 CI 检查失败，从而阻止 PR 合并到 `main`。
- **可看见**：`sim/Badge.py` 把覆盖率、issue 状态渲染成 shields.io 风格的徽章 JSON，公开挂到文档里。

学完本讲，你应当能够：

1. 说清 `AnalyzeCoverage.py` 如何解析 ModelSim 的 `vcover` 报告、如何用 `--min_coverage` 阈值判定并退出。
2. 理解 `AnalyzeIssues.py` 如何用 GitHub label 把 issue 关联到具体实体，以及「潜在 bug / 确认 bug」如何决定徽章颜色。
3. 掌握 `Badge.py` 生成徽章 JSON 并上传到 Google Cloud Storage 的机制。
4. 把三者串成一条「仿真 → 解析 → 阈值/徽章 → CI 门禁」的质量闭环，并能解释 PR 到 `main` 时 95% 阈值如何阻止合并。

## 2. 前置知识

本讲是纯 Python 与 CI 脚本的阅读课，不需要写 VHDL，但需要以下基础（u10-l2 已建立大部分）：

- **代码覆盖率（coverage）**：仿真时统计「哪些语句/分支被真正执行过」。Open Logic 关注两类：
  - **语句覆盖率（statement coverage）**：被跑到过的可执行语句占比。
  - **分支覆盖率（branch coverage）**：`if`/`case` 的每个分支是否都被选中过，比语句覆盖更严格。
- **ModelSim / Questa 与 `vcover`**：Mentor（现 Siemens）系商业仿真器，`vcover` 是它附带的原生覆盖率工具，能把收集到的覆盖率导出成文本报告。Open Logic 的覆盖率流程依赖它，故该 CI 跑在带 NIC 锁授权的 AWS runner 上（见 u10-l2、`doc/CI-Workflows.md`）。
- **GitHub label（标签）**：issue 上贴的分类标签。Open Logic 约定每个实体名（如 `olo_base_cam`）本身就是一个 label，外加 `potential-bug`（疑似 bug）与 `confirmed-bug`（确认 bug）两个质量标签。
- **shields.io 徽章 JSON**：一种 `{"schemaVersion":1,"label":...,"message":...,"color":...}` 的小 JSON，动态徽章服务（`img.shields.io/endpoint`)可把它渲染成 SVG 小图标。
- **退出码（exit code）与 CI 门禁**：脚本 `sys.exit(1)` 表示失败；GitHub Actions 的一个 step 只要命令非零退出，整个 step 就被标红；若该 workflow 是分支保护里设置的**必需检查（required status check）**，检查失败就会禁用 GitHub 的合并按钮。

> 本讲承接 u10-l2（`sim/run.py` 如何用 `--modelsim --coverage` 跑出带覆盖率的仿真）。覆盖率数据由 `run.py` 产出，本讲的三段脚本消费它。

## 3. 本讲源码地图

| 文件 | 作用 | 行数（约） |
| --- | --- | --- |
| [sim/AnalyzeCoverage.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py) | 调 `vcover` 导出报告 → 逐文件解析语句/分支覆盖率 → 可选生成徽章 → 按 `--min_coverage` 阈值判定并可能 `sys.exit(1)` | 123 |
| [sim/AnalyzeIssues.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py) | 用 PyGithub 按实体名 label 查询 issue 数与潜在/确认 bug 数 → 生成 issue 徽章 | 48 |
| [sim/Badge.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py) | 把「标签+数值+颜色」组装成徽章 JSON 并上传到 GCS 桶 `open-logic-badges`；定义覆盖率/分支/版本/issue 四类徽章的颜色规则 | 73 |
| [.github/workflows/coverage_sim.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml) | 覆盖率仿真 workflow：PR 到 main 跑 `--min_coverage=95`，push/schedule 跑 `--badges` | 80 |
| [.github/workflows/analyze_issues.yml](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/.github/workflows/analyze_issues.yml) | 每日定时跑 `AnalyzeIssues.py` 更新 issue 徽章 | 33 |
| [doc/CI-Workflows.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md) | 用表格说明各 workflow 的触发事件与运行环境 | 103 |

三个脚本的依赖关系很简单：`AnalyzeCoverage.py` 与 `AnalyzeIssues.py` 都 `from Badge import ...`，即 `Badge.py` 是被另两者调用的底层工具库。

## 4. 核心概念与源码讲解

### 4.1 覆盖率解析与 95% 阈值（AnalyzeCoverage.py）

#### 4.1.1 概念说明

`AnalyzeCoverage.py` 要解决的问题是：**一次覆盖率仿真跑完后，产物是一个二进制的覆盖率数据库（`coverage_data`），人没法直接读，CI 也没法据此做判定。** 这个脚本就是「数据库 → 结构化数据 → 判定/徽章」的翻译层。

它做四件事，顺序很重要：

1. 调 `vcover report` 把数据库导出成可读文本 `coverage_report.txt`。
2. 逐行扫文本，按 `File:` / `Branches` / `Statements` 三个关键字把每个文件的覆盖率装进一个 `Entity` 对象。
3. （可选）为每个实体调用 `Badge.py` 生成覆盖率/分支徽章。
4. 按 `--min_coverage` 阈值逐个检查，任何一个 `olo_*` 实体不达标就 `sys.exit(1)`。

注意第 3 步和第 4 步的先后：**先生成徽章，再判阈值**。脚本注释明确写道「我们不希望掩盖差的覆盖率」——也就是说，即使覆盖率很烂、即将让 CI 失败，也要先把真实的烂覆盖率做成徽章挂出去，绝不藏。

#### 4.1.2 核心流程

```text
vcover 数据库 coverage_data
        │  (1) os.system("vcover report -byfile -nocomment coverage_data > coverage_report.txt")
        ▼
coverage_report.txt （纯文本，每个文件一段）
        │  (2) 逐行扫描
        │      命中 "File:"       → 新建 Entity，解析文件名
        │      命中 "Branches"    → 解析分支覆盖率
        │      命中 "Statements"  → 解析语句覆盖率，把 Entity 加入列表
        ▼
List[Entity]
        │  (3) 过滤掉 *_tb 与非 olo_* ；若 --badges 则生成徽章
        ▼
        │  (4) 若 statements 或 branches < --min_coverage
        │         打印 ERROR → sys.exit(1)   ← CI 在此失败、阻止合并
        ▼
正常退出（exit 0）→ CI 通过
```

判定逻辑用一句话概括：**对每个 `olo_*` 实体，语句覆盖率和分支覆盖率都必须 ≥ `--min_coverage`，任一低于即整体失败。** 默认阈值是 `0.0`（只统计不拦），CI 在 PR 到 `main` 时传 `--min_coverage=95`。

#### 4.1.3 源码精读

**(1) 命令行参数与阈值默认值**——`--badges` 是开关，`--min_coverage` 默认 `0.0`：

[sim/AnalyzeCoverage.py:L14-L26](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L14-L26) — 用 `argparse` 定义两个参数。`--min_coverage` 的 `default=0.0` 意味着「本地随手跑不会拦你」，真正的 95% 由 CI 传入。

**(2) 调 `vcover` 导出文本报告**：

[sim/AnalyzeCoverage.py:L62](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L62) — `os.system("vcover report -byfile -nocomment coverage_data > coverage_report.txt")`。`-byfile` 表示「按文件分组输出」，正是后面按文件解析的前提；`-nocomment` 去掉注释行。注意用的是 `os.system` 而非 `subprocess`，它**不会在命令失败时抛异常**——这意味着没有装 Questa 时这一步静默失败，紧接着 `open("coverage_report.txt")` 才会报 `FileNotFoundError`（这是为何本地无 Questa 时无法直接运行该脚本，见 4.1.4 实践）。

**(3) `Entity` 类：把一行行文本变成结构化数据**：

[sim/AnalyzeCoverage.py:L31-L55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L31-L55) — 三个解析方法的共同套路是「取最后一个 token、去掉 `%`、转 float」。文件名解析 `line.split("/")[-1].split(".")[0]` 先取路径最后一段（去掉目录），再去掉扩展名，于是 `../src/base/vhdl/olo_base_cam.vhd` → `olo_base_cam`。注意 `branches` 的默认值是 `100.0`——若某文件没有分支（纯组合逻辑），它不会被改写，保持满分，不会冤枉地拖低阈值判定。

**(4) 逐行扫描主循环**：

[sim/AnalyzeCoverage.py:L65-L73](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L65-L73) — 这里有一个初学者容易看错的细节：`Entity` 是在命中 `"File:"` 时创建、在命中 `"Statements"` 时才 `append` 进列表。因此 `vcover` 报告里每个文件段落必须是「先 Branches 行、后 Statements 行」的顺序，否则 Statements 行到达时 branches 还没被设置（会保持默认 100%）。这是对 `vcover -byfile` 输出格式的隐式依赖。

**(5) 「兜底实体」补丁——以及一处值得细读的代码**：

[sim/AnalyzeCoverage.py:L78-L82](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L78-L82) — 少数实体（`olo_fix_cplx_addsub`、`olo_fix_sample_hold`）的代码结构使它们天然不出现在覆盖率报告里，脚本用 100% 把它们补进去。注释说意图是「仅当它们缺失时才补，以免覆盖真实结果」。

> **细读会发现**：判定条件写的是 `if not enforc_entities in [entity.name for entity in entities]:`，注意是 `enforc_entities`（那个**列表**）而不是循环变量 `enforce_entity`（字符串）。`<列表> in <字符串列表>` 永远为 `False`（一个 list 不可能等于任一字符串），于是 `not False` 恒为 `True`——也就是说**这两个实体会被无条件地以 100% 追加**，并不真的检查「是否缺失」。因为这两个实体本就不出现在真实报告里，实际运行没有造成重复或错误，但这个守卫其实是失效的。这是一处典型的「注释意图与代码实际不符」，读开源工程时值得留意。

**(6) 先徽章后阈值**：

[sim/AnalyzeCoverage.py:L86-L101](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L86-L101) — 生成输出与徽章。循环里先 `continue` 掉 `*_tb`（测试台不计入产品代码覆盖率）与非 `olo_*` 的文件（例如第三方 `en_cl_fix`），再打印并可选地生成徽章。

[sim/AnalyzeCoverage.py:L103-L118](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeCoverage.py#L103-L118) — 真正的「门禁」：再次遍历（同样的过滤），任何 `statements < min_coverage` 或 `branches < min_coverage` 都打印 `ERROR` 并 `sys.exit(1)`。这一句 `sys.exit(1)` 就是「PR 到 main 时 95% 阈值如何阻止合并」的源头（见 4.4）。

#### 4.1.4 代码实践

完整运行该脚本需要 Questa/ModelSim 与一次带覆盖率的仿真（`python3 ./run.py --modelsim --coverage`，见 u10-l2），多数读者本地没有商业授权。因此本实践采用**「源码阅读 + 手工合成报告 + 复刻核心逻辑」**的方式，让你在任何装了 Python 的机器上都能亲历「解析 → 找最低 → 阈值拦截」全过程。

**实践目标**：亲手喂一份合成的 `coverage_report.txt` 给一段复刻脚本，观察它打印出最低覆盖率的文件、并在低于阈值时以非零码退出。

**操作步骤**：

1. 准备目录与一份合成报告 `coverage_report.txt`（注意每段都是「`File:` → `Branches` → `Statements`」顺序，符合 4.1.3 (4) 的隐式依赖）：

   ```text
   File: ../src/base/vhdl/olo_base_pl_stage.vhd
       Branches     97.0%
       Statements   98.0%

   File: ../src/base/vhdl/olo_base_cam.vhd
       Branches     91.0%
       Statements   92.0%

   File: ../src/base/vhdl/olo_base_fifo_sync.vhd
       Branches     99.0%
       Statements   99.0%
   ```

2. 把下面这段「示例代码」存为 `coverage_replay.py`（它抽离了 `AnalyzeCoverage.py` 第 31–55、65–73、103–118 行的核心逻辑，去掉了 `vcover` 调用）：

   ```python
   # 示例代码：复刻 AnalyzeCoverage.py 的解析 + 阈值判定（不依赖 vcover）
   import sys

   class Entity:
       def __init__(self):
           self.name = None
           self.statements = None
           self.branches = 100.0  # 无分支则保持满分，不冤枉拖低判定
       def parse_name_line(self, line):
           self.name = line.split("/")[-1].split(".")[0]
       def parse_statement_line(self, line):
           self.statements = float(line.split()[-1].replace("%", ""))
       def parse_branch_line(self, line):
           self.branches = float(line.split()[-1].replace("%", ""))

   entities = []
   for line in open("coverage_report.txt"):
       if "File:" in line:
           e = Entity(); e.parse_name_line(line)
       if "Branches" in line:
           e.parse_branch_line(line)
       if "Statements" in line:
           e.parse_statement_line(line)
           entities.append(e)

   min_cov = 95.0  # 对应 CI 的 --min_coverage=95
   worst = min((e for e in entities if e.name.startswith("olo_")),
               key=lambda e: e.statements)
   print(f"最低语句覆盖率: {worst.name} = {worst.statements}%")

   for e in entities:
       if e.name.endswith("_tb") or not e.name.startswith("olo_"):
           continue
       if e.statements < min_cov or e.branches < min_cov:
           print(f"ERROR - {e.name} 语句={e.statements}% 分支={e.branches}%，最低要求 {min_cov}%")
           sys.exit(1)
   print("全部达标，CI 通过")
   ```

3. 运行：`python3 coverage_replay.py ; echo "退出码=$?"`

**需要观察的现象**：

- 终端先打印「最低语句覆盖率: olo_base_cam = 92.0%」。
- 紧接着对 `olo_base_cam` 打印 ERROR，脚本退出。
- 末尾 `echo` 显示 `退出码=1`。

**预期结果**：`olo_base_cam` 因语句 92% / 分支 91% 均低于 95% 而触发 `sys.exit(1)`，退出码非零。把这个非零退出码对应到 CI：GitHub Actions 的 step 因此标红 → 若 `coverage_sim` 是 `main` 的必需检查 → 合并按钮被禁用（详见 4.4）。

**待本地验证**：以上为合成数据下的预期；真实数值以你在 Questa 中跑出的 `coverage_report.txt` 为准。若你确有 Questa，可直接 `cd sim && python3 ./run.py --modelsim --coverage` 后运行原版 `python3 ./AnalyzeCoverage.py --min_coverage=95` 验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么脚本要先（第 3 步）生成徽章、后（第 4 步）判阈值，而不是反过来？

> **答案**：注释明言「We do not want to hide bad coverage」。若先判阈值、失败就立刻退出，则覆盖率差的实体的徽章永远不会被更新，外部看到的徽章会停留在上一次（可能更高）的旧值，等于掩盖了质量回退。先上传真实徽章再退出，保证对外展示的永远是最新真相。

**练习 2**：把合成报告里 `olo_base_cam` 的 `Statements` 改成 `96.0%`、`Branches` 仍是 `91.0%`，复刻脚本的退出码会变成什么？为什么？

> **答案**：退出码仍是 `1`。因为判定条件是 `statements < min_cov **or** branches < min_cov`，分支 91% 仍低于 95%，任一不达标即失败。这体现了「语句和分支两条线都必须达标」。

---

### 4.2 Issue 关联分析（AnalyzeIssues.py）

#### 4.2.1 概念说明

仅有覆盖率还不够——一个实体可能 100% 覆盖，却仍被用户报了 bug。`AnalyzeIssues.py` 负责**把 GitHub issue 与具体实体关联起来，并据此生成 issue 徽章**。

它的核心约定是：**实体名即 label**。仓库约定每个 `olo_*` 实体（如 `olo_base_cam`）在 GitHub 上对应一个同名 label；维护者收到相关 issue 时就贴上对应实体 label。于是「查某个实体有多少 issue」就退化成「查带这个 label 的 issue 有几个」——无需自建数据库，GitHub label 体系本身就是单一真相源。

它还区分两类质量 label：`potential-bug`（疑似，尚未确认）与 `confirmed-bug`（已确认是真 bug），二者决定徽章颜色（见 4.3）。

#### 4.2.2 核心流程

```text
(1) sys.argv[1] 取 GitHub token → g = Github(token)
(2) g.get_repo("open-logic/open-logic")
(3) glob("../src/**/*.vhd") → 得到全部实体名（文件名去扩展名）
(4) for 每个实体名 entity:
        总 issue 数   = get_issues(labels=[entity]).totalCount
        疑似 bug 数   = get_issues(labels=[entity, "potential-bug"]).totalCount
        确认 bug 数   = get_issues(labels=[entity, "confirmed-bug"]).totalCount
        create_issues_badge(entity, 总数, 疑似>0, 确认>0)
```

注意第 (3) 步用 `glob` 扫源码目录反推实体清单，而不是去 GitHub 查「所有 label」——这保证只对**当前仓库里真实存在**的实体生成徽章，已删除实体的旧 label 不会被处理。

#### 4.2.3 源码精读

**(1) 两类 bug label 用枚举收口**：

[sim/AnalyzeIssues.py:L16-L18](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py#L16-L18) — `Labels(Enum)` 把 `POTENTIAL_BUG="potential-bug"`、`CONFIRMED_BUG="confirmed-bug"` 收成枚举，避免脚本里到处硬编码字符串、拼错难查。

**(2) 鉴权与定位仓库**：

[sim/AnalyzeIssues.py:L23-L33](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py#L23-L33) — token 从命令行第一个参数取（CI 由 secret `ISSUES_TOKEN` 注入，见 `analyze_issues.yml`），仓库写死为 `open-logic/open-logic`。

**(3) 扫源码反推实体清单**：

[sim/AnalyzeIssues.py:L36-L37](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py#L36-L37) — `glob('../src/**/*.vhd', recursive=True)` 递归搜集全部 VHDL 源文件（脚本在 `sim/` 下运行，故用 `../src`）。注意这会一并扫到 `olo_test_*_vc`（验证组件）等 `test/` 外但位于 `src/` 之外的文件吗？不会——它只扫 `src/`。但 `src/fix/python/` 下的 `.py` 不被扫（只匹配 `.vhd`），所以 Python 工具不会被当成实体。

**(4) 三连查询 + 生成徽章**：

[sim/AnalyzeIssues.py:L40-L45](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py#L40-L45) — 对每个实体做三次 `get_issues(labels=[...]).totalCount`，把「疑似/确认是否 > 0」作为布尔传给 `create_issues_badge`。这里只关心「有没有」，不关心「有几个」疑似/确认 bug——颜色只分三档（绿/橙/红），数量体现在徽章的 `message` 里（总 issue 数）。

#### 4.2.4 代码实践

**实践目标**：在不调用 GitHub API（不需要 token）的前提下，用「示例代码」复刻「实体名 ↔ label ↔ 颜色」的关联逻辑，理解三类 issue 状态如何映射到绿/橙/红。

**操作步骤**：

1. 阅读 [sim/AnalyzeIssues.py:L40-L45](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/AnalyzeIssues.py#L40-L45) 与 [sim/Badge.py:L65-L72](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L65-L72)。
2. 运行下面这段「示例代码」，它把 API 查询换成了一个本地字典 `fake_issues`，键为实体名，值为 `(总数, 疑似数, 确认数)`：

   ```python
   # 示例代码：复刻 issue → 颜色的关联逻辑（不调用 GitHub）
   fake_issues = {
       "olo_base_cam":        (3, 0, 0),   # 有 issue，但都不是 bug
       "olo_base_fifo_sync":  (1, 1, 0),   # 有疑似 bug
       "olo_base_pl_stage":   (2, 1, 1),   # 有确认 bug
       "olo_base_ram_sp":     (0, 0, 0),   # 无 issue
   }
   def color_for(total, potential, confirmed):
       if confirmed > 0:   return "red"
       if potential > 0:   return "orange"
       return "green"
   for entity, (total, pot, conf) in fake_issues.items():
       print(f"{entity:25} issues={total} 颜色={color_for(total, pot, conf)}")
   ```

**需要观察的现象**：四个实体分别得到 `green`、`orange`、`red`、`green`。

**预期结果**：颜色优先级为 `confirmed-bug > potential-bug > 无`，与 `Badge.py` 的 `create_issues_badge` 完全一致；`olo_base_cam` 虽有 3 个 issue 但无 bug 标签故仍为绿，说明「issue 数量多」不等于「质量差」。

**待本地验证**：若你有带 `issues:read` 权限的 token，可改写示例代码用 `github.Github(token).get_repo("open-logic/open-logic").get_issues(labels=[...]).totalCount` 对真实仓库跑一遍，对比线上徽章。

#### 4.2.5 小练习与答案

**练习 1**：为什么脚本用 `glob('../src/**/*.vhd')` 反推实体清单，而不是用 `repo.get_labels()` 列出所有 label？

> **答案**：用源码反推能保证只处理「当前仓库真实存在的实体」。已删除实体的旧 label 仍可能残留在 GitHub，若用 `get_labels()` 就会为不存在的实体生成无意义徽章；以源码为准则自动忽略它们。

**练习 2**：某实体有 5 个 issue、其中 0 个疑似、0 个确认，它的徽章是什么颜色？这说明了什么？

> **答案**：绿色。说明 issue 多寡本身不改颜色——颜色只由 bug 标签决定。功能咨询类 issue 不会让实体「变红」，只有疑似/确认 bug 才会，这把「活跃度」与「质量风险」两个维度解耦了。

---

### 4.3 质量徽章生成（Badge.py）

#### 4.3.1 概念说明

`Badge.py` 是前两个脚本的底层工具库，本身不读覆盖率也不查 issue，只做一件事：**把「标签文字 + 数值 + 颜色」组装成 shields.io 端点格式的 JSON，上传到一个公开对象存储桶**。文档里用一行 `https://img.shields.io/endpoint?url=...` 引用这个 JSON，浏览器就会把它渲染成实时徽章。

它把存储后端选为 **Google Cloud Storage（GCS）** 桶 `open-logic-badges`，而不是直接 commit 到仓库——这样每次更新徽章不会污染 git 历史，且可被 CI 高频写入而不产生 PR。凭证走加密文件：CI 用 `GCS_PASSPHRASE` secret 解密出服务账号 key 到本地，路径由环境变量 `GCS_FILE` 指给脚本（见 `analyze_issues.yml` 里的 `install_gcs_key.sh`、`doc/GcsCredentialsEncryption.txt`）。

#### 4.3.2 核心流程

```text
create_badge(text, value, color, folder, filename)
   │
   │  组装 batch = {"schemaVersion":1, "label":text, "message":value, "color":color}
   │  用 GCS_FILE 凭证建客户端 → 取桶 open-logic-badges
   │  blob = bucket.blob(f"{folder}/{filename}.json")
   │  blob.upload_from_string(json, predefined_acl='publicRead')
   ▼
公开 URL:  https://storage.googleapis.com/open-logic-badges/{folder}/{filename}.json
   │  被 img.shields.io/endpoint 渲染成 SVG
   ▼
文档里看到的小图标
```

四类徽章分三个文件夹存放：`coverage/`（语句覆盖）、`branches/`（分支覆盖）、`issues/`（issue 状态）。颜色规则是本模块的重点。

#### 4.3.3 源码精读

**(1) 两个枚举：颜色与文件夹**：

[sim/Badge.py:L8-L18](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L8-L18) — `BadgeColor`（green/red/orange/lightgrey/blue）与 `BadgeFolder`（coverage/branches/issues）用枚举收口，避免拼错字符串导致徽章传到错误文件夹。

**(2) 上传一个徽章 JSON**：

[sim/Badge.py:L21-L36](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L21-L36) — 关键三点：凭证从 `os.getenv("GCS_FILE")` 取；对象路径为 `{folder}/{filename}.json`；`predefined_acl='publicRead'` 让任何人都能匿名读这个 JSON（否则 shields.io 拉不到、徽章显示不出来）。

**(3) 覆盖率徽章的颜色规则——以及一处值得细读的代码**：

[sim/Badge.py:L38-L44](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L38-L44) — 先默认红，`if value > 90.0: 绿`，`elif value > 95.0: 橙`。

> **细读会发现**：由于 `elif value > 95.0` 只有在 `value > 90.0` 不成立（即 `value <= 90.0`）时才会被求值，而 `value > 95.0` 又要求 `value > 90.0`，二者不可能同时满足，所以**这条 `elif` 永远不会为真，`ORANGE` 分支是不可达死代码**。实际效果只有两档：`value <= 90.0` → 红，`value > 90.0` → 绿。若作者的意图是「≤90 红 / 90–95 橙 / >95 绿」，应写成先判 `> 95`、再 `elif > 90`。读这段时不要被字面三个分支误导，要看 `if/elif` 的短路求值顺序。`create_branch_badge`（[L46-L52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L46-L52)）是同样写法、同样的问题。

**(4) 版本徽章：git 哈希 + 日期**：

[sim/Badge.py:L54-L62](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L54-L62) — `create_coverage_version_badge` 用 `git log -1 --pretty=format:%h` 取短哈希、用 `datetime.date.today()` 取日期，各做一个蓝色徽章，让人一眼看出「这批覆盖率徽章是哪个版本、哪天跑的」。注意此函数名带 `version` 但其实产两个徽章（`version` + `date`）。

**(5) issue 徽章的颜色规则**：

[sim/Badge.py:L65-L72](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L65-L72) — 优先级清晰：有 `confirmed-bug` → 红；否则有 `potential-bug` → 橙；都没有 → 绿。`message` 是 issue 总数（字符串）。这与 4.2.4 示例代码的逻辑一致。

#### 4.3.4 代码实践

**实践目标**：离线复刻「数值/状态 → 颜色」的判定，并亲手组装一个 shields.io 端点 JSON，用公开的 shields.io 查看渲染效果（无需 GCS 凭证）。

**操作步骤**：

1. 阅读 [sim/Badge.py:L21-L36](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L21-L36) 与 [L38-L44](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/Badge.py#L38-L44)。
2. 运行下面这段「示例代码」，它复刻了 `create_badge` 的 JSON 组装（去掉 GCS 上传），并把结果写成本地文件：

   ```python
   # 示例代码：复刻 Badge.create_badge 的 JSON 组装（不上传）
   import json
   def make_badge(label, message, color):
       return {"schemaVersion": 1, "label": label, "message": message, "color": color}
   def cov_color(v):       # 复刻 Badge.py L38-L44 的实际两档行为
       return "green" if v > 90.0 else "red"
   b = make_badge("statement coverage", "92.0%", cov_color(92.0))
   open("olo_base_cam.json", "w").write(json.dumps(b))
   print(json.dumps(b, indent=2))
   ```

3. 把生成的 `olo_base_cam.json` 内容贴到 [shields.io 的 endpoint 调试页](https://img.shields.io/endpoint)，或直接在浏览器访问（需自建公开托管）；本地可用 `json.dumps` 输出确认结构。

**需要观察的现象**：JSON 含 `schemaVersion/label/message/color` 四个键；`cov_color(92.0)` 返回 `green`（因为按实际行为 `> 90` 即绿）。

**预期结果**：你组装出的 JSON 与 `Badge.create_badge` 产出的结构逐字一致；shields.io 能把它渲染成「statement coverage | 92.0%」的绿色徽章。

**待本地验证**：徽章的实际渲染取决于 shields.io 服务与公开托管 URL，本地仅能验证 JSON 结构。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `create_badge` 上传时必须带 `predefined_acl='publicRead'`？省略会怎样？

> **答案**：省略后对象默认是私有，shields.io 匿名拉取会得到 403，徽章显示为「无法访问」。徽章是给公众看的，必须显式设为公共读。

**练习 2**：按 `Badge.py` 当前的 `if/elif` 写法，一个语句覆盖率为 `97.0%` 的实体会得到什么颜色？若把判定改成「先 `>95` 绿、`elif >90` 橙」，又会变成什么？

> **答案**：当前写法下为**绿色**（`>90` 命中第一条，橙色 elif 不可达）。改成「先 `>95` 绿、`elif >90` 橙」后，97% 仍命中第一条故仍为绿——区别在于 90–95 区间：当前写法把它们判为绿，改写后会判为橙。也就是说当前代码对所有 `>90` 一视同仁为绿。

---

### 4.4 CI 质量闭环

#### 4.4.1 概念说明

前三个模块是「零件」，本模块讲它们如何被 CI 串成一条**自动化的质量闭环**。关键在于「**谁、何时、在哪台机器上**」调用哪个脚本，以及失败如何阻止合并。Open Logic 的设计取舍很清晰：

- **免费检查（GitHub runner）跑得勤**：HDL-Check、Doc-Check 在每个贡献 PR 上跑，零成本。
- **昂贵检查（AWS runner）跑得精**：覆盖率仿真需要 Questa 的 NIC 锁授权，跑在 AWS 自托管 runner 上，只在 PR 到 `main`、push 到 `main`、每月定时触发。
- **issue 徽章每日一更**：`analyze_issues.yml` 每天 03:00 UTC 跑，保证 bug 状态不过期。

`sys.exit(1)` 是贯穿全局的「闸门」：脚本失败 → step 标红 → 必需检查失败 → 合并被阻止。

#### 4.4.2 核心流程

两条 workflow，两个触发策略：

```text
┌─ coverage_sim.yml ──────────────────────────────────────────────┐
│ 触发: PR→main / push→main / 每月10号03:00 / 手动                │
│ 运行: AWS self-hosted runner（需要 Questa NIC 锁授权）          │
│                                                                  │
│  step1: run.py --modelsim --coverage   ← 跑出 coverage_data     │
│  step2 (仅 PR/手动):  AnalyzeCoverage.py --min_coverage=95       │
│                        └ 任一文件 <95% → sys.exit(1) → 阻止合并 │
│  step3 (仅 push/定时): AnalyzeCoverage.py --badges               │
│                        └ 更新全部覆盖率徽章（即使覆盖率低也更新）│
└──────────────────────────────────────────────────────────────────┘

┌─ analyze_issues.yml ────────────────────────────────────────────┐
│ 触发: 每天03:00 UTC / 手动          运行: GitHub runner（免费） │
│  step: 解密 GCS 凭证 → AnalyzeIssues.py $ISSUES_TOKEN            │
│        └ 为每个实体查 issue 数 + potential/confirmed bug         │
│           → 更新 issue 徽章                                      │
└──────────────────────────────────────────────────────────────────┘
```

一个关键设计：**同一个 `AnalyzeCoverage.py` 在 PR 时只判阈值、不更新徽章；在 push 到 main 时只更新徽章、不判阈值。** 因为 PR 分支还没合并，更新徽章会误导；而 main 上即使覆盖率回退也要如实挂出徽章（与 4.1.1「不掩盖」原则一致）。

#### 4.4.3 源码精读

**(1) 覆盖率 workflow 的触发条件**：

[.github/workflows/coverage_sim.yml:L10-L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml#L10-L19) — 四种触发：`workflow_dispatch`（手动）、`pull_request` 到 `main`、`push` 到 `main`、`schedule` 每月 10 号 03:00（`cron: '0 3 10 * *'`）。

**(2) 判阈值 step 的条件守卫**：

[.github/workflows/coverage_sim.yml:L63-L68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml#L63-L68) — `if: github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'` 确保阈值判定**只在 PR 与手动时跑**，并传 `--min_coverage=95`。这正是「PR 到 main 时 95% 阈值如何阻止合并」的落点。

**(3) 更新徽章 step 的条件守卫**：

[.github/workflows/coverage_sim.yml:L71-L76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml#L71-L76) — `if: github.event_name == 'push' || github.event_name == 'schedule'` 确保徽章**只在合并到 main 或定时后更新**，用 `--badges`。

**(4) issue workflow 每日触发**：

[.github/workflows/analyze_issues.yml:L3-L6](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/analyze_issues.yml#L3-L6) — `cron: '0 3 * * *'` 每日 03:00 UTC，仅 `workflow_dispatch` 与 schedule 两种触发——它**不跑在 PR 上**，因为 issue 状态与具体 PR 无关。

**(5) 文档对触发矩阵的权威说明**：

[doc/CI-Workflows.md:L11-L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md#L11-L19) — 用一张表把每个 workflow 对应到「PR 到 develop / PR 到 main / push 到 main / 月 / 日 / GitHub runner / AWS runner」。注意覆盖率仿真只在 **PR 到 main** 与 **push 到 main** 触发，不在「PR 到 develop（贡献 PR）」上跑——贡献阶段的免费门禁由 HDL-Check 用开源仿真器（GHDL/NVC）兜底，不含覆盖率（见 [doc/CI-Workflows.md:L38-L48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md#L38-L48)）。

#### 4.4.4 代码实践

**实践目标**：把三条脚本的两条 workflow 整理成一张「触发事件 × 运行环境 × 失败后果」的对照表，并理清「PR 到 main、某实体覆盖率 92%」时的完整失败传导链。

**操作步骤**：

1. 阅读 [.github/workflows/coverage_sim.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml) 与 [.github/workflows/analyze_issues.yml](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/.github/workflows/analyze_issues.yml) 全文。
2. 自行填写下表（答案见后）：

   | Workflow | 触发事件 | 运行环境 | 失败会阻止 PR 合并吗？ |
   | --- | --- | --- | --- |
   | Coverage Simulation（判阈值） | ? | ? | ? |
   | Coverage Simulation（更新徽章） | ? | ? | ? |
   | analyze-issues | ? | ? | ? |

3. 写出「PR 到 main、`olo_base_cam` 语句覆盖 92%」时，从仿真到合并被阻止的完整链路（应包含至少 5 个环节）。

**需要观察的现象 / 预期答案**：

| Workflow | 触发事件 | 运行环境 | 失败会阻止 PR 合并吗？ |
| --- | --- | --- | --- |
| Coverage Simulation（判阈值） | PR→main / 手动 | AWS runner | **是**（`--min_coverage=95` 失败即 `sys.exit(1)`） |
| Coverage Simulation（更新徽章） | push→main / 每月 | AWS runner | 否（这是 main 上的徽章更新，无阈值判定） |
| analyze-issues | 每天 / 手动 | GitHub runner | 否（与 PR 无关，只更新徽章） |

完整失败传导链（共 6 环）：

```text
(1) run.py --modelsim --coverage 跑出 coverage_data
(2) AnalyzeCoverage.py --min_coverage=95 解析，发现 olo_base_cam 语句 92% < 95%
(3) 脚本打印 ERROR 并 sys.exit(1)
(4) GitHub Actions 把该 step 标记为 failed
(5) Coverage Simulation 这个 workflow 的状态变成 failed
(6) 若它是 main 分支保护的必需检查 → GitHub 禁用 Merge 按钮，阻止合并
```

**待本地验证**：第 (6) 步是否真的阻止合并，取决于仓库管理员是否在 branch protection rules 里把 Coverage Simulation 设为 required check；代码层面只能保证到第 (5) 步。

#### 4.4.5 小练习与答案

**练习 1**：为什么阈值判定 step 用 `if: ... pull_request`，而徽章更新 step 用 `if: ... push`？若把徽章更新也放到 PR 上会怎样？

> **答案**：PR 分支尚未合并，其覆盖率不代表 main 的真实状态；若在 PR 上更新徽章，会把「未合并的临时分支」的覆盖率公开挂出去，可能误报（例如 PR 临时为某个实体只写了部分测试）。徽章只反映 main，故只在 push 到 main 后更新。

**练习 2**：贡献者从 fork 提 PR 到 develop 时，覆盖率仿真会跑吗？为什么这么设计？

> **答案**：不会。`doc/CI-Workflows.md` 注明 fork 的 PR 需维护者批准才跑昂贵 workflow，以避免恶意代码在 Open Logic 的 AWS CI 里执行、并省 AWS 成本。贡献阶段的门禁由跑在免费 GitHub runner 上的 HDL-Check（用开源 GHDL/NVC 仿真、不做覆盖率检查）兜底。

---

## 5. 综合实践

把本讲四个模块串起来，模拟一次「质量回退被 CI 拦下」的完整事件：

**背景**：假设你给 `olo_base_cam` 改了一版代码，重构后测试台少覆盖了一个分支，语句覆盖率掉到 92%、分支覆盖率掉到 91%。请按以下步骤推演并记录每一步的产物与后果。

1. **复刻解析与徽章**：用 4.1.4 的合成报告（`olo_base_cam` 设为语句 92%、分支 91%），运行复刻脚本，确认它打印 ERROR 且退出码为 1。
2. **推演徽章颜色**：用 4.3.4 的 `cov_color(92.0)` 判定该实体徽章颜色（应为绿，因为按当前 `Badge.py` 实际行为 `>90` 即绿——这正好暴露了 4.3.3 指出的「橙分支不可达」问题：92% 其实不算高，却被显示为绿）。
3. **写失败传导链**：按 4.4.4 第 3 步，写出从 `run.py` 到「合并被阻止」的 6 环链路。
4. **反思徽章语义**：结合第 2 步，说明「徽章颜色（绿）」与「CI 判定（失败）」为何会出现「徽章显示绿、CI 却阻止合并」的看似矛盾——即徽章颜色分档（>90 绿）比 CI 阈值（95%）更宽松，二者服务不同目的：徽章面向公众做粗粒度提示，CI 阈值面向维护者做严格门禁。

**交付物**：一份一页纸记录，包含 4.1.4 脚本的运行输出截图/文本、徽章颜色判定、6 环失败链、以及第 4 点的反思。

> **待本地验证**：综合实践全部基于合成数据与源码阅读，可在任何装有 Python 的机器上完成；真实 CI 行为以 GitHub Actions 实际运行结果与仓库 branch protection 设置为准。

## 6. 本讲小结

- `AnalyzeCoverage.py` 是「覆盖率数据库 → 结构化数据 → 判定/徽章」的翻译层：调 `vcover` 导报告、按 `File:/Branches/Statements` 解析、过滤 `*_tb` 与非 `olo_*`，最后用 `--min_coverage` 判定。
- 95% 门禁的源头是 `sys.exit(1)`：任一 `olo_*` 实体的**语句或分支**覆盖率低于阈值即整体失败，且**先生成徽章后判阈值**，绝不掩盖差覆盖率。
- `AnalyzeIssues.py` 用「实体名即 label」的约定把 GitHub issue 关联到实体，`potential-bug`/`confirmed-bug` 两个 label 决定风险等级；实体清单用 `glob` 扫 `src/` 反推，保证只处理真实存在的实体。
- `Badge.py` 把数据组装成 shields.io 端点 JSON 上传到 GCS 公开桶 `open-logic-badges`；issue 徽章颜色优先级为「确认 bug 红 > 疑似 bug 橙 > 无绿」。
- 两处值得细读的代码：`Badge.py` 覆盖率徽章的 `elif value > 95.0` 是不可达死代码（实际只有 ≤90 红 / >90 绿两档）；`AnalyzeCoverage.py` 的兜底守卫 `if not enforc_entities in [...]` 误用了列表而非循环变量，导致恒为真。
- CI 闭环按成本分级：覆盖率仿真（昂贵、AWS runner）只在 PR/push 到 main 与月更触发，issue 徽章（便宜、GitHub runner）每日更新；贡献阶段由免费的 HDL-Check 用开源仿真器兜底。

## 7. 下一步学习建议

- 下一讲 **u10-l4（代码检查与综合测试自动化）** 讲 VSG 风格检查与 `tools/inference_test` 综合资源评估，与本讲同属「CI 质量闭环」的另一条线（静态检查与综合），可对照阅读。
- 若想看「免费门禁」如何兜底覆盖率缺失，阅读 [.github/workflows/hdl_check.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml) 与 `doc/CI-Workflows.md` 的 HDL-Check 段。
- 若对覆盖率数据的来源感兴趣，回看 u10-l2 的 `sim/run.py` 中 `--coverage` 与 `--modelsim` 的配合，以及它为何必须跑在带 NIC 锁授权的 runner 上。
- 进阶读者可尝试修补本讲指出的两处代码问题（`Badge.py` 的颜色判定顺序、`AnalyzeCoverage.py` 的兜底守卫变量名），并思考：改动是否会改变现有徽章的显示效果？是否会引入重复实体？以此训练「改一行代码的全局影响」判断力。
