# 工艺 LEF（一）：金属栈与层规则

> 本讲对应的 PDK 文件：`prtech/techLEF/N551P6M.lef`（全文 672 行，仓库内自带的纯文本，无需下载）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 tech LEF 的文件骨架：`VERSION`、`PROPERTYDEFINITIONS`、`UNITS`、`MANUFACTURINGGRID` 各自在说什么。
2. 区分 `TYPE` 的三种主要取值——`MASTERSLICE`、`CUT`、`ROUTING`——并说出 ICS55 工艺里每一层属于哪一类。
3. 逐项解释 `ROUTING` 层的六个关键参数：`DIRECTION`、`PITCH`、`WIDTH`、`SPACING`、`RESISTANCE RPERSQ`、`DCCURRENTDENSITY`，并理解它们如何共同描述一张"金属栈"。
4. 用 `RESISTANCE RPERSQ`（方块电阻）估算一段互连的寄生电阻，理解它在寄生提取（parasitic extraction）中的意义。

## 2. 前置知识

本讲只依赖 u1-l2 建立的两个认知：仓库里 `prtech/techLEF/` 是全局唯一的工艺文件目录；LEF 是给布局布线工具读的"物理抽象视图"。在此之上补充几个术语：

- **tech LEF（工艺 LEF）**：描述"工艺规则"的部分——有哪些层、每层能怎么走线。与之相对的是 **cell LEF**（单元 LEF），描述每个标准单元的外形和引脚。两者合起来，布线器才知道"在什么网格上、用什么过孔、把哪些引脚连起来"。
- **金属栈（metal stack）**：芯片从下到上的导体层次。先是非金属层（阱、有源区、多晶硅），然后是金属层与过孔层交替堆叠：`MET1 → VIA1 → MET2 → VIA2 → …`。
- **pitch（节距）**：同层相邻两条走线中心线之间的距离，决定了"每微米有多少条可用轨道"。
- **方块电阻（sheet resistance，RPERSQ）**：一块薄膜材料"任意正方形"两对边之间的电阻，单位 Ω/square。它只跟材料和厚度有关，跟方块大小无关——这是估算互连电阻的基础（见 4.3 节的公式）。
- **寄生（parasitic）**：真实导线自带的电阻和电容。工具做时序分析时要把这些寄生算进去，否则估计的延迟会过于乐观。

## 3. 本讲源码地图

本讲只涉及一个文件，但它内部天然分成几段：

| 源码段落 | 行号 | 作用 | 本讲是否展开 |
| --- | --- | --- | --- |
| Apache-2.0 许可头 | L1–L13 | 逐文件版权声明（u1-l1 已讲） | 不展开 |
| 文件头 | L15–L17 | `VERSION 5.7`、总线位/层次分隔符 | 简述 |
| `PROPERTYDEFINITIONS` | L19–L24 | 声明 LEF 5.8 风格的扩展属性名 | 简述 |
| `UNITS` + `MANUFACTURINGGRID` | L26–L29 | 数据库单位与制造网格 | **模块 4.1** |
| 20 个 `LAYER` 定义 | L30–L201 | 三类层的全部规则 | **模块 4.2、4.3** |
| 固定 `VIA` 定义 | L202–L581 | 每对相邻层的 9 种预制过孔 | 下一讲 u2-l2 |
| `VIARULE … GENERATE` | L584–L642 | 过孔自动生成规则 | 下一讲 u2-l2 |
| `NONDEFAULTRULE DefaultTaper` | L644–L652 | 非默认布线规则 | u2-l3 会再遇到 |
| `SITE CoreSite/core7/core9` | L654–L670 | 布局行/单元占位定义 | 下一讲 u2-l2 |

## 4. 核心概念与源码讲解

### 4.1 单位与制造网格

#### 4.1.1 概念说明

任何 EDA 数据文件都要先回答两个问题："数字的单位是什么"和"最小刻度是多少"。tech LEF 用两个语句回答：

- `UNITS … DATABASE MICRONS 1000`：声明 1 个数据库单位（DBU）= 1/1000 微米。
- `MANUFACTURINGGRID 0.001`：声明所有几何图形必须落在 0.001 μm 的整数倍网格上。

两者必须配合：制造网格 0.001 μm 恰好等于 1 个 DBU，这样"对齐到网格"和"能用整数 DBU 表示"是同一件事。

#### 4.1.2 核心流程

工具读入 tech LEF 时的单位处理可以概括为：

```text
读入 UNITS 块
  → 计算 DBU 换算系数：1 μm = 1000 DBU
  → 记录 MANUFACTURINGGRID = 0.001 μm = 1 DBU
此后：
  - LEF 中的尺寸默认按 μm 解释（本文件如此）
  - 生成的 DEF 网表按 DBU（整数）存储
  - 布线器每画一段线，坐标都取整到 1 DBU 的倍数
```

