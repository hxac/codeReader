# CI 流程与文档/依赖自动化

## 1. 本讲目标

学完本讲，读者应该能够：

- 说出 psi_fix 的 CI 是如何「一键跑完整个库的回归」的：谁启动、跑什么、怎么判通过/失败。
- 解释 `ciFlow.py` 为什么**不信任仿真器退出码**，而是去文本文件 `Transcript.transcript` 里找两个魔法字符串来分级判定（功能错 `-1` / 环境错 `-2`）。
- 掌握 `scripts/dependencies.py` 如何把 `README.md` 的依赖段当作「唯一真相源」来检出姊妹仓库。
- 理解 `hdl2md.py` 如何把一个 VHDL entity 自动翻译成 Markdown 接口文档，以及为什么文档是「生成产物」。
- 指出 `unittest/psi_fix_pkg_test.py` 覆盖的是库的**哪一层**（原语层），它和 VHDL 协同仿真覆盖的**组件层**如何互补。

本讲是学习手册的倒数第二篇，承接 [u1-l3 仿真与回归测试框架](u1-l3-simulation-regression.md)：那里讲了「人怎么手动跑回归」，这里讲「CI 怎么自动跑回归并做判决」，以及围绕回归的三件配套自动化（依赖、文档、单测）。

## 2. 前置知识

本讲默认你已经熟悉以下概念（前序讲义已建立）：

- **位真双模型**：每个可综合 VHDL 组件必须配套一个逐位一致的 Python 黄金模型（见 u1-l1、u2-l3）。
- **`###ERROR###` 约定**：自检测试台发现 VHDL 输出与 Python 模型不一致时，打印这个魔法字符串，它是功能失败的唯一判据（见 u1-l3、u3-l2）。
- **PsiSim 五步回归**：`init → source config.tcl → compile_files -all -clean → run_tb -all → run_check_errors`（见 u1-l3）。
- **并排摆放的目录结构**：psi_fix 与 en_cl_fix / psi_common / psi_tb / PsiSim 四个姊妹仓库按固定相对路径摆放（见 u1-l1）。

三个对初学者可能陌生的术语，先解释清楚：

| 术语 | 通俗解释 |
|:-----|:---------|
| **CI（持续集成）** | 每次提交代码后，机器自动跑一遍全套测试，告诉你「这次的改动有没有把库弄坏」。 |
| **batch 模式** | 仿真器（Modelsim 的 `vsim`）不开图形界面、不等人交互，直接执行一批命令后退出，适合被脚本调用。 |
| **退出码（exit code）** | 程序结束时返回给操作系统的一个整数。CI 一般约定 `0` 表示成功，**非 0** 表示失败；不同非零值可区分不同失败类型。 |

> 关于退出码的一个细节：Python 的 `exit(-1)` 在大多数 shell 里会被截断成 `255`（exit code 是 8 位无符号）。但这对 CI 判定无害——CI 只关心「是否为 0」，`-1` 和 `-2` 在 shell 层面都表现为非零失败，区分它们的是 `ciFlow.py` 内部的代码意图（功能错 vs 环境错），用于日志可读性而非 shell 层判别。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|:-----|:-----|:---------|
| `scripts/ciFlow.py` | CI 总编排：跑 Modelsim 回归 + 跑 Python 单测，解析日志做判决 | **主角** |
| `sim/ci.do` | Modelsim 的 batch do-file，把 `run.tcl` 包成一条命令 | CI 仿真入口 |
| `sim/run.tcl` | PsiSim 五步回归脚本（人/CI 共用） | 被调用方（u1-l3 详讲） |
| `scripts/dependencies.py` | 依赖检出：解析 README 依赖段、调用外部包克隆姊妹仓库 | 自动化配套 |
| `scripts/hdl2md.py` / `hdl2md_all.py` | 从 VHDL entity 生成 Markdown 接口文档 | 文档自动化 |
| `unittest/psi_fix_pkg_test.py` | Python 单元测试，覆盖 `psi_fix_pkg.py` 原语层 | CI 第二阶段 |

一句话定位：`ciFlow.py` 是「裁判」，`ci.do`/`run.tcl` 是「VHDL 考场」，`psi_fix_pkg_test.py` 是「Python 考场」，`dependencies.py` 和 `hdl2md.py` 是「场务」（准备环境、准备文档）。

## 4. 核心概念与源码讲解

### 4.1 CI 回归流程：从 ci.do 到 ciFlow.py

#### 4.1.1 概念说明

