# 工艺 LEF（三）：_ecos 版与 RC 寄生参数

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `_ecos` 版工艺 LEF（`N551P6M_ecos.lef`）相对原版（`N551P6M.lef`）的**三类差异**：新增 RC 寄生参数、OFFSET 轨道相位平移、删除 `NONDEFAULTRULE DefaultTaper`。
2. 理解 `CAPACITANCE CPERSQDIST` 与 `EDGECAPACITANCE` 的物理含义、单位，并能用它们算出一条金属线的电阻和电容。
3. 判断什么场景**必须**选 `_ecos` 版（做时序/寄生估计的开源流程），什么场景原版也能用（纯物理生成）。

## 2. 前置知识

本讲会用到的概念，用大白话先过一遍：

- **寄生参数（parasitics）**：芯片里的金属线不是理想的导线，它有电阻（R）和电容（C）。线越长、越细，R 越大；面积越大、边缘越长，C 越大。这些"附带"出来的电学效应叫寄生参数。数字后端工具在评估时序时，必须把互连的 R 和 C 也算进去。
- **方块电阻（sheet resistance，RPERSQ）**：一层金属薄膜有一个固有属性"每方块多少欧姆"。一段导线的电阻只取决于它横向占几个"方块"：
  \[ R = \text{RPERSQ} \times \frac{L}{W} \]
  一条 1μm 长、0.09μm 宽的 MET1 线占 \( 1/0.09 \approx 11.1 \) 个方块。
- **面积电容（CPERSQDIST）**：把金属线看成平板电容的一个极板，电容正比于极板面积：
  \[ C_{\text{area}} = \text{CPERSQDIST} \times W \times L \]
- **边缘电容（EDGECAPACITANCE）**：导线侧面与邻居/衬底之间的耦合，按边缘长度计费，一条线左右两条边都要算：
  \[ C_{\text{edge}} = \text{EDGECAPACITANCE} \times 2L \]
- **Elmore 延迟**：一条分布 RC 线末端的本征延迟约为 \( 0.5 \times R_{\text{total}} \times C_{\text{total}} \)。它让我们不用跑仿真就能估算"这根线有多慢"。
- **轨道（track）与 OFFSET**：布线器不可以在任意位置走线，只能走在一条条平行的"轨道"上，轨道间距就是 `PITCH`。`OFFSET` 决定第一条轨道中心离坐标原点有多远，也就是整个轨道网格的**相位**。
- **NONDEFAULTRULE**：LEF 里定义"非默认布线规则"的语法，允许某根网线用比默认更宽的线宽/专用过孔（常用于电源或时钟）。u2-l2 已经提过：原版里的 `DefaultTaper` 规则引用了一个**并不存在**的 `USEVIARULE MET1_POLY`，是个悬空引用。

如果 `PITCH`/`WIDTH`/`RESISTANCE RPERSQ` 这些层参数你还不熟，请先回顾 u2-l1《工艺 LEF（一）：金属栈与层规则》。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `prtech/techLEF/N551P6M.lef` | 672 | 原版工艺 LEF：20 个 LAYER、38 个固定 VIA、6 个 VIARULE、3 个 SITE，含 `DefaultTaper`，**无任何电容参数** |
| `prtech/techLEF/N551P6M_ecos.lef` | 676 | 开源工具适配版：在 7 个 ROUTING 层上补电容、平移 OFFSET、删除 `DefaultTaper`，其余与原版逐字节相同 |

两份文件都由 ICsprout 以 Apache-2.0 发布，文件头部 1–14 行是许可证声明，正文从 `VERSION 5.7` 开始。注意一个细节：**tech LEF 的 `_ecos` 版仍是 VERSION 5.7**（见 [prtech/techLEF/N551P6M_ecos.lef:L15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L15)），而标准单元 LEF 的 `_ecos` 版升级到了 5.8——tech 文件的适配是"最小改动"，不伴随版本号升级。

## 4. 核心概念与源码讲解

### 4.1 LEF 电阻电容参数

#### 4.1.1 概念说明

布线器把一根网线分配到轨道上之后，时序工具需要回答："这根线引入了多少延迟？"回答这个问题至少需要每层的：

1. 单位长度电阻（由 `RESISTANCE RPERSQ` + `WIDTH` 推出）；
2. 单位长度电容（由 `CAPACITANCE CPERSQDIST` + `EDGECAPACITANCE` + `WIDTH` 推出）；
3. 每个过孔的固定电阻（VIA 语句的 `RESISTANCE`）。

