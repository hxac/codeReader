# IP 打包流程总览：从 RTL 到可例化 IP

## 1. 本讲目标

本讲是「项目认识与目录结构」单元的收尾。前面三讲我们已经知道：本仓库只是 Vivado IP 封装层，功能 VHDL 在上游 `psi_multi_stream_daq`，基础库在 `psi_common`。但有一件事我们还没讲清楚——**这些散落的 VHDL 文件，到底是怎么变成一个可以在 Vivado Block Design 里拖出来、双击就能配置参数的「IP-Core」的？**

这一讲就是回答这个问题。学完后你应该能够：

- 说清楚 `scripts/package.tcl` 这个脚本的「源」地位，以及它和 `component.xml`、`xgui/*.tcl` 这些「产物」的关系。
- 按顺序讲出打包一个 IP 需要经过的几个阶段（元信息 → 源/库文件 → 驱动 → GUI 参数 → 端口使能 → `package_ip`）。
- 区分 `add_sources_relative` 与 `add_lib_relative` 的不同用途。
- 看懂 `gui_create_parameter`、`gui_parameter_set_widget_dropdown`、`add_port_enablement_condition` 这些命令各自在做什么。
- 知道 `package_ip` 的产物长什么样、目标器件是什么、是否做综合。
- 判断 `xgui/` 下的 `.tcl` 文件是手工写的还是工具自动生成的（这是本讲的实践题）。

本讲**只读不写**，不修改任何源码。

## 2. 前置知识

在进入打包流程前，先建立几个最基本的概念。如果你已经熟悉 Vivado IP，可以跳过本节。

- **IP-Core（IP 核）**：一段可复用的硬件模块。在 Vivado 里，一个 IP-Core 表现为可以在 Block Design（BD，图形化连线画布）里拖拽、双击配置参数、自动生成例化代码的「盒子」。
- **IP-XACT**：一种 IEEE 标准（1685-2009）的 XML 格式，用来描述一个 IP 的元信息：它有哪些端口、哪些参数（泛型）、挂哪些总线（AXI/AXI-Stream）、依赖哪些源文件、支持哪些 FPGA 系列。Vivado 读取的 IP 描述文件就叫 `component.xml`，它就是一份 IP-XACT 文档。本仓库的 `component.xml` 开头就能看到 `xmlns:spirit="http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009"`，`spirit` 就是 IP-XACT 的旧称（SPIRIT 联盟）。
- **TCL**：Vivado 的脚本语言。本仓库所有自动化（打包、依赖拉取、重建工程）都用 TCL 写。
- **GUI 参数 vs VHDL 泛型（generic）**：用户在 Vivado 里双击 IP 看到的是「GUI 参数」（比如「Number of Streams」）；它最终要把值传给底层 VHDL 实体的「泛型」（比如 `Streams_g`）。打包脚本的一项重要工作，就是建立这两者之间的对应关系。
- **端口使能条件（enablement condition）**：一个 IP 可能有 16 路输入流，但用户只用了 3 路，剩下的 13 路端口就不应该出现在生成的 HDL 里。「端口使能条件」就是一条用参数写成的布尔表达式（如 `$Streams_g > 5`），决定某个端口是否生效。

记住一句话心智模型：**`scripts/package.tcl` 是「源代码」，`component.xml` 和 `xgui/*.tcl` 是它「编译」出来的「产物」。** 改 IP 的打包行为，永远改源（package.tcl），然后重新打包；直接改产物会被下一次打包覆盖。这条结论在 [u1-l2](u1-l2-repository-structure.md) 已经建立，本讲把它的内部机制讲透。

## 3. 本讲源码地图

本讲涉及三个文件，它们正好构成「源 → 产物」的完整链条：

| 文件 | 角色 | 说明 |
|------|------|------|
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl) | **源（人写）** | 约 189 行的打包脚本，本讲主线。它调用外部工具 `PsiIpPackage`，按顺序声明元信息、源文件、驱动、GUI 参数、端口使能，最后调用 `package_ip`。 |
| [xgui/psi_ms_daq_axi_v1_2.tcl](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl) | **产物（工具生成）** | Vivado 在 `package_ip` 时自动生成的 GUI 布局脚本，定义「哪个参数放在哪个页面、用什么控件」。文件名里的 `v1_2` 对应 IP 版本 1.2。 |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml) | **产物（工具生成）** | 约 7093 行的 IP-XACT 描述，是 Vivado 识别这个 IP 的「总入口」。包含总线接口、端口、参数、源文件清单、支持器件等全部信息。 |

> 提示：本讲会频繁引用 `package.tcl` 的行号，建议把它打开对照阅读。

## 4. 核心概念与源码讲解

### 4.1 IP 打包的心智模型：源与产物

#### 4.1.1 概念说明

