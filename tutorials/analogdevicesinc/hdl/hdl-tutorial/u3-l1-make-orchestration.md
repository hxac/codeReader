# GNU Make 的整体编排

## 1. 本讲目标

本讲是「构建系统」单元的第一篇。学完之后，你应该能够：

- 看懂顶层 `Makefile` 是如何**自动发现** `projects/` 下所有参考设计的，并能预测一条形如 `make fmcomms2.zcu102` 的命令最终会进入哪个子目录。
- 理解 `quiet.mk` 提供的 `build` / `clean` / `skip_if_missing` 三个宏分别做什么，以及它们如何让海量工程的构建输出保持干净、并在缺少外部依赖时**优雅跳过**而非崩溃。
- 理解 `project-toplevel.mk` 通过「`wildcard` 发现子目录 + 递归 `make -C`」实现的可复用递归模式，并能把它和 `library/Makefile` 的发现方式做对比。

一句话概括本讲：**这套构建系统的骨架是「自动发现目录 → 生成虚拟目标 → 递归进入子目录执行 make」**，本讲只讲这套骨架，不讲单个工程内部的打包细节（那是 u3-l2 的内容）。

## 2. 前置知识

本讲假设你已学过 u1-l4（知道 `make` 是 ADI HDL 的构建入口、构建最终产物是比特流/`.xsa`）。在此基础上，你需要一点 GNU Make 的函数与自动变量知识。下面是本讲会反复用到的几个，先建立一个速查表：

| 名称 | 类别 | 作用 |
| --- | --- | --- |
| `$(wildcard pattern)` | 函数 | 按通配符展开成**实际存在**的文件/目录列表（不存在的不会报错，返回空） |
| `$(notdir path)` | 函数 | 去掉路径前缀，只留文件名/目录名，如 `projects/fmcomms2` → `fmcomms2` |
| `$(dir path)` | 函数 | 与上面相反，只留目录部分，如 `a/b/Makefile` → `a/b/` |
| `$(filter pat,list)` / `$(filter-out pat,list)` | 函数 | 从列表里**保留**/**剔除**匹配模式的项 |
| `$(subst from,to,text)` | 函数 | 把文本里的 `from` 全部替换成 `to` |
| `$(foreach var,list,body)` | 函数 | 对 `list` 里每个元素，代入 `var` 求值 `body`，拼成结果列表 |
| `$(addsuffix s,list)` | 函数 | 给列表每项追加后缀 |
| `$(lastword list)` | 函数 | 取列表最后一个单词 |
| `$@` | 自动变量 | 当前规则的目标名 |
| `$(@D)` / `$(@F)` | 自动变量 | 目标名的**目录部分** / **文件部分** |
| `make -C dir target` | 命令 | 切换到 `dir` 目录再执行 `target`（递归 make） |
| `MAKEFLAGS` | 变量 | 传递给子 make 的全局选项（如 `--quiet`、`-jN`） |

另外两个术语：

- **递归 make（recursive make）**：父 make 用 `make -C` 调用子目录里的 make。本仓库几乎每一层目录转交都靠它。
- **虚拟目标（phony target）**：用 `.PHONY` 声明的目标名不对应真实文件，因此 make 每次都认为它「需要重建」，总会执行其配方。目录名、`all`、`clean` 都属于这类。

> 术语提示：「虚拟目标」「自动发现」听着抽象，本质上就是：**make 先用 `wildcard` 扫描磁盘上的真实目录，把目录名拼成一批目标名，再为每个目标名写一条「进入该目录执行 make」的规则**。本讲三段源码都是这个套路的不同变体。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 |
| --- | --- |
| [Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile) | **仓库顶层**编排：发现所有工程、定义 `all`/`lib`/`clean`、把 `proj.board` 名字映射到目录 |
| [quiet.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk) | **公共宏库**：颜色、静默模式、`build`/`clean`/`skip_if_missing` 三个宏，被几乎所有 make 文件 `include` |
| [projects/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Makefile) | `projects/` 的入口，只有一行：`include scripts/project-toplevel.mk` |
| [projects/scripts/project-toplevel.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk) | **可复用的递归骨架**：发现本层子目录、生成 `子目录/all|clean|clean-all` 目标并递归；被 `projects/` 和每个评估板目录各 include 一次 |
| [library/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/Makefile) | `library/` 的入口，用 `find -mindepth 2` 发现所有库（含框架内的嵌套库），是递归模式的另一种实现 |

辅助理解（非本讲重点，但实践任务会用到）：

| 文件 | 角色 |
| --- | --- |
| `projects/fmcomms2/Makefile` / `projects/fmcomms2/zcu102/Makefile` | 递归链条的中间层与叶子层样本 |
| `projects/scripts/project-xilinx.mk` | 叶子工程真正建工程的脚本，触发 `quiet.mk` 的跳过逻辑（u3-l2 详讲） |

## 4. 核心概念与源码讲解

### 4.1 顶层 Makefile 的目录自动发现

#### 4.1.1 概念说明

ADI HDL 仓库里有近百个评估板、每个评估板又对应若干载板，组合出上千个可构建目标。如果靠人手维护一份「目标 → 目录」对照表，每加一块板子都要改顶层文件，既易错又难维护。

顶层 `Makefile` 的解法是**自动发现**：它扫描 `projects/` 下真实存在的目录，再根据每个目录的内部结构，自动生成形如 `fmcomms2.zcu102` 的目标名。你只需要敲 `make <自动生成的名字>`，make 就知道该去哪个目录。

这里有一个关键设计选择：目标名用**点**分隔（`fmcomms2.zcu102`），而目录用**斜杠**分隔（`fmcomms2/zcu102`）。顶层 Makefile 用一个 `subst` 把点换成斜杠，就把目标名直接变成了子目录路径——无需维护任何对照表。

#### 4.1.2 核心流程

构建一个具体目标的完整流转：

```text
用户: make fmcomms2.zcu102
   │
   ▼
顶层 Makefile 的 $(SUBPROJECTS) 规则
   规则体: make -C projects/$(subst .,/,$@)
   即      make -C projects/fmcomms2/zcu102        ← 点换斜杠，直接跳到叶子目录
   │
   ▼
projects/fmcomms2/zcu102/Makefile（叶子工程）
   声明 LIB_DEPS / M_DEPS，include project-xilinx.mk，真正跑 Vivado
```

注意：当你指定了具体目标 `fmcomms2.zcu102` 时，**不会**经过 `projects/Makefile` 和 `projects/fmcomms2/Makefile` 的递归，而是被顶层那条 `$(SUBPROJECTS)` 规则「一步直达」叶子目录。递归（4.3 节）只在 `make all` 这种「全部构建」时才发生。

还有两个易踩的坑：

1. **裸 `make` 不构建任何东西**。顶层文件的第一个目标是 `help`，GNU Make 默认执行第一个目标，所以光敲 `make` 只会打印用法提示。要构建必须显式写 `make all` 或 `make <具体目标>`。
2. **`make all` 只构建 projects，不会单独构建所有 library**。顶层有独立的 `lib` 目标用于「把所有 IP 库各构建一遍」；而 `make all` 走 `projects/all`，库是作为工程的依赖（`LIB_DEPS`）被按需构建的（详见 u3-l2）。

#### 4.1.3 源码精读

先看工程名的自动发现。第一步是收集 `projects/` 下的所有名字：

[Makefile:24-28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L28) —— 这两行是本讲最核心的逻辑。

```makefile
PROJECTS := $(filter-out $(NO_PROJ), $(notdir $(wildcard projects/*)))
SUBPROJECTS := $(foreach projname,$(PROJECTS), \
	$(if $(wildcard projects/$(projname)/system_project.tcl), $(projname) , \
	$(foreach archname,$(notdir $(subst /Makefile,,$(wildcard projects/$(projname)/*/Makefile))), \
		$(projname).$(archname))))
