# 天线效应与 ant LEF

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话解释**工艺天线效应（process antenna effect）**为什么会损坏栅氧，以及天线比率（Antenna Ratio）是怎么定义的。
2. 读懂 LEF 中 `ANTENNAPARTIALMETALAREA`、`ANTENNAPARTIALCUTAREA` 等引脚级天线属性的含义与书写位置。
3. 用 `diff` + `grep` 精确定位 `ics55_LLSC_H7CH_ant.lef` 相对普通版 LEF 的全部差异（答案是：全文件只多 2 行），并说出它们属于哪两个单元、哪个引脚。
4. 查出 `ANT2H7H` / `ANT4H7H` 两个天线二极管单元的尺寸、引脚和空 Verilog 模型，并说明在数字后端流程的哪个阶段、什么条件下需要插入它们。

## 2. 前置知识

本讲默认你已学过 u2-l1（工艺 LEF 的层规则）和 u3-l2（单元 LEF 的 MACRO/PIN/PORT/RECT 结构）。在此之上，补充三个新概念：

- **制造顺序与「天线」**：芯片是自下而上逐层制造的——先做晶体管，再做 MET1，再做 VIA1/MET2……每一层金属都要经历**等离子体刻蚀（plasma etch）**。刻蚀时，离子和电子在等离子体中的迁移率不同，金属线会像一根「天线」一样净吸附电荷。如果这根金属线此刻**只连着栅极、还没有连到任何可以泄放电荷的扩散区（驱动管的源漏）**，积累的电荷就会在薄栅氧上打出一条隧穿通路，轻则阈值电压漂移，重则栅氧击穿、晶体管永久损坏。
- **天线比率（Antenna Ratio, AR）**：判断是否危险的核心度量，是「连到栅极的金属面积」与「栅氧面积」之比：

  \[
    \mathrm{AR} = \frac{A_{\text{metal}}}{A_{\text{gate}}}
  \]

  工艺厂会为每一层给一个上限 \( \mathrm{AR}_{\max}(\text{层}) \)，超过即违例。因为金属面积只有在**布线之后**才确定，天线检查天然是布线后（post-route）的物理检查。此外还分 **PAR**（Partial Area Ratio，按单一金属层分别算）与 **CAR**（Cumulative Area Ratio，把到目前为止的所有下层金属累计起来算）；过孔（cut）也有对应的 cut 面积版本。
- **两种修复手段**：一是**跳线（jumper）**——把长导线中紧邻栅极的那段改到更高金属层，让低层金属在刻蚀时只剩一个很短的 stub，各层的部分面积都变小，而高层刻蚀时下层已经铺好、电荷可以顺着通孔流到驱动管的扩散区；二是**插入天线二极管（antenna diode）**——在违例网络上挂一个反偏二极管，给电荷提供一条通向衬底/电源的泄放通路。本仓库的 `ANT2H7H`/`ANT4H7H` 就是第二类修复所用的单元。

一句话直觉：**天线效应是「制造过程中的静电事故」，检查靠面积比值，修复靠「让电荷有地方跑」**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef` | H7CH 库的「带天线注记版」单元 LEF（79582 行） | 仅有的 2 行 `ANTENNAPARTIALMETALAREA`；`ANT2H7H`/`ANT4H7H` 宏定义 |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef` | H7CH 库普通版单元 LEF（79580 行） | 与 ant 版做 diff，确认「除了 2 行之外逐字节相同」 |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v` | H7CH 仿真模型 | `ANT2H7H`/`ANT4H7H` 的空模型 |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt` | 748 个可综合单元名单 | 验证 ANT 单元**不在**其中 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef` | IO 库 LEF | 191 处天线注记，含 `ANTENNAPARTIALCUTAREA` |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/lef/ics55_LLSC_H7CL_ant.lef`、`.../ics55_LLSC_H7CR/lef/ics55_LLSC_H7CR_ant.lef` | 另两套阈值库的 ant LEF | 验证「三库同样只加 2 行」的规律 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**天线效应原理**、**LEF ANTENNA 属性**、**ANT 二极管单元**。

