# u6-l1 把 PDK 装进开源 EDA 工具

## 1. 本讲目标

学完本讲，你应当能够：

1. 写出一个用 OpenROAD 读入 ICS55 全套 PDK 视图（tech LEF + cell LEF + liberty）的 TCL 脚本。
2. 说清楚为什么在开源流程中应选 `_ecos` 版文件，而把原版文件留给什么场景。
3. 在工具加载完成后，核对 site、布线层、单元数量等报告值与 PDK 源数据是否一致，形成一张「装载核对清单」。
4. 在没有安装任何 EDA 工具的机器上，用纯 Python 脚本完成同样的核对报告。

## 2. 前置知识

- **EDA 工具为什么要「装载」PDK**：综合器、布局布线器本身不懂任何工艺，它们对 55nm 的全部认知——金属层有几层、单元多大、时序多快——都来自运行时读入的 PDK 文件。装载（load）就是把文本视图翻译成工具内部数据库的过程，读不进或读歪了，后面全错。
- **OpenROAD**：目前最主流的开源数字后端平台，把综合（通过 yosys 集成）、布局、布线、时序优化集成在一个 TCL 环境里。它的底层物理数据库叫 **odb**，`read_lef`、`read_liberty` 是把 LEF 与 liberty 灌入 odb 的入口命令。本讲只用到「读入 + 查询」，不跑完整流程。
- **KLayout**：开源版图查看器，可通过图形界面 `File → Import` 导入 LEF/DEF 做可视化检查，是 OpenROAD 之外的轻量替代。
- **三类视图的分工**（前文已学，此处装载顺序要用）：tech LEF 提供层与过孔（u2 系列）、cell LEF 提供 785 个单元抽象（u3-l2/u3-l3）、liberty 提供时序功耗模型（u3-l6）。
- **`_ecos` 变体**（u2-l3、u4-l2 已铺垫）：仓库为开源工具链维护的平行文件，本讲将把散落各讲的差异汇总成一张「选型决策表」，并落到装载环节验证。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [prtech/techLEF/N551P6M_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef) | 工艺 LEF（_ecos 版），675 行 | 装载的第一个文件：层栈、过孔、SITE |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) | 工艺 LEF（原版） | 对照出 DefaultTaper 差异 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef) | H7CH 标准单元 LEF（_ecos 版），93576 行、785 个 MACRO | 装载的第二个文件：单元抽象 |
| [IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef) | IO 单元 LEF（_ecos 版） | IOSite 定义的补齐样例 |
| [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile) | Release 下载与解压 | liberty 不在 git 内，装载前必须先 `make unzip` |
| `IP/STD_cell/.../ics55_LLSC_H7CH/liberty/*.lib`（下载后生成） | H7CH liberty 时序库 | 装载的第三个文件；git 内不存在，内部 .lib 文件名**待确认** |

> 提示：本仓库不含任何 OpenROAD/KLayout 配置或脚本——README 与 CONTRIBUTING 均未提及具体工具。因此本讲所有 TCL/Python 脚本都是**示例代码**（我们为 PDK 新写的），不是项目自带文件。

## 4. 核心概念与源码讲解

本讲三个最小模块：**4.1 OpenROAD 读入命令**、**4.2 ecos 版文件选择策略**、**4.3 加载结果核对**。

### 4.1 OpenROAD 读入命令

#### 4.1.1 概念说明

把 PDK 装进 OpenROAD 只需要三条核心命令：

- `read_lef <tech.lef>`：读入工艺 LEF，在 odb 中建立层表、过孔表、SITE 表；
- `read_lef <cell.lef>`：读入单元 LEF，为每个 `MACRO` 建立一个 master 单元（引用第一步的层与 SITE）；
- `read_liberty <file.lib>`：读入时序库，供综合映射与时序分析使用。

