# 单元 LEF 抽象视图解剖

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段读懂一个标准单元 MACRO 的头部：`CLASS`、`ORIGIN`、`FOREIGN`、`SIZE`、`SYMMETRY`、`SITE`，并说清楚布局布线工具分别拿它们做什么。
2. 掌握 `MACRO → PIN → PORT → LAYER → RECT` 的四层嵌套结构，理解 `DIRECTION`、`USE`、`SHAPE` 三个引脚属性的含义。
3. 理解 `OBS`（布线障碍区）是什么、为什么 54 个单元没有它。
4. 会写脚本从 LEF 中提取单元几何数据，画出引脚分布示意图，并据此估算单元面积。

本讲只解剖**普通版**单元 LEF（`ics55_LLSC_H7CH.lef`）。带 `_ecos` 后缀的适配版差异（电源轨道引脚补充、高层引脚可达性）留到下一讲 u3-l3 专门对比。

## 2. 前置知识

### 2.1 什么是「抽象视图」

在 u1-l1 我们说过，PDK 用多种「视图」描述同一批单元。GDS 是**完整版图**——包含每一块金属、每一个晶体管的多边形，体积巨大且细节过多；而布局布线工具其实不需要知道单元内部长什么样，它只需要回答三个问题：

1. 这个单元**占多大地方**？（尺寸）
2. 它的**引脚在哪儿**、在哪层金属上？（连接点）
3. 单元内部**哪些区域我不能走线**？（障碍）

回答这三个问题的「浓缩版版图」就是单元 LEF（Library Exchange Format）中的 **MACRO**。一个 GDS 动辄几十万字节，而一个 MACRO 只有几十行文本，79580 行的 LEF 文件就装下了全部 785 个标准单元的抽象。

### 2.2 坐标系与单位

LEF 中所有几何坐标的单位是**微米（μm）**（由 tech LEF 的 `UNITS` 决定，见 u2-l1），坐标系原点在单元**左下角**，x 向右、y 向上。这与 u2-l1 学过的 `MANUFACTURINGGRID 0.001`（1nm 网格）一致，所以坐标最多出现 3 位小数。

### 2.3 site 与行高（承接 u2-l2）

u2-l2 讲过 SITE 是行式布局的最小格点。ICS55 的标准单元全部落在 `core7` 上：

[prtech/techLEF/N551P6M.lef:660-664](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L660-L664)
—— `SITE core7` 定义宽 0.200 μm、高 1.400 μm。名字里的 7 指行高恰好是 MET1 节距（[prtech/techLEF/N551P6M.lef:65-65](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L65-L65) 中 `PITCH 0.2 0.2`）的 7 倍：\( 1.4 = 7 \times 0.2 \)。本讲会反复用到「0.2 的整数倍」这个量化规律。

### 2.4 承接 u3-l1 的命名

u3-l1 解码过单元名：`ADDFX1H7H` = 全加器（ADDF）+ 驱动强度 X1 + 库后缀 H7H（HVT 库）。本讲以它为主角，同时对照最小功能单元 `ANT2H7H`（天线二极管）和填充单元 `FILLER1H7H`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef` | 本讲主角。H7CH（HVT）库全部 785 个单元的 MACRO 抽象，79580 行 |
| `prtech/techLEF/N551P6M.lef` | 交叉引用：`SITE core7` 的定义地（u2-l1/u2-l2 已精读） |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt` | 对照：cell_list 只列 748 个单元，LEF 多出的 37 个 ANT 单元（u1-l2 已发现） |

文件开头三行全局声明值得先认识一下：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:15-17](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L15-L17)
—— `VERSION 5.7` 声明 LEF 语法版本（`_ecos` 版升级到 5.8）；`BUSBITCHARS "[]"` 告诉工具位总线下标用方括号（如 `data[3]`）；`DIVIDERCHAR "/"` 定义层次分隔符。这两行让 LEF 引脚名能与 Verilog 网表名互相翻译。

