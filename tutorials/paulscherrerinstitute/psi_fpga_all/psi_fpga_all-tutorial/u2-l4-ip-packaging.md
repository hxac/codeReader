# Vivado IP 批量打包脚本

> 本讲是第二单元「脚本驱动的仿真、编译与 IP 打包」的最后一讲。
> 上一讲（u2-l3）我们从「兼容性」视角重新审视了三个仿真脚本，最后留下一个伏笔：**「能跑仿真的 IP」和「会被打包的 IP」是两个不同的集合**。
> 本讲我们终于把镜头转向 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)——4 个驱动脚本里**最干净**的一个，看它如何把一批 HDL 库批量封装成可被 Vivado 直接调用的 IP 核，并亲手验证上面那句「两个不同集合」的论断。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚**为什么要把 HDL「打包」成 Vivado IP**，以及 `package.tcl` 在这个流程里扮演的角色，并把它与仿真流程里的 `config.tcl` 做类比。
2. 一眼读懂 [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) 的全部结构：`set myPath [pwd]` 锚定起点 → 逐个 `cd` 到 `vivadoIP_*/scripts` 后 `source package.tcl` → `cd $myPath` 收尾。
3. 列举该脚本当前打包了哪 **8 个** IP、对比 `.gitmodules` 里全部 **11 个** `vivadoIP_*` 子模块，找出**没被打包的 3 个**。
4. 用 `power_sink`（能打包但不仿真）和 `axi_mm_reader`（能仿真但不打包）两个反例，论证「打包集合」与「仿真集合」相互独立，并解释其原因。

---

## 2. 前置知识

本讲直接依赖 u2-l1 建立的「驱动脚本遍历模板」，并与 u2-l2/u2-l3 的仿真流程处处对比。开始前，先把三件最关键的直觉复习一遍。

### 2.1 为什么要「打包」HDL——Vivado IP 是什么

在 Xilinx Vivado 里，你可以把自己写的 HDL 模块**封装（package）成一个 IP 核**：给它起一个名字、定一个版本号，把 VHDL 的 `generic` 暴露成图形界面里可填的参数，把端口整理成 IP 的对外接口，最终生成一个**自包含、可版本化、可被别人从 IP Catalog 直接拖进工程复用**的 IP 包。

打个比方：仿真（u2-l2/u2-l3）是**验证**你写的模块「行为对不对」；打包（本讲）是把这个验证过的模块**装进盒子、贴上标签、放进货架**，让团队里其他人不用关心内部实现，直接拿来用。两件事面向同一批 HDL，但目的完全不同——这就是「打包集合 ≠ 仿真集合」的根源。

> 关于 PSI 的打包工具链：本仓库的 `TCL/PsiIpPackage` 子模块是 PSI 自研的 IP 打包 TCL 框架（见 `.gitmodules` 第 28–30 行，Changelog 中版本从 2.0.0 演进到 2.4.0），它**在 IP 打包这件事上扮演的角色，正相当于 `PsiSim` 在仿真里扮演的角色**。但它与各 IP 的 `package.tcl` 都住在子模块内部，本仓库未直接包含，其具体 API 与内部行为标注「**待确认**」。本讲只讲驱动脚本 [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) 这一层能确定的「编排约定」。

### 2.2 u2-l1 的遍历模板（一句话回顾）

四个驱动脚本共享同一套骨架：**版权头 → `set myPath [pwd]` 记下起点 → 遍历子模块（`cd` 进约定目录 + `source` 内部脚本）→ `cd $myPath` 回起点**。其中：

- 仿真的「约定目录」是 `<lib>/sim`、内部脚本是 `config.tcl`。
- 打包的「约定目录」是 `<lib>/scripts`、内部脚本是 `package.tcl`——**只有这两个槽位不同**，其余编排逻辑完全一致。

> 关键约束（来自 u2-l1）：这些脚本假设自己被运行时 `pwd` 是仓库内的 `scripts/`，因此所有相对路径都写成 `$myPath/../...`；`cd` 与 `source` 必须成对出现，因为 `package.tcl` 内部很可能也用相对路径引用本 IP 的 HDL 文件，必须先 `cd` 到该 IP 的约定工作目录，这些相对路径才能正确解析（其确切写法「待确认」，因 `package.tcl` 不在本仓库）。

