# 工艺 LEF（二）：VIA、VIARULE 与 SITE

## 1. 本讲目标

上一讲（u2-l1）我们读完了 `N551P6M.lef` 的「层定义」部分——知道了 20 个 LAYER 分成 MASTERSLICE / CUT / ROUTING 三类，金属栈是 MET1–MET5 + T4M2 + RDL。但只认识「层」还不足以布线：信号从 MET1 爬到 MET2 需要过孔，标准单元要摆进「行」里才成芯片。本讲读完你应该能够：

1. 看懂一段固定 `VIA` 定义，把 `LAYER ... RECT ...` 三层语句翻译成「过孔的物理形状清单」；
2. 说出仓库里 4 组 × 9 个过孔变体的命名规律，并解释为什么恰好是 9 个；
3. 区分固定 `VIA` 与 `VIARULE ... GENERATE`：一个是「预制积木」，一个是「现场生成规则」；
4. 解释 `SITE` 的宽度/高度如何决定行式布局，并能从 `core7` / `core9` 的尺寸反推出「7 轨 / 9 轨单元库」的含义；
5. 顺带认识 `NONDEFAULTRULE DefaultTaper`——以及它为什么在 `_ecos` 版里被删掉。

## 2. 前置知识

**过孔（via）是什么。** 芯片是立体的：金属线在不同层上跑，层与层之间隔着绝缘介质。要 把 MET1 上的一根线和 MET2 上的一根线连起来，需要在介质上刻一个「洞」，洞里填上金属（通常是钨），这个洞就是过孔的 **cut**。为了让 cut 与上下两层的金属线可靠接触，cut 上下还要各垫一小块金属 **pad**（即金属层上的矩形）。所以一个过孔 = 1 个 cut 矩形 + 2 个金属 pad 矩形，共三层。

**enclosure（包覆）是什么。** cut 必须被金属 pad「包住」一圈才不会断路。若 pad 宽 \( W_m \)、cut 宽 \( W_c \) 且两者中心对齐，则单边包覆量为

\[ e = \frac{W_m - W_c}{2} \]

包覆太小工艺上容易失效（对准偏差吃掉接触面积），太大又浪费面积、挤占邻近走线空间。过孔设计就是在两者之间取平衡。

**RECT 语法回顾。** LEF 中 `RECT x1 y1 x2 y2` 给出矩形的两个对角点。宽 \( = x_2 - x_1 \)，高 \( = y_2 - y_1 \)。注意本文件所有 VIA 的 RECT 都是关于原点对称的（如 `-0.045 -0.045 0.045 0.045`），即「以过孔中心为原点」的局部坐标，放置时再平移。

**布线轨道（track）回顾。** u2-l1 讲过 `PITCH 0.2 0.2`：每 0.2μm 一条轨道。层定义里 MET1 的 pitch 会直接决定 site 高度的「轨道数」，这是本讲 4.3 节的关键。

**行式布局（row-based placement）是什么。** 数字芯片的标准单元像地砖一样规格统一：同一行里单元高度相同、首尾相接；一行行摞起来构成 core 区域。`SITE` 就是这块「地砖」的最小规格——宽度是合法宽度的最小增量，高度就是行高。

## 3. 本讲源码地图

本讲的核心源码只有一个文件，但它内部段落分明：

| 文件 | 行范围 | 内容 |
|---|---|---|
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L202-L581) | 202–581 | 38 个固定 `VIA` 定义 |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L584-L642) | 584–642 | 6 个 `VIARULE ... GENERATE` |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652) | 644–652 | `NONDEFAULTRULE DefaultTaper` |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L654-L670) | 654–670 | 三个 `SITE`：CoreSite / core7 / core9 |

验证 SITE 引用时还要看标准单元 LEF（只需看一个单元的头部）：

| 文件 | 行范围 | 内容 |
|---|---|---|
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048-L3054) | 3048–3054 | `MACRO ANT2H7H` 头部：`SIZE 0.4 BY 1.4` 与 `SITE core7` |

一句话概括文件结构：**层定义（30–201 行）→ 固定 VIA（202–581）→ VIARULE（584–642）→ NONDEFAULTRULE（644–652）→ SITE（654–670）→ END LIBRARY（672）**。从上到下正好是「材料 → 连接件 → 布局格点」的顺序。

## 4. 核心概念与源码讲解

### 4.1 固定 VIA：预制的过孔「积木」

#### 4.1.1 概念说明

布线器（router）在布一根线时，遇到需要换层的时刻就「放置」一个过孔。最简单的做法是：工艺厂预先设计好一批形状固定、已通过工艺验证的过孔模板，写进 tech LEF，布线器按名字取用——这就是**固定 VIA**。语句 `VIA <名字> DEFAULT` 中的 `DEFAULT` 关键字表示「布线器可以把它当作默认过孔直接选用，不需要设计者显式指定」。