原版 `N551P6M.lef` 只提供了第 1 项和第 3 项——u2-l1 的结论是"本版 tech LEF 无任何 CAPACITANCE 参数，只能估电阻不能估电容"。`_ecos` 版补齐的正是缺失的第 2 项。

关于单位：两份文件的 `UNITS` 块都只声明了数据库精度（[prtech/techLEF/N551P6M_ecos.lef:L26-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L26-L28) 声明 `DATABASE MICRONS 1000`），没有显式声明电容/电阻单位，此时按 LEF 规范取默认值：**电容皮法（pF）、电阻欧姆（Ω）**。于是 `0.0007630` 读作"每平方微米 0.0007630 pF"，即 0.763 fF/μm²。

#### 4.1.2 核心流程

给定一段长 \( L \)、最小线宽 \( W \) 的第 \( k \) 层金属线，寄生估算流程：

```text
输入: L(μm), 层参数 RPERSQ, CPERSQDIST, EDGECAPACITANCE, WIDTH
  R  = RPERSQ × L / W
  C  = CPERSQDIST × W × L + EDGECAPACITANCE × 2L
  每个过孔再加 RESISTANCE = 2.5 Ω（两版文件中 38 个 VIA 均为该值）
输出: R(Ω), C(pF), Elmore 延迟 ≈ 0.5·R·C
```

以 MET1（W=0.09）为例代入 ecos 版参数：

\[ R = 0.1122 \times \frac{L}{0.09} \approx 1.247\,L \ \ \Omega, \qquad C = (0.0007630 \times 0.09 + 0.0000339 \times 2)\,L \approx 0.000136\,L \ \text{pF} \]

即每微米约 1.25 Ω、0.136 fF——这个量级决定了"为什么长线必须插中继 buffer"。

#### 4.1.3 源码精读

**ecos 版 MET1——七个 ROUTING 层中被补全 RC 的第一层。**
[prtech/techLEF/N551P6M_ecos.lef:L62-L76](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L62-L76)：这一段定义 MET1。第 72 行新增 `CAPACITANCE CPERSQDIST 0.0007630`（面积电容 0.763 fF/μm²），第 73 行新增 `EDGECAPACITANCE 0.0000339`（边缘电容 0.0339 fF/μm）；第 74 行的 `RESISTANCE RPERSQ 0.1122` 则是两版共有的。

```text
LAYER MET1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.2 0.2 ;
  WIDTH 0.09 ;
  OFFSET 0.1 0.1 ;          <-- 原版是 OFFSET 0 0
  AREA 0.042 ;
  SPACING 0.09 ;
  MAXWIDTH 10 ;
  MINENCLOSEDAREA 0.18 ;
  CAPACITANCE CPERSQDIST 0.0007630 ;   <-- ecos 新增
  EDGECAPACITANCE 0.0000339 ;          <-- ecos 新增
  RESISTANCE RPERSQ 0.1122 ;           <-- 两版共有
  DCCURRENTDENSITY AVERAGE 1.5 ;
END MET1
```

**原版 MET1 对照。**
[prtech/techLEF/N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74)：同样的 MET1，第 67 行是 `OFFSET 0 0`，块内没有任何 `CAPACITANCE`/`EDGECAPACITANCE` 语句——时序工具读到这里只能拿到电阻。

**ecos 版 MET2/MET5。**
[prtech/techLEF/N551P6M_ecos.lef:L85-L99](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L85-L99)：MET2 的 `CPERSQDIST 0.0011069`、`EDGECAPACITANCE 0.0000391`（第 95–96 行）。
[prtech/techLEF/N551P6M_ecos.lef:L155-L169](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L155-L169)：MET5 的电容明显回落到 `0.0006259`（第 165 行）。注意各层电容并不单调：MET2–MET4 最高，MET1、MET5 更低。

**ecos 版厚金属与封装层。**
[prtech/techLEF/N551P6M_ecos.lef:L180-L194](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L180-L194)：厚金属 T4M2 的 `CPERSQDIST 0.0001299`（第 190 行），比 MET2 低近一个数量级。
[prtech/techLEF/N551P6M_ecos.lef:L205-L215](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L205-L215)：封装再布线层 RDL 的 `CPERSQDIST 0.0000574`（第 212 行），是七层中最低的——层越高、越厚，离衬底越远，对地耦合越弱。

把七个 ROUTING 层的 ecos 版 RC 汇总（并按 4.1.2 的公式折算成"每毫米最小宽度的线"）：

