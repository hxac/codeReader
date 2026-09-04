# u4-l1 IO 单元家族盘点

## 1. 本讲目标

学完本讲，你应该能够：

1. 把 IO 库的 23 个单元按「信号 pad / 电源 pad / 填充 / 拐角 / 切割」五大类准确分类，并说出每一类在 pad ring（压焊盘环）里的角色。
2. 解释 FILLER 系列九种宽度（0.005 ~ 50 μm）的编码规律，以及它们如何像「找零钱」一样贪心递补 pad 之间的间隙。
3. 区分 CORNER、CUT 这类纯结构单元与 PAR、PBMUX、PWE 这类有晶体管的电学 pad——前者不在 liberty 里，后者在。
4. 独立完成一个假想 40 引脚芯片的 pad 环清单，让每条边的几何精确闭合。

本讲只做「盘点与分类」：每个单元长什么样、有多大、有什么引脚。IO LEF 的 SITE 定义、`_ecos` 版适配留给 u4-l2，liberty 电学参数与 CDL 电路细节留给 u4-l3。

## 2. 前置知识

### 2.1 芯片边界与压焊盘（bond pad）

标准单元（u3 系列）住在芯片中间的 core 区，按行摆放；而芯片与外界的电气连接只能通过四周一圈 **IO pad**。每个 pad 顶部有一块金属方块，封装时用金属线把它和引脚框架（或基板）焊起来，这个过程叫 bonding（压焊）。所以「IO 单元」本质上是：一块可压焊的金属板 + 把信号/电源在 3.3V IO 域和 1.2V 核域之间搬运的电路 + ESD 保护器件。

### 2.2 pad ring 是怎么拼起来的

芯片最外圈不是随意摆放，而是一圈首尾相接的「积木」：

- 四个角放 **CORNER**（拐角单元），让环拐弯；
- 每条边上按 65 μm 的基本宽度铺 pad；
- 摆不满的零头用 **FILLER**（填充单元）补齐——就像用不同面值的硬币凑出任意金额；
- 需要隔断或收尾时插入 **CUT**（切割单元）。

### 2.3 双电压域：1.2 V 与 3.3 V

回顾 u1-l2：本 IO 库目录名为 `ICsprout_55LLULP1233_IO`，其中 `1233` 对应核 1.2 V / IO 3.3 V 双电压域（可由 liberty 文件名 `tt_1p2_3p3_25c` 中 `1p2`/`3p3` 印证）。库文件名 `ICSIOA_N55_3P3_...` 的 `3P3` 即 3.3 V。LEF 文件名后缀 `1P6M1TM` 推断为「1 层多晶硅 + 6 层金属 + 1 层厚金属」，与 u2-l1 讲过的金属栈 MET1–MET5 + T4M2（+封装 RDL）对应（推断，待确认）。

### 2.4 你已经知道、本讲直接复用的结论

- u1-l2：IO 库有 23 个单元；每库视图子目录为 cell_list / lef / liberty / verilog / cdl / gds / doc。
- u2-l2：标准单元用 SITE `core7`（0.2×1.4 μm）做行式布局；IO 单元不用行，而用本讲的 IOSite。
- u3-l2：LEF 的 MACRO 用 `SIZE 宽 BY 高`、`PIN → PORT → LAYER → RECT` 描述物理抽象。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| `IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt` | 23 个单元的官方名单 | 分类总表 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef` | 63,197 行的 IO 物理抽象 | 提取 SIZE、引脚、FILLER 宽度 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef` | 开源工具适配版 | 只看它新增的 SITE 定义（L19-29） |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib` | 时序/功能库 | 判断「哪些单元有电学模型」+ 引脚方向与功能表达式 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl` | 晶体管级网表 | 佐证 PAR 电阻结构与 PWE 振荡器结构 |

注意一个重要事实：IO 库的 6 个 liberty 因为体积小而留在 git 内（u1-l3），所以本讲可以直接引用，不需要 `make unzip`。

## 4. 核心概念与源码讲解

### 4.1 pad 类型分类：23 个单元的五类划分

#### 4.1.1 概念说明

「IO 库」听起来像一个统一的家族，其实混居着五种身份截然不同的成员：

| 类别 | 判据 | 在 liberty 里？ | 举例 |
|---|---|---|---|
| 信号 pad | 有信号引脚（PAD + 核侧数据/控制） | 有 | PBMUX、PWE、PAR |
| 电源 pad | 只连接电源/地 | 有（8 个） | VDD1、VSSIO3 等 |
| 填充单元 | 名字以 FILLER 开头，无信号 | 无 | FILLER50 ~ FILLER0005 |
| 拐角单元 | 名字含 CORNER | 无 | P65_1233_CORNER |
| 切割单元 | 名字含 CUT | 无 | P65_1233_CUT |