### 4.1 天线效应原理

#### 4.1.1 概念说明

「天线效应」不是电路工作时的效应，而是**制造过程**中的效应。理解它的钥匙是记住两点：

1. **层是逐层制造的**：刻蚀 MET1 的时候，MET2 还不存在；一根「最终」会连到驱动管的 MET1 长线，在它自己被刻蚀的那一刻，可能只连着几个接收端的栅极。
2. **栅氧非常薄**：55nm 工艺的栅氧只有几纳米，能承受的注入电荷有限。

于是定义天线比率：

\[
  \mathrm{AR}_{L} = \frac{A_{\text{metal},L}}{A_{\text{gate}}}, \qquad \text{违例当 } \mathrm{AR}_{L} > \mathrm{AR}_{\max}(L)
\]

其中 \( A_{\text{metal},L} \) 是**只看第 \(L\) 层**、与被保护栅极相连的金属面积（PAR 口径），\( A_{\text{gate}} \) 是该栅极的栅氧面积。注意这是「每层分别检查」，所以同一根网上的金属要按 MET1、MET2……分别统计——这正是 LEF 里 `ANTENNAPARTIALMETALAREA ... LAYER MET1` 要写明层名的原因。

#### 4.1.2 核心流程

一次典型的布线后天线修复流程：

```text
详细布线完成
   │
   ▼
按层统计每根 net 的 金属面积（cell 内部 pin 注记 + 布线器画出的线段）与 栅面积
   │
   ▼
对每个接收端栅极计算 AR_L = A_metal,L / A_gate
   │
   ├── AR_L ≤ AR_max(L)  → 通过
   │
   └── AR_L >  AR_max(L)  → 违例
          │
          ├── 优先：跳线（把长段换到高层，重布后复查）
          └── 兜底：在违例 net 靠近接收端插入天线二极管（ANT 单元），
                     A 脚接 net，VDD/VSS 轨提供对衬底的泄放通路
```

要点：单元 LEF 里的天线注记负责「cell 内部贡献的那部分金属面积」，布线器自己画的线段面积则由工具从版图几何直接累加，两者相加才是完整的 \( A_{\text{metal},L} \)。

#### 4.1.3 源码精读

原理本身不体现在某个具体代码行里，但本仓库的数据侧面印证了「按层、按面积」这一模型：

- IO 库 LEF 中，同一个引脚上按 MET2/3/4/5 分别写了 4 条 `ANTENNAPARTIALMETALAREA`、按 VIA2/3/4 分别写了 3 条 `ANTENNAPARTIALCUTAREA`——正是「每层单独统计面积」的直接物证（详见 4.2.3 的精读）。
- 标准单元库只给 MET1 注记，因为普通版单元 LEF 的所有引脚几何都在 MET1 上（u3-l2 的结论：普通版所有几何均在 MET1）。

#### 4.1.4 代码实践

**实践目标**：用一个数值例子建立对比率量级的直觉。

**操作步骤**（手算即可）：

1. 设某接收栅极的栅面积 \( A_{\text{gate}} = 0.05\,\mu m^2 \)，工艺规定 MET1 的 \( \mathrm{AR}_{\max} = 400 \)。
2. 一根只连该栅极的 MET1 长线宽 0.09 μm（本 PDK MET1 最小宽度，见 u2-l1）、长 250 μm。
3. 计算 \( \mathrm{AR}_{\text{MET1}} \) 并判断是否违例。

**需要观察的现象 / 预期结果**：

\[ A_{\text{metal}} = 0.09 \times 250 = 22.5\,\mu m^2,\qquad \mathrm{AR} = 22.5 / 0.05 = 450 > 400 \]

