# 维护与扩展集合仓库

## 1. 本讲目标

本讲面向**已经理解 psi_fpga_all 是一个 collection-repo、并读过其版本管理方式**的读者，回答三个「我想动手维护这个仓库」时最实际的问题：

1. 我想新增一个 FPGA 库到这个集合仓库，`.gitmodules` 里该写什么？为什么 url 长成 `../../paulscherrerinstitute/...` 这样？
2. 提交 `774a090`「Submodules usable with either SSH or HTTPS」到底改了什么？为什么这一改动让 SSH 用户和 HTTPS 用户都能直接用，而以前不行？
3. `scripts/.gitignore` 用「先忽略全部、再放行 `*.tcl`」的白名单策略，这种写法在维护中起什么保护作用？

学完本讲，你应当能够：

- 按 `.gitmodules` 现有约定，独立写出一条新增 submodule 的配置，并说清楚 `path` 与相对 `url` 各自的含义。
- 解释相对 URL（`../../`）如何被 git 解析、为何能让子模块自动继承父仓库的克隆协议（SSH/HTTPS）。
- 读懂 `scripts/.gitignore` 的「ignore-all-except」白名单，并知道新增脚本、新增运行产物时该不该动它。

> 本讲是「专家层」的第二讲，承接 [u1-l2（submodule 机制与克隆）](u1-l2-submodules-and-cloning.md) 与 [u3-l1（发布管理与版本固定）](u3-l1-release-and-version-pinning.md)。它把前两讲建立的「`.gitmodules` 的 path/url」「gitlink 指针」概念推进到**动手维护**层面。

## 2. 前置知识

在进入本讲之前，请确认你已理解下列概念（均在依赖讲义中建立）：

- **collection-repo（集合仓库）**：psi_fpga_all 本身几乎不含代码，而是用 git submodule 把一批独立的 FPGA 库挂到固定的目录结构上。详见 [u1-l1](u1-l1-project-overview.md)。
- **submodule 的两个关键字段**：`.gitmodules` 里每条子模块用 `path` 决定本地目录、用 `url` 决定远程仓库；父仓库只保存指向子模块某次 commit 的指针（gitlink，模式 `160000`），不保存子模块的文件内容。详见 [u1-l2](u1-l2-submodules-and-cloning.md)。
- **两套事实来源**：`Changelog.md` 是人工维护的「人类账本」，而父仓库里记录的 gitlink 才是机器认定的真实指针；单独更新一个子模块只改 gitlink、不会自动改 Changelog。详见 [u3-l1](u3-l1-release-and-version-pinning.md)。

本讲会用到的两个 git 基础概念，先用大白话补一下：

- **相对 URL（relative URL）**：submodule 的 `url` 不一定非要是完整的 `https://...` 或 `git@...`，也可以写成以 `../` 开头的相对路径。git 会以**父仓库（superproject）的远程地址**为基准，像拼路径一样把它解析成完整地址。
- **gitignore 的「先否后肯」规则**：gitignore 按从上到下的顺序生效，**后面的规则可以覆盖前面的**。`*` 表示「忽略一切」，`!xxx` 表示「取消忽略 xxx」。把 `*` 放最上面、再用一串 `!` 放行少数文件，就构成了「白名单」策略。

## 3. 本讲源码地图

本讲只读三个文件，它们正是「维护与扩展」的全部着力点：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `.gitmodules` | 声明全部 23 个子模块的 `path` 与相对 `url` | 相对 URL 约定、新增 submodule 的写法 |
| `README.md` | 仓库说明，含 `Cloning` 章节（SSH/HTTPS 两条命令） | SSH/HTTPS 双兼容的用户侧表现 |
| `scripts/.gitignore` | scripts 目录的白名单忽略规则 | ignore-all-except 策略如何保护仓库整洁 |

此外会引用一次提交 `774a090` 的 diff 作为「相对 URL 改造」的历史证据。

## 4. 核心概念与源码讲解

### 4.1 `.gitmodules` 相对 URL 约定（新增 submodule 的格式）

#### 4.1.1 概念说明

集合仓库每加一个库，本质上是「在固定目录下挂一个 submodule」。这件事在仓库里留下两处痕迹：