## 4. 核心概念与源码讲解

### 4.1 模块一：MACRO 头部字段

#### 4.1.1 概念说明

`MACRO` 是一个单元的「名片 + 外轮廓」。布局工具读它来做**布局合法性检查**（单元是否落在格点上、行内是否对齐），布线工具读 `SIZE` 来圈定「这个单元框内不能乱穿线」的范围。头部的六个字段各管一件事：

| 字段 | 例子（ADDFX1H7H） | 含义 | 谁用它 |
| --- | --- | --- | --- |
| `CLASS` | `CORE` | 单元类别。CORE=逻辑区行式单元（对照 IO 库的 `CLASS PAD`，见 u4-l2） | 布局器（决定能放进哪种行） |
| `ORIGIN` | `0 0` | GDS 版图坐标系原点相对 MACRO 原点的偏移 | 版图导出/DEF 回写 |
| `FOREIGN` | `ADDFX1H7H 0 0` | 指向 GDS 里真实版图单元的名字与偏移，即「抽象 ↔ 完整版图」的挂钩 | 流片数据合并 |
| `SIZE` | `4.8 BY 1.4` | 外框宽 × 高（μm），面积 = 宽 × 高 | 布局器（面积估算）、布线器（障碍外框） |
| `SYMMETRY` | `X Y` | 允许的放置变换：可绕 x 轴翻转、绕 y 轴翻转 | 布局器（等价摆放） |
| `SITE` | `core7` | 本单元占用的格点类型 | 布局器（行对齐与宽度量化） |

#### 4.1.2 核心流程

布局工具放置一个单元时的判断链：

```
读 MACRO
  ├─ CLASS CORE      → 只能放进 core 行（不能进 pad 环）
  ├─ SITE core7      → 宽度必须量化到 0.2 的整数倍，高度恰为一行 1.4
  ├─ SIZE 4.8 BY 1.4 → 占 4.8/0.2 = 24 个 site，行内横跨 24 格
  ├─ SYMMETRY X Y    → 允许水平/垂直镜像摆放，共用同一抽象
  └─ FOREIGN         → 最终导出 GDS 时按这个名字替换回完整版图
```

关键量化规律（本讲用脚本验证）：**高恒为 1.4（单行单元），宽恒为 0.2 的整数倍**。所以单元面积天然是「最小格面积 \(0.2 \times 1.4 = 0.28\ \mu m^2\)」的整数倍。

#### 4.1.3 源码精读

**主角 ADDFX1H7H 的头部**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:19-25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L19-L25)
—— 全加器抽象的开头：CORE 类、零偏移、SIZE 4.8×1.4（24 个 site 宽）、可双向镜像、落在 core7 上。

**对照组一：最小的功能单元 ANT2H7H**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:3048-3054](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048-L3054)
—— 天线二极管只有 0.4×1.4（2 个 site 宽）。注意它也在 cell_list 之外（u1-l2 发现的 37 个 ANT 单元之一），却在 LEF 里有完整 MACRO——**LEF 才是抽象视图的全集**。

**对照组二：最窄的单元 FILLER1H7H**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:27970-27976](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L27970-L27976)
—— 填充单元恰为 1 个 site 宽（0.2×1.4），是全库最小格。它没有信号引脚（后文 4.2.3 会再看它的电源轨道）。可见**规格里说的「最小单元 ANT2H7H」是指最小功能单元**；若把 filler 算进来，FILLER1H7H 才是最小的。

**全库头部统计**（笔者用 grep 对 79580 行文件实际统计）：

