# 工艺 LEF（三）：_ecos 版与 RC 寄生参数

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `_ecos` 版工艺 LEF（`N551P6M_ecos.lef`）相对原版（`N551P6M.lef`）的**三类差异**：7 个 ROUTING 层新增电容参数、7 个层 `OFFSET` 由 `0 0` 改为 `0.1 0.1`、删除 `NONDEFAULTRULE DefaultTaper`。
2. 理解 `CAPACITANCE CPERSQDIST` 与 `EDGECAPACITANCE` 的物理含义与单位（默认 pF），并能算出一条金属线的电阻、电容和一阶 RC 延迟。
3. 判断什么场景**必须**选 `_ecos` 版（需要做寄生/时序估计的开源流程），什么场景原版也够用（纯物理规则查询）。

## 2. 前置知识

本讲会用到的概念，先用大白话过一遍：

- **寄生参数（parasitics）**：芯片里的金属线不是理想导线。它有电阻（R），线越长越细 R 越大；它还有电容（C），对面（衬底、邻居线）越近、面积和边缘越长 C 越大。这些"附带"出来的电学效应叫寄生参数。后端工具评估时序时必须把互连的 R、C 算进去。
- **方块电阻（`RESISTANCE RPERSQ`）**：一层金属薄膜"每个方块多少欧姆"。一段导线的电阻只看它横向占几个方块：

  \[ R = \text{RPERSQ} \times \frac{L}{W} \]

  例如 1μm 长、0.09μm 宽的 MET1 线占 \( 1/0.09 \approx 11.1 \) 个方块。

- **面积电容（`CAPACITANCE CPERSQDIST`）**：把导线看成平板电容的一个极板，电容正比于极板面积：

  \[ C_{\text{area}} = \text{CPERSQDIST} \times W \times L \]

- **边缘电容（`EDGECAPACITANCE`）**：导线两个侧面与周围导体的耦合，按边缘长度计，一条线有左右两条边：

  \[ C_{\text{edge}} = \text{EDGECAPACITANCE} \times 2L \]

- **一阶 RC 延迟**：分布 RC 线的末端本征延迟约为 \( 0.5 \times R_{\text{total}} \times C_{\text{total}} \)（电阻乘电容，Ω×pF = ps）。不用跑仿真就能估"这根线有多慢"。
- **轨道（track）与 OFFSET**：布线器只能在一条条平行"轨道"上走线，轨道间距即 `PITCH`。`OFFSET` 决定第一条轨道中心离原点多远，也就是整个轨道网格的**相位**。
- **NONDEFAULTRULE**：LEF 中"非默认布线规则"的语法，允许某根网线用专属线宽/过孔（常见于电源、时钟）。u2-l2 已指出：原版 `DefaultTaper` 里的 `USEVIARULE MET1_POLY` 引用了一个**并不存在**的规则。

如果 `PITCH`/`WIDTH`/`RESISTANCE RPERSQ` 这些层参数你还生疏，请先回看 u2-l1《工艺 LEF（一）：金属栈与层规则》。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) | 672 | 原版工艺 LEF：20 个 LAYER、38 个固定 VIA、6 个 VIARULE、3 个 SITE；含 `DefaultTaper`，**无任何电容参数** |
| [prtech/techLEF/N551P6M_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef) | 676 | 开源工具适配版：7 个 ROUTING 层补电容、OFFSET 平移 0.1、删除 `DefaultTaper`，其余与原版逐字节相同 |

两份文件头部 1–13 行都是 Apache-2.0 许可声明，正文从 `VERSION 5.7` 开始（原版 [L15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L15)、ecos 版 [L15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L15)）。注意一个容易想当然的细节：**tech LEF 的 `_ecos` 版仍是 VERSION 5.7**，而标准单元 LEF 的 `_ecos` 版升级到了 5.8（[ics55_LLSC_H7CH_ecos.lef:L1](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L1)）——tech 文件的适配走的是"最小改动"，不伴随版本号变化。

本讲还会顺带引用一个佐证文件：标准单元 `_ecos` 版 LEF（用于验证 OFFSET 与电源轨道的几何关系）。

## 4. 核心概念与源码讲解

### 4.1 LEF 电阻电容参数

#### 4.1.1 概念说明

布线器把一根网线分配到轨道之后，时序引擎要回答"这根线引入多少延迟"。回答它至少需要每层的：