```

逐层拆解 `PROJECTS`：

- `$(wildcard projects/*)`：展开 `projects/` 下所有条目（**包括文件和目录**）。
- `$(notdir ...)`：去掉 `projects/` 前缀，得到 `fmcomms2`、`adrv9009`…… 以及 `Makefile`、`Readme.md` 等文件名。
- `$(filter-out $(NO_PROJ), ...)`：剔除 `NO_PROJ` 里列出的名字。`NO_PROJ` 默认未定义（为空），它是留给用户在命令行临时排除某些工程的「后门」，例如 `make NO_PROJ=fmcomms2 all`。文件名条目（如 `Readme.md`）虽然会留在 `PROJECTS` 里，但下一步会自然过滤掉，无害。

`SUBPROJECTS` 的生成用了一个**两层 `foreach` + `if`**，处理两种工程目录形态：

- **形态 A：单板工程**——`system_project.tcl` 直接放在 `projects/<工程名>/` 下（没有载板子目录）。`$(if $(wildcard projects/$(projname)/system_project.tcl), ...)` 命中，目标名就是 `$(projname)` 本身。
- **形态 B：多板工程**（绝大多数，如 `fmcomms2`）——工程目录下没有直接的 `system_project.tcl`，而是每个载板一个子目录（`projects/fmcomms2/zcu102/`、`projects/fmcomms2/vc707/`……），每个子目录里各有 `Makefile`。此时走 `if` 的 else 分支：用 `$(wildcard projects/$(projname)/*/Makefile)` 找出所有载板子目录的 Makefile，`$(subst /Makefile,,...)` 去掉尾部 `/Makefile`、`$(notdir ...)` 取载板名，最后拼成 `$(projname).$(archname)`，即 `fmcomms2.zcu102`、`fmcomms2.vc707` 等。

接下来是把这些名字变成真实目标并绑定递归：

[Makefile:30-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L30-L33) —— 把全部 `SUBPROJECTS` 声明为 phony，并用一条**模式规则**统一处理。

```makefile
.PHONY: lib all clean clean-ipcache clean-all $(SUBPROJECTS)