| 统计项 | 数值 | 命令线索 |
| --- | --- | --- |
| `MACRO` 总数 | 785 | `grep -c "^MACRO"` |
| `CLASS` 取值 | 全部 785 个都是 `CORE` | `grep "  CLASS"` 后 `sort \| uniq -c` |
| `SITE core7 ;` 引用数 | 785（每个 MACRO 恰好一次） | `grep -c "SITE core7 ;"` |
| `SIZE` 高度取值 | 全部为 `BY 1.4` | `grep "^  SIZE"` 后 `sort \| uniq -c` |
| `SIZE` 宽度范围 | 0.2（仅 FILLER1H7H）~ 12.8（FILLER64H7H、TINVX16H7H） | 同上 |

宽度分布是离散的 0.2 步进序列：0.2/0.4/0.6/0.8/1.0/…/9.8/12.8，无一例外是 0.2 的整数倍——这就是 `SITE core7` 宽度量化在数据上的直接体现。

#### 4.1.4 代码实践

**实践目标**：用脚本验证「宽度量化到 0.2、高度恒为 1.4」，并找出全库最窄/最宽单元。

**操作步骤**（示例代码，可保存为 `size_stats.py` 在仓库根目录运行）：

```python
# 示例代码：统计单元 LEF 的 SIZE 分布
import re
from collections import Counter

path = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef"
macro = None
sizes = {}                      # 单元名 -> (宽, 高)
for line in open(path):
    if line.startswith("MACRO "):
        macro = line.split()[1]
    elif line.startswith("  SIZE") and macro:
        m = re.match(r"\s*SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", line)
        sizes[macro] = (float(m.group(1)), float(m.group(2)))

widths = Counter(w for w, h in sizes.values())
print("MACRO 总数:", len(sizes))
print("高度集合:", set(h for w, h in sizes.values()))
print("宽度对 0.2 取余不为 0 的单元:",
      [k for k, (w, h) in sizes.items() if abs(w / 0.2 - round(w / 0.2)) > 1e-9])
print("最窄:", min(sizes.items(), key=lambda kv: kv[1][0]))
print("最宽:", max(sizes.items(), key=lambda kv: kv[1][0]))
```

**需要观察的现象**：高度集合只有一个值 `{1.4}`；「取余不为 0」列表为空；最窄是 `FILLER1H7H (0.2, 1.4)`，最宽是 `FILLER64H7H` 或 `TINVX16H7H`（都是 12.8，`min`/`max` 只返回先遇到者）。

**预期结果**：与上表统计一致（785 个、宽度全部量化）。具体打印格式**待本地验证**（脚本未在讲义编写环境中执行，统计结论来自 grep）。

#### 4.1.5 小练习与答案

**练习 1**：`FOREIGN ADDFX1H7H 0 0` 里第一个 `ADDFX1H7H` 和 MACRO 名相同，这一定是巧合吗？去 GDS 视图（u1-l3：GDS 需 `make unzip` 下载）或 CDL 中找证据。

**答案**：不是巧合。`FOREIGN` 就是指向 GDS 完整版图单元名的挂钩，本库约定抽象与版图同名；CDL 中 `.SUBCKT ADDFX1H7H ...`（u5-l1 会精读）同样沿用该名字。三个视图共用一个名字正是 u5-l2 一致性检查的基础。若某天出现不同名，第二列的 `0 0` 偏移用于对齐两个坐标系。

**练习 2**：为什么全库高度只有 1.4 一种，宽度却有几十种？

**答案**：标准单元采用「行式布局」：一行高度固定（core7 高 1.4），单元像积木一样横向拼接；功能越复杂、驱动越强，需要的晶体管越多，宽度就越大，但高度不变才能让任意单元共行堆叠、电源轨对齐。宽度必须量化到 site 宽（0.2），保证左右相邻单元严丝合缝。

**练习 3**：`SYMMETRY X Y` 对布局器意味着什么节省？

**答案**：声明单元可绕 x 轴、y 轴镜像摆放后，镜像后的摆放**不需要新的抽象数据**——布局器直接复用同一份 MACRO，把几何做变换即可。这既能提高布局密度（把引脚朝向需要的方向），也让库数据量减半（不必为镜像版单列单元）。