1. 单位长度电阻——由 `RESISTANCE RPERSQ` 与 `WIDTH` 推出；
2. 单位长度电容——由 `CAPACITANCE CPERSQDIST`、`EDGECAPACITANCE` 与 `WIDTH` 推出；
3. 每个过孔的固定电阻——固定 VIA 语句里的 `RESISTANCE`。

原版 `N551P6M.lef` 只提供第 1、3 项。u2-l1 的结论是"本版 tech LEF 无任何 CAPACITANCE 参数，只能估电阻不能估电容"。`_ecos` 版补齐的正是缺失的第 2 项。

**单位从哪来？** 两份文件的 `UNITS` 块都只声明了数据库精度（[prtech/techLEF/N551P6M_ecos.lef:L26-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L26-L28) 中仅有 `DATABASE MICRONS 1000`），没有显式写电容/电阻单位。按 LEF 规范，此时取默认值：**电容皮法（pF）、电阻欧姆（Ω）**。于是 `0.0007630` 读作"每平方微米 0.0007630 pF"，即 0.763 fF/μm²；`EDGECAPACITANCE 0.0000339` 即每微米边缘 0.0339 fF。

#### 4.1.2 核心流程

给定一段长 \( L \)、最小线宽 \( W \) 的某层金属线，寄生估算流程：

```text
输入: L(μm)，该层的 RPERSQ / CPERSQDIST / EDGECAPACITANCE / WIDTH
  R = RPERSQ × L / W
  C = CPERSQDIST × W × L + EDGECAPACITANCE × 2L
  若干过孔：每个再 +2.5 Ω（两版文件 38 个固定 VIA 均为此值）
输出: R(Ω)、C(pF)、一阶延迟 ≈ 0.5·R·C
```

代入 ecos 版 MET1 参数（W=0.09），得每微米常数：

\[ R/L = \frac{0.1122}{0.09} \approx 1.247\ \Omega/\mu m, \qquad C/L = 0.0007630 \times 0.09 + 2 \times 0.0000339 \approx 1.365 \times 10^{-4}\ \text{pF}/\mu m \]

即每微米约 1.25 Ω、0.136 fF。注意 R 和 C 都正比于 L，所以 RC 乘积按 \( L^2 \) 增长：100μm 的 MET1 线 RC≈1.7ps，1mm 就是 ≈170ps——涨了 100 倍而不是 10 倍，这就是"长线必须分段插 buffer"的数学根源。

#### 4.1.3 源码精读

**ecos 版 MET1——第一个被补全 RC 的层。**
[prtech/techLEF/N551P6M_ecos.lef:L62-L76](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L62-L76) 定义 MET1：第 72 行新增 `CAPACITANCE CPERSQDIST 0.0007630`，第 73 行新增 `EDGECAPACITANCE 0.0000339`，第 74 行 `RESISTANCE RPERSQ 0.1122` 则两版共有：