1. `.gitmodules` 里新增一条 `[submodule "..."]` 块，写明 `path`（挂到哪个本地目录）和 `url`（从哪个远程仓库拉）。
2. 父仓库的 git 索引里新增一个 gitlink（模式 `160000`），指向该子模块的某次 commit（详见 [u3-l1](u3-l1-release-and-version-pinning.md)）。

psi_fpga_all 对这两处都有**严格的格式约定**，遵守约定是「目录结构即接口」这个核心约束的直接体现——任何库都靠相对路径找到别的库，path 写错或 url 写错都会让引用断裂。

#### 4.1.2 核心流程

新增一个 VHDL 库（假设叫 `psi_new_lib`）的标准流程：

```
1. 确认它在 GitHub 上的归属：github.com/paulscherrerinstitute/psi_new_lib
   （与现有所有库同属 paulscherrerinstitute 这个 org）

2. 用 git submodule add 登记，path 放到对应类别目录下：
   git submodule add ../../paulscherrerinstitute/psi_new_lib.git VHDL/psi_new_lib

3. git 会自动：
   - 克隆该仓库到 VHDL/psi_new_lib
   - 在 .gitmodules 写入一条符合约定的条目
   - 在父仓库索引写入指向当前 commit 的 gitlink

4. （人工）按 u3-l1 的约定，在 Changelog.md 对应 release 段补一条版本记录
```

关键格式约定有两条：

- **`path` = `<类别目录>/<仓库名>`**。类别目录只能是 `VHDL/`、`TCL/`、`Python/`、`VivadoIp/` 四选一（详见 [u1-l3](u1-l3-directory-and-library-categories.md)）。VHDL 库就放 `VHDL/` 下，工具框架放 `TCL/` 下，以此类推。
- **`url` = `../../paulscherrerinstitute/<仓库名>.git`**。一律用相对形式，`../../` 走两级、再进 `paulscherrerinstitute` org。**不要**写成 `git@github.com:...` 或 `https://github.com/...` 这样的绝对地址——原因见 4.2。

#### 4.1.3 源码精读

打开 `.gitmodules`，每一条都是同一个三行模板。以第一条为例：

[.gitmodules:1-3](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L1-L3) —— 声明 `VHDL/psi_tb` 子模块：`path` 把它挂到 `VHDL/psi_tb`，`url` 用相对形式 `../../paulscherrerinstitute/psi_tb.git`。

```ini
[submodule "VHDL/psi_tb"]
	path = VHDL/psi_tb
	url = ../../paulscherrerinstitute/psi_tb.git
```

注意三点：

1. `[submodule "..."]` 标题里的名字与 `path` 完全一致（都是 `VHDL/psi_tb`）。这是全文件的一致约定，新增时也要保持标题名 == path。
2. `path` 的第一段 `VHDL` 就是四大类别目录之一，决定了这个库在「目录结构即接口」里所处的层级。
3. `url` 全文件 23 条**全部**是 `../../paulscherrerinstitute/<repo>.git`，无一例外。换一个类别（比如 TCL）也只是仓库名变，相对前缀不变：

[.gitmodules:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L7-L9) —— `TCL/PsiSim` 的声明：类别换成 `TCL/`，但 url 仍是 `../../paulscherrerinstitute/PsiSim.git`。

```ini
[submodule "TCL/PsiSim"]
	path = TCL/PsiSim
	url = ../../paulscherrerinstitute/PsiSim.git
```

VivadoIp 类同理，仓库名带 `vivadoIP_` 前缀，但相对前缀依旧：

[.gitmodules:31-33](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L31-L33) —— 第一个 VivadoIp 子模块 `vivadoIP_data_rec`，url 仍以 `../../paulscherrerinstitute/` 开头。

```ini
[submodule "VivadoIp/vivadoIP_data_rec"]
	path = VivadoIp/vivadoIP_data_rec
	url = ../../paulscherrerinstitute/vivadoIP_data_rec.git
```

**结论**：新增 submodule 的「正确答案」只有一个模板，把 `<类别>` 和 `<仓库名>` 填进去即可。

#### 4.1.4 代码实践

**实践目标**：为假设的新 VHDL 库 `psi_new_lib` 写出一条完全符合现有约定的 `.gitmodules` 配置。

**操作步骤**：