### 2.3 「打包」与「仿真」是两套独立的流程

u2-l3 末尾留了一句话：**「能跑仿真的 IP」和「会被打包的 IP」是两个不同的集合**。本讲会用 `packageAllIp.tcl` 的打包清单与 u2-l3 的仿真矩阵做交叉对比，亲手证实它——最典型的两个反例是：

- `vivadoIP_power_sink`：三种仿真器**都不跑它**（功耗不可仿真、没有 self-checking TB），但它**被 `packageAllIp.tcl` 打包**了。打包它恰恰有用：在真实设计里例化它来制造翻转活动、喂给功耗分析工具。
- `vivadoIP_axi_mm_reader`：能仿真的仿真器都跑它（GHDL/ModelSim 启用），但它**没有被 `packageAllIp.tcl` 打包**。

记住这两个名字，它们是本讲最有价值的洞察的支点。

---

## 3. 本讲源码地图

本讲只读**一个**本仓库内的真实文件，但它会和另外两份「清单」交叉印证：

| 文件 | 位置 | 角色 | 本讲关注点 |
|---|---|---|---|
| [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) | 本仓库 | IP 打包驱动脚本 | 全文精读（仅 38 行） |
| [`.gitmodules`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) | 本仓库 | submodule 权威清单 | 数出全部 11 个 `vivadoIP_*`，与打包清单做差集 |
| [`Changelog.md`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md) | 本仓库 | 版本固定记录 | 印证 `PsiIpPackage` 框架与各 IP 的版本演变 |

> 一如既往：脚本里被 `source` 进来的 `package.tcl`（以及它依赖的 `TCL/PsiIpPackage` 框架）**都不在本仓库**，分别住在对应 submodule 里。本仓库的角色只是「按固定相对路径把这些脚本串起来」。

---

## 4. 核心概念与源码讲解

本讲把 `packageAllIp.tcl` 拆成 **3 个最小模块**来讲：① IP 打包的目的与 `package.tcl` 的角色；② 遍历与 `source`：`set myPath` → `cd vivadoIP_*/scripts` → `source package.tcl` → `cd $myPath` 收尾；③ 打包集合 vs 全部 IP 集合（含与仿真矩阵的交叉对比）。

### 4.1 IP 打包的目的与 package.tcl 的角色

#### 4.1.1 概念说明

上一讲的三个 `run*.tcl` 解决的问题是「**这批库的功能对不对**」，手段是跑 self-checking TB。本讲的 `packageAllIp.tcl` 解决的是一个完全不同的问题：「**把哪些库封装成可复用的 Vivado IP**」。

两件事的关系可以这样理解：

| 流程 | 回答的问题 | 产出 | 内部脚本 |
|---|---|---|---|
| 仿真（`run*.tcl`） | 这个库**功能对不对**？ | 通过/失败的判定（看 `###ERROR###`） | `config.tcl`（登记源文件与 TB） |
| 打包（`packageAllIp.tcl`） | 这个库**能不能封装成可复用 IP**？ | 一个个 Vivado IP 包（产物「待确认」） | `package.tcl`（声明如何封装本 IP） |

`package.tcl` 的角色，与 `config.tcl` 在仿真里的角色是**对称**的：

- `config.tcl`（仿真侧）：住在 `<lib>/sim` 里，**知道这个库要编译哪些源文件、跑哪些 TB**，把它登记给 PsiSim 框架。
- `package.tcl`（打包侧）：住在 `<lib>/scripts` 里，**知道这个 IP 要包含哪些 HDL、暴露哪些参数/端口、版本号是多少**，把它封装成一个 Vivado IP。

> 两者的共同点是：它们都是「**住在子模块内部、承载该子模块特有知识**」的脚本；本仓库的驱动脚本只是「挨个把它们喊出来执行」，本身不含任何编译规则或封装逻辑。`package.tcl` 内部具体如何调用 PsiIpPackage 框架或 Vivado 原生打包命令，因文件不在本仓库而「**待确认**」。

#### 4.1.2 核心流程

`packageAllIp.tcl` 的整体流程，抽象成伪代码：