「打包（packaging）」就是把一组零散的 VHDL 文件 + 一份参数/端口说明，编译成 Vivado 能识别的 IP 包目录。这个过程需要一个「打包工具」。本仓库自己不实现打包逻辑，而是调用 PSI 维护的共享工具 **PsiIpPackage**（见 [u1-l3](u1-l3-dependencies-and-sources.md) 讲过的开发期依赖）。

`scripts/package.tcl` 做的第一件事就是把外部工具「装载」进来：

[scripts/package.tcl:10-11](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L10-L11) —— `source` 外部的 `PsiIpPackage.tcl`，然后把它的所有命令导入当前命名空间。

第 10 行的相对路径 `../../../TCL/PsiIpPackage/PsiIpPackage.tcl` 说明：在 PSI 的统一目录布局里，向上退三级到达 PsiFpgaLib 根，再进入 `TCL/PsiIpPackage/`。这与 [u1-l3](u1-l3-dependencies-and-sources.md) 讲的 `add_lib_relative` 基准 `"../../.."` 是同一个根。

#### 4.1.2 核心流程

打包的整体流程可以抽象成：

```text
人写的 package.tcl（源）
        │  调用 PsiIpPackage 提供的命令
        ▼
   init → 声明源文件 → 声明驱动 → 声明 GUI 参数 → 声明端口使能 → package_ip
        │
        ▼  Vivado + PsiIpPackage 自动生成
产物目录（目标器件 xczu9eg 综合网表 + component.xml + xgui/*.tcl + ...）
```

关键认知：`package.tcl` 里调用的那些 `gui_*`、`add_*`、`add_port_enablement_condition` 命令，本身并不直接生成 `component.xml`，而是先在一个内存中的「IP 对象」上累积属性，最后由 `package_ip` 一次性把这些属性序列化成 `component.xml` 和一整套产物文件。所以 `component.xml` 是**整条流水线的最终快照**，而不是某一步的中间结果。

#### 4.1.3 源码精读

`package.tcl` 顶部「Include PSI packaging commands」一节就是装载工具：

[scripts/package.tcl:7-11](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L7-L11) —— 注释写明「Include PSI packaging commands」，两行命令完成「加载 + 导入」。

这一节之后的所有 `init`、`add_sources_relative`、`gui_create_parameter` 等命令，全部来自 `psi::ip_package::latest::*` 这个命名空间。换句话说，**`package.tcl` 的语法是 PsiIpPackage 定义的「领域专用语言（DSL）」**，而不是 Vivado 原生命令。PsiIpPackage 内部再把这些高层命令翻译成 Vivado 的底层 `ipx::` 命令。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需运行 Vivado。

1. **实践目标**：确认「源 vs 产物」的判断方法。
2. **操作步骤**：
   - 打开 `scripts/package.tcl` 第 10 行，确认它 `source` 的是一个**不存在于本仓库**的路径（`../../../TCL/PsiIpPackage/...`）。
   - 用 `git log --oneline -- xgui/psi_ms_daq_axi_v1_2.tcl` 查看 `xgui/` 下文件的提交历史。
3. **需要观察的现象**：`xgui/` 下文件是否在「手工修改 GUI」之类的提交里被多次人工编辑，还是只在打包相关提交里整文件出现/更新。
4. **预期结果**：`package.tcl` 引用的 PsiIpPackage 路径在本仓库不存在（需要开发期依赖提供）；`xgui/*.tcl` 的历史应当表现为「整文件替换」，符合「工具生成」的特征。
5. **待本地验证**：`git log` 的具体提交信息需在本地仓库实际执行后确认。

#### 4.1.5 小练习与答案

**练习 1**：如果有人直接修改了 `component.xml` 里某个参数的默认值，但没有改 `package.tcl`，下次重新打包会发生什么？

**参考答案**：改动会被覆盖。`component.xml` 是 `package_ip` 从 `package.tcl`（以及内存中的 IP 对象）重新序列化出来的产物，参数默认值的「唯一事实来源」是 `package.tcl` 里的 `gui_create_parameter` 声明（以及 Vivado 写入 `component.xml` 的 `<spirit:parameter>` 段）。要永久改默认值，必须改 `package.tcl`。

---

### 4.2 package.tcl 全文结构：一条六阶段流水线

#### 4.2.1 概念说明

`package.tcl` 虽然只有 189 行，但结构非常清晰——它被注释分成了几个段落，每段对应打包流水线的一个阶段。本节先给出「地图」，后续 4.3～4.5 节再逐段精读。

#### 4.2.2 核心流程

把 `package.tcl` 的注释段落抽出来，正好对应这条流水线：