#### 4.1.3 源码精读

文件头声明这是 LEF 5.7 格式，并定义总线位与层次分隔符：

[prtech/techLEF/N551P6M.lef:L15-L17](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L15-L17) —— `VERSION 5.7` 声明语法版本；`BUSBITCHARS "[]"` 让工具把 `A[0]`、`A[1]` 识别成总线位；`DIVIDERCHAR "/"` 定义层次分隔符（如 `u1/inv0/Z`）。

接着是一段属性声明：

[prtech/techLEF/N551P6M.lef:L19-L24](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L19-L24) —— `PROPERTYDEFINITIONS` 预先登记了 4 个 `LEF58_` 前缀的字符串属性（类型、包容、宽度、间距的 5.8 版扩展写法）。注意这只是"登记名字"：通读全文会发现后面没有任何一层真正给这些属性赋值。在 5.7 的文件里提前登记 5.8 属性名，是为兼容会读取这些扩展的工具做的铺垫。

然后是本模块的主角：

[prtech/techLEF/N551P6M.lef:L26-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L26-L29) —— `DATABASE MICRONS 1000` 与 `MANUFACTURINGGRID 0.001`。换算关系是：

\[ 1\ \text{DBU} = \frac{1\ \mu m}{1000} = 1\ nm = 0.001\ \mu m \]

55nm 工艺能做到 1nm 的制造网格刻度，这与工艺的最小几何尺寸（约 0.06 μm 量级，见 L646 的 `POLY WIDTH 0.06`）之间留了 60 倍的余量，保证任何合法尺寸都能被精确表示。

#### 4.1.4 代码实践

**实践目标**：用最简单的检索验证"单位声明"与"全文件尺寸精度"的一致性。

**操作步骤**：

1. 在仓库根目录执行下面的 grep，统计 tech LEF 里所有 `WIDTH`/`SPACING`/`PITCH` 数值中小数位不超过 3 位的比例：

```bash
# 示例命令：提取所有数值参数，观察小数位数
grep -nE 'PITCH|WIDTH|SPACING|AREA|RESISTANCE' prtech/techLEF/N551P6M.lef | head -40
```

2. 再确认 `MANUFACTURINGGRID` 与 `DATABASE` 的数值关系：

```bash
grep -n -A2 'UNITS\|MANUFACTURINGGRID' prtech/techLEF/N551P6M.lef | head -8
```

**需要观察的现象**：所有几何参数最多 3 位小数（如 `0.09`、`0.042`、`0.1122` 的电阻是 4 位，但它不是几何量），也就是说每个几何量都能被 1nm 网格整除。

**预期结果**：`DATABASE MICRONS 1000` 与 `MANUFACTURINGGRID 0.001` 数值互为倒数关系（精度 = 1/DBU 换算系数），全文件没有任何几何参数需要比 1nm 更细的刻度。（结果可直接对照源码行 L26–L29 与 L62–L201 核实，无需运行也能确认。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DATABASE MICRONS` 改成 `10000` 而 `MANUFACTURINGGRID` 仍是 `0.001`，会有什么后果？

**参考答案**：单纯看这个文件，语义不变——0.001 μm 依然能被 1/10000 μm 的 DBU 表示，只是换算系数变了。真正的风险在下游：DEF、布局布线工具按 DBU 存整数坐标，系数改变意味着所有衍生文件要同步换算，混用两套系数的文件会出现 10 倍比例错乱。PDK 一旦发布，DBU 系数就不再可改。

**练习 2**：`MANUFACTURINGGRID 0.001` 中的单位是什么？为什么 LEF 里不写单位后缀？

**参考答案**：单位是微米（μm）。LEF 的几何语句默认以 μm 为单位（由 `UNITS` 块声明），所以语句里只写裸数字；只有在 `UNITS` 这类元数据块里才会出现 `MICRONS` 这样的单位字。

**练习 3**：`BUSBITCHARS "[]"` 和 `DIVIDERCHAR "/"` 各自影响什么？

**参考答案**：前者告诉工具 `[]` 是总线下标括号，`DATA[3]` 会被解析成"总线 DATA 的第 3 位"而不是一个含方括号的普通名字；后者定义层次路径分隔符，`top/core/ff0` 表示 core 模块里的 ff0 实例。两者都只是"字符的语法含义声明"，与工艺无关。

---

### 4.2 MASTERSLICE / CUT / ROUTING 三类层

#### 4.2.1 概念说明

LEF 用 `LAYER` 块定义工艺层，用 `TYPE` 关键字说明层的角色。本文件一共 20 个 `LAYER` 块，分四类：

| TYPE | 层 | 数量 | 角色 |
| --- | --- | --- | --- |
| `OVERLAP` | OVERLAP | 1 | 标记"重叠区域"用的特殊层，不参与制造图形 |
| `MASTERSLICE` | ACT、NP、PP、NW1、POLY | 5 | 器件层（阱/有源区/注入/多晶硅），在硅片内部，不可布线 |
| `CUT` | CT、VIA1、VIA2、VIA3、VIA4、T4V2、RV | 7 | 过孔/接触孔层，连接上下两个导体层 |
| `ROUTING` | MET1–MET5、T4M2、RDL | 7 | 可走线的金属层 |

一句话记忆：**MASTERSLICE 描述器件长在哪，CUT 是层与层之间的"电梯"，ROUTING 是水平面上真正走信号的"马路"。** 布线器只会真正使用 ROUTING 和 CUT；MASTERSLICE 主要供 LVS、寄生提取工具和单元 LEF 里的阻挡区（OBS）引用。

#### 4.2.2 核心流程

把 20 层按"自下而上的层叠"排开，就是这张工艺剖面图（层名后括号内为 TYPE）：

```text
硅片内部（不可布线）
  NW1  (MASTERSLICE)   N 阱
  ACT  (MASTERSLICE)   有源区
  PP   (MASTERSLICE)   P+ 注入
  NP   (MASTERSLICE)   N+ 注入
  POLY (MASTERSLICE)   栅多晶硅