1. 打开 `.gitmodules`，对照 4.1.3 的三段示例，确认模板为：

   ```
   [submodule "<类别>/<仓库名>"]
   	path = <类别>/<仓库名>
   	url = ../../paulscherrerinstitute/<仓库名>.git
   ```

2. 因为 `psi_new_lib` 是 VHDL 库（类比 `psi_tb`、`psi_common`），`<类别>` 取 `VHDL`。

3. 写出新条目（**示例答案**，并非仓库已有内容）：

   ```ini
   [submodule "VHDL/psi_new_lib"]
   	path = VHDL/psi_new_lib
   	url = ../../paulscherrerinstitute/psi_new_lib.git
   ```

4. 如果想真正登记（需要该仓库在 GitHub 上存在、且你有写权限），等价的命令是：

   ```bash
   git submodule add ../../paulscherrerinstitute/psi_new_lib.git VHDL/psi_new_lib
   ```

   git 会自动写入上面那段 `.gitmodules`，并登记 gitlink。

**需要观察的现象**：手写条目时，三行的「名字」必须一致——标题里的 `"VHDL/psi_new_lib"`、`path` 的值、`url` 末尾的仓库名 `psi_new_lib.git`，三者不对应就会被 git 视为异常。

**预期结果**：得到一条与现有 23 条风格完全一致的配置；`git submodule status` 能识别它（前提是远程仓库真实存在）。

**待本地验证**：由于 `psi_new_lib` 是假设库，`git submodule add` 实际会因远程不存在而失败；本实践以「写出正确格式」为验收标准，真实登记需替换为一个已存在的仓库。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个新库错写成 `url = ../paulscherrerinstitute/psi_new_lib.git`（只有一个 `../`），它还能解析到正确的仓库吗？

> **答案**：能解析到**同一个**目标。以父仓库路径 `paulscherrerinstitute/psi_fpga_all.git` 为基准：`../paulscherrerinstitute/psi_new_lib.git` 与 `../../paulscherrerinstitute/psi_new_lib.git` 最终都落在 `paulscherrerinstitute/psi_new_lib.git`（前者是「上一级到 org 再进同名 org」，后者是「上两级到根再进 org」，殊途同归）。但本仓库**统一采用 `../../` 的显式写法**，新增时应遵循现有约定，不要混用。

**练习 2**：为什么新增条目的 `path` 第一段必须是四大类别目录之一，而不能自创新目录（比如 `MyLibs/`）？

> **答案**：因为「目录结构即接口」——其它库用相对路径引用本库时，依赖它在固定类别目录下的位置。自创新目录会让所有依赖该库的相对路径失效，也会破坏 `Changelog.md` 按 `TCL/VHDL/Python/VivadoIP` 四类分组的版本记录结构。

---

### 4.2 SSH/HTTPS 双兼容（提交 774a090）

#### 4.2.1 概念说明

GitHub 上的仓库有两种主流克隆协议：

- **SSH**：`git@github.com:paulscherrerinstitute/psi_fpga_all.git`，需要本地配置 SSH key 并关联 GitHub 账号。
- **HTTPS**：`https://github.com/paulscherrerinstitute/psi_fpga_all.git`，任何能上网的人都能拉取（公开仓库），无需 SSH key。

对**单仓库**这无所谓，但对**带 submodule 的集合仓库**却是个坑：submodule 的 `url` 写在 `.gitmodules` 里、对所有克隆者共享。如果它写死成 SSH 地址，那么用 HTTPS 克隆、且没配 SSH key 的用户，在拉子模块时就会失败；反之亦然。

提交 `774a090`「Submodules usable with either SSH or HTTPS」就是为了根治这个问题：它把全部子模块的 `url` 从**绝对 SSH 地址**改成**相对地址**，让子模块自动继承克隆父仓库时所用的协议。

#### 4.2.2 核心流程

相对 URL 的解析规则（git 标准行为）：以**父仓库远程地址的「路径部分」**为基准，按文件系统路径语义向上回溯。

