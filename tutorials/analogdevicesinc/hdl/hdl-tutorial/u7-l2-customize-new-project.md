# 创建与定制新工程

> 单元 u7 · 第 2 讲（u7-l2）· 进阶/实战层
> 依赖：[u7-l1 移植工程到新载板](u7-l1-porting-project.md)、[u3-l2 工程构建 Makefile 内部：project-xilinx.mk](u3-l2-project-build-makefile.md)

## 1. 本讲目标

本讲解决一个非常实际的问题：**「我想基于 ADI 的现成 IP 和 base design，从零搭出一个属于自己的评估板工程，该写哪些文件、怎么填 Makefile、怎么写 README、怎么让一个工程支持多种配置？」**

学完后你应当能够：

- 说出一个标准 Xilinx 工程目录必须包含哪些文件，以及每个文件的职责。
- 独立填写工程 `Makefile` 中的 `PROJECT_NAME`、`LIB_DEPS`、`M_DEPS` 三类字段。
- 理解 ADI 的「参数化（CFG）」机制：命令行变量 `make VAR=val` 与配置文件 `make CFG=file.mk` 两条路径如何把参数透传到 Tcl、并把不同配置的构建产物隔离开。
- 根据「是否有 make 参数」为工程挑选正确的 README 模板，并正确填写控制 GitHub Action 行为的隐藏 flag。

本讲全部基于真实源码，引用行号均对应当前 HEAD `e57851ff`。

## 2. 前置知识

本讲承接 u7-l1（移植）与 u3-l2（构建 Makefile 内部）。在进入正文前，请确认你已经理解以下几点（这些是前序讲义的结论，本讲不再重复推导）：

1. **三层工程架构**：每个参考设计由「载板 base design（第一层）+ 评估板 base design（第二层）+ 系统特化（第三层）」叠加而成，第三层入口 `system_bd.tcl` 铁律是「先 source 载板、再 source 评估板」。
2. **工程标准五件套**：`Makefile`（声明依赖）、`system_project.tcl`（建工程跑综合）、`system_bd.tcl`（搭块设计）、`system_constr.xdc`（引脚/时序约束）、`system_top.v`（综合顶层，例化 `system_wrapper`）。
3. **`project-xilinx.mk` 的依赖驱动模型**：工程 `Makefile` 只负责「报菜名」——用 `LIB_DEPS` 列出要用的 library IP、用 `M_DEPS` 列出要直接引用的文件，公共脚本把每个 `LIB_DEPS` 翻译成 `library/<ip>/component.xml` 目标，最终产物是 `system_top.xsa`。
4. **构建入口**：在工程目录下敲 `make`，最终会执行 `vivado -mode batch -source system_project.tcl`。

如果你对上面任何一条感到陌生，建议先回到 u2-l1、u2-l2、u3-l2 复习。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `docs/user_guide/customize_hdl.rst` | 官方「定制 HDL 工程」导览页，列出所有定制主题的入口 |
| `docs/user_guide/ip_cores/use_adi_ips.rst` | 在自己的工程里复用 ADI IP 的官方指南（Vivado IP 仓库、JESD204/SPI Engine 拼装） |
| `projects/fmcomms2/zcu102/Makefile` | **标准非参数化**工程 Makefile 的范本 |
| `projects/fmcomms2/zcu102/system_project.tcl` | 对应的 Vivado 入口脚本，展示「准备 + 三行核心 + 特化」结构 |
| `projects/cn0506/zed/Makefile` + `system_project.tcl` | **参数化**工程范本，演示 `make INTF_CFG=...` 如何生效 |
| `projects/scripts/project-xilinx.mk` | 参数化机制（CFG/CMD_VARIABLES/DIR_NAME）的真实实现 |
| `projects/common/README.md` | README 模板使用规则总说明 |
| `projects/common/template_readme_evalboard.md` | 评估板层 README 模板 |
| `projects/common/template1_readme_carrier.md` | 载板层 README 模板①：无 make 参数 |
| `projects/common/template2_readme_carrier.md` | 载板层 README 模板②：有 make 参数 |
| `projects/common/template3_readme_carrier.md` | 载板层 README 模板③：固定配置不可改 |
| `docs/user_guide/docs_guidelines.rst` | 文档（含 projects 文档）撰写与构建规范 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**① 新工程文件骨架**、**② CFG 参数化机制**、**③ README 与文档模板**。

