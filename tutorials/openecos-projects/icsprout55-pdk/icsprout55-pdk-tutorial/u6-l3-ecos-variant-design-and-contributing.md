# u6-l3 _ecos 变体设计与二次开发贡献

## 1. 本讲目标

学完本讲，你应当能够：

1. 把散落在 u2-l3、u3-l3、u4-l2、u6-l1 各讲的 `_ecos` 差异，归纳成一张**四类适配总表**（补 RC、补电源/地语义、补高层引脚与 SITE、修正 USE/DIRECTION），并为每一类指出涉及的文件与可量化证据。
2. 说出「新增或修改一个标准单元」需要动哪些视图文件（cell_list、LEF、_ecos LEF、verilog、CDL、liberty、GDS），并按各文件的真实模板手写出合格片段。
3. 解释 Apache-2.0 对 ICS55 的两条硬要求——保留归属声明、修改须在文件头注明——并掌握 LEF（`#`）、CDL（`*`）、Verilog/liberty（`/* */`）三种注释语法的 license header 写法。
4. 按 CONTRIBUTING.md 走通一次**模拟贡献**：分支 → 修改 → 带 header → `git commit -s`（DCO 签署）→ PR → 评审，全程不污染上游仓库。

## 2. 前置知识

- **平行变体（parallel variant）**：仓库里每个 `*_ecos.lef` 都有一个同名原版文件。原版是代工厂/原厂交付的「金数据」，`_ecos` 版是 ECOS Team 为开源工具链做的适配副本。**改副本、不改原件**，既能快速迭代，又能随时 diff 出「社区适配了什么」。
- **为什么需要适配**：商业 PDK 的视图文件默认服务商业工具；开源工具（OpenROAD/yosys 等）对文件的完整性更敏感——缺 SITE 定义就报错、缺电容参数就算不准延迟、`USE SIGNAL` 的地引脚进不了电源网络。前几讲已经分别见过这些坑，本讲把它们收拢成体系。
- **视图即契约（u5-l2）**：同一个单元在 cell_list、LEF、verilog、CDL、liberty 中的名字与引脚必须一致。二次开发改一个单元，等于**同时修改五份契约**，漏一份就在一致性检查中露馅。
- **DCO（Developer Certificate of Origin）**：开源界常用的贡献确权机制。提交时在 commit message 末尾加一行 `Signed-off-by: 姓名 <邮箱>`（`git commit -s` 自动生成），声明你对代码拥有著作权且愿意按项目许可贡献。
- **pull request（PR）评审**：本仓库所有改动（包括维护者自己的）都必须经 GitHub PR 评审合入——u6-l3 末尾会分析一个真实被合入的修 bug PR。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [prtech/techLEF/N551P6M_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef) | 工艺 LEF（_ecos 版），675 行 | 适配类别①：补 RC 电容参数 |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) | 工艺 LEF（原版），671 行 | diff 基准：无 CAPACITANCE、含 DefaultTaper |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef) | H7CH 单元 LEF（_ecos 版），93576 行 | 适配类别③：信号脚补 MET2/VIA1 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef) | H7CH 单元 LEF（原版），79580 行 | diff 基准：0 个 MET2/VIA1 |
| [IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef) | IO 单元 LEF（_ecos 版），63209 行 | 适配类别②④：SITE 定义、USE/DIRECTION 修正 |
| [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md) | 项目主页 | Apache-2.0 条款表述与 header 模板原文 |
| [CONTRIBUTING.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md) | 贡献指南 | DCO、PR 评审、行为准则 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt) | 单元名单（无 header 的例外文件） | 视图生成清单的入口条目 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v) | 仿真模型库 | 新单元 verilog 模块模板 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl) | 晶体管级网表 | 新单元 CDL 网表模板 |

> 提示：本讲引用的 git 历史（提交 `e5c881b`、PR #20）来自仓库真实记录，可用 `git show` 复现；所有 diff 计数均可用文中 grep 命令验证。标注「示例代码」的脚本是为讲解新写的，不属于项目文件。

## 4. 核心概念与源码讲解

本讲三个最小模块：**4.1 ecos 变体适配要点汇总**、**4.2 视图文件生成清单**、**4.3 Apache-2.0 header 与贡献流程**。

### 4.1 ecos 变体适配要点汇总

#### 4.1.1 概念说明

仓库共 41 个 git 跟踪文件，其中 **5 个是 `_ecos` 变体**：工艺 LEF 1 个 + 三套标准单元 LEF 各 1 个 + IO LEF 1 个。它们不是「另一个库」，而是与原版**逐单元对应**的平行文件——原版 785 个 MACRO，`_ecos` 版也是 785 个；单元名、尺寸、CLASS 完全一致，差别只在做减法/加法的「适配项」上。

维护平行变体而不是直接改原版，动机有三：

1. **保真**：原版是 ICsprout 交付物，保留原始形态便于与新版本原厂数据做 diff、做归档追溯。
2. **可审计**：`diff 原版 _ecos版` 的输出就是「社区适配清单」，评审者一眼看全改动，不用在 9 万行文件里找改动。
3. **可回退**：开源工具升级后若某项适配不再需要（或引发新问题），删掉对应增量即可，原版永远是基线。

#### 4.1.2 核心流程

把全部 `_ecos` 差异归入四类适配（计数均为实测，验证方法见 4.1.4）：