### 4.2 模块二：PIN/PORT/RECT 结构

#### 4.2.1 概念说明

引脚描述是四层嵌套：

```
MACRO
 └─ PIN <引脚名>          电学上一个节点
     ├─ DIRECTION ...     方向：INPUT / OUTPUT / OUTPUT TRISTATE / INOUT
     ├─ USE ...           用途：SIGNAL / POWER / GROUND
     ├─ SHAPE ...         形状类别：ABUTMENT（对接轨道）等
     └─ PORT              物理上一个可放置的连接图形组
         ├─ LAYER MET1    该组图形所在层
         └─ RECT x1 y1 x2 y2   矩形（可重复多个）
```

三个属性的区别要分清：

- **`DIRECTION` 是电学方向**，给综合/时序工具看：信号从哪儿进、哪儿出。
- **`USE` 是用途分类**，给布线器看：`SIGNAL` 引脚要连线，`POWER`/`GROUND` 引脚交给电源网络（PDN）工具，普通信号布线不碰它们。
- **`SHAPE ABUTMENT` 表示「对接形」**：这个引脚的形状专门设计成与相邻单元的同名引脚在拼接时物理连通，不需要任何布线——这正是电源轨道的实现方式。

一个 `PIN` 可以只有一个 `PORT`（本库全部如此：785 个单元共 5069 个 `PORT` 层语句，与引脚总数相等），但一个 `PORT` 里可以有**多个 `RECT`**——同一电学节点的金属被拆成若干块不连通的碎片（版图上由内部更低层连通），布线器连到任意一块即可。

#### 4.2.2 核心流程

布线器给某条网络接线时，对目标引脚的处理：

```
1. 按 USE 过滤：POWER/GROUND 引脚不属于信号布线（交给 PDN）
2. 在 PORT 的 LAYER（本库全是 MET1）上，把 RECT 与布线轨道求交
3. 交集非空的轨道点就是「可接入点」（access point）
4. 多 RECT 引脚任选一块接入；RECT 越大、跨越轨道越多，接入点越多
```

所以引脚矩形的**位置和大小直接决定布线难度**：太小的引脚可能不落在任何轨道上，导致布线器绕远路甚至失败——这正是 `_ecos` 版要「补高层引脚」的动机（u3-l3 展开）。

#### 4.2.3 源码精读

**最简单的引脚：ADDFX1H7H 的输入 A**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:26-33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33)
—— 输入、信号用、MET1 上一块 0.12×0.225 的小矩形，位于单元左半部（x≈0.43–0.55）。

**多矩形引脚：进位输入 CI**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:42-56](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L42-L56)
—— 同一个 PORT 里 8 块 RECT，横向铺满 x≈1.56–3.02 的中段。这是版图抽象的典型产物：内部 MET1 走线被拆成多段，但电学上是同一节点；对布线器而言意味着 CI 有 8 个候选接入区。输出引脚 CO、S（[L57-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L57-L74)）同理各含 2 块竖条矩形。

**电源轨道：VDD / VSS 与「越界」的矩形**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:75-100](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L75-L100)
—— VDD（USE POWER）主矩形是 `RECT 0 1.32 4.8 1.48`，VSS（USE GROUND）主矩形是 `RECT 0 -0.08 4.8 0.08`。注意**y 坐标超出了 SIZE 的 0~1.4 边界**：轨道分别以行边界 y=1.4 和 y=0 为中心线、各 0.16 高，左右相邻单元拼接（abut）时首尾相接成横贯整行的电源轨，上下两行再共享同一条边界轨。这就是 `SHAPE ABUTMENT` 的字面含义——「对接即连通，无需布线」。其余 4 块小矩形是单元内部把轨道垂下拉到晶体管的竖条。