```text
LAYER MET1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.2 0.2 ;
  WIDTH 0.09 ;
  OFFSET 0.1 0.1 ;                      <-- 原版此处是 OFFSET 0 0
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
[prtech/techLEF/N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74)：同一层，第 67 行是 `OFFSET 0 0`，块内从 `MINENCLOSEDAREA` 直接跳到 `RESISTANCE`，没有任何电容语句——时序工具读到这里只能拿到电阻。

**其余六层的新电容。**
MET2：[L95-L96](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L95-L96)（`0.0011069` / `0.0000391`）；MET3：[L118-L119](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L118-L119)；MET4：[L141-L142](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L141-L142)；MET5：[L165-L166](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L165-L166)；厚金属 T4M2：[L190-L191](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L190-L191)；封装再布线层 RDL：[L212-L213](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L212-L213)。

把 7 个 ROUTING 层的 ecos 版 RC 折算成"每毫米最小宽度线"：

| 层 | RPERSQ (Ω/□) | CPERSQDIST (pF/μm²) | EDGECAP (pF/μm) | WIDTH (μm) | R (Ω/mm) | C (fF/mm) |
| --- | --- | --- | --- | --- | --- | --- |
| MET1 | 0.1122 | 0.0007630 | 0.0000339 | 0.09 | 1246.7 | 136.5 |
| MET2 | 0.0914 | 0.0011069 | 0.0000391 | 0.10 | 914.0 | 188.9 |
| MET3 | 0.0914 | 0.0011069 | 0.0000409 | 0.10 | 914.0 | 192.5 |
| MET4 | 0.0914 | 0.0011069 | 0.0000409 | 0.10 | 914.0 | 192.5 |
| MET5 | 0.0914 | 0.0006259 | 0.0000344 | 0.10 | 914.0 | 131.4 |
| T4M2 | 0.0239 | 0.0001299 | 0.0000368 | 0.40 | 59.8 | 125.6 |
| RDL  | 0.0151 | 0.0000574 | 0.0000281 | 3.00 | 5.0 | 228.4 |

两个直观读数：

- 1mm 最小宽度 MET1 线：\( RC = 1246.7 \times 0.1365\,\text{pF} \approx 170\,\text{ps} \)，分布 RC 末端延迟约 85ps——相当于几十级门延迟。
- 同样 1mm 的 T4M2：\( 59.8 \times 0.1256\,\text{pF} \approx 7.5\,\text{ps} \)，RC 乘积只有 MET1 的约 1/23——"关键长网上厚金属"的量化依据。

（表中电阻列对 MET2–MET5 相同是数据使然：四层 `RPERSQ` 与 `WIDTH` 完全一致，见 u2-l1 的"均质中层"分档。）

#### 4.1.4 代码实践

**实践目标**：用脚本从 `_ecos` 版提取 7 个 ROUTING 层的 RC，复算上表，并顺带验证"原版没有电容"。

**操作步骤**：

1. 在仓库外的练习目录新建 `techlef_rc.py`（**示例代码**，不是仓库自带文件；不要提交进仓库）：

```python
#!/usr/bin/env python3
"""示例代码：从 tech LEF 提取 ROUTING 层 RC 并折算每毫米参数"""
import sys

def parse_layers(path):
    layers, cur = {}, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("LAYER "):      # 只认顶格 LAYER，避开 VIA 块内的缩进引用
            cur = line.split()[1]
            layers[cur] = {}
        elif line.startswith("END "):      # 同理只认顶格 END
            cur = None
        elif cur:
            for key in ("TYPE", "WIDTH", "CAPACITANCE",
                        "EDGECAPACITANCE", "RESISTANCE"):
                if line.strip().startswith(key):
                    layers[cur][key] = line.strip()[len(key):].split(";")[0].strip()
                    break
    return layers

L = parse_layers(sys.argv[1])
print(f"{'LAYER':6}{'R/(Ohm/mm)':>12}{'C/(fF/mm)':>11}")
for name, p in L.items():
    if p.get("TYPE") != "ROUTING":
        continue
    w = float(p["WIDTH"].split()[0])
    r = float(p["RESISTANCE"].split()[-1]) / w * 1000
    c = (float(p["CAPACITANCE"].split()[-1]) * w
         + 2 * float(p["EDGECAPACITANCE"].split()[-1])) * 1000   # pF -> fF
    print(f"{name:6}{r:12.1f}{c:11.1f}")