固定 VIA 解决的问题：**把过孔的物理形状标准化**。若每个工具都自己发明过孔形状，DRC（设计规则检查）将无法收敛；预制形状则保证「凡是在 LEF 里登记的过孔，工厂一定能造、一定合格」。

#### 4.1.2 核心流程

一个固定 VIA 的定义结构固定为：

```text
VIA <名字> DEFAULT
    RESISTANCE <阻值> ;        ← 这个过孔的寄生电阻（Ω）
    LAYER <下层金属> ;          ← 下层 pad
        RECT x1 y1 x2 y2 ;
    LAYER <cut 层> ;            ← 过孔切割方块
        RECT x1 y1 x2 y2 ;
    LAYER <上层金属> ;          ← 上层 pad
        RECT x1 y1 x2 y2 ;
END <名字>
```

布线器换层时的动作：

1. 确定要连接的两层（例如 MET1 → MET2）；
2. 在候选过孔集合（这里是 `MET2_MET1_VIA1_0` … `_8` 共 9 个）里挑选一个——依据上下两层走线方向、周围拥塞、DRC 余量；
3. 把选中的三层矩形整体平移到连接点，写进 DEF。

本仓库固定 VIA 的完整清单：

| 分组 | 数量 | 覆盖层对 |
|---|---|---|
| `MET2_MET1_VIA1_0` … `_8` | 9 | MET1 ↔ MET2（cut 层 VIA1） |
| `MET3_MET2_VIA2_0` … `_8` | 9 | MET2 ↔ MET3（cut 层 VIA2） |
| `MET4_MET3_VIA3_0` … `_8` | 9 | MET3 ↔ MET4（cut 层 VIA3） |
| `MET5_MET4_VIA4_0` … `_8` | 9 | MET4 ↔ MET5（cut 层 VIA4） |
| `T4M2_MET5` | 1 | MET5 ↔ T4M2（cut 层 T4V2） |
| `RDL_T4M2` | 1 | T4M2 ↔ RDL（cut 层 RV） |

合计 **38 个**。注意：普通金属层对每层给 9 个变体，而厚金属/封装层（T4M2、RDL）只给 1 个——因为厚金属层几乎只用于电源和封装，布线场景单一，不需要精细变体。

为什么恰好是 9 个？观察 `_0` 到 `_8` 的矩形会发现 pad 只有三种形状：

| 代号 | RECT（局部坐标） | 宽×高（μm） | x 向包覆 | y 向包覆 |
|---|---|---|---|---|
| 竖条 | `-0.050 -0.085 0.050 0.085` | 0.10 × 0.17 | 0.005 | 0.04 |
| 方块 | `-0.075 -0.075 0.075 0.075` | 0.15 × 0.15 | 0.03 | 0.03 |
| 横条 | `-0.085 -0.050 0.085 0.050` | 0.17 × 0.10 | 0.04 | 0.005 |

（cut 恒为 0.09 × 0.09，包覆按 \( e = (W_m - W_c)/2 \) 计算。）

下、上两层金属的形状各有 3 种选择，组合数 \( 3 \times 3 = 9 \)。而且仓库的命名遵循一个整齐的编码规律——若记竖=0、方=1、横=2，则

\[ n \;=\; 3 \times (\text{上层形状}) + (\text{下层形状}) \]

即编号由「上层形状 × 3 + 下层形状」决定。这个规律在 4.1.4 的实践中会用脚本逐个验证。

为什么需要不同形状的 pad？回忆 u2-l1：MET1 水平走线、MET2 垂直走线，方向交替。横条在水平方向包覆大（0.04），顺着水平层的走线方向扩展；竖条在垂直方向包覆大，更贴合垂直层。布线器据此挑选最省空间、最不容易和邻居冲突的变体——9 种组合覆盖了「上下两层方向偏好」的全部搭配。

#### 4.1.3 源码精读

先看最基本的一个变体：

[VIA MET2_MET1_VIA1_0，见 prtech/techLEF/N551P6M.lef:L202-L210](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L202-L210)

这段代码定义了 MET1→MET2 的 0 号过孔：电阻 2.5Ω；MET1 pad 是 0.10×0.17 的竖条，VIA1 cut 是 0.09×0.09 的方块，MET2 pad 与 MET1 相同。整段语句以 `END MET2_MET1_VIA1_0` 收尾，名字首尾呼应，这是 LEF 块语句的标准防错写法。

三层矩形翻译成物理形状清单就是：

| 层 | 矩形（μm，局部坐标） | 尺寸 | 作用 |
|---|---|---|---|
| MET1 | (-0.050, -0.085) → (0.050, 0.085) | 0.10 × 0.17 | 下层金属 pad |
| VIA1 | (-0.045, -0.045) → (0.045, 0.045) | 0.09 × 0.09 | cut（介质上的孔） |
| MET2 | (-0.050, -0.085) → (0.050, 0.085) | 0.10 × 0.17 | 上层金属 pad |