**三态输出：TBUFX0P5H7H 的 Y**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:73687-73696](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L73687-L73696)
—— `DIRECTION OUTPUT TRISTATE`：输出但可高阻。全库仅 22 个这种引脚（都属 TBUF/TINV 三态族），综合工具在推断三态总线时才会用到。

**全库引脚统计**（grep 实测）：5069 个引脚 = 2584 `INPUT` + 893 `OUTPUT` + 22 `OUTPUT TRISTATE` + 1570 `INOUT`。1570 恰为 785×2——**每个单元都有一对 VDD/VSS**（`grep -c "^  PIN VDD"` = 785）。所有引脚和 OBS 的 `LAYER` 全部是 `MET1`：普通版把一切抽象压在第一层金属上，这一事实是理解 `_ecos` 版「补 MET2/VIA1 引脚」价值的前提。

#### 4.2.4 代码实践

**实践目标**：提取 ADDFX1H7H 的全部引脚矩形，画出版图抽象示意图。

**操作步骤**：使用第 5 节综合实践中的解析脚本（`parse_macro.py`），运行 `python3 parse_macro.py ADDFX1H7H > addfx.txt`。

**需要观察的现象**：输出应包含 7 个引脚（A、B、CI、CO、S、VDD、VSS）、每个引脚的 RECT 数（依次为 1/1/8/2/2/5/5），以及一张按坐标绘制的字符示意图。

手工推导后的引脚分布示意图（供对照，非严格比例；上下两条粗轨为电源）：

```
y(μm)
1.48 ┌────────────────────────────────────────┐ ← VDD 轨 (1.32~1.48)
1.2  │              [OBS 区]                  │
0.9  │                        ▌CO▐  ▌S▐      │ ← CO/S 竖条引脚
0.6  │  ▐B▌ ▐A▌  ▭▭▭▭ CI(8 块)▭▭▭▭           │ ← 输入引脚带
0.3  │                                        │
0.08 └────────────────────────────────────────┘ ← VSS 轨 (-0.08~0.08)
     0    1         2         3         4    4.8  → x(μm)
```

**预期结果**：脚本输出的引脚表与上图位置关系一致；CI 的 8 块矩形集中在 x∈[1.56, 3.02]、y∈[0.425, 0.62] 的横带内。字符图的具体渲染**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：ADDFX1H7H 有 7 个引脚，其中信号引脚几个？方向如何分布？

**答案**：信号引脚 5 个——输入 A、B、CI（`DIRECTION INPUT`），输出 CO、S（`DIRECTION OUTPUT`）；这与全加器「三入两出」（A+B+CI→本位 S 与进位 CO）的逻辑完全对应。引脚方向信息与 liberty/verilog 视图应一致（u5-l2 检查）。

**练习 2**：为什么 VDD/VSS 的 `DIRECTION` 是 `INOUT` 而不是 `INPUT`？

**答案**：电源引脚既给单元供电、也可能作为穿过单元的电源路径的一部分（轨道首尾相接），电流方向不定，故标 `INOUT`；真正区分它身份的是 `USE POWER/GROUND`。布线器按 `USE` 把它移交给电源网络工具。

**练习 3**：数一数：全库 1570 个 INOUT 引脚是怎么算出来的？如果某单元漏写 VDD 会导致什么？

**答案**：785 个 MACRO × (VDD + VSS) = 1570，与 `DIRECTION` 统计吻合（2584+893+22+1570=5069=引脚总数）。漏写 VDD 的单元在 PDN 综合时得不到电源连接，形式验证（power connectivity check）会报「悬空电源」，流片后该单元不工作。这类「电源引脚完整性」正是 u3-l3 中 `_ecos` 版重点修补的内容之一。

### 4.3 模块三：OBS 障碍区

#### 4.3.1 概念说明

`OBS`（obstruction，障碍）声明单元内部**既不是引脚、又会挡住布线**的金属图形。单元内部晶体管之间的连线在抽象时被「降级」为障碍：布线器不知道它们连到哪，只知道「别从这儿过」。与之相对，引脚矩形是「欢迎连接」的区域——同一层 MET1 上，一块是通行许可，一块是禁行区。