| 层 | RPERSQ (Ω/□) | CPERSQDIST (pF/μm²) | EDGECAP (pF/μm) | WIDTH (μm) | R (Ω/mm) | C (fF/mm) |
| --- | --- | --- | --- | --- | --- | --- |
| MET1 | 0.1122 | 0.0007630 | 0.0000339 | 0.09 | 1246.7 | 136.5 |
| MET2 | 0.0914 | 0.0011069 | 0.0000391 | 0.1 | 914.0 | 188.9 |
| MET3 | 0.0914 | 0.0011069 | 0.0000409 | 0.1 | 914.0 | 192.5 |
| MET4 | 0.0914 | 0.0011069 | 0.0000409 | 0.1 | 914.0 | 192.5 |
| MET5 | 0.0914 | 0.0006259 | 0.0000344 | 0.1 | 914.0 | 131.4 |
| T4M2 | 0.0239 | 0.0001299 | 0.0000368 | 0.4 | 59.8 | 125.6 |
| RDL | 0.0151 | 0.0000574 | 0.0000281 | 3 | 5.0 | 228.4 |

两个直观结论：

- 1mm 最小宽度的 MET1 线：\( R \cdot C = 1246.7 \times 136.5\,\text{fF} = 170\,\text{ps} \)，分布 RC 末端延迟约 \( 0.5 \times 170 = 85 \) ps——相当于几十个门延迟，所以长线必须分段加 buffer。
- 同样 1mm，T4M2 只有 \( 59.8 \times 125.6\,\text{fF} \approx 7.5\,\text{ps} \) 的 RC 乘积，约为 MET1 的 1/23——这就是"关键长网走厚金属"的量化依据。

（表中数值由本讲实践脚本计算；电容数值随工艺提取条件不同而不同，PDK 未附提取设置，具体分解待确认。）

#### 4.1.4 代码实践

**实践目标**：用脚本从 `_ecos` 版 tech LEF 中提取七个 ROUTING 层的 RC，复算上面的"每毫米"表，验证你自己的手算。

**操作步骤**：