接触/过孔与金属交替
  CT   (CUT)   ─ 接触孔：器件层 → MET1
  MET1 (ROUTING, 水平)
  VIA1 (CUT)   ─ MET1 → MET2
  MET2 (ROUTING, 垂直)
  VIA2 (CUT)   ─ MET2 → MET3
  MET3 (ROUTING, 水平)
  VIA3 (CUT)   ─ MET3 → MET4
  MET4 (ROUTING, 垂直)
  VIA4 (CUT)   ─ MET4 → MET5
  MET5 (ROUTING, 水平)
  T4V2 (CUT)   ─ MET5 → T4M2（厚金属过孔）
  T4M2 (ROUTING, 垂直)  加厚顶层金属
  RV   (CUT)   ─ T4M2 → RDL（超大过孔）
  RDL  (ROUTING, 水平)  再布线层（封装用）
```

CUT 层连接哪两层，最可靠的证据来自文件后半段的 `VIA` 定义：`VIA T4M2_MET5`（L563）同时引用 MET5、T4V2、T4M2 三层，`VIA RDL_T4M2`（L573）同时引用 T4M2、RV、RDL——这直接证实了 T4V2 和 RV 在层叠中的位置（这两个 VIA 的细节留给 u2-l2）。器件层名称（ACT/NP/PP/NW1）的含义仓库未附文档，上表按行业命名惯例推断，**待确认**。

#### 4.2.3 源码精读

先看特殊的第一层：

[prtech/techLEF/N551P6M.lef:L30-L32](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L30-L32) —— `LAYER OVERLAP / TYPE OVERLAP` 是一个"零参数"层，只声明存在性，没有任何几何规则。它给工具提供标记重叠区域的语义占位。

接着是五个器件层，结构完全一样：

[prtech/techLEF/N551P6M.lef:L34-L52](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L34-L52) —— ACT、NP、PP、NW1、POLY 五个层各自只有 `TYPE MASTERSLICE` 一句，**没有宽度、间距、电阻等任何规则**。这正是"不可布线层"在 LEF 中的表达方式：登记名字供别的视图引用，不交给布线引擎。POLY 是其中唯一还会在本文件其他地方出现的——L645 的 `DefaultTaper` 规则引用了它。

CUT 层以 CT 为代表：

[prtech/techLEF/N551P6M.lef:L54-L60](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L54-L60) —— CT 层定义了孔的尺寸（`WIDTH 0.09`，即孔的边长）、孔与孔的最小间距（`SPACING 0.11`）、单侧包容要求（`ENCLOSURE ABOVE 0.04 0`：上方导体层每边至少包住孔 0.04 μm）以及直流电流密度上限 0.29 mA。CUT 层没有 `DIRECTION`/`PITCH`——孔是"点"，不占轨道。

VIA1–VIA4 四个常规过孔层结构与 CT 几乎相同（L76–L81、L97–L102、L118–L123、L139–L145），尺寸都是 0.09/0.11；差别在于 ENCLOSURE：VIA1–VIA3 没有在本块里写包容要求（包容在 L202 起的每个具体 VIA 里定义），VIA4 多了 `ENCLOSURE BELOW 0.02 0.005`。顶层两个"巨型"过孔对比鲜明：

[prtech/techLEF/N551P6M.lef:L162-L168](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L162-L168) —— T4V2 过孔：孔宽 0.36、间距 0.34、电流 3.2 mA，是普通 VIA1 的 4 倍宽、近 24 倍载流。

[prtech/techLEF/N551P6M.lef:L185-L191](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L185-L191) —— RV 过孔：孔宽 3、间距 3，上下两层都要各包住 1.5 μm，且**没有 DCCURRENTDENSITY**——这是为 RDL 封装连接准备的巨型过孔，量级比 VIA1 大 33 倍。

#### 4.2.4 代码实践

**实践目标**：数出全部 20 层并按 TYPE 分组，验证 4.2.1 表格。

**操作步骤**：

```bash
# 每个层块的第一行是 "LAYER <名字>"，紧随其下一行是 "TYPE <类型>"
grep -A1 '^LAYER ' prtech/techLEF/N551P6M.lef | grep '^TYPE' | sort | uniq -c
grep -c '^LAYER ' prtech/techLEF/N551P6M.lef
```

也可以用下面这段 Python（示例代码，保存为 `layer_types.py` 后运行）：

```python
# 示例代码：统计 tech LEF 中各 TYPE 的层数
import re, collections
text = open("prtech/techLEF/N551P6M.lef").read()
blocks = re.findall(r'^LAYER\s+(\S+)(.*?)^END\s+\1', text, re.M | re.S)
types = collections.Counter()
for name, body in blocks:
    m = re.search(r'TYPE\s+(\w+)', body)
    types[m.group(1)] += 1