再看「双方块」变体，它是 9 个组合的几何中心：

[VIA MET2_MET1_VIA1_4，见 prtech/techLEF/N551P6M.lef:L242-L250](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L242-L250)

这段代码把 MET1 和 MET2 两个 pad 都换成了 0.15×0.15 的方块（cut 不变），对应编号公式 \( n = 3 \times 1 + 1 = 4 \)。对比 `_0` 可以清楚看到「只有 pad 在变、cut 永远是 0.09」——cut 尺寸由工艺决定（VIA1 层定义 `WIDTH 0.09`，见 [prtech/techLEF/N551P6M.lef:L76-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L76-L81)），pad 尺寸才是设计自由度。

然后是两个「大块头」：

[VIA T4M2_MET5，见 prtech/techLEF/N551P6M.lef:L563-L571](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L563-L571)

这段代码定义 MET5 与厚金属 T4M2 之间的过孔：T4V2 cut 是 0.36×0.36（恰好等于 T4V2 层定义的 `WIDTH 0.36`），MET5 pad 0.38×0.46，T4M2 pad 0.4×0.4（等于 T4M2 层 `WIDTH 0.4`）。注意 MET5 本身的 `WIDTH 0.09` 线宽只有 0.1（见 [L104-L116](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L104-L116)），但过孔处 pad 扩到 0.38×0.46——厚金属电流大（`DCCURRENTDENSITY 8.1`），接触面必须大。

[VIA RDL_T4M2，见 prtech/techLEF/N551P6M.lef:L573-L581](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L573-L581)

这段代码定义 T4M2 与封装再布线层 RDL 之间的过孔：RV cut 3×3，两侧金属 pad 都是 6×6。这里可以做一个精确验算——RV 层定义要求 `ENCLOSURE BELOW 1.5 1.5` 和 `ENCLOSURE ABOVE 1.5 1.5`（见 [L185-L191](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L185-L191)），而 \( (6 - 3)/2 = 1.5 \)：**金属 pad 的单边包覆恰好压线满足层定义的工艺要求，一点不多**。这是全文件里「固定 VIA 与层规则精确咬合」的最漂亮例子。

最后看一眼过孔电阻：38 个固定 VIA 的 `RESISTANCE` 全部是 2.5Ω，与层电阻（`RPERSQ`）是两类参数——前者是一个过孔的整体电阻，后者是金属薄层方块电阻。做寄生提取时，一个过孔贡献 2.5Ω，一段金属线按 \( R = R_{persq} \times L / W \) 累加（u2-l1 讲过）。

#### 4.1.4 代码实践

**实践目标**：把 `MET2_MET1_VIA1_0` 与 `VIARULE MET2_MET1 GENERATE` 的完整语句解析出来，翻译成三层矩形清单，并用脚本验证「9 变体 = 3×3 编码」规律。

**操作步骤**：

1. 提取一个固定 VIA 的原文，确认结构：

   ```bash
   sed -n '202,210p' prtech/techLEF/N551P6M.lef
   ```

2. 数一数每组的变体数（应输出 9 行）：

   ```bash
   grep -c '^VIA MET2_MET1_VIA1_' prtech/techLEF/N551P6M.lef
   ```

3. 运行下面这段示例代码（**示例代码**，非仓库自带脚本），它解析所有固定 VIA、给 pad 归类形状、验证编号公式：

   ```python
   #!/usr/bin/env python3
   # 示例代码：解析 tech LEF 固定 VIA，验证 9 变体的 3x3 编码规律
   import re

   src = open("prtech/techLEF/N551P6M.lef").read()
   via_re  = re.compile(r"VIA (\S+) DEFAULT\s*(.*?)\nEND \1", re.S)
   rect_re = re.compile(r"LAYER (\S+) ;\s*RECT ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)")

   def wh(r):                      # 两点坐标 -> (宽, 高)
       x1, y1, x2, y2 = map(float, r)
       return (round(x2 - x1, 3), round(y2 - y1, 3))

   shapes = {(0.10, 0.17): 0, (0.15, 0.15): 1, (0.17, 0.10): 2}  # 竖/方/横

   for m in via_re.finditer(src):
       name, body = m.group(1), m.group(2)
       layers = {l: wh(r) for l, *r in rect_re.findall(body)}
       cut  = [k for k in layers if k.startswith(("VIA", "CT", "T4V", "RV"))][0]
       lo, hi = sorted(k for k in layers if k != cut)   # 字母序即"下层,上层"
       print(f"{name}: 下层{lo}={layers[lo]} cut{cut}={layers[cut]} 上层{hi}={layers[hi]}")
       if name.startswith("MET2_MET1_VIA1_"):
           n = int(name.rsplit("_", 1)[1])
           assert n == 3 * shapes[layers[hi]] + shapes[layers[lo]], name
   print("9 变体编码规律 n = 3*上层形状 + 下层形状：全部通过")
   ```

   （注：`lo, hi = sorted(...)` 利用 MET1 < MET2 的字典序区分上下层，对本文件的名字成立。）