三条命令背后有一个隐含约束：**cell LEF 必须在 tech LEF 之后读入**。cell LEF 里的 `SITE core7 ;` 与 `LAYER MET1 ;` 都只是「名字引用」，定义必须先于引用。这正是 u4-l2 发现的问题原委——IO 原版 LEF 的宏引用了 `IOSite`，但原版 tech LEF 和 IO 原版 LEF 自己都没有定义它，直接读入会报「site 未定义」类错误；_ecos 版在文件头补上了定义（见 4.2.3）。

liberty 与 LEF 相互独立，顺序不限；但若要用 OpenROAD 做后续综合/时序，liberty 的单元集合应与 LEF 的 MACRO 集合对得上（u5-l2 的一致性思想）。

#### 4.1.2 核心流程

装载一个可做综合与布局的 ICS55 最小环境：

```text
make unzip RELEASE_TAG=v1.10.100        # ① 下载并解压 liberty（GDS 可选）
        │
        ▼
openroad 脚本：
  read_lef  prtech/techLEF/N551P6M_ecos.lef        # ② tech：层/过孔/SITE
  read_lef  IP/STD_cell/.../ics55_LLSC_H7CH_ecos.lef  # ③ cell：785 个 master
  read_liberty IP/STD_cell/.../liberty/<待确认>.lib   # ④ 时序模型
        │
        ▼
查询 odb：层数、SITE、master 数 → 与 grep 统计互相印证（4.3）
```

若要加 IO 库（做带 pad ring 的设计），在 ③ 之后追加 `read_lef IP/IO/.../ICSIOA_N55_3P3_1P6M1TM_ecos.lef`。

没有 OpenROAD 时，KLayout 图形界面可通过 `File → Import` 把 LEF/DEF 叠加显示，适合肉眼检查几何；但本讲的「数量核对」用 4.3 的 Python 脚本即可完成，不依赖任何工具。

#### 4.1.3 源码精读

**（a）先解决 liberty 的来源——Makefile 的下载与解压规则**

[Makefile:L11-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L13) 声明三套标准单元库的 liberty 压缩包是 Release 资产（H7CH/H7CL/H7CR 各一个 `.tar.bz2`）：

```make
RELEASE_FILE_LIB := ics55_LLSC_H7CH_liberty.tar.bz2 \
                    ics55_LLSC_H7CL_liberty.tar.bz2 \
                    ics55_LLSC_H7CR_liberty.tar.bz2
```

[Makefile:L22-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22-L23) 用 `patsubst` 从压缩包名推导解压目录——`ics55_LLSC_H7CH_liberty.tar.bz2` 会被解到 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/`：

```make
DECOMP_DIR_LIB_P := IP/STD_cell/ics55_LLSC_H7C_V1p10C100
DECOMP_DIR_LIB   := $(patsubst %_liberty.tar.bz2, $(DECOMP_DIR_LIB_P)/%/liberty, $(RELEASE_FILE_LIB))
```

[Makefile:L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66) 是解压的模式规则；[Makefile:L80-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L80-L81) 的 `unzip` 目标按「清理旧目录 → 下载 → 解压 → 删压缩包」串起全流程。压缩包内部的 `.lib` 文件名（通常含工艺角）**待确认**——解压后 `ls` 一下即可，选择 `tt` 典型角作为首次装载的 liberty 最稳妥（u3-l6 的 corner 知识）。

**（b）第一个读入的文件：_ecos 版 tech LEF 的骨架**

[N551P6M_ecos.lef:L15-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L15-L29) 定版本（5.7）、数据库精度（1 数据库单位 = 1nm）与 0.001 制造网格——OpenROAD 读完后所有几何坐标都按这套精度入库：

```lef
VERSION 5.7 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
...
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
MANUFACTURINGGRID 0.001 ;
```

[N551P6M_ecos.lef:L62-L76](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L62-L76) 是第一个布线层 MET1 的完整定义，其中 `CAPACITANCE CPERSQDIST`/`EDGECAPACITANCE`（L72-L73）是 _ecos 版独有的电容参数，`RESISTANCE RPERSQ`（L74）供寄生估计使用：

```lef
LAYER MET1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.2 0.2 ;
  WIDTH 0.09 ;
  OFFSET 0.1 0.1 ;
  ...
  CAPACITANCE CPERSQDIST 0.0007630 ;
  EDGECAPACITANCE 0.0000339 ;
  RESISTANCE RPERSQ 0.1122 ;