print(dict(types), "total =", sum(types.values()))
```

**需要观察的现象**：`LAYER` 开头的行恰好 20 个；TYPE 统计为 OVERLAP 1、MASTERSLICE 5、CUT 7、ROUTING 7。

**预期结果**：`{'ROUTING': 7, 'CUT': 7, 'MASTERSLICE': 5, 'OVERLAP': 1} total = 20`。该结果可直接对照源码 L30–L201 逐块数出核实。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MASTERSLICE 层一个几何规则都没有，却还要写进 tech LEF？

**参考答案**：因为别的视图要引用这些名字。单元 LEF 的 OBS 阻挡区、LVS 工具的层对应表、寄生提取工具的器件识别都需要统一的层名。tech LEF 在这里是"层名字典"：先登记，规则留给专门的规则文件（ICS55 目前尚未发布 DRC/LVS 规则，见 u1-l1）。

**练习 2**：CT 和 VIA1 都是 CUT 层，它们连接的对象有什么不同？

**参考答案**：CT 是"接触孔"，把器件层（POLY/有源区）连到第一层金属 MET1——它从层叠位置（位于 POLY 之后、MET1 之前）可以推断；VIA1 是"金属间过孔"，把 MET1 连到 MET2。二者尺寸规则在本文件中恰好相同（0.09 宽、0.11 间距），但 ENCLOSURE 要求不同（CT 有 `ABOVE 0.04 0`，VIA1 无），电流上限也不同（0.29 vs 0.135 mA）。CT 连接的具体对象本仓库无文档说明，**待确认**。

**练习 3**：从 TYPE 的视角看，布线器"可用"的层有多少个？

**参考答案**：14 个——7 个 ROUTING 层提供水平走线资源，7 个 CUT 层提供跨层连接资源。MASTERSLICE 与 OVERLAP 共 6 层对布线器是只读的背景信息。

---

### 4.3 金属层参数：pitch / width / spacing / direction / resistance

#### 4.3.1 概念说明

每个 ROUTING 层用一组参数把自己"是什么规格的马路"讲清楚：

| 参数 | 含义 | 布线器怎么用 |
| --- | --- | --- |
| `DIRECTION` | 首选走线方向（水平/垂直） | 分配轨道方向，规划层间资源 |
| `PITCH a b` | 相邻轨道中心距（a=X 方向，b=Y 方向） | 生成布线轨道网格 |
| `WIDTH` | 最小线宽 | 画线的默认宽度 |
| `SPACING` | 同层两线间最小净距 | 冲突检查 |
| `AREA` | 金属岛最小面积 | 消除工艺上难以做的小碎块 |
| `MAXWIDTH` | 单根线最大宽度 | 超过需开槽（slotting） |
| `MINENCLOSEDAREA` | 包围孔洞的最小面积 | 避免 CMP 平坦化问题的小洞 |
| `RESISTANCE RPERSQ r` | 方块电阻（Ω/square） | 互连电阻估计、寄生提取 |
| `DCCURRENTDENSITY AVERAGE i` | 直流电流密度上限（mA） | 电迁移检查 |

其中三个几何量的关系是理解轨道网格的关键：同层相邻两条最小宽度线、按最小间距摆放时，中心距等于 `WIDTH + SPACING`。PITCH 必须 ≥ 这个值。对 MET2–MET5 来说 \(0.1 + 0.1 = 0.2\) 恰好等于 PITCH 0.2——轨道被"满密度"铺满；MET1 则是 \(0.09 + 0.09 = 0.18 \le 0.2\)，轨道间留了 0.02 μm 余量。

`RESISTANCE RPERSQ` 是寄生提取的输入之一：一段长 \(L\)、宽 \(W\) 的导线电阻为

\[ R = R_{\text{persq}} \times \frac{L}{W} \]

导线越长、越细，电阻越大；与绝对尺寸无关的"方块数"（\(L/W\)）是唯一变量。

#### 4.3.2 核心流程

布线器拿到一个 ROUTING 层后建立轨道网格的流程：

```text
读入 LAYER MET2
  → 记录 DIRECTION VERTICAL（本层优先走竖线）
  → 记录 PITCH 0.2 0.2、OFFSET 0 0
  → 从原点起，每 0.2 μm 生成一条竖直轨道（X 方向按 X-pitch）
  → 线默认宽 0.1，与相邻轨道净距 0.1
  → 信号引脚落在轨道上 → 分配一条轨道 → 用 VIA1/VIA2 与上下层换层