$(SUBPROJECTS):
	$(MAKE) -C projects/$(subst .,/,$@)
```

精妙之处在 `projects/$(subst .,/,$@)`：

- `$@` 是触发本规则的目标名，如 `fmcomms2.zcu102`。
- `$(subst .,/,$@)` 把点换成斜杠 → `fmcomms2/zcu102`。
- 于是 `make -C projects/fmcomms2/zcu102` 直接进入叶子工程目录执行 make。

一条规则，零维护成本，覆盖所有工程——这就是「目标名即路径」的威力。

最后看顶层的几个聚合目标：

[Makefile:35-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L35-L44) —— `lib` / `all` / `clean` 三个顶层入口。

```makefile
lib:
	$(MAKE) -C library/ all

all:
	$(MAKE) -C projects/ all

clean:
	$(MAKE) -C projects/ clean
```

- `make lib` → 进入 `library/` 构建全部 IP 库（4.3 节会看到它的内部）。
- `make all` → 进入 `projects/` 触发**全量递归构建**（注意不是 `lib`）。
- `make clean` → 进入 `projects/` 递归清理。

> 对照：[Makefile:11-21](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L11-L21) 的 `help` 目标排在 `PROJECTS` 之前，是文件的第一个目标，因此裸 `make` 执行它而非构建。

#### 4.1.4 代码实践

**实践目标**：亲手验证「目标名 → 目录」的映射逻辑，并理解 `subst` 的作用。

**操作步骤**（纯源码阅读型，不需要安装工具链）：

1. 打开顶层 `Makefile` 第 24–28 行，确认 `SUBPROJECTS` 的生成逻辑。
2. 在仓库根目录执行（仅查看 make 的目标列表，**不会**真正构建）：
   ```bash
   make -n fmcomms2.zcu102        # -n = dry-run，只打印将要执行的命令
   make -n adv7511.zed
   ```
3. 再用一个不存在的组合试一试，观察 make 的报错：
   ```bash
   make -n fmcomms2.does_not_exist
   ```

**需要观察的现象**：

- `make -n fmcomms2.zcu102` 应打印类似 `make -C projects/fmcomms2/zcu102` 的命令——点确实被换成了斜杠。
- `make -n fmcomms2.does_not_exist` 应报 `No rule to make target ...`——因为 `SUBPROJECTS` 里没有这个名字。

**预期结果**：dry-run 输出的目录路径与你手动把目标名里的点换成斜杠得到的结果完全一致。若想看 make 实际识别到哪些目标，可在根目录执行 `make -p 2>/dev/null | grep -E '^(fmcomms2|adv7511)\.'`（从 make 的数据库里筛出所有自动生成的目标名）。

#### 4.1.5 小练习与答案

**练习 1**：如果某天新增了一个评估板工程 `projects/myadc/`，并在其下放了 `zcu102/Makefile` 和 `zed/Makefile`，顶层 `Makefile` 需要改吗？会自动生成哪些新目标？

**答案**：不需要改顶层 `Makefile`。`$(wildcard projects/myadc/*/Makefile)` 会自动发现这两个子目录，生成 `myadc.zcu102` 和 `myadc.zed` 两个新目标。这正是自动发现带来的零维护好处。

**练习 2**：为什么 `$(SUBPROJECTS)` 这条规则里，递归用的是 `projects/$(subst .,/,$@)` 而不是直接写死某个目录？

**答案**：因为这一条规则要覆盖**所有**自动发现的目标。`$@` 是当前被触发的那个目标名，`subst` 把它动态转换成对应目录，于是「一条规则服务上千个目标」，无需为每个工程单独写规则。

---

### 4.2 quiet.mk 的构建宏与日志

#### 4.2.1 概念说明

假设 `make all` 真的去构建几十上百个工程，每个工程又调用一次 Vivado，终端会被数万行工具输出淹没，根本看不出哪个成功了、哪个失败了。`quiet.mk` 就是为解决这个问题而生的**公共宏库**。

它提供三个核心宏：

- `build`：执行一条构建命令，把**完整输出重定向到日志文件**，终端只显示一行「Building ... → OK / FAILED」的状态。
- `clean`：删除一批文件，附一句人话描述。
- `skip_if_missing`：在执行真正的构建前，先检查「外部依赖是否齐全」；不齐全就打印 `SKIPPED` 并跳过，而不是让整个 `make all` 失败。

`quiet.mk` 不是某个目录的私有文件，而是被顶层、`projects/`、`library/`、各工程脚本**反复 include** 的公共件（4.3 节会看到 include 的路径技巧）。

#### 4.2.2 核心流程

`build` 宏的执行流程（非 VERBOSE 模式）：

```text
echo -n "Building <描述> [<日志路径>] ..."   ← 先打一行状态（串行模式不带换行）
<真实命令> >> <日志文件> 2>&1               ← 命令输出全部进日志，终端看不到
捕获退出码 ERR
  ERR == 0 → echo "OK"                       ← 成功
  ERR != 0 → echo "FAILED" + 提示看日志      ← 失败
exit $ERR                                    ← 把退出码透传给 make，触发 make 的错误处理
```

`skip_if_missing` 宏的执行流程：

```text
检查当前目录是否存在 missing_external.log
  存在  → echo "<类型> <名字> SKIPPED ..." + 执行「跳过分支」(通常是 no-op 的 true)
  不存在→ 执行「真正构建分支」
```

那么 `missing_external.log` 谁来产生？答案在叶子工程脚本里（本讲只点出机制，细节留 u3-l2）：工程若声明了 `EXTERNAL_DEPS`（指向仓库外的依赖文件，例如某个相邻仓库的 Tcl），构建前会逐个检查这些路径是否存在，**任何一个不存在**就把它的名字追加进 `missing_external.log`。于是 `skip_if_missing` 据此决定是构建还是跳过。

#### 4.2.3 源码精读

**先看颜色与静默开关**：

[quiet.mk:6-20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L6-L20) —— 颜色随终端有无自动开关，默认进入静默模式。

```makefile
ifdef MAKE_TERMOUT
  ESC:=$(shell printf '\033')
  GREEN:=$(ESC)[1;32m
  RED:=$(ESC)[1;31m
  HL:=$(ESC)[0;33m
  NC:=$(ESC)[0m
else
  GREEN:=  RED:=  HL:=  NC:=        # 重定向到文件时不带颜色码
endif

ifneq ($(VERBOSE),1)
  MAKEFLAGS += --quiet
```

要点：

- `MAKE_TERMOUT` 是 GNU Make 在输出真正发往终端时才定义的内置变量。用它判断是否上色，可以保证日志文件里不会混入 ANSI 转义码。
- `ifneq ($(VERBOSE),1)`：只要没设 `VERBOSE=1`，就给 `MAKEFLAGS` 追加 `--quiet`，并启用下面那套「只打状态行」的宏；反之 `VERBOSE=1` 会走简化版宏（4.2.3 末尾）。

**`skip_if_missing` 宏**：

[quiet.mk:28-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L28-L36) —— 缺依赖即优雅跳过的核心。

```makefile
define skip_if_missing
	if [ -f missing_external.log ]; then \
		echo "$(1) $(HL)$(strip $(2)) SKIPPED$(NC)" due to missing external dependencies; \
		echo "For the list of expected files see $(HL)$(CURDIR)/missing_external.log$(NC)"; \
		($(3)) ; \
	else \
		($(4)) ; \
	fi
endef
```

四个参数：`$(1)` 类型（如 `Project`）、`$(2)` 名字（如工程名）、`$(3)` **跳过时**执行的命令、`$(4)` **不跳过时**（即正常）执行的命令。`$(CURDIR)` 是 make 内置变量，表示当前 make 的工作目录。

**`build` 宏**：

[quiet.mk:42-52](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L42-L52) —— 把冗长输出收进日志，只回报一行状态。

```makefile
define build
	(echo $(if $(filter -j%,$(MAKEFLAGS)),,-n) "Building $(strip $(3)) [$(HL)$(CURDIR)/$(strip $(2))$(NC)] ..." ; \
	$(strip $(1)) >> $(strip $(2)) 2>&1 ; \
	(ERR=$$?; if [ $$ERR = 0 ]; then \
		echo "$(if $(filter -j%,$(MAKEFLAGS)),Build $(strip $(3)) [...]) $(GREEN)OK$(NC)"; \
	else \
		echo "... $(RED)FAILED$(NC)"; echo "For details see ..."; echo ""; \
	fi ; exit $$ERR))
endef
```

参数：`$(1)` 真实命令、`$(2)` 日志文件名、`$(3)` 人话描述。两个值得注意的细节：

- `>> $(2) 2>&1`：标准输出和标准错误都追加进日志，终端只见一行状态。
- `$(if $(filter -j%,$(MAKEFLAGS)),,-n)`：当 make **没有**并行（不含 `-j`）时，给 `echo` 加 `-n`（不换行），让「Building ...」和随后的「OK/FAILED」紧凑地排在同一相关输出里；并行模式下不加 `-n`，避免多个并发任务的状态行相互串行粘连。

**`clean` 宏**：

[quiet.mk:57-60](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L57-L60) —— 删除文件并打一句描述。

```makefile
define clean
	@echo "Cleaning $(strip $(2)) ..."
	-rm -rf $(strip $(1))
endef
```

`$(1)` 是要删的文件列表、`$(2)` 是描述。前导 `-` 表示「即使 rm 出错也别中断 make」。

**这套宏如何被实际调用**——看叶子工程脚本里 `skip_if_missing` 与 `build` 的合用：

[projects/scripts/project-xilinx.mk:128-136](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L128-L136) —— 缺依赖就 `true`（跳过），否则才真正构建。

```makefile
$(call skip_if_missing, \
	Project, \
	$(PROJECT_NAME), \
	true, \                                   ← 跳过分支：仅 true，什么也不做且返回成功
	rm -rf $(CLEAN_TARGET) ; \
	$(call build, \                           ← 正常分支：跑 Vivado，输出进日志
		$(VIVADO) system_project.tcl, \
		$(PROJECT_NAME)_vivado.log, \
		$(HL)$(PROJECT_NAME)$(NC) project))
```

而 `missing_external.log` 的产生在同一文件里：

[projects/scripts/project-xilinx.mk:104-112](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L104-L112) —— 构建前先检查外部依赖、记录缺失项。

```makefile
external_dependencies: external_dependencies_cleanup $(EXTERNAL_DEPS)

external_dependencies_cleanup:
	rm -f missing_external.log

$(EXTERNAL_DEPS):
	if [ ! -d $@ ]; then \
		echo $@ >> missing_external.log ; \
	fi
```

由于 `all` 目标写作 `all: external_dependencies <xsa>`（见 [project-xilinx.mk:87](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L87)），所以执行顺序固定是：先清空旧日志 → 逐个检查 `EXTERNAL_DEPS` 并补写缺失项 → 最后才执行 xsa 配方里的 `skip_if_missing`。`EXTERNAL_DEPS` 由个别工程自行声明（例如 `projects/adrv9009zu11eg/adrv2crr_fmc/Makefile` 声明了对相邻 Corundum 仓库若干 Tcl 的依赖）；大多数工程不声明，日志永远不会出现，于是永远走「正常构建」分支。

> 旁支：当设了 `VERBOSE=1` 时，[quiet.mk:62-68](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L62-L68) 把 `build`/`clean` 退化成「直接执行命令、输出仍进日志」的极简版，方便调试时还原原始行为。

#### 4.2.4 代码实践

**实践目标**：直观感受 `build` 宏「终端干净、细节进日志」的效果。

**操作步骤**：

1. 阅读上面引用的 `build` 宏，确认它把命令输出重定向到了第二个参数指定的文件。
2. （可选，需已构建过任一工程）在某个工程构建产物目录里找到形如 `<工程名>_vivado.log` 的日志文件，用 `tail` 查看末尾，确认 Vivado 的真实输出确实落在这里，而终端当时只显示了一行 `OK`/`FAILED`。
3. 想象 `make -j4 all` 同时构建多个工程：根据 `build` 宏里 `$(filter -j%,$(MAKEFLAGS))` 的判断，思考此时状态行带不带换行，以及为什么。

**需要观察的现象**：日志文件体积远大于终端看到的一行状态；终端每个工程只有「Building … → OK/FAILED」一行。

**预期结果 / 待本地验证**：若手头没有可构建环境，这一步标注为「待本地验证」——重点是理解「输出被收进日志、终端只留一行」这一设计意图，而不是真的跑出结果。

#### 4.2.5 小练习与答案

**练习 1**：`build` 宏最后为什么要 `exit $$ERR`？如果去掉会怎样？

**答案**：`exit $$ERR` 把真实命令的退出码透传给 make。若去掉，shell 默认以最后一条 `echo` 的退出码（成功，0）结束，make 会**误以为构建成功**而继续后续目标——失败的工程被悄悄掩盖。这是必须保留的。

**练习 2**：某工程依赖一个你本地没有的外部文件，`make <该工程>` 会直接报错中断吗？

**答案**：不会。流程是：`EXTERNAL_DEPS` 检查发现该文件不存在 → 写入 `missing_external.log` → `skip_if_missing` 命中 → 打印 `Project <名字> SKIPPED due to missing external dependencies` 并执行跳过分支 `true`（返回成功）。整个 `make` 不中断，这正是「优雅跳过」。

---

### 4.3 project-toplevel.mk 的递归子目录模式

#### 4.3.1 概念说明

4.1 节解决了「构建某一个指定工程」，那 `make all`（构建**全部**）怎么遍历？答案是**递归 make**：每一层目录都扫描自己的子目录，对每个子目录再调用一次 make，层层下传。

`project-toplevel.mk` 就是这套递归的**可复用骨架**。它的精妙之处在于同一份文件被复用了**两次**：

- `projects/Makefile` include 它一次 → 用来发现所有评估板（`fmcomms2/`、`adrv9009/`……）。
- 每个 `projects/<评估板>/Makefile` 再 include 它一次 → 用来发现该评估板下的所有载板（`zcu102/`、`vc707/`……）。

于是两级目录用的是**完全相同**的发现与递归逻辑，写一次、用两层。

作为对比，`library/Makefile` 走了另一条路：它**没有**复用 `project-toplevel.mk`，而是自己用 `find -mindepth 2` 发现所有库。原因是库目录里有「框架内嵌套库」的结构（如 `library/jesd204/axi_jesd204_rx/`、`library/util_pack/util_cpack2/`），需要按更深一层来发现，且库只有一级递归需求，所以单独写更直接。

#### 4.3.2 核心流程

`make all` 的全量递归（以 `projects/` 为例）：

```text
顶层: make all
  └─ make -C projects/ all
       └─ projects/Makefile include project-toplevel.mk
            SUBDIRS = projects/ 下所有「含 Makefile 的子目录」= [fmcomms2/, adrv9009/, ...]
            目标 fmcomms2/all:
            └─ make -C projects/fmcomms2/ all
                 └─ projects/fmcomms2/Makefile 同样 include project-toplevel.mk
                      SUBDIRS = fmcomms2/ 下所有子目录 = [zcu102/, vc707/, ...]
                      目标 zcu102/all:
                      └─ make -C projects/fmcomms2/zcu102/ all
                           └─ 叶子 Makefile: include project-xilinx.mk，真正跑 Vivado
```

可见 `project-toplevel.mk` 在第一层和第二层各执行一次，每次都「发现自己当前目录的子目录 → 递归」。叶子层（`zcu102/`）不再 include 它，而是切到 `project-xilinx.mk` 真正建工程。

#### 4.3.3 源码精读

**先看 projects/ 的入口**：

[projects/Makefile:7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Makefile#L7) —— 整个文件只有一行有效内容。

```makefile
include scripts/project-toplevel.mk
```

[projects/fmcomms2/Makefile:7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/Makefile#L7) —— 第二层入口几乎一样，只是路径不同。

```makefile
include ../scripts/project-toplevel.mk
```

两份「自动生成」文件各自只 include 一次，差别仅在 include 路径（一个 `scripts/...`、一个 `../scripts/...`）。**同一份 `project-toplevel.mk` 服务两层**。

**再看骨架本身**——它如何「定位自己」以便 include 到正确层级的 `quiet.mk`：

[projects/scripts/project-toplevel.mk:6-11](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk#L6-L11) —— 自定位 + 子目录发现。

```makefile
# Assumes this file is in projects/scripts/project-toplevel.mk
HDL_PROJECT_PATH := $(subst scripts/project-toplevel.mk,,$(lastword $(MAKEFILE_LIST)))

include $(HDL_PROJECT_PATH)../quiet.mk

SUBDIRS := $(dir $(wildcard */Makefile))
```

这里的「自定位」技巧值得细看：

- `$(MAKEFILE_LIST)` 是 make 内置变量，记录到目前为止被读入的所有 make 文件；`$(lastword ...)` 取最后一个，也就是**当前正在被 include 的本文件**的路径。
- 被 `projects/Makefile` include 时，该路径是 `scripts/project-toplevel.mk`，`subst` 把文件名部分删掉，`HDL_PROJECT_PATH` 得空串，于是 `include ../quiet.mk`（相对 `projects/`，指向仓库根的 `quiet.mk`）。
- 被 `projects/fmcomms2/Makefile` include 时，路径是 `../scripts/project-toplevel.mk`，`subst` 删掉文件名后剩下 `../`，于是 `include ../../quiet.mk`（相对 `projects/fmcomms2/`，仍指向仓库根的 `quiet.mk`）。

这样无论从哪一层 include，都能准确找到仓库根的 `quiet.mk`，让骨架具备**可重用性**。

- `SUBDIRS := $(dir $(wildcard */Makefile))`：在**当前目录**下找所有「直接子目录里的 Makefile」，`$(dir ...)` 取目录部分，得到 `fmcomms2/`、`adrv9009/` 这样的列表（带尾斜杠）。注意 `*/` 只匹配一层，正好对应「只发现直接子目录」。

**生成虚拟目标并递归**：

[projects/scripts/project-toplevel.mk:13-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk#L13-L25) —— 这是递归的核心。

```makefile
SUBDIRS_ALL := $(addsuffix all,$(SUBDIRS))
SUBDIRS_CLEAN := $(addsuffix clean,$(SUBDIRS))
SUBDIRS_CLEANALL := $(addsuffix clean-all,$(SUBDIRS))