```
# ── 1. 版权头 ──
# ── 2. 记下起点 ──
set myPath [pwd]                              ;# 必须保证 pwd == scripts/

# ── 3. 逐个打包（每个 IP 重复一次）──
foreach 要打包的 IP in [IP 列表] {
    cd $myPath/../VivadoIp/<IP 名>/scripts     ;# 切到该 IP 的约定工作目录
    source package.tcl                         ;# 执行该 IP 的封装脚本
}

# ── 4. 回到起点 ──
cd $myPath
```

这里有一个**与仿真脚本的关键差异**值得提前点出（4.2 会用源码证实）：仿真脚本在 `set myPath` **之前**，会在脚本顶部统一 `source ../TCL/PsiSim/PsiSim.tcl` 并 `namespace import` 把框架命令导入全局；而 `packageAllIp.tcl` 的顶部**没有任何框架加载**——它直接 `set myPath [pwd]` 就开始遍历。这说明打包所需的框架（`PsiIpPackage`）很可能是在**每个 `package.tcl` 内部各自加载**的，而非由驱动脚本统一加载。这一点是本仓库代码可直接读出的事实，而「`package.tcl` 内部如何加载框架」则是「**待确认**」。

#### 4.1.3 源码精读

版权头（注意年份是 **2019**，比三个仿真脚本的 2018 晚一年，说明打包脚本是后来才加入的）：[scripts/packageAllIp.tcl:1-5](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L1-L5) —— PSI 标准 copyright 块，标注 2019 年、作者 Oliver Bruendler。

**与仿真脚本最显著的结构差异**：紧跟版权头之后，脚本**没有** `source ../TCL/PsiIpPackage/...` 之类的框架加载行，直接就是记起点：[scripts/packageAllIp.tcl:7-8](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L7-L8) —— 注释 `#Setup` 之后一句 `set myPath [pwd]`，前面没有任何前置框架加载。对比 u2-l2 里 `runModelsim.tcl` 第 7–12 行的 `source PsiSim.tcl` + `namespace import` + `init`，这里「干净」得多。

> 这一点不能过度解读：驱动脚本顶部不加载框架，**不代表打包不需要框架**——更可能是框架加载被推迟到了每个 `package.tcl` 内部。具体机制「待确认」，但「驱动脚本顶部无框架加载」这一事实是确定的，也是它与仿真脚本最直观的差异。

#### 4.1.4 代码实践

**目标**：通过对比，亲手确认「打包脚本顶部无框架加载」这一结构差异，并理解 `package.tcl` 与 `config.tcl` 的对称角色。

**操作步骤**：