「在不在 liberty 里」是一条硬判据：liberty 描述时序与功能，纯几何积木（拐角、切割、填充）没有电路行为，自然不需要时序模型。打开 liberty 数一数 `cell (` 语句，恰好 12 个：PAR、PAR_5、PBMUX、PWE + 8 个电源 pad——与 23 − 11 = 12 吻合。

#### 4.1.2 核心流程

给一个陌生 IO 单元分类的流程：

```text
读 cell_list 拿到名字
    │
    ├─ 名字含 CORNER / CUT / FILLER ──→ 结构类（跳过 liberty）
    │        └─ FILLER 后缀数字 → 填充；CORNER → 拐角；CUT → 切割
    │
    └─ 其余 ──→ 查 liberty 是否存在该 cell
              ├─ 存在且信号引脚只有电源 → 电源 pad
              └─ 存在且有 PAD + 数据/控制脚 → 信号 pad
```

信号 pad 内部再细分角色，依据是引脚清单（LEF）+ 方向与功能表达式（liberty）：

- **PBMUX**：全功能双向 GPIO（输出使能、输入使能、上拉/下拉、驱动强度可选……16 个引脚）；
- **PWE**：晶振 pad，带 XIN/XOUT 两个压焊端子，所以宽度是别人的两倍；
- **PAR / PAR_5**：只有「串联电阻 + ESD」的极简 pad，两个挡位。

#### 4.1.3 源码精读