```

[N551P6M_ecos.lef:L658-L674](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L658-L674) 定义三个 SITE（CoreSite 0.2×1.4、core7 0.2×1.4、core9 0.2×1.8），全文件以 [L676](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L676) 的 `END LIBRARY` 收尾。装载后 `SITE` 表应为 3 项。

**（c）第二个读入的文件：_ecos 版单元 LEF 的头部**

[ics55_LLSC_H7CH_ecos.lef:L15-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L15-L25) 版本升到 5.8，第一个 MACRO 的头部引用 tech LEF 的 `core7`：

```lef
VERSION 5.8 ;
...
MACRO ADDFX1H7H
  CLASS CORE ;
  ORIGIN 0 0 ;
  FOREIGN ADDFX1H7H 0 0 ;
  SIZE 4.8 BY 1.4 ;
  SYMMETRY X Y ;
  SITE core7 ;
```

文件共 93576 行、785 个 `MACRO`，以 [L93576](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L93576) 的 `END LIBRARY` 结束——这两个数字就是 4.3 核对清单里的期望值。

#### 4.1.4 代码实践

**实践：编写并运行装载脚本 `load_ics55.tcl`（OpenROAD）**

1. **实践目标**：用三条读入命令 + 一段查询循环，把 ICS55 装进 OpenROAD，并打印 site/层/单元统计。
2. **操作步骤**：
   - 在仓库根目录执行 `make unzip RELEASE_TAG=v1.10.100`（只需 liberty，GDS 也会顺带下载；网络受限时可用 `PROXY_USE=true` 或 `TOOL=wget`，见 u1-l3）。
   - `ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/` 确认解压出的 `.lib` 文件名（待确认），挑 tt 工艺角。
   - 新建 `load_ics55.tcl`（**示例代码**，非项目自带）：

     ```tcl
     # load_ics55.tcl — 把 ICS55 装进 OpenROAD 并打印核对报告（示例代码）
     read_lef prtech/techLEF/N551P6M_ecos.lef
     read_lef IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef
     # 换成上一步 ls 看到的真实文件名（待确认）
     read_liberty IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/ics55_LLSC_H7CH_tt_1p2_25C.lib

     set tech [ord::get_db_tech]
     set nroute 0
     foreach l [$tech getLayers] {
       if { [$l getType] == "ROUTING" } { incr nroute }
     }
     puts "routing layers = $nroute"
     puts "sites:"
     foreach s [$tech getSites] { puts "  [$s getName] : [$s getWidth] x [$s getHeight]" }

     set db [ord::get_db]
     set nmaster 0
     foreach lib [$db getLibs] { incr nmaster [llength [$lib getMasters]] }
     puts "cell macros = $nmaster"
     ```

   - 运行：`openroad -exit load_ics55.tcl | tee load_report.log`。
3. **需要观察的现象**：read_lef 过程无 error/warning 停止；报告打印出的三个数字。
4. **预期结果**：`routing layers = 7`、`sites` 含 `core7 : 0.2 x 1.4`、`cell macros = 785`（期望值的来源见 4.3.1）。odb 的 Tcl 查询接口在不同 OpenROAD 版本间偶有变化，若 `ord::get_db_tech` 报错，可改用 `openroad -help` 中列出的等价命令。本环境未安装 OpenROAD，以上输出**待本地验证**。

#### 4.1.5 小练习与答案

1. **练习**：把 ④ 的 `read_liberty` 换成 IO 库 git 内自带的 `IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib`，还需要做哪一步才不至于报 site 相关错误？
   **答案**：还要读入 IO 的 `_ecos` 版 LEF（`ICSIOA_N55_3P3_1P6M1TM_ecos.lef`），因为 IO 单元宏引用 `IOSite`，该 SITE 只在 _ecos 版 LEF 文件头有定义（见 4.2.3 的 c）。
2. **练习**：如果先读 cell LEF、后读 tech LEF，会发生什么？
   **答案**：cell LEF 中 `SITE core7 ;`、`LAYER MET1 ;` 等引用找不到定义，OpenROAD 会报 site/layer 未定义错误或直接中止——名字引用必须晚于定义。
3. **练习**：为什么 `read_liberty` 与 `read_lef` 的先后顺序无所谓？
   **答案**：liberty 与 LEF 描述同一批单元的不同侧面（时序 vs 几何），两者在工具内是独立的数据库对象，互不引用；只有涉及「综合后映射」的命令才要求两者都已就绪。

### 4.2 ecos 版文件选择策略

#### 4.2.1 概念说明

仓库为 tech LEF、三套标准单元 LEF、IO LEF 各维护一个 `_ecos` 平行版本。汇总前文各讲的发现，差异共四类，每一类都对应开源工具的一个真实需求：

| 差异 | 原版 | _ecos 版 | 开源工具为什么需要 |
| --- | --- | --- | --- |
| RC 电容参数 | 只有 `RESISTANCE` | 每个布线层补 `CAPACITANCE CPERSQDIST`/`EDGECAPACITANCE` | 布线后估计线延迟 \( \tau \approx 0.5RC \)，没有 C 就只能算 R，时序估计失真 |
| OFFSET/DefaultTaper | `OFFSET 0 0`；含 `NONDEFAULTRULE DefaultTaper` | `OFFSET 0.1 0.1`；删除 DefaultTaper | 轨道对准 site 中心、避开行边界电源轨道；DefaultTaper 含悬空 `USEVIARULE` 引用，严格的解析器会告警 |
| 单元信号引脚 | 全部只在 MET1 | 每个信号脚补 MET2 竖条 + VIA1 | 布线器对「只有 M1 的引脚」访问通道有限，易 congestion；补上层引脚显著提高可布性 |
| IO 库 SITE/USE | 宏引用 `IOSite` 但全仓库无定义；部分地脚 `USE SIGNAL` | 文件头定义 `IOSite`/`IOCorner`；48 处 USE/DIRECTION 修正 | 未定义 SITE 直接读入失败；`USE GROUND` 让 PDN 正确识别电源地网络 |

**选型规则**：凡是要喂给开源工具（OpenROAD、yosys+abc、OpenLane 等）做布线、寄生估计、时序分析的流程，一律用 `_ecos` 版；原版适合作为「厂商原始数据的对照基准」和纯规则查询。

一个需要澄清的细节（本讲实测纠正了大纲的一个说法）：**电源引脚不是 _ecos 版新增的**——原版单元 LEF 同样有 `PIN VDD/VSS ... USE POWER/GROUND ; SHAPE ABUTMENT`（每宏一对，785 对），两版相同。真正的增量是信号引脚的 MET2/VIA1 形状与 `VERSION 5.8`。用 grep 即可验证（见 4.2.4）。

#### 4.2.2 核心流程

选择文件版本的决策流程：

```text
这个 LEF 要喂给开源工具吗？
├─ 否（人工查规则、做对照）──────→ 用原版 N551P6M.lef / ics55_LLSC_H7CH.lef
└─ 是
   ├─ tech LEF：要做寄生/时序估计？──→ 必须 N551P6M_ecos.lef（唯一含 CAPACITANCE 的版本）
   ├─ 标准 cell LEF：要布线？────────→ 用 *_ecos.lef（MET2/VIA1 引脚）
   ├─ IO LEF：要摆 pad ring？────────→ 必须 *_ecos.lef（IOSite 有定义）
   └─ ant 版（u3-l4）：与上面正交，仅在需要天线检查数据时替换