.PHONY: all clean clean-all $(SUBDIRS_ALL) $(SUBDIRS_CLEAN) $(SUBDIRS_CLEANALL)

all: $(SUBDIRS_ALL)
clean: $(SUBDIRS_CLEAN)
clean-all: $(SUBDIRS_CLEANALL)

$(SUBDIRS_ALL) $(SUBDIRS_CLEAN) $(SUBDIRS_CLEANALL):
	$(MAKE) -C $(@D) $(@F)
```

要点：

- `$(addsuffix all,$(SUBDIRS))` 给每个子目录追加 `all`，得到 `fmcomms2/all`、`adrv9009/all` 这样的虚拟目标名。`clean`、`clean-all` 同理。
- `all: $(SUBDIRS_ALL)`：本层的 `all` 依赖所有「子目录/all」目标。
- 最后一条模式规则用 `$(@D)` / `$(@F)` 拆解目标名：例如目标是 `fmcomms2/all` 时，`$(@D)=fmcomms2`、`$(@F)=all`，于是执行 `make -C fmcomms2 all`——**进入子目录、执行同名目标**。这就是「目标名编码了目录与动作」的递归手法。

`projects/` 层的 `make -C fmcomms2 all` 会触发 `projects/fmcomms2/Makefile`（它又 include 了同一份骨架），于是 `fmcomms2/all` 进一步展开成 `zcu102/all`、`vc707/all`……直到叶子层切换到 `project-xilinx.mk`。两级递归由此贯通。

**对比：library/Makefile 的另一种发现方式**：

[library/Makefile:11-29](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/Makefile#L11-L29) —— 库侧自成一体的发现与递归。

```makefile
.PHONY: all lib clean clean-all
all: lib