1. 打开 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，确认它的前 8 行里**没有** `source` 任何 `PsiSim.tcl`/`PsiIpPackage` 框架文件，`set myPath [pwd]` 前面只有版权头和 `#Setup` 注释。
2. 打开 [`scripts/runModelsim.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl) 第 7–12 行，确认仿真脚本在 `set myPath` 前确实 `source` 了 PsiSim 框架。

**需要观察的现象**：同样在「记下起点」之前，仿真脚本多了一截框架加载，打包脚本没有。

**预期结果**：你能指着两份脚本的前 8 行说清楚——这是两类驱动脚本在结构上（除「约定目录/内部脚本名」之外）最明显的差异。

**说明**：纯源码阅读型实践，不需要安装 Vivado。

#### 4.1.5 小练习与答案

**练习 1**：`config.tcl` 和 `package.tcl` 各自「知道」什么？为什么它们必须住在子模块内部、而不能集中放进本仓库？

**参考答案**：`config.tcl` 知道这个库要编译哪些源文件、跑哪些 TB；`package.tcl` 知道这个 IP 要包含哪些 HDL、暴露哪些参数/端口、版本号多少。两者承载的都是「该子模块特有的知识」，且会随各子模块独立发版而变化，所以必须住在各自子模块内部、由各子模块维护者维护。本仓库只是集合仓库，不持有这些知识。

**练习 2**：`packageAllIp.tcl` 顶部不加载 `PsiIpPackage` 框架，这与仿真脚本顶部加载 `PsiSim` 的做法不同。能不能据此下结论说「打包流程根本不需要框架」？

**参考答案**：不能。驱动脚本顶部不加载，只说明框架**不是在这里**统一加载的；更合理的推断是每个 `package.tcl` 内部各自加载 `PsiIpPackage`（或直接调用 Vivado 原生打包命令）。`PsiIpPackage` 作为 `TCL/` 下的子模块（见 `.gitmodules` 与 Changelog）确实存在，打包流程是否/如何依赖它，需读到子模块内的 `package.tcl` 才能确认，标注「待确认」，不应臆断。

---

### 4.2 遍历与 source：set myPath → cd vivadoIP_*/scripts → source package.tcl → cd $myPath 收尾

#### 4.2.1 概念说明

这是 `packageAllIp.tcl` 的**全部主体**，也是 u2-l1「遍历模板」的一个**最干净实例**：没有 `init` 选仿真器、没有 `compile/run/check` 三段执行，整份脚本从头到尾就是「`cd` 进一个 IP 的 `scripts/` 目录 → `source package.tcl`」的重复，外加首尾各一句路径管理。

注意它与仿真脚本的另一个差异：仿真脚本「跳过一个库」的做法是**把 `cd`+`source` 两行整块 `#` 注释掉**并写明原因（见 u2-l3）；而打包脚本里**没有「注释跳过」这种写法**——一个 IP 要么被写进脚本（打包），要么干脆不出现（不打包）。所以判断「哪些 IP 被打包」，就是数脚本里出现了多少个 `source package.tcl`，没有歧义。

#### 4.2.2 核心流程

把 `packageAllIp.tcl` 的主体抽象成模板（与 u2-l1 的模板完全同构，只填入打包场景的槽位）：

```
set myPath [pwd]
# 重复 N 次（每个被打包的 IP）：
cd $myPath/../VivadoIp/<IP 名>/scripts      ;# 约定子目录是 scripts（不是 sim）
source package.tcl                          ;# 内部脚本叫 package.tcl（不是 config.tcl）
# ...
cd $myPath                                  ;# 回到起点收尾
```

从源码里能直接数出来的事实：

1. **相对路径以 `$myPath` 为锚点**：`$myPath/../VivadoIp/vivadoIP_xxx/scripts` 意味着「从 `scripts/` 回到仓库根，再进 `VivadoIp/<IP>/scripts`」。隐含使用约定：脚本必须在 `scripts/` 下被 `source`（`pwd == scripts/`）。
2. **每个 IP 固定两行**：一行 `cd`、一行 `source package.tcl`，成对出现，中间空行分隔。
3. **全部遍历完后 `cd $myPath` 收尾**，把工作目录还原回 `scripts/`。

#### 4.2.3 源码精读

起点（与版权头之间无框架加载）：[scripts/packageAllIp.tcl:7-8](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L7-L8) —— `#Setup` 后 `set myPath [pwd]` 记下起点。

打包段的第一对（也是全脚本的第一对）：[scripts/packageAllIp.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L11-L12) —— `cd` 到 `vivadoIP_axis_data_gen/scripts` 后 `source package.tcl`。注意 `package.tcl` 是**不带路径的裸文件名**，所以它一定是「刚 `cd` 进去的 `scripts` 目录里的 `package.tcl`」——这正是 u2-l1 强调的「`cd` 是为了配合内部脚本的相对路径」。

这种「`cd` + `source package.tcl`」的两行组合在脚本里**重复了 8 次**，依次对应 8 个 IP：

| 序 | 源码位置 | 被打包的 IP |
|---|---|---|
| 1 | [scripts/packageAllIp.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L11-L12) | `vivadoIP_axis_data_gen` |
| 2 | [scripts/packageAllIp.tcl:14-15](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L14-L15) | `vivadoIP_clock_measure` |
| 3 | [scripts/packageAllIp.tcl:17-18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L17-L18) | `vivadoIP_data_rec` |
| 4 | [scripts/packageAllIp.tcl:20-21](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L20-L21) | `vivadoIP_mem_test` |
| 5 | [scripts/packageAllIp.tcl:23-24](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L23-L24) | `vivadoIP_psi_ms_daq` |
| 6 | [scripts/packageAllIp.tcl:26-27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L26-L27) | `vivadoIP_spi_simple` |
| 7 | [scripts/packageAllIp.tcl:29-30](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L29-L30) | `vivadoIP_i2c_devreg` |
| 8 | [scripts/packageAllIp.tcl:32-33](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L32-L33) | `vivadoIP_power_sink` |

收尾回到起点：[scripts/packageAllIp.tcl:35-36](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L35-L36) —— 注释 `#Go back to initial directory` 后一句 `cd $myPath`，与仿真脚本收尾完全一致。打包流程到此结束，**没有**仿真里那种 `run_check_errors` 式的成功/失败判定——打包是否成功，要看 Vivado 控制台是否生成了预期的 IP 包产物（具体判定标志「待确认」，因 `package.tcl` 不在本仓库）。

> 一个**与 u2-l3 直接呼应**的观察：第 8 个被打包的是 `vivadoIP_power_sink`——正是那个「三种仿真器都不跑、因为没有 self-checking TB」的库。它在这里被**照常打包**。这就是 2.3 节埋下的第一个反例的源码出处：**功耗不可仿真，但完全可以封装成 IP**（例化进真实设计、给功耗分析工具喂翻转激励，正是它的用途）。

#### 4.2.4 代码实践

**目标**：亲手数出被打包的 8 个 IP，并验证「`cd`+`source` 两行一对、中间空行」的版式。

**操作步骤**：

1. 打开 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，统计 `source package.tcl` 出现的次数，把每一对对应的 IP 名（从 `cd` 那行的路径里读出）填进一张 8 行的表。
2. 检查每一对之间是否都是「一行 `cd` + 一行 `source` + 一行空行」的固定版式。

**需要观察的现象**：恰好 8 次 `source package.tcl`，IP 名依次为 axis_data_gen、clock_measure、data_rec、mem_test、psi_ms_daq、spi_simple、i2c_devreg、power_sink；版式完全规整。

**预期结果**：你得到一张与 4.2.3 表格一致的「被打包 IP 清单」，并确认这份脚本没有任何 `#` 注释跳过的写法。

**说明**：源码阅读型实践，不需要运行 Vivado。若想在真实环境验证，需先按 u1-l2 用 `--recurse-submodules` 完整克隆，再在 Vivado Tcl 控制台里 `cd` 到 `scripts/` 后 `source packageAllIp.tcl`（完整运行「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `source package.tcl` 不写成 `source $myPath/../VivadoIp/vivadoIP_xxx/scripts/package.tcl`（带完整路径），而要先 `cd` 再用裸文件名？

**参考答案**：因为 `package.tcl` 内部很可能也用相对路径引用该 IP 的 HDL 与其它文件（确切写法「待确认」）。TCL 里相对路径相对于「当前工作目录」解析，所以必须先 `cd` 到该 IP 的 `scripts/` 约定工作目录，内部所有相对路径才会同时正确。只把 `source` 写成绝对路径、却不 `cd`，内部脚本的相对路径仍会指错。

**练习 2**：仿真脚本「跳过一个库」用 `#` 注释两行，打包脚本「不打包某个 IP」是怎么做的？这种做法对「判断哪些 IP 被打包」有什么影响？

**参考答案**：打包脚本里没有「注释跳过」的写法——一个 IP 要么被打包（写进脚本），要么根本不出现。因此判断「哪些 IP 被打包」没有歧义：直接数 `source package.tcl` 的次数即可，不需要像 u2-l3 那样去分辨「启用 / 被注释 / 缺位」三种状态（缺位状态会在 4.3 专门讨论）。

---

### 4.3 打包集合 vs 全部 IP 集合（与仿真矩阵的交叉对比）

#### 4.3.1 概念说明

4.2 列出了被打包的 8 个 IP。但 `.gitmodules` 显示仓库里一共有 **11 个** `vivadoIP_*` 子模块。把两者一比，就会发现有 **3 个 IP 没有被 `packageAllIp.tcl` 打包**。这引出本讲第二个关键洞察：**「会被打包的 IP」是「全部 IP」的一个子集，而且这个子集的选取规则在本仓库里并没有文档说明**——它直接体现在脚本里写了谁、没写谁。

同时，把这个「打包集合」和 u2-l3 的「仿真集合」放在一起，就能兑现 2.3 节的承诺：**这两个集合相互独立**。

#### 4.3.2 核心流程

判定一个 IP 是否被打包的口径很简单：**它的 `package.tcl` 是否在 `packageAllIp.tcl` 里被 `source`**。据此把全部 11 个 `vivadoIP_*` 分成两组：

**被脚本打包的 8 个**（见 4.2.3 表格）：`axis_data_gen`、`clock_measure`、`data_rec`、`mem_test`、`psi_ms_daq`、`spi_simple`、`i2c_devreg`、`power_sink`。

**未被脚本打包的 3 个**（出现在 `.gitmodules`，但脚本里搜不到）：

| 未打包的 IP | `.gitmodules` 出处 | Changelog 中的版本线索 |
|---|---|---|
| `vivadoIP_fpga_base` | [.gitmodules:61-63](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L61-L63) | 2020.2 起固定为 1.4.0 |
| `vivadoIP_sync_edge_det` | [.gitmodules:64-66](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L64-L66) | 早期名为 `vivadoIP_sync_det_edge`（见 Changelog，u1-l3 已述其改名史） |
| `vivadoIP_axi_mm_reader` | [.gitmodules:67-69](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L67-L69) | 2020.2 起固定为 1.0.0 |

> 这 3 个 IP **为什么没被打包**，本仓库没有给出原因。可能的解释（均「**待确认**」，不应臆断）：例如 `fpga_base` 从名字看像「板级/平台基座设计」，可能本就是顶层聚合而非可独立复用的 IP；`sync_edge_det` 与 `axi_mm_reader` 可能是后来新增时漏改了打包脚本，也可能有意暂不打包。要确定原因，需查提交历史或问维护者，本仓库脚本本身不回答这个问题。

接下来兑现与仿真矩阵的交叉对比。把本讲的「打包」列并到 u2-l3 的矩阵里：

| IP（submodule） | GHDL 仿真 | ModelSim 仿真 | Vivado 仿真 | 是否被打包 | 备注 |
|---|:---:|:---:|:---:|:---:|---|
| `vivadoIP_axis_data_gen` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| `vivadoIP_clock_measure` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| `vivadoIP_data_rec` | ❌ | ✅ | ❌ | ✅ | GHDL 不兼容，但能打包 |
| `vivadoIP_mem_test` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| `vivadoIP_psi_ms_daq` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| `vivadoIP_spi_simple` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| `vivadoIP_i2c_devreg` | ✅ | ✅ | ❌ | ✅ | 仿真与打包都覆盖 |
| **`vivadoIP_power_sink`** | ❌ | ❌ | ❌ | **✅** | **全不仿真，却被打包**（功耗不可仿真，但可做 IP） |
| `vivadoIP_fpga_base` | — | — | — | ❌ | 不仿真也不打包 |
| `vivadoIP_sync_edge_det` | — | — | — | ❌ | 不仿真也不打包 |
| **`vivadoIP_axi_mm_reader`** | ✅ | ✅ | —（缺位） | **❌** | **能仿真，却未被打包** |

（仿真三列的判定口径见 u2-l3；`power_sink` 三列红是因为「无 self-checking TB」，`axi_mm_reader` 在 Vivado 列「缺位」。）

一眼可见的结论：

- **`power_sink` 行**：仿真三列全红，打包列却是绿——**能打包 ≠ 能仿真**。打包关注「能不能封装成可复用 IP」，与「能不能用功能仿真器自检」无关。
- **`axi_mm_reader` 行**：仿真两列绿，打包列却是红——**能仿真 ≠ 能打包**。它能跑 TB 验证功能，却没进打包脚本。
- 这两个反例方向相反，共同证明：**「打包集合」与「仿真集合」是两个独立的集合**，一个 IP 在一个集合里的状态，不能推断它在另一个集合里的状态。

> 选型含义：你要复用一个 PSI 的 IP，**既不能只看它能不能仿真，也不能只看它有没有被打包**——前者只保证功能对，后者只保证能封装进 Vivado。理想情况下两者都满足；遇到 `power_sink`（只打包）或 `axi_mm_reader`（只仿真）这种只有一边的，要清楚自己拿到的「保障」是哪一种。

#### 4.3.3 源码精读

3 个未打包 IP 在 `.gitmodules` 里确有其模块：[.gitmodules:61-69](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L61-L69) —— 连续声明了 `vivadoIP_fpga_base`、`vivadoIP_sync_edge_det`、`vivadoIP_axi_mm_reader` 三个子模块，它们都真实存在于仓库目录结构中。

但这 3 个名字在打包脚本里**搜不到**：通读 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) 全文（共 38 行），`source package.tcl` 仅出现 8 次（见 4.2.3），路径里出现的 IP 名只有 8 个，恰好缺这 3 个。这就是「缺位」判定——与 u2-l3 里 `axi_mm_reader` 在 `runVivado.tcl` 中的缺位是同一种现象：**既未启用、也未注释，而是压根没写进去**。