```

#### 4.2.3 源码精读

**（a）RC 参数——只有 _ecos 版有电容**

[N551P6M_ecos.lef:L72-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L72-L74)（MET1 的电容 + 电阻三连）：

```lef
  CAPACITANCE CPERSQDIST 0.0007630 ;
  EDGECAPACITANCE 0.0000339 ;
  RESISTANCE RPERSQ 0.1122 ;
```

原版 [N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74) 的 MET1 段没有这两行电容，且 `OFFSET 0 0`（L67，_ecos 版为 0.1 0.1）——u2-l1 的结论：原版 0 个 CAPACITANCE。装载原版后，OpenROAD 的单位长度电容为 0，任何线延迟估计都系统性偏乐观。

**（b）DefaultTaper——原版独有的悬空引用**

[N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652) 是原版的 `NONDEFAULTRULE DefaultTaper` 段，其中 `USEVIARULE` 引用了未定义的规则（u2-l2 已析）；_ecos 版把整段删除。两版行数（`wc -l` 实测：原版 671 行、_ecos 版 675 行，净增 4 行）由「新增 7×2 行电容」与「删除 DefaultTaper 段」两项相抵而成，逐行对齐留给 4.2.5 练习 3。

**（c）IO 的 IOSite——「引用先于定义」的修复**

[ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L19-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L19-L29) 在文件头补上了原版缺失的 SITE 定义：

```lef
SITE IOSite
    SYMMETRY x y r90 ;
    CLASS pad ;
    SIZE 0.005 BY 130.000 ;