_LIBS := $(dir $(shell find . -mindepth 2 -name Makefile | sort))

_LIBS_ALL := $(addsuffix all, $(_LIBS))
_LIBS_CLEAN := $(addsuffix clean, $(_LIBS))

$(_LIBS_ALL):
	$(MAKE) -C $(@D) $(@F)
$(_LIBS_CLEAN):
	$(MAKE) -C $(@D) $(@F)

clean: $(_LIBS_CLEAN)
lib: $(_LIBS_ALL)
```

对比要点：

- 发现方式不同：`find . -mindepth 2 -name Makefile` 一次性找出**所有**库（含 `jesd204/axi_jesd204_rx/`、`util_pack/util_cpack2/` 这类嵌套两层的库），`-mindepth 2` 保证跳过 `library/Makefile` 自身、只取子库。库只有一层递归需求，所以不借用 `project-toplevel.mk`。
- 递归手法相同：仍是 `$(@D)`/`$(@F)` + `make -C`，目标名仍是「目录 + 动作」。可见这是整个仓库统一遵循的递归范式。
- 入口不同：`all: lib`，而 `lib: $(_LIBS_ALL)`——所以 `make -C library all` 与 `make -C library lib` 都会构建所有库。

> 小结：`projects/` 用「同一骨架 include 两次」实现两级递归；`library/` 用「一次 `find -mindepth 2`」实现单级发现。两者递归配方一致，区别只在发现策略——这是根据各自目录形态（均匀两层 vs 含嵌套框架）做出的合理取舍。

#### 4.3.4 代码实践

**实践目标**：用 dry-run 亲眼看到两级递归的展开。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   make -n -C projects fmcomms2/all          # 看第一层如何进入 fmcomms2/
   ```