| 阶段 | package.tcl 段落 | 关键命令 | 作用 |
|------|------------------|----------|------|
| ① 装载工具 | Include PSI packaging commands | `source` / `namespace import` | 把 PsiIpPackage 的命令引进来 |
| ② 元信息 | General Information | `init` / `set_description` / `set_logo_relative` | 给 IP 取名、定版本、挂说明 |
| ③ 源文件 | Add Source Files | `add_sources_relative` / `add_lib_relative` | 声明要综合进 IP 的 VHDL |
| ④ 驱动 | Driver Files | `file copy` / `add_drivers_relative` | 把上游 C 驱动拷到本地并打包 |
| ⑤ GUI 参数 | GUI Parameters | `gui_add_page` / `gui_create_parameter` / `gui_add_parameter` | 定义用户能配置的参数与控件 |
| ⑥ 端口使能 | Optional Ports | `add_port_enablement_condition` / `add_interface_enablement_condition` | 按参数裁剪实际出现的端口 |
| ⑦ 打包 | Package Core | `package_ip` | 生成最终产物（含综合） |

> 说明：实践题里说「5 个步骤」是对主流程的概括；把「装载工具」和「打包」算作首尾后，中间的核心声明工作正好是「元信息 → 源文件 → 驱动 → GUI 参数 → 端口使能」五大块。

#### 4.2.3 源码精读

整张地图在 `package.tcl` 里由这些注释行分隔，每段顶上都有一句 `#####...#####` 包裹的标题：

[scripts/package.tcl:27-29](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L27-L29) —— 「Add Source Files」段开始。
[scripts/package.tcl:66-68](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L66-L68) —— 「Driver Files」段开始，含一句重要 WARNING。
[scripts/package.tcl:84-86](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L84-L86) —— 「GUI Parameters」段开始。
[scripts/package.tcl:167-169](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L167-L169) —— 「Optional Ports」段开始。
[scripts/package.tcl:184-186](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L184-L186) —— 「Package Core」段，整条流水线的终点。

#### 4.2.4 代码实践

1. **实践目标**：建立「段落 → 阶段」的肌肉记忆。
2. **操作步骤**：在编辑器里折叠 `package.tcl` 的每个 `#####...#####` 段，只看段标题，自上而下抄写一遍。
3. **需要观察的现象**：段标题的顺序是否与上表一致；是否有哪个段跨越了多个职责。
4. **预期结果**：七个段一一对应上表七个阶段，顺序固定，无混杂。
5. 本实践为纯阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么「端口使能」必须放在「GUI 参数」之后，而不能放在最前面？

**参考答案**：端口使能条件是一条**引用参数**的布尔表达式（如 `$Streams_g > 5`），它依赖 `Streams_g` 这个参数已经被声明。PsiIpPackage 在处理 `add_port_enablement_condition` 时，需要能解析表达式里的参数名，所以参数必须先由 `gui_create_parameter` 建好。顺序上「参数定义」必须先于「端口使能」。

---

### 4.3 元信息、源文件与驱动声明

#### 4.3.1 概念说明

本节精读流水线的第 ②③④ 三个阶段。

- **元信息**：IP 的「身份证」——名字、版本、所属库、描述、Logo、数据手册。
- **源文件**：声明哪些 VHDL 会被综合进这个 IP。这里要区分两类：
  - 本仓库自己拥有的 RTL（只有 `hdl/psi_ms_daq_vivado.vhd` 一个），用 `add_sources_relative`。
  - 来自上游依赖库（`psi_common`、`psi_multi_stream_daq`）的 VHDL，用 `add_lib_relative`，需要额外指定一个「基准目录」。
- **驱动**：IP 不只是硬件，还附带 C 驱动，供 Vitis/XSDK 生成 BSP 时使用。驱动源码也在上游，打包时先拷贝到本地、再声明。

#### 4.3.2 核心流程

```text
元信息：  init(NAME, VERSION, REVISION, LIBRARY) → set_description → set_logo/datasheet
源文件：  add_sources_relative {本仓库 RTL}
         add_lib_relative  <基准目录>  {上游 VHDL 列表}
驱动：    file copy -force <上游 .c/.h> → <本地 drivers/...>
         add_drivers_relative <本地驱动目录> {要打包的文件}
```

`add_sources_relative` 与 `add_lib_relative` 的根本区别在于「相对谁」：

- `add_sources_relative` 的路径相对**本脚本所在目录**（`scripts/`），所以 `../hdl/psi_ms_daq_vivado.vhd` 指向仓库内的文件。
- `add_lib_relative` 第一个参数是一个**基准目录**（这里是 `"../../.."`，即 PsiFpgaLib 根），后面的文件列表都相对这个基准。所以列表里的 `VHDL/psi_common/...` 是相对 PsiFpgaLib 根的路径，指向**仓库外**的上游依赖。

#### 4.3.3 源码精读

**元信息**（第 ② 阶段）：

[scripts/package.tcl:13-25](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L13-L25) —— 设定 `IP_NAME=psi_ms_daq_axi`、`IP_VERSION=1.2`、`IP_REVISION="auto"`、`IP_LIBRARY=PSI`，再调用 `init` 注册这些信息，并用 `set_description` 写入「Mutli channel data recorder (to AXI memory)」（注：源码里 `IP_DESCIRPTION` 与 `Mutli` 均为原拼写，未修正）。

注意第 25 行 `set_datasheet_relative` 指向的是 **上游** `psi_multi_stream_daq` 的 PDF：