**第一步：23 个单元的官方名单。** [ICSIOA_N55_3P3.txt:L1-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt#L1-L23) 每行一个单元名。前 14 行是有电路的成员（CORNER/CUT 除外），后 9 行全是 FILLER——注意实际是 **9 种**填充，不是 8 种：`FILLER50/20/10/5/2/1/01/001/0005`。

**第二步：用 liberty 划出「电学/结构」分界线。**
[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L141-L148](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L141-L148) 是第一个 cell（PAR），关键字段 `pad_cell : true` 标记这是压焊盘单元，`area : 8450` 恰好等于 65×130（LEF 的 SIZE 宽×高），两个视图可以互相验算。[L816-L819](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L816-L819) 的 PWE `area : 16900` = 130×130，同样吻合。而 CORNER/CUT/FILLER 在整个 liberty 中查无此 cell。

**第三步：PBMUX——唯一的全功能双向 pad。**
LEF 里它的宏头是 [ICSIOA_N55_3P3_1P6M1TM.lef:L54887-L54892](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L54887-L54892)：`CLASS PAD`、`SIZE 65 BY 130`。它有 16 个引脚，压焊侧的 PAD 脚画在厚金属 T4M2 上：[L55256-L55261](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L55256-L55261)——厚金属是真正的可压焊层（u2-l1 讲过 T4M2 是低阻大电流资源）。

各控制脚的语义要以 liberty 为准（LEF 里信号脚一律写 `DIRECTION INPUT`，信息不全）：

- [liberty:L252-L258](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L252-L258)：`pin (PAD)` 是 `inout`、`is_pad : true`，`function : "I"`、`three_state : "(!OE)"`——PAD 作为输出时驱动核侧数据 I，OE 无效时高阻；
- [liberty:L645-L647](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L645-L647)：`pin (C)` 是 `output`，`function : "(PAD&IE)"`——C 是送进核的接收数据，被输入使能 IE 门控。

由此得到 PBMUX 的双向结构：发送路径 I → PAD（受 OE 控制），接收路径 PAD → C（受 IE 控制），再加上 [L54894-L54909](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L54894-L54909) 起的 A、CS、DS0/DS1、OD、PD、PU 等引脚。按业界通用命名：PU/PD 是上拉/下拉选择，DS0/DS1 是驱动强度挡位，OD 是开漏，IE/OE 是输入/输出使能（推断，精确语义以 doc 目录下的 datasheet PDF 为准，该 PDF 本环境无法解析，待确认）。

**第四步：PWE——双倍宽的晶振 pad。**
[L55559-L55564](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L55559-L55564)：`SIZE 130 BY 130`，占两个普通 pad 位。为什么？它要同时压焊晶振的两个端子：

- [liberty:L827-L838](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L827-L838)：`XIN` 为 `input`、`XOUT` 为 `output`，且都带 `is_pad : true`——两个都是压焊端子，正是皮尔斯晶振的一对驱动/接收脚；
- [liberty:L956-L958](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L956-L958)：`XC` 为 `output`，`function : "(E&XIN)"`——使能 E 有效时，核拿到 XIN 缓冲后的时钟。

CDL 进一步给出了内部结构：[ICSIOA_N55_3P3.cdl:L535-L545](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L535-L545) 显示 PWE 由三个子电路拼成——`PWE_lever_shift`（电平位移，把 1.2 V 核域的 E 抬到 3.3 V IO 域）、`PWE_shimit`（施密特整形）、`PWE_nand`（含 W=520μ/440μ 巨型管的振荡驱动）。「PWE」全称推断与晶振/使能相关（待确认），但「晶振 pad」这个功能定位由 XIN/XOUT + is_pad + 巨型管三重证据支撑。

**第五步：PAR / PAR_5——带电阻挡位的极简 pad。**
LEF 中 [L53569-L53574](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53569-L53574) 定义 PAR，引脚只有 A、PAD 加四个模拟电源脚。CDL 一句话道破天机：[L164-L172](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L164-L172) 中 `X2 PAD A re_ppo_2t W=8u L=3.34u` 就是 PAD 与 A 之间的串联电阻，M0/M1/DD0 是 ESD 保护管和钳位二极管。对比 [L178-L186](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L178-L186) 的 PAR_5：`X2 PAD A re_ppo_sab_2t W=78u L=400n`。电阻阻值正比于长宽比：

\[ R = R_s \cdot \frac{L}{W}, \quad \frac{(L/W)_{\mathrm{PAR}}}{(L/W)_{\mathrm{PAR\_5}}} = \frac{3.34/8}{0.4/78} \approx 81 \]

即 PAR 的串联电阻约为 PAR_5 的 81 倍，`_5` 推断指约 5 Ω 的低阻挡位（精确阻值需 SPICE 模型，本 PDK 尚未提供，待确认）。这类 pad 用于需要限流、端接或模拟输入的场合。

另外留意 [L53576-L53581](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53576-L53581)：PAR 的 A 脚带 `ANTENNAPARTIALCUTAREA` 注记——这是 u3-l4 讲过的天线效应属性，IO LEF 全文件共 165 条，是标准单元库（仅 2 条）的完整得多的样例。

#### 4.1.4 代码实践

**实践目标**：用脚本自动完成「23 个单元 × 五类」分类，不靠手工数。

**操作步骤**（示例代码，保存为 `classify_io.py`，在仓库根目录运行 `python3 classify_io.py`）：

```python
import re, pathlib
root = pathlib.Path("IP/IO/ICsprout_55LLULP1233_IO_251013")

cells = [l.strip() for l in
         (root / "cell_list/ICSIOA_N55_3P3.txt").read_text().splitlines() if l.strip()]

lef = (root / "lef/ICSIOA_N55_3P3_1P6M1TM.lef").read_text().splitlines()
lib = (root / "liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib").read_text()

# 逐 MACRO 收集 SIZE 与信号引脚（排除电源名）
macro = None; size = {}; pins = {}
for line in lef:
    if m := re.match(r"MACRO (\S+)", line):
        macro = m.group(1)
    elif macro and (m := re.match(r"  SIZE (\S+) BY (\S+)", line)):
        size[macro] = (float(m.group(1)), float(m.group(2)))
    elif macro and (m := re.match(r"  PIN (\S+)", line)):
        pins.setdefault(macro, []).append(m.group(1))

POWER = {"VDD", "VSS", "VDDA", "VSSA", "VDDIO", "VSSIO", "VDD1", "VSS1", "VDDA1"}
def classify(c):
    if "CORNER" in c: return "拐角"
    if "CUT" in c:    return "切割"
    if "FILLER" in c: return "填充"
    sig = [p for p in pins.get(c, []) if p not in POWER]
    if f'("{c}")' not in lib: return "结构(无liberty)"
    return "信号pad" if sig else "电源pad"

for c in cells:
    w, h = size[c]
    print(f"{c:22s} {w:8.3f}x{h:.0f}  {classify(c):8s} pins={pins.get(c)}")
```

**需要观察的现象**：
1. 输出表中 `classify` 列只出现「拐角/切割/填充/信号pad/电源pad」五种值；
2. FILLER0005 的 `pins` 是空列表 `[]`，而 FILLER001 起都有 4 个电源脚；
3. 只有 PAR/PAR_5/PBMUX/PWE 四行的 `pins` 里出现 PAD 及数据/控制脚。

**预期结果**（关键数值已人工核对源码）：23 行输出；信号 pad 4 个（PAR、PAR_5、PBMUX、PWE）、电源 pad 8 个、填充 9 个、拐角 1 个、切割 1 个，合计 23。若你的正则没匹配到，注意 LEF 的语句是两个空格缩进（`  SIZE`、`  PIN`）。脚本运行输出整体待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CORNER、CUT、FILLER 不在 liberty 里，而 PAD 类都在？
**答案**：liberty 的职责是给综合和时序分析提供功能、电容、时序模型。拐角/切割/填充单元内部没有信号通路（FILLER0005 甚至一个引脚都没有），不参与逻辑与时序；而 12 个电学 pad 有驱动、接收、上拉等电路行为，必须建模。这也解释了综合工具只看 liberty 时「看不见」结构单元——它们属于纯物理实现阶段。

**练习 2**：只看 LEF，怎么最快把 PWE 从 23 个单元里挑出来？
**答案**：看 `SIZE`。22 个单元的宽度是 65（或更小的填充宽度）、高度一律 130；唯独 PWE 是 `SIZE 130 BY 130`（[L55563](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L55563)），因为它要为晶振提供 XIN/XOUT 两个压焊端子，占两个 pad 位。

**练习 3**：PAR 和 PAR_5 的 liberty 引脚电容几乎相同（约 2.7 pF），这说明什么？
**答案**：[liberty:L145-L151 与 L164-L170](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L145-L170) 两处 PAD 脚电容都在 2.7 pF 量级，二者对外的负载模型一致，差别只在内部串联电阻的挡位——对综合器而言它们可互换，对电路设计者而言是两个阻值选择。

### 4.2 FILLER 宽度系列

#### 4.2.1 概念说明

pad ring 要求一圈**无缝闭合**：每个 pad 宽 65 μm，但芯片边长减去两个拐角后剩下的长度往往不是 65 的整数倍；而且不同边要摆的信号/电源 pad 数也不同。零头间隙必须用**填充单元**补上，否则断开的电源轨道无法环绕供电，DRC 也会报错。

FILLER 的设计哲学和「找零钱」一模一样：提供一套面值，让任意金额都能凑出。本库的面值表（宽，单位 μm）：

| 单元 | 宽度 | SIZE 所在行 |
|---|---|---|
| P65_1233_FILLER50 | 50 | [L53427](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53427) |
| P65_1233_FILLER20 | 20 | [L53135](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53135) |
| P65_1233_FILLER10 | 10 | [L52845](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52845) |
| P65_1233_FILLER5 | 5 | [L53281](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53281) |
| P65_1233_FILLER2 | 2 | [L52991](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52991) |
| P65_1233_FILLER1 | 1 | [L52707](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52707) |
| P65_1233_FILLER01 | 0.1 | [L52573](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52573) |
| P65_1233_FILLER001 | 0.01 | [L52487](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52487) |
| P65_1233_FILLER0005 | 0.005 | [L52478](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52478) |

命名编码规律：`FILLER` 后的数字串按「缺省小数点」读——`01` = 0.1、`001` = 0.01、`0005` = 0.005。**注意是大纲修正：本库实际有 9 种宽度，而非 8 种。**

#### 4.2.2 核心流程

给定间隙宽度 \( g \)（μms），贪心从大到小递补：

```text
面值 D = [50, 20, 10, 5, 2, 1, 0.1, 0.01, 0.005]（降序）
for d in D:
    取 n = floor(g / d) 个 FILLER(d)；g ← g − n·d
若最终 g == 0：闭合成功
```

因为最小面值 0.005 μm 恰好等于 IOSite 的 site 宽度（见 4.2.3），**凡是被 0.005 整除的间隙都能精确闭合**。举例：

\[ 740 - 11 \times 65 = 25 = 20 + 5 \]

即一边摆 11 个 pad 后剩 25 μm，用 FILLER20 + FILLER5 补齐。再如非整数间隙：

\[ 12.345 = 10 + 2 + 3 \times 0.1 + 4 \times 0.01 + 1 \times 0.005 \]

这就是亚微米三兄弟（0.1/0.01/0.005）存在的意义：处理金属层错位、非整数边长等「零头的零头」。

#### 4.2.3 源码精读

**最特殊的 FILLER0005——全库唯一没有引脚的单元。**
[ICSIOA_N55_3P3_1P6M1TM.lef:L52474-L52481](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52474-L52481) 整个宏只有 8 行：`MACRO / CLASS PAD / ORIGIN / FOREIGN / SIZE 0.005 BY 130 / SYMMETRY / SITE / END`，没有任何 PIN。0.005 μm = 5 nm，只等于 5 个制造网格（u2-l1 讲过 MANUFACTURINGGRID 0.001）——它不是为了「补 5 nm 的缝」，而是作为与 site 同宽的**几何量子**存在。

**其余 FILLER 都是「电源延续器」。**
以 FILLER001 为例，宏头 [L52483-L52489](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52483-L52489) 之后紧跟四个电源脚，其中 VDD 脚 [L52490-L52504](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52490-L52504) 写明 `DIRECTION INOUT ; USE POWER`，矩形从 x=0 铺到 x=0.01——正好贯穿自身宽度。FILLER 的使命因此清晰：**自身不带电路，但把左右邻居的 VDD/VDDIO/VSS/VSSIO 四条轨道接续起来**，让电源环闭环。

**site 量子与 _ecos 版的定义。**
本 LEF 的 23 个宏全都写着 `SITE IOSite ;`（全文件 23 处，如 [L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L25)、[L52480](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52480)），但**本文件并没有定义 IOSite**。定义在 `_ecos` 版里：[ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L19-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L19-L29) 给出 `SITE IOSite` 尺寸 `0.005 BY 130`、`CLASS pad`，以及拐角用的 `IOCorner`（130×130）。IOSite 宽 0.005 与 FILLER0005 宽 0.005 互相印证：**pad 环上的摆放格点是 5 nm**，凡间隙是 0.005 的整数倍即可闭合。SITE 的详细语义与 `_ecos` 适配动机是 u4-l2 的主题。

顺带一提：u2-l2 的 core7 site 宽 0.2 μm 管核区行布局，IOSite 宽 0.005 μm 管 pad 环——两个网格互不相干，这正是 PDK 里「核区」与「环区」物理分属两个世界的一个体现。

#### 4.2.4 代码实践

**实践目标**：从 LEF 自动提取 9 个 FILLER 宽度、排序，并实现贪心闭合函数。

**操作步骤**（示例代码，接在 4.1.4 的解析结果之后）：

```python
fillers = {c: size[c][0] for c in cells if "FILLER" in c}
for c, w in sorted(fillers.items(), key=lambda kv: -kv[1]):
    print(f"{c:22s} {w:8.3f}")

# 名字后缀 → 宽度的编码验证：数字串补小数点
for c, w in fillers.items():
    digits = c.replace("P65_1233_FILLER", "")
    decoded = int(digits) / 10 ** (len(digits) - 1) if len(digits) > 1 else int(digits)
    assert abs(decoded - w) < 1e-9, (c, decoded, w)

def fill(gap):                       # 贪心找零
    used = []
    for c, w in sorted(fillers.items(), key=lambda kv: -kv[1]):
        n, gap = divmod(round(gap * 1000), round(w * 1000))
        gap /= 1000
        used += [c] * n
    return used, gap

print(fill(25))        # 期望: (['FILLER20', 'FILLER5'], 0.0)
print(fill(12.345))    # 期望: ([...'FILLER01'x3, 'FILLER001'x4, 'FILLER0005'x1], 0.0)
```

**需要观察的现象**：
1. 排序后宽度序列为 50、20、10、5、2、1、0.1、0.01、0.005——十进制「1-2-5」体系（每十年内三个面值），与电阻标称值同一思路；
2. 名字解码断言全部通过，说明命名即宽度；
3. `fill()` 对 25 和 12.345 都返回余数 0.0。

**预期结果**：9 行宽度表 + 两组闭合解。25 = FILLER20+FILLER5；12.345 = FILLER10+FILLER2+FILLER01×3+FILLER001×4+FILLER0005×1。以上数值已人工核对；脚本实际输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么面值选 1-2-5 体系，而不是 1-2-4-8（二进制）或 10 的幂？
**答案**：1-2-5 体系在每个十进制约数内用最少的面值覆盖最多组合（任何 1~9 的整数至多 3 枚：如 8=5+2+1），工程上「单位面积内的种类数」与「拼凑枚数」折中最好；二进制 1-2-4-8 拼某些值要更多枚（如 7=4+2+1 也是 3 枚，但 9 需 8+1 两枚而 5+2+1+1 需 4 枚——总体上 1-2-5 在十进制间隙下平均枚数更少），且与 65 μm pad 间距的十进制余数更贴合。

**练习 2**：FILLER0005 宽 5 nm，比一条真实金属线还窄，为什么还要留？
**答案**：它与 IOSite 的 site 宽度（0.005 μm）相等，是摆放格点的「单位量子」。当间隙不是 0.01 的整数倍而是 0.005 的奇数倍时（例如 12.345），没有它就无法闭合；同时它也是面积趋零的占位符，DRC 上比「留缝」安全。

**练习 3**：一个间隙恰好 65 μm，你放 FILLER50+FILLER10+FILLER5，别人放一个普通信号 pad，哪个对？
**答案**：都对几何闭合，但语义不同。若该位置不需要任何电气连接，放填充即可；若电源环在此处电流瓶颈明显，工程师也可能刻意放一个电源 pad 增强供电。FILLER 只延续轨道，不提供新的压焊或供电能力——选哪种取决于供电/信号规划，这正是 pad ring 规划（floorplan）的决策内容。

### 4.3 电源 pad 与拐角/切割单元

#### 4.3.1 概念说明

**电源 pad 家族（8 个）**：芯片的电流必须从外面进来。核域（1.2 V）和 IO 域（3.3 V）各自需要正负两个电极，部分场景还要独立的模拟电源，于是有：

| 单元 | 域 | 功能（推断自命名与引脚） |
|---|---|---|
| VDD1 / VSS1 | 1.2 V 核 | 核电源 / 核地 |
| VDD1A / VSS1A | 1.2 V 模拟 | 模拟核电源 / 模拟核地（引脚名 VDDA1/VSSA） |
| VDD3 / VSS3 | 3.3 V IO | IO 域电源 / 地（引脚为 VDD/VSS） |
| VDDIO3 / VSSIO3 | 3.3 V IO | IO 供电总线电源 / 地（引脚为 VDDIO/VSSIO） |

（VDD3 与 VDDIO3 的确切分工需 datasheet 确认，待确认；u4-l3 会用 liberty 参数再比对。）

**拐角单元 CORNER**：pad 环是矩形，四条边交接处需要一个 130×130 的方形单元转 90° 弯，同时把四条边的电源轨道焊在一起。

**切割单元 CUT**：宽度仍是 65×130，但引脚里多了 VDDA/VSSA。用于在环上隔断一段电源域或收尾（推断，精确用法待确认）。

#### 4.3.2 核心流程

拼一个完整 pad ring 的步骤：

```text
1. 由封装/裸片尺寸定边长 L
2. 四角各放 1 个 CORNER（130×130）
3. 每边可用长度 U = L − 2×130
4. 按信号表分配 PBMUX/PWE/电源 pad（宽 65，PWE 占 2 位）
5. 剩余间隙 g = U − Σ已用宽度，用 FILLER 贪心闭合（4.2.2）
6. 核对电源轨道连续性：数字域单元带 VDD/VSS/VDDIO/VSSIO，模拟域单元（PAR/PAR_5/VDD1A/VSS1A）带 VDD/VSS/VDDA/VSSA
```

#### 4.3.3 源码精读

**CORNER：占了全文件 82% 行数的「大块头」。**
[ICSIOA_N55_3P3_1P6M1TM.lef:L19-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L19-L25) 是它的宏头：`SIZE 130 BY 130`、`SYMMETRY X Y R90`（允许旋转 90°，一个单元服务四个角）、`SITE IOSite`。它只有 4 个引脚：VDD（[L26](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L26)）、VDDIO（[L539](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L539)）、VSS（[L12091](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L12091)）、VSSIO（[L12976](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L12976)）——纯电源，无信号。这个宏从 L19 一直延伸到 L51583，约 5.16 万行、占全文件 82%：因为它的电源形状要在 MET2~MET5 上画大量阶梯状矩形（如 [L26-L60](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L26-L60) 里一层层微调的 RECT），把两条边的轨道圆滑转接。这也是为什么 IO LEF 有 6.3 万行而标准单元 LEF 只有它的几分之一。

**CUT：带模拟轨道的切割单元。**
[L51585-L51591](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51585-L51591) 宏头 `SIZE 65 BY 130`，引脚 6 个：VDD（[L51592](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51592)）、VDDA（[L51605](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51605)）、VDDIO（[L51710](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51710)）、VSS（[L51815](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51815)）、VSSA（[L51829](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51829)）、VSSIO（[L51912](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51912)）。比 CORNER 多出 VDDA/VSSA 两条模拟轨道。

**8 个电源 pad：命名即域。**
LEF 中它们都是 65×130（[VDD1:L56388-L56393](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56388-L56393)、[VDD1A:L57104-L57109](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L57104-L57109)、[VDD3:L58306-L58311](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L58306-L58311)、[VDDIO3:L58996-L59001](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L58996-L59001)、[VSS1:L60056-L60061](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L60056-L60061)、[VSS1A:L60613-L60618](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L60613-L60618)、[VSS3:L61616-L61621](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L61616-L61621)、[VSSIO3:L62159-L62164](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L62159-L62164)），liberty 中 [L1074-L1145](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1074-L1145) 八个 cell 依次列出，`area : 8450` 与 LEF 尺寸互验。命名的「域后缀」规律：

- 尾数 `1` → 1.2 V 核域（VDD1 的对外引脚就叫 `VDD1`，[L56423](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56423)；liberty 对应 [L1078-L1081](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1078-L1081)，`is_pad : true`）；
- 尾数 `3` → 3.3 V IO 域；名字带 `IO` → 接 VDDIO/VSSIO 总线（VDDIO3 的引脚 [L59064](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L59064)）；
- 尾数 `A` → 模拟域（VDD1A 的引脚叫 `VDDA1`，[L57172](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L57172)；VSS1A 的引脚叫 `VSSA`）。

**每个 pad 都是「轨道中继站」。** 观察 VDD1 的完整引脚表：VDD（[L56395](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56395)）、VDD1（[L56423](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56423)）、VDDIO（[L56497](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56497)）、VSS（[L56596](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56596)）、VSSIO（[L56624](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L56624)）——即便是「核电源 pad」，也同时把 IO 域的 VDDIO/VSSIO 轨道从左邻居接到右邻居。

逐个核对 23 个宏的引脚表后有一条更精细的规律：**数字域成员（CORNER、CUT、9 个 FILLER、PBMUX、PWE、VDD1/VDD3/VDDIO3/VSS1/VSS3/VSSIO3）都带 VDD/VDDIO/VSS/VSSIO 四条轨道；而四个模拟域成员——PAR、PAR_5、VDD1A、VSS1A——带的是 VDDA/VSSA，没有 VDDIO/VSSIO**（如 PAR 的引脚为 A、VDD、VDDA、VSS、VSSA、PAD）。也就是说环上跑的不止四条而是六条轨道，模拟段用 VDDA/VSSA 替代 IO 段。这就是 FILLER 必须延续电源、CORNER 必须把轨道转过弯、CUT 需要同时带六条轨道（[L51592-L51912](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51592-L51912)）的原因。

#### 4.3.4 代码实践

**实践目标**：为一个假想的 40 引脚芯片写出完整 pad 环清单，几何精确闭合。

**操作步骤**：
1. 设裸片为正方形，边长 \( L = 1000\ \mu m \)；
2. 四角各放 1 个 CORNER，每边可用 \( U = 1000 - 2 \times 130 = 740\ \mu m \)；
3. 分配 40 个信号引脚（PBMUX）+ 时钟（PWE，130）+ 核电源对（VDD1、VSS1）；
4. 用 4.2.4 的 `fill()` 算每边零头；
5. 输出清单并验算每边宽度和。

**参考解**（每边合计 740 μm）：

| 边 | 内容 | 宽度验算 |
|---|---|---|
| 顶 | 11×PBMUX + FILLER20 + FILLER5 | \( 11 \times 65 + 25 = 740 \) |
| 底 | 11×PBMUX + FILLER20 + FILLER5 | \( 715 + 25 = 740 \) |
| 左 | 9×PBMUX + VDD1 + VSS1 + FILLER20 + FILLER5 | \( 585 + 130 + 25 = 740 \) |
| 右 | 9×PBMUX + PWE + FILLER20 + FILLER5 | \( 585 + 130 + 25 = 740 \) |
| 四角 | 4×CORNER | — |

**需要观察的现象**：四条边的等式全部严格等于 740，无剩余缝隙；信号引脚合计 11+11+9+9 = 40。

**预期结果**：全环共用单元 \( 40\,\mathrm{PBMUX} + 1\,\mathrm{PWE} + 1\,\mathrm{VDD1} + 1\,\mathrm{VSS1} + 4\,\mathrm{CORNER} + 8 \times \mathrm{FILLER20} + 8 \times \mathrm{FILLER5} = 63 \) 个。注意这只是**几何可行解**：真实设计还需按供电电流决定电源 pad 数量、按 ESD 策略插入保护、按晶振布局就近放 PWE，并可能需要 VDDIO3/VSSIO3——把顶边两个 PBMUX 换成它们，等式依然成立（65↔65），这正是留给你的扩展练习。电源 pad 数量的电气依据需查 datasheet（待确认）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CORNER 的 SIZE 是 130×130，而普通 pad 是 65×130？
**答案**：pad 沿边排布只占「宽度」这一维（65），高度 130 是环的厚度；拐角处两条垂直的边要交接，单元必须在两个方向都占 130，才能把水平段和垂直段的轨道无缝转接。`SYMMETRY X Y R90` 允许同一个单元旋转/镜像后用于四个角。

**练习 2**：VDD1A 和 VDD1 都是 1.2 V 核域电源 pad，为什么留两个？
**答案**：尾缀 A 表示模拟域，引脚名 `VDDA1`（[L57172](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L57172)）区别于数字域的 `VDD1`。模拟电路（PLL、ADC 等）对电源噪声敏感，需要独立供电避免数字翻转噪声串扰——这是「同电压、不同域」的典型做法。精确电气隔离度需 datasheet/SPICE（待确认）。

**练习 3**：如何用一次 grep 验证「模拟域成员不带 VDDIO/VSSIO」？
**答案**：`grep -n '^  PIN ' ICSIOA_N55_3P3_1P6M1TM.lef` 列出全部引脚后，按宏分段统计：PAR、PAR_5、VDD1A、VSS1A 四个宏的引脚表里只有 VDDA/VSSA 而没有 VDDIO/VSSIO，其余宏（除无引脚的 FILLER0005）都是 VDD/VDDIO/VSS/VSSIO 齐备。这正对应 4.3.3 的结论：环上有六条轨道，模拟段换用 VDDA/VSSA（结合 4.1.4 脚本的 pins 列观察更直观）。

## 5. 综合实践

**任务：生成一份「40 引脚演示芯片」的 pad ring 报告。**

把 4.1.4、4.2.4、4.3.4 三个脚本合并成一个 `padring_report.py`，输入裸片边长与信号引脚数，输出：

1. **单元分类表**：23 个单元按五类分组，附 SIZE、引脚数、是否在 liberty（数据源：cell_list + LEF + liberty 三方交叉）；
2. **FILLER 面值表**：9 种宽度降序 + 名字解码验证；
3. **pad 环清单**：按「顶/底/左/右/角」列出所用单元，每边给出宽度验算等式；
4. **一致性自检**：
   - `Σ(每边宽度) == 4 × (边长 − 2×130) + 4×130`（周长闭合）；
   - 所有用到的单元都在 cell_list 的 23 个名字里（防拼写错误）；
   - PWE 只出现一次且占用宽度 130。

**验收标准**：把报告中的等式逐条手算核对；将清单里的每个单元名回查 cell_list 确认存在。若你装了 OpenROAD，还可以 `read_lef` 这个 IO LEF，用 `foreach macro` 打印 MACRO 名单与脚本输出比对（工具用法见 u6-l1；无工具则纯脚本即可）。

## 6. 本讲小结

- IO 库 23 个单元分五类：**信号 pad 4**（PAR/PAR_5/PBMUX/PWE）、**电源 pad 8**（VDD1/VDD1A/VDD3/VDDIO3 与四个 VSS 对应）、**填充 9**、**拐角 1**、**切割 1**；前三类在 liberty 里有 cell（12 个），后两类（CORNER/CUT/FILLER）是纯物理积木。
- FILLER 实际有 **9 种宽度**（50/20/10/5/2/1/0.1/0.01/0.005 μm），按「缺省小数点」命名，构成 1-2-5 面值体系；最小面值 0.005 μm 与 IOSite 的 site 宽度一致，是摆放格点的量子。
- 电源轨道靠单元拼接延续：数字域成员带 VDD/VDDIO/VSS/VSSIO 四条，**模拟域成员 PAR/PAR_5/VDD1A/VSS1A 换用 VDDA/VSSA**（环上共六条轨道）；FILLER 的本质是轨道延续器，而 FILLER0005 是唯一无引脚的纯几何单元。
- PWE 是 130×130 的双倍宽晶振 pad（XIN/XOUT 两个压焊端子，内部为电平位移 + 施密特 + 振荡驱动三级）；PBMUX 是 16 脚全功能双向 GPIO（PAD 受 OE 三态、C = PAD&IE）；PAR/PAR_5 是串联电阻挡位相差约 81 倍的极简 pad。
- LEF 的 `DIRECTION` 普遍填 INPUT、信息不全，引脚方向与功能要以 liberty 的 `direction/function/three_state` 为准——这是多视图一致性问题（u5-l2）的预演。
- 本 LEF 引用 `SITE IOSite` 23 次却未定义它，定义在 `_ecos` 版 L19-29（IOSite 0.005×130、IOCorner 130×130）——下一讲的入口。

## 7. 下一步学习建议

- **u4-l2（IO LEF：PAD 宏、IOSite 与 ecos 适配）**：本讲遗留的两个问题——`SITE IOSite/IOCorner` 的完整语义、`ORIGIN/FOREIGN` 的 20 μm 偏移、`_ecos` 版还修正了哪些电源引脚的 `USE` 属性——都在那一讲展开。
- **u4-l3（IO 电学模型）**：想深挖 PBMUX 的驱动电流（4 mA）、双电压域 vil/vih 阈值、以及 CDL 里 PWE 三级子电路的晶体管级细节，继续读 `liberty/` 与 `cdl/`。
- **回看 u3-l4**：本讲提到的 165 条 `ANTENNAPARTIALCUTAREA` 是天线效应属性在 IO 库的完整样例，可对照标准单元库仅有的 2 条理解差异。
- 若想动手验证分类脚本，推荐先重读 u1-l3 的 Makefile 一讲，确认 IO liberty 已在 git 内、无需 `make unzip` 即可解析。