### 4.1 新工程文件骨架

#### 4.1.1 概念说明

「新建一个评估板工程」并不是从空白目录开始写 RTL，而是**复用已有的三层架构与现成 IP，只补齐第三层（系统特化）那几个文件**。本质上你在做两件事：

- **拼装**：把评估板子卡上的器件（ADC/DAC/收发器…）对应的 ADI library IP，在 `system_bd.tcl` 里用 `ad_ip_instance` / `ad_connect` 等 Tcl 原语连成数据通路。
- **声明**：在 `Makefile` 里告诉构建系统「我用了哪些 IP（`LIB_DEPS`）、我直接引用了哪些文件（`M_DEPS`）」。

因此一个最小 Xilinx 工程目录通常长这样（以评估板 `myadc` + 载板 `zcu102` 为例）：

```
projects/myadc/zcu102/
├── Makefile               # 声明依赖（PROJECT_NAME / LIB_DEPS / M_DEPS）
├── system_top.v           # 综合顶层：例化 system_wrapper + IO 缓冲
├── system_constr.xdc      # 评估板相关引脚/电平/时钟约束
├── system_bd.tcl          # 系统特化：source 载板 + source 评估板 + 微调
└── system_project.tcl     # 流程脚本：adi_project → adi_project_files → adi_project_run
```

> 说明：载板 base design（`projects/common/zcu102/...`）与评估板 base design（`projects/myadc/common/myadc_bd.tcl`）通常已经存在或可从兄弟工程复制；本模块聚焦第三层那五个文件。

#### 4.1.2 核心流程

新建一个工程的典型步骤（伪代码）：

```
1. 在 projects/<eval>/<carrier>/ 下建立目录
2. 复制一个最相似的兄弟工程（同载板或同器件）的五个文件作为骨架
3. 改 Makefile：
     PROJECT_NAME := <eval>_<carrier>
     LIB_DEPS += <用到的每个 library IP>
     M_DEPS  += <本工程直接引用的 bd/constr/tcl/verilog>
     include ../../scripts/project-xilinx.mk
4. 改 system_top.v：把 system_wrapper 例化好，处理物理引脚与 IO 缓冲
5. 改 system_constr.xdc：分配引脚（PACKAGE_PIN）、电平（IOSTANDARD）、时钟周期
6. 改 system_bd.tcl：先 source 载板 base design，再 source 评估板 base design，最后做组合级微调
7. 改 system_project.tcl：source 三个公共脚本 → adi_project → adi_project_files → adi_project_run
8. make
```

#### 4.1.3 源码精读

**范本一：`fmcomms2/zcu102`（非参数化，最简形态）**

工程 Makefile 只做三件事——声明工程名、报依赖、include 公共脚本：

[projects/fmcomms2/zcu102/Makefile:7-27](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L7-L27) —— 注意三段结构：

- `PROJECT_NAME := fmcomms2_zcu102`（第 7 行）：工程名，遵循 `<eval>_<carrier>` 命名，构建产物目录与日志都以此为前缀。
- `M_DEPS += ...`（第 9–14 行）：本工程**直接引用**的文件，含评估板 base design（`../common/fmcomms2_bd.tcl`）、载板约束与 base design（`../../common/zcu102/...`）、延时校准脚本（`axi_ad9361_delay.tcl`）等。
- `LIB_DEPS += ...`（第 16–25 行）：本工程**用到的 library IP 名字**，共 10 个，如 `axi_ad9361`、`axi_dmac`、`util_pack/util_cpack2`。公共脚本会把每个名字翻译成 `library/<ip>/component.xml` 依赖（详见 u3-l2）。
- `include ../../scripts/project-xilinx.mk`（第 27 行）：接入公共构建逻辑。

对应的 Vivado 入口脚本同样极简，是「准备 + 三行核心 + 特化」结构：

[projects/fmcomms2/zcu102/system_project.tcl:6-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L6-L23) —— 它做了：

1. source 三个公共脚本（`adi_env.tcl`、`adi_project_xilinx.tcl`、`adi_board.tcl`）。
2. 设 `ADI_POST_ROUTE_SCRIPT` 指向 `auto_timing_fix_xilinx.tcl`（布线后自动时序修复）。
3. `adi_project fmcomms2_${BOARD_NAME}`（建工程 + 搭块设计）。
4. `adi_project_files ...`（加源码与约束）。
5. 一行特化：把 `impl_1` 的策略设为 `Congestion_SpreadLogic_high` 以缓解 hold 违例。
6. `adi_project_run ...`（综合 + 实现 + 出比特流）。