[scripts/package.tcl:25](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L25) —— 数据手册直接引用 `../../../VHDL/psi_multi_stream_daq/doc/psi_multi_stream_daq.pdf`。这与 [u1-l1](u1-l1-project-overview.md) 讲的「功能文档在上游」完全一致：连 IP 的说明书都是上游提供的。

**源文件**（第 ③ 阶段）：

[scripts/package.tcl:32-34](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L32-L34) —— `add_sources_relative` 声明本仓库唯一的 RTL `../hdl/psi_ms_daq_vivado.vhd`（即封装外壳）。

[scripts/package.tcl:37-64](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L37-L64) —— `add_lib_relative` 以 `"../../.."` 为基准，列出 24 个上游文件：17 个来自 `psi_common`（数组包、数学包、各种 FIFO、AXI master/slave 等），7 个来自 `psi_multi_stream_daq`（`psi_ms_daq_pkg`、`input`、`daq_sm`、`daq_dma`、`axi_if`、`reg_axi`、`axi`）。这正是 [u1-l3](u1-l3-dependencies-and-sources.md) 讲过的「24 个运行时依赖文件」的来源。

**驱动**（第 ④ 阶段）：

[scripts/package.tcl:70-82](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L70-L82) —— 这里有一段关键 WARNING 注释。驱动源码（`psi_ms_daq.c`/`.h`）的「真身」在上游 `VHDL/psi_multi_stream_daq/driver/`，打包时第 75–76 行用 `file copy -force` 把它们**强制覆盖**到本地 `drivers/psi_ms_daq_axi/src/`，然后第 79–82 行 `add_drivers_relative` 把本地副本声明进 IP。这与 [u1-l2](u1-l2-repository-structure.md) 的结论呼应：「本地 `drivers/*.c/*.h` 每次打包都会被上游同名文件覆盖」。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「源文件 vs 库文件」的相对路径基准差异。
2. **操作步骤**：
   - 找到 `add_sources_relative` 里的 `../hdl/psi_ms_daq_vivado.vhd`，确认它相对 `scripts/` 目录解析后指向 `hdl/psi_ms_daq_vivado.vhd`（本仓库内）。
   - 找到 `add_lib_relative` 的基准 `"../../.."`，确认它相对 `scripts/` 解析后是 PsiFpgaLib 根；再确认列表里第一个文件 `VHDL/psi_common/hdl/psi_common_array_pkg.vhd` 在本仓库内**不存在**（它在仓库外）。
3. **需要观察的现象**：第一类文件能在本仓库 `git ls-files` 里找到；第二类文件找不到。
4. **预期结果**：`add_sources_relative` 的 1 个文件在本仓库内；`add_lib_relative` 的 24 个文件在本仓库外（需先拉依赖）。
5. **待本地验证**：`git ls-files hdl/psi_ms_daq_vivado.vhd` 应有输出；对上游文件的 `ls` 在未拉依赖时会失败。

#### 4.3.5 小练习与答案

**练习 1**：如果上游 `psi_common` 新增了一个必需的 VHDL 文件，本仓库要怎么把它综合进 IP？

**参考答案**：在 `package.tcl` 第 37–64 行的 `add_lib_relative` 列表里新增该文件的相对路径（相对基准 `"../../.."`），然后重新运行打包。同时 README 的 Dependencies 段通常也要相应更新（参见 [u1-l3](u1-l3-dependencies-and-sources.md)）。

**练习 2**：为什么驱动要用 `file copy -force` 拷贝一份本地副本，而不是直接 `add_drivers_relative` 指向上游路径？

**参考答案**：因为打包产出的 IP 包需要**自包含**——用户拿到这个 IP 包时，未必也拉了 `psi_multi_stream_daq` 仓库。把驱动拷成本地副本再声明，能保证 IP 包里的驱动文件完整、可被 Vitis BSP 直接使用。`-force` 保证每次打包都用上游最新版覆盖，避免本地副本与上游漂移。

---

### 4.4 GUI 参数与端口使能条件

#### 4.4.1 概念说明

本节精读第 ⑤⑥ 阶段，也是 `package.tcl` 篇幅最大的部分。

- **GUI 参数定义**：每个参数用三步声明——`gui_create_parameter`（建参数并绑定到一个 VHDL 泛型名 + 显示标签）→ 可选地设控件类型/取值范围（`gui_parameter_set_widget_dropdown`、`gui_parameter_set_widget_checkbox`、`gui_parameter_set_range`）→ `gui_add_parameter`（真正加入当前页面）。页面用 `gui_add_page` 切分。
- **端口使能条件**：用 `add_port_enablement_condition` 给单个端口、`add_interface_enablement_condition` 给整个总线接口挂一条布尔表达式。表达式里用 `$参数名` 引用 GUI 参数。

#### 4.4.2 核心流程

GUI 参数声明的「三步曲」：