违例。若把这根线的长段改到 MET2、只留 5 μm 的 MET1 stub，则 \( \mathrm{AR}_{\text{MET1}} = 0.09\times 5/0.05 = 9 \)，MET1 通过；MET2 刻蚀时电荷可经 VIA1 流向下层已连好的扩散区，通常也安全——这就是跳线修复的量化直觉。（本例中的 \( \mathrm{AR}_{\max}=400 \) 为教学假设值，本 PDK 真实的分层 AR 上限在仓库提供的工艺文档中，git 内未含，**待确认**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么天线检查放在布线之后而不是综合之后做？

**答案**：综合产物是门级网表，只有网表连接关系、没有任何金属几何，\( A_{\text{metal}} \) 无从谈起；只有详细布线完成后，每根 net 在每一层上画了多长的线才确定下来，比率才可计算。

**练习 2**：跳线为什么能修复天线违例？请从「制造顺序」角度回答。

**答案**：金属自下而上逐层制造。把长段改到高层后，低层在刻蚀时只剩很短的 stub，低层部分面积比大幅下降；而高层被刻蚀时，下层金属和通孔已经就位，电荷可以沿通孔流到已形成的驱动管扩散区泄放，不再单独堆积在被保护的栅极上。

**练习 3**：PAR 和 CAR 的区别是什么？

**答案**：PAR（Partial Area Ratio）按单一层分别计算金属面积，检查「这一层刻蚀时」的风险；CAR（Cumulative Area Ratio）把该层以下（含该层）的所有金属面积累计，检查「到这一层为止」的累积风险。两者都除以同一个栅面积。

### 4.2 LEF ANTENNA 属性

#### 4.2.1 概念说明

LEF 允许在 `MACRO → PIN` 内部书写天线属性，把**单元内部挂在某个引脚节点上的金属/过孔面积**显式告诉工具。常用的有：

| 属性 | 含义 | 典型位置 |
| --- | --- | --- |
| `ANTENNAPARTIALMETALAREA area [LAYER layerName]` | 该引脚在指定金属层上的部分金属面积 | 任意引脚 |
| `ANTENNAPARTIALCUTAREA area [LAYER cutLayerName]` | 该引脚在指定 cut 层上的过孔面积 | 任意引脚 |
| `ANTENNAGATEAREA area [LAYER layerName]` | 该引脚连接的栅面积（作为分母） | 输入引脚 |
| `ANTENNADIFFAREA area` | 该引脚连接的扩散面积（可泄放，作有利项） | 输出引脚 |

语法位置很严格：**写在 `PIN` 语句之后、`PORT` 之前**，与 `DIRECTION`、`USE` 同级。工具读入后，在做布线后天线检查时把这部分面积累加到对应 net。

本仓库的现状值得注意：

- **三套标准单元库的 ant LEF 每套只补了 2 行**，都是 `ANTENNAPARTIALMETALAREA`，都挂在输出引脚 `Y` 上；全仓库**没有任何** `ANTENNAGATEAREA` 或 `ANTENNADIFFAREA`（用 `grep` 全仓库 LEF 验证，0 命中）。
- IO 库 LEF 则有 191 处注记（26 处 `ANTENNAPARTIALMETALAREA` + 165 处 `ANTENNAPARTIALCUTAREA`），覆盖多层多 cut。

也就是说，标准单元的 ant 版是「极简注记」，IO 库才是「完整注记」的样例。

#### 4.2.2 核心流程

ant 版 LEF 的生成与使用可以概括为：

```text
普通版 LEF（785 个 MACRO，79580 行）
   │  厂商/维护者为个别单元的引脚补充天线面积注记
   ▼
ant 版 LEF（79582 行 = 79580 + 2）
   │  使用时与普通版二选一读入布线/物理验证工具
   │  （两者 MACRO 集合完全相同，同时读入会重复定义）
   ▼
布线后天线检查时，pin 注记 + 布线几何 一同参与 AR 计算
```

