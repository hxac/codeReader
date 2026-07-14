# 获取、编译与集成到厂商工具

## 1. 本讲目标

学完本讲，你应该能够：

- 用正确的方式获取 Open Logic 源码，包括它依赖的子模块 `en_cl_fix`。
- 说清楚「单库编译策略」是什么、为什么要这样做，以及库名 `olo` 的由来。
- 看懂 `tools/<厂商>/import_sources.*` 这类导入脚本，知道它们如何把源文件挂进厂商工程、设置库与约束。
- 在从 Verilog/SystemVerilog 实例化 VHDL 实体时，规避跨语言实例化的几个已知陷阱。

本讲不涉及具体电路实现，重点是「把代码拿到手、编译进一个库、接到工具链里」这条工程链路。

## 2. 前置知识

在继续前，确认你已经了解以下概念（若不熟悉，可先看本手册 u1-l1、u1-l2）：

- **VHDL 库（library）**：VHDL 把已编译的设计单元（entity、package 等）归类放进「库」里，其他代码通过 `library olo; use olo.xxx;` 来引用。库本质上是一组已编译符号的命名空间。
- **VHDL-2008**：VHDL 的一个语言标准版本。Open Logic 全部代码都要求按 VHDL-2008 编译（例如它依赖 `ieee.math_real`）。
- **子模块（git submodule）**：把另一个独立的 git 仓库作为子目录嵌进当前仓库。克隆时若不加 `--recurse-submodules`，子目录会是空的。
- **厂商工具**：把 HDL 源码综合成比特流（bitstream）的 FPGA 厂商软件，例如 AMD/Xilinx 的 Vivado、Altera/Intel 的 Quartus、Microchip 的 Libero、高云 Gowin、安路 Efinity 等。
- **TCL**：很多 EDA 工具内置的脚本语言，厂商工程操作（加文件、设属性）大多通过 TCL 命令完成。
- **scoped constraints（带作用域的约束）**：AMD Vivado 特有的能力——把时序约束直接写在 `.tcl` 文件里并标注它作用于哪个实体，工具会自动应用；其他多数厂商工具不支持，只能手动加全局约束。

> 名词提示：下文把「把 Open Logic 源文件加进厂商工程」这个动作统称为「导入（import）」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `Readme.md` | 顶层说明，含「Get It」获取方式与「Structure」单库编译建议 |
| `.gitmodules` | 声明子模块 `3rdParty/en_cl_fix` 的路径与远程地址 |
| `compile_order.txt` | 按依赖排序的全部源文件清单（93 个文件） |
| `doc/HowTo.md` | 各厂商工具的集成步骤与跨语言实例化注意事项 |
| `tools/vivado/import_sources.tcl` | Vivado 导入脚本：加文件、设库 `olo`、导入约束 |
| `tools/quartus/import_sources.tcl` | Quartus 导入脚本：加文件、设库 `olo`、设 VHDL-2008 |
| `tools/vivado/all_constraints_amd.tcl` | Vivado 脚本末尾调用的约束聚合脚本 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 子模块获取；② 单库编译策略；③ 厂商导入脚本；④ 跨语言实例化注意事项。

### 4.1 子模块获取

#### 4.1.1 概念说明

Open Logic 自身的四个区域（`base`/`axi`/`intf`/`fix`）都是纯 VHDL 源码，但 `fix`（定点运算）区域额外依赖一个第三方库 **`en_cl_fix`**（MIT 许可）。为了不把别人的代码「复制粘贴」进自己的仓库、同时还能跟踪其版本，Open Logic 用 **git submodule** 的方式引入它，放在 `3rdParty/en_cl_fix/` 目录下。

这意味着：仅执行普通的 `git clone`，`3rdParty/en_cl_fix/` 目录会是空的，`fix` 区域无法编译。所以「获取源码」这一步必须连同子模块一起拿到。

#### 4.1.2 核心流程

Open Logic 提供了三种获取方式，适用不同场景：