作为对照，`power_sink` 同时出现在「仿真跳过」（u2-l3 三处 `#Does not have a self-checking TB ...` 注释）与「打包启用」（[scripts/packageAllIp.tcl:32-33](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L32-L33)）两份脚本里——同一库在两个流程里状态相反，正是「两个集合独立」最直接的源码证据。

#### 4.3.4 代码实践

**目标**：亲手做「全部 IP − 被打包 IP = 未打包 IP」这个差集，验证结论只有 3 个未打包 IP。

**操作步骤**：

1. 从 [`.gitmodules`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) 里筛出所有 `path = VivadoIp/...` 的条目，得到全部 11 个 `vivadoIP_*`。
2. 从 [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) 里抄出被打包的 8 个（4.2.3 已列）。
3. 做差集，列出未被打包的 3 个。

**需要观察的现象**：差集恰好是 `vivadoIP_fpga_base`、`vivadoIP_sync_edge_det`、`vivadoIP_axi_mm_reader` 三个。

**预期结果**：你得到一张「11 = 8 打包 + 3 未打包」的对照表，与 4.3.2 一致。

**说明**：纯源码阅读型实践，无需运行任何工具。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释，为什么 `power_sink` 在三种仿真器下都不跑，却被 `packageAllIp.tcl` 打包？