它与 `_ecos` 版是**正交**的两个维度：`_ecos` 版改的是几何与电源引脚（u3-l3），ant 版改的是天线注记。仓库目前没有「ant + ecos 合体」的版本，**待确认**是否有必要由维护者提供。

#### 4.2.3 源码精读

先看 diff 证据（在仓库根目录执行）：

```bash
diff IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef \
     IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef
```

实际输出只有两组（本讲已运行验证）：

```text
54821a54822
>     ANTENNAPARTIALMETALAREA 4.8e-05 LAYER MET1 ;
79490a79492
>     ANTENNAPARTIALMETALAREA 3.1e-05 LAYER MET1 ;
```

第一处差异属于 `OAI21BX6H7H`（OAI21 复合门、带一个反相输入 B、驱动强度 X6）。先看普通版的输出引脚 `Y`——`USE SIGNAL` 之后直接就是 `PORT`：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef:54819-54824](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L54819-L54824)：普通版 `OAI21BX6H7H` 的输出引脚 `Y`，`DIRECTION OUTPUT ; USE SIGNAL ;` 之后没有任何天线注记，直接进入 `PORT`/`LAYER MET1`。

再看 ant 版同一位置——多出的那一行就插在 `USE SIGNAL` 与 `PORT` 之间：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:54819-54833](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L54819-L54833)：ant 版同一引脚，第 54822 行新增 `ANTENNAPARTIALMETALAREA 4.8e-05 LAYER MET1 ;`，声明「本单元内部、挂在 Y 节点上的 MET1 金属面积为 4.8e-05」；下面 8 个 `RECT` 是该引脚的 MET1 几何。
- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:54762-54768](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L54762-L54768)：`OAI21BX6H7H` 的宏头，`SIZE 3.4 BY 1.4`、`SITE core7`，三个信号引脚 A0/A1/B0N 加电源 VDD/VSS。

第二处差异属于 `XOR3X6H7H`（三输入异或、X6 驱动）：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:79489-79508](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L79489-L79508)：`XOR3X6H7H` 的输出引脚 `Y`，第 79492 行新增 `ANTENNAPARTIALMETALAREA 3.1e-05 LAYER MET1 ;`；其下 12 个 `RECT` 是引脚几何（一个阶梯状堆叠）。
- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:79429-79435](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L79429-L79435)：`XOR3X6H7H` 的宏头，`SIZE 5 BY 1.4`。

**IO 库里的「完整版」注记长什么样**——以 `P65_1233_CORNER`（宏起始于 [IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:19](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L19)）的 `VSS` 引脚为例：

- [IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:12091-12100](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L12091-L12100)：同一个引脚上按 MET2/MET3/MET4/MET5 各写一条 `ANTENNAPARTIALMETALAREA 4.8e-05`，再按 VIA2/VIA3/VIA4 各写一条 `ANTENNAPARTIALCUTAREA`（数值 2.3328/0.0081/1.5876）——一条引脚、七个层维度，正是「按层分别统计」的完整体现。注意这里的 `VSS` 被标成 `DIRECTION INPUT ; USE SIGNAL ;`，这是普通版 IO LEF 的已知瑕疵，`_ecos` 版做了修正（u4-l2 会展开）。

两个可以自行验证的规律（本讲已用 grep 验证）：

1. **三库同构**：`ics55_LLSC_H7CL_ant.lef` 与 `ics55_LLSC_H7CR_ant.lef` 相对各自普通版同样只多 2 行，数值同样是 4.8e-05 与 3.1e-05，所属单元分别是 `OAI21BX6H7L`/`XOR3X6H7L` 与 `OAI21BX6H7R`/`XOR3X6H7R`，且都位于第 54822 行与第 79492 行（三库文件逐行对齐）。
2. **数值口径待确认**：按 LEF 惯例该数值单位为平方微米，但 4.8e-05 μm² 远小于引脚矩形本身的几何面积（约 0.1~0.3 μm² 量级），推测是厂商按自家口径折算的「部分面积」，其标定方式在 git 内的文件中无文档说明，**待确认**（`doc/` 目录下有数据手册 `ics55_LLSC_H7CH_TYPICAL_V1P2_T25.pdf`，但不在 git 跟踪范围的解释见 u1-l2）。