```

2. 依次运行：

```console
$ python3 techlef_rc.py prtech/techLEF/N551P6M_ecos.lef
$ python3 techlef_rc.py prtech/techLEF/N551P6M.lef
```

**需要观察的现象**：第一次运行应重现 4.1.3 表格的 R/C 两列；第二次运行在 `float(p["CAPACITANCE"]...)` 处抛 `KeyError: 'CAPACITANCE'`——这个报错本身就是"原版没有电容参数"的程序化证据。

**预期结果**：ecos 版输出 MET1 约 1246.7 / 136.5、T4M2 约 59.8 / 125.6、RDL 约 5.0 / 228.4；原版必然 KeyError。（脚本为读者侧练习，未在讲义编写环境执行，输出数值由 4.1.3 公式手算核对，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：一条 500μm、最小宽度的 MET2 线，R 和 C 各多少（ecos 版参数）？
**答**：\( R = 0.0914 \times 500/0.1 = 457\,\Omega \)；\( C = 0.0011069 \times 0.1 \times 500 + 0.0000391 \times 2 \times 500 = 0.0553 + 0.0391 \approx 0.0944\,\text{pF} \)（约 94.4 fF）。

**练习 2**：为什么 T4M2、RDL 的 CPERSQDIST 比 MET2 小一到两个数量级？
**答**：它们在金属栈中位置更高、导体更厚，离衬底更远，单位面积的对地耦合更弱。具体数值来自工艺方的提取，PDK 未附带提取条件说明，其精确分解待确认。

**练习 3**：`EDGECAPACITANCE` 为什么乘 2L？
**答**：一条导线有左右两条侧边，每条都对邻居/衬底耦合，\( C_{\text{edge}} = \text{EDGECAPACITANCE} \times 2L \)。以 MET1 为例每微米面积电容约 0.0687 fF、边缘电容约 0.0678 fF，几乎对半——忽略任何一项都会错约 50%。

### 4.2 两版 tech LEF diff 分析

#### 4.2.1 概念说明

仓库同时维护两份工艺 LEF，是"一份工艺数据服务多类工具"的做法：原版面向传统商业流程，`_ecos` 版面向开源 EDA 工具链。README 里明确写了维护方"committed to improving its compatibility and stability with mainstream open source EDA tool chains"（[README.md:L61](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L61)）。

这个变体不是凭空出现的，git 历史把动机写得很清楚（以下均为只读命令，可自行复现）：

```console
$ git log --oneline --follow -- prtech/techLEF/N551P6M_ecos.lef
4f5b659 style: rename from *_ieda.lef to *_ecos.lef
993caaf tech: Remove Virtuoso poly rule
0ed49f0 feat: add some properties
327eb8a feat: add tech lef for ieda
3338e16 feat: add open source pdk
```

时间线（`--follow` 之所以追到 3338e16，是因为 327eb8a 以"复制原版再修改"的方式建新文件）：

1. **3338e16**（2025-10，"add open source pdk"）：仓库初始导入，原版 `N551P6M.lef` 诞生——OFFSET 0 0、无电容、含 `virtuosoDefaultTaper`。
2. **327eb8a**（2025-10-21，"add tech lef for ieda"）：以原版为底新建 `N551P6M_ieda.lef`，新建时就已把 7 层 OFFSET 改成 0.1（可用 `git show 327eb8a:prtech/techLEF/N551P6M_ieda.lef` 验证），但**还没有**电容参数。最初的服务对象是 iEDA 工具链。
3. **0ed49f0**（2025-10-22，"add some properties"）：插入 14 行，即 7 层 ×（`CAPACITANCE CPERSQDIST` + `EDGECAPACITANCE`）。
4. **993caaf**（2025-11-11，"Remove Virtuoso poly rule"）：删除 10 行的 `NONDEFAULTRULE virtuosoDefaultTaper`——提交标题直接点明这是 Cadence Virtuoso 的遗留物。
5. **4f5b659**（2025-12-24）：`_ieda` 改名 `_ecos`（同批还有三个标准单元 LEF），把"为某一工具定制"泛化为"为开源生态定制"。

原版一侧的对称操作同样值得注意：`5dbfd0e`（2025-11-12）把同名 `virtuosoDefaultTaper` 从原版删掉，而 `6e902bf`（2026-08-01）又把这段规则以 `DefaultTaper` 之名**加了回去**。所以今天"原版有、ecos 版没有"的状态是两份文件**各自独立演进**的结果，不是简单的单向裁剪。

#### 4.2.2 核心流程

量化两份文件差异的标准流程：

```text
1. 整体统计:  git diff --no-index --stat A.lef B.lef
   -> 21 insertions(+), 17 deletions(-)   (672 行 -> 676 行)
2. 逐块定位:  git diff --no-index A.lef B.lef
   -> 8 个改动块 = 7 个层定义块 + 1 个 DefaultTaper 删除块
3. 对账:
   +21 = 14 行电容(纯新增) + 7 行 OFFSET 新值(改写)
   -17 = 7 行 OFFSET 旧值(改写) + 10 行 DefaultTaper(9 行语句 + 1 行空行)
   672 + 21 - 17 = 676 ✓
4. 反证: 38 个 VIA、6 个 VIARULE、3 个 SITE、UNITS、MANUFACTURINGGRID
   在 diff 输出中不出现任何 hunk，即逐字节相同