```text
gui_add_page "页面名"                       # 切到一个新页面（后续参数挂这里）
gui_create_parameter <泛型名> <显示标签>     # 建参数，绑定到 VHDL generic
gui_parameter_set_range <min> <max>          # 可选：数值范围
gui_parameter_set_widget_dropdown {a b c}    # 可选：下拉框枚举
gui_parameter_set_widget_checkbox            # 可选：勾选框（布尔）
gui_add_parameter                            # 提交，加入当前页面
```

端口使能的写法：

```text
add_port_enablement_condition       <端口名>  <含 $参数 的布尔表达式>
add_interface_enablement_condition  <接口名>  <含 $参数 的布尔表达式>
```

#### 4.4.3 源码精读

**通用配置页**的几个典型参数（第 ⑤ 阶段）：

[scripts/package.tcl:88-101](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L88-L101) —— 在「General Configuration」页声明 `Streams_g`（范围 1–16）、`TsPerStream_g`（勾选框）、`UseLastAsTrigger_g`（勾选框）、`MaxWindows_g`（范围 1–32）。注意每个参数名末尾的 `_g` 正是 VHDL 泛型的命名约定，PsiIpPackage 据此把 GUI 参数与泛型一一对应。

[scripts/package.tcl:107-109](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L107-L109) —— `IntDataWidth_g` 用下拉框 `{64 128 256}`，其显示标签里写明了约束 `max(Stream Data Width) <= Internal Data Width <= AXI Master Data Width`。这是给用户看的提示文本，Vivado 不会自动校验它（校验逻辑在 `xgui` 的 `validate_*` 里，但本 IP 留空了）。

**16 路流的批量声明**——这是 TCL 循环的典型用法：

[scripts/package.tcl:138-164](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L138-L164) —— 用 `for {set i 0} {$i < 16} {incr i}` 循环 16 次，每次 `gui_add_page "Stream $i"`，并在该页声明 7 个参数（`Width`/`Prio`/`Buffer`/`TimeoutUs`/`ClkFreqHz`/`TsFifoDepth`/`UseTs`）。注意 `Stream$i\Width_g` 这种写法：`\` 是为了转义 Tcl 解析，把 `Stream` + 变量 `i` + 字面量 `Width_g` 拼成 `Stream0Width_g`、`Stream1Width_g` …… 这正好与 VHDL 实体里 16 组逐流泛型对应（详见 [u2-l1](u2-l1-wrapper-entity-generics-ports.md)）。

**端口使能条件**（第 ⑥ 阶段）：

[scripts/package.tcl:171-182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L171-L182) —— 同样循环 16 次。第 172 行 `set i02 [format "%02d" $i]` 把循环变量格式化成两位（`00`、`01`……`15`），与端口名 `Str00_TData` 的零填充对齐。第 173–179 行给每路流的 6 个信号（`TData`/`Ts`/`TValid`/`TReady`/`TLast`/`Clk`）以及整个 `Str##` 接口挂条件 `$Streams_g > $i`——意思是「只有当流总数大于本路编号时，本路端口才出现」。

特别注意 `Str##_Ts`（时间戳）的条件更严格：

[scripts/package.tcl:174](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L174) —— `($Streams_g > $i) && $Stream$i\UseTs_g && $TsPerStream_g`，即「本路启用 + 本路使用时间戳 + 每流独立时间戳」三个条件同时成立，逐流时间戳端口才出现。

循环之外还有两个「互斥」端口：

[scripts/package.tcl:181-182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L181-L182) —— `StrX_Ts`（共享时间戳端口）在「非每流时间戳」时出现；`Trig`（外部触发端口）在「非 Last 作触发」时出现。这两条与第 174 行形成互斥：时间戳要么走每流 `Str##_Ts`、要么走共享 `StrX_Ts`；触发要么走 AXI-Stream 的 `TLast`、要么走外部 `Trig` 端口。这正是 [u2-l2](u2-l2-stream-array-mapping-generate.md) 将要讲的 generate 块选择逻辑的「打包层映射」。

#### 4.4.4 代码实践

1. **实践目标**：把「参数声明」与「端口使能」对照起来，理解参数如何驱动端口裁剪。
2. **操作步骤**：
   - 在 `package.tcl` 第 173 行，把 `$Streams_g > $i` 里的 `$i` 替换成具体数字，手算 `i=3`（即第 4 路流 Str03）的使能条件。
   - 假设用户在 GUI 里把 `Streams_g` 设为 3，逐路判断 Str00、Str01、Str02、Str03 的 `TData` 端口是否会出现。
3. **需要观察的现象**：当 `Streams_g=3` 时，条件 `3 > 0/1/2` 为真，`3 > 3` 为假。
4. **预期结果**：Str00、Str01、Str02 的端口出现，Str03 及之后的端口被裁掉——恰好剩 3 路。这与 README 声称「Up to 16 Streams」一致。
5. 本实践为纯手算，无需运行 Vivado。

#### 4.4.5 小练习与答案