#### 4.2.4 代码实践

**实践目标**：不依赖任何 EDA 工具，用 diff/grep 精确回答「ant 版到底改了什么、改在哪个单元哪个引脚」。

**操作步骤**：

1. 在仓库根目录执行 diff（命令见 4.2.3），确认差异行号 `54822` 与 `79492`。
2. 用 grep 确认 ant 版全文件只有这 2 行天线注记：

   ```bash
   grep -n "ANTENNA" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef
   ```

3. 定位差异行所属的宏。给读者一个可复用的小脚本（**示例代码**，逻辑等价于「记住最近一次出现的 MACRO/PIN，遇到 ANTENNA 行就输出三元组」）：

   ```python
   #!/usr/bin/env python3
   # 示例代码：扫描 LEF 中的 ANTENNA 注记并定位所属 MACRO/PIN
   import sys

   def scan(path):
       macro = pin = None
       with open(path, encoding="utf-8", errors="replace") as f:
           for n, line in enumerate(f, 1):
               s = line.strip()
               if s.startswith("MACRO "):
                   macro = s.split()[1]
               elif s.startswith("PIN "):
                   pin = s.split()[1]
               elif s.startswith("ANTENNA"):
                   print(f"{path}:{n}  {macro}  {pin}  {s}")

   for p in sys.argv[1:]:
       scan(p)
   ```

   运行：`python3 scan_antenna.py <ant LEF 路径>`。

**需要观察的现象 / 预期结果**（与本次用 grep/读文件得到的结论一致）：

```text
.../ics55_LLSC_H7CH_ant.lef:54822  OAI21BX6H7H  Y  ANTENNAPARTIALMETALAREA 4.8e-05 LAYER MET1 ;
.../ics55_LLSC_H7CH_ant.lef:79492  XOR3X6H7H   Y  ANTENNAPARTIALMETALAREA 3.1e-05 LAYER MET1 ;
```

4. 把脚本换成 IO 库 LEF 再跑一遍，会得到 191 行输出（26 条 METALAREA + 165 条 CUTAREA），引脚名多为 `VSS`/`VSSIO`/`VSSA` 等电源脚。
5. 对照普通版与 ant 版的行数：`wc -l` 分别为 79580 与 79582，差值恰为 2。

**预期结果**：ant 版的全部信息量 = 2 行注记。这就是「ant 版是普通版的严格超集」的直接证据，也意味着任何读入普通版的脚本只需忽略这 2 行即可处理 ant 版。

#### 4.2.5 小练习与答案

**练习 1**：`ANTENNAPARTIALMETALAREA` 在 LEF 语法树中的位置是什么？写在 `PORT` 里面行不行？

**答案**：位于 `MACRO → PIN` 内、与 `DIRECTION`/`USE` 同级，必须在 `PORT` 之前。写在 `PORT` 里不符合 LEF 语法（`PORT` 内只放 `LAYER`/`RECT` 等几何语句）。

**练习 2**：为什么本仓库只在这两个单元、且只在输出引脚 `Y` 上加注记？

**答案**：可以确认的事实是：两处都挂在 X6 高驱动复合门的输出节点上，该节点在 cell 内部有较多 MET1 金属（引脚矩形多达 8~12 个）；工具计算 net 的金属面积时，驱动端 cell 内部金属也计入 net，所以厂商为内部金属贡献显著的两个单元显式标注。至于「为什么恰好只有这两个、为什么没有输入脚的 GATEAREA」，仓库内无文档说明，**待确认**。

**练习 3**：普通版与 ant 版能否同时读进同一个布线工具？