```

全部 7 个 ROUTING 层的参数汇总（数据逐行取自 L62–L201，可作为本讲的"速查表"）：

| 层 | DIRECTION | PITCH (X/Y) | WIDTH | SPACING | RPERSQ | DCCUR (mA) | 定义行号 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MET1 | HORIZONTAL | 0.2 / 0.2 | 0.09 | 0.09 | 0.1122 | 1.5 | L62–L74 |
| MET2 | VERTICAL | 0.2 / 0.2 | 0.10 | 0.10 | 0.0914 | 1.7 | L83–L95 |
| MET3 | HORIZONTAL | 0.2 / 0.2 | 0.10 | 0.10 | 0.0914 | 1.7 | L104–L116 |
| MET4 | VERTICAL | 0.2 / 0.2 | 0.10 | 0.10 | 0.0914 | 1.7 | L125–L137 |
| MET5 | HORIZONTAL | 0.2 / 0.2 | 0.10 | 0.10 | 0.0914 | 1.7 | L147–L159 |
| T4M2 | VERTICAL | 0.8 / 0.8 | 0.40 | 0.40 | 0.0239 | 8.1 | L170–L182 |
| RDL | HORIZONTAL | 5 / 5 | 3 | 2 | 0.0151 | — | L193–L201 |

三个规律一目了然：

1. **方向严格交替**：H、V、H、V、H、V、H——任何相邻两层互相垂直。
2. **MET2–MET5 规格完全相同**：这是"均质中层"设计，中间几层只负责普通信号。
3. **越往上越粗**：MET1/MET2–MET5/T4M2/RDL 三档，线宽 0.09 → 0.1 → 0.4 → 3，电阻 0.1122 → 0.0914 → 0.0239 → 0.0151，载流 1.5/1.7 → 8.1 →（RDL 未规定）。顶层为电源和封装连接保留了粗壮、低阻、大电流的资源。

#### 4.3.3 源码精读

第一档——最底层金属 MET1：

[prtech/techLEF/N551P6M.lef:L62-L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L62-L74) —— MET1 是唯一 0.09 μm 线宽的布线层（其余中层都是 0.1），`DIRECTION HORIZONTAL`。这个方向不是随意选的：u1-l2 里我们看到标准单元的电源/地轨道是 MET1 横向贯穿的，单元行沿水平方向排布，MET1 水平方向正好承载这些轨道。`AREA 0.042`、`MINENCLOSEDAREA 0.18`、`MAXWIDTH 10` 分别限制碎块、孔洞与过宽线。`RESISTANCE RPERSQ 0.1122` 是全栈最高的方块电阻——最细、最薄的层。

第二档——中层 MET2（MET3–MET5 与之逐字相同，只是方向交替）：

[prtech/techLEF/N551P6M.lef:L83-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L83-L95) —— MET2 `DIRECTION VERTICAL`、`PITCH 0.2 0.2`、`WIDTH 0.1`、`SPACING 0.1`、`RPERSQ 0.0914`。注意 `PITCH 0.2 0.2` 给了两个数：分别是 X 和 Y 方向的轨道节距；`OFFSET 0 0` 说明第一条轨道从坐标原点开始（对照 `_ecos` 版会把这个值改成 0.1，那是 u2-l3 的内容）。MET3（L104–L116）、MET4（L125–L137）、MET5（L147–L159）复制了同一套数字，只有 `DIRECTION` 按 H/V 交替。

第三档——加厚顶层金属 T4M2：

[prtech/techLEF/N551P6M.lef:L170-L182](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L170-L182) —— T4M2 的所有参数整体放大 4 倍（PITCH 0.8、WIDTH 0.4、SPACING 0.4），`MAXWIDTH` 放宽到 20，方块电阻降到 0.0239（约为 MET2 的 \(1/3.8\)），直流电流密度升到 8.1 mA（MET1 的 5.4 倍）。文件名 `N551P6M` 按行业惯例可解读为"55nm、1 层多晶、6 层金属"——MET1–MET5 加上这个加厚顶层正好 6 层金属（此解码为惯例推断，**待确认**）。"T4M2"的字面含义仓库未提供文档，从 `VIA T4M2_MET5`（L563）的引用关系可确定它是位于 MET5 之上的顶层厚金属。低阻+大电流的组合决定了它的用途：电源网络主干、时钟树主干、以及向封装凸点的连接。

第四档——再布线层 RDL：

[prtech/techLEF/N551P6M.lef:L193-L201](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L193-L201) —— RDL 的参数比其他 ROUTING 层少一截：只有 DIRECTION/PITCH/WIDTH/OFFSET/SPACING/RESISTANCE，**没有 AREA、MAXWIDTH、MINENCLOSEDAREA、DCCURRENTDENSITY**。它的线宽 3 μm、间距 2 μm、节距 5 μm，比信号层粗 30 倍，方块电阻 0.0151 全栈最低。RDL（Re-Distribution Layer，再布线层）是芯片最顶上为封装服务的厚金属层，用来把压焊点/凸点重新排布到想要的位置。它名义上是 ROUTING 类型（因此布线器能读它），但从参数量级看它不是给信号自动布线用的层——规则缺失本身就在告诉工具"别把我当普通信号层"。

用方块电阻公式做一个具体计算，感受层间差异：一段 50 μm 长、取各自最小宽度的导线

\[ R_{MET1} = 0.1122 \times \frac{50}{0.09} \approx 62.3\ \Omega , \qquad R_{T4M2} = 0.0239 \times \frac{50}{0.4} \approx 2.99\ \Omega \]

同样长度下 T4M2 的电阻只有 MET1 的约 1/21。这就是电源网络、时钟主干要用高层走线的原因。

最后要注意一个"缺位"：**本文件的 ROUTING 层只有 RESISTANCE，没有任何 CAPACITANCE 参数**（通读 L62–L201 可验证，没有一个 `CAPACITANCE` 关键字）。这意味着直接用这份 tech LEF 的工具只能估电阻、估不了电容——`prtech/techLEF/` 下还放了一个 `N551P6M_ecos.lef`，正是为补上电容等参数而存在的适配版本，我们在 u2-l3 专门对比。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写脚本把所有 `TYPE ROUTING` 层解析成"层名/方向/pitch/最小宽度/最小间距/电阻"对照表，然后回答两个理解性问题。

**操作步骤**：

1. 把下面这段 Python 保存为 `stack_report.py`（示例代码，放在仓库任意临时位置均可，不要改动仓库文件）：

```python
# 示例代码：解析 tech LEF 的 ROUTING 层参数
import re