1. 在仓库根目录新建 `techlef_rc.py`（**示例代码**，不是仓库自带文件；请把脚本放在仓库外的练习目录，或用完即删，不要提交进仓库）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例代码：从 tech LEF 提取 ROUTING 层 RC 并折算每毫米参数"""
import re

def parse_layers(path):
    layers, cur = {}, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("LAYER "):          # 顶格的 LAYER 才是层定义
            cur = line.split()[1]
            layers[cur] = {}
        elif line.startswith("END "):          # VIA 内部的 END 带缩进，不会被误判
            cur = None
        elif cur:
            for key in ("TYPE", "DIRECTION", "PITCH", "WIDTH", "OFFSET",
                        "CAPACITANCE", "EDGECAPACITANCE", "RESISTANCE"):
                if line.strip().startswith(key):
                    layers[cur][key] = line.strip()[len(key):].split(";")[0].strip()
                    break
    return layers

L = parse_layers("prtech/techLEF/N551P6M_ecos.lef")
routing = [n for n, p in L.items() if p.get("TYPE") == "ROUTING"]
print(f"{'LAYER':6}{'R/Ω/mm':>10}{'C/fF/mm':>10}")
for n in routing:
    w = float(L[n]["WIDTH"].split()[0])
    r = float(L[n]["RESISTANCE"].split()[-1]) / w * 1000
    c = (float(L[n]["CAPACITANCE"].split()[-1]) * w
         + 2 * float(L[n]["EDGECAPACITANCE"].split()[-1])) * 1000  # pF→fF
    print(f"{n:6}{r:10.1f}{c:10.1f}")
```

2. 运行 `python3 techlef_rc.py`。

**需要观察的现象**：解析器必须只认"顶格的 `LAYER`"。如果你把 `line.startswith("LAYER ")` 改成对 `line.strip()` 判断，38 个 VIA 语句块内部的 `\tLAYER MET1 ;` 会被误当成层定义，MET1 的参数会被 VIA 块里的同名引用覆盖出错。

**预期结果**：输出与 4.1.3 表格的 R/C 两列一致（MET1 约 1246.7 / 136.5，RDL 约 5.0 / 228.4）。把脚本的输入换成 `N551P6M.lef` 会因为 `L[n]["CAPACITANCE"]` 缺键而 KeyError——这本身就是"原版没有电容参数"的程序化证据。

#### 4.1.5 小练习与答案

**练习 1**：一条 500μm 长、最小宽度的 MET2 线，电阻和电容各是多少（用 ecos 版参数）？
**答**：\( R = 0.0914 \times 500 / 0.1 = 457\,\Omega \)；\( C = 0.0011069 \times 0.1 \times 500 + 0.0000391 \times 2 \times 500 = 0.0553 + 0.0391 = 0.0945\,\text{pF} \)（94.5 fF）。

**练习 2**：为什么 T4M2 的 CPERSQDIST 比 MET2 小一个数量级，RDL 更小？
**答**：T4M2 是厚金属、RDL 是封装再布线层，二者在金属栈中位置更高、离衬底更远，单位面积的对地耦合更弱；同时更厚更宽的导体也改变了边缘场分布。具体数值来自工艺方提取，PDK 未附带提取脚本，其分解待确认。

**练习 3**：`EDGECAPACITANCE` 为什么按"两条边"计？
**答**：一条导线有左右两条侧边，每条侧边都对相邻导体/衬底产生耦合电容，公式为 \( C_{\text{edge}} = \text{EDGECAPACITANCE} \times 2L \)；若只算一条边，会系统性低估约一半的耦合。

### 4.2 两版 tech LEF diff 分析

#### 4.2.1 概念说明

仓库同时维护两份工艺 LEF，是"一份交付多类工具"的典型做法：原版面向传统商业流程，`_ecos` 版面向开源 EDA。这个变体的来历可以直接从 git 历史读出来（以下均为只读命令，可自行复现）：

```console
$ git log --oneline --follow -- prtech/techLEF/N551P6M_ecos.lef
4f5b659 style: rename from *_ieda.lef to *_ecos.lef
993caaf tech: Remove Virtuoso poly rule
0ed49f0 feat: add some properties
327eb8a feat: add tech lef for ieda
3338e16 feat: add open source pdk
```

时间线很清晰：

1. **327eb8a**（2025-10-21，myyerrol，"add tech lef for ieda"）：以原版为底新建 `N551P6M_ieda.lef`，一次性把 7 个 ROUTING 层的 `OFFSET 0 0` 改为 `OFFSET 0.1 0.1`——最初是为**iEDA**（华大九天开源 EDA 工具）准备的。
2. **0ed49f0**（2025-10-22，myyerrol，"add some properties"）：插入 14 行，即 7 层 × (`CAPACITANCE CPERSQDIST` + `EDGECAPACITANCE`)。
3. **993caaf**（2025-11-11，Philippe Sauter，"Remove Virtuoso poly rule"）：删除 10 行的 `NONDEFAULTRULE virtuosoDefaultTaper` 块——提交标题直接点明这是 Cadence Virtuoso 专用遗留。
4. **4f5b659**（2025-12-24，myyerrol）：`_ieda` 改名 `_ecos`（同批改名的还有三个标准单元 LEF），把"为某一工具定制"泛化为"为开源生态定制"。

原版那边的对称操作也值得注意：`5dbfd0e`（2025-11-12）从原版删掉了同名 `virtuosoDefaultTaper`，但 `6e902bf`（2026-08-01，"update io cell and std cell"）又把这段规则以 `DefaultTaper` 之名**加了回去**。所以今天的状态是：原版有这条规则、ecos 版没有——两个版本是**各自独立演进**的分叉，而不是一方每次都同步另一方。

#### 4.2.2 核心流程

量化两份文件差异的标准流程：

```text
1. 整体统计:  git diff --no-index --stat  A.lef B.lef
   -> 21 insertions(+), 17 deletions(-)   (672 行 -> 676 行)
2. 定位 hunk:  git diff --no-index A.lef B.lef
   -> 8 个改动块: 7 个层定义块 + 1 个 DefaultTaper 删除块
3. 对账:
   +21 = 14 行电容 + 7 行 OFFSET(改写)
   -17 = 7 行 OFFSET(旧值) + 10 行 DefaultTaper(9 行语句 + 1 行空行)
   672 + 21 - 17 = 676 ✓
4. 其余部分（38 个 VIA、6 个 VIARULE、3 个 SITE、UNITS、MANUFACTURINGGRID）
   逐字节相同——diff 输出中不出现任何相关 hunk 即为证明
```

"对账"这一步很重要：它能证明除了三类差异**再无其他改动**，避免讲义或文档里凭印象多写一条不存在的差异。

#### 4.2.3 源码精读

**差异一：OFFSET 0 0 → 0.1 0.1（7 个层）。**
[prtech/techLEF/N551P6M.lef:L67](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L67)：原版 MET1 的 `OFFSET 0 0`，轨道中心从原点 0 开始。
[prtech/techLEF/N551P6M_ecos.lef:L67](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L67)：ecos 版同位置改为 `OFFSET 0.1 0.1`。MET2 同理（原版 [L88](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L88) → ecos [L90](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L90)），七个层全部统一平移 0.1μm。

这条改动的意义：对 MET1–MET5（PITCH 0.2）而言，0.1 恰是**半节距**，也正是 LEF 规范允许的 OFFSET 上限（不得超过 pitch 的一半）。结合 u2-l2 的结论——site 宽 0.2、全部标准单元引用 core7——原版 OFFSET 0 会让轨道中心恰好落在 0、0.2、0.4…这些 **site 列边界**上；平移半节距后轨道穿过每个 site 的**中心**，引脚接入点不再压在相邻单元的拼接缝上。注意对 T4M2（pitch 0.8）和 RDL（pitch 5），0.1 并不是半节距，只是同幅度平移，说明这是一次整体搬移网格相位，而非逐层定制。（这是基于 LEF 语义的机理解释；维护者的完整动机未在提交说明中展开，待确认。）

**差异二：新增 14 行电容参数（7 个层）。**
即 4.1.3 已精读的各层 `CAPACITANCE CPERSQDIST` / `EDGECAPACITANCE` 行（如 [prtech/techLEF/N551P6M_ecos.lef:L95-L96](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L95-L96) 是 MET2 的两行）。原版对应位置（[prtech/techLEF/N551P6M.lef:L83-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L83-L95)）从 `MINENCLOSEDAREA` 直接跳到 `RESISTANCE`，中间没有任何电容语句。

**差异三：删除 NONDEFAULTRULE DefaultTaper。**
[prtech/techLEF/N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652)：原版在 VIARULE 之后定义了名为 `DefaultTaper` 的非默认规则——把 POLY 线宽设为 0.06、MET1 线宽设为 0.09，并 `USEVIARULE MET1_POLY`。

```text
NONDEFAULTRULE DefaultTaper
  LAYER POLY
    WIDTH 0.06 ;
  END POLY
  LAYER MET1
    WIDTH 0.09 ;
  END MET1
  USEVIARULE MET1_POLY ;      <-- 全文件找不到 MET1_POLY 的 VIARULE 定义
END DefaultTaper
```

这段有两个问题：其一，`MET1_POLY` 在整份文件里没有对应的 VIARULE 定义，是悬空引用；其二，POLY 在本工艺 LEF 里是 MASTERSLICE 层（[prtech/techLEF/N551P6M.lef:L50-L52](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L50-L52)），并非布线层，这条"poly 布线规则"是版图编辑器（Virtuoso）层面的概念，对布线工具没有意义。ecos 版把它整段拿掉：
[prtech/techLEF/N551P6M_ecos.lef:L656-L658](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L656-L658)：这里 `END RDL_T4M2` 之后空一行就直接进入 `SITE CoreSite`，中间不再有 NONDEFAULTRULE。

#### 4.2.4 代码实践

**实践目标**：用脚本把 4.2.2 的三类差异一次性量化，输出"层 × 差异"报告。

**操作步骤**：

1. 保存以下**示例代码**为 `techlef_diff.py`，在仓库根目录运行 `python3 techlef_diff.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例代码：量化 N551P6M.lef 与 N551P6M_ecos.lef 的三类差异"""
import re