```

第 3 步"对账"很重要：它证明除了三类差异**再无其他改动**，防止文档凭印象多写一条不存在的差异。

#### 4.2.3 源码精读

**差异一：OFFSET `0 0` → `0.1 0.1`（7 个层）。**
原版 MET1 的 [L67](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L67) 是 `OFFSET 0 0`，ecos 版同位置 [L67](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L67) 改为 `OFFSET 0.1 0.1`。MET2 同理（原版 [L88](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L88) → ecos [L90](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L90)），七个层统一平移 0.1μm。

这条改动的几何意义可以算出来。对 MET1–MET5（PITCH 0.2），0.1 恰是**半节距**：

- **x 方向（VERTICAL 层）**：site 宽 0.2、全部标准单元引用 core7（u2-l2 结论）。OFFSET 0 时轨道中心落在 x = 0, 0.2, 0.4…，正好压在 **site 列边界**（相邻单元拼接缝）上；平移半节距后轨道穿过每个 site 的中心。
- **y 方向（MET1 等 HORIZONTAL 层）**：行高 1.4 = 7 × 0.2。OFFSET 0 时轨道中心含 y = 0 与 y = 1.4，恰是**行边界**；而标准单元的电源轨道正画在行边界上——ecos 版单元 LEF 中 ADDFX1H7H（SIZE 4.8 BY 1.4，[ics55_LLSC_H7CH_ecos.lef:L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L23)）的 VDD 轨道 `RECT 0 1.32 4.8 1.48` 中心在 y=1.4（[L32](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L32)），VSS 轨道 `RECT 0 -0.08 4.8 0.08` 中心在 y=0（[L45](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L45)）。OFFSET 0.1 后，一行内 7 条轨道中心为 0.1, 0.3, …, 1.3，全部避开两个轨道中心与电源轨道中心重合的位置。

注意对 T4M2（pitch 0.8）与 RDL（pitch 5），0.1 并非半节距，只是同幅平移——说明这是一次整体的网格相位搬移，而非逐层定制。布线器对"轨道压在行边界/列边界"是否视为冲突属于工具行为，待本地验证；提交说明里也未展开维护者的完整动机，上述是数据层面可验证的几何事实。

**差异二：新增 14 行电容（7 个层）。**
即 4.1.3 精读过的各行。原版对应层块（如 MET2 [prtech/techLEF/N551P6M.lef:L83-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L83-L95)）从 `MINENCLOSEDAREA` 直接到 `RESISTANCE`，中间没有电容语句。

**差异三：删除 NONDEFAULTRULE DefaultTaper。**
[prtech/techLEF/N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652) 在六个 VIARULE 之后定义了名为 `DefaultTaper` 的非默认规则：

```text
NONDEFAULTRULE DefaultTaper
  LAYER POLY
    WIDTH 0.06 ;
  END POLY
  LAYER MET1
    WIDTH 0.09 ;
  END MET1
  USEVIARULE MET1_POLY ;      <-- 全文件找不到名为 MET1_POLY 的 VIARULE 定义
END DefaultTaper
```

它有两个可疑点：其一，`MET1_POLY` 是悬空引用——`grep -rn MET1_POLY prtech/` 只命中这一行；其二，POLY 在本工艺 LEF 里是 MASTERSLICE 层（[prtech/techLEF/N551P6M.lef:L50-L52](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L50-L52)），不是布线层，"poly 线宽规则"是版图编辑器（Virtuoso）语境的概念，对布线工具没有意义。ecos 版把它整段拿掉：[prtech/techLEF/N551P6M_ecos.lef:L656-L658](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L656-L658) 处 `END RDL_T4M2` 之后空一行就直接进入 `SITE CoreSite`。

#### 4.2.4 代码实践

**实践目标**：用脚本把三类差异一次性量化，输出"层 × 差异"报告，并用 git 对账。

**操作步骤**：

1. 保存以下**示例代码**为 `techlef_diff.py`，在仓库根目录运行 `python3 techlef_diff.py`：

```python
#!/usr/bin/env python3
"""示例代码：量化 N551P6M.lef 与 N551P6M_ecos.lef 的三类差异"""

def parse_layers(path):
    layers, cur = {}, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("LAYER "):
            cur = line.split()[1]
            layers[cur] = {}
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

n_off = n_cap = 0
print(f"{'LAYER':6}{'OFFSET 原版':>13}{'OFFSET ecos':>13}  新增电容")
for name, p in orig.items():
    if p.get("TYPE") != "ROUTING":
        continue
    q = ecos[name]
    if p["OFFSET"] != q["OFFSET"]:
        n_off += 1
    cap = ""
    if "CAPACITANCE" in q and "CAPACITANCE" not in p:
        n_cap += 1
        cap = q["CAPACITANCE"] + " / " + q["EDGECAPACITANCE"]
    print(f"{name:6}{p['OFFSET']:>13}{q['OFFSET']:>13}  {cap}")