| 类别 | 适配内容 | 涉及文件 | 实测证据 | 解决的问题 |
| --- | --- | --- | --- | --- |
| ① 补 RC 寄生 | 7 个 ROUTING 层新增 `CAPACITANCE CPERSQDIST` 与 `EDGECAPACITANCE`；`OFFSET` 由 `0 0` 改 `0.1 0.1`；删除含悬空引用的 `NONDEFAULTRULE DefaultTaper` | tech LEF | 原版 `CAPACITANCE` 出现 0 次，_ecos 版 14 次 | 开源工具估线延迟需 \( C = C_{persq}\cdot WL + 2L\cdot C_{edge} \)，无电容则延迟被系统性低估 |
| ② 补电源/地语义 | 46 处地类引脚 `USE SIGNAL` → `USE GROUND` | IO LEF | diff 计数 46 行 | PDN 工具靠 `USE` 把引脚归入电源/地网络，标错则地网连不通 |
| ③ 补高层引脚与 SITE | 每个信号脚新增 `LAYER MET2` + `LAYER VIA1` 形状（0→3499 对）；文件头补 `SITE IOSite`/`IOCorner` 定义 | 标准单元 LEF、IO LEF | MET2/VIA1 各 3499 处；IO 原版 23 个宏引用 IOSite 却 0 处定义 | 引脚只停在 MET1 时高层布线不可达；SITE 未定义则读 LEF 直接报错 |
| ④ 修正 USE/DIRECTION | 2 处 `DIRECTION INPUT`→`OUTPUT`（PBMUX 的 C、PWE 的 XC）；326 处 `DIRECTION OUTPUT`→`INPUT`（SN/SI/SE） | IO LEF、标准单元 LEF | 提交 `e5c881b` 修复 326 处 | 方向错则综合网表与 LEF 对不上，时序弧方向全反 |

两条补充事实，避免误记（与 u6-l1 的结论一致）：

- **电源轨道 pin 不是 `_ecos` 增量**：标准单元两版都有 `USE POWER/GROUND` + `SHAPE ABUTMENT` 的 VDD/VSS MET1 轨道（_ecos 版 785 个 VDD pin，原版同样 785 个）。_ecos 版只是把电源脚**挪到了每个 MACRO 的最前面**（便于工具优先处理电源）。
- **VERSION 抬升只发生在标准单元 LEF**（5.7→5.8）；工艺 LEF 的 _ecos 版仍是 `VERSION 5.7`。

选型规则（承接 u6-l1）：凡喂给开源工具的流程一律用 `_ecos` 版；原版只用于查代工厂原始规则。

#### 4.1.3 源码精读

**（a）类别①：tech LEF 补电容、删 DefaultTaper**

先看原版 MET1 的定义——只有电阻没有电容：

[N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74)（原版 MET1：`OFFSET 0 0`，仅有 `RESISTANCE RPERSQ 0.1122`）：

```lef
LAYER MET1
  TYPE ROUTING ;
  ...
  OFFSET 0 0 ;
  ...
  RESISTANCE RPERSQ 0.1122 ;
```

再看 `_ecos` 版同一层：

[N551P6M_ecos.lef:L62-L76](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L62-L76)（_ecos 版 MET1：OFFSET 半节距平移 + 补两条电容参数）：

```lef
LAYER MET1
  TYPE ROUTING ;
  ...
  OFFSET 0.1 0.1 ;
  ...
  CAPACITANCE CPERSQDIST 0.0007630 ;
  EDGECAPACITANCE 0.0000339 ;
  RESISTANCE RPERSQ 0.1122 ;
```

有了这三行，工具才能用方块电阻 \( R = R_{persq}\cdot L/W \) 与面电容 \( C = C_{persq}\cdot WL + 2L\cdot C_{edge} \) 估出互连寄生（量化结论见 u2-l3：1mm 最小宽 MET1 约 1.25kΩ/137fF）。

原版还有一段被 _ecos 版整体删除的内容：

[N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652)（`NONDEFAULTRULE DefaultTaper`，其 `USEVIARULE MET1_POLY` 引用的规则在本文件中不存在）：

```lef
NONDEFAULTRULE DefaultTaper
  LAYER POLY
    WIDTH 0.06 ;
  END POLY
  LAYER MET1
    WIDTH 0.09 ;
  END MET1
  USEVIARULE MET1_POLY ;
END DefaultTaper
```

悬空引用对严格的解析器是隐患，_ecos 版直接拿掉（`grep NONDEFAULTRULE` 在 _ecos 版为 0 次）。

**（b）类别③：标准单元 LEF 补 MET2/VIA1**

以 ADDFX1H7H 的输入脚 A 为例。原版只有 MET1 一个矩形：

[ics55_LLSC_H7CH.lef:L26-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33)（原版 PIN A：仅 MET1）：

```lef
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER MET1 ;
        RECT 0.425 0.555 0.545 0.78 ;
    END
  END A
```

_ecos 版给同一引脚加了 MET2 竖条与 VIA1 过孔：

[ics55_LLSC_H7CH_ecos.lef:L52-L63](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L52-L63)（_ecos 版 PIN A：MET1 + MET2 + VIA1 三层可达）：

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

MET1 在本工艺是水平层（u2-l1），单元内引脚原本只能被水平进入；加了竖直 MET2 条和 VIA1 后，布线器在 MET2 轨道上就能直接打到引脚。全库 3499 个信号脚因此获得高层可达性。同时注意文件头：