def parse_layers(path):
    layers, cur = {}, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("LAYER "):
            cur = line.split()[1]; layers[cur] = {}
        elif line.startswith("END "):
            cur = None
        elif cur:
            for key in ("TYPE", "OFFSET", "CAPACITANCE", "EDGECAPACITANCE"):
                if line.strip().startswith(key):
                    layers[cur][key] = line.strip()[len(key):].split(";")[0].strip()
                    break
    return layers

orig = parse_layers("prtech/techLEF/N551P6M.lef")
ecos = parse_layers("prtech/techLEF/N551P6M_ecos.lef")

n_offset = n_cap = 0
print(f"{'LAYER':6}{'OFFSET 原版':>12}{'OFFSET ecos':>12}  新增电容")
for name, p in orig.items():
    if p.get("TYPE") != "ROUTING":
        continue
    q = ecos[name]
    cap = "" if "CAPACITANCE" in q else "  (无)"
    if p["OFFSET"] != q["OFFSET"]:
        n_offset += 1
    if "CAPACITANCE" in q and "CAPACITANCE" not in p:
        n_cap += 1
        cap = q["CAPACITANCE"] + " / " + q["EDGECAPACITANCE"]
    print(f"{name:6}{p['OFFSET']:>12}{q['OFFSET']:>12}  {cap}")