for tag, path in (("原版", "prtech/techLEF/N551P6M.lef"),
                  ("ecos 版", "prtech/techLEF/N551P6M_ecos.lef")):
    rows = [i + 1 for i, l in enumerate(open(path, encoding="utf-8"))
            if l.startswith("NONDEFAULTRULE")]
    print(f"{tag} NONDEFAULTRULE 所在行: {rows}")
print(f"\nOFFSET 改变的层: {n_off} 个; 新增电容的层: {n_cap} 个")
```

2. 再做人工对账：

```console
$ git diff --no-index --stat prtech/techLEF/N551P6M.lef prtech/techLEF/N551P6M_ecos.lef
```

**需要观察的现象**：脚本列出 7 个 ROUTING 层，OFFSET 全部 `0 0 → 0.1 0.1`，每层都带一行新增电容；`NONDEFAULTRULE 所在行` 原版为 `[644]`、ecos 版为空列表。

**预期结果**：`OFFSET 改变的层: 7 个; 新增电容的层: 7 个`，与 git 的 21 增 17 删、672→676 对账一致。（git diff 部分的数据来自讲义编写时的实际运行；Python 脚本为读者侧练习，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `672 + 21 - 17 = 676` 恰好等于 ecos 版行数？
**答**：+21 = 14 行电容（纯新增）+ 7 行 OFFSET 新值；−17 = 7 行 OFFSET 旧值 + DefaultTaper 块（9 行语句 + 1 行空行）。OFFSET 属"一删一增"的改写，电容是纯增，规则是纯删。

**练习 2**：只看行数差（676 − 672 = 4）会得出什么错误结论？
**答**：会以为"只多了 4 行"，从而漏掉 7 行 OFFSET 改写与 10 行规则删除这些行数相消的改动。行数差只反映净变化，量化 diff 必须分别统计插入与删除。

**练习 3**：`_ecos` 版为什么同时补电容、改 OFFSET、删 DefaultTaper，而不是只补电容？
**答**：三类改动服务同一目标——让开源工具正确消费这份文件：电容让时序估计有数据，轨道相位平移让布线网格与单元视图对齐，删除带悬空引用的 Virtuoso 遗留规则消除解析隐患。改名史（`_ieda` → `_ecos`）也说明它被定位为面向整个开源生态的通用适配层。

### 4.3 开源工具寄生估计依赖

#### 4.3.1 概念说明

开源数字后端流程（以 OpenROAD 为代表，iEDA 同理）的时序闭环依赖互连寄生：

```text
布线(或全局布线) → 估计寄生 R/C → 延迟计算 → 时序分析 → 优化/再布线
                        ↑
             这一步的输入只有两个来源:
             a) 专门的 RC 提取规则文件（工艺方提供；ICS55 preview 未提供）
             b) tech LEF 层表里的 RESISTANCE / CAPACITANCE 一阶参数
```

ICS55 目前没有随包发布 RC 提取规则文件（u1-l1 的 Todo 清单里 RC 也在待补之列），因此开源流程的寄生估计只能走路线 (b)。这正是 `_ecos` 版补 14 行电容的动机：没有它们，路线 (b) 也只剩一半——有电阻、没电容。

需要说明：不同工具/版本在缺数据时的回退行为不同，有的用 LEF 层参数做一阶估计，有的置零并告警。本讲只确立"PDK 侧的数据前提"，具体工具行为在 4.3.4 实践中验证，待本地验证。

#### 4.3.2 核心流程

以一根从单元 A 输出到单元 B 输入的网线为例，一阶估计链路：

```text
1. 读 tech LEF 层表: 每层 RPERSQ / CPERSQDIST / EDGECAPACITANCE / WIDTH
2. 读布线结果: 网线在各层的分段长度 ℓ_k 与过孔数 v
3. R_net = Σ_k (RPERSQ_k × ℓ_k / W_k) + 2.5Ω × v     (2.5Ω 来自 VIA 语句)
   C_net = Σ_k (CPERSQDIST_k × W_k + 2 × EDGECAPACITANCE_k) × ℓ_k
         + Σ 引脚电容（来自 liberty）