END IOSite

SITE IOCorner
    SYMMETRY x y r90 ;
    CLASS pad ;
    SIZE 130.000 BY 130.000 ;
END IOCorner
```

随后第一个宏 [L31-L37](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L31-L37) 的 `SITE IOSite ;` 才有处可落。

**（d）标准单元引脚升级——_ecos 版的真实增量**

[ics55_LLSC_H7CH_ecos.lef:L52-L63](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L52-L63) 中 ADDFX1H7H 的输入脚 A 有三层形状——MET1 原有形状之上叠加 MET2 竖条与一个 VIA1：

```lef
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER MET1 ;
        RECT 0.425 0.555 0.545 0.78 ;
      LAYER MET2 ;
        RECT 0.45 0.43 0.55 0.96 ;
      LAYER VIA1 ;
        RECT 0.455 0.655 0.545 0.745 ;
    END
  END A
```

量化对比（grep 实测）：原版 H7CH LEF 中 `LAYER MET2`/`LAYER VIA1` 出现 **0 次**，_ecos 版各 **3499 次**；`LAYER MET1` 两版同为 5800 次；`USE POWER`/`USE GROUND` 两版同为 785/785 次。即 _ecos 版 = 原版 + 每个信号脚的 MET2/VIA1 形状（这正是两版行数差 93576 − 79580 = 13996 行的主体）。

#### 4.2.4 代码实践

**实践：用 grep 量化两版单元 LEF 的差异**

1. **实践目标**：亲手验证 4.2.1 表格中「引脚升级」一行的数字，确认电源引脚两版相同。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   N=IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef
   E=IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef

   for f in $N $E; do
     echo "== $f =="
     grep -c "^MACRO"      $f   # MACRO 数
     grep -c "LAYER MET2"  $f   # MET2 引脚形状数
     grep -c "LAYER VIA1"  $f   # VIA1 引脚形状数
     grep -c "USE POWER"   $f   # 电源脚数
   done
   ```

3. **需要观察的现象**：两列数字中 MACRO、USE POWER 完全相同，MET2/VIA1 从 0 变 3499。
4. **预期结果**：`785/785`、`0/3499`、`0/3499`、`785/785`。若你的输出一致，就证明了「电源脚不是增量、高层引脚才是增量」。（本组数字在本仓库 HEAD 上已用 grep 实测。）

#### 4.2.5 小练习与答案

1. **练习**：某同学用原版 tech LEF + 原版 cell LEF 在 OpenROAD 里跑完了布线，时序报告看起来「还不错」，最可能哪里骗了他？
   **答案**：原版 tech LEF 无 CAPACITANCE，线电容按 0 估计，线延迟 \( \approx 0.5RC \) 被系统性低估；应换 `N551P6M_ecos.lef` 重跑。