```
父仓库 origin 的路径部分：paulscherrerinstitute/psi_fpga_all.git

子模块 url：             ../../paulscherrerinstitute/psi_tb.git

解析（逐级 ..）：
   paulscherrerinstitute/psi_fpga_all.git
   ..        → paulscherrerinstitute
   ../..     → （主机根）
   再拼接 paulscherrerinstitute/psi_tb.git
   =         paulscherrerinstitute/psi_tb.git

最终地址（继承父仓库的「协议 + 主机」）：
   SSH  克隆者 → git@github.com:paulscherrerinstitute/psi_tb.git
   HTTPS 克隆者 → https://github.com/paulscherrerinstitute/psi_tb.git
```

关键点：相对 URL **只携带路径信息，不携带协议**。协议与主机从父仓库继承，于是「父仓库怎么克隆，子模块就怎么拉」。这就是提交信息里说的 *"depending on which method was used to originally clone this repo"*。

改造前后对照：

| 阶段 | `.gitmodules` 里的 url | HTTPS 用户的体验 |
| --- | --- | --- |
| 改造前（`774a090` 之前） | `git@github.com:paulscherrerinstitute/<repo>.git`（绝对 SSH） | 拉 submodule 时走 SSH，无 key 则**失败**，需手动 `insteadOf` 重写 |
| 改造后（`774a090` 至今） | `../../paulscherrerinstitute/<repo>.git`（相对） | 自动走 HTTPS，**开箱即用** |

#### 4.2.3 源码精读

提交 `774a090` 的 diff 是最强证据。它的 `.gitmodules` 改动只有一种模式，反复出现 23 次：

```diff
 [submodule "VHDL/psi_tb"]
 	path = VHDL/psi_tb
-	url = git@github.com:paulscherrerinstitute/psi_tb.git
+	url = ../../paulscherrerinstitute/psi_tb.git
```

即把每一条 `git@github.com:paulscherrerinstitute/<repo>.git` 换成 `../../paulscherrerinstitute/<repo>.git`，**协议前缀整段消失，换成 `../../`**。这就是 4.1 里那个相对约定的由来——它不是风格选择，而是「让两种协议都能用」的功能性改动。

同一提交还更新了 README，正式向用户说明 HTTPS 也可以用：

[README.md:34-45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L34-L45) —— `Cloning` 章节：先给出 SSH 克隆命令，紧接着说明「如果没有配 SSH 的 GitHub 账号，就改用 HTTPS」，并给出 HTTPS 命令。

```markdown
Because the repository contains submodules, it must be cloned with the *--recurse-submodules* option:

git clone --recurse-submodules git@github.com:paulscherrerinstitute/psi_fpga_all.git

If you do not have a github account with SSH configured use https instead:

git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
```

两条命令**唯一区别是协议前缀**。正是因为子模块用了相对 URL，用户选哪条，子模块就跟着走哪条协议——README 才敢同时给出两种克隆方式而无需任何额外配置。

> 历史脉络：从 `git log` 可见，仓库在 `774a090` 之前曾在绝对地址之间来回切换（例如 `36eb014 GIT: https->ssh`，把地址改成绝对 SSH）。这种「写死协议」的做法总会让另一拨用户吃亏。`774a090` 用相对 URL 一劳永逸地消除了协议绑定，是这条维护痛点的最终解法。

#### 4.2.4 代码实践

**实践目标**：亲手验证「相对 URL 继承父仓库协议」，并说清楚相对 URL 相比绝对 URL 的好处。

**操作步骤**：

1. 在 `.gitmodules` 任取一条（如 `psi_tb`），写下它的相对 url：`../../paulscherrerinstitute/psi_tb.git`。
2. 按 4.2.2 的解析表，分别假设父仓库被 SSH 和 HTTPS 克隆，手算两种最终地址。
3. （可选，待本地验证）在两个不同的临时目录里分别用 SSH 与 HTTPS 克隆本仓库，再执行 `git submodule status`，观察子模块是否都成功拉取、且 `git config --get submodule.VHDL/psi_tb.url` 在两种克隆下分别解析成什么。

**需要观察的现象**：无论用哪种协议克隆，子模块都能拉取成功，**无需**手动编辑 `.gitmodules`，也**无需**配置任何 `insteadOf` 重写规则。

**预期结果**：

- SSH 克隆 → 子模块走 `git@github.com:...`
- HTTPS 克隆 → 子模块走 `https://github.com/...`

**改造前对比**（用于理解「为什么相对 URL 是进步」）：在 `774a090` 之前，`.gitmodules` 写死 `git@github.com:...`，HTTPS 用户必须额外执行一条全局重写才能拉子模块（**示例命令**，属标准 git 用法）：