2. 对照输出，确认它执行了 `make -C fmcomms2 all`（由 `$(@D)`/`$(@F)` 拼出）。
3. 再执行：
   ```bash
   make -n -C projects/fmcomms2 all          # 看第二层如何进入各载板
   ```
4. 对比 `library/` 的发现：执行下面命令，观察列出的库目录（应包含嵌套库）：
   ```bash
   find library -mindepth 2 -name Makefile | sort | head
   ```

**需要观察的现象**：`projects/` 侧的 dry-run 呈现「进入评估板 → 进入载板」的链式 `make -C`；`library/` 侧的 `find` 列出形如 `library/jesd204/axi_jesd204_rx/Makefile`、`library/util_pack/util_cpack2/Makefile` 的嵌套路径。

**预期结果**：两级递归的目标名均为「目录 + 动作」、配方均为 `make -C $(@D) $(@F)`，与源码完全吻合。`find` 结果验证了库侧需要 `-mindepth 2` 才能覆盖嵌套库。

#### 4.3.5 小练习与答案

**练习 1**：`project-toplevel.mk` 里 `SUBDIRS := $(dir $(wildcard */Makefile))` 用的是 `*/Makefile`（一层）。如果改成 `**/Makefile`（多层）会带来什么问题？

**答案**：会把更深层（如叶子工程的）Makefile 也算进当前层的子目录列表，导致目标名重复、递归层次混乱（比如直接从 `projects/` 跳过评估板层去 `projects/fmcomms2/zcu102`）。原设计刻意只用一层通配，保证「每层只负责自己的直接子目录」，分层清晰。