2. **练习**：`ant` 版（u3-l4）和 `_ecos` 版能同时用吗？
   **答案**：不能同时读同一个库的两个 LEF（宏重定义）；二者是正交变体，各自与普通版二选一。天线注记只有 2 行差异，若流程需要两者，可手工把 ANTENNAPARTIALMETALAREA 行合并进 _ecos 版（属于本地适配，注意 Apache-2.0 的修改声明要求，见 u6-l3）。
3. **练习**：不用 diff，如何快速确认两个 tech LEF 的差异只有电容、OFFSET 和 DefaultTaper 三类？
   **答案**：`diff prtech/techLEF/N551P6M.lef prtech/techLEF/N551P6M_ecos.lef | grep '^[<>]' | grep -v -E "CAPACITANCE|EDGECAPACITANCE|OFFSET|DefaultTaper|USEVIARULE|VIA|NONDEFAULT"`，若输出为空即得证（u2-l3 已做过同类验证）。

### 4.3 加载结果核对

#### 4.3.1 概念说明

「装载成功」不等于「装载正确」。核对（sanity check）是把工具报告的统计值与**从 PDK 源文件直接统计出的期望值**对照，任何不一致都说明读入有丢失或解析有出入。本讲建立了 ICS55（H7CH 配置）的期望值基线，全部由 grep 对仓库 HEAD 实测：

| 核对项 | 期望值 | 来源 |
| --- | --- | --- |
| tech LEF 布线层（`TYPE ROUTING`） | 7（MET1–MET5、T4M2、RDL） | [N551P6M_ecos.lef:L62-L206](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L62-L206) |
| tech LEF SITE | 3（CoreSite、core7、core9） | [N551P6M_ecos.lef:L658-L674](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L658-L674) |
| 有电容参数的布线层 | 7（全部） | 4.2.3（a） |
| H7CH master 单元（MACRO） | 785 | [ics55_LLSC_H7CH_ecos.lef:L19](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L19) 起 |
| 引用 `SITE core7` 的宏 | 785（全部） | grep `SITE core7 ;` |
| 引脚（PIN 语句，含电源） | 5069 | grep `^  PIN ` |
| cell_list 条目（≠MACRO 数！） | 748 | [cell_list/ics55_LLSC_H7CH.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt) |

最后一行是 u1-l2/u5-l2 结论的复用：cell_list 不是全集，785 − 748 = 37 个 ANT 天线单元只在 LEF 中。所以「工具报 785、cell_list 只有 748」不是错误，而是视图职责差异；核对前要先明确用哪个视图当金标准。

#### 4.3.2 核心流程

```text
装载完成
  │
  ├─ ① 层栈核对：工具布线层数 == grep "TYPE ROUTING" techLEF 的 7
  ├─ ② SITE 核对：3 个 site；被宏引用的只有 core7
  ├─ ③ 单元数核对：master 数 == MACRO 数 785（不是 cell_list 的 748）
  ├─ ④ 引脚数核对：5069，其中 POWER/GROUND 各 785
  └─ ⑤ liberty 核对：liberty 内 cell 数应与 MACRO 集合对照（u5-l2 的脚本思路）
       任一不符 → 检查读入顺序、文件版本（_ecos 与否）、是否漏读文件
```

#### 4.3.3 源码精读

核对用到的三处源码锚点：

- [N551P6M_ecos.lef:L85-L99](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L85-L99)：MET2 层块，`DIRECTION VERTICAL` 与 MET1 的 `HORIZONTAL` 交替——核对时每个布线层都应有方向、pitch、宽度、间距、电容、电阻六项。

  ```lef
  LAYER MET2
    TYPE ROUTING ;
    DIRECTION VERTICAL ;
    PITCH 0.2 0.2 ;
    WIDTH 0.1 ;
    ...
    CAPACITANCE CPERSQDIST 0.0011069 ;
  ```