```bash
git config --global url."https://github.com/".insteadOf "git@github.com:"
```

这条 `insteadOf` 把所有 SSH 地址改写成 HTTPS，是相对 URL 出现之前的权宜之计。相对 URL 让每个普通用户都不必再这么做。

**一句话总结（本实践要求写出的那段话）**：相对 URL（`../../`）相比绝对 SSH/HTTPS URL 的好处是——**协议不再硬编码进仓库共享的 `.gitmodules`**，子模块继承每个克隆者各自选用的协议，于是同一个集合仓库对 SSH 用户和 HTTPS 用户都「零配置可用」；维护者也只需维护一份 `.gitmodules`，不必在两种协议之间二选一、也不必逼用户各自打补丁。

**待本地验证**：手算解析可在阅读中完成；真实双协议克隆验证需要本地具备 SSH key 与网络环境。

#### 4.2.5 小练习与答案

**练习 1**：假设未来某子模块要迁移到**另一个 GitHub org**（比如 `paulscherrerinstitute2`），现有 `../../paulscherrerinstitute/<repo>.git` 还能用吗？

> **答案**：不能直接用。`../../paulscherrerinstitute/...` 写死了 org 名 `paulscherrerinstitute`。迁到别的 org 需要改成 `../../paulscherrerinstitute2/<repo>.git`。这正是相对 URL 的边界：它只「协议无关」，并不「org 无关」——org 仍是路径的一部分。

**练习 2**：如果某用户公司防火墙只允许 HTTPS、禁用 SSH，在改造前（绝对 SSH url）和改造后（相对 url）分别会怎样？

> **答案**：改造前，该用户拉子模块会尝试 SSH 被防火墙阻断，必须靠 `insteadOf` 把 SSH 重写成 HTTPS（或手动改 `.gitmodules`）。改造后，只要该用户用 HTTPS 克隆父仓库，子模块自动走 HTTPS，**无需任何额外配置**即可通过防火墙。

---

### 4.3 `scripts/.gitignore` 的 ignore-all-except 策略

#### 4.3.1 概念说明

`scripts/` 是仓库里唯一有「真实可执行代码」的地方——4 个 TCL 驱动脚本（`runModelsim.tcl`、`runGhdl.tcl`、`runVivado.tcl`、`packageAllIp.tcl`，详见 [u2-l1](u2-l1-scripts-overview.md)）。但这里同时也是**仿真与打包实际运行的现场**：跑一次 PsiSim 仿真会在 `scripts/` 下生成日志、临时工作目录、以及一个由框架生成的 `psi_sim_run.tcl` 等产物。

如果让这些产物混进版本库，仓库会迅速被噪声淹没。`scripts/.gitignore` 用一种**白名单（ignore-all-except）**策略解决这个问题：默认忽略一切，只放行需要长期保存的 `.tcl` 驱动脚本。

#### 4.3.2 核心流程

gitignore 规则按顺序生效、后者覆盖前者。白名单策略的三段逻辑：

```
1. *          → 忽略 scripts/ 下所有文件与目录（默认全屏蔽）
2. !*.tcl     → 放行所有 .tcl 文件（手动维护的驱动脚本）
3. !.gitignore→ 放行 .gitignore 自身（否则它会被第 1 行忽略掉，无法被跟踪）
   ── 以上构成「白名单」 ──
4. psi_sim_run.tcl → 再次忽略这个特定文件（覆盖第 2 行的 !*.tcl）
```

第 4 行是点睛之笔：`psi_sim_run.tcl` 虽然后缀是 `.tcl`（本会被第 2 行放行），但它是**运行时生成的产物**，所以放在最后、用更具体的规则把它重新踢回忽略名单。

维护含义：

- **新增一个驱动脚本**：只要它叫 `xxx.tcl` 且直接放在 `scripts/` 下，就**自动被跟踪**，无需改动 `.gitignore`。
- **新增一个运行产物**：如果它恰好也以 `.tcl` 结尾（如 `psi_sim_run.tcl`），必须在文件末尾补一行显式忽略；如果不是 `.tcl`，则被第 1 行的 `*` 自动忽略，无需处理。

#### 4.3.3 源码精读