#### 4.3.2 核心流程

布线器在每个布线步骤里都要做障碍查询：

```
候选走线段 s
  ├─ 与所有已布网络的金属求交 → 冲突则换道
  ├─ 与所有 MACRO 的 OBS 求交    → 冲突则换道
  ├─ 与 MACRO 外框 SIZE 求交（跨单元穿行受限）
  └─ 与目标 PIN 的 RECT 求交     → 交集即合法接入点
```

OBS 矩形越多、越大，布线资源越紧张。因此 LEF 生成工具通常会把内部金属**合并简化**（相邻矩形融合），而不是 1:1 照搬 GDS。

#### 4.3.3 源码精读

**ADDFX1H7H 的 OBS**：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:101-149](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L101-L149)
—— 结构与 PORT 相同（`LAYER MET1` + 一串 `RECT`），共 46 块矩形，覆盖单元内部除引脚外的 MET1 区域；首块 `RECT 3.795 1.01 4.415 1.1` 等大致占据中上部，与 4.2.4 示意图中 CO/S 之间的空白对应。

**没有 OBS 的单元**：全库 731 个 `OBS` 块对 785 个 MACRO，即 **54 个单元没有 OBS**。两个已核实的例子：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:3048-3082](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048-L3082)
—— ANT2H7H 整个 MACRO 到 `END ANT2H7H` 结束，PIN A 之后直接收尾，没有 OBS 段。

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:27970-27995](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L27970-L27995)
—— FILLER1H7H 更极端：连信号引脚都没有，只有一对电源轨道。

为什么它们可以没有 OBS？ANT 单元内部几乎没有 MET1 布线（天线二极管就是直接接地的保护结构）；FILLER 单元本身就是「占位 + 电源连通」的空壳。它们对布线器完全「透明」。

#### 4.3.4 代码实践

**实践目标**：统计每个单元的 OBS 矩形数，找出 OBS 最多的单元和没有 OBS 的单元清单。

**操作步骤**（示例代码）：

```python
# 示例代码：统计 OBS 矩形数
path = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef"
macro, in_obs, cnt, result = None, False, 0, {}
for line in open(path):
    s = line.strip()
    if s.startswith("MACRO "):
        macro, in_obs, cnt = s.split()[1], False, 0
    elif s == "OBS":
        in_obs = True
    elif s.startswith("END " + str(macro)) and macro:
        result[macro] = cnt; macro = None
    elif in_obs and s.startswith("RECT"):
        cnt += 1

no_obs = [k for k, v in result.items() if v == 0]
print("单元总数:", len(result), " 无 OBS 单元数:", len(no_obs))
print("无 OBS 示例:", no_obs[:10])
print("OBS 最多:", max(result.items(), key=lambda kv: kv[1]))
```

**需要观察的现象**：无 OBS 单元数应为 54（731/785 之差）；清单应以 ANT*、FILLER*、FILLTAP* 类名字为主。

**预期结果**：`无 OBS 单元数: 54`。OBS 最多的单元名与数值**待本地验证**（笔者仅核实了总数差值与 ANT2/FILLER1 两个样本）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 ADDFX1H7H 的 OBS 段再布线，会发生什么？

**答案**：布线器会以为单元内部 MET1 空闲，直接从单元框内穿过走线。这些走线与单元**真实版图**（GDS）中的内部金属短路——LEF 抽象没告诉它的事，流片后就变成短路缺陷。所以 OBS 是「抽象安全」的底线：宁可多挡，不可漏挡。

**练习 2**：OBS 与 PIN 都画在 MET1 上，布线器如何区分对待同一层的两类矩形？