**需要观察的现象**：

- 每个固定 VIA 都打印出「下层 pad → cut → 上层 pad」三个尺寸；
- 9 个 `MET2_MET1_VIA1_*` 的 pad 尺寸只在 {0.10×0.17, 0.15×0.15, 0.17×0.10} 三种里取值；
- 最后断言全部通过，没有 `AssertionError`。

**预期结果**（作者已逐行人工核对当前 HEAD 的 [L202-L290](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L202-L290)，脚本本身待本地验证运行）：9 个变体的形状矩阵为

| n | MET1（下层） | MET2（上层） | 校验 3×上+下 |
|---|---|---|---|
| 0 | 竖 | 竖 | 3×0+0 = 0 ✓ |
| 1 | 方 | 竖 | 3×0+1 = 1 ✓ |
| 2 | 横 | 竖 | 3×0+2 = 2 ✓ |
| 3 | 竖 | 方 | 3×1+0 = 3 ✓ |
| 4 | 方 | 方 | 3×1+1 = 4 ✓ |
| 5 | 横 | 方 | 3×1+2 = 5 ✓ |
| 6 | 竖 | 横 | 3×2+0 = 6 ✓ |
| 7 | 方 | 横 | 3×2+1 = 7 ✓ |
| 8 | 横 | 横 | 3×2+2 = 8 ✓ |

#### 4.1.5 小练习与答案

**练习 1**：不查文件，推出 `MET3_MET2_VIA2_5` 的 MET2 与 MET3 pad 形状。

**答案**：\( 5 = 3 \times 1 + 2 \)，上层（MET3）形状编号 1 = 方块，下层（MET2）编号 2 = 横条。对照 [L342-L350](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L342-L350)：MET2 为 `-0.085 -0.050 0.085 0.050`（横条）、MET3 为 `-0.075 -0.075 0.075 0.075`（方块），与推导一致。

**练习 2**：为什么所有 VIA1 过孔的 cut 都是 `-0.045 -0.045 0.045 0.045`？

**答案**：因为 VIA1 是 CUT 类型层，其层定义规定 `WIDTH 0.09`（[L76-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L76-L81)）——cut 的物理尺寸由工艺决定，不随变体变化；变体只改变上下金属 pad 的形状。

**练习 3**：`T4M2_MET5` 与 `RDL_T4M2` 为什么不像普通金属层对那样给 9 个变体？

**答案**：它们连接的是厚金属/封装层，几乎只承载电源和压焊盘连接，走线场景单一、图形远大于最小线宽，不需要按走线方向细分 pad 形状，各给 1 个经过认证的形状即可。

### 4.2 GENERATE VIARULE：让工具「现场计算」过孔

#### 4.2.1 概念说明

固定 VIA 只有一种固定形状，适合信号线。但电源网络走的是几十倍于最小线宽的宽线，一个 cut 的电流承载不够，需要把很多 cut 排成**过孔阵列**（via array / via stack）。为每种阵列尺寸都预定义一个固定 VIA 显然不现实——于是 LEF 提供 `VIARULE ... GENERATE`：**不直接给形状，而是给「生成规则」，让工具按需现场计算任意大小的过孔**。

一条 GENERATE 规则包含三类信息：

- 两个金属层的 `ENCLOSURE overhang1 overhang2`：cut 之外每侧要被金属包住多少；
- cut 层的 `RECT`：单个 cut 的尺寸；
- cut 层的 `SPACING x BY y`：多个 cut 排阵列时 cut 之间的间距。

#### 4.2.2 核心流程

工具为一条宽线生成过孔阵列的过程（伪代码）：

```text
输入: 需连接的金属线宽 W、方向；VIARULE（cut 尺寸 c、间距 s、包覆 e）
1. 估算需要的 cut 数 k     ← 由允许电流 / DCCURRENTDENSITY 推出
2. 阵列尺寸 = k 个 cut，相邻 cut 中心距 = s
3. 金属 pad = cut 阵列外扩 e（两个方向各自的 overhang）
4. 输出三层矩形，作为一个"现场生成的 via"写入 DEF
```

与固定 VIA 的分工：

| | 固定 `VIA` | `VIARULE GENERATE` |
|---|---|---|
| 形状 | 预先写死，3 或 1 个矩形 | 现场计算，任意大小 |
| 数量 | 本文件 38 个 | 本文件 6 条规则 |
| 典型用途 | 信号线换层 | 电源网、宽线的 cut 阵列 |
| 电阻 | 每个登记了 2.5Ω | 按生成的 cut 个数折算 |

#### 4.2.3 源码精读

[VIARULE MET2_MET1 GENERATE，见 prtech/techLEF/N551P6M.lef:L584-L592](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L584-L592)