[ics55_LLSC_H7CH_ecos.lef:L15-L17](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L15-L17) 声明 `VERSION 5.8 ;`（原版 [ics55_LLSC_H7CH.lef:L15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L15) 为 5.7）。

**（c）类别②③④：IO LEF 的 SITE 定义与属性修正**

原版 IO LEF 的 23 个宏都写着 `SITE IOSite ;`（grep 计数 23），但整个文件 `^SITE` 定义为 **0 次**——引用先于定义且无定义。_ecos 版在文件头补齐：

[ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L19-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L19-L29)（补定义 IOSite 与 IOCorner 两种 SITE）：

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

随后每个宏的 `SITE IOSite ;` 引用才真正落地（如 [L31-L37](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L31-L37) 的 CORNER 宏）。

DIRECTION 修正的两处核心侧输出（PAD 内部逻辑输出给核域，却错标成 INPUT）：

[ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L54923-L54925](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L54923-L54925)（PBMUX 的 C 脚改为 OUTPUT；PWE 的 XC 同理见 [L55843-L55845](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L55843-L55845)）：

```lef
  PIN C
    DIRECTION OUTPUT ;
    USE SIGNAL ;
```

**（d）类别④的案例研究：提交 `e5c881b`（PR #20）**

这是仓库里一次真实的「修 `_ecos` 变体 bug」贡献，可用 `git show e5c881b` 复现：

```text
fix: correct direction for some pins in *_ecos.lef
 3 files changed, 356 insertions(+), 356 deletions(-)
 涉及：ics55_LLSC_H7CH_ecos.lef / H7CL / H7CR
 修正引脚：SN 122 处、SI 102 处、SE 102 处，共 326 处 OUTPUT → INPUT
```

SN（低有效置位）、SI/SE（扫描输入/使能）都是**输入**信号，此前在 `_ecos` 版 LEF 里被标成 `DIRECTION OUTPUT`。合并提交 `1349812`（Merge pull request #20）说明它走的正是 CONTRIBUTING 要求的 PR 评审流程。

顺带一个「课后可挖」的发现：该修复只动了 `_ecos` 版；用 `grep -Pz` 检查会发现 H7CH 两版 LEF 中**仍有 48 个 SN/SI/SE 引脚是 `DIRECTION OUTPUT`**（例如原版 [ics55_LLSC_H7CH.lef:L18637-L18638](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L18637-L18638) 的 DFFNSRQX1H7H SN 脚，`_ecos` 版 [L22608-L22609](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L22608-L22609) 同样）。这批残留是漏修还是有意保留**待上游确认**——但「补齐同类修正」正是一个范围明确、风险可控的候选 PR（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：写一个「适配审计」脚本，对仓库 5 对（原版, _ecos）文件自动产出 4.1.2 表格中的全部计数，验证本讲结论。

**操作步骤**（示例代码，保存为 `~/ecos_audit.sh`，在仓库根目录运行）：

```bash
#!/usr/bin/env bash
# 示例代码：统计每对 (原版, _ecos) 文件的关键差异计数
TECH=prtech/techLEF/N551P6M
CH=IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH
IO=IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM
cnt() { grep -c "$2" "$1" || true; }   # 无匹配时 grep 返回 1，|| true 保证循环继续
for f in "$TECH" "$CH" "$IO"; do
  echo "== $f =="
  for key in "CAPACITANCE CPERSQDIST" "LAYER MET2" "LAYER VIA1" "^SITE " "USE GROUND"; do
    echo "  '$key'  原版=$(cnt "${f}.lef" "$key")  ecos=$(cnt "${f}_ecos.lef" "$key")"
  done
done
```

**需要观察的现象**：每对文件的计数差值。

**预期结果**（本人已按同口径 grep 验证）：

| 关键字 | TECH 原版→ecos | CH 原版→ecos | IO 原版→ecos |
| --- | --- | --- | --- |
| `CAPACITANCE CPERSQDIST` | 0 → 7 | 不适用 | 不适用 |
| `LAYER MET2` | 相同 | 0 → 3499 | 相同（IO 本就有多层脚） |
| `LAYER VIA1` | 相同 | 0 → 3499 | 相同 |
| `^SITE ` | 3 → 3（CoreSite/core7/core9，与 DefaultTaper 无关） | 0 → 0（宏内的 `  SITE core7 ;` 有缩进、不匹配 `^SITE `） | 0 → 2（IOSite/IOCorner） |
| `USE GROUND` | 0 → 0（无引脚） | 785 → 785（每宏一个 VSS） | 0 → 46（全部来自 USE SIGNAL 修正） |

若表格中某项对不上，先检查是否把 `LAYER MET2`（含空格）误写成 `MET2`（会连带匹配 MET2 层引用）。