**答案**：靠语法角色而非层：`PIN…PORT` 内的 RECT 是「可连接目标」，`OBS` 内的 RECT 是「禁止穿越目标」。同层并不冲突——接入引脚的走线允许落在引脚矩形上（那正是目的），但不允许压在 OBS 上。

**练习 3**：54 个无 OBS 单元为什么反而对流程重要？

**答案**：它们几乎全是 ANT（天线保护）与 FILLER/FILLTAP（填充与电源接续）单元：布局后用来填满行内空隙、修补电源轨与阱接触。没有 OBS 意味着它们不占任何布线资源，可以随意插入密集区域，这正是填充类单元的设计意图。

## 5. 综合实践

把三个模块串起来：写一个通用的 MACRO 解析器 `parse_macro.py`，输入单元名，输出「头部字段表 + 引脚明细 + 字符版图图 + 面积对比」。

```python
#!/usr/bin/env python3
# 示例代码：MACRO 抽象解剖器
# 用法: python3 parse_macro.py ADDFX1H7H
import re, sys

LEF = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef"
target = sys.argv[1] if len(sys.argv) > 1 else "ADDFX1H7H"

header, pins, cur_pin, in_target, in_obs = {}, {}, None, False, False
obs_rects, W, H = [], 0.14, 1.4

for line in open(LEF):
    s = line.strip()
    if s.startswith("MACRO "):
        in_target = (s.split()[1] == target); continue
    if not in_target:
        continue
    if s.startswith("END " + target):
        break
    key = s.split()[0] if s else ""
    if key in ("CLASS", "ORIGIN", "FOREIGN", "SYMMETRY", "SITE"):
        header[key] = s.rstrip(" ;")
    elif key == "SIZE":
        m = re.match(r"SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", s)
        W, H = float(m.group(1)), float(m.group(2))
    elif key == "PIN":
        cur_pin = {"name": s.split()[1], "dir": "", "use": "",
                   "shape": "", "layer": "", "rects": []}
        pins[cur_pin["name"]] = cur_pin
    elif key in ("DIRECTION", "USE", "SHAPE", "LAYER") and cur_pin:
        cur_pin[{"DIRECTION": "dir", "USE": "use",
                 "SHAPE": "shape", "LAYER": "layer"}[key]] = s.rstrip(" ;")
    elif key == "RECT" and cur_pin:
        cur_pin["rects"].append(tuple(float(v) for v in s.split()[1:5]))
    elif s == "OBS":
        cur_pin, in_obs = None, True
    elif key == "RECT" and in_obs:
        obs_rects.append(tuple(float(v) for v in s.split()[1:5]))

print(f"== {target} ==")
for k in ("CLASS", "ORIGIN", "FOREIGN", "SYMMETRY", "SITE"):
    print(f"{k:9s}: {header.get(k, '-')}")

# 引脚明细
print(f"SIZE     : {W} BY {H}  (面积 {W*H} um^2, {round(W/0.2)} 个 site)")
for p in pins.values():
    print(f"  PIN {p['name']:4s} {p['dir']:17s} {p['use']:7s} "
          f"{p['shape']:9s} {p['layer']:5s} {len(p['rects'])} 个 RECT")
print(f"OBS      : {len(obs_rects)} 个 RECT")

# 字符示意图: x 方向 1 字符 = W/64, y 方向 1 行 = H/14
sx, sy = W / 64, H / 14
canvas = [["."] * 64 for _ in range(14)]
def paint(rects, ch):
    for x1, y1, x2, y2 in rects:            # 裁剪到单元框内
        for r in range(13, -1, -1):
            for c in range(64):
                cx, cy = (c + .5) * sx, (r + .5) * sy
                if x1 - 1e-9 <= cx <= x2 + 1e-9 and y1 - 1e-9 <= cy <= y2 + 1e-9:
                    canvas[13 - r][c] = ch
for name, p in pins.items():
    paint(p["rects"], name[0] if p["use"] == "SIGNAL" else
          ("#" if p["use"] == "POWER" else "="))
paint(obs_rects, " ")
print("\n字符版图（#=VDD轨 ==VSS轨 字母=信号pin首字母 空格=OBS .=空闲）:")
for row in canvas:
    print("".join(row))

# 面积对比（最小功能单元 ANT2H7H 与最小格 FILLER1H7H）
a_ant, a_fill = 0.4 * 1.4, 0.2 * 1.4
print(f"\n面积相对 ANT2H7H(0.56)  = {W*H/a_ant:.1f} 倍")
print(f"面积相对 FILLER1H7H(0.28) = {W*H/a_fill:.1f} 倍")
```