**答案**：不能。两者的 MACRO 集合完全相同（785 个），同时读入会重复定义宏名；应按需二选一。做天线检查时选 ant 版，纯物理抽象用途时普通版即可。

### 4.3 ANT 二极管单元

#### 4.3.1 概念说明

`ANT2H7H` 与 `ANT4H7H` 是**天线二极管单元**：一个反偏二极管，`A` 引脚接到违例网络，`VDD`/`VSS` 轨提供到衬底/电源的泄放通路，让刻蚀期间积累的电荷有地方流走。它没有逻辑功能，是「物理功能单元」，因此：

- Verilog 模型是**空的**（只声明端口，无任何行为）——仿真时它什么都不做；
- 它们**不在** `cell_list` 里——综合器永远不应该选中它们，只有布线后的物理修复阶段会用到。

两个型号的差别只是宽度：`ANT2H7H` 占 2 个 site 宽（0.4 μm），`ANT4H7H` 占 4 个 site 宽（0.8 μm），高度都是 1.4 μm（行高）。提供两种宽度是为了在行内剩余空隙不同时都能塞得进去。

#### 4.3.2 核心流程

```text
详细布线 → 天线检查发现 net N 的接收端栅极违例
   │
   ▼
在 net N 上（通常靠近接收端/在同行空隙里）插入 ANT 单元：
   A  ← 接 net N
   VDD/VSS ← 靠 ABUTMENT 轨自动对齐电源轨
   │
   ▼
重新检查：net N 的金属面积未变，但电荷有了泄放通路 → 违例消除
```

在使用 OpenROAD 这类开源工具时，对应的命令是 `repair_antennas`（可指定二极管单元名），检查命令是 `check_antennas`；这类操作都发生在 detailed placement / routing 之后，与 FILLER 单元同属流程最末段的物理处理。

#### 4.3.3 源码精读

先看 `ANT2H7H` 的宏定义：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:3048-3082](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L3048-L3082)：`ANT2H7H`，`SIZE 0.4 BY 1.4`（2 个 site 宽），`SITE core7`；唯一信号引脚 `A`（`DIRECTION INPUT ; USE SIGNAL`）带 2 个 MET1 矩形，`VDD`/`VSS` 为 `SHAPE ABUTMENT` 的对接形电源轨（注意 `VSS` 的 RECT 纵坐标为 -0.08~0.08，越过单元边界，与 u3-l2 讲的「行边界电源轨」一致）。没有 `OBS`。

再看宽版：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef:3084-3117](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef#L3084-L3117)：`ANT4H7H`，`SIZE 0.8 BY 1.4`（4 个 site 宽），引脚结构完全相同，`A` 只有 1 个更大的 MET1 矩形。

Verilog 侧印证「无逻辑功能」：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v:19-33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L19-L33)：`module ANT2H7H ( A); inout A;`，`ifdef functional` 分支为空、`else` 分支只有一个空 `specify...endspecify`——整个模块没有任何行为语句，`ANT4H7H`（第 38 行起）同样如此。

cell_list 侧印证「综合不可见」：

- [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt:1-5](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L5)：名单从 `ADDFX1H7H` 等功能单元开始；对整个文件 `grep -c "ANT"` 结果为 0，而文件共 748 行——两个 ANT 单元与 9 个 FILLER 等物理单元都在 LEF 里（785 个 MACRO）但不在 cell_list 里（785 − 748 = 37，见 u1-l2/u3-l1 的统计）。

#### 4.3.4 代码实践

**实践目标**：从 LEF 和 cell_list 两个数据源独立回答「ANT 单元长什么样、归不归综合管」。

**操作步骤**：

1. 用 grep 找到宏并读取（或直接打开文件跳到对应行）：

   ```bash
   grep -n "^MACRO ANT" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ant.lef
   # 3048:MACRO ANT2H7H
   # 3084:MACRO ANT4H7H
   ```

