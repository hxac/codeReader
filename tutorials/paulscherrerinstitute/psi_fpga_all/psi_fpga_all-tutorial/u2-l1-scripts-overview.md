# scripts 目录与驱动脚本总览

> 本讲是第二单元「脚本驱动的仿真、编译与 IP 打包」的第一讲。
> 上一单元（u1）我们建立了「psi_fpga_all 是一个 collection-repo，目录结构即接口」的认知。
> 本讲我们把镜头对准本仓库**唯一真正可读的代码**——`scripts/` 目录下的 4 个 TCL 驱动脚本，先看它们的**共同骨架**，不深入任何单一脚本的业务细节（那是 u2-l2/u2-l3/u2-l4 的事）。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 一眼看出 `runModelsim.tcl`、`runGhdl.tcl`、`runVivado.tcl`、`packageAllIp.tcl` 四个脚本**共同的结构模板**：版权头 → `set myPath [pwd]` → 遍历子模块 `cd` + `source` → `cd $myPath` 回到起点。
2. 看懂 [`scripts/.gitignore`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore) 使用的「全忽略除 `*.tcl`」白名单策略，并解释它为什么这样写。
3. 清楚地区分两类脚本：**驱动脚本**（本仓库里的这 4 个，只负责「编排/指挥」）与它们 `source` 进来的**子模块内部脚本**（`config.tcl` / `package.tcl` / `PsiSim.tcl`，都住在各 submodule 里，本仓库不直接包含）。

---

## 2. 前置知识

在进入源码之前，先建立三个直觉。如果你已经学过 u1，下面只是快速回顾。

### 2.1 什么是「驱动脚本」

一个**驱动脚本（driver script）**自己几乎不做具体活儿，它只负责「按顺序把别人喊出来干活」。打个比方：它像是一个**导演**，自己不上台演戏，而是依次走到每个剧组的化妆间（`cd`），把对应的演员（`source` 内部脚本）叫上台。真正「编译、仿真、打包」的逻辑，都在那些被叫上台的演员身上。

### 2.2 TCL 里 `source` 和「当前工作目录」的关系（关键）

这是理解本讲所有脚本的一把钥匙，务必先想明白：

- TCL 的 `source 文件名` 会读取该文件，并**在当前解释器里执行**它的内容。
- 被执行的文件内部如果也用了**相对路径**（例如 `source ../xxx.vhd`、打开 `./tb/xxx.vhd`），这些相对路径是相对于**进程的当前工作目录（pwd）**解析的，**不是**相对于被 `source` 的文件所在目录。
- TCL 的 `cd 路径` 会改变这个「当前工作目录」。

> 结论：驱动脚本在 `source` 某个子模块的内部脚本之前，必须先 `cd` 到该子模块约定的工作目录，子模块内部那些相对路径才能正确解析。这就是为什么 4 个脚本都长着「`cd` 进去 → `source` → （下一轮再 `cd`）」的样子。

> 关于子模块内部脚本（`config.tcl`、`package.tcl`、`PsiSim.tcl`）的真实内容：它们位于对应 submodule 内部，本 collection-repo 当前并未直接包含，因此本讲对它们的具体内容标注「**待确认**」，只讲驱动脚本这一层能确定的「编排约定」。

### 2.3 「全忽略除白名单」的 gitignore 写法

`.gitignore` 里先写 `*`（忽略一切），再用 `!模式` 把想保留的东西**逐条放行**回来，这种写法叫**白名单策略（allowlist）**。它是应对「会生成大量未知临时产物」场景的标准做法，本讲 4.3 节会结合实际文件细讲。

---

## 3. 本讲源码地图

`scripts/` 目录下一共只有 5 个被 git 跟踪的文件（已用 `git ls-files scripts/` 确认，没有别的）：