**任务要求**：

1. 运行 `python3 parse_macro.py ADDFX1H7H`，核对输出：`SIZE 4.8 BY 1.4`、24 个 site、7 个引脚（A/B/CI/CO/S/VDD/VSS）、CI 有 8 个 RECT、OBS 46 个 RECT。
2. 读字符图：确认 VDD 在顶部、VSS 在底部各成一横带，信号引脚集中在中带，OBS（空格）填补引脚之间的区域。
3. 面积结论核对：ADDFX1H7H 面积 \(4.8 \times 1.4 = 6.72\ \mu m^2\)，相对 ANT2H7H（\(0.4 \times 1.4 = 0.56\ \mu m^2\)）为 **12.0 倍**；相对 FILLER1H7H（0.28 μm²）为 24 倍。
4. 换 `python3 parse_macro.py FILLER1H7H` 与 `ANT2H7H` 各跑一次，观察「只有电源轨」「单引脚无 OBS」两种极简形态。
5. 进阶（可选）：把 `paint` 的输出改成 matplotlib 的 `patches.Rectangle` 彩色图，信号/电源/OBS 三色图例，即完成规格里「pin 着色、标注层」的要求。

**预期结果**：第 1、3 项的所有数值可由本讲已核实的源码数据直接对账；字符图与 matplotlib 图的具体渲染**待本地验证**。

## 6. 本讲小结

- **MACRO 头部**：785 个单元全部 `CLASS CORE`、`SITE core7`、高恒 1.4、宽为 0.2 的整数倍（0.2~12.8）；`FOREIGN` 把抽象挂钩到 GDS 同名版图，`SYMMETRY X Y` 允许镜像复用。
- **引脚四层结构**：`PIN → PORT → LAYER → RECT`；`DIRECTION`（5069 个 = 2584 IN + 893 OUT + 22 三态 + 1570 INOUT）管电学方向，`USE`（SIGNAL/POWER/GROUND）管布线归属，一个 PORT 可含多块 RECT（CI 有 8 块）。
- **电源轨道**：每单元一对 VDD/VSS，`SHAPE ABUTMENT`，矩形以行边界为中心线**越出 SIZE 边界**（1.32~1.48 / −0.08~0.08），靠单元拼接天然成轨，无需布线。
- **OBS**：731/785 个单元带障碍区（ADDFX1H7H 有 46 块 MET1 矩形），54 个无 OBS 的多为 ANT/FILLER 类「透明」单元。
- **普通版的一切几何都在 MET1 上**——这是理解 `_ecos` 版补高层引脚、补 RC 的出发点。

## 7. 下一步学习建议

下一讲 **u3-l3「_ecos 单元 LEF：电源轨道与高层引脚」**将把本讲的解剖刀对准 `_ecos` 变体：逐 MACRO 对比 `ics55_LLSC_H7CH_ecos.lef`（VERSION 5.8，同样 785 个 MACRO），看它新增了哪些电源轨道矩形、把哪些信号引脚抬升到 MET2/VIA1，以及提交 e5c881b 修正引脚方向的来龙去脉。建议先用自己的 diff 命令扫一遍两文件中 ADDFX1H7H 段落的差异，带着「普通版缺什么」的清单去读下一讲。之后再进入 u3-l4 的天线属性与 ant LEF。