这段代码给出 MET1↔MET2 过孔阵列的生成规则：MET1 层的包覆参数是 0.04 和 0，MET2 层是 0.04 和 0.005，单个 VIA1 cut 为 0.09×0.09（`RECT -0.045 -0.045 0.045 0.045`），cut 间间距 `SPACING 0.13 BY 0.13`。工具据此可以在任意宽的 MET1/MET2 重叠区生成 1×1、2×2、3×5……任意规模的 cut 阵列，并把金属 pad 外扩到满足包覆参数。

值得注意的一点：**GENERATE 规则的包覆参数与 4.1 节固定 VIA 的实际包覆并不是一套数**。例如固定 VIA 竖条 pad 的 x 向包覆只有 0.005，小于这里 MET1 的 0.04。这说明两者是独立维护的约束体系——固定 VIA 的形状是工艺厂预先认证的成品（并且 LEF 层定义里 VIA1 本身没有写 `ENCLOSURE` 要求，见 [L76-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L76-L81)，只有 CT 层写了 `ENCLOSURE ABOVE 0.04 0`，见 [L54-L60](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L54-L60)），而 GENERATE 的 `ENCLOSURE` 只约束由该规则**生成**的过孔。两套数值不必一致。

厚金属层的生成规则，pad 明显更「慷慨」：

[VIARULE T4M2_MET5 GENERATE，见 prtech/techLEF/N551P6M.lef:L624-L632](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L624-L632)

这段代码把单个 T4V2 cut 定为 0.36×0.36，cut 间距放大到 1×1，金属包覆参数为 MET5 侧 0.1/0.05、T4M2 侧 0.5/0.02——比普通金属层大一个数量级，因为要通过大电流。对比 4.1 节的固定 `VIA T4M2_MET5`（MET5 pad 0.38×0.46）还能发现：固定 VIA 的 pad 也**不等于**「按本规则最小包覆生成的结果」（0.36 + 2×0.1 = 0.56 > 0.38）。再次印证：固定 VIA 与 GENERATE 规则各管各的。

最后一条规则结构比较特殊，值得停下来看一看：

[VIARULE RDL_T4M2 GENERATE，见 prtech/techLEF/N551P6M.lef:L634-L642](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L634-L642)

这段代码的三个 LAYER 段落是：T4M2 给 `ENCLOSURE`、RV 给 `ENCLOSURE`、RDL 给 `RECT` + `SPACING`。按 LEF 惯例，`RECT`/`SPACING` 应该出现在 cut 层（RV）、`ENCLOSURE` 出现在两个金属层（T4M2、RDL），这里恰好反了过来。它与固定 `VIA RDL_T4M2`（4.1 节）数值上能对上（3×3 的 cut 窗口、1.5 的包覆），但段落角色与常见写法不一致，**其确切语义待确认**——可能是针对 RDL 这种超粗层的特殊约定，也可能是源文件的笔误。读 PDK 源码时遇到这种「不像教科书」的地方，正确做法就是如实存疑，而不是脑补一个解释。

#### 4.2.4 代码实践

**实践目标**：把 `VIARULE MET2_MET1 GENERATE` 翻译成与 4.1.4 同格式的「三层矩形清单」，并对比例外的两个规则。

**操作步骤**：

1. 用 awk 打印整条规则（584–592 行）：

   ```bash
   awk 'NR>=584 && NR<=592' prtech/techLEF/N551P6M.lef
   ```

2. 手工推导「单 cut、按规则生成」的过孔形状：cut 0.09×0.09；MET1 pad = cut + 两侧各 0.04（一个方向）与 0（另一方向）；MET2 pad = cut + 0.04 与 0.005。
3. 与固定 `VIA MET2_MET1_VIA1_0`（0.10×0.17 竖条）比较，记录两者差异。
4. 换到 [L624-L632](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L624-L632) 与 [L634-L642](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L634-L642) 重复第 2 步，观察厚金属规则的数量级变化。

**需要观察的现象**：`ENCLOSURE` 的两个参数如何进入 pad 尺寸；`SPACING` 只在多 cut 阵列时起作用；RDL 规则的段落角色与另外两条不同。

**预期结果**：`MET2_MET1` 规则的单 cut 生成结果约为「cut 0.09×0.09 + 金属 pad 0.17×0.09（MET1，按 0.04/0 方向扩展）与 0.17×0.10（MET2，按 0.04/0.005 扩展）」量级；它与固定 VIA 的 0.10×0.17 / 0.15×0.15 / 0.17×0.10 三种形状**都不相同**，证明两套机制独立。参数到 pad 的精确映射方向（哪个参数对应 x、哪个对应 y）依工具实现而异，**待本地验证**（可用 OpenROAD 生成一个过孔后查看 DEF）。

#### 4.2.5 小练习与答案

**练习 1**：`SPACING 0.13 BY 0.13` 与 VIA1 层定义的 `SPACING 0.11`（[L76-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L76-L81)）是什么关系？