**练习 1**：如果用户同时设 `TsPerStream_g=false` 且 `Stream0UseTs_g=true`，Str00 的时间戳端口 `Str00_Ts` 会不会出现？

**参考答案**：不会。根据第 174 行的条件 `($Streams_g > $i) && $Stream0UseTs_g && $TsPerStream_g`，`TsPerStream_g=false` 会让整个表达式为假，`Str00_Ts` 被裁掉。此时走的是第 181 行的共享端口 `StrX_Ts`（条件 `!$TsPerStream_g` 为真）。

**练习 2**：`gui_parameter_set_range 1 16` 和 `gui_parameter_set_widget_dropdown {64 128 256}` 在用户体验上有什么差别？

**参考答案**：`set_range` 给的是**连续区间**（1 到 16 任意整数都行，用户可手输），适合 `Streams_g` 这类取值多的参数；`set_widget_dropdown` 给的是**离散枚举**（只能从 64/128/256 三选一），适合 `IntDataWidth_g` 这类只有少数合法硬件实现选项的参数。下拉框能有效防止用户填入硬件不支持的值。

---

### 4.5 package_ip 终点：目标器件、综合与 IP-XACT 产物

#### 4.5.1 概念说明

流水线的最后一站是 `package_ip`。它会做三件大事：

1. 把前面累积的所有元信息、源文件、参数、端口使能序列化成 `component.xml`（IP-XACT）。
2. 生成 GUI 布局脚本 `xgui/<ipname>_v<ver>.tcl`。
3. 按指定目标器件做一次综合（可选），产出网表，让用户例化时不必从源码重新综合。

`package_ip` 的参数明确给出：是否可编辑、是否综合、目标器件型号。本节还要回答实践题的第二问：`xgui/` 下的 `.tcl` 到底是手工写的还是自动生成的。

#### 4.5.2 核心流程

```text
package_ip  <目标目录>  <Edit?>  <Synth?>  <Part>
              ".."       false    true      xczu9eg-ffvb1156-2-e
              │
              ▼
产物： ../  下生成完整 IP 包
      ├── component.xml          (IP-XACT 总描述，7093 行)
      ├── xgui/psi_ms_daq_axi_v1_2.tcl   (GUI 布局，工具生成)
      ├── 综合网表 (.ngo/.v/.xml 等，因 Synth=true)
      └── 各 view 的 fileSet 引用的源文件副本
```

#### 4.5.3 源码精读

[scripts/package.tcl:187-189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L187-L189) —— 整条流水线的终点。`TargetDir` 设为 `".."`（即仓库根，产物写回本仓库），`package_ip` 的四个参数依次是：目标目录、`Edit=false`（打包后不再可编辑回源工程）、`Synth=true`（**做综合**）、目标器件 `xczu9eg-ffvb1156-2-e`。

目标器件 `xczu9eg-ffvb1156-2-e` 是 Xilinx Zynq UltraScale+ MPSoC 的 PL（FPGA）部分型号——正是 ZCU102 开发板上的主芯片（详见 [u5-l1](u5-l1-refdesign-vivado-project.md)）。这说明这个 IP 默认是面向 ZCU102 这类器件打包并预综合的。

**产物之一：`xgui/psi_ms_daq_axi_v1_2.tcl`** —— 这个文件**是 Vivado 在 `package_ip` 时自动生成的**，不是手写的。证据有三：