2. 记录两者的 `SIZE`、引脚名与 `USE`/`SHAPE`，并换算 site 数（site 宽 0.2 μm，见 u2-l2）。
3. 验证 cell_list 不含它们：

   ```bash
   grep -c ""    IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt   # 748
   grep -c "ANT" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt   # 0
   ```

4. 打开 Verilog 模型文件第 19~50 行，确认两个模块体为空。

**需要观察的现象 / 预期结果**：

| 单元 | SIZE (μm) | site 宽数 | 信号引脚 | 电源引脚 | Verilog 模型 | 在 cell_list |
| --- | --- | --- | --- | --- | --- | --- |
| ANT2H7H | 0.4 × 1.4 | 2 | A（INPUT/SIGNAL，2 个 MET1 矩形） | VDD/VSS（ABUTMENT） | 空 | 否 |
| ANT4H7H | 0.8 × 1.4 | 4 | A（INPUT/SIGNAL，1 个 MET1 矩形） | VDD/VSS（ABUTMENT） | 空 | 否 |

以上结果本讲已逐一验证。

5. 思考并回答：流程中什么时候插入它们？（见下方预期结论）

**预期结果**：在**详细布线完成、天线检查报违例之后**，由 `repair_antennas` 一类命令把 ANT 单元的 `A` 接到违例 net；这是流程的末段（与 FILLER 同时期），综合与布局阶段都不会出现 ANT 单元。

#### 4.3.5 小练习与答案

**练习 1**：`ANT2H7H` 和 `ANT4H7H` 在版图行内分别占多宽？为什么提供两种宽度？

**答案**：0.4 μm 与 0.8 μm，即 site 宽 0.2 μm 的 2 倍和 4 倍。行内剩余空隙宽度不定，提供两种宽度（并配合各种 FILLER 宽度）可以让工具在不同大小的空隙里都能找到放得下的二极管，避免为了插一个二极管而挪动一整行单元。

**练习 2**：为什么 `ANT2H7H` 的 Verilog 模型里除了端口声明什么都没有？门级仿真包含它会出问题吗？

**答案**：因为它是纯物理单元——二极管反偏、对逻辑没有任何贡献，所以模型为空（空 `specify` 表示也没有时序）。门级仿真中它表现为 `A` 上的一个悬空 inout，不影响任何逻辑值；这也是厂商把天线/填充类单元做成空模型的标准做法（与 u3-l5 讲的 TIEHI/空模型处理同属一类风格）。

**练习 3**：如果综合器把 ANT2H7H 选进了网表，会发生什么？如何从数据文件层面防止？

**答案**：综合映射的依据是 liberty，ANT 单元的时序模型不在综合可用集合里就不会被选中；cell_list 同样不含它（本仓库 cell_list 是人工维护的可综合单元清单）。所以只要不把 ANT 的 liberty 提供给综合器、不把它加进 cell_list，就不会误选。仓库目前的做法正是「LEF 有、cell_list 无」。

## 5. 综合实践

**任务：写一个「天线注记审计器」，盘点整个仓库的天线元数据。**

要求实现一个脚本（语言不限，Python 最顺手），完成三件事：

1. **ant 版差异审计**：对三套库分别 diff 普通版与 `_ant` 版 LEF，输出表格「库 / 行号 / 单元 / 引脚 / 属性 / 数值 / 层」，验证三库都是 2 行、数值同为 4.8e-05 与 3.1e-05、单元为 `OAI21BX6?H7?` 与 `XOR3X6?H7?`、引脚均为 `Y`。
2. **ANT 单元画像**：从任一 ant LEF 中提取 `ANT2H7H`/`ANT4H7H` 的 `SIZE`、site 数、引脚与 `SHAPE`，并与 cell_list、Verilog 空模型做三方交叉核对（LEF 有 / cell_list 无 / Verilog 空）。
3. **IO 库对照**：统计 IO LEF（普通版与 `_ecos` 版各跑一遍）中 `ANTENNAPARTIALMETALAREA` 与 `ANTENNAPARTIALCUTAREA` 的条数（预期分别为 26 与 165，两版相同），并按「宏 / 引脚」聚合输出前 10 个注记最多的引脚。