**答案**：层定义的 `SPACING 0.11` 是同层 cut 之间的**工艺最小间距**（DRC 底线）；VIARULE 里的 `0.13` 是**生成阵列时采用的间距**，留了 0.02μm 的制造余量，比 DRC 底线更保守。规则值 ≥ 工艺最小值，这是 PDK 的常见做法。

**练习 2**：如果电源网需要在 MET5 和 T4M2 之间通过 10×10 的 cut 阵列，固定 `VIA T4M2_MET5` 够用吗？

**答案**：不够。固定 `T4M2_MET5` 是单个 cut（0.36×0.36）的预制形状，无法扩展成阵列；应使用 `VIARULE T4M2_MET5 GENERATE`（cut 0.36、间距 1×1、包覆 0.1/0.05 与 0.5/0.02）现场生成 10×10 阵列及对应的大 pad。

**练习 3**：为什么 `VIARULE` 都带 `DEFAULT` 关键字？

**答案**：与固定 VIA 的 `DEFAULT` 含义相同——标记这条生成规则可被布线器作为默认规则直接使用，无需设计者在 DEF/脚本里显式指定（对比：非 DEFAULT 的规则只在被显式引用时生效）。

### 4.3 SITE 与行高：行式布局的地基

#### 4.3.1 概念说明

`SITE`（布局格点/单元占位）定义标准单元的「最小地砖」：宽度是合法单元宽度的最小公因子（所有单元宽度必须是它的整数倍），高度就是**行高**（row height）。布局工具（placer）在 core 区域画出一行行宽度对齐的 site 网格，把每个单元吸附到网格上——这就是**行式布局**。

site 尺寸不是随便定的：**行高必须被布线 pitch 整除**，否则单元上下边界的金属轨道会错位，电源轨（VDD/VSS 横穿每行顶底的 MET1）无法首尾对齐。行高与 MET1 pitch 的比值就是「轨道数」（track count），直接决定单元库里逻辑门的晶体管堆叠高度，是库架构的第一个决策。

#### 4.3.2 核心流程

布局工具使用 site 的过程：

```text
1. 从 tech LEF 读入 SITE 定义（宽 w、高 h）
2. 在 core 区域按 h 为行距、w 为列距生成 site 网格，逐行翻转（N 行 / FS 行交替，
   使相邻行的 VDD 轨贴 VDD 轨、VSS 贴 VSS）
3. 放置单元时，单元原点吸附到 site 网格点（宽度必须是 w 的整数倍）
4. 每行顶部/底部自动生成横穿全行的电源轨
```

轨道数公式：

\[ N_{\text{track}} = \frac{H_{\text{site}}}{P_{\text{MET1}}} \]

代入本 PDK 的两个 site（MET1 `PITCH 0.2`，见 [L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74)）：

| SITE | 尺寸（μm） | \( N_{\text{track}} = H / 0.2 \) | 行业叫法 |
|---|---|---|---|
| CoreSite | 0.2 × 1.4 | 7 | 7 轨 |
| core7 | 0.200 × 1.400 | 7 | 7 轨 |
| core9 | 0.200 × 1.800 | 9 | 9 轨 |

`core7` / `core9` 的名字就从轨道数来（这是从尺寸反推的命名含义，与业界 7-track / 9-track 库的通行叫法一致）。7 轨库矮、面积小、速度略慢；9 轨库高、驱动强、适合高性能路径。同一工艺同时提供两种行高的库是常见配置。

#### 4.3.3 源码精读

三个 SITE 定义挤在文件末尾：

[SITE CoreSite，见 prtech/techLEF/N551P6M.lef:L654-L658](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L654-L658)

这段代码定义了名为 CoreSite 的 site：尺寸 0.2×1.4，`SYMMETRY Y` 表示允许上下翻转后放入行中，`CLASS CORE` 说明它属于 core（标准单元）区域而非 pad 区域。

[SITE core7，见 prtech/techLEF/N551P6M.lef:L660-L664](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L660-L664) 与 [SITE core9，见 prtech/techLEF/N551P6M.lef:L666-L670](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L666-L670)

这两段定义了 7 轨的 core7（0.200×1.400，参数与 CoreSite 完全等价，只是名字不同）和 9 轨的 core9（0.200×1.800，比 core7 高 0.4μm，即多 2 条 MET1 轨道）。

那么标准单元到底用哪个？看单元 LEF 自己声明的：

[MACRO ANT2H7H 头部，见 IP/STD_cell/.../ics55_LLSC_H7CH.lef:L3048-L3054](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048-L3054)