**待本地验证**：`USE GROUND` 精确计数依赖 grep 转义，建议用 `grep -c 'USE GROUND ;'` 复核 IO 一对文件（预期差 46）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_ecos` 版把 OFFSET 从 `0 0` 改成 `0.1 0.1`，而不是改 PITCH？

**答案**：PITCH（0.2）决定布线轨道总量，改它会改变所有布线坐标体系、与 GDS/设计规则冲突。OFFSET 只是把轨道网格整体平移半节距（0.1 = PITCH/2），使轨道穿过 site 中心、避开行边界上的电源轨道（u2-l3），是零破坏的适配。

**练习 2**：如果一个流程「只做综合、不做布局布线」，还需要 `_ecos` 版文件吗？

**答案**：综合只消费 liberty（本来就要 `make unzip` 下载），不读 LEF，所以 tech/cell LEF 用哪版都无影响。但只要进入布局布线或时序驱动的优化（OpenROAD 等），就必须用 `_ecos`：类别①提供电容、类别③保证高层引脚可达、类别②④保证电源网与引脚方向正确。

**练习 3**：如何用一条命令证明「电源轨道 pin 不是 `_ecos` 增量」？

**答案**：分别统计两版的 VDD pin 数：`grep -c '^  PIN VDD$' ics55_LLSC_H7CH.lef` 与同命令作用于 `_ecos` 版，两者都是 785；再抽一个单元（如 INVX1H7H）看两版的 VDD 定义都含 `USE POWER` + `SHAPE ABUTMENT`。

### 4.2 视图文件生成清单

#### 4.2.1 概念说明

「二次开发」在这个仓库里最常见两种形态：

- **加一个单元**：如补一个缺失的驱动档位；
- **改一个单元**：如修正引脚方向、补天线属性。

无论哪种，**单元是基本单位，视图是交付单位**。一个 H7CH 单元要在全流程可用，必须在下列每个视图里各有一份描述，缺一不可：

| 视图 | 文件 | 语法模板出处 | 谁消费它 |
| --- | --- | --- | --- |
| 名单 | `cell_list/ics55_LLSC_H7CH.txt` | 4.2.3（a） | 人、脚本、流程清点 |
| 物理抽象（原版） | `lef/ics55_LLSC_H7CH.lef` | 4.2.3（b） | 商业布线器/对照基准 |
| 物理抽象（_ecos） | `lef/ics55_LLSC_H7CH_ecos.lef` | 4.2.3（c） | 开源布线器（OpenROAD） |
| 仿真模型 | `verilog/ics55_LLSC_H7CH.v` | 4.2.3（d） | iverilog/verilator 门级仿真 |
| 晶体管网表 | `cdl/ics55_LLSC_H7CH.cdl` | 4.2.3（e) | LVS、SPICE |
| 时序模型 | `liberty/*.lib`（不在 git，经 `make unzip` 分发） | 文件名**待确认**（解压前不可见） | yosys/OpenSTA |
| 版图 | `gds/*.gds`（不在 git，经 `make unzip` 分发） | — | 物理验证、流片 |

注意两层「不对称」：cell_list 不是全集（不含 37 个 ANT 单元，u1-l2/u3-l4）；liberty/GDS 不在 git 内——改这两类文件无法通过普通 PR 提交，需要走 Release 通道（联系维护方，见 README 联系表）。

#### 4.2.2 核心流程

新增一个标准单元的生成顺序（自上而下，前者约束后者）：

```text
① 电路设计（W/L 定尺寸） ──► ⑤ CDL 网表（晶体管级真值）
② 版图绘制              ──► ⑥ GDS（交付代工厂）        ┐
③ 从版图抽取抽象        ──► ②' LEF 原版（SIZE/pin/OBS）├─ 同一几何的三种投影
                              ②'' LEF _ecos（②'+电源前置+MET2/VIA1）
④ 特性仿真（各 corner） ──► ⑦ liberty（不在 git）
汇总命名与登记          ──► ①' cell_list 条目 + ④' verilog 模块
```

对贡献者的实操约束：

1. **命名先行**：功能＋驱动＋库后缀的规则（u3-l1）决定了单元名，所有视图用同一个名字互相引用；
2. **LEF 几何必须落在格点上**：宽度是 site 宽 0.2 的整数倍、高度恒 1.4（u3-l2）；
3. **每份新文件/新片段遵守该格式的注释语法**（LEF `#`、CDL `*`、Verilog `/* */`），并带 license header（4.3）；
4. **提交前跑一致性检查**（u5-l2 的方法）：单元×视图、引脚×方向两张矩阵必须通过。

#### 4.2.3 源码精读

以最小单元 **INVX1H7H** 为标本，看它在五个 git 内视图里的真实样子。

**（a）cell_list 条目**——纯名单、一行一个、字符串序排列：

[ics55_LLSC_H7CH.txt:L283](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L283)（第 283 行就是一行单元名；注意字符串序里 `INVX16H7H` 排在 `INVX1H7H` 之前，因为逐字符比较 `6 < H`）：

```text
INVX1H7H
```

**（b）原版 LEF 片段**——MACRO 头 + 信号脚（仅 MET1）+ 电源脚 + 无 OBS：

[ics55_LLSC_H7CH.lef:L29793-L29838](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L29793-L29838)（INVX1H7H：SIZE 0.6×1.4、SITE core7、A/Y/VDD/VSS 四脚）：

```lef
MACRO INVX1H7H
  CLASS CORE ;
  ORIGIN 0 0 ;
  FOREIGN INVX1H7H 0 0 ;
  SIZE 0.6 BY 1.4 ;
  SYMMETRY X Y ;
  SITE core7 ;
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER MET1 ;
        RECT 0.055 0.625 0.235 0.775 ;
    END
  END A
  ...