- [ics55_LLSC_H7CH_ecos.lef:L26-L51](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L26-L51)：ADDFX1H7H 的 VDD/VSS 电源脚——`DIRECTION INOUT` + `USE POWER/GROUND` + `SHAPE ABUTMENT`，rect 越出 SIZE 边界（VDD 到 1.48 > 高 1.4；VSS 到 −0.08 < 0），靠邻接单元拼接成连续电源轨，这是 ④ 中「电源脚各 785」的实体：

  ```lef
  PIN VDD
    DIRECTION INOUT ;
    USE POWER ;
    SHAPE ABUTMENT ;
    PORT
      LAYER MET1 ;
        RECT 0 1.32 4.8 1.48 ;
  ```

- [Makefile:L74-L78](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L74-L78)：GDS 的 IO 解压规则——若核对几何（KLayout 查看 GDS），路径从这里推导，GDS 同样不在 git 内。

#### 4.3.4 代码实践

**实践：无工具环境的 Python 核对报告 `check_load.py`**

1. **实践目标**：不装任何 EDA 工具，用纯 Python（标准库 re）解析两个 `_ecos` LEF，输出与 OpenROAD 报告同口径的统计，与 4.3.1 基线互证。
2. **操作步骤**：新建 `check_load.py`（**示例代码**，约 40 行，只依赖标准库）：

   ```python
   #!/usr/bin/env python3
   """check_load.py — ICS55(_ecos) 装载核对报告（示例代码，无需任何 EDA 工具）"""
   import re

   TECH = "prtech/techLEF/N551P6M_ecos.lef"
   CELL = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef"

   tech = open(TECH).read()
   routing = []
   for m in re.finditer(r"LAYER (\S+)\n(.*?)END \1", tech, re.S):
       name, body = m.group(1), m.group(2)
       if "TYPE ROUTING" not in body:
           continue
       d = re.search(r"DIRECTION (\S+)", body)
       cap = "CAPACITANCE CPERSQDIST" in body
       routing.append((name, d.group(1), cap))
   sites = re.findall(r"SITE (\S+)\n\s*SIZE (\S+) BY (\S+)", tech)

   print("== tech LEF ==")
   print("routing layers:", len(routing))
   for name, direction, cap in routing:
       print("  %-5s %-10s cap=%s" % (name, direction, cap))
   print("sites:", sites)

   lef = open(CELL).read()
   macros = re.findall(r"^MACRO (\S+)", lef, re.M)
   used_sites = set(re.findall(r"^  SITE (\S+) ;", lef, re.M))
   pins = re.findall(r"^  PIN (\S+)", lef, re.M)
   power = len(re.findall(r"USE POWER", lef))
   ground = len(re.findall(r"USE GROUND", lef))

   print("== cell LEF ==")
   print("macros:", len(macros))
   print("sites referenced by macros:", sorted(used_sites))
   print("pins (incl. power):", len(pins),
         "POWER:", power, "GROUND:", ground)
   ```

   运行：`python3 check_load.py`。（正则说明：tech LEF 的顶层层块形如 `LAYER MET1\n...END MET1`，宏内 pin 的 `LAYER MET1 ;` 带分号不会被误匹配。）
3. **需要观察的现象**：输出的层数、SITE 表、宏数、引脚数。
4. **预期结果**：`routing layers: 7` 且每行 `cap=True`；`sites` 为 `[('CoreSite','0.2','1.4'), ('core7','0.200','1.400'), ('core9','0.200','1.800')]`；`macros: 785`；`sites referenced by macros: ['core7']`；`pins: 5069, POWER: 785, GROUND: 785`。这些数值与 4.3.1 的 grep 基线一一对应（脚本本身的格式化输出**待本地验证**，各统计值已用等价 grep 命令实测）。
5. 若你有 OpenROAD：把 `check_load.py` 的输出与 `load_ics55.tcl` 的报告逐行对照，两套独立实现给出同一组数，装载即可判为「读入完整」。

#### 4.3.5 小练习与答案

1. **练习**：OpenROAD 报 `cell macros = 748`，恰好等于 cell_list 行数，这正常吗？
   **答案**：不正常——LEF 里有 785 个 MACRO，报 748 说明有 37 个宏没进去（大概率读错成了某个「按 cell_list 裁剪过」的文件，或旧文件未更新）；748 这个巧合恰恰暗示错把 cell_list 当成了 LEF 的全集。