这段代码是天线二极管单元的宏头部：`SIZE 0.4 BY 1.4` 说明它高 1.4μm（= core7 行高）、宽 0.4μm（= 2 个 site 宽），`SITE core7` 显式声明吸附到 core7 格点。用 `grep -c 'SITE core7 ;'` 统计可知该文件 785 个 MACRO **每一个**都写着 `SITE core7`——**本 PDK 的三套标准单元库（H7CH/H7CL/H7CR）用的都是 core7（7 轨、1.4μm 行高）**；而 `CoreSite` 和 `core9` 在当前 git 跟踪的所有 cell LEF 中均未被引用（已用 grep 全仓验证），属于预留或兼容性定义，用途待确认。

SITE 之前还有一个库级定义：

[NONDEFAULTRULE DefaultTaper，见 prtech/techLEF/N551P6M.lef:L644-L652](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L644-L652)

`NONDEFAULTRULE` 定义一组「非默认布线规则」：当某条网线需要特殊处理（这里是名为 DefaultTaper 的「锥形/渐变」规则）时引用它。这段代码规定 POLY 层线宽 0.06、MET1 层线宽 0.09，并通过 `USEVIARULE MET1_POLY` 指定配套过孔规则。有两个值得注意的观察：其一，`MET1_POLY` 这条 VIARULE 在本文件中**并未定义**（全部 6 条 GENERATE 规则里没有它），属于悬空引用，可能依赖外部文件或为遗留问题，**待确认**；其二，`_ecos` 版 tech LEF（`N551P6M_ecos.lef`）把整个 `NONDEFAULTRULE DefaultTaper` 段删掉了，而 VIA / VIARULE / SITE 与普通版完全一致——开源工具链对这条规则既用不上也解析不好，这是 u2-l3 的伏笔。

最后，[END LIBRARY，见 prtech/techLEF/N551P6M.lef:L672](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L672) 收束整个文件。至此 tech LEF 的全部构件——层、过孔、生成规则、非默认规则、site——都读完了。

#### 4.3.4 代码实践

**实践目标**：从 SITE 定义推导 core7 与 core9 的行高差异，并结合标准单元 `SIZE` 验证本 PDK 实际使用哪个 site。

**操作步骤**：

1. 打印三个 SITE 定义，观察字段：

   ```bash
   awk 'NR>=654 && NR<=670' prtech/techLEF/N551P6M.lef
   ```

2. 读 MET1 的 pitch（65 行附近），计算两个行高的轨道数：`1.4/0.2` 与 `1.8/0.2`。

3. 验证标准单元的 site 引用（H7CH 普通版、ecos 版、ant 版三个文件都数一遍）：

   ```bash
   grep -c 'SITE core7 ;' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/*.lef
   ```

4. 再反向确认没有单元引用另外两个 site：

   ```bash
   grep -rl 'SITE core9\|CoreSite' --include='*.lef' .
   ```

5. 任选一个单元（如 [ANT2H7H:L3048-L3054](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048-L3054)），用宽 ÷ 0.2、高 ÷ 1.4 算出它占几个 site。

**需要观察的现象**：第 3 步三个 LEF 各报 785；第 4 步只命中两个 tech LEF 自身（定义处），无任何 cell LEF 引用。

**预期结果**：core7 = 7 轨（1.4μm），core9 = 9 轨（1.8μm），相差 2 条 MET1 轨道；本 PDK 全部标准单元声明 `SITE core7`，即采用 7 轨 1.4μm 行高；ANT2H7H 尺寸 0.4×1.4 = 2 site 宽 × 1 site 高。以上均已通过 grep / 人工读源确认。

#### 4.3.5 小练习与答案

**练习 1**：core7 与 CoreSite 参数完全相同，为什么文件里要定义两个？

**答案**：从本仓库现状看不出必然原因——所有 cell LEF 只引用 core7。合理的推测是兼容不同来源的宏或历史遗留（例如早期版本/其他工具链使用 CoreSite 这个名字），其真实原因**待确认**。读 PDK 时应记住：名字不同的两个等价 site 定义，引用哪一个由 cell LEF 的 `SITE` 语句决定。

**练习 2**：一个 `SIZE 3.6 BY 1.4` 的宽单元（如大驱动 buffer）占多少个 site？如果换到 core9 行高，它的 `SIZE` 第二个数字必须改成多少？

**答案**：宽度 3.6 ÷ 0.2 = 18 个 site 宽、1 个 site 高；换到 core9 后高度必须是 1.8（单元高度恒等于行高，且晶体管堆叠要重新设计为 9 轨——这不是改数字就行，而是另一个库）。

**练习 3**：为什么行高必须等于 MET1 pitch 的整数倍？

**答案**：电源轨用 MET1 横穿每行，单元引脚也落在 MET1 水平轨道上。若行高不是 pitch 整数倍，相邻行的轨道无法对齐拼接，贯穿全行的电源轨和引脚网格就会错位，布线轨道无法连续分配。

## 5. 综合实践

**任务：写一个 30 行左右的「tech LEF 结构审计器」，给 N551P6M.lef 出一份体检报告。**

把本讲三个模块串起来，脚本需要输出四张表并做三项断言（**示例代码**框架）：