[scripts/.gitignore:1-7](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore#L1-L7) —— 全文只有 5 行有效规则，却把「保留什么、忽略什么」表达得一清二楚。

```gitignore
#Ignore everything except the file types required
*
!*.tcl
!.gitignore

#Ignore artifacts from runs
psi_sim_run.tcl
```

逐行解读：

| 行 | 规则 | 作用 |
| --- | --- | --- |
| 2 | `*` | 忽略 `scripts/` 下的一切（默认屏蔽） |
| 3 | `!*.tcl` | 放行所有 `.tcl`（4 个驱动脚本由此被跟踪） |
| 4 | `!.gitignore` | 放行 `.gitignore` 自身（否则被第 2 行屏蔽） |
| 7 | `psi_sim_run.tcl` | 再次忽略运行产物（覆盖第 3 行对该文件的放行） |

一个容易踩的坑（重要维护提示）：gitignore 规定「**若某文件所在的父目录已被忽略，就无法再用 `!` 把它放行**」。本策略能工作，是因为 4 个驱动脚本都**直接平铺在 `scripts/` 根下**，`*.tcl` 直接匹配到它们。如果将来有人把驱动脚本放进子目录（例如 `scripts/helpers/xxx.tcl`），那么第 1 行的 `*` 会先把 `helpers/` 这个目录整个忽略，第 3 行的 `!*.tcl` **救不回来**——届时需要先 `!helpers/` 再 `!helpers/*.tcl`。这是白名单策略在维护中的主要注意点。

#### 4.3.4 代码实践

**实践目标**：读懂 `scripts/.gitignore` 的每一行，并判断「新增一种运行产物」时该不该改动它。

**操作步骤**：

1. 打开 `scripts/.gitignore`，对照 4.3.3 的表格，逐行标注「忽略 / 放行」。
2. 思考并回答两个假设场景：
   - 在 `scripts/` 下新增一个 `runMyFlow.tcl` 驱动脚本：需要改 `.gitignore` 吗？
   - 仿真新产生一个名为 `my_sim_output.tcl` 的临时文件：需要改 `.gitignore` 吗？如果需要，加在哪一行？
3. 用 git 实际验证（**待本地验证**）：在 `scripts/` 下分别创建一个 `scratch.txt` 和一个 `fake_run.tcl`，执行 `git status --ignored scripts/`，观察前者被忽略、后者（在无第 7 行那种规则时）被跟踪。

**需要观察的现象**：

- `scratch.txt` 因匹配 `*` 而被忽略（非 `.tcl`，不会被 `!*.tcl` 放行）。
- `fake_run.tcl` 因匹配 `!*.tcl` 而被跟踪——这正说明：任何 `.tcl` 产物若想被忽略，**必须**像 `psi_sim_run.tcl` 那样在末尾单独列一行。

**预期结果**：你会得出两条维护准则——
1. 新增手写驱动脚本（`.tcl`、平铺在 `scripts/`）：**不用动** `.gitignore`。
2. 新增 `.tcl` 结尾的运行产物：**必须在末尾追加一行**该文件名，否则会被误提交。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉第 4 行 `!.gitignore`，会发生什么？

> **答案**：`.gitignore` 自身会匹配第 2 行的 `*` 而被忽略，git 就不再跟踪它。结果是 `.gitignore` 仿佛「消失」于版本控制（本地还在，但不被纳入提交），其他人克隆后拿不到这套白名单规则，`scripts/` 的整洁性随之失效。所以 `!.gitignore` 这一行是白名单策略的「自举」必需项。

**练习 2**：为什么 `psi_sim_run.tcl` 要写在文件**最末尾**，而不是开头？

> **答案**：因为 gitignore 后行覆盖前行。`psi_sim_run.tcl` 同时匹配 `!*.tcl`（放行）和它自身的忽略规则，只有把它的忽略规则写在 `!*.tcl` **之后**，忽略才能压过放行。写在开头会被第 3 行 `!*.tcl` 反过来覆盖，导致它仍被跟踪。

---

## 5. 综合实践

把本讲三个模块串成一个「为集合仓库接入一个新库」的完整维护任务。

**场景**：PSI 新发布了一个 VHDL 库 `psi_new_lib`（GitHub: `paulscherrerinstitute/psi_new_lib`），你要把它接入 psi_fpga_all，并确保接入过程符合仓库的全部维护约定。

**任务清单**：

1. **写配置**：按 4.1 的约定，写出新增的 `.gitmodules` 条目（`path` 与相对 `url` 都要符合现有风格）。
2. **验证协议无关**：按 4.2 的解析表，说明为什么这条相对 `url` 同时对 SSH 克隆者和 HTTPS 克隆者有效，无需他们各自配置。
3. **保持 scripts 整洁**：假设你还要在 `scripts/` 下新增一个驱动脚本 `runNewLib.tcl`（平铺在 `scripts/` 根下），按 4.3 判断是否需要改 `scripts/.gitignore`；若该脚本运行时还会生成 `new_lib_sim_run.tcl`，又该在 `.gitignore` 哪个位置补一行。
4. **别忘了账本**：参照 [u3-l1](u3-l1-release-and-version-pinning.md)，说明接入后还应在 `Changelog.md` 的对应 release 段手工补一条版本记录——因为登记 submodule 只会写 gitlink，不会自动更新 Changelog。

**参考答案要点**：

1. 配置见 4.1.4；核心是 `path = VHDL/psi_new_lib` + `url = ../../paulscherrerinstitute/psi_new_lib.git`。
2. 相对 URL 不携带协议，从父仓库继承协议与主机，故 SSH/HTTPS 克隆者皆零配置可用（见 4.2.2）。
3. `runNewLib.tcl` 平铺于 `scripts/`、后缀 `.tcl`，被 `!*.tcl` 自动放行，**无需**改 `.gitignore`；但运行产物 `new_lib_sim_run.tcl` 必须在 `scripts/.gitignore` **末尾**补一行 `new_lib_sim_run.tcl`，否则会被误提交（见 4.3.3）。
4. gitlink ≠ Changelog：接入后必须在 `Changelog.md` 对应 release 段、`VHDL` 分组下补一行 `psi_new_lib <版本号>`，否则人类账本与机器指针不一致（见 [u3-l1](u3-l1-release-and-version-pinning.md)）。

## 6. 本讲小结

- 新增 submodule 只有一个模板：`path = <类别>/<仓库名>`，`url = ../../paulscherrerinstitute/<仓库名>.git`；类别目录只能是 `VHDL/TCL/Python/VivadoIp` 四选一。
- 相对 URL（`../../`）以父仓库远程地址的路径部分为基准解析，走到主机根再进 `paulscherrerinstitute` org，最终落到同 org 的目标仓库。
- 提交 `774a090` 把全部子模块 url 从绝对 SSH 改成相对，使子模块自动继承父仓库的克隆协议，SSH 与 HTTPS 用户都零配置可用；README 的 `Cloning` 章节因此能同时给出两种克隆命令。
- 相对 URL 只「协议无关」，并不「org 无关」——org 名仍是路径的一部分，跨 org 迁移需改路径。
- `scripts/.gitignore` 用 `*` + `!*.tcl` + `!.gitignore` 构成白名单，自动跟踪手写驱动脚本、忽略一切运行产物；末行 `psi_sim_run.tcl` 用「后行覆盖前行」把 `.tcl` 结尾的运行产物重新排除。
- 白名单能生效，前提是脚本平铺在 `scripts/` 根下；若改放子目录，需先放行目录再放行文件，否则父目录被 `*` 忽略后无法用 `!` 救回。

## 7. 下一步学习建议

- 继续阅读 [u3-l3（端到端工作流：克隆→仿真→打包）](u3-l3-end-to-end-workflow.md)，把本讲的「克隆协议选择」与 [u2-l2](u2-l2-simulation-psisim-flow.md)、[u2-l4](u2-l4-ip-packaging.md) 的仿真/打包脚本串成一条可执行的完整流程。
- 想巩固「gitlink 与 Changelog 两套事实来源」的认知，可重读 [u3-l1](u3-l1-release-and-version-pinning.md)，并尝试对比两次 release 之间子模块版本的差异。
- 若你真的要维护一个类似的集合仓库，建议进一步阅读 git 官方文档中「relative submodule URL」与 `git-config` 的 `insteadOf` / `pushInsteadOf` 条目，理解相对 URL 的完整解析规则与回退方案。