for tag, path in (("原版", "prtech/techLEF/N551P6M.lef"),
                  ("ecos 版", "prtech/techLEF/N551P6M_ecos.lef")):
    hits = [i + 1 for i, l in enumerate(open(path, encoding="utf-8"))
            if "NONDEFAULTRULE" in l]
    print(f"{tag} NONDEFAULTRULE 所在行: {hits}")
print(f"\nOFFSET 改变的层: {n_offset} 个; 新增电容的层: {n_cap} 个")
```

2. 再跑一次人工对账：`git diff --no-index --stat prtech/techLEF/N551P6M.lef prtech/techLEF/N551P6M_ecos.lef`。

**需要观察的现象**：脚本输出 7 个层的 OFFSET 全部由 `0 0` 变为 `0.1 0.1`；`原版 NONDEFAULTRULE 所在行: [644]`、`ecos 版 NONDEFAULTRULE 所在行: []`；git 统计为 21 增 17 删。

**预期结果**：`OFFSET 改变的层: 7 个; 新增电容的层: 7 个`，与 git diff 的 8 个 hunk、21/17 行对账一致。待本地验证（脚本在讲义编写时未经实际执行，若你的 Python 版本低于 3.7，dict 不保序，层顺序会不同但不影响结论）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `672 + 21 - 17 = 676` 恰好等于 ecos 版行数？
**答**：21 行新增 = 7 层 × 2 行电容 + 7 行 OFFSET 新值；17 行删除 = 7 行 OFFSET 旧值 + DefaultTaper 块（9 行语句 + 1 行随块删除的空行）。OFFSET 是"改写"（一删一增），电容是"纯新增"，DefaultTaper 是"纯删除"。

**练习 2**：如果只看行数差（676 - 672 = 4），会得出什么错误结论？
**答**：会以为只多了 4 行，从而漏掉"7 行 OFFSET 改写 + 10 行规则删除"这些行数相消的改动。行数差只反映净变化，量化 diff 必须分别统计插入与删除。

**练习 3**：`_ecos` 版为什么同时改 OFFSET 又删 DefaultTaper，而不是只补电容？
**答**：三类改动服务同一目标——让开源工具能正确使用这份文件：补电容让时序估计可用，平移轨道相位让布线网格与单元引脚对齐，删除带悬空引用的 Virtuoso 遗留规则避免解析问题。变体名从 `_ieda` 改为 `_ecos` 也说明它被定位为面向整个开源生态的通用适配层。

### 4.3 开源工具寄生估计依赖

#### 4.3.1 概念说明

开源数字后端流程（以 OpenROAD 为代表，iEDA 同理）的时序闭环依赖互连寄生：

```text
布线(或全局布线) → 估计寄生 R/C → 延迟计算 → 时序分析 → 优化/再布线
                        ↑
             这一步的输入只有两个来源:
             a) 专门的 RC 提取规则文件 (需工艺方提供, ICS55 preview 未提供)
             b) tech LEF 层表里的 RESISTANCE / CAPACITANCE 一阶参数
```

ICS55 目前**没有**随包发布 RC 提取规则文件（u1-l1 的 Todo 清单里 RC 也在待补之列），因此开源流程里的寄生估计只能走路线 (b)——这正是 `_ecos` 版补那 14 行电容的动机：没有它们，路线 (b) 也只剩一半（只有电阻、没有电容）。

需要说明的是：不同工具/版本在没有规则文件时的回退行为不同，有的用 LEF 层参数做一阶估计，有的直接给零并告警。本讲只确立"PDK 侧的数据前提"，具体工具行为请在 4.3.4 的实践中验证，待本地验证。

#### 4.3.2 核心流程

以一根从单元 A 输出到单元 B 输入的网线为例，一阶估计链路：

```text
1. 读 tech LEF 层表: 每层 RPERSQ / CPERSQDIST / EDGECAPACITANCE / WIDTH
2. 读布线结果: 网线在各层上的分段长度 ℓ_k 与过孔数 v
3. R_net = Σ_k (RPERSQ_k × ℓ_k / W_k) + 2.5Ω × v      (过孔值来自 VIA 语句)
   C_net = Σ_k (CPERSQDIST_k × W_k + 2×EDGECAPACITANCE_k) × ℓ_k
         + Σ 引脚电容 (来自 liberty)