> 这四个 `adi_project*` 过程的内部封装见 u3-l3；本讲你只需知道**调用顺序固定**即可。

**范本二：`cn0506/zed`（参数化工程，文件更多）**

当工程支持多种配置时，`system_top` 会拆成多个变体文件，`system_project.tcl` 按参数选择其一：

[projects/cn0506/zed/Makefile:7-27](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/Makefile#L7-L27) —— 与 fmcomms2 同样的三段式，但 `M_DEPS` 同时引用了 `rgmii_bd.tcl` 与 `mii_bd.tcl` 两套评估板 base design（不同接口模式用不同块设计）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：通过对比两个范本，建立「文件骨架与 Makefile 字段的对应感」。

**步骤**：

1. 打开 [projects/fmcomms2/zcu102/Makefile:7-27](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L7-L27)，数一数 `LIB_DEPS` 列了几个 IP、`M_DEPS` 列了几个文件。
2. 打开 [projects/cn0506/zed/Makefile:7-27](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/Makefile#L7-L27)，对比它与 fmcomms2 的差异：cn0506 多引用了哪些 IP（提示：以太网相关）？
3. 打开 [projects/fmcomms2/zcu102/system_project.tcl:12-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L12-L23)，确认 `adi_project → adi_project_files → adi_project_run` 的调用顺序，并指出 `BOARD_NAME` 变量在第几行被赋值。

**需要观察的现象**：两个工程的 `Makefile` 结构完全一致（三段式），差异只在「依赖列表的内容」；`system_project.tcl` 的核心都是同样三行调用，差异只在前后特化。

**预期结果**：你能不查文档说出「改一个新工程，最少要动 Makefile 的哪三个字段」。

> 实际执行 `make` 需要 Vivado 工具链与硬件，本环境无法运行，相关命令标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果新工程用到了一个名为 `axi_ad4000` 的 ADI IP，应该在 `Makefile` 的哪个字段加一行？怎么写？

**参考答案**：加在 `LIB_DEPS`，写作 `LIB_DEPS += axi_ad4000`。`LIB_DEPS` 只填 IP 名字（不含路径），公共脚本会自动拼成 `library/axi_ad4000/component.xml` 依赖。

**练习 2**：`M_DEPS` 与 `LIB_DEPS` 的区别是什么？

**参考答案**：`M_DEPS` 收集本工程**直接引用的文件**（带相对路径，如 `../common/xxx_bd.tcl`、`../../common/zcu102/zcu102_system_constr.xdc`）；`LIB_DEPS` 收集**用到的 library IP 名字**（不带路径）。前者是「文件依赖」，后者是「IP 依赖（会被翻译成打包产物 component.xml）」。

---

### 4.2 CFG 参数化机制

#### 4.2.1 概念说明

很多 ADI 评估板支持多种工作配置——例如 CN0506 的以太网接口可以是 MII / RGMII / RMII，AD9081 的 JESD204 链路有不同的 M/L/S 组合。如果每种配置都复制一份完整工程目录，维护成本会爆炸。

ADI 的解决方案是 **参数化（parameterization）**：保持同一份源码，通过 `make` 命令传入参数来切换配置，并把**不同配置的构建产物隔离开**，互不覆盖。

参数透传的物理链路是：

```
make VAR=val              ← 你在命令行传参
   ↓ GNU Make 把命令行变量自动 export 到 recipe 的环境
vivado（子进程）          ← 继承了环境变量 VAR
   ↓ Tcl 读取 $::env(VAR)
system_project.tcl        ← 用参数选择不同的块设计/顶层/约束
```

也就是说，**参数并不是 Make 自己消费的，而是 Make 把它「搬运」到环境变量，再由 Tcl 读取**。理解这一点，就能看懂所有参数化工程。

#### 4.2.2 核心流程

`project-xilinx.mk` 中参数化分两条路径，最终都汇入同一个「产物隔离」机制：

```
路径 A（命令行）：make INTF_CFG=MII
   CMD_VARIABLES = "INTF_CFG=MII"
   → 把 '=' 换成 '_'  → PARAMS = "INTF_CFG_MII"
   → GEN_SED 去掉 JESD/LANE 字样、去掉下划线 → GEN_NAME = "INTFCFGMII"
   → DIR_NAME = GEN_NAME（若无 CFG）

路径 B（配置文件）：make CFG=my_config.mk
   include $(CFG)                 # 变量成为 Make 变量
   export <所有变量名>             # 导出到环境，供 Tcl 读取
   DIR_NAME = my_config           # 取配置文件主名

两条路径汇合：
   if DIR_NAME 非空：
       PROJECT_NAME := $(DIR_NAME)/$(PROJECT_NAME)   # 产物进子目录
       mkdir $(DIR_NAME)
       VIVADO 日志/临时目录都重定向进 $(DIR_NAME)/
```

关键直觉：**`DIR_NAME` 是「配置指纹」**。每传一组参数，就生成一个独立子目录存放该配置的全部产物（`.xsa`、日志、`.runs` 等），所以 `make INTF_CFG=MII` 与 `make INTF_CFG=RGMII` 的产物不会互相覆盖。

#### 4.2.3 源码精读

**机制实现：`project-xilinx.mk` 的参数解析块**

[projects/scripts/project-xilinx.mk:12-43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L12-L43) —— 这一段是整个参数化机制的核心，逐段读：

- 第 13–14 行：`PARAMS_REPLACE_LIST := JESD LANE` 定义了一组「在生成目录名时要剔除的词」。因为 JESD 参数组合极多（`RX_JESD_M`、`TX_JESD_L`…），若全保留会让目录名长得不可读，所以生成 `DIR_NAME` 时把 `JESD`、`LANE` 这两个词整体抹掉。
- 第 15–20 行（路径 B，`ifdef CFG`）：若用 `make CFG=file.mk`，则 `include` 该文件、用 `sed` 提取所有变量名并 `export` 到环境、把文件内容读进 `PARAMS`、用文件主名作 `DIR_NAME`。
- 第 25–29 行：用 `$(MAKELEVEL) % 2` 区分递归层级，捕获命令行变量（`$(-*-command-variables-*-)` 或 `$(MAKEOVERRIDES)`）。
- 第 31–35 行（路径 A）：把命令行变量串里的 `=` 换成 `_` 得 `PARAMS`，再用 `GEN_SED`（剔除 JESD/LANE、去下划线）生成紧凑的 `GEN_NAME`，拼到 `DIR_NAME` 上。
- 第 37–43 行（产物隔离）：若 `DIR_NAME` 非空，把 `PROJECT_NAME` 改成 `$(DIR_NAME)/$(PROJECT_NAME)`，`mkdir` 建子目录，并把 Vivado 的 `tempDir`、`log`、`journal` 都重定向进该子目录。

> 注意：路径 A（命令行）里**没有显式 `export`**——这是因为 GNU Make 默认就会把「来自命令行的变量」放进子进程环境。这正是 Tcl 端能用 `$::env(INTF_CFG)` 读到它的原因。路径 B 走文件，文件里定义的变量不会被自动导出，所以需要显式 `export`。

**Tcl 端消费参数：`cn0506/zed/system_project.tcl`**

[projects/cn0506/zed/system_project.tcl:29-60](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/system_project.tcl#L29-L60) —— 这是「读取-默认-回填」惯用法的标准写法：

```tcl
set intf RGMII                       ;# 默认值
if {[info exists ::env(INTF_CFG)]} {  ;# 命令行传了就用传的
  set intf $::env(INTF_CFG)
} else {
  set env(INTF_CFG) $intf            ;# 没传就把默认值回填进环境
}
```

随后第 47–60 行用 `switch $intf` 选择不同的 `system_top_*.v` 顶层文件加入工程。脚本顶部注释把这套写法称为 `get_env_param` 过程的用法（本工程以内联形式实现；多数 ADI 工程沿用同一惯用法）。

**README 端记录参数：`cn0506/zed/README.md`**

[projects/cn0506/zed/README.md:17-19](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/README.md#L17-L19) 明确写出可覆盖参数 `INTF_CFG` 及其取值 MII/RGMII/RMII，并给出每种配置的 `make` 命令示例。这是参数化工程的文档约定（详见 4.3）。

#### 4.2.4 代码实践（源码阅读型 + 待本地验证）

**目标**：亲手追踪一次 `make INTF_CFG=MII` 的参数流转。

**步骤**：

1. 打开 [projects/cn0506/zed/system_project.tcl:29-39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/system_project.tcl#L29-L39)，确认 Tcl 是从 `$::env(INTF_CFG)` 读参数。
2. 打开 [projects/scripts/project-xilinx.mk:31-43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L31-L43)，按 `make INTF_CFG=MII` 手算 `GEN_NAME`：`INTF_CFG=MII` → 去 `=` → `INTF_CFG_MII` → 去 `JESD`/`LANE`（无影响）→ 去下划线 → `INTFCFGMII`。所以产物应落在 `INTFCFGMII/` 子目录。
3. （待本地验证）若本地装了 Vivado，进入 `projects/cn0506/zed` 执行 `make INTF_CFG=MII`，观察是否生成 `INTFCFGMII/` 目录及其中的 `vivado.log`。

**需要观察的现象**：默认 `make`（不传参）时 `DIR_NAME` 为空，产物直接落在工程目录；传参后产物落在参数派生的子目录里，且 Tcl 日志中 `intf` 变量值应等于传入值。

**预期结果**：你能在不运行的情况下，根据命令行参数预测出构建产物目录名。

#### 4.2.5 小练习与答案

**练习 1**：为什么路径 A（命令行变量）在 `project-xilinx.mk` 里没有写 `export`，而路径 B（`CFG` 文件）却显式 `export`？

**参考答案**：GNU Make 默认会把「命令行传入的变量」自动放入子进程环境，所以路径 A 无需显式导出；而 `CFG` 文件里用 `KEY=value` 定义的变量属于 Makefile 内变量，默认不导出，必须显式 `export` 才能让 Tcl 通过 `$::env()` 读到。

**练习 2**：运行 `make JESD_MODE=8B10B RX_JESD_L=4`（仿 AD9081 风格），`DIR_NAME` 里的 `JESD`、`LANE` 字样会发生什么？为什么这样设计？

**参考答案**：会被 `GEN_SED` 整体抹掉（`PARAMS_REPLACE_LIST := JESD LANE`）。因为 JESD 参数组合极多，全保留会让目录名过长不可读；这些词对「区分配置」并非必需（值本身已留在名字里），所以生成目录名时剔除。

**练习 3**：如果一个工程同时支持「软件可切换的小配置」和「综合期固定的硬配置」，本讲的 CFG 机制处理的是哪一种？

**参考答案**：CFG 处理的是**综合期（build-time）配置**——不同参数会生成不同的比特流/`.xsa`，是「硬件级」切换，不是运行时软件改寄存器就能切的。

---

### 4.3 README 与文档模板

#### 4.3.1 概念说明

ADI 对工程的 README 有**强约束**：每个评估板在 `projects/<eval>/README.md` 放一份评估板级 README，每个载板工程在 `projects/<eval>/<carrier>/README.md` 放一份载板级 README，且必须按官方模板填写。

这套约束不是「写给人看」那么简单——README 第一行藏着机器可读的 **flag 注释**，会被 GitHub Action 脚本读取来决定 CI 行为。写错 flag 会导致 CI 报错。因此「会挑模板、会填 flag」是贡献新工程的必备技能。

#### 4.3.2 核心流程

挑选与填写 README 的决策树：

```
评估板层 projects/<eval>/README.md
   → 永远用 template_readme_evalboard.md
   → 内容：产品页链接、系统文档链接、HDL 项目文档链接、VADJ 范围、支持的器件表

载板层 projects/<eval>/<carrier>/README.md
   → 工程有没有 make 参数？
        ├─ 没有 → template1_readme_carrier.md
        ├─ 有   → template2_readme_carrier.md（要列出所有可覆盖参数 + 示例配置）
        └─ 固定不可改 → template3_readme_carrier.md（列出固化配置）

任何载板 README 第一行：
   <!-- Put flags here, i.e. no_build_example, no_dts, no_no_os -->
   按需保留：no_build_example / no_dts / no_no_os
```

#### 4.3.3 源码精读

**总规则：`projects/common/README.md`**

[projects/common/README.md:1-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/README.md#L1-L44) —— 这份文件不是某个工程的 README，而是「README 模板的使用说明书」。关键规则：

- 评估板 README 标题必须是评估板文件夹名；系统文档链接若暂缺可用占位符 `"to be added"`（这是唯一允许的占位）。
- 载板 README 有三个模板，按「是否有 make 参数 / 是否固定配置」三选一。
- 第一行必须是 Markdown 注释形式的 flag，**渲染后不可见**，供 GitHub Action 读取。
- 必须列出所有可从环境覆盖的参数及其含义与取值；JESD 参数则给出数据手册链接，并列出「有对应 Linux 设备树」的所有配置。

**评估板模板：`template_readme_evalboard.md`**

[projects/common/template_readme_evalboard.md:1-16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template_readme_evalboard.md#L1-L16) —— 结构：标题 → 产品页/系统文档/HDL 文档三条链接 → VADJ 范围 → 支持器件表 → 「进入载板目录读 README」的构建说明。

**载板模板①（无参数）：`template1_readme_carrier.md`**

[projects/common/template1_readme_carrier.md:1-14](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template1_readme_carrier.md#L1-L14) —— 第一行 flag 注释，正文给 VADJ/VIO 测试值、构建命令（`cd ... && make`）、对应设备树链接。适用于像 `ad35xxr_evb/zed` 这类无 make 参数的工程。

**载板模板②（有参数）：`template2_readme_carrier.md`**

[projects/common/template2_readme_carrier.md:1-84](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template2_readme_carrier.md#L1-L84) —— 这是信息量最大的模板。以 AD9081 为例，它必须：

- 逐条列出所有可覆盖参数及含义（第 22–44 行，如 `JESD_MODE`、`RX_LANE_RATE`、`RX_JESD_M`…）。
- 给出每种配置对应的 `make` 命令（第 52–62、68–78 行的 Example configurations）。
- 每种配置附「对应的 Linux 设备树」链接——因为硬件配置变了，软件设备树也必须配套。

**载板模板③（固定配置）：`template3_readme_carrier.md`**

[projects/common/template3_readme_carrier.md:1-29](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template3_readme_carrier.md#L1-L29) —— 明确声明「不可参数化、配置固定」，并列出所有固化参数值（如 `adrv9009/a10soc` 的 JESD M/L/S）。适用于硬件走线已固定、不支持软件切配置的工程。

**文档（非 README）撰写规范：`docs/user_guide/docs_guidelines.rst`**

[docs/user_guide/docs_guidelines.rst:85-96](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/docs_guidelines.rst#L85-L96) —— 如果还要给工程写 Sphinx 文档页，官方提供了 `docs/projects/template` 模板，使用时需删掉首行的 `:orphan:` 及所有占位说明文字。文档构建步骤见 u2-l3。

#### 4.3.4 代码实践（源码阅读型）

**目标**：为一个假想工程挑对模板并填对 flag。

**步骤**：

1. 阅读 [projects/common/README.md:13-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/README.md#L13-L44)，把三个载板模板的适用条件抄成一张速查表。
2. 打开 [projects/cn0506/zed/README.md:1-1](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/README.md#L1-L1)，看它的第一行 flag 是什么（应为 `<!-- no_no_os -->`），思考：为什么 cn0506 用了 template2 却只标 `no_no_os`？
3. 假设你新工程「有 make 参数、有设备树、有 no-OS 工程」，判断该用哪个模板、第一行 flag 该怎么写。

**需要观察的现象**：flag 是 Markdown 注释（`<!-- ... -->`），在 GitHub 渲染页面**看不到**，但「查看源码」能看到；GitHub Action 正是读源码里的这行。

**预期结果**：你能说出三个 flag（`no_build_example`、`no_dts`、`no_no_os`）各自的含义，以及「有参数」必用 template2。

#### 4.3.5 小练习与答案

**练习 1**：一个工程支持 `make INTF_CFG=MII/RGMII/RMII` 三种配置，每种都有对应设备树和 no-OS 工程。该用哪个载板模板？第一行 flag 怎么写？

**参考答案**：用 `template2_readme_carrier.md`（有 make 参数）。三种配套资产都齐全，所以**不写任何 flag**，只保留占位注释 `<!-- Put flags here, i.e. no_build_example, no_dts, no_no_os -->`（表示无一缺失）。

**练习 2**：`no_dts` flag 的作用是什么？漏写会怎样？

**参考答案**：`no_dts` 表示「撰写 README 时尚未发布对应设备树」。它告诉 GitHub Action「不要因为找不到设备树就报错」。漏写会导致 CI 误以为该有设备树却没找到而报错。

**练习 3**：评估板层 README（`projects/<eval>/README.md`）和载板层 README 的模板是同一个吗？

**参考答案**：不是。评估板层固定用 `template_readme_evalboard.md`（强调产品页/文档链接/支持器件表/VADJ 范围）；载板层才在 template1/2/3 中三选一（强调构建命令、参数、设备树）。

---

## 5. 综合实践

把三个模块串起来，完成一次「最小新工程起草」。

**场景**：假设有一块新 ADC FMC 子卡 `myadc-fmcz`，载一颗通过 SPI 控制的 ADC，计划先在 `zcu102` 载板上跑通，支持两种采样模式 `SAMPLE_MODE=LOW/HIGH`（综合期切换）。请产出三份草案（**均为示例代码，非仓库已有文件**）。

### 任务 1：文件清单

参照 [projects/fmcomms2/zcu102/](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L1-L25) 的结构，列出 `projects/myadc/zcu102/` 应有的文件：

| 文件 | 来源 / 做法 |
|---|---|
| `Makefile` | 复制 fmcomms2/zcu102/Makefile 改写（见任务 2） |
| `system_top.v` | 参考兄弟工程，例化 `system_wrapper` + `ad_iobuf` 处理 GPIO |
| `system_constr.xdc` | 按 myadc-fmcz 的 FMC 引脚映射分配 `PACKAGE_PIN`/`IOSTANDARD` |
| `system_bd.tcl` | 先 source `../../common/zcu102/zcu102_system_bd.tcl`（载板层），再 source `../common/myadc_bd.tcl`（评估板层，需自建），最后微调 |
| `system_project.tcl` | source 三公共脚本 → `adi_project` → `adi_project_files` → `adi_project_run`（仿 [cn0506/zed/system_project.tcl:29-39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/cn0506/zed/system_project.tcl#L29-L39) 加入 `SAMPLE_MODE` 的 env 读取） |

> 另需自建评估板 base design `projects/myadc/common/myadc_bd.tcl`（第二层）；载板 base design `projects/common/zcu102/` 已存在，直接复用。

### 任务 2：Makefile 草案（示例代码）

```makefile
## Auto-generated, do not modify!   ← 沿用官方头部约定（见下方说明）

PROJECT_NAME := myadc_zcu102

# 直接引用的文件
M_DEPS += ../common/myadc_bd.tcl
M_DEPS += ../../scripts/adi_pd.tcl
M_DEPS += ../../common/zcu102/zcu102_system_constr.xdc
M_DEPS += ../../common/zcu102/zcu102_system_bd.tcl
M_DEPS += ../../../library/common/ad_iobuf.v

# 用到的 library IP（SPI 控制 ADC 的典型组合，按实际器件调整）
LIB_DEPS += axi_dmac
LIB_DEPS += axi_spi_engine
LIB_DEPS += axi_sysid
LIB_DEPS += sysid_rom
LIB_DEPS += spi_engine/spi_engine_execution
LIB_DEPS += spi_engine/spi_engine_offload
LIB_DEPS += util_cdc

include ../../scripts/project-xilinx.mk
```

> **关于「Auto-generated」头部的重要说明**：仓库里每个工程/库的 `Makefile`（含顶层 `Makefile`）头部都标注 `## Auto-generated, do not modify!`，但**本仓库源码中并未包含生成这些 Makefile 的脚本**——生成器疑似内部/CI 工具、未随源码发布（在 `projects/scripts/` 下只能找到读取 Makefile 的 `adi_make.tcl`，没有写入/生成它的脚本）。因此新建一个官方尚未收录的工程时，现实做法是**从最相似的兄弟工程复制 Makefile 再改写**，而不是去运行某个生成器。改写时务必保证三件事正确：`PROJECT_NAME`、`LIB_DEPS`（IP 名字）、`M_DEPS`（带相对路径的文件）。

### 任务 3：README 骨架（示例，基于 template2 + evalboard）

评估板层 `projects/myadc/README.md`（套用 [template_readme_evalboard.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template_readme_evalboard.md#L1-L16)）：

```markdown
# MYADC HDL Project

- Evaluation board product page: <myadc-fmcz 产品页链接>
- System documentation: <wiki 链接 或 "to be added">
- HDL project documentation: http://analogdevicesinc.github.io/hdl/projects/myadc/index.html
- Evaluation board VADJ range: 1.8V - 3.3V

## Supported parts

| Part name | Description |
|---|---|
| <ADC 型号> | <简述> |

## Building the project

Please enter the folder for the FPGA carrier you want to use and read the README.md.
```

载板层 `projects/myadc/zcu102/README.md`（套用 [template2_readme_carrier.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/template2_readme_carrier.md#L1-L84)，因为有 `SAMPLE_MODE` 参数）：

```markdown
<!-- Put flags here, i.e. no_build_example, no_dts, no_no_os -->

# MYADC/ZCU102 HDL Project

- VADJ/VIO with which it was tested in hardware: 1.8V

## Building the project

The parameters configurable through the `make` command, can be found below,
as well as in the **system_project.tcl** file; it contains the default configuration.

​```
cd projects/myadc/zcu102
make
​```

The overwritable parameter from the environment:

- SAMPLE_MODE - defines the ADC sampling mode;
  - LOW  - 低速高精度模式
  - HIGH - 高速模式

### Example configurations

#### LOW mode (default)

​```
cd projects/myadc/zcu102
make SAMPLE_MODE=LOW
​```

Corresponding device tree: <zynqmp-zcu102-myadc-low.dts 链接 或标记 no_dts>

#### HIGH mode

​```
make SAMPLE_MODE=HIGH
​```

Corresponding device tree: <对应设备树链接>
```

**验收检查**：用本讲 4.2 的方法手算 `make SAMPLE_MODE=HIGH` 的产物目录名（应为去掉下划线的 `SAMPLEMODEHIGH` 之类），确认它不会覆盖默认 `LOW` 的产物；并核对你的 README 第一行 flag 是否与「实际是否齐全设备树/no-OS」一致。

---

## 6. 本讲小结

- **新工程 = 复用三层架构 + 补第三层五件套**：`Makefile`、`system_top.v`、`system_constr.xdc`、`system_bd.tcl`、`system_project.tcl`，几乎不写新 RTL，只做拼装与声明。
- **Makefile 三段式**：`PROJECT_NAME`（工程名）+ `LIB_DEPS`（IP 名字，被翻译成 `component.xml`）+ `M_DEPS`（带路径的文件），最后 `include project-xilinx.mk`。
- **参数化两条路径**：命令行 `make VAR=val`（GNU Make 自动导出到环境）与配置文件 `make CFG=file.mk`（需显式 `export`）；Tcl 端统一用 `$::env()` 读取。
- **`DIR_NAME` 是配置指纹**：每传一组参数就生成独立子目录隔离产物，互不覆盖；生成名字时剔除 `JESD`/`LANE` 字样以避免过长。
- **README 强约束**：评估板层用 `template_readme_evalboard.md`；载板层按「无参数 / 有参数 / 固定配置」选 template1/2/3；第一行 flag 注释控制 GitHub Action 行为。
- **「Auto-generated」现实**：仓库内所有 Makefile 均标自动生成，但生成器未随源码发布；新建工程的现实路径是从兄弟工程复制改写，而非运行生成器。

## 7. 下一步学习建议

- **多厂商扩展**：本讲聚焦 Xilinx（Vivado）。若你的新工程要跑在 Intel/Lattice 上，下一步读 [u7-l3 多厂商构建：Intel 与 Lattice 工程](u7-l3-multi-vendor-build.md)，了解 `project-intel.mk`/`project-lattice.mk` 与 `system_qsys.tcl`/`system_pb.tcl` 的差异。
- **IP 复用深化**：若新工程要拼 JESD204 或 SPI Engine 数据通路，精读 [docs/user_guide/ip_cores/use_adi_ips.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/ip_cores/use_adi_ips.rst#L1-L30) 与 u6（JESD204/SPI Engine 框架）。
- **提交合规**：工程起草完准备提 PR 前，务必对照 [u8-l2 HDL 编码规范与贡献流程](u8-l2-coding-guidelines-contributing.md) 做自检，避免 CI 的 guideline/lint 与 README-flag 检查报错。
- **官方定制导览**：[docs/user_guide/customize_hdl.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/customize_hdl.rst#L1-L24) 还列出了「创建新 IP」「模型化设计」「no-OS 快速验证」等更多定制入口，可按需深入。