**参考答案**：因为「能不能仿真」看的是有没有 self-checking TB（power_sink 做功耗分析、功耗不可仿真，所以没有 TB），而「能不能打包」看的是能不能封装成可复用 IP——power_sink 作为 IP 例化进真实设计、给功耗分析工具喂翻转激励，正是它的用途，所以它完全可以被打包。两件事的判据不同，结论自然可以相反。

**练习 2**：`vivadoIP_axi_mm_reader` 能仿真却没有被打包；`vivadoIP_power_sink` 能打包却不能仿真。这两个反例共同说明了什么？

**参考答案**：共同说明「打包集合」与「仿真集合」是两个**相互独立**的集合——一个 IP 在一个集合里的状态（打不打包 / 仿不仿真）不能用来推断它在另一个集合里的状态。要复用一个 IP，需要分别确认这两方面的保障。

**练习 3**：未被打包的 3 个 IP，其「未打包」的原因能从本仓库确定吗？如果不能，该怎么做？

**参考答案**：不能。本仓库的 `packageAllIp.tcl` 只是「没写它们」，并没有留下原因说明（不像仿真脚本有 `#TB not XX compatible!` 这类注释）。要确定原因，需要查 `git log`/`git blame` 看打包脚本的演变历史，或直接问仓库维护者，不能凭空假定（标注「待确认」）。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个「**清单核对 + 补全脚本**」的小任务。