2. **练习**：为什么核对 SITE 时还要看「被宏引用的 SITE」而不是只数定义？
   **答案**：tech LEF 定义了 3 个 SITE，但 core9（1.8μm 行高）与 CoreSite 当前没有任何宏引用；「定义 3、引用 1（core7）」两个数都对才完整，单看任何一个都无法发现「定义了用不上/引用了没定义」两类问题（后者正是 IO 原版的坑）。
3. **练习**：把 `check_load.py` 的 CELL 路径换成原版 `ics55_LLSC_H7CH.lef`，哪几项会变？
   **答案**：`macros/pins/POWER/GROUND/sites referenced` 都不变（785/5069/785/785/core7）；由于原版信号脚无 MET2，若脚本加统计 `LAYER MET2` 计数会从 3499 变 0——这正是区分两版的最短判据。

## 5. 综合实践

**任务：给 ICS55 做一张「装载体检单」并跑通。**

1. 执行 `make unzip RELEASE_TAG=v1.10.100`，记录解压出的 liberty 文件名清单（**待确认**项落定）；确认 `.gitignore` 的 `/**/Std_cell/**/liberty/` 规则解释了为何 `git status` 看不到它们。
2. 写 `load_ics55.tcl`（4.1.4）读入 tech `_ecos` LEF + H7CH `_ecos` LEF + tt liberty；没有 OpenROAD 就跳到第 4 步。
3. 用 grep 独立统计期望值（4.2.4 的循环 + 4.3.1 各行），填入下表左列。

   | 核对项 | grep 期望值 | 工具报告值 | 一致? |
   | --- | --- | --- | --- |
   | 布线层 | | | |
   | SITE 定义 / 被引用 | | | |
   | master 数 | | | |
   | 引脚数 / POWER / GROUND | | | |

4. 运行 `check_load.py` 作为第二实现，三方（grep、Python、OpenROAD）数字一致即体检通过。
5. 写 5 行结论：装载用了哪些文件、为什么选 `_ecos`、liberty 覆盖了哪些 corner、有无不一致项及原因。

## 6. 本讲小结

- 装载三件套：`read_lef`(tech) → `read_lef`(cell) → `read_liberty`；引用必须晚于定义，cell LEF 不能先于 tech LEF。
- liberty 不在 git 内：`make unzip` 经 [Makefile:L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66) 的模式规则解压到各库 `liberty/` 目录，内部文件名待确认。
- `_ecos` 选型的四类增量：布线层电容参数、OFFSET 0.1 与 DefaultTaper 删除、信号脚 MET2/VIA1 形状（0→3499）、IO 的 IOSite/IOCorner 定义与 USE 修正；电源 ABUTMENT 脚两版都有，不是增量。
- 核对基线（H7CH）：7 布线层、3 SITE（仅 core7 被引用）、785 MACRO、5069 引脚、POWER/GROUND 各 785；cell_list 的 748 不是全集。
- 工具不是必需品：grep + 40 行 Python 就能完成与 OpenROAD 同口径的装载核对。

## 7. 下一步学习建议

- **u6-l2 综合实验**：用 yosys `read_liberty -lib` + `dfflibmap` + `abc -liberty` 把一个小 RTL 映射到 H7C 门级网表，再用 PDK 自带 Verilog 模型做门级仿真——本讲装载的 liberty 正是那条流水线的输入。
- **u6-l3 贡献模拟**：本讲的 `_ecos` 差异清单就是二次开发的适配模板；按 Apache-2.0 要求为适配文件保留 license header。
- 继续阅读：OpenROAD 官方文档中 `read_lef`/`read_liberty` 与 odb Tcl API 章节，把 4.1.4 脚本扩展成 `initialize_floorplan` 的完整流程；KLayout 的 LEF/DEF 导入用于目检 GDS（解压后）与 LEF 抽象的叠加一致性。