END INVX1H7H
```

**（c）_ecos 版 LEF 片段**——同一单元的「开源工具友好」形态：

[ics55_LLSC_H7CH_ecos.lef:L34857-L34910](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L34857-L34910)（电源脚前置 + 每个信号脚补 MET2/VIA1）：

```lef
MACRO INVX1H7H
  ...
  SITE core7 ;
  PIN VDD                       # 电源脚挪到最前
    DIRECTION INOUT ; USE POWER ; SHAPE ABUTMENT ;
    PORT
      LAYER MET1 ;
        RECT 0 1.32 0.6 1.48 ;
        ...
  PIN A
    DIRECTION INPUT ; USE SIGNAL ;
    PORT
      LAYER MET1 ;
        RECT 0.055 0.625 0.235 0.775 ;
      LAYER MET2 ;              # 新增：高层可达
        RECT 0.05 0.43 0.15 0.96 ;
      LAYER VIA1 ;              # 新增：连接两层
        RECT 0.055 0.655 0.145 0.745 ;
    END
  END A
  ...
END INVX1H7H
```

**（d）verilog 模块**——门原语 + `ifdef functional` 切换延迟：

[ics55_LLSC_H7CH.v:L16074-L16089](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L16074-L16089)（INVX1H7H 模块全文；文件级模板见 [L17-L18](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L17-L18) 的 `timescale + `celldefine 前导）：

```verilog
`timescale 1ns/1ps
`celldefine
module INVX1H7H (Y,A);
output Y;
input A;

  not I0(Y, A);

`ifdef functional // functional //
`else // functional //
specify
(A => Y) = (1.0,1.0);

endspecify
`endif // functional //
endmodule //INVX1H7H
`endcelldefine
```

**（e）CDL 网表**——`.SUBCKT` + `.PININFO` + 模板实例：

[ics55_LLSC_H7CH.cdl:L8612-L8615](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L8612-L8615)（INVX1H7H：一行 X 语句实例化 ICSCORE 的 INV 模板并传 W/L 形参；模板定义在同文件 [L21-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L21-L25)，用 `nm1p2_hvt_lp/pm1p2_hvt_lp` 器件）：

```text
.SUBCKT INVX1H7H A VDD VSS Y
*.PININFO A:I Y:O VDD:B VSS:B
XXI0 A VDD VSS Y / INV pl=6e-08 pw=2.7e-07 nl=6e-08 nw=2.1e-07
.ENDS
```

五份视图合起来就是一份**单元契约**：名字 INVX1H7H、端口 {A:in, Y:out, VDD/VSS:电源}、尺寸 0.6×1.4、晶体管 2 只（1N1P）。

#### 4.2.4 代码实践

**实践目标**：写一个「契约卡提取器」，自动从五个视图抽取 INVX1H7H 的契约字段，输出的卡片即 4.2.1 清单的机器可读版——它是综合实践（第 5 节）的验收工具。

**操作步骤**（示例代码，保存为 `~/contract_card.py`，仓库根目录运行）：

```python
#!/usr/bin/env python3
# 示例代码：从五视图抽取单元契约卡
import re, pathlib
ROOT = pathlib.Path(".")
CH = ROOT/"IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH"
cell = "INVX1H7H"

def between(text, start, end):
    m = re.search(rf"^{start}.*?^{end}", text, re.M | re.S)
    return m.group(0) if m else ""

lef = (CH/"lef/ics55_LLSC_H7CH.lef").read_text()
macro = between(lef, rf"MACRO {cell}", rf"END {cell}")
size = re.search(r"SIZE ([\d.]+) BY ([\d.]+)", macro).groups()
pins = re.findall(r"^  PIN (\S+)\n    DIRECTION (\w+)", macro, re.M)

vlog = (CH/"verilog/ics55_LLSC_H7CH.v").read_text()
mod = between(vlog, rf"module {cell}", "endmodule")
vports = re.findall(r"^(input|output|inout) (\w+);", mod, re.M)

cdl = (CH/"cdl/ics55_LLSC_H7CH.cdl").read_text()
sub = between(cdl, rf"\.SUBCKT {cell}", ".ENDS")
cports = sub.splitlines()[0].split()[2:]
pinfo = dict(re.findall(r"(\w+):([IOB])", sub))