4. 延迟 ≈ 驱动电阻 × C_net (负载项) + 0.5 × R_net × C_net (线项)
```

第 1 步若缺失电容参数，第 3 步的 C_net 只剩 liberty 引脚电容，**线电容为零**——这就是误用原版的直接后果。

#### 4.3.3 源码精读

**原版没有任何电容语句的证据。**
[prtech/techLEF/N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74)：MET1 从几何规则直接跳到 `RESISTANCE RPERSQ 0.1122`，全文件 7 个 ROUTING 层皆如此。可以自己验证：`grep -c CAPACITANCE prtech/techLEF/N551P6M.lef` 返回 0，而对 `_ecos` 版返回 14。

**过孔电阻两版一致，且相当可观。**
[prtech/techLEF/N551P6M_ecos.lef:L216-L224](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L216-L224)：`VIA MET2_MET1_VIA1_0` 的 `RESISTANCE 2.5`——一个过孔的电阻约等于 22μm 最小宽度的 MET1 走线（2.5 / 1.247 ≈ 22μm），这是"能少打过孔就少打"的量化依据。这段在原版 [L202-L210](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L202-L210) 逐字节相同。

**UNITS 未声明电容单位。**
[prtech/techLEF/N551P6M_ecos.lef:L26-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L26-L28)：`UNITS` 块只有 `DATABASE MICRONS 1000`。LEF 允许在这里显式写 `CAPACITANCE PICOFARADS`、`RESISTANCE OHMS`，本文件未写，按规范默认 pF/Ω——4.1.3 的单位换算即以此为据。若工具以其他单位解读，数值会差若干数量级，这也是排查"寄生值离谱"时的第一个检查点。

#### 4.3.4 代码实践

**实践目标**：验证"原版缺电容、ecos 版补齐"，并写一段误用后果分析；有 OpenROAD 环境者做加载对比。

**操作步骤**：

1. 在仓库根目录运行两条统计命令并记录输出：

```console
$ grep -c 'CAPACITANCE' prtech/techLEF/N551P6M.lef
$ grep -c 'CAPACITANCE' prtech/techLEF/N551P6M_ecos.lef
```

2. 用 4.1.4 的脚本分别对两份文件计算 MET1 每毫米电容，记录原版的行为（缺键报错或为空）。
3. （可选，需自备环境）用 OpenROAD 分别只读入两版 tech LEF，观察告警差异：

```tcl
# 示例脚本
read_lef prtech/techLEF/N551P6M.lef        ; # 换成 _ecos 版再跑一次对比
puts [[[ord::get_db_tech] getLayers] size]
```

4. 写一段 150–250 字的说明：在开源布线/时序工具中误用无 RC 版本会导致什么后果。

**需要观察的现象**：第 1 步原版返回 0、ecos 版返回 14（7 个 `CAPACITANCE` + 7 个 `EDGECAPACITANCE`）；第 2 步原版无法产出电容值；第 3 步关注工具是否打印缺少 RC 的告警（具体行为随版本而异，待本地验证）。

**预期结果**：确认"原版只够算 R，ecos 版才能算 R 和 C"。误用后果的参考要点（写进你的说明里）：线电容被记为零 → 互连延迟只剩驱动电阻 × 引脚电容，长网延迟被严重低估 → 时序"纸面通过、实片失败"；优化器看不到长线的真实代价，不会插中继 buffer、也不会把关键网换到高层金属；CTS 的时钟偏斜估计失真；后续若用真实提取规则复核，会发现大量违例集中在长网。另外，若流程里有 DEF 引用了 `NONDEFAULTRULE DefaultTaper`，读入不含该规则的 `_ecos` 版可能报"未定义规则"——选版本时这条也要一并考虑。

#### 4.3.5 小练习与答案

**练习 1**：一个设计只用原版 tech LEF 做了布局布线并导出 DEF，最后用商业工具的完整 RC 做签核，可能看到什么现象？
**答**：布线本身可能合法（几何规则两版一致），但基于它的时序估计不可信；签核时长网的延迟明显大于流程内估计，出现成片的负时序裕量，集中在扇出大、线长的网。

**练习 2**：既然 `_ecos` 版"更全"，为什么仓库不直接替换原版？
**答**：原版是工艺方交付的基线，服务于以商业工具为主的传统流程（`DefaultTaper` 就是 Virtuoso 语境的产物）；`_ecos` 是叠加在其上的适配层。平行维护让两类用户各取所需，代价是两份文件需要分别演进（见 4.2.1 中 DefaultTaper 的分叉史）。这是开源 PDK 常见的工程折中。

**练习 3**：`EDGECAPACITANCE` 和 `CPERSQDIST` 哪个对"细长线"更敏感？
**答**：细长线面积小、边缘长，边缘电容占比升高。以 MET1 为例：每微米面积电容 0.0007630×0.09 ≈ 0.0687 fF，边缘电容 2×0.0339 ≈ 0.0678 fF——几乎各占一半，忽略任何一项都会错约 50%。

## 5. 综合实践

**任务：产出一份《ICS55 tech LEF 选型报告》。**

假设你要为一个基于 OpenROAD 的课程实验项目选择工艺文件，请综合本讲三个模块完成：

1. **数据核对**（用 4.2.4 的脚本）：列出两版文件在 7 个 ROUTING 层上的 OFFSET、RC 差异表，附 git diff 的 21/17 行对账。
2. **量化分析**（用 4.1.4 的脚本）：取 MET1 与 T4M2 各 1mm 最小宽度线，计算 R、C 与 Elmore 延迟，说明厚金属在长网上的收益倍数。
3. **决策**：分别给出"只做综合后门级仿真""做布局布线 + 时序评估""需要 DefaultTaper 非默认规则"三种场景下的选型建议，并指出每一种的选择依据来自本讲哪个证据（提交号或行号）。
4. **风险清单**：列出选 `_ecos` 版需要注意的两件事（如：电容单位依赖 LEF 默认 pF 约定、NONDEFAULTRULE 缺失对已有 DEF 的影响）。

参考结论骨架：场景一与 tech LEF 基本无关（verilog/liberty 即可，见 u3-l5、u3-l6）；场景二必须 `_ecos`（证据：0ed49f0、[N551P6M_ecos.lef:L72-L73](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L72-L73)）；场景三只能原版或去掉规则引用（证据：[N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652)）。

## 6. 本讲小结

- `_ecos` 版工艺 LEF 与原版只有三类差异：7 个 ROUTING 层新增 `CAPACITANCE CPERSQDIST` + `EDGECAPACITANCE`（14 行）、7 个层 `OFFSET 0 0 → 0.1 0.1`、删除 10 行的 `NONDEFAULTRULE DefaultTaper`；git 对账为 21 增 17 删，其余（38 个 VIA、6 个 VIARULE、3 个 SITE）逐字节相同。
- 电容按 pF 计：面积电容 \( C = \text{CPERSQDIST} \times W L \)，边缘电容 \( C = \text{EDGECAPACITANCE} \times 2L \)；电阻 \( R = \text{RPERSQ} \times L/W \)；一个过孔固定 2.5Ω。
- 量化后果：1mm 最小宽度 MET1 线约 1.25kΩ / 137fF、Elmore 延迟约 85ps；同长度 T4M2 的 RC 乘积只有约 1/23——厚金属是长网的正确选择。
- 变体史：`_ieda`（327eb8a，OFFSET）→ 补 RC（0ed49f0）→ 删 Virtuoso 规则（993caaf）→ 改名 `_ecos`（4f5b659）；原版同期删了又加回 DefaultTaper（5dbfd0e → 6e902bf），两版各自独立演进。
- 选型规则：凡是要做寄生/时序估计的开源流程必须 `_ecos`；纯物理生成可用原版；引用了 `DefaultTaper` 的流程不能直接换 `_ecos`。
- ICS55 未提供 RC 提取规则文件，开源工具只能依赖 LEF 层参数做一阶估计——这 14 行电容是当前唯一的电容数据来源。

## 7. 下一步学习建议

tech LEF 的三讲到此完整：u2-l1 层规则、u2-l2 VIA/VIARULE/SITE、本讲 `_ecos` 变体与 RC。接下来两条路：

1. **进入单元库**：u3-l2《单元 LEF 抽象视图解剖》——看 MACRO 如何引用本讲的 core7 site 与 MET1/MET2 层；随后 u3-l3 讲单元级 `_ecos` 变体（电源轨道 pin 与 MET2 引脚），与本讲的 OFFSET 半节距平移正好衔接：轨道相位变了，引脚形状也要跟着补。
2. **动手验证**：u6-l1《把 PDK 装进开源 EDA 工具》会用 `read_lef` 实际读入 `N551P6M_ecos.lef`，把本讲 4.3.4 的"待本地验证"项落到实处。

继续阅读建议：先自己跑一遍 4.2.4 的脚本再看 u3-l2；有余力可读 LEF/DEF 规范中 LAYER(ROUTING) 的 OFFSET 约束一节，验证"OFFSET ≤ pitch/2"的约定在本仓库数据（0.1 vs 0.2）上的体现。