4. 延迟 ≈ 驱动电阻 × C_net（负载项） + 0.5 × R_net × C_net（线项）
```

第 1 步若缺失电容参数，第 3 步的 C_net 只剩 liberty 引脚电容，**线电容为零**——这就是误用原版的直接后果。

#### 4.3.3 源码精读

**原版零电容的证据。**
[prtech/techLEF/N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74) 的 MET1 从几何规则直接跳到 `RESISTANCE RPERSQ 0.1122`，7 个 ROUTING 层皆如此。可自行验证：

```console
$ grep -c CAPACITANCE prtech/techLEF/N551P6M.lef       # -> 0
$ grep -c CAPACITANCE prtech/techLEF/N551P6M_ecos.lef  # -> 14（含 EDGECAPACITANCE 行）
```

**过孔电阻两版一致，且不可忽略。**
[prtech/techLEF/N551P6M_ecos.lef:L216-L217](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L216-L217)：`VIA MET2_MET1_VIA1_0` 的 `RESISTANCE 2.5`。按 4.1.2 的每微米电阻（MET1 约 1.247 Ω/μm），一个过孔 ≈ **2μm** 最小宽度 MET1 走线的电阻；换层三次就是 7.5Ω 起步，"能少换层就少换层"有了量化依据。这段在原版 [L202-L203](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L202-L203) 逐字节相同。

**单位约定是隐式的。**
[prtech/techLEF/N551P6M_ecos.lef:L26-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L26-L28) 的 `UNITS` 块只有 `DATABASE MICRONS 1000`。LEF 允许在此显式写 `CAPACITANCE PICOFARADS`、`RESISTANCE OHMS`，本文件未写而取默认 pF/Ω——4.1 的单位换算即以此为据。若工具按其他单位解读，数值会差若干数量级，这是排查"寄生值离谱"时的第一个检查点。

#### 4.3.4 代码实践

**实践目标**：验证"原版缺电容、ecos 版补齐"，并写一段误用后果分析；有 OpenROAD 环境者做加载对比。

**操作步骤**：

1. 在仓库根目录运行并记录输出（讲义编写时实际运行过，结果如下）：

```console
$ grep -c 'CAPACITANCE' prtech/techLEF/N551P6M.lef
0
$ grep -c 'CAPACITANCE' prtech/techLEF/N551P6M_ecos.lef
14
```

2. 用 4.1.4 的脚本分别处理两份文件，记录原版的行为（KeyError 即预期）。
3. （可选，需自备环境）用 OpenROAD 分别只读入两版 tech LEF，对比告警与延迟估计：

```tcl
# 示例脚本
read_lef prtech/techLEF/N551P6M.lef      ;# 换成 _ecos 版再跑一次对比
puts [[[ord::get_db_tech] getLayers] size]
```

4. 写一段 150–250 字说明：在开源布线/时序工具中误用无 RC 版本会导致什么后果。

**需要观察的现象**：第 1 步原版返回 0、ecos 版返回 14；第 2 步原版无法产出电容值；第 3 步关注工具是否打印缺少 RC 的告警、布线后 `estimate_parasitics` 类命令的输出差异（具体行为随版本而异，待本地验证）。

**预期结果**：确认"原版只够算 R，ecos 版才能算 R 和 C"。误用后果分析的参考要点（写进你的说明里）：线电容被记为零 → 互连延迟只剩驱动电阻 × 引脚电容，长网延迟被严重低估，时序"纸面通过"；优化器看不到长线真实代价，不会插中继 buffer、也不会把关键网升到高层金属；时钟树综合的偏斜估计失真；换用完整 RC 复核时会发现违例集中出现在长网上。此外，若已有 DEF 引用了 `NONDEFAULTRULE DefaultTaper`，读入不含该规则的 `_ecos` 版可能报"未定义规则"——选版本时这条也要一并考虑。

#### 4.3.5 小练习与答案

**练习 1**：某设计用原版 tech LEF 完成布局布线并导出 DEF，最后用完整 RC 做签核，可能看到什么？
**答**：布线几何可能合法（几何规则两版一致），但流程内时序估计不可信；签核时长网延迟明显大于估计值，出现成片负裕量，且集中在扇出大、线长的网。

**练习 2**：既然 `_ecos` 版"更全"，仓库为什么不直接替换原版？
**答**：原版是工艺方交付的基线，服务于商业工具为主的传统流程（`DefaultTaper` 正是 Virtuoso 语境的产物）；`_ecos` 是叠加其上的适配层。平行维护让两类用户各取所需，代价是两份文件要分别演进（见 4.2.1 的分叉史）。这是开源 PDK 常见的工程折中。

**练习 3**：一个 2.5Ω 的过孔相当于多长最小宽度的 MET1 与 MET2？
**答**：MET1 每微米约 1.247Ω，2.5/1.247 ≈ 2μm；MET2 每微米 0.0914/0.1 = 0.914Ω，2.5/0.914 ≈ 2.7μm。

## 5. 综合实践

**任务：产出一份《ICS55 tech LEF 选型报告》。**

假设你要为一个基于 OpenROAD 的课程实验选择工艺文件，综合本讲三个模块完成：

1. **数据核对**（用 4.2.4 的脚本）：列出两版文件在 7 个 ROUTING 层上的 OFFSET 与 RC 差异表，附 git diff 的 21 增 17 删对账。
2. **量化分析**（用 4.1.4 的脚本）：取 MET1 与 T4M2 各 1mm 最小宽度线，计算 R、C 与一阶延迟，说明厚金属在长网上的收益倍数（应约 23 倍）。
3. **决策**：给出三种场景的选型建议并注明证据出处——(a) 只做综合后门级仿真；(b) 做布局布线 + 时序评估；(c) 流程里引用了 `DefaultTaper` 非默认规则。
4. **风险清单**：列出选 `_ecos` 版要注意的两件事（例如：电容单位依赖 LEF 默认 pF 约定；`NONDEFAULTRULE` 缺失对已有 DEF 的影响）。

参考结论骨架：场景 (a) 与 tech LEF 基本无关（verilog/liberty 足够，见 u3-l5、u3-l6）；场景 (b) 必须 `_ecos`（证据：提交 0ed49f0 与 [N551P6M_ecos.lef:L72-L73](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M_ecos.lef#L72-L73)）；场景 (c) 只能用原版或先去掉规则引用（证据：[N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652)）。

## 6. 本讲小结

- `_ecos` 版与原版只有三类差异：7 个 ROUTING 层新增 `CAPACITANCE CPERSQDIST` + `EDGECAPACITANCE`（14 行）、7 个层 `OFFSET 0 0 → 0.1 0.1`、删除 10 行 `NONDEFAULTRULE DefaultTaper`；git 对账 21 增 17 删，其余（38 个 VIA、6 个 VIARULE、3 个 SITE）逐字节相同。
- 电容默认按 pF 计：\( C = \text{CPERSQDIST} \cdot WL + \text{EDGECAPACITANCE} \cdot 2L \)，\( R = \text{RPERSQ} \cdot L/W \)，每个过孔固定 2.5Ω（≈ 2μm 最小宽度 MET1）。
- 量化后果：1mm 最小宽度 MET1 线约 1.25kΩ / 137fF、一阶延迟约 85ps；同长 T4M2 的 RC 乘积约 1/23；且 RC 随长度平方增长。
- OFFSET 平移半节距（0.1 = 0.2/2）使垂直层轨道穿过 site 中心、水平层轨道避开行边界上的电源轨道中心——几何事实可从 core7（0.2×1.4）与单元 LEF 电源轨道 RECT 直接验证。
- 变体史：`_ieda`（327eb8a，OFFSET）→ 补 RC（0ed49f0）→ 删 Virtuoso 规则（993caaf）→ 改名 `_ecos`（4f5b659）；原版同期删了又加回 DefaultTaper（5dbfd0e → 6e902bf），两版各自独立演进。
- 选型规则：凡要做寄生/时序估计的开源流程必须 `_ecos`；纯物理规则查询可用原版；引用 `DefaultTaper` 的流程不能直接换 `_ecos`。

## 7. 下一步学习建议

tech LEF 三讲到此完整：u2-l1 层规则、u2-l2 VIA/VIARULE/SITE、本讲 `_ecos` 变体与 RC。接下来两条路：

1. **进入单元库**：u3-l2《单元 LEF 抽象视图解剖》——看 MACRO 如何引用本讲的 core7 site 与 MET1/MET2 层；随后 u3-l3 讲单元级 `_ecos` 变体（VDD/VSS 电源轨道 pin 与 MET2 高层引脚），与本讲正好衔接：tech 层的轨道相位变了，单元视图的引脚形状也要跟着补。
2. **动手验证**：u6-l1《把 PDK 装进开源 EDA 工具》会用 `read_lef` 实际读入 `N551P6M_ecos.lef` 与下载的 liberty，把本讲 4.3.4 的"待本地验证"项落到实处。

继续阅读建议：先自己跑一遍 4.2.4 的脚本再看 u3-l2；有余力可对照 LEF/DEF 规范中 LAYER(ROUTING) 的 OFFSET 语义一节，加深对"轨道相位"的理解。