def parse(path):
    layers, cur = [], None
    for line in open(path):
        s = line.strip()
        m = re.match(r'LAYER\s+(\S+)', s)
        if m:
            cur = {"name": m.group(1)}
            layers.append(cur)
            continue
        if cur is None:
            continue
        for key in ("TYPE", "DIRECTION", "PITCH", "WIDTH", "SPACING", "RESISTANCE"):
            if s.startswith(key + " "):
                cur[key] = s.split(None, 1)[1].rstrip(" ;")
        if s.startswith("END " + cur["name"]):
            cur = None
    return layers

rows = [l for l in parse("prtech/techLEF/N551P6M.lef")
        if l.get("TYPE") == "ROUTING"]
print(f"{'LAYER':6} {'DIRECTION':11} {'PITCH':9} {'WIDTH':7} "
      f"{'SPACING':8} {'RPERSQ':8}")
for r in rows:
    res = r.get("RESISTANCE", "-").split()[-1] if "RESISTANCE" in r else "-"
    print(f"{r['name']:6} {r.get('DIRECTION','-'):11} "
          f"{r.get('PITCH','-'):9} {r.get('WIDTH','-'):7} "
          f"{r.get('SPACING','-'):8} {res:8}")
```

2. 运行 `python3 stack_report.py`。
3. 用同一脚本跑 `prtech/techLEF/N551P6M_ecos.lef`，对比两张表的差异（预习 u2-l3）。

**需要观察的现象**：表格输出 7 行；方向列呈 H/V 交替；MET2–MET5 四行除名字与方向外完全一致；T4M2、RDL 两行的数字明显"大一截"。

**预期结果**（本表数值已逐行对照源码 L62–L201 核实）：

```text
LAYER  DIRECTION   PITCH    WIDTH   SPACING  RPERSQ
MET1   HORIZONTAL  0.2 0.2  0.09    0.09     0.1122
MET2   VERTICAL    0.2 0.2  0.1     0.1      0.0914
MET3   HORIZONTAL  0.2 0.2  0.1     0.1      0.0914
MET4   VERTICAL    0.2 0.2  0.1     0.1      0.0914
MET5   HORIZONTAL  0.2 0.2  0.1     0.1      0.0914
T4M2   VERTICAL    0.8 0.8  0.4     0.4      0.0239
RDL    HORIZONTAL  5 5      3       2        0.0151
```

然后回答两个问题：

**问题 A：MET1 与 MET2 的布线方向为何交替？**

参考答案：`DIRECTION` 是布线器分配给每层的"首选方向"。相邻层垂直交替（MET1 横、MET2 竖、MET3 横……）是标准做法，原因有三：① 任何两点间的曼哈顿连接都能用"横一层+竖一层"组合完成，层间只需一个过孔，路径最短；② 若相邻两层同向，其中一层的资源很难被利用，等于浪费一整层；③ 电源网格天然需要横竖两组金属带交织成网——MET1 横向承载单元行内的电源轨道（与 `DIRECTION HORIZONTAL` 一致），上层竖向的 MET2/MET4/T4M2 再搭垂直电源带。本工艺从 MET1 到 RDL 严格两两垂直，说明这套栈是按经典交替规则设计的。

**问题 B：T4M2 与 RDL 和普通 MET 层有何不同？**

参考答案：见 4.3.3 精读的分析——T4M2 是"加厚顶层金属"（规格放大 4 倍、电阻约为中层 1/3.8、载流 8.1 mA、MAXWIDTH 放宽到 20），面向电源网络与封装连接；RDL 是"再布线层"（宽 3 μm、节距 5 μm、电阻全栈最低，且缺失 AREA/MAXWIDTH/MINENCLOSEDAREA/DCCURRENTDENSITY 四项规则），面向封装级压焊点/凸点重排，不参与片内信号布线。两者与下层金属之间用巨型过孔 T4V2（0.36）和 RV（3.0）连接，进一步印证它们服务于"大电流、粗线条"的场景。

**如果无法确定运行结果**：脚本依赖 Python 3 标准库，预期输出已依据源码手工核实；若你的环境中结果与上表不符，优先检查是否读入了 `_ecos` 版文件（两者的 OFFSET/电容参数不同，见 u2-l3）。

#### 4.3.5 小练习与答案

**练习 1**：一段 120 μm 长、最小宽度的 MET2 线，电阻是多少？同样长度换成 T4M2 呢？

**参考答案**：\(R_{MET2} = 0.0914 \times 120 / 0.1 \approx 109.7\ \Omega\)；\(R_{T4M2} = 0.0239 \times 120 / 0.4 \approx 7.17\ \Omega\)。差距约 15 倍。注意导线电阻与长度成正比、与宽度成反比，这正是"长线走高层"的定量依据。

**练习 2**：`PITCH 0.2 0.2` 的两个 0.2 分别是什么？为什么 MET1 的 `WIDTH + SPACING = 0.18 < 0.2`？

**参考答案**：分别是 X 方向和 Y 方向的轨道节距。PITCH 是"中心到中心"的距离，WIDTH+SPACING 是"边到边"最小占用的下限；前者必须不小于后者。MET1 的 0.18 < 0.2 说明其轨道之间留有 0.02 μm 的额外余量；MET2–MET5 则恰好取等（0.1+0.1=0.2），轨道满密度排布。

**练习 3**：SITE core7 的宽度是 0.2 μm（L660–L664），这个数字和本讲的哪个参数相等？为什么？

**参考答案**：等于 MET1/MET2 的 PITCH 0.2。标准单元的宽度必须量化到布线轨道的整数倍，单元引脚才能落在布线网格上、被自动布线直接接上。site 宽度 = 布线节距是 PDK 设计的基本约束（SITE 的细节在 u2-l2 展开）。

**练习 4**：`DCCURRENTDENSITY AVERAGE 1.5` 这个 1.5 的单位是什么，用来防什么？

**参考答案**：单位是毫安（mA），表示该层走线允许的平均直流电流。它用于电迁移（electromigration）检查：长期大电流会把金属原子"冲走"造成断线或短路，时序签核与电源分析工具会用这个上限校验每段金属的电流密度。注意 RV 和 RDL 没有声明这个值，说明它们不按常规信号层做该项检查。

## 5. 综合实践

**任务：给 ICS55 工艺画一张"层规格名片"。**

把本讲三个模块串起来，产出一页报告：

1. **脚本层**：扩展 4.3.4 的 `stack_report.py`，让它输出全部 20 层（不限 ROUTING），每层一行，列为"层名 / TYPE / 方向 / pitch / 宽 / 间距 / RPERSQ / 电流密度"，缺项填 `-`。CUT 层还要输出 ENCLOSURE（提示：解析 `ENCLOSURE ABOVE|BELOW x y` 两种写法，参考 L58、L143、L166、L189–L190）。
2. **表格层**：在报告里手绘（或用文本画）4.2.2 那张层叠剖面图，并在每个 CUT 层旁标注它连接的上下两个导体层（CT→POLY/MET1、VIA1→MET1/MET2…T4V2→MET5/T4M2、RV→T4M2/RDL；T4V2 与 RV 的连接对象可用 L563–L581 的 VIA 定义作证据）。
3. **计算层**：假设电源网络需要从 MET5 竖直下到 MET1 水平轨，路径为"MET5 上 200 μm 水平线 + 一个过孔 + MET4 上 200 μm 垂直线"，用 RPERSQ 估算这段电源路径的金属电阻（过孔电阻取 L203 的 `RESISTANCE 2.5` Ω/孔），并回答：如果把 200 μm 的水平段从 MET5 换到 T4M2，电阻降到多少？
4. **思考层**：用一句话向同事解释"为什么这份 tech LEF 不能直接用来做准确的寄生提取"（提示：4.3.3 结尾的"缺位"）。

第 3 步参考数值：MET5 段 \(0.0914 \times 200/0.1 = 182.8\ \Omega\)，MET4 段同为 182.8 Ω，加一个过孔 2.5 Ω，合计约 368.1 Ω；换 T4M2 后水平段变为 \(0.0239 \times 200/0.4 = 11.95\ \Omega\)，总电阻约 197.3 Ω——电源路径电阻几乎减半，这就是把电源主干放上厚金属的意义。

## 6. 本讲小结

- tech LEF 用 `UNITS DATABASE MICRONS 1000` + `MANUFACTURINGGRID 0.001` 定义 1nm 的数据精度，所有几何量都落在这个网格上。
- 20 个 `LAYER` 分四类：1 个 OVERLAP、5 个 MASTERSLICE（器件层，零规则）、7 个 CUT（过孔层）、7 个 ROUTING（金属层）——布线器真正用的是后 14 层。
- 金属层方向 H/V 严格交替：MET1 横（承载单元行电源轨道）、MET2 竖……直到 RDL 横；交替让任意曼哈顿路径都能"一层横一层竖"完成。
- 金属栈分三档：MET1（0.09 细线）→ MET2–MET5（0.1 均质中层）→ T4M2（0.4 厚金属，低阻大电流）→ RDL（3 μm 封装再布线层，规则残缺即"非信号层"信号）。
- 寄生电阻由 `RESISTANCE RPERSQ` 按公式 \(R = R_{persq} \cdot L/W\) 估算，高层厚金属电阻可比 MET1 低一个数量级以上。
- 本版 tech LEF **没有任何 CAPACITANCE 参数**——只有 `_ecos` 版补齐了电容，这是下一讲的引子。

## 7. 下一步学习建议

本讲只读了 L30–L201 的层定义，文件后半段还藏着三块内容，正好是 u2-l2 的全部素材：

1. **固定 VIA 定义（L202–L581）**：每对相邻金属层有 9 种预制过孔（`MET2_MET1_VIA1_0` 到 `_8`），每种是三层矩形的组合——去读 L202–L290，看 ENCLOSURE 如何随过孔种类变化。
2. **VIARULE GENERATE（L584–L642）**：布线器动态生成过孔的规则，与固定 VIA 的适用场景不同。
3. **SITE 定义（L654–L670）**：CoreSite（0.2×1.4）、core7（0.2×1.4）、core9（0.2×1.8）三种布局行，以及标准单元为什么"高 1.4 μm"。

学完 u2-l2 后，再进入 u2-l3 对比 `N551P6M_ecos.lef`——你会看到本讲留下的"没有电容"问题是如何被解决的，以及 `OFFSET 0` 改成 `0.1` 对轨道网格的影响。