1. **文件名编码版本**：`v1_2` 来自 `IP_VERSION=1.2`，文件名由工具按版本拼出。
2. **内容是死板的重复模式**：其 `init_gui` proc（[xgui/psi_ms_daq_axi_v1_2.tcl:2-182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl#L2-L182)）把 `package.tcl` 第 ⑤ 阶段用循环写的 16 路参数，**展开**成了 16 段几乎逐字重复的 `ipgui::add_page` / `ipgui::add_param`——这是典型的「生成器输出」，而非人写风格。
3. **大量空桩函数**：文件后半（[xgui/psi_ms_daq_axi_v1_2.tcl:184-1298](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl#L184-L1298)）为每个参数生成了一对空的 `update_PARAM_VALUE.X` / `validate_PARAM_VALUE.X` 桩函数（注释写着「Procedure called to update/validate ...」但函数体为空），这是 Vivado IP-XACT 打包器的标准模板，留作日后手写校验逻辑的占位。

其中真正有逻辑的是 `update_MODELPARAM_VALUE.X` 系列（[xgui/psi_ms_daq_axi_v1_2.tcl:1301-1319](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl#L1301-L1319)）——它们把 GUI 参数值（`PARAM_VALUE.Streams_g`）赋给 VHDL 泛型（`MODELPARAM_VALUE.Streams_g`），实现了「GUI 参数 → VHDL 泛型」的桥接。这恰好印证了 [前置知识](#2-前置知识) 里讲的两者关系。

**产物之二：`component.xml`（IP-XACT 总入口）** —— 它的结构与本讲流水线一一对应：

| component.xml 区段 | 行号区间 | 对应 package.tcl 的阶段 |
|---------------------|----------|--------------------------|
| 头部 `vendor/library/name/version` | [component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L3-L6) | ② 元信息（`psi.ch` / `PSI` / `psi_ms_daq_axi` / `1.2`） |
| `busInterfaces`（含 enablement 依赖） | [component.xml:7-55](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L7-L55) 等 | ⑥ 端口/接口使能（如 `$Streams_g > 0`） |
| `model > views`（synthesis/sim/xgui/driver…） | [component.xml:1801-1891](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1801-L1891) | ⑦ package_ip（定义各视图） |
| `modelParameters`（HDL 泛型） | [component.xml:5452-6073](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L5452-L6073) | ⑤ GUI 参数对应的泛型 |
| `fileSets`（源文件清单） | [component.xml:6116-6245](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6116-L6245) | ③ 源文件（24 上游 + 1 本地） |
| `parameters`（GUI 参数默认值） | [component.xml:6421-7047](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6421-L7047) | ⑤ GUI 参数默认值 |
| `vendorExtensions`（支持器件/taxonomy） | [component.xml:7048-7092](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L7048-L7092) | ⑦ package_ip 元数据 |

举两个具体例子来验证「源 → 产物」的映射：

- 第 50 行 `Str00` 接口的使能条件 `<xilinx:dependency="$Streams_g > 0">`（[component.xml:47-53](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L47-L53)），正是 `package.tcl` 第 179 行 `add_interface_enablement_condition "Str00" "$Streams_g > 0"` 的序列化结果。
- `fileSets` 里 24 个上游文件 + `hdl/psi_ms_daq_vivado.vhd`（[component.xml:6240-6244](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6240-L6244)），正是 `package.tcl` 第 32–64 行两个 `add_*_relative` 调用的序列化结果；每个文件都带 `logicalName=psi_ms_daq_axi_1_2`，说明它们被归进同一个 VHDL 库。

`vendorExtensions` 里还能看到打包环境信息：`supportedFamilies` 列出从 spartan7 到 zynquplus 的一众 7 系/U 系器件（[component.xml:7050-7071](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L7050-L7071)），`taxonomy` 是 `/UserIP`（[component.xml:7072-7074](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L7072-L7074)，即 Vivado IP 目录里这个 IP 出现在「UserIP」分类下），打包用的 Vivado 版本是 `2022.2.1`（[component.xml:7083](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L7083)）。

#### 4.5.4 代码实践

1. **实践目标**：回答实践题第二问——`xgui/*.tcl` 是手写还是自动生成；并验证 `package_ip` 的目标器件。
2. **操作步骤**：
   - 打开 `xgui/psi_ms_daq_axi_v1_2.tcl`，对比它的 `init_gui`（第 5–180 行的 16 段 `Stream_X`）与 `package.tcl` 第 138–164 行的 `for` 循环，看后者是不是前者的「紧凑源」。
   - 在 `package.tcl` 第 189 行确认目标器件字符串。
   - 在 `component.xml` 第 6240 行附近，确认本地 RTL 文件 `hdl/psi_ms_daq_vivado.vhd` 出现在综合 fileSet 里。
3. **需要观察的现象**：`xgui` 的 16 段是否是 `package.tcl` 16 次循环的「展开」；`component.xml` 是否包含本地 RTL 条目。
4. **预期结果**：`xgui/*.tcl` 是 Vivado 在 `package_ip` 时**自动生成**的（`package.tcl` 的循环是它的源）；目标器件是 `xczu9eg-ffvb1156-2-e`（ZCU102 主芯片）。
5. 本实践为纯阅读对照，无需运行 Vivado。

#### 4.5.5 小练习与答案

**练习 1**：`package_ip` 的 `Synth=true` 对最终用户有什么实际好处？

**参考答案**：打包时已经针对 `xczu9eg` 做了一次综合，产出了预编译网表。当用户在自己的工程里例化这个 IP 时，Vivado 可以直接复用这份网表而不必从 VHDL 源码重新综合，从而缩短用户工程的构建时间，也保证 IP 内部实现的一致性（用户不会误改 IP 内部逻辑）。代价是 IP 包体积更大，且面向具体器件族。

**练习 2**：`xgui/psi_ms_daq_axi_v1_2.tcl` 文件名里的 `v1_2` 如果改成 `v2_0`，会发生什么？

**参考答案**：文件名是工具按 `IP_VERSION` 自动拼出来的（`1.2` → `v1_2`）。如果只手改文件名而不改 `package.tcl` 里的 `IP_VERSION`，下次打包会**重新生成**一个名为 `psi_ms_daq_axi_v1_2.tcl` 的文件（因为版本仍是 1.2），手改的 `v2_0` 文件会变成孤儿、不被引用。要让改名生效，必须改 `package.tcl` 第 17 行的 `IP_VERSION`，重新打包后工具自然会生成与新版本号匹配的 `xgui` 文件。

---

## 5. 综合实践

把本讲的「源 → 产物」链条串起来，完成下面这个**追踪任务**（纯源码阅读，无需 Vivado）：

**任务**：追踪 GUI 参数 `Streams_g` 从「声明」到「落地为 IP-XACT」的完整路径。

1. 在 `scripts/package.tcl` 找到 `Streams_g` 的 GUI 声明（提示：[L91-93](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L91-L93)），记录它的显示标签、取值范围。
2. 在 `xgui/psi_ms_daq_axi_v1_2.tcl` 找到 `Streams_g` 的 GUI 布局位置（提示：[L6](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl#L6)），确认它挂在 `General Configuration` 页下；再找到它的 `update_MODELPARAM_VALUE.Streams_g`（提示：[L1301-1304](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/xgui/psi_ms_daq_axi_v1_2.tcl#L1301-L1304)），看清它如何把 GUI 值赋给 VHDL 泛型。
3. 在 `component.xml` 里找到三处 `Streams_g` 的踪迹：
   - 作为 `modelParameter`（HDL 泛型声明，在 [L5452-6073](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L5452-L6073) 区段内）。
   - 作为 `parameter`（GUI 参数默认值，在 [L6421-7047](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6421-L7047) 区段内）。
   - 作为端口使能依赖 `$Streams_g > N`（如 [Str00 接口的 L50](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L50)）。
4. 用一句话总结：`Streams_g` 这一个名字，在三个文件里分别扮演了哪三种角色？

**预期结论**：`Streams_g` 在 `package.tcl` 是**人写的声明（源）**，在 `xgui/*.tcl` 是**布局与泛型桥接（生成代码）**，在 `component.xml` 是**三处序列化投影**（泛型定义、GUI 默认值、端口使能依赖）。这正好印证本讲核心论点：`package.tcl` 是唯一事实来源，其余两个文件都是它的产物投影。

> **待本地验证**：`component.xml` 里 `Streams_g` 三处出现的精确行号，建议用编辑器搜索功能在本地确认（该文件 7093 行，不同 Vivado 版本打包可能略有偏移）。

## 6. 本讲小结

- `scripts/package.tcl` 是 IP 打包的**唯一事实来源（源）**，`component.xml` 和 `xgui/*.tcl` 都是 `package_ip` **自动生成的产物**——改 IP 行为永远改源，改产物会被覆盖。
- `package.tcl` 是一条清晰的流水线：**装载工具 → 元信息 → 源/库文件 → 驱动 → GUI 参数 → 端口使能 → `package_ip`**，由 `#####...#####` 注释分段。
- `add_sources_relative` 声明本仓库 RTL（相对 `scripts/`），`add_lib_relative` 声明上游依赖 VHDL（相对 PsiFpgaLib 根 `"../../.."`）；两者基准不同，是本仓库「封装层」定位的直接体现。
- 驱动源码在上游，打包时用 `file copy -force` 强制覆盖本地副本再声明，保证 IP 包自包含——这就是为什么本地 `drivers/*.c/*.h` 每次打包都被覆盖。
- GUI 参数用「`gui_create_parameter` → 设控件 → `gui_add_parameter`」三步曲声明，参数名末尾 `_g` 与 VHDL 泛型一一对应；16 路流参数用 `for` 循环批量声明。
- 端口使能条件是引用参数的布尔表达式（如 `$Streams_g > $i`），决定哪些端口/接口在用户配置下实际出现；时间戳与触发端口存在互斥关系。
- `package_ip` 的目标是器件 `xczu9eg-ffvb1156-2-e`（ZCU102），`Synth=true` 表示打包时已预综合；`xgui/psi_ms_daq_axi_v1_2.tcl` 由工具按版本号自动生成，文件名 `v1_2` 对应 `IP_VERSION=1.2`。

## 7. 下一步学习建议

本讲把「打包流程」的整体框架讲清楚了，但有意回避了 RTL 内部细节。接下来推荐：

1. **进入单元 2（VHDL 封装层源码精读）**，从 [u2-l1 封装实体：泛型与端口全景](u2-l1-wrapper-entity-generics-ports.md) 开始，看清 `package.tcl` 里那些 `_g` 泛型在 `hdl/psi_ms_daq_vivado.vhd` 的 entity 里到底长什么样、为什么 16 路流要展开成 16 组泛型。
2. 在读 u2 系列时，随时回看本讲的 GUI 参数声明与端口使能条件——你会发现 VHDL 实体、`package.tcl` 声明、`component.xml` 序列化三者严格一一对应，这种「三处投影」的对称性是理解整个封装层的关键。
3. 如果你对 IP-XACT 标准本身感兴趣，可以跳到 [u5-l3 IP-XACT 描述与驱动 BSP 集成](u5-l3-ipxact-and-bsp-integration.md)，那里会讲 `component.xml` 如何驱动 Vitis BSP 的生成（`.mdd` / `.tcl` / `xparameters.h`）。