| 文件 | 行数 | 角色 | 本讲关注点 |
|---|---|---|---|
| [`scripts/runModelsim.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl) | 65 | 仿真驱动（ModelSim） | 共同骨架的代表 |
| [`scripts/runGhdl.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl) | 67 | 仿真驱动（GHDL） | 与上者对比 |
| [`scripts/runVivado.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl) | 72 | 仿真驱动（Vivado） | 与上两者对比 |
| [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) | 38 | IP 打包驱动 | 最「干净」的骨架样例 |
| [`scripts/.gitignore`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore) | 7 | 目录级忽略策略 | 白名单策略 |

> 提醒：每个驱动脚本里被 `source` 的 `config.tcl`、`package.tcl` 以及 `../TCL/PsiSim/PsiSim.tcl`，**都不在本仓库**——它们各自住在对应的 submodule 里。本仓库的角色只是「把这些脚本按固定相对路径串起来」。

---

## 4. 核心概念与源码讲解

本讲把 4 个脚本抽取出 **3 个最小模块**来讲：① 版权头与 `set myPath [pwd]`；② `cd` 进子模块后 `source` 内部脚本的编排模式；③ `.gitignore` 的白名单规则。

### 4.1 版权头与 set myPath [pwd]

#### 4.1.1 概念说明

每个脚本的开头都有一段**统一的版权头**，声明版权归属（Paul Scherrer Institute）、年份与作者。版权头之后，脚本做的第一件「有状态」的事，是**把当前工作目录存进一个变量**：

```tcl
set myPath [pwd]
```

`[pwd]` 返回进程当前的绝对路径，`set myPath ...` 把它存进变量 `myPath`。为什么要存？因为接下来的脚本会**反复 `cd` 进出于各个子模块目录**，如果不记下起点，最后就回不来了，也无法再用「相对起点」的路径去访问下一个子模块。

> 直觉比喻：进游乐园前先在入口拍一张照记下位置（`set myPath [pwd]`），这样不管你逛到哪个角落（`cd`），随时能凭照片走回入口（`cd $myPath`）。

#### 4.1.2 核心流程

四个脚本在「保存起点」这件事上**完全一致**，但**位置**略有不同：

- **三个仿真脚本**（`runModelsim/runGhdl/runVivado`）：先 `source` 进 PsiSim 框架并 `init` 初始化仿真，**然后**才 `set myPath [pwd]`。
- **打包脚本**（`packageAllIp`）：不需要加载任何框架，**脚本一开头**就是 `set myPath [pwd]`。

抽象出的最小模板：

```
# ── 1. 版权头 ──
# (可选) 加载框架 / 初始化
set myPath [pwd]            # 记下起点
```

#### 4.1.3 源码精读

先看仿真脚本一族的版权头（三个仿真脚本**逐字相同**，年份都是 2018、作者都是 Oliver Bruendler）。以 `runModelsim.tcl` 为例：

版权头：[scripts/runModelsim.tcl:1-5](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L1-L5) —— 这 5 行是 PSI 标准 copyright 块，标注 2018 年与作者 Oliver Bruendler。

加载框架与初始化（仿真脚本特有）：[scripts/runModelsim.tcl:7-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L7-L12) —— `source` 进 PsiSim 框架、`namespace import` 导入命令、再 `init` 初始化仿真。（`init` 后面可带 `-ghdl` / `-vivado` 参数，具体差异留到 u2-l2 讲。）

记录起点：[scripts/runModelsim.tcl:14-15](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L14-L15) —— 注释 `#Configure` 之后，用 `set myPath [pwd]` 把当前目录（即 `scripts/`）存下来。

再看打包脚本，它的版权头**年份是 2019**（比仿真脚本晚一年，说明它是后来才加入的）：

版权头：[scripts/packageAllIp.tcl:1-5](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L1-L5) —— 注意这里是 `Copyright (c) 2019`，而三个仿真脚本都是 `2018`。

记录起点：[scripts/packageAllIp.tcl:7-8](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L7-L8) —— 注释 `#Setup` 之后直接 `set myPath [pwd]`，没有任何前置的框架加载。

> 小结：四个脚本都「在动手前先 `set myPath [pwd]`」，区别只在于仿真脚本前面多了「加载 PsiSim + `init`」两步。

#### 4.1.4 代码实践

**目标**：亲手验证「`[pwd]` 记录的是绝对路径，`cd` 之后靠它回得来」。

**操作步骤**（需要本机有 `tclsh`，没有则改为「源码阅读型实践」见下）：

1. 在任意目录创建一个临时文件 `demo.tcl`（**示例代码**，不是项目原有文件）：

   ```tcl
   set myPath [pwd]
   puts "起点: $myPath"
   cd /tmp
   puts "cd 后: [pwd]"
   cd $myPath
   puts "回到: [pwd]"
   ```

2. 运行 `tclsh demo.tcl`。

**需要观察的现象**：第一行与第三行打印的路径完全相同，且都是绝对路径；中间一行是 `/tmp`。

**预期结果**：三行输出证明「记下起点 → 乱跑 → 回到起点」成立。

**源码阅读型替代实践**（无需 tclsh）：打开 [`scripts/runModelsim.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl) 与 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，分别找到 `set myPath [pwd]` 所在行，在两个文件里它的「上一行注释」分别是什么？这印证了 4.1.2 中「位置略有不同」的说法。

#### 4.1.5 小练习与答案

**练习 1**：为什么四个脚本都要存 `myPath`，而不是每次需要时重新 `[pwd]`？
**答案**：因为脚本中途会 `cd` 离开起点；离开之后再 `[pwd]` 得到的是「当前所在子模块目录」，而不是最初的 `scripts/` 起点。只有提前存下起点，才能在任意时刻「回到原点」、并用 `$myPath/../...` 这种「相对起点」的写法去访问下一个子模块。

**练习 2**：`set myPath [pwd]` 存的是绝对路径还是相对路径？这对脚本健壮性有什么好处？
**答案**：`[pwd]` 返回的是**绝对路径**。好处是：之后无论 `cd` 到哪里，`cd $myPath` 都能精确回到起点，不依赖当前所在位置，避免相对路径叠加出错。

---

### 4.2 cd 到各子模块后 source 内部脚本的模式

#### 4.2.1 概念说明

这是四个脚本**最核心、也最相似**的部分。它们都按下面的套路遍历一批子模块：

> 对每一个要处理的子模块：先 `cd` 进它的某个约定子目录（仿真场景是 `<lib>/sim`，打包场景是 `<lib>/scripts`），再 `source` 该子模块自带的内部脚本（仿真场景是 `config.tcl`，打包场景是 `package.tcl`）。

这里必须强调一个本讲的核心学习目标——**区分两类脚本**：

| 类别 | 位置 | 职责 | 谁写、谁维护 |
|---|---|---|---|
| **驱动脚本** | 本仓库 `scripts/*.tcl` | 「编排」：决定按什么顺序、访问哪些子模块 | 本仓库（PSI 集合仓库维护者） |
| **内部脚本**（`config.tcl` / `package.tcl` / `PsiSim.tcl`） | 各 submodule 内部 | 「干活」：声明要编译哪些文件、跑哪些 TB、怎么打包 IP | 各子模块自己的维护者 |

> 换句话说：本仓库里的 4 个 `run*.tcl` / `packageAllIp.tcl` **本身不含任何 VHDL 编译规则或 IP 打包逻辑**。它们只是「调度员」。具体的仿真/打包知识，全部封装在它们 `source` 进来的内部脚本里（内容「待确认」，因为不在本仓库）。

#### 4.2.2 核心流程

把四个脚本「遍历子模块」的部分抽象成伪代码：

```
# myPath 指向 scripts/
foreach 子模块 in [要处理的子模块列表] {
    cd $myPath/../<大类>/<子模块名>/<约定子目录>   # 例：../VHDL/psi_common/sim
    source <内部脚本>                              # 例：config.tcl 或 package.tcl
}
cd $myPath    # 全部处理完，回到起点
```

几个**从源码里能直接读出来的事实**：

1. **相对路径都以 `$myPath` 为锚点**。例如 `$myPath/../VHDL/psi_common/sim` 意味着「从 `scripts/` 往上一级到仓库根，再进 `VHDL/psi_common/sim`」。这隐含一个**使用约定**：这些驱动脚本必须在 `scripts/` 目录下被 `source`（pwd 必须是 `scripts/`），否则所有 `$myPath/../...` 都会指错地方。
2. **`cd` 和 `source` 成对出现**，一次处理一个子模块。
3. **「不处理」=「把这两行注释掉」**。脚本用 TCL 注释 `#` 把某个子模块的 `cd`+`source` 整块注释掉，就等于「这次跳过它」。三种仿真器各自跳过的库不同（这部分留到 u2-l3「仿真器兼容性矩阵」细讲）。
4. **全部处理完后 `cd $myPath` 收尾**，回到起点，方便后续步骤（仿真脚本会在回到起点后继续 `compile_files` / `run_tb` / `run_check_errors`）。

#### 4.2.3 源码精读

**仿真脚本的标准「cd + source」段**（`runModelsim.tcl` 第一段为例）：[scripts/runModelsim.tcl:17-18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L17-L18) —— `cd` 到 `../VHDL/psi_common/sim`，再 `source config.tcl`。注意 `config.tcl` 是**不带任何路径**的裸文件名，因此它一定是「当前目录（即刚 cd 进去的 sim 目录）里的 config.tcl」——这正是 4.2.1 强调的「`cd` 是为了配合内部脚本的相对路径」。

**「跳过某库」的写法**（GHDL 不兼容的例子）：[scripts/runGhdl.tcl:23-25](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L23-L25) —— 注释 `#TB not GHDL compatible!` 下面把 `psi_multi_stream_daq` 的 `cd`+`source` 两行整体注释掉，于是 GHDL 跑仿真时跳过这个库。同样的「整块注释 + 一句原因」的模式在 `runVivado.tcl` 里出现得更密集。

**收尾回到起点**：[scripts/runModelsim.tcl:50](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L50) —— 一句 `cd $myPath` 把工作目录还原回 `scripts/`，随后才进入 compile/run/check 阶段。

**打包脚本的对应段**（结构最干净）：[scripts/packageAllIp.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L11-L12) —— `cd` 到 `../VivadoIp/vivadoIP_axis_data_gen/scripts`，再 `source package.tcl`。和仿真脚本一模一样的「`cd` 进约定目录 + `source` 内部脚本」组合，只是约定子目录从 `sim` 换成了 `scripts`、内部脚本从 `config.tcl` 换成了 `package.tcl`。

**打包脚本的收尾**：[scripts/packageAllIp.tcl:35-36](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L35-L36) —— 注释 `#Go back to initial directory` 后 `cd $myPath`，与仿真脚本收尾完全一致。

#### 4.2.4 代码实践

**目标**：从 4 个脚本里提炼出唯一的「遍历模板」，并用它解释一个真实差异。

**操作步骤**：

1. 打开 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，数一数它 `source package.tcl` 了多少次（即打包了多少个 IP）。
2. 打开 [`scripts/runModelsim.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl)，数一数它「未被注释」的 `source config.tcl` 有多少次。
3. 把两者的「遍历模板」并排写出来，确认它们是同一个模板（仅子目录名与内部脚本名不同）。

**需要观察的现象**：两个脚本都是「重复 N 次：`cd $myPath/../<lib>/<dir>` + `source <内部脚本>`」，没有任何其它花招。

**预期结果**：你会得到这样一段通用伪代码（请亲手填空）：

```
set myPath [pwd]
# 重复 N 次：
cd $myPath/../____/____/____      ;# 仿真: VHDL或VivadoIp / <lib> / sim ；打包: VivadoIp / <lib> / scripts
source ____                       ;# 仿真: config.tcl ；打包: package.tcl
cd $myPath
```

**待本地验证**：如果你已在本地用 `--recurse-submodules` 完整克隆了仓库，可以试着在 Vivado/ModelSim 的 Tcl 控制台里 `cd` 到 `scripts/` 后 `source runModelsim.tcl`，观察它是否真的依次进入各子模块目录（若未完整克隆子模块，`source config.tcl` 会因文件不存在而报错——这正好印证「内部脚本不在本仓库」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `source config.tcl` 不写成 `source $myPath/../VHDL/psi_common/sim/config.tcl`（带完整路径），而要先 `cd` 再用裸文件名？
**答案**：因为 `config.tcl` 内部很可能也用了相对路径去引用该子模块的其它文件（如待编译的 VHDL、TB 文件等，**待确认**）。TCL 里相对路径相对于「当前工作目录」解析，所以必须先 `cd` 到该子模块的约定工作目录，内部的所有相对路径才会同时正确。只把 `source` 自身写成绝对路径、却不 `cd`，内部脚本的相对路径仍会指错。

**练习 2**：在 `runVivado.tcl` 里，某个库被「跳过」时，脚本是怎么写的？这种写法的好处是什么？
**答案**：用 TCL 注释 `#` 把该库对应的 `cd` + `source` 两行整体注释掉，并在上方加一行原因（如 `#TB not Vivado compatible!`）。好处是：保留原始信息、一目了然地看到「这个库存在，但当前仿真器不兼容」，想重新启用时取消注释即可，不需要重新查路径。

**练习 3**：这些驱动脚本假设自己被运行时 `pwd` 是哪个目录？依据是什么？
**答案**：假设 `pwd` 是仓库内的 `scripts/` 目录。依据是所有相对路径都以 `$myPath/../...` 开头——只有当 `myPath` 指向 `scripts/` 时，`../VHDL/...`、`../TCL/...`、`../VivadoIp/...` 才会正确指向仓库里对应的大类目录。

---

### 4.3 .gitignore 的 * + !*.tcl + !.gitignore 规则

#### 4.3.1 概念说明

`scripts/` 目录里运行仿真和打包时，会**产生大量临时产物**（编译中间文件、仿真工作库、日志、PsiSim 生成的运行脚本等）。如果不去管，这些产物会被 `git status` 当成「未跟踪文件」刷屏，甚至被误提交。

[`scripts/.gitignore`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore) 用的是一种**白名单（allowlist）策略**——和普通「列出要忽略的东西」相反，它**先忽略一切，再把要保留的逐条放行**。这样无论工具生成什么奇奇怪怪的文件，默认都不会进版本库，只有被明确放行的才会。

#### 4.3.2 核心流程

逐行解释这份 `.gitignore`：

| 行 | 内容 | 含义 |
|---|---|---|
| 1 | `#Ignore everything except the file types required` | 注释：说明本文件意图 |
| 2 | `*` | **忽略一切**（白名单的「黑底」） |
| 3 | `!*.tcl` | 放行所有 `.tcl` 文件（要保留的脚本） |
| 4 | `!.gitignore` | 放行 `.gitignore` 自身（否则它自己也被 `*` 忽略了） |
| 5 | （空行） | 分组分隔 |
| 6 | `#Ignore artifacts from runs` | 注释：下面专门处理运行产物 |
| 7 | `psi_sim_run.tcl` | 即使它是 `.tcl`，也强制忽略（运行产物，不该提交） |

关于第 7 行有一个**重要的 git 规则**值得记住：**同一个 `.gitignore` 里，后出现的模式会覆盖先出现的**。`psi_sim_run.tcl` 既匹配第 3 行的 `!*.tcl`（放行），又匹配第 7 行的 `psi_sim_run.tcl`（忽略）；因为第 7 行在后，所以**最终它被忽略**。这是刻意为之——`psi_sim_run.tcl` 是 PsiSim 运行时生成的临时驱动脚本（文件名「待确认」其确切生成时机），即便扩展名是 `.tcl`，也不该进版本库。

#### 4.3.3 源码精读

白名单主体：[scripts/.gitignore:1-4](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore#L1-L4) —— `*` 先忽略全部，再用 `!*.tcl`、`!.gitignore` 把要保留的放行回来。注意 `!.gitignore` 这一行**必不可少**——因为 `*` 会把 `.gitignore` 自己也忽略掉，必须显式放行，否则这份规则文件本身就无法被 git 跟踪。

运行产物例外：[scripts/.gitignore:6-7](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/.gitignore#L6-L7) —— 单独把 `psi_sim_run.tcl` 列出并忽略，靠「后写覆盖先写」的规则盖过上面的 `!*.tcl`。

> 一个推论（有助于理解 git 行为）：白名单策略对**直接放在 `scripts/` 下的 `.tcl`** 放行有效；但若工具在某**子目录**（被 `*` 当作目录忽略）里生成了 `.tcl`，由于「父目录被忽略时无法重新包含其内文件」这条 git 规则，那些深层 `.tcl` 仍会被忽略——这正是我们想要的（深层产物一律不进库）。

#### 4.3.4 代码实践

**目标**：在本地感受「白名单 + 后写覆盖」的真实效果。

**操作步骤**：

1. 在仓库的 `scripts/` 目录下，**新建**三个测试文件（**示例文件，用完请删除**，不要提交）：
   - `my_tool.tcl`（模拟一个手写脚本）
   - `junk.txt`（模拟一个无关产物）
   - `psi_sim_run.tcl`（模拟 PsiSim 的运行产物）
2. 运行 `git status scripts/`，观察哪几个文件出现、哪几个不出现。
3. 把三个测试文件删掉，恢复干净。

**需要观察的现象**：

- `my_tool.tcl`：**出现**（被 `!*.tcl` 放行）。
- `junk.txt`：**不出现**（被 `*` 忽略）。
- `psi_sim_run.tcl`：**不出现**（虽然匹配 `!*.tcl`，但被第 7 行覆盖）。

**预期结果**：只有 `my_tool.tcl` 出现在 `git status` 里，验证了「白名单放行 `*.tcl`，但 `psi_sim_run.tcl` 被末行覆盖忽略」。

**源码阅读型替代实践**（不改文件系统）：用文字回答——如果有人误把 `psi_sim_run.tcl` 这一行从 `.gitignore` 删掉，会发生什么？为什么第 7 行的顺序（必须排在 `!*.tcl` 之后）很关键？

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接写一份「列出所有要忽略的产物扩展名」的黑名单，而要用「`*` + 白名单」？
**答案**：因为仿真器和打包工具产生的临时产物**种类繁多、且会随版本变化**（编译工作库、日志、临时 tcl、中间网表……）。逐一枚举很容易漏，漏掉就会被误提交。白名单策略「默认拒绝、逐条放行」更安全：**凡是没显式放行的，一律不进库**，无论工具将来生成什么新东西。

**练习 2**：如果把第 4 行 `!.gitignore` 删掉，会有什么后果？
**答案**：`*` 会把 `.gitignore` 自身也忽略掉，导致这份规则文件无法被 git 跟踪/提交。一旦别人 clone 仓库，就拿不到这份忽略规则了。所以 `!.gitignore` 是白名单写法里**必须记得放行**的一行。

**练习 3**：`psi_sim_run.tcl` 既匹配 `!*.tcl` 又匹配 `psi_sim_run.tcl`，git 最终按哪个处理？依据是什么？
**答案**：最终按**忽略**处理。依据是 git 的「**最后一个匹配的模式生效**」规则——`psi_sim_run.tcl` 排在 `!*.tcl` 之后，因此它的「忽略」覆盖了前者的「放行」。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个「**模板提炼 + 策略解释**」的小任务。

### 任务背景

假设你是一位新加入 PSI 的工程师，要给同事写一份一页纸的「`scripts/` 目录速览」，让别人 5 分钟看懂这 4 个脚本。

### 你要交付的两样东西

**交付物 A：通用驱动脚本模板（伪代码）**

请基于本讲对四个脚本的分析，提炼出一段**所有四个脚本共享**的骨架伪代码，并在每个关键步骤旁标注「来自哪个最小模块」。参考答案框架（请补全）：

```
# [模块4.1] 版权头（PSI copyright，作者 Oliver Bruendler）
# [模块4.1] （仅仿真脚本）加载框架：source ../TCL/PsiSim/PsiSim.tcl + namespace import + init
# [模块4.1] set myPath [pwd]            ;# 记下起点（必须 pwd == scripts/）

# [模块4.2] 遍历子模块（每个子模块重复一次）：
#           cd $myPath/../<大类>/<lib>/<约定子目录>   ;# 仿真: sim  打包: scripts
#           source <内部脚本>                         ;# 仿真: config.tcl  打包: package.tcl
#           （如需跳过该子模块，把这两行用 # 注释掉，并写明原因）

# [模块4.2] cd $myPath                   ;# 回到起点
# （仅仿真脚本）compile_files -all -clean / run_tb -all / run_check_errors "###ERROR###"
```

**交付物 B：`.gitignore` 策略一句话解释**

用一段话（不超过 3 句）解释 `scripts/.gitignore` 为什么用「先 `*` 忽略全部，再放行 `*.tcl` 与 `.gitignore`，最后再把 `psi_sim_run.tcl` 单独忽略」的方式。提示词：仿真/打包会产生大量临时产物；白名单默认更安全；末行覆盖规则。

### 检验标准

- 你的模板能同时解释 `runModelsim.tcl`（仿真）和 `packageAllIp.tcl`（打包）两类的结构，只是「约定子目录」和「内部脚本名」两个槽位不同。
- 你的 `.gitignore` 解释能讲清「为什么 `psi_sim_run.tcl` 虽是 `.tcl` 却被忽略」。
- 全程**不需要**查看任何 submodule 内部文件即可完成（因为本讲只讲「驱动脚本」这一层）。

---

## 6. 本讲小结

- `scripts/` 下只有 **5 个被跟踪文件**：4 个 TCL 驱动脚本 + 1 个 `.gitignore`。
- 四个驱动脚本共享同一套骨架：**版权头 → `set myPath [pwd]` 记下起点 → 遍历子模块（`cd` 进约定目录 + `source` 内部脚本）→ `cd $myPath` 回到起点**。
- `cd` + `source` 成对出现，是因为 `source` 进来的内部脚本使用**相对路径**，必须先把工作目录切到该子模块的约定目录（仿真是 `<lib>/sim`、打包是 `<lib>/scripts`）。
- 必须区分**驱动脚本**（本仓库的 4 个，只做编排）与**内部脚本**（`config.tcl` / `package.tcl` / `PsiSim.tcl`，住在各 submodule 里，本仓库不含，具体内容「待确认」）。
- 所有脚本假设运行时 `pwd` 为 `scripts/`，因此路径都写成 `$myPath/../...`。
- `scripts/.gitignore` 用**白名单策略**（`*` + `!*.tcl` + `!.gitignore`）默认忽略一切产物，仅放行脚本本身，并用末行的 `psi_sim_run.tcl` 覆盖规则把运行产物单独排除。

---

## 7. 下一步学习建议

本讲只看了「骨架」，接下来三讲会分别填进「血肉」：

- **u2-l2 仿真驱动脚本与 PsiSim 流程**：深入三个 `run*.tcl`，讲清 PsiSim 的 `init → configure → compile → run → check` 五步流水线，以及 `namespace import psi::sim::*` 的作用。
- **u2-l3 仿真器兼容性矩阵**：利用脚本里的注释（`#TB not GHDL compatible!` / `#TB not Vivado compatible!` / power_sink 无自检 TB），整理出「库 × 仿真器」的兼容矩阵。
- **u2-l4 Vivado IP 批量打包脚本**：聚焦 [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，讲它如何遍历各 `vivadoIP_*` 的 `scripts/` 目录、`source package.tcl` 完成批量打包。

建议在进入 u2-l2 之前，先回头确认你已经能默写出本讲 4.2.2 的「遍历模板」——那是后续三讲共同的脚手架。