### 任务背景

假设你是新接手 `psi_fpga_all` 的工程师。同事告诉你：「我们有一批 Vivado IP，用 `packageAllIp.tcl` 批量打包。但好像不是所有 IP 都在里面，你帮我核对一下，并把漏掉的补进去。」

### 你要交付的三样东西

**交付物 A：被打包 IP 清单**

打开 [`scripts/packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)，列出它当前 `source package.tcl` 的全部 IP（应得 8 个，见 4.2.3）。

**交付物 B：未打包 IP 清单**

打开 [`.gitmodules`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules)，列出全部 11 个 `vivadoIP_*`，与 A 做差集，指出 3 个未打包的 IP（`fpga_base`、`sync_edge_det`、`axi_mm_reader`），并说明这些「未打包」的原因在本仓库里能否确定（不能——见 4.3.5 练习 3）。

**交付物 C：补一个漏掉的 IP（示例代码）**

仿照脚本里现有的「`cd` + `source package.tcl`」两行模式，把其中一个漏掉的 IP（建议选 `vivadoIP_axi_mm_reader` 或 `vivadoIP_fpga_base`）补进 [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl)。以 `vivadoIP_axi_mm_reader` 为例，应在最后一个 IP（`power_sink`，第 32–33 行）之后、`#Go back` 注释（第 35 行）之前，插入下面两行（**示例代码**，需确保该子模块内有对应的 `scripts/package.tcl` 才能真正生效）：

```tcl
cd $myPath/../VivadoIp/vivadoIP_axi_mm_reader/scripts
source package.tcl
```

### 检验标准

- A 的清单与 4.2.3 一致（8 个）。
- B 的差集恰好是 3 个，且你明确写出「原因待确认」而非编造理由。
- C 插入的两行与现有 IP 的写法**逐字同构**（路径模式 `$myPath/../VivadoIp/<IP>/scripts` + 裸名 `source package.tcl`），位置正确（在收尾 `cd $myPath` 之前）。
- 全程不需要查看任何 submodule 内部文件即可完成核对（A、B）；只有真正运行 C 才需要完整克隆子模块并打开 Vivado（「待本地验证」）。

> ⚠️ 本仓库的约束：worker 规则禁止修改源码。本实践仅要求你**写出**应插入的代码作为学习产出，**不要真的去改 `scripts/packageAllIp.tcl`**。若要在真实环境运行，请在自己的 fork/工作副本里操作。

---

## 6. 本讲小结

- [`packageAllIp.tcl`](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl) 的全部结构是 u2-l1 遍历模板的**最干净实例**：版权头 → `set myPath [pwd]` → 重复「`cd $myPath/../VivadoIp/<IP>/scripts` + `source package.tcl`」→ `cd $myPath` 收尾。
- **打包**与**仿真**目的不同：仿真用 self-checking TB 验功能（`config.tcl`），打包把 HDL 封装成可复用 Vivado IP（`package.tcl`）；两者对称地住在各子模块内部，本仓库的驱动脚本只负责「挨个 `source`」。
- 与仿真脚本的一个结构差异：打包脚本**顶部不加载任何框架**（无 `source PsiSim.tcl`/`PsiIpPackage`），框架加载很可能发生在每个 `package.tcl` 内部（「待确认」）。
- 脚本当前打包 **8 个** IP：`axis_data_gen`、`clock_measure`、`data_rec`、`mem_test`、`psi_ms_daq`、`spi_simple`、`i2c_devreg`、`power_sink`。
- `.gitmodules` 里有 **11 个** `vivadoIP_*`，差出 **3 个未打包**：`fpga_base`、`sync_edge_det`、`axi_mm_reader`；未打包原因本仓库未说明，标注「待确认」。
- **「打包集合」与「仿真集合」相互独立**：`power_sink` 全不仿真却被打包，`axi_mm_reader` 能仿真却不打包——一个 IP 在一处状态不能推断另一处。这也兑现了 u2-l3 末尾「能跑仿真的 IP ≠ 会被打包的 IP」的伏笔。

---

## 7. 下一步学习建议

至此第二单元（脚本驱动的仿真、编译与 IP 打包）完结。你已经掌握本仓库**全部可读代码**——4 个驱动脚本 + `.gitignore`。接下来：

- **u3 单元（版本管理与仓库维护）** 会回到「集合仓库」的维护视角：
  - **u3-l1 发布管理与 submodule 版本固定**：当你想升级某个 IP（比如把 `axi_mm_reader` 升到新版本以便补进打包脚本）时，参考 Changelog 的版本固定机制与「整体快照 + 个别更新」策略。
  - **u3-l2 维护与扩展集合仓库**：想新增一个带 `package.tcl` 的 IP 子模块、并把它登记进 `packageAllIp.tcl`？这里讲新增 submodule 的相对 URL 约定与 SSH/HTTPS 双兼容。
  - **u3-l3 端到端工作流**：把 u1 的克隆、u2 的仿真与打包串成一条完整操作手册，在真实环境里按顺序驱动整个集合仓库。

建议进入 u3 之前，回头确认你能默写出本讲 4.2.2 的打包遍历模板，并能不假思索地说出「为什么 `power_sink` 不仿真却能打包」——那是本讲最该带走的一个判断力。