```text
方式一（开发者常用）：git clone --recurse-submodules
方式二（不装 git 的用户）：从 release 页下载 CompleteSources.zip（注意：不是自动生成的归档）
方式三（用包管理器）：FuseSoC，依赖自动解析，无需手动处理子模块
```

无论哪种方式，最终都要保证 `3rdParty/en_cl_fix/hdl/` 下有真实的 `.vhd` 文件。如果该目录为空，编译到 `fix` 区域时会报「找不到 `en_cl_fix_pkg`」之类的错误。

如果你已经用普通 `git clone` 拉过了，也不用重新克隆，补救命令是：

```shell
git submodule update --init --recursive
```

#### 4.1.3 源码精读

子模块的声明写在仓库根目录的 `.gitmodules` 中，明确登记了路径与远程仓库地址：

[.gitmodules:1-3](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.gitmodules#L1-L3) — 这里登记了唯一的子模块 `3rdParty/en_cl_fix`，指向 `en_cl_fix` 的官方仓库。注意它的 URL 是 `https://github.com/open-logic/en_cl_fix.git`（Open Logic 自己维护的镜像/分支）。

README 的「Get It」段落把三种获取方式讲得很清楚：

[Readme.md:39-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L39-L54) — 开头说明仓库包含子模块、列出了子模块清单（`en_cl_fix`，MIT 许可），并强调 git clone 必须带 `--recurse-submodules` 开关。

下载归档方式的陷阱在下面这一段：

[Readme.md:56-61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L56-L61) — 从 release 页下载时必须选 `CompleteSources.zip`，因为 GitHub 自动生成的源码归档里**不含**子模块内容，会导致 `fix` 区域缺文件。

#### 4.1.4 代码实践

1. **实践目标**：确认本地工作副本里的子模块是「已检出」而非空目录。
2. **操作步骤**：
   - 在仓库根目录执行 `git submodule status`。
   - 用 `ls 3rdParty/en_cl_fix/hdl/` 查看是否能看到 `en_cl_fix_pkg.vhd` 等文件。
3. **需要观察的现象**：
   - `git submodule status` 应输出一行，前面是 commit 哈希，后面是路径 `3rdParty/en_cl_fix`；若行首是减号 `-` 或空白目录，说明子模块未初始化。
   - `hdl/` 目录里应有若干 `.vhd` 文件。
4. **预期结果**：能看到哈希且 `hdl/` 非空。若为空，执行 `git submodule update --init --recursive` 后重试。
5. 命令输出格式与具体哈希值取决于本地检出状态，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Open Logic 不直接把 `en_cl_fix` 的源码拷进自己的 `src/fix/` 目录，而要用子模块？

> **参考答案**：`en_cl_fix` 是独立项目、有自己的版本与 MIT 许可。用子模块既能跟踪其上游版本、方便升级，又能保留作者署名与许可边界，避免代码「 fork 后失联」。

**练习 2**：如果你只关心 `base` 区域，不打算用 `fix`，是否还必须初始化子模块？

> **参考答案**：如果只用 `base`（以及只依赖 `base` 的 `axi`/`intf`），理论上可以不初始化子模块；但运行官方导入脚本时它们会一并把 `3rdParty/en_cl_fix` 加进工程，若目录为空可能报错。所以即便不用 `fix`，也建议把子模块初始化好，或手动从工程中删掉 `fix` 与 `3rdParty` 相关文件。

---

### 4.2 单库编译策略

#### 4.2.1 概念说明

一个 FPGA 工程里可能有几十甚至上百个 Open Logic 文件，再加上用户自己的代码。如果把 Open Logic 的文件分散编译进多个库，引用时就要频繁写 `library xxx; use xxx.yyy;`，且要自己理清库之间的依赖。

Open Logic 的建议非常简单：**把你需要的所有区域（及其依赖）编译进同一个 VHDL 库**。这个库的名字你随便取，官方约定俗成叫 `olo`。你甚至可以把 Open Logic 的文件和用户自己的代码放进同一个库里。

#### 4.2.2 核心流程

单库策略的关键是「编译顺序」。VHDL 要求「被依赖者先编译」——package 要先于使用它的 entity。Open Logic 在仓库根目录维护了一份 `compile_order.txt`，它**按依赖关系**（而非按区域）把全部 93 个源文件排好了序：

```text
1. 先编译 base 的若干 package（如 olo_base_pkg_math / pkg_logic / pkg_array ...）
2. 再编译依赖这些 package 的 entity（pl_stage / ram / fifo ...）
3. 接着是 intf、axi 区域
4. 最后是 fix 区域：先编译 3rdParty/en_cl_fix 的 package，再编译 olo_fix_* 系列
```

只要按这份清单从上到下编译进同一个库，就不会出现「找不到符号」的问题。厂商导入脚本内部虽然不直接读这份 txt（它们靠工具自己解析依赖），但这份清单是「手工编译 / 自定义脚本」场景的权威顺序参考。

#### 4.2.3 源码精读

README 在「Structure」段落给出单库策略的官方表述：

[Readme.md:80-83](https://github.com/open-logic/open-logic/blob/ecca8af952798b8a3bc6610cb52be9688ae01/Readme.md#L80-L83) — 建议把所需区域的全部文件（连同依赖）编译进**一个** VHDL 库；库名可自选；也可以让 Open Logic 文件与用户代码共用同一个库。

`compile_order.txt` 开头是 base 区域的基础 package，正是其他一切文件的依赖根：

[compile_order.txt:1-6](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L1-L6) — 第一批就是 `olo_base_pkg_attribute`、`olo_base_pkg_array`、`olo_base_pkg_math` 等 package，它们必须先编译。

fix 区域依赖第三方库，所以在清单里能看到 `3rdParty/en_cl_fix` 的文件穿插出现：

[compile_order.txt:56-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L56-L58) — 这里先出现 `en_cl_fix_private_pkg.vhd` 与 `en_cl_fix_pkg.vhd`，紧接着才是 `olo_fix_pkg.vhd`，体现了「en_cl_fix 先于 olo_fix」的依赖顺序。

> 提示：上面这条链接里第 58 行正是 `src/fix/vhdl/olo_fix_pkg.vhd`，它 `use` 了上一行的 `en_cl_fix_pkg`，所以必须排在后面。

#### 4.2.4 代码实践

1. **实践目标**：用 `compile_order.txt` 统计各区域的文件数，直观感受「单库」里装了多少东西。
2. **操作步骤**：在仓库根目录执行下面这条命令（统计四个区域 + 第三方各自出现的行数）：

   ```shell
   for a in base axi intf fix 3rdParty; do
     echo "$a: $(grep -c "/$a/" compile_order.txt)"
   done
   ```

3. **需要观察的现象**：每行输出一个区域名和它的文件计数。
4. **预期结果**：`base` 最多（约 50 个），`fix` 与 `3rdParty` 合计约 36 个，`axi` 5 个，`intf` 6 个。总数应为 93。具体数值请以本地输出为准。
5. 若你的统计与 93 对不上，检查是否把空行或注释算进去了。

#### 4.2.5 小练习与答案

**练习 1**：如果把库命名为 `mylib` 而不是 `olo`，实例化语句该怎么写？

> **参考答案**：写成 `i_fifo : entity mylib.olo_base_fifo_sync`。库名只是命名空间标签，改名不影响实体名；只需保证实例化时 `library` / `use` 子句与编译时指定的库一致。

**练习 2**：为什么 `olo_base_pkg_math` 必须排在大部分 entity 之前？

> **参考答案**：很多 entity 在代码里 `use olo.olo_base_pkg_math.all;`，调用了其中的函数（如 `log2`）。VHDL 要求被引用的 package 已经编译进库，所以 package 必须先于使用它的 entity 编译。

---

### 4.3 厂商导入脚本

#### 4.3.1 概念说明

即便理解了「单库编译」，手动在 GUI 里把 93 个文件一个个加进工程、再统一设成库 `olo`、还要设成 VHDL-2008，既繁琐又容易出错。所以 Open Logic 为每个支持的厂商都写了一个**导入脚本**，放在 `tools/<厂商>/` 下：

- Vivado：`tools/vivado/import_sources.tcl`（TCL）
- Quartus：`tools/quartus/import_sources.tcl`（TCL）
- Questa/ModelSim：`tools/questa/vcom_sources.tcl`（TCL）
- Libero / Gowin：`tools/<厂商>/import_sources.tcl`（TCL）
- Efinity：`tools/efinity/import_sources.py`（Python）
- Yosys / oss-cad-suite：`tools/yosys/compile_olo.py`（Python）

这些脚本虽然语言和具体命令不同，但核心做三件事：① 遍历 `src/<area>/vhdl` 与 `3rdParty/en_cl_fix/hdl` 收集源文件；② 把它们都登记到库 `olo`；③ 设置正确的 VHDL-2008 标准。

#### 4.3.2 核心流程

下面以两个最具代表性的脚本说明导入流程。它们的共同骨架是：

```text
1. 由「脚本自身所在路径」反推出 Open Logic 仓库根目录 oloRoot
2. for area in {base, axi, intf, fix}:
       把 src/<area>/vhdl 下的 .vhd 加入工程，归属库 olo
3. 把 3rdParty/en_cl_fix/hdl 下的 .vhd 加入工程，归属库 olo
4. 设置语言标准为 VHDL-2008
5. （仅 Vivado）额外导入 scoped 约束
```

一个关键差异是**约束**：AMD Vivado 支持 scoped constraints，所以它的脚本会额外调用约束聚合脚本，自动把跨时钟域、接口等实体需要的时序约束挂上；Quartus、Gowin、Libero、Efinity 不支持 scoped constraints，脚本只导源码，约束要用户参照各实体文档手动添加。

#### 4.3.3 源码精读

**Vivado 脚本**先靠脚本路径定位仓库根：

[tools/vivado/import_sources.tcl:39-41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L39-L41) — `[info script]` 取到当前脚本文件，`file dirname` 得到 `tools/vivado`，再 `../..` 上溯两级就是仓库根 `oloRoot`。这样脚本无论仓库放在哪都能正确工作。

然后用一个循环把四个区域的源码一次性加进来，并设置库与文件类型：

[tools/vivado/import_sources.tcl:43-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L43-L48) — `foreach area {base axi intf fix}` 逐区 `add_files`；`set_property LIBRARY olo` 把匹配 `*olo_<area>_*` 的文件归入库 `olo`；`set_property FILE_TYPE {VHDL 2008}` 强制按 VHDL-2008 解析。注意它用通配符 `*olo_$area\_*` 来筛选本区域的文件。

第三方库走完全相同的套路：

[tools/vivado/import_sources.tcl:50-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L50-L53) — 把 `3rdParty/en_cl_fix/hdl` 加进来，同样设库 `olo` 与 VHDL-2008。这正是「单库策略」的落地：en_cl_fix 与 olo 自己的代码都在同一个库里。

脚本最后处理约束（这是 Vivado 独有的步骤）：

[tools/vivado/import_sources.tcl:68-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L68-L69) — `source .../all_constraints_amd.tcl`，该聚合脚本再去加载 `base` 与 `intf` 区域的 scoped 约束（`axi`、`fix` 没有约束）。具体可见 [tools/vivado/all_constraints_amd.tcl:1-13](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/all_constraints_amd.tcl#L1-L13)。

**Quartus 脚本**思路一致，但命令是 Quartus 的 `set_global_assignment`：

[tools/quartus/import_sources.tcl:49-56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/quartus/import_sources.tcl#L49-L56) — 同样 `foreach area {base axi intf fix}`，但用 `glob` 显式列出 `*.vhd`，再用辅助函数 `relpath` 转成相对工程目录的路径，通过 `set_global_assignment -name VHDL_FILE <path> -library olo` 逐个登记到库 `olo`。

它还在最前面强制设置语言标准：

[tools/quartus/import_sources.tcl:41-42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/quartus/import_sources.tcl#L41-L42) — `set_global_assignment -name VHDL_INPUT_VERSION VHDL_2008`，这是 Open Logic 能正常编译的前提（Gowin 的 HowTo 也特别强调要设 VHDL2008，否则 `ieee.math_real` 不被识别）。

> 注意：Quartus 脚本里**没有**任何约束相关代码，因为 Quartus 不支持 scoped constraints——这与 HowTo.md 的说明一致。

#### 4.3.4 代码实践

这是本讲的主实践，对应规格里的代码实践任务。

1. **实践目标**：通读 Vivado 导入脚本，能用自己的话描述它「如何收集源文件并设置库 `olo`」，并在一个真实厂商工具里跑通一次导入。
2. **操作步骤**：
   - 阅读 [tools/vivado/import_sources.tcl:39-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L39-L69)，在笔记里回答三个问题：
     - (a) 脚本如何不依赖任何硬编码路径就找到仓库根？
     - (b) 它把哪些目录下的文件加进了工程？分别归到哪个库？
     - (c) 脚本末尾为什么只对 `base` 和 `intf` 加载约束、却没有 `axi`/`fix`？
   - （可选，需本机装有厂商工具）在你选择的工具里新建一个空工程，打开其 TCL 控制台，执行 `source <open-logic-root>/tools/vivado/import_sources.tcl`（或对应厂商脚本）。
   - 导入完成后，在工程的源文件视图里确认：所有文件都被归到库 `olo`；在 Vivado 里还应能看到新增的约束文件。
3. **需要观察的现象**：
   - 阅读题：(a) 靠 `[info script]` + 两级 `../..`；(b) 四个区域 + `3rdParty/en_cl_fix/hdl`，全部归库 `olo`；(c) 因为 `axi`、`fix` 区域本身没有约束文件。
   - 实操题（若有工具）：导入后源文件列表里出现 `olo_base_*`、`olo_axi_*` 等文件，库列显示 `olo`。
4. **预期结果**：阅读题应能完整回答；实操题若完成，截图应显示文件归属 `olo` 库。若你手边没有厂商工具，把阅读题答完即可，并在笔记里标注「实操待本地验证」。
5. 不要声称已经运行过导入——只有当你在本机真实执行并看到结果时才算验证通过。

#### 4.3.5 小练习与答案

**练习 1**：Vivado 脚本里 `set_property LIBRARY olo [get_files -all *olo_$area\_*]`，为什么用通配符 `*olo_$area\_*` 而不是直接用目录名筛选？

> **参考答案**：`add_files` 是按目录加的，但 `set_property LIBRARY` 需要的是「文件对象」而非目录。用通配符 `*olo_base_*` 等可以精确选中该区域所有以 `olo_<area>_` 开头的文件，避免把目录里可能存在的非目标文件（或后续误放的文件）误归库。

**练习 2**：如果你只想用 `base` 和 `axi` 两个区域，导入后如何精简工程？

> **参考答案**：HowTo.md 多处都提到「不需要全部文件时，可手动删掉用不到的文件」。因此可在导入后，从工程里移除 `olo_intf_*`、`olo_fix_*` 与 `3rdParty/en_cl_fix_*` 相关文件即可（注意 `axi` 依赖 `base`，二者都要保留）。

---

### 4.4 跨语言实例化注意事项

#### 4.4.1 概念说明

Open Logic 用 VHDL-2008 编写，但官方明确支持从 **Verilog / SystemVerilog** 实例化它的实体（这是「Ease of Use」的一部分）。所谓跨语言实例化，就是在一个 Verilog 模块里直接例化一个 VHDL 实体，例如：

```verilog
olo_base_wconv_xn2n #(.InWidth_g(256), .OutWidth_g(128)) i_inst ( ... );
```

这看起来很方便，但 EDA 工具在跨语言时对「可选端口默认值」和「库名查找」的处理并不统一，踩坑很常见。Open Logic 把这些注意事项集中写在了 `doc/HowTo.md` 开头的「Use Open Logic from Verilog」一节，**强烈建议从 Verilog 使用前先读这一节**。

#### 4.4.2 核心流程

跨语言实例化要记住两件事：

```text
A. 端口默认值：不要依赖 VHDL 里声明的默认值，所有输入端口都要在 Verilog 里显式赋值。
B. 库名选择：不同工具对「Verilog 在哪个库里找 VHDL 实体」要求不同，需按工具把 Open Logic 编进正确的库。
```

关于 A：Open Logic 的很多实体有「可选端口」，在 VHDL 里这些端口带有默认值，你不连它们也能正常工作。但在跨语言时，部分工具（典型如 Vivado）会把未连接的输入端口当成 0，而不是用 VHDL 声明的默认值，导致综合结果错误。

关于 B：下表汇总自 HowTo.md 各厂商小节（库名默认都是 `olo`，仅 Verilog 场景需调整）：

| 工具 | Verilog 场景建议的库名 | 原因 |
|------|------------------------|------|
| Libero（SynplifyPro） | `work` | SynplifyPro 只在 `work` 库里找被 Verilog 实例化的 VHDL 实体 |
| Efinity | `default` | Efinity 要求顶层在 `default` 库，且 Verilog 只能引用同库的 VHDL 实体 |
| Vivado / Quartus / Gowin 等 | `olo`（一般无需特殊处理） | 实例化时显式写库名即可 |

#### 4.4.3 源码精读

HowTo.md 的「Use Open Logic from Verilog」一节开门见山地点出默认值陷阱：

[doc/HowTo.md:29-38](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L29-L38) — 明确指出「并非所有工具都能在跨语言实例化时正确应用输入端口默认值」，并以 Vivado 为例：它会把未连接端口赋成 0，而不是 VHDL 里的默认值。结论是：**从 Verilog 实例化时，不要依赖默认端口值，所有输入端口都要显式赋值**，未用到的端口也要按文档把默认值显式写出来。

紧接着是一个具体例子（`olo_base_wconv_xn2n`）：

[doc/HowTo.md:41-62](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L41-L62) — 示例里 `In_WordEna` 被显式写成 `2'b11`、`In_Last` 写成 `0`。文档特别说明：如果不显式写 `.In_WordEna(2'b11)`，即使 VHDL 里该端口默认值是 `(others => '1')`，综合也不会正确。

库名差异散落在各厂商小节，例如 Libero：

[doc/HowTo.md:107-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L107-L110) — 写明 VHDL 无需参数（默认进库 `olo`），但**从 Verilog 使用时必须传 `lib=work`**，因为 SynplifyPro 只在 `work` 库搜索被 Verilog 实例化的 VHDL 实体。

Efinity 也有类似但不同的要求：

[doc/HowTo.md:152-155](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L152-L155) — 从 Verilog 用时必须选 `default` 库，因为 Efinity 要求顶层在 `default` 库，且只允许实例化同库的 VHDL 实体。

#### 4.4.4 代码实践

1. **实践目标**：在真实源码里识别「跨语言实例化必须显式赋值」的端口，并理解默认值为什么不可靠。
2. **操作步骤**：
   - 打开 `olo_base_wconv_xn2n` 的实体声明（`src/base/vhdl/olo_base_wconv_xn2n.vhd`），找到 `In_WordEna` 端口，看它的默认值是什么。
   - 对照 HowTo.md 的 Verilog 示例（上文链接），确认示例里把 `In_WordEna` 显式写成了和默认值相同的值。
3. **需要观察的现象**：VHDL 里 `In_WordEna` 有默认值；但 Verilog 示例仍然显式赋值。
4. **预期结果**：你能说出「默认值在 VHDL 内实例化时有效，但在 Verilog 跨语言实例化时不可靠，故必须显式写」。
5. 若你顺手在 Vivado 里分别用「显式赋值」与「省略」两种写法综合一次，对比综合结果/警告差异，效果更好——但这一步**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：在 Verilog 里实例化 `olo_base_pl_stage`，发现偶尔丢数据，怀疑和默认值有关。应如何排查？

> **参考答案**：先查 `olo_base_pl_stage` 实体里哪些输入端口带默认值（尤其是流控相关、`Ready`/`Valid` 类的可选端口），在 Verilog 实例化里把它们全部显式赋成与文档默认值相同的值，再重新综合验证。

**练习 2**：为什么 Efinity 从 Verilog 用 Open Logic 时必须编译进 `default` 库，而不能用 `olo`？

> **参考答案**：因为 Efinity 要求顶层模块在 `default` 库，且它只允许 Verilog 实例化**同一个库**里的 VHDL 实体。若 Open Logic 在 `olo` 库，Verilog 顶层在 `default` 库，就跨库了，Efinity 找不到被实例化的 VHDL 实体。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从零到导入」的完整流程（可在纯命令行完成，不依赖厂商 GUI）：

1. **获取**：在一个干净目录执行 `git clone --recurse-submodules https://github.com/open-logic/open-logic.git`，克隆后用 `git submodule status` 确认子模块已初始化、`3rdParty/en_cl_fix/hdl/` 非空。
2. **理解单库**：用第 4.2.4 节的命令统计 `compile_order.txt` 各区域文件数，确认 `fix` 区域依赖了 `3rdParty/en_cl_fix`。
3. **读懂导入脚本**：通读 `tools/vivado/import_sources.tcl` 与 `tools/quartus/import_sources.tcl`，用一张表对比二者在「定位仓库根、加文件、设库、设语言标准、处理约束」五个方面分别用了什么命令、有什么差异（最显著的差异应是约束处理）。
4. **跨语言注意**：假设你要从一个 Verilog 顶层模块实例化 `olo_base_fifo_sync`，列出你需要显式赋值的端口清单（查阅该实体文档与 `doc/HowTo.md`），并说明在 Libero 下应把它编进哪个库。

完成上述四步后，你应当能用一段话向同事解释：Open Logic 怎么拿、怎么编进一个库、怎么挂进厂商工程、从 Verilog 用时要注意什么。

> 说明：第 3 步中真正在厂商工具里执行导入的部分，需要本机装有对应工具，**待本地验证**；纯源码阅读与对比部分可以离线完成。

## 6. 本讲小结

- Open Logic 的 `fix` 区域依赖第三方子模块 `en_cl_fix`，获取源码必须带 `--recurse-submodules`，或下载 `CompleteSources.zip`，否则 `fix` 缺文件。
- 官方建议把所需区域的全部文件（含依赖）编译进**同一个** VHDL 库，库名约定为 `olo`；`compile_order.txt` 给出了按依赖排序的权威编译顺序。
- 每个厂商在 `tools/<厂商>/` 下都有导入脚本，统一完成「定位仓库根 → 加四个区域 + en_cl_fix 源码 → 设库 `olo` → 设 VHDL-2008 →（仅 Vivado）导入 scoped 约束」。
- Vivado 因支持 scoped constraints 能自动应用时序约束；Quartus/Gowin/Libero/Efinity 不支持，约束需手动添加。
- 从 Verilog/SystemVerilog 实例化 VHDL 实体时，不要依赖端口默认值，所有输入端口都要显式赋值；不同工具对 Verilog 场景下的库名有不同要求（Libero 用 `work`、Efinity 用 `default`）。

## 7. 下一步学习建议

本讲解决的是「拿到代码、挂进工具」。接下来建议：

- 想立刻跑起来看效果：进入 **u1-l4 运行第一个仿真（VUnit + GHDL）**，用纯开源工具链在命令行跑通一个测试用例，无需任何厂商工具。
- 想学会读 Open Logic 的实体代码：进入 **u1-l5 编码规范与阅读一个实体**，结合 `olo_base_pl_stage.vhd` 掌握命名后缀、握手与复位约定。
- 对工具集成更深一层（FuseSoC、CI、综合测试自动化）感兴趣：可先跳到 **u10-l5 厂商集成、FuseSoC 与 CI/发布流程**，但要先具备一定的实体阅读基础。