**练习 2**：为什么 `projects/` 能复用同一份 `project-toplevel.mk` 两次，而 `library/` 却单独写发现逻辑？

**答案**：`projects/` 的目录形态是均匀的两层（评估板 → 载板），两层发现逻辑完全相同，所以「include 同一骨架」最省事；`library/` 含框架内嵌套库（深度不一），且只需单级递归，用 `find -mindepth 2` 一次取全更直接、更符合其结构。两者都是按自身目录形态做的取舍。

---

## 5. 综合实践

把本讲三段知识串起来，完成下面这个**追踪型实践**（无需工具链，纯阅读 + dry-run）。

**任务**：预测 `make fmcomms2.zcu102` 的完整目录流转，并解释全量构建时缺依赖工程为何不会拖垮整体。

**步骤**：

1. **定位 SUBPROJECTS 生成逻辑**。打开顶层 [Makefile:24-28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L28)。回答：
   - `fmcomms2` 是怎么进入 `PROJECTS` 的？（提示：`wildcard` + `notdir`）
   - `projects/fmcomms2/system_project.tcl` 是否存在？因此 `SUBPROJECTS` 走 `if` 的哪个分支？（提示：实际 `system_project.tcl` 在 `projects/fmcomms2/zcu102/` 下，所以走 else 分支）
   - 最终生成的目标名里，`fmcomms2.zcu102` 是如何被拼出来的？