cl = (CH/"cell_list/ics55_LLSC_H7CH.txt").read_text()
print(f"cell_list : {'命中' if cell in cl.split() else '未登记'}")
print(f"LEF SIZE  : {size[0]} x {size[1]} um")
print(f"LEF pins  : {pins}")
print(f"Vlog ports: {vports}")
print(f"CDL ports : {cports}  PININFO={pinfo}")
```

**需要观察的现象**：三份视图的端口集合是否同名同向（LEF 的 VDD/VSS 为 INOUT，对应 CDL 的 B 标记；标准单元 verilog 无电源端口——这是 u5-l2 讲过的视图差异，属正常）。

**预期结果**（按源码手工推导；**待本地验证**具体打印格式）：

```text
cell_list : 命中
LEF SIZE  : 0.6 x 1.4 um
LEF pins  : [('A','INPUT'), ('VDD','INOUT'), ('VSS','INOUT'), ('Y','OUTPUT')]
Vlog ports: [('output','Y'), ('input','A')]
CDL ports : ['A','VDD','VSS','Y']  PININFO={'A':'I','Y':'O','VDD':'B','VSS':'B'}
```

**预期结果（核对）**：把 `cell` 换成任意单元名重跑，卡片仍应自洽；换成一个只在 CDL 出现的名字（如 `SDFFRQX3H7H`，u5-l2），则 cell_list/LEF/verilog 三处会报「未登记/空」——这正是一致性检查器要抓的差异。

#### 4.2.5 小练习与答案

**练习 1**：驱动档位 INVX9H7H 目前不存在（INV 家族有 X0P5~X8、X10、X12、X16、X20，独缺 X9）。若要新增它，五份视图各需要改什么？

**答案**：cell_list 加一行 `INVX9H7H`（字符串序插在 `INVX8H7H` 之后，因为 `8 < 9`）；LEF/_ecos LEF 各加一个 MACRO（宽度取 0.2 的整数倍，按 X8 与 X10 之间插值设计版图）；verilog 加模块（门原语与 INVX1H7H 相同，specify 占位延迟可先沿用）；CDL 加 `.SUBCKT`（复制 X8 的 X 行，W 参数按驱动比例放大）；liberty 需特性仿真后由维护方经 Release 更新——git 内 PR 覆盖不了它。

**练习 2**：为什么 verilog 模型可以「零延迟占位」，LEF 几何却必须精确？

**答案**：门级仿真验证的是逻辑功能，占位延迟不影响功能判定，真实时序靠 SDF 反标（u3-l5/u6-l2）；LEF 几何直接决定布线器在哪里打线、单元占多大面积，错一个 RECT 就会造成实际短路/开路，属于物理正确性问题。

**练习 3**：`*_ant.lef`（u3-l4）与本讲的 `_ecos` 变体是什么关系？

**答案**：两者都是「原版 + 单一目的增量」的平行变体，但增量正交：ant 版只加 ANTENNA 属性（2 行）、_ecos 版做开源工具适配。使用时二选一与原版组合，不能同时把 ant 和 ecos 的增量叠在一个文件里（仓库没有提供叠加版）。

### 4.3 Apache-2.0 header 与贡献流程

#### 4.3.1 概念说明

[README.md:L118-L122](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L118-L122) 把 Apache-2.0 的义务概括为两句：**分发时必须保留版权、专利、商标与归属声明；修改必须在被改文件的头部注释中注明**。并且明确「每个源文件都带 Apache-2.0 header，以保证许可约束延伸到每个组件文件」。

对二次开发者的直接要求：

1. **新建文件**：照抄 header 模板，用目标格式的注释语法包裹；
2. **修改现有文件**：保留原 header，并在其后追加修改说明（谁、何时、改了什么）；
3. **逐文件**：不是每目录一个 NOTICE，而是每个文件自证许可。

#### 4.3.2 核心流程

一次合格贡献的完整动线：

```text
fork 仓库 → git switch -c <特性分支>
   → 修改/新增视图文件（4.2 清单）
   → 新文件加 header；改动文件在 header 下追加修改注记
   → 本地验收：4.1.4 审计脚本 + 4.2.4 契约卡 + u5-l2 一致性检查
   → git add … && git commit -s        # -s 生成 Signed-off-by（DCO）
   → git push 并开 PR（描述动机、影响面、验证方法）
   → 评审（所有提交都要评审，含维护者）→ 合入
```

两个容易踩的坑：

- **改原版还是改 _ecos 版？** 工具适配类改动（补 RC、补 SITE 等）只进 `_ecos` 版；数据错误类改动（如引脚方向、拼错的引脚名）应同时修两版——参考 `e5c881b` 只修了 `_ecos` 版，导致原版仍留 48 处同类问题（4.1.3（d）），这就是「该改两版只改了一版」留下的尾巴。
- **大文件通道**：liberty/GDS 不在 git 内，相关修正无法走普通 PR，应先通过 README 联系表（[L142-L144](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L142-L144)，ecos-all@ict.ac.cn）与维护者沟通。

#### 4.3.3 源码精读

**（a）header 模板原文**——README 给出的标准文本：

[README.md:L124-L138](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L124-L138)（15 行模板，`Copyright 2025 ICsprout Integrated Circuit Co., Ltd.` + 许可声明 + 许可链接 + 免责声明）。

仓库里三种注释语法的真实用法：

| 格式 | 注释语法 | 实例 |
| --- | --- | --- |
| LEF | `#` 行注释 | [N551P6M_ecos.lef:L1-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L1-L13) |
| CDL | 行首 `*` | [ics55_LLSC_H7CH.cdl:L1-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L1-L13) |
| Verilog / liberty | `/* */` 块注释 | [ics55_LLSC_H7CH.v:L1-L15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L1-L15) |

三份 header 正文逐字相同，只有包裹符号不同——新建文件时照对应语法抄即可。

**（b）一个诚实的例外**：cell_list 四个 `.txt` 是纯名单文件，第 1 行直接是单元名（见 [ics55_LLSC_H7CH.txt:L1](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1)），**没有 license header**（可能因为 `#`/`*` 都不是该文件格式的注释符，加了会破坏消费方解析——**待上游确认**）。仓库元文件（Makefile、.gitignore、CONTRIBUTING.md、CODE_OF_CONDUCT.md）同样不带。实践结论：**跟随所在文件的现状**——往 cell_list 加条目时不发明 header；新建 LEF/CDL/verilog 文件时必须带 header。

**（c）贡献规则的三个文件**：