psi_fix 是一个有几十个 DSP 组件、每个都要位真验证的库。靠人手动跑回归既慢又容易漏。CI（持续集成）把这件事自动化：**提交代码 → 机器自动编译所有 VHDL → 自动跑所有测试台 → 自动判决通过/失败**。

psi_fix 的 CI 设计有一个鲜明特点：它**不信任仿真器的退出码**。原因是不同仿真器（Modelsim、GHDL）在不同平台上、遇到不同错误时，退出码行为并不统一；而且 VHDL 编译错误、TCL 脚本错误、license 失效、仿真器崩溃都会让进程异常退出，光看退出码无法区分「测试没过」和「环境坏了」。

于是 psi_fix 采用**文本契约**判决：让所有自检测试台在失败时统一打印一个魔法字符串 `###ERROR###`，让 PsiSim 框架在全部跑完后打印 `SIMULATIONS COMPLETED SUCCESSFULLY`，CI 脚本再去日志文件里搜这两个字符串。这就是 u1-l3 提到的「`###ERROR###` 字符串约定」在 CI 层的最终落点。

#### 4.1.2 核心流程

整个 CI 分两大阶段，串行执行，**前一阶段不过则后一阶段不跑**：

```text
ciFlow.py 启动
   │
   ├─ 阶段 A：VHDL 回归（Modelsim）
   │    1. chdir 到 sim/
   │    2. os.system("vsim -batch -do ci.do -logfile Transcript.transcript")
   │         └─ ci.do:  onerror {exit}; source run.tcl; quit
   │              └─ run.tcl 五步: init→config.tcl→compile→run_tb→run_check_errors
   │    3. 读 Transcript.transcript 文本
   │    4. 判决：
   │         含 "###ERROR###"                     → exit(-1)  【功能错：某测试台位真不匹配】
   │         不含 "SIMULATIONS COMPLETED SUCCESSFULLY" → exit(-2) 【环境错：没跑完】
   │
   └─ 阶段 B：Python 单元测试（仅当阶段 A 通过）
        5. chdir 到 unittest/
        6. from psi_fix_pkg_test import *  +  unittest.main(exit=False)
        7. 判决：errors 或 failures 非空 → exit(-1)
```

关键设计点有三：

1. **两阶段串行门控**：阶段 A 的两个 `exit()` 会直接终止进程（见源码 L22-L26），所以阶段 B 只有在 VHDL 回归干净通过时才会执行。这避免了「环境都崩了还去跑 Python」的无意义开销。
2. **两级错误码**：`-1` 表示「测试真的失败了」（功能回归不过），`-2` 表示「测试根本没跑完」（编译错、license 错、仿真器崩）。这种区分让维护者一眼看出是该查 RTL/模型，还是该查 CI 环境。
3. **忽略 `os.system` 返回值**：脚本完全不读 `vsim` 的退出码，判决 100% 依赖对 `Transcript.transcript` 的文本扫描，从而绕开「仿真器退出码不可靠」的坑。

#### 4.1.3 源码精读

先看 CI 的总入口 `ciFlow.py`。它先用相对路径定位自己所在目录，再切到 `sim/` 启动 Modelsim batch 模式：