2. **追踪递归入口**。看 [Makefile:32-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L32-L33) 的 `$(SUBPROJECTS)` 规则。写出 `make fmcomms2.zcu102` 实际执行的命令（应得到 `make -C projects/fmcomms2/zcu102`）。用 `make -n fmcomms2.zcu102` 验证。**关键结论**：指定具体目标时**一步直达叶子**，不经 `projects/Makefile` 与 `projects/fmcomms2/Makefile` 的递归。
3. **对比全量构建**。写出 `make all` 的两级递归链（顶层 → `projects/all` → 各评估板 `all` → 各载板 `all` → 叶子 `project-xilinx.mk`），引用 [project-toplevel.mk:20-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk#L20-L25) 说明 `$(@D)`/`$(@F)` 的作用。
4. **解释优雅跳过**。引用 [quiet.mk:28-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L28-L36) 与 [project-xilinx.mk:104-112](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L104-L112) 说明：当某工程（如 `adrv9009zu11eg/adrv2crr_fmc`）声明了本地不存在的 `EXTERNAL_DEPS` 时，构建链会如何把它标记为 `SKIPPED` 而非失败，从而让 `make all` 继续推进其余工程。

**交付物**：一张「`make fmcomms2.zcu102` 的目录流转图」+ 一段「缺依赖为何不影响整体」的文字说明。

**预期结果 / 待本地验证**：步骤 1–3 可在纯源码 + `make -n` 下完全确定；步骤 4 的真实 `SKIPPED` 输出需本地具备声明了 `EXTERNAL_DEPS` 的工程并刻意缺失依赖才能复现，否则标注为「待本地验证」。

## 6. 本讲小结

- 顶层 `Makefile` 用 `wildcard`/`notdir`/`foreach` 自动发现 `projects/` 下所有工程，生成 `proj.board` 形态的目标名，再用 `$(subst .,/,$@)` 把目标名直接变成子目录路径，实现「零维护、一条规则服务所有工程」。
- 裸 `make` 只打印 `help`；`make all` 走 `projects/all` 全量递归，`make lib` 才单独构建全部 IP 库；指定具体目标则一步直达叶子目录。
- `quiet.mk` 是全仓公共宏库，提供 `build`（输出进日志、终端只留一行 OK/FAILED）、`clean`、`skip_if_missing`（缺外部依赖则 `SKIPPED` 而非报错）三个宏，并按 `MAKE_TERMOUT`/`VERBOSE` 自动切换颜色与详略。
- `skip_if_missing` 依赖叶子脚本生成的 `missing_external.log`：工程声明的 `EXTERNAL_DEPS` 若有缺失会被记录，从而触发优雅跳过，保证 `make all` 不被个别缺依赖工程拖垮。
- `project-toplevel.mk` 是可复用的递归骨架：用 `$(dir $(wildcard */Makefile))` 发现直接子目录，用 `$(@D)`/`$(@F)` + `make -C` 展开两级递归；同一份文件被 `projects/` 和每个评估板目录各 include 一次。
- `library/Makefile` 走另一条路：用 `find -mindepth 2 -name Makefile` 一次性发现所有库（含嵌套框架库），递归配方与 projects 侧一致，发现策略按库的目录形态定制。

## 7. 下一步学习建议

本讲只讲了构建骨架——「怎么找到目录、怎么递归、怎么打日志、怎么跳过」。当一个叶子工程真正开始构建时，它如何把 `LIB_DEPS` 翻译成 IP 打包目标、如何用 `flock` 保证并行打包安全、如何处理 `CFG` 参数化与增量编译？这些是下一讲 **u3-l2「工程构建 Makefile 内部：project-xilinx.mk」** 的主题。建议你带着本讲得到的「顶层 → 叶子」目录流转印象，直接打开 `projects/scripts/project-xilinx.mk`，重点看 `component.xml` 目标、`all`/`lib` 目标、以及 `skip_if_missing` 的真实调用点，把它们与本讲的 `quiet.mk` 宏对应起来。之后再进入 u3-l3（Tcl 工程助手）与 u3-l4（板级连线助手），从 Make 层过渡到 Tcl 层。