- [CONTRIBUTING.md:L6-L14](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md#L6-L14)：DCO 要求——贡献保留你的版权，但须附 Developer Certificate of Origin 声明（sign-off 即表示同意）；
- [CONTRIBUTING.md:L16-L20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md#L16-L20)：所有提交（含项目成员）一律经 GitHub PR 评审；
- [CONTRIBUTING.md:L22-L24](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md#L22-L24)：遵循 Contributor Covenant 行为准则（CODE_OF_CONDUCT.md）。

另外 [README.md:L5-L8](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L5-L8) 的 Todo（RAM、DRC/LVS 规则、SPICE 模型、PDN、RC、用户文档）就是官方发布的「求贡献方向清单」；[L61](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L61) 明确 ECOS Team 正在持续修复「与主流开源 EDA 工具链的兼容性问题」——`_ecos` 变体与 `e5c881b` 这类提交就是这句话的落地。

#### 4.3.4 代码实践

**实践目标**：写一个 license header 审计脚本，盘点全仓库哪些文件带/不带 `Copyright 2025 ICsprout` 声明，输出一张「header 状态表」——这是任何涉及新增文件的 PR 提交前的自查步骤。

**操作步骤**（示例代码，在仓库根目录运行）：

```bash
# 示例代码：对全部 git 跟踪文件检查版权串
git ls-files | while read -r f; do
  case "$f" in *.pdf) echo "SKIP  $f"; continue;; esac
  if head -20 "$f" | grep -q "Copyright 2025 ICsprout"; then
    echo "OK    $f"
  else
    echo "MISS  $f"
  fi
done | sort | uniq -c | sort -rn     # 汇总
```

**需要观察的现象**：OK 与 MISS 两类文件的分布规律。

**预期结果**（已用等价 grep 验证）：41 个跟踪文件中，**29 个文本文件带 header**——全部 13 个 LEF（含 _ant/_ecos）、4 个 CDL、4 个 verilog、6 个 IO liberty、README.md、LICENSE；**9 个 MISS**——4 个 cell_list `.txt`、Makefile、.gitignore、CONTRIBUTING.md、CODE_OF_CONDUCT.md；3 个 PDF 跳过。

**预期结果（判断）**：MISS 清单印证 4.3.3（b）的结论——数据文件全带，纯名单/工程元文件不带。给自己的新文件选边时：LEF/CDL/verilog/liberty 类必须 OK，工程脚本可跟随 MISS 现状（或与维护者在 issue 中先讨论）。

#### 4.3.5 小练习与答案

**练习 1**：你为 H7CH 修正了 3 个引脚方向，PR 里应该改哪些文件？

**答案**：数据错误类问题应同时修原版与 _ecos 版两份 LEF（`ics55_LLSC_H7CH.lef` 与 `ics55_LLSC_H7CH_ecos.lef`）；若 H7CL/H7CR 存在同样问题（三库平行），一并修复并逐文件在 header 下追加修改注记。`e5c881b` 只修 _ecos 版留下原版残留，是反面教材。

**练习 2**：`git commit -s` 与 `git commit --amend` 连用时要注意什么？

**答案**：`-s` 只是向 message 追加 `Signed-off-by:` 行（DCO 签署）；已推送的提交被 amend 后 sign-off 行不会自动重加，重写历史时要再次 `-s`（或 `git interpret-trailers --add`）。DCO 检查通常校验 sign-off 邮箱与作者邮箱一致。

**练习 3**：为什么本仓库选择「每个文件都放 header」而不是只放根目录 LICENSE？

**答案**：PDK 文件常被单独抽取使用（例如只把某个 LEF 拷进自己的工程）。文件级 header 保证任何单个文件被复制分发时都携带许可信息，法律约束不因拆包而丢失——README L122 明说这是「确保法律效力延伸到每个组件文件」的措施。

## 5. 综合实践：模拟一次完整贡献

**任务**：以 H7CH 中已有的最小单元 **INVX1H7H** 为标本，演练「假如它是本次要提交的新单元/新版本」——在**仓库之外**的沙盒目录手工生成全套视图变更、写说明文档、模拟提交流程。全程不修改仓库文件。

**步骤 1：建沙盒并采集标本**（把 4.2.3 的五段原文各自存档，作为 diff 基准）：

```bash
mkdir -p ~/ics55-dryrun && cd ~/ics55-dryrun
git -C <仓库路径> show HEAD > base_commit.txt     # 记录基准 HEAD
sed -n '29793,29838p' <仓库路径>/IP/STD_cell/.../lef/ics55_LLSC_H7CH.lef > inv.lef.orig
sed -n '34857,34910p' <仓库路径>/IP/STD_cell/.../lef/ics55_LLSC_H7CH_ecos.lef > inv_ecos.lef.orig
sed -n '16074,16089p' <仓库路径>/IP/STD_cell/.../verilog/ics55_LLSC_H7CH.v > inv.v.orig
sed -n '8612,8615p'   <仓库路径>/IP/STD_cell/.../cdl/ics55_LLSC_H7CH.cdl  > inv.cdl.orig
```

**步骤 2：生成五份「新版本视图」文件**（示例代码——以下是本讲为演练新写的片段，非项目原有内容；每份都以正确注释语法的 Apache-2.0 header 开头）：

- `cell_list.patch.txt`：一行 `INVX1H7H`，注明应插入字符串序位置（INVX16H7H 与 INVX1P4H7H 之间）；
- `inv.lef.new`：header（`#` 语法）+ 步骤 1 的原版 MACRO 原文；
- `inv_ecos.lef.new`：header + _ecos 版 MACRO，并**手工重构**其三处增量——电源脚前置、信号脚补 `LAYER MET2`/`LAYER VIA1`、确认 `VERSION 5.8` 由整文件统一声明；
- `inv.v.new`：header（`/* */` 语法）+ `` `timescale``/`` `celldefine`` 前导 + 模块（门原语 + `` `ifdef functional`` 分支 + specify 占位延迟）；
- `inv.cdl.new`：header（`*` 语法）+ 横幅注释（Library/Cell/View Name 三行）+ `.SUBCKT`/`*.PININFO`/X 行。

**步骤 3：写 `DIFF_NOTES.md`**（模拟 PR 描述），至少回答四个问题：

1. 每份视图改了什么（对照步骤 1 的 `.orig`）；
2. 为什么 _ecos 版要加 MET2/VIA1 而原版不加（类别③：开源布线器高层可达性）；
3. 电源脚为什么两版都保留 ABUTMENT（行边界对接成轨，非 _ecos 增量）；
4. liberty/GDS 为什么不在本次变更里（不在 git，走 Release 通道）。

**步骤 4：本地验收**——把 4.2.4 的契约卡脚本指向沙盒文件，核对：LEF 尺寸 0.6×1.4；LEF 引脚 {A:INPUT, Y:OUTPUT, VDD/VSS:INOUT}；verilog 端口 {Y,A}；CDL 端口 {A,VDD,VSS,Y} 与 PININFO 一致；`grep -c 'LAYER MET2' inv_ecos.lef.new` 得 2（A、Y 各一）。

**步骤 5：模拟提交**（在自己的 fork 或干脆 `git init` 的沙盒仓库里）：

```bash
git init && git add -A
git commit -s -m "feat(std_cell): add view set for INVX1H7H (dry-run)

- cell_list entry + normal LEF + _ecos LEF + verilog model + CDL netlist
- all new files carry Apache-2.0 headers
- verified by cross-view contract card check"
git log -1 --format=%B        # 观察 Signed-off-by 行是否生成
```

**预期结果**：

1. 沙盒里 5 份新文件 + 2 份说明文档，全部带正确语法的 header；
2. 契约卡输出与步骤 4 的清单逐项一致；
3. `git log -1` 的 message 末尾出现 `Signed-off-by: 你的名字 <邮箱>`。

**待本地验证**：sed 行号抽取依赖当前 HEAD（68d89ed）的文件内容；若仓库已更新，先用 `grep -n '^MACRO INVX1H7H'` 重新定位行号再抽取。步骤 5 的 push/开 PR 环节需要你自己的 GitHub fork 与网络环境，属可选项——演练到本地 commit 即视为完成。

## 6. 本讲小结

- **四类适配**构成 `_ecos` 变体的全部增量：①tech LEF 补 7 层电容 + OFFSET 半节距平移 + 删悬空 DefaultTaper；②IO LEF 46 处 `USE GROUND` 修正；③标准单元 3499 个信号脚补 MET2/VIA1、IO LEF 补 IOSite/IOCorner 定义；④53 处 DIRECTION 修正（IO 2 处 + 标准单元 326 处，后者由提交 `e5c881b` 经 PR #20 合入）。
- **电源 ABUTMENT 轨道两版皆有**，不是 `_ecos` 增量；VERSION 抬升只发生在标准单元 LEF（5.7→5.8）。
- **一个单元 = 五份视图契约**（cell_list、原版 LEF、_ecos LEF、verilog、CDL；liberty/GDS 走 Release 通道不在 git），新增/修改必须五处同步，用契约卡与一致性检查验收。
- **Apache-2.0 的实操要求**：新文件带 header（LEF 用 `#`、CDL 用 `*`、Verilog/liberty 用 `/* */`），修改须在文件头注明；cell_list 等纯名单文件现状无 header，跟随现状。
- **贡献动线**：分支 → 修改（适配类只进 _ecos，数据错误类两版同修）→ header → 本地审计 → `git commit -s`（DCO）→ PR 评审；大文件问题先联系 ecos-all@ict.ac.cn。
- 仓库仍有可量化的候选贡献：H7CH 两版 LEF 各残留 48 个 SN/SI/SE 引脚 `DIRECTION OUTPUT`（**待上游确认**），Todo 中的 RC/PDN/文档也是官方指明的方向。

## 7. 下一步学习建议

至此六个单元十九讲全部完成。接下来建议三条路线：

1. **真刀真枪提一个 PR**：从 4.1.3（d）的 48 处残留 DIRECTION 问题入手（先开 issue 与维护者确认是否为漏修），或修复 u5-l2 发现的视图不一致（SDFFRQX3H7H 只在 CDL 出现）；小而可验证的 PR 最容易被合入。
2. **跑通完整设计闭环**：用 u6-l1/u6-l2 的脚本把一个小 RTL（如本讲提到的 8 位计数器）从 yosys 综合推进到 OpenROAD 布局布线，用本讲的审计脚本在关键节点核对 PDK 数据，体验 `_ecos` 适配在每个环节的实际作用。
3. **跟踪上游演进**：用 `git log --oneline -- <路径>` 定期查看三个 lef 目录与 Makefile 的变化；README 的 Todo（RAM、DRC/LVS、SPICE 模型、PDN）每落地一项，本手册对应章节就值得按 update 模式重读一遍。