[scripts/ciFlow.py:L12-L16](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py#L12-L16) —— 切到 `sim/` 目录，用 `vsim -batch -do ci.do` 以 batch 模式执行 `ci.do`，并把全部输出重定向到 `Transcript.transcript` 日志文件。注意 `os.system` 的返回值没有被接收，脚本不靠它判决。

接下来是整个 CI 最核心的「文本契约判决」：

[scripts/ciFlow.py:L18-L26](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py#L18-L26) —— 读出日志全文 `content`，先搜 `###ERROR###`：只要任一自检测试台打印过它（即 VHDL 输出与 Python 黄金模型逐位不一致），就 `exit(-1)` 判功能错；否则再搜 `SIMULATIONS COMPLETED SUCCESSFULLY`：若这条没出现，说明仿真压根没正常跑完（编译失败、TCL 报错、仿真器崩溃等），`exit(-2)` 判环境错。两条检查的**顺序很重要**——先查功能错，这样「既报错又没跑完」时优先归类为功能错。

> 注意 `###ERROR###` 在此处是 **CI 自己的第二道扫描**。PsiSim 的 `run_check_errors "###ERROR###"`（见 `run.tcl`）在 TCL 运行期间已经扫过一次并在命中时报错中止；`ciFlow.py` 事后对落盘日志再扫一次，属于「双保险」——不依赖 PsiSim 的中止行为是否可靠。

`ci.do` 只有三行有效内容，是把交互式 `run.tcl` 包成 batch 任务的薄壳：

[sim/ci.do:L7-L9](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/ci.do#L7-L9) —— `onerror {exit}` 让任何 TCL 错误立即退出 `vsim`（避免挂着等输入）；`source run.tcl` 复用人在交互式下用的同一份五步回归脚本；`quit` 退出仿真器。

`run.tcl` 的五步与 u1-l3 完全一致，这里只点出最后一步与 CI 的呼应：

[sim/run.tcl:L21-L30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/run.tcl#L21-L30) —— `compile_files -all -clean` 干净重编译、`run_tb -all` 跑所有测试台、`run_check_errors "###ERROR###"` 让 PsiSim 在 TCL 层扫错误标记。这三个 `puts` 分隔的段落（Compile/Run/Check）正是 `Transcript.transcript` 里 CI 能看到的结构化输出。

第二阶段——Python 单元测试：

[scripts/ciFlow.py:L28-L36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py#L28-L36) —— 切到 `unittest/`，`from psi_fix_pkg_test import *` 把所有测试类导入当前命名空间，`unittest.main(exit=False)` 发现并运行它们（`exit=False` 让它返回对象而不直接 `sys.exit`），再检查 `res.result.errors`（意外异常）与 `res.result.failures`（断言失败），任一非空则 `exit(-1)`。

#### 4.1.4 代码实践

**实践目标**：亲手追一遍 `ciFlow.py` 对日志的判决逻辑，验证「两级错误码」的分流。

**操作步骤**（纯源码阅读型，不需要安装 Modelsim）：

1. 打开 [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py)，定位 L22-L26 两条 `if`。
2. 在本地用 Python 模拟三种日志内容，观察分流：
   ```python
   # 示例代码：模拟 ciFlow 的判决逻辑（非项目原有代码）
   def triage(content: str) -> int:
       if "###ERROR###" in content:
           return -1            # 功能错
       if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
           return -2            # 环境错
       return 0                 # 全过

   print(triage("... ###ERROR### ..."))                      # 期望 -1
   print(triage("** Error: cannot find en_cl_fix"))          # 期望 -2（没跑完）
   print(triage("... SIMULATIONS COMPLETED SUCCESSFULLY"))   # 期望 0
   ```
3. 思考：如果某次回归既打印了 `###ERROR###` 又没打印成功标志，`triage` 返回什么？为什么 `ciFlow.py` 把 `###ERROR###` 检查放在前面是合理的？

**需要观察的现象**：上面三个 `print` 分别输出 `-1`、`-2`、`0`。

**预期结果**：第三步的答案是 `-1`。因为功能错检查在前——一旦确认有测试台报错，就无需再纠结「有没有跑完」，直接定性为功能回归失败。

> 待本地验证：若你装有 Modelsim 与全部姊妹仓库，可在 `scripts/` 下 `python ciFlow.py` 实跑一次；若没有，上面的源码阅读型实践已足够理解判决逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ciFlow.py` 用 `os.system(...)` 启动 `vsim` 却不检查它的返回值？

**参考答案**：因为不同仿真器/平台下退出码不可靠——编译错、license 失效、TCL 报错都可能让进程以不同方式退出，光看退出码无法稳定区分「测试不过」和「环境崩溃」。psi_fix 改用文本契约（搜 `###ERROR###` 与 `SIMULATIONS COMPLETED SUCCESSFULLY`），让判决只依赖自己定义、自己控制的字符串，跨仿真器一致。

**练习 2**：阶段 A 失败时，阶段 B（Python 单测）会跑吗？为什么这样设计？

**参考答案**：不会。阶段 A 的 `exit(-1)`/`exit(-2)` 会终止整个 Python 进程，阶段 B 根本到不了。设计成串行门控是为了：环境都崩了还跑 Python 单测是浪费；而且 VHDL 回归是更重的、更可能因环境问题失败的一环，先把它挡住。

### 4.2 依赖检出：dependencies.py 与 README 解析

#### 4.2.1 概念说明

psi_fix 不是孤立仓库——它的 VHDL 要编译就需要 `en_cl_fix`（定点算术内核）、`psi_common`（通用工具）、`psi_tb`（测试台帮手）；它的回归脚本需要 `PsiSim`（TCL 仿真框架）。如 u1-l1 所述，这些仓库必须按固定目录结构「并排摆放」。

手动一个一个 `git clone` 并切换到正确版本很繁琐且易错。`scripts/dependencies.py` 把这件事自动化：它读 `README.md` 里维护的依赖清单，自动把每个姊妹仓库克隆到正确位置、切换到要求的最低版本。

#### 4.2.2 核心流程

```text
dependencies.py
   ├─ from PsiFpgaLibDependencies import *      # 外部包提供 Parse / Actions
   ├─ Parse.FromReadme("../README.md")          # 解析 README 的依赖段
   │      └─ 读取 <!-- DO NOT CHANGE FORMAT --> 与 <!-- END OF PARSED SECTION --> 之间的内容
   └─ Actions.ExecMain(repo, dependencies)       # 按 CLI 参数克隆/检出依赖
```

这里的关键思想是 **「README 即唯一真相源」**：依赖清单只在 README 里维护一份，脚本去读它，而不是在脚本里再抄一份会过时的副本。README 用两行 HTML 注释作为「解析契约」，划出可被机器解析的区域。

#### 4.2.3 源码精读

`dependencies.py` 整个脚本只有 10 行，是对外部包的薄封装：

[scripts/dependencies.py:L1-L10](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/dependencies.py#L1-L10) —— `from PsiFpgaLibDependencies import *` 引入外部包（需单独安装，见 README 说明）；`Parse.FromReadme` 把 README 的依赖段解析成结构化对象；`Actions.ExecMain(repo, dependencies)` 根据命令行参数对这个仓库执行动作（克隆、检出指定版本等）。脚本本身不含任何依赖 URL 或版本号——全部来自 README。

README 中的「解析契约」用 HTML 注释明确标注：

[README.md:L45-L62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L45-L62) —— L45 的 `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->` 是解析起点，L62 的 `<!-- END OF PARSED SECTION -->` 是终点。中间列出了 `PsiSim (≥2.1.0)`、`psi_common (≥2.15.0)`、`psi_tb (≥2.7.0)`、`en_cl_fix (≥1.2.0)` 四个依赖及其版本下限。注释里的「DO NOT CHANGE FORMAT」是对维护者的硬约束——改了格式，脚本就解析不出来。

README 紧接着说明了脚本的用法与前置条件：

[README.md:L64-L70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L64-L70) —— 用 `python dependencies.py -help` 查看用法，并强调必须先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 这个 Python 包才能运行脚本。

#### 4.2.4 代码实践

**实践目标**：确认 README 的依赖段确实可被机械解析，并理解「唯一真相源」设计。

**操作步骤**：

1. 打开 [README.md 的依赖段](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L45-L62)，找出两行 HTML 注释边界。
2. 用下面的示例脚本（非项目原有代码）模拟 `Parse.FromReadme` 的核心——按注释边界截取中间文本：

   ```python
   # 示例代码：模拟依赖段截取（非项目原有代码）
   import re
   text = open("README.md").read()
   start = text.index("<!-- DO NOT CHANGE FORMAT")
   end   = text.index("<!-- END OF PARSED SECTION -->")
   print(text[start:end])
   ```
3. 检查输出是否包含四个依赖名与各自的版本号。

**需要观察的现象**：截取到的文本正好是依赖清单（PsiSim / psi_common / psi_tb / en_cl_fix 及版本下限），不含 README 其余部分。

**预期结果**：能拿到一段干净的、可被正则进一步解析的依赖列表。

> 待本地验证：若已安装 `PsiFpgaLibDependencies`，可 `cd scripts && python dependencies.py -help` 查看实际 CLI。

#### 4.2.5 小练习与答案

**练习 1**：为什么把依赖清单写在 README 里、而不是写在 `dependencies.py` 里？

**参考答案**：为了避免「两份会过时的副本」。README 是人读的，脚本是机器读的；若各维护一份，迟早对不上。让脚本去解析 README，README 就是唯一真相源，改依赖只改一处。

**练习 2**：如果有人把 `<!-- END OF PARSED SECTION -->` 这行删了，会发生什么？

**参考答案**：`Parse.FromReadme` 找不到结束边界，解析失败或截取到错误内容，脚本无法正确检出依赖。这正是注释里写「DO NOT CHANGE FORMAT」的原因——这两行 HTML 注释是人与机器之间的硬契约。

### 4.3 文档生成：hdl2md.py

#### 4.3.1 概念说明

psi_fix 的 `doc/files/` 下每个组件都有一份 Markdown 文档（如 `psi_fix_mov_avg.md`），里面有 generics 表和 interfaces 表。这些表**不是手写的**——它们是从 VHDL entity 源码**自动生成**的。这是 u1-l2 提到的「文档是生成产物」的具体实现。

`scripts/hdl2md.py` 就是这个生成器：它读一个 `.vhd` 文件，用正则表达式解析出 generic 列表和 port 列表，再用 pandas 的 `to_markdown()` 生成两张 Markdown 表，写进 `.md` 文件。

#### 4.3.2 核心流程

```text
hdl2md.py::hdl2md(file_name_i, path_name_o, psi_lib)
   ├─ 正则扫描 .vhd 文件，定位 generic(...) 与 port(...) 段
   ├─ 逐行解析：
   │    generic → gName / gType / gDesc
   │    port    → name / direction / size / desc
   ├─ size 为空或 '0' → 记为 '1'（标量端口）
   ├─ 构造两个 pandas DataFrame（generics、interfaces）
   └─ 写 .md 文件：PSI logo + 源码链接 + Description 占位 + 两张 to_markdown() 表
```

`hdl2md_all.py` 是它的批量版：列出一个目录下所有 `.vhd` 文件，对每个调用一次 `hdl2md`。

#### 4.3.3 源码精读

主函数签名与职责说明：

[scripts/hdl2md.py:L12-L17](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py#L12-L17) —— 三个参数：输入的 `.vhd` 文件、输出 `.md` 的目录、是否加 PSI logo。函数内声明了多组列表（`name/vector/size/desc/direction` 与 generic 的 `gName/gType/gVal/gDesc`）来收集解析结果。注意文件头注释明确说明：**本脚本只处理 RTL/entity，不处理 package 与 testbench**。

正则模式定义了解析的「触发开关」：

[scripts/hdl2md.py:L40-L43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py#L40-L43) —— `start_port`/`start_generic` 标记进入 port/generic 段，`end_entity` 标记 entity 结束，`end_element` 匹配行尾的 `;` 或 `)`。脚本靠这几个开关在逐行扫描中决定「当前在不在解析区」。

生成 Markdown 的关键——pandas DataFrame 转 Markdown 表：

[scripts/hdl2md.py:L131-L134](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py#L131-L134) —— 把 generic 的 `(Name, type, Description)` 与 port 的 `(Name, In/Out, Length, Description)` 各装进一个 DataFrame，用 `Name` 做索引，随后调 `to_markdown()` 一行生成 GitHub 风格的 Markdown 表格。

最终写出的文档结构：

[scripts/hdl2md.py:L139-L162](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py#L139-L162) —— 写入 PSI logo（可选）、标题、VHDL 源码链接、testbench 链接、`*INSERT YOUR TEXT*` 的 Description 占位符、`### Generics` 表、`### Interfaces` 表。注意 Description 是占位符——自动生成只负责结构，组件的功能描述仍需人工填写（这也是为什么 `doc/files/psi_fix_mov_avg.md` 里有完整的中文/英文描述，那是人补的，而两张表是机器生成的）。

批量生成器只是套了个循环：

[scripts/hdl2md_all.py:L22-L39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md_all.py#L22-L39) —— 列出目录下所有文件，原计划过滤掉 package（含 `pkg` 的文件名），然后对每个文件调一次 `hdl2md`。这是「一次生成全库文档」的入口。

#### 4.3.4 代码实践

**实践目标**：对照真实产出，确认文档表格确由 VHDL entity 生成。

**操作步骤**：

1. 打开已生成的文档样例 [doc/files/psi_fix_mov_avg.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_mov_avg.md)（本仓库内）。
2. 对照 VHDL 源 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) 的 generic 与 port 声明。
3. 核对：文档 `### Generics` 表里的 `in_fmt_g / out_fmt_g / taps_g / gain_corr_g / round_g / sat_g / out_regs_g` 是否与 entity 声明一一对应；`### Interfaces` 表里的 `clk_i / rst_i / dat_i / vld_i / dat_o / vld_o` 同理。

**需要观察的现象**：文档表格的每一行都能在 VHDL entity 的 generic/port 声明里找到同名的对应项；而 `### Description` 段是人工撰写的（如「This entity implements a moving average...」），不在 `hdl2md` 能生成的范围内。

**预期结果**：确认「接口表 = 生成产物、描述段 = 人工产物」的分工。

> 待本地验证：若装有 `pandas` 与 `tabulate`（`to_markdown` 的依赖），可 `cd scripts && python hdl2md_all.py` 对 `../hdl` 跑一遍，对比生成结果与已提交文档。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `hdl2md.py` 的注释说它「不处理 package 与 testbench」？

**参考答案**：因为它靠 `port(` / `generic(` 关键字定位解析区，而 package 声明的是常量与类型（没有 port）、testbench 的 entity 通常是空的（DUT 在 architecture 里例化）。两者都没有典型的「带方向的端口列表」可解析，强行解析会得到空表或乱码。所以生成器只服务于有实际端口的 RTL entity。

**练习 2**：`hdl2md` 生成的文档里 `### Description` 段为什么是 `*INSERT YOUR TEXT*`？

**参考答案**：因为组件的功能描述需要人用自然语言写，无法从 VHDL 语法机械推导。生成器只负责把可结构化的信息（端口名、方向、位宽、类型）落成表，描述段留占位符提醒作者补写。这体现了「机器管结构、人管语义」的分工。

### 4.4 Python 单元测试：psi_fix_pkg_test.py

#### 4.4.1 概念说明

CI 的第二阶段跑的是 `unittest/psi_fix_pkg_test.py`。要理解它覆盖「哪一层」，先回忆 psi_fix 的两层结构：

- **原语层（`psi_fix_pkg`）**：`resize / add / sub / mult / abs / neg / shift / compare / from_real / size` 等基础定点运算，是所有组件模型的公共积木（见 u2-l1、u2-l2、u2-l3）。
- **组件层（`psi_fix_mov_avg` / `psi_fix_fir_*` / `psi_fix_cic_*` / ...）**：一个个具体的 DSP 组件，每个有自己的 Python 模型与 VHDL 实现。

组件层由 **VHDL 协同仿真**（preScript 生成文本、测试台逐位比对、`###ERROR###`）验证；而原语层由 **Python 单元测试** 验证。这两层互补：

| 验证手段 | 覆盖层 | 比对方式 | 需要仿真器 |
|:---------|:-------|:---------|:-----------|
| VHDL 回归（阶段 A） | 组件层（每个组件的 VHDL vs Python 黄金模型） | 逐位整数比对 | 需要 Modelsim |
| Python 单测（阶段 B） | 原语层（`psi_fix_pkg.py` 的 API 正确性） | 浮点 `assertEqual` | 不需要 |

为什么单测只覆盖原语层就够了？因为**原语是所有组件模型的地基**。如果 `psi_fix_resize` 算错了，每个组件的 Python 黄金模型会同时跟着错——而那时 VHDL 协同仿真反而会「通过」（两边错得一样），掩盖问题。所以用一组快、纯 Python、不依赖仿真器的原语单测给地基把关，是高性价比的第一道防线。

#### 4.4.2 核心流程

```text
psi_fix_pkg_test.py
   ├─ sys.path.append("../model"); from psi_fix_pkg import *   # 引入被测原语
   ├─ 按函数分组组织测试类：
   │     PsiFixSizeTest / PsiFixFromRealTest / PsiFixResizeTest /
   │     PsiFixAddTest / PsiFixSubTest / PsiFixMultTest /
   │     PsiFixAbsTest / PsiFixNegTest /
   │     PsiFixShiftLeftTest / PsiFixShiftRightTest /
   │     PsiFixUpperBoundTest / PsiFixLowerBoundTest / PsiFixInRangeTest / ...
   └─ ciFlow 经 unittest.main(exit=False) 运行，errors/failures 非空 → exit(-1)
```

每个测试方法都用 `self.assertEqual(期望浮点值, psi_fix_xxx(...))` 断言，且测试值都选成**二进制可精确表示**的数（如 `1.25`、`2.5`、`-1.5`），避免浮点比较的坑。这与 VHDL 协同仿真的「逐位整数比对」是不同层级的验证——单测验的是数学 API 的语义正确性，协同仿真题的是位真一致性。

#### 4.4.3 源码精读

测试文件如何接入被测包：

[unittest/psi_fix_pkg_test.py:L6-L10](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L6-L10) —— `sys.path.append("../model")` 把 `model/` 加入搜索路径，`from psi_fix_pkg import *` 引入全部原语函数与类型（`psi_fix_fmt_t`、`psi_fix_rnd_t`、`psi_fix_sat_t`）。这说明单测的**被测对象就是 `model/psi_fix_pkg.py`**——即位真双模型的 Python 半边原语层（u2-l3 详讲）。

测试类的组织方式——以 `psi_fix_size`（算总位宽）为例：

[unittest/psi_fix_pkg_test.py:L16-L38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L16-L38) —— 每个被测函数一个 `unittest.TestCase` 子类，每个用例测一种格式组合。例如 `test_IntAndFract` 断言 `psi_fix_size(psi_fix_fmt_t(1, 3, 3)) == 7`（1 符号 + 3 整数 + 3 小数 = 7 位），`test_NegativeInt` 验证负整数位（如 `[1,-2,3]` = 1−2+3 = 2 位）也能正确计算。这些都是 u1-l4 讲过的 `[s,i,f]` 总位宽 W=s+i+f 规则。

再看一个带舍入/饱和的典型用例——`psi_fix_resize` 的饱和行为：

[unittest/psi_fix_pkg_test.py:L122-L132](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L122-L132) —— `test_RemoveInterBit_Signed_Wrap_Positive` 验证：把 `5.5`（格式 `[1,3,1]`）塞进更窄的 `[1,2,1]` 并用 `wrap`，结果回绕成 `-2.5`；而 `test_RemoveInterBit_Signed_Sat_Positive` 用 `sat` 则饱和到 `3.5`。这两条用例直接守住了 u1-l4 讲的「饱和 vs 回绕」语义，是整个库所有组件都会间接依赖的原语契约。

测试运行器（也可脱离 CI 单独跑）：

[unittest/psi_fix_pkg_test.py:L609-L613](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L609-L613) —— 文件末尾的 `if __name__ == "__main__": unittest.main()` 让你可以在 `unittest/` 目录下直接 `python psi_fix_pkg_test.py` 单跑这一层，不必走完整 CI。

#### 4.4.4 代码实践

**实践目标**：亲手跑一次 Python 单测，确认它只覆盖原语层、且不依赖任何仿真器。

**操作步骤**：

1. 确认本机有 Python 3 与 `numpy`/`scipy`（`model/psi_fix_pkg.py` 的依赖），并把姊妹仓库 `en_cl_fix` 摆到与本项目同级（因为 `psi_fix_pkg.py` 会 `sys.path.insert` 引用它，见 u2-l3）。
2. 进 `unittest/` 目录直接跑：
   ```bash
   cd unittest
   python psi_fix_pkg_test.py
   ```
3. 观察输出的 `Ran N tests in x.xs` 与 `OK`。

**需要观察的现象**：测试能**在没有 Modelsim 的机器上**全部通过；输出里能看到按函数分组的用例数（size、from_real、resize、add、sub、mult、abs、neg、shift_left、shift_right、upper_bound、lower_bound、in_range …）。

**预期结果**：`OK`，且没有任何组件名（如 `mov_avg`、`fir`）出现在测试里——这印证了单测只覆盖**原语层**，组件层交给 VHDL 协同仿真。

> 待本地验证：若 `en_cl_fix` 未并排摆放，`from psi_fix_pkg import *` 会因找不到依赖而失败——这正好印证了 u1-l1 讲的并排摆放目录结构。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Python 单测里能用浮点 `assertEqual(1.25, psi_fix_from_real(...))`，而 VHDL 协同仿真却要把结果转成整数再比？

**参考答案**：单测验的是原语 API 的数学语义，测试值都选成二进制可精确表示的数（如 `1.25 = 1 + 1/4`），浮点无误差，故可直接比浮点。VHDL 协同仿真题的是**位真**——必须确认 VHDL 与 Python 在每一个 bit 上都一致，而「同一段二进制位按有符号整数解读相等 ⟺ 每一位都相等」（见 u3-l2），所以转成整数比更严格、也更贴近硬件。

**练习 2**：如果有人改坏了 `model/psi_fix_pkg.py` 里的 `psi_fix_resize`，CI 的哪个阶段会先报警？

**参考答案**：阶段 A（VHDL 回归）**可能反而通过**——因为 Python 黄金模型和它驱动的 preScript 期望值会跟着一起错，与 VHDL「错得一致」仍逐位相等。这时阶段 B（Python 单测）的 `PsiFixResizeTest` 会用独立硬编码的期望值（如 `3.5`、`-2.5`）抓住回归。这正是单测作为「地基防线」的价值——它不依赖黄金模型自身，而是用绝对真值卡住原语语义。

## 5. 综合实践

**任务**：为 psi_fix 设计一张「CI 全景流程图」，并把本讲四个模块串成一条因果链。

**操作步骤**：

1. **画环境准备链**：从「一台空机器」出发，画出 `dependencies.py` 如何解析 [README 依赖段](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L45-L62)、把四个姊妹仓库（PsiSim / psi_common / psi_tb / en_cl_fix）摆到正确位置。
2. **画回归执行链**：`ciFlow.py` → `vsim -batch -do ci.do` → `ci.do` → `run.tcl` 五步 → 全部测试台（每个跑 preScript 生成的位真文本比对）。
3. **画判决链**：`Transcript.transcript` 里的两个魔法字符串如何分流成 `-1`（功能错）/ `-2`（环境错）；通过后进入阶段 B 的 Python 单测。
4. **画文档旁路**：`hdl2md.py` 不参与 CI 判决，但维护 `doc/files/` 的接口表，是「文档即生成产物」的一环。
5. **标注互补关系**：在图上用两种颜色区分「组件层验证（VHDL 协同仿真，逐位）」与「原语层验证（Python 单测，浮点）」。

**交付物**：一张图 + 一段说明，解释「为什么 `ciFlow.py` 既要做 VHDL 回归又要做 Python 单测，二者不可互相替代」。

**参考要点**：VHDL 回归覆盖每个组件的端到端位真，但依赖仿真器、且黄金模型自身错了会漏报；Python 单测覆盖原语层、不依赖仿真器、用绝对真值卡住地基，是黄金模型自身的守门员。两者一纵（组件）一横（原语），共同构成回归网。

> 待本地验证：若有完整环境，可故意在 `model/psi_fix_pkg.py` 里把 `psi_fix_resize` 的饱和逻辑改错，分别观察阶段 A、阶段 B 的表现，验证练习 2 的结论。

## 6. 本讲小结

- `ciFlow.py` 是 CI 总编排，**两阶段串行**：先 Modelsim 跑 VHDL 回归，通过后才跑 Python 单测；前一阶段失败直接 `exit`，后一阶段不执行。
- 判决**不信任仿真器退出码**，而是扫描 `Transcript.transcript` 文本：含 `###ERROR###` → `exit(-1)`（功能错/位真不匹配）；缺 `SIMULATIONS COMPLETED SUCCESSFULLY` → `exit(-2)`（环境错/没跑完）。这是两级错误分流。
- `ci.do` 只是 `onerror {exit}; source run.tcl; quit` 的 batch 薄壳，复用人在交互式下用的同一份五步回归脚本。
- `dependencies.py` 是对外部 `PsiFpgaLibDependencies` 包的薄封装，把 `README.md`（用 HTML 注释划定解析边界）当作依赖清单的**唯一真相源**。
- `hdl2md.py` 用正则解析 VHDL entity、借 pandas `to_markdown()` 生成 generics/interfaces 表，文档的接口表是**生成产物**、描述段是人工产物。
- `psi_fix_pkg_test.py` 覆盖的是**原语层**（`psi_fix_pkg.py` 的 resize/add/mult/… API），与覆盖组件层的 VHDL 协同仿真互补——它是黄金模型自身的守门员。

## 7. 下一步学习建议

本讲是「贡献、CI 与二次开发」单元的第二篇，也是整套学习手册的尾声之一。建议：

- **回顾闭环**：回到 [u10-l1 贡献新组件的完整流程](u10-l1-contributing-new-component.md)，把本讲学到的「CI 两个阶段、`###ERROR###` 判据、文档生成、依赖检出」套到一个新组件的入库 checklist 上——你会发现自己已经能说清「一个新组件从写完到被 CI 验收」的每一步机器行为。
- **实战建议**：挑一个简单组件（如 `psi_fix_comparator`），刻意构造一次「VHDL 输出与 Python 模型不一致」的改动，push 后观察 CI 如何在 `Transcript.transcript` 里打印 `###ERROR###` 并 `exit(-1)`，把本讲的判决链看一遍真实发生的样子。
- **延伸阅读**：若想深入 PsiSim 框架本身（`run_check_errors`、`run_tb -all` 的实现、`SIMULATIONS COMPLETED SUCCESSFULLY` 的发射点），需走出本仓库、去看并排摆放的 `PsiSim` 仓库的 TCL 源码——本讲只覆盖了 psi_fix 这一侧的契约。
- **若继续维护本手册**：关注 `scripts/` 下未来新增的自动化脚本（如重构工具 `scripts/refactoring/`），它们可能值得新增一篇讲义补充进本单元。