```python
# 示例代码：tech LEF 结构审计器
import re
src = open("prtech/techLEF/N551P6M.lef").read()

# ① 层清单：按 TYPE 分组计数
layers = re.findall(r"LAYER (\S+)\s*\n\s*TYPE (\w+)", src)

# ② 固定 VIA：按层对分组计数，提取每个的三层矩形
vias = re.findall(r"VIA (\S+) DEFAULT\s*(.*?)\nEND \1", src, re.S)

# ③ VIARULE：提取每条规则的 cut 层与 SPACING
rules = re.findall(r"VIARULE (\S+) GENERATE DEFAULT\s*(.*?)\nEND \1", src, re.S)

# ④ SITE：名字 -> (宽, 高)，计算轨道数 = 高 / 0.2
sites = re.findall(r"SITE (\S+)\s*\n\s*SIZE ([\d.]+) BY ([\d.]+)", src)

# 断言 1：固定 VIA 共 38 个，且 4 个普通层对各 9 个变体
# 断言 2：所有 VIA*_n 的 cut 尺寸等于对应 CUT 层的 WIDTH
# 断言 3：每个 SITE 高度 / MET1 pitch(0.2) 为整数（7 或 9）
```

完成后回答三个问题：

1. 4 组普通层对的 36 个变体是否全部满足 3×3 编码规律（把断言 2 扩展到 VIA2/VIA3/VIA4）？
2. `RDL_T4M2` 的金属包覆验算（(6−3)/2 = 1.5）是否与 RV 层的 `ENCLOSURE 1.5 1.5` 精确一致？`T4M2_MET5` 呢？
3. 用 `grep -c 'SITE core7 ;'` 对 H7CH / H7CL / H7CR 三套 cell LEF 各数一遍，三个数字是否都等于各自的 MACRO 数？

**预期结果**：普通版与 `_ecos` 版 tech LEF 在 VIA / VIARULE / SITE 上应得到**完全相同**的审计报告，唯一差异是 `_ecos` 版没有 `NONDEFAULTRULE`——用 `diff` 两个文件的审计输出即可直观看到。数量核对结论（38 个 VIA、6 条 VIARULE、3 个 SITE、785 个 `SITE core7` 引用）已人工验证；脚本运行输出**待本地验证**。

## 6. 本讲小结

- 一个固定 `VIA` = 下层 pad + cut + 上层 pad 三层矩形，本文件共 38 个：4 个普通金属层对各 9 个变体，加 `T4M2_MET5`、`RDL_T4M2` 各 1 个。
- 9 个变体来自 pad 的 3 种形状（竖条 0.10×0.17 / 方块 0.15×0.15 / 横条 0.17×0.10）的 3×3 组合，编号规律是 \( n = 3 \times \text{上层形状} + \text{下层形状} \)；cut 恒为 0.09×0.09，由 CUT 层 `WIDTH` 决定。
- `VIARULE ... GENERATE` 不给形状而给生成参数（金属包覆 + 单 cut 尺寸 + cut 间距），供工具为电源网/宽线现场生成任意规模的 cut 阵列；其 `ENCLOSURE` 只约束生成物，与固定 VIA 的实际包覆是两套独立数值。
- `SITE` 是行式布局的最小格点：宽度 0.2μm 是单元合法宽度的最小增量，高度即行高。core7（1.4μm = 7 条 MET1 轨道）被全部 785 个标准单元引用；core9（1.8μm = 9 轨）与 CoreSite 当前无引用。
- `NONDEFAULTRULE DefaultTaper` 是库尾的非默认布线规则，其 `USEVIARULE MET1_POLY` 在本文件中悬空未定义；`_ecos` 版把它整段删除，其余 VIA/VIARULE/SITE 与普通版完全一致。
- `RDL_T4M2` 提供了全文件最干净的验算样本：6×6 金属 pad 包 3×3 cut，单边包覆恰为 1.5，与 RV 层 `ENCLOSURE BELOW/ABOVE 1.5 1.5` 精确咬合。

## 7. 下一步学习建议

下一讲（u2-l3）把 `N551P6M.lef` 与 `N551P6M_ecos.lef` 做整体 diff，看开源适配版除了删掉 `DefaultTaper` 之外，还给每个 ROUTING 层补了哪些 `CAPACITANCE` / `EDGECAPACITANCE`、把 `OFFSET` 从 0 改成了多少——那是本讲反复提到的「电阻有了、电容还缺」的补齐现场。

想先动手的读者，可以带着本讲的知识去翻 u3-l2 将精读的单元 LEF：随便挑一个 [ics55_LLSC_H7CH.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef) 里的 MACRO，检查它的 `SIZE` 宽度是否都是 0.2 的整数倍、引脚 `RECT` 是否落在 0.2 的轨道上——把 tech LEF 的格点规则和 cell LEF 的实际图形互相印证，是理解 PDK 一致性的最快路径。