提示：

- 解析用 4.2.4 的状态机即可（跟踪最近一次 `MACRO`/`PIN`）；对 `SIZE x BY y` 用 `SIZE` 行取宽高，site 数 = 宽 / 0.2。
- 一个容易踩的坑：`H7CL` 的普通版 LEF 相比其 `_ant` 版**少了一个 `ADDFX1H7L` 宏**（diff 会出现整段新增），所以「只差 2 行」的结论严格来说只对 H7CH 与 H7CR 成立；审计脚本应把「整段宏新增」与「单行注记新增」区分开报告。这也是一个真实的数据质量观察，值得记进你的审计结论（该差异的成因仓库内无说明，**待确认**）。

**预期产出**：一份 Markdown 报告，包含三张表和一句结论——「本 PDK 的天线元数据集中在 IO 库（191 条、多层多 cut），标准单元库仅有两个 X6 复合门输出引脚的 MET1 注记（每库 2 条）；天线修复用的二极管单元 ANT2/ANT4 只存在于 LEF，不在 cell_list，Verilog 模型为空」。

## 6. 本讲小结

- 天线效应是**制造期**的静电损伤：逐层刻蚀时「只连栅极的金属」收集电荷，以天线比率 \( \mathrm{AR} = A_{\text{metal}}/A_{\text{gate}} \) 按层判定风险，布线后才能检查。
- LEF 用 `MACRO → PIN` 内（`PORT` 之前）的 `ANTENNAPARTIALMETALAREA`/`ANTENNAPARTIALCUTAREA` 声明单元内部的金属/过孔面积贡献；`ANTENNAGATEAREA`/`ANTENNADIFFAREA` 本仓库未使用。
- `ics55_LLSC_H7CH_ant.lef` 相对普通版**只多 2 行**：`OAI21BX6H7H` 的 `Y`（4.8e-05）与 `XOR3X6H7H` 的 `Y`（3.1e-05），均为 `LAYER MET1`；H7CL/H7CR 两库同构（注意 H7CL 普通 LEF 还少一个 `ADDFX1H7L` 宏）。
- IO 库 LEF 才是天线注记的「完整样例」：191 条（26 METALAREA + 165 CUTAREA），同一引脚按 MET2–MET5、VIA2–VIA4 逐层声明。
- `ANT2H7H`（0.4×1.4，2 site）与 `ANT4H7H`（0.8×1.4，4 site）是天线二极管：LEF 有定义、cell_list 不含、Verilog 模型为空；在详细布线后的天线修复阶段由工具插入。
- ant 版与 `_ecos` 版是正交的两个变体维度：前者加天线注记，后者改几何/电源引脚/RC，使用时各自与普通版二选一。

## 7. 下一步学习建议

- 下一讲 u3-l5 将转向 `verilog/ics55_LLSC_H7CH.v`：本讲你已经见过 ANT2/ANT4 与 `\`ifdef functional` 的空模型，下一讲会系统讲解门原语建模、`\`celldefine`/`\`timescale`/`specify` 模板和带条件延迟的模型。
- 若想继续深挖天线方向：等 u6-l1 用 OpenROAD 读入 `_ecos` 版 LEF 后，可以尝试 `check_antennas` / `repair_antennas`（二极管指定为 `ANT2H7H` 或 `ANT4H7H`），把本讲的数据与真实工具行为对上。
- 建议顺带阅读 u4-l2（IO LEF 与 IOSite）：本讲提到的 `P65_1233_CORNER` 引脚 `USE SIGNAL` 瑕疵及其 `_ecos` 修正将在那一讲展开。
