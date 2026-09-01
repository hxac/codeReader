# u4-l2 IO LEF：PAD 宏、IOSite 与 ecos 适配

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段读懂一个 PAD 类 LEF 宏的几何与对称性描述（CLASS / SIZE / SYMMETRY / ORIGIN / FOREIGN / SITE）。
2. 解释 SITE `IOSite` 与 `IOCorner` 的定义内容，以及它们为什么只出现在 `_ecos` 版、缺失后开源工具会发生什么。
3. 用 pad 尺寸数据（pad 65μm、拐角 130μm、FILLER 九档宽度）估算一条边能摆多少个 pad，并验证缝隙能否被 FILLER 精确填满。

本讲是 u4-l1（IO 单元家族盘点）的物理侧续篇：u4-l1 回答「有哪些 pad、各是干什么的」，本讲回答「这些 pad 在 LEF 里长什么样、怎么拼成一个 pad ring」。

## 2. 前置知识

- **LEF 抽象视图**（u3-l2）：LEF 宏是单元版图的「抽象骨架」，只保留布线器需要的边界、引脚矩形和障碍，不含晶体管。本讲把同一套方法用到 IO 库。
- **SITE 与行式布局**（u2-l2）：SITE 是布局的最小格点。标准单元用 `core7`（0.2 × 1.4 μm）成行摆放；本讲会看到 pad 也有自己的 SITE（`IOSite`），思想相同、尺寸量级完全不同。
- **pad ring**（u4-l1）：IO 单元沿芯片四边首尾相接围成一圈，四个角放 CORNER 拐角单元，缝隙用 FILLER 补齐。本讲用 LEF 里的真实尺寸把这个环「算」出来。
- **_ecos 变体**（u2-l3、u3-l3）：仓库为开源工具链维护的平行版本文件，与原版内容基本一致、只做针对性适配。本讲是这套思想的 IO 篇。
- **压焊（bonding）**：芯片封装时用金线把 pad 上的金属开孔连到封装引脚。pad 的压焊开口在物理上位于核心区外侧，这个事实决定了本讲 ORIGIN/FOREIGN 偏移的方向。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef` | IO 库普通版 LEF，63197 行、23 个 PAD 宏。本讲的主分析对象 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef` | IO 库 _ecos 版 LEF，63209 行。相对普通版多 12 行（两个 SITE 定义）并修正 48 处引脚属性 |
| `prtech/techLEF/N551P6M.lef` | 工艺 LEF。本讲只用到其中一个事实：它定义了 CoreSite/core7/core9 三个 SITE，**没有** IOSite——这是理解 ecos 版为何要补 SITE 的关键 |

文件名里的 `1P6M1TM` 表示 1 层多晶、6 层金属、1 层厚金属（top metal）的工艺叠层，与 u2-l1 讲过的金属栈对应。

## 4. 核心概念与源码讲解

### 4.1 PAD 宏几何结构

#### 4.1.1 概念说明

IO LEF 与标准单元 LEF（u3-l2）共享同一套语法，但描述对象换成了压焊盘。一个 PAD 宏要回答四个问题：

1. **占多大地方**——`SIZE` 给出外框（outline）；
2. **能不能翻转旋转**——`SYMMETRY`，pad ring 四条边要用同一套单元拼出来，必须允许镜像和 90° 旋转；
3. **抽象坐标和版图坐标怎么对齐**——`ORIGIN` / `FOREIGN`，因为压焊开口物理上伸在核心区外面，两套坐标系差了一个固定偏移；
4. **摆在什么格点上**——`SITE`，与标准单元引用 `core7` 同理，pad 引用 `IOSite`。

#### 4.1.2 核心流程

读取一个 PAD 宏头部信息的顺序：

```text
MACRO <名字>
  ├─ CLASS PAD        → 声明这是压焊盘类单元（区别于 CORE / BLOCK）
  ├─ ORIGIN dx dy     → 宏原点相对外框的偏移
  ├─ FOREIGN <gds名> dx dy → 对应的 GDS 版图单元及其坐标平移
  ├─ SIZE w BY h      → 外框尺寸（布线器据此预留面积）
  ├─ SYMMETRY X Y R90 → 允许 X 镜像、Y 镜像、90° 旋转
  └─ SITE IOSite      → 摆放格点
之后是 PIN / OBS，语法与标准单元 LEF 完全一致（u3-l2）
```

23 个宏按尺寸分三档：

| 尺寸 | 宏 | 说明 |
| --- | --- | --- |
| 130 × 130 | CORNER、PWE | 拐角；晶振 pad（双压焊端子，故双倍宽） |
| 65 × 130 | CUT、PAR、PAR_5、PBMUX、VDD1/VDD1A/VDD3/VDDIO3、VSS1/VSS1A/VSS3/VSSIO3 | 常规 pad，宽 65、高 130 |
| 0.005 ~ 50 × 130 | FILLER0005/001/01/1/2/5/10/20/50（九档） | 纯填充，宽度呈 1-2-5 面值系列 |

#### 4.1.3 源码精读

先看文件头与第一个宏 CORNER（普通版）：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L15-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L15-L25)

这几行是：`VERSION 5.7` 声明 LEF 版本，`BUSBITCHARS`/`DIVIDERCHAR` 定义总线位与层次分隔符；随后 CORNER 宏头部给出了全部六个关键字段——`CLASS PAD`、`ORIGIN 20 20`、`FOREIGN ... -20 -20`、`SIZE 130 BY 130`、`SYMMETRY X Y R90`、`SITE IOSite`。

对比一个常规 pad（CUT 切割单元）的头部：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L51585-L51591](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L51585-L51591)

CUT 的 ORIGIN/FOREIGN 是 `0 20` / `0 -20`，只有 y 方向偏移 20μm；而 CORNER 是 `20 20` / `-20 -20`，x、y 双向偏移——拐角单元朝芯片外侧的两个方向都外扩。

**20μm 偏移的物理解释**藏在文件尾部的 OBS 里。看 VSSIO3 的障碍区：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L63190-L63195](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L63190-L63195)

`RECT 0 -20 65 -5` 出现了**负 y 坐标**：压焊开口（含 RDL 层）位于 y = −20 到 −5 之间，越出了 `SIZE` 框（y = 0..130）的下边界，向芯片边缘方向伸出。`ORIGIN 0 20` 与 `FOREIGN 0 -20` 成对出现，正是把「版图比抽象框多出的这 20μm」编码进两套坐标系的平移参数：GDS 版图单元的坐标原点相对 LEF 抽象坐标下移了 20μm。布线器只按 SIZE 框预留核心区一侧的空间，压焊手指区悬在框外，不会与内部布线冲突。

再看宽度系列的两个极端——最小的 FILLER0005：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L52474-L52481](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52474-L52481)

注意这个宏**只有 8 行、没有任何 PIN**——0.005μm 宽的单元连一个引脚都摆不下，是纯粹的「占位胶」。而稍大的 FILLER001 就带有 VDD/VDDIO/VSS/VSSIO 四个电源地引脚：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L52483-L52516](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52483-L52516)

FILLER001 的 VDD 引脚在 MET2~MET5 四层上各有一个贯穿全高的竖条矩形（`RECT 0 99.5 0.01 107.5`）——填充单元虽然没有信号功能，却要**接续 pad ring 的电源轨道**，这正是 u4-l1 讲过的「环上电源轨道不能断」在 LEF 里的落点。

最后是双倍宽的 PWE（晶振 pad，XIN/XOUT 两个压焊端子）：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L55559-L55565](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L55559-L55565)

`SIZE 130 BY 130`，宽度是常规 pad 的两倍——一个宏里装两个压焊端子。

#### 4.1.4 代码实践

**实践目标**：用一条 grep 管线提取全部 23 个宏的名字与尺寸，验证 4.1.2 的三档分类。

**操作步骤**（在仓库根目录执行）：

```bash
grep -A4 '^MACRO' IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef \
  | grep -E '^MACRO|SIZE'
```

`-A4` 取 MACRO 行及其后 4 行（CLASS、ORIGIN、FOREIGN、SIZE），再过滤出 MACRO 与 SIZE 两类行，得到「宏名 × 尺寸」对照。

**需要观察的现象**：输出应为 23 对记录；逐条核对宽度只有 130、65、以及 0.005/0.01/0.1/1/2/5/10/20/50 这几种取值，高度全部为 130。

**预期结果**（本人已用等价检索核实）：23 个宏；其中 130 宽 2 个（CORNER、PWE）、65 宽 12 个、FILLER 九档 9 个；`END LIBRARY` 在文件末行 L63197。脚本本身的输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PAD 宏需要 `SYMMETRY X Y R90`，而标准单元（u3-l2）只写 `SYMMETRY X Y`？

**答案**：pad ring 有四条边，北边的 pad 摆到东边必须旋转 90°；标准单元在行内只做水平镜像翻转（X）和上下翻转（Y），不会旋转，所以不声明 R90。若工具尊重 SYMMETRY 声明，给标准单元加 R90 反而是非法操作。

**练习 2**：`FOREIGN P65_1233_CUT 0 -20` 中 `-20` 的含义是什么？删掉这个偏移会导致什么？

**答案**：GDS 版图单元的坐标原点相对 LEF 抽象坐标向 y 负方向（芯片边缘侧）平移 20μm，对应压焊开口区 `RECT 0 -20 65 -5` 悬在 SIZE 框外。删掉偏移后，做 LVS/版图对照时 GDS 版图会整体「缩进」核心区 20μm，与 LEF/DEF 里摆放的位置对不上，压焊开口将落到核心区布线域内。

**练习 3**：FILLER0005 为什么一个引脚都没有？

**答案**：它的宽度只有 0.005μm，小于任何工艺图形的最小宽度要求，只用于吸收 pad ring 收尾时不足 0.01μm 的残余缝隙；电源轨道的接续由相邻的较大 FILLER 承担。

### 4.2 SITE IOSite / IOCorner

#### 4.2.1 概念说明

SITE 是布局合法化的最小格点（u2-l2 讲过 `core7`：宽 0.2、高 1.4）。pad ring 同样需要格点，但量级完全不同：**pad 的 SITE 高度等于 pad 高度 130μm，宽度则细到 0.005μm**——宽度取得这么小，是为了让 FILLER0005（最小面值）恰好占一个格点，任何缝隙都能被格点整除。

本模块要解释的核心现象是：**普通版 IO LEF 的 23 个宏全部写着 `SITE IOSite ;`，但整个仓库没有任何地方定义过 IOSite**——工艺 LEF 只定义了 CoreSite/core7/core9 三个（见 [prtech/techLEF/N551P6M.lef:L654-L668](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L654-L668)），普通版 IO LEF 自身一条 `SITE` 定义语句都没有（可全文检索 `^SITE` 验证）。引用了却未定义，这就是 `_ecos` 版要补的第一块拼图。

#### 4.2.2 核心流程

工具读入 LEF 时对 SITE 的处理顺序：

```text
read_lef(techLEF)   → 登记 CoreSite / core7 / core9
read_lef(IO LEF)    → 逐个解析 MACRO
    ├─ 遇到 SITE IOSite → 查登记表
    ├─ 普通版：查不到 → 引用悬空，pad 无法合法化摆放
    └─ ecos 版：文件头部已定义 IOSite/IOCorner → 查到，正常建行
```

ecos 版补的两个 SITE：

```text
SITE IOSite              SITE IOCorner
  SYMMETRY x y r90 ;       SYMMETRY x y r90 ;
  CLASS pad ;              CLASS pad ;
  SIZE 0.005 BY 130.000 ;  SIZE 130.000 BY 130.000 ;
END IOSite                END IOCorner
```

注意两个尺寸的来源：`IOSite` 的宽 0.005 正是 FILLER0005 的宽度（最小面值 = 格点宽度）；`IOCorner` 的 130 × 130 正是 CORNER 宏的尺寸——拐角格点一格装一个拐角单元。

#### 4.2.3 源码精读

ecos 版文件头，紧跟 `DIVIDERCHAR` 之后、第一个 MACRO 之前：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L19-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L19-L29)

这段（ecos 版独有，共 12 行）定义了 `IOSite`（0.005 × 130）与 `IOCorner`（130 × 130），二者均声明 `SYMMETRY x y r90` 与 `CLASS pad`。

而普通版同一位置是直接从文件头跳到 CORNER 宏的：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L15-L19](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L15-L19)

普通版 `DIVIDERCHAR` 之后第 4 行就是 `MACRO P65_1233_CORNER`——中间没有任何 SITE 定义，但该宏第 25 行（本章 4.1.3 第一条链接）仍然写着 `SITE IOSite ;`。全文件 23 处这样的引用全部悬空。

**为什么 SITE 定义可以放在 IO LEF 里而不是 tech LEF 里？** LEF 解析器把一次会话读入的所有文件（tech LEF + 各 cell LEF）的 SITE 登记在同一张表里，定义在哪个文件并不重要，只要「先定义、后引用」。ecos 版选择把 IOSite 定义放进 IO LEF 头部，使这个文件自洽——单独读它也不缺定义；代价是与 tech LEF 的 site 表共存（core7 与 IOSite 互不冲突）。

**缺 SITE 的实际后果**（定性描述，具体报错文本待本地验证）：以 OpenROAD 为例，读入引用未定义 SITE 的宏时，或直接报「未定义 site」类错误，或静默丢弃 site 归属；两种情况下 pad ring 自动生成（如 IO 规划器按格点摆放 PAD 类宏）都无法执行，只能手工摆。

#### 4.2.4 代码实践

**实践目标**：验证「普通版引用、ecos 版定义」这一论断，并找出两版文件长度的差值来源。

**操作步骤**：

```bash
# 1. 两个文件里以行首 SITE 开头的定义语句各有多少条？
grep -c '^SITE' IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef
grep -c '^SITE' IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef

# 2. 引用语句（宏体内、带缩进）各有多少条？
grep -c '^  SITE IOSite ;' IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef
grep -c '^  SITE IOSite ;' IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef
```

**需要观察的现象**：定义语句普通版 0 条、ecos 版 2 条；引用语句两版各 23 条。

**预期结果**（已核实）：两文件行数差 63209 − 63197 = 12，恰等于 ecos 头部新增的 12 行（两个 SITE 定义各 5 行 + 1 空行 + 分隔空行）。grep 输出格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：IOSite 的宽度为什么取 0.005 而不是 1 或 0.2？

**答案**：格点宽度必须能整除所有可能要摆的宏宽度。FILLER 面值下探到 0.005，所以格点必须 ≤ 0.005；取等号让最小面值恰好占一格。取 1 或 0.2 会使 FILLER0005/001/01 无法落在合法格点上。

**练习 2**：`IOCorner`（130 × 130）与 `IOSite`（0.005 × 130）为什么需要两个 SITE 而不是一个？

**答案**：拐角单元是正方形，在环的转角处占一整格 130 × 130；常规 pad 与 FILLER 在边内按 0.005 细格点排布。两种排布粒度差异达四个数量级，分成两个 SITE 各自描述，工具在拐角与边内分别合法化。（也可以只用 IOSite 覆盖一切——130 是 0.005 的整数倍——但显式的 IOCorner 向工具表达了「拐角一格一单元」的意图。）

**练习 3**：如果把你自己的 tech LEF 与这个 IO LEF 一起读入，`core7` 与 `IOSite` 会冲突吗？

**答案**：不会。SITE 按名字登记，`core7`（0.2 × 1.4，CLASS CORE）与 `IOSite`（0.005 × 130，CLASS PAD）名字不同、类别不同，各自服务于标准单元行和 pad ring，互不干扰。

### 4.3 ecos 版修正内容

#### 4.3.1 概念说明

ecos 版相对普通版的全部差异只有三类（已用 diff 全量核实）：

| 类别 | 数量 | 内容 |
| --- | --- | --- |
| 新增 SITE 定义 | 2 个 | IOSite、IOCorner（4.2 已讲） |
| `USE SIGNAL` → `USE GROUND` | 46 处 | 全部落在**地引脚**上：VSS、VSSA、VSSIO、VSS1 |
| `DIRECTION INPUT` → `DIRECTION OUTPUT` | 2 处 | PBMUX 的 C、PWE 的 XC——两个「指向核心区的输出」引脚 |

要理解这三类修正的动机，回忆 u3-l2 讲过的 `USE` 属性的作用：它告诉布线器这个引脚属于信号网络还是电源地网络。开源工具（如 OpenROAD）会按 `USE POWER/GROUND` 识别电源网络并做 PDN 连通——若地引脚被错标成 `SIGNAL`，电源地网络就识别不完整，PDN 生成与连通性检查都会漏掉 pad 侧的地。普通版里 VDD 引脚从一开始就是对的（`USE POWER`），错的全是地一侧，ecos 版正是把这一侧补齐。

`DIRECTION` 的两处修正则是另一种错：C（PBMUX 的核心侧输出）与 XC（PWE 的核心侧输出）是 pad 送进核心区的信号，方向应为 OUTPUT，普通版错标 INPUT。这类错误影响综合/形式验证工具对 IO 端口方向的推断。

#### 4.3.2 核心流程

用一条 diff 命令即可得到全部差异。整体结构：

```text
diff 普通版 ecos版
  18a19,30        ← 头部插入 12 行（两个 SITE 定义）
  46 × "USE SIGNAL → USE GROUND"
  2  × "DIRECTION INPUT → DIRECTION OUTPUT"
（再无其他差异——两版其余 6 万余行逐字节相同）
```

46 处 USE 修正按宏分组（`/` 前为宏名，后为被修正的引脚；已逐一核对行号）：

| 宏 | 被修正为 USE GROUND 的引脚 | 处数 |
| --- | --- | --- |
| CORNER | VSS、VSSIO | 2 |
| CUT | VSS、VSSA、VSSIO | 3 |
| FILLER001 / 01 / 1 / 2 / 5 / 10 / 20 / 50（共 8 个） | 各 VSS、VSSIO | 16 |
| PAR、PAR_5 | 各 VSS、VSSA | 4 |
| PBMUX、PWE | 各 VSS、VSSIO | 4 |
| VDD1、VDD3、VDDIO3 | 各 VSS、VSSIO | 6 |
| VDD1A | VSS、VSSA | 2 |
| VSS1 | VSS、VSS1、VSSIO | 3 |
| VSS1A | VSS、VSSA | 2 |
| VSS3、VSSIO3 | 各 VSS、VSSIO | 4 |
| 合计 | | **46** |

注意两个细节：FILLER0005 因无引脚（4.1.3）不参与；VSS1 宏自带名为 `VSS1` 的引脚（核心域专用地，参见 u4-l1 的电源域划分），所以它有 3 处修正。

规律总结：**凡是名字以 VSS 开头的引脚，普通版一律错标 `DIRECTION INPUT ; USE SIGNAL`，ecos 版统一改为 `USE GROUND`（DIRECTION 保持 INPUT 未动）**；而 VDD 引脚普通版本来就写 `DIRECTION INOUT ; USE POWER`，无需修正。

#### 4.3.3 源码精读

先看普通版的「错误现场」——CORNER 的 VSS：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L12091-L12094](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L12091-L12094)

`PIN VSS` 被标成 `DIRECTION INPUT ; USE SIGNAL`——一个纯地引脚被当成了普通输入信号。对照同宏的 VDD（[L26-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L26-L28)）：`DIRECTION INOUT ; USE POWER`，一正一错，反差鲜明。

同一位置在 ecos 版里：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef:L12103-L12106](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L12103-L12106)

`USE GROUND` 修正后的同一引脚（ecos 版因头部多 12 行，行号整体 +12）。电源地网络识别从此完整。

再看 FILLER001 的 VSS（普通版）：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L52531-L52534](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L52531-L52534)

填充单元的地引脚同样错标 `USE SIGNAL`，ecos 版对应位置为 [L52543-L52546](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L52543-L52546)。若不修正，由 FILLER 接续的电源轨道段会被 PDN 工具当作悬空信号线处理。

两处 DIRECTION 修正之一，PBMUX 的 C 引脚（普通版）：

[IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef:L54911-L54916](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L54911-L54916)

C 是 PBMUX（u4-l1 讲过的双向 GPIO）送往核心区的输出端，普通版却写 `DIRECTION INPUT`；ecos 版在 [L54923-L54928](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM_ecos.lef#L54923-L54928) 改为 `DIRECTION OUTPUT`。另一处同型修正是 PWE 的 XC（普通版 [L55831-L55835](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L55831-L55835)，ecos 版 L55843 起）：晶振 pad 送核心区的时钟输出。两个引脚的共同点：**名字里带 C（Core）、方向朝核心区**。

顺带一提，上面引用的 C 引脚代码里还能看到 `ANTENNAPARTIALCUTAREA` 注记——u3-l4 讲过的天线属性在 IO 库中有 191 条，是全仓库最完整的天线标注样例。

#### 4.3.4 代码实践

**实践目标**：亲手复现「46 处 USE GROUND + 2 处 DIRECTION OUTPUT」的完整清单，并按宏分组统计。

**操作步骤**：

```bash
cd IP/IO/ICsprout_55LLULP1233_IO_251013/lef

# 1. 全量 diff，只看「修改」型 hunks（其余是新增 SITE 的 12 行）
diff ICSIOA_N55_3P3_1P6M1TM.lef ICSIOA_N55_3P3_1P6M1TM_ecos.lef

# 2. 统计各类差异数量
diff ICSIOA_N55_3P3_1P6M1TM.lef ICSIOA_N55_3P3_1P6M1TM_ecos.lef | grep -c 'USE GROUND'
diff ICSIOA_N55_3P3_1P6M1TM.lef ICSIOA_N55_3P3_1P6M1TM_ecos.lef | grep -c 'DIRECTION OUTPUT'
```

diff 输出的行号是普通版坐标；要定位属于哪个宏哪个引脚，可对每个行号回查上文（例如 12093 行属于 CORNER 的 VSS）：

```bash
sed -n '12091,12094p' ICSIOA_N55_3P3_1P6M1TM.lef
```

**需要观察的现象**：diff 输出仅三种形态——头部 12 行插入、46 个 `USE SIGNAL→USE GROUND` 单行替换、2 个 `DIRECTION INPUT→DIRECTION OUTPUT` 单行替换；除此之外两文件完全一致。

**预期结果**（已核实）：`grep -c 'USE GROUND'` 输出 46，`grep -c 'DIRECTION OUTPUT'` 输出 2；被修改引脚全部以 VSS 开头（46 处）或为 C/XC（2 处）。你手工整理的分组表应与 4.3.2 的表格一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ecos 版把 VSS 的 `USE SIGNAL` 改成 `USE GROUND`，却把 `DIRECTION INPUT` 原样保留，而不是像 VDD 那样改成 `DIRECTION INOUT`？

**答案**：`USE` 决定网络归属（电源地识别），是开源 PDN 工具的硬需求，必须改；`DIRECTION` 对电源地引脚只是描述性信息，工具不据它做电源网络连通，改动它超出「最小适配」原则。ecos 变体一贯只修影响开源工具正确性的字段（对照 u2-l3：tech LEF 只补 RC、u3-l3：cell LEF 只补电源轨道与高层引脚）。

**练习 2**：46 处修正里为什么没有任何一个 VDD/VDDA/VDDIO 引脚？

**答案**：普通版中电源引脚从一开始就正确（`DIRECTION INOUT ; USE POWER`，如 VDD1 宏的 VDD 引脚 L56395-L56397）；原始数据的错误只发生在地一侧，ecos 版是对错误的最小修复集，而非全面重写。

**练习 3**：假如你在普通版上跑 OpenROAD 的 PDN，只用 `USE` 信息识别电源地网络，会出现什么现象？

**答案**：VDD 网络能通过 pad 侧的 VDD/VDDIO 引脚连到环上轨道，但 VSS/VSSIO 网络在 pad 一侧「无源」——所有 pad 的地引脚被当成信号，PDN 无法把核心区的地网络延伸到 pad ring 的地轨道上，地连通性检查报未连接。这正是 ecos 版修这 46 处的直接动机。

## 5. 综合实践

把本讲三块内容（pad 尺寸、IOSite 格点、ecos 修正）串成一个「pad ring 规划器」小工具。

**任务**：写一个 Python 脚本 `padring.py`（示例代码，非项目原有文件），输入芯片边长 \( L \)（μm），输出一条边的 pad 摆放清单。步骤：

1. **解析 LEF**：读 `ICSIOA_N55_3P3_1P6M1TM.lef`，用正则 `^MACRO (\S+)` 与 `^  SIZE (\S+) BY (\S+)` 提取全部 23 个宏的宽高（即 4.1.4 的 grep 逻辑的 Python 版）。
2. **计算边内容**：每条边两端各被拐角占去 130，可用长度 \( U = L - 260 \)；设常规 pad 宽 \( w_p = 65 \)，则每边最多摆 \( n = \lfloor U / w_p \rfloor \) 个 pad，余隙 \( r = U - 65n \)。
3. **FILLER 分解**：对余隙 \( r \) 用面值系列 \(\{50, 20, 10, 5, 2, 1, 0.1, 0.01, 0.005\}\) 做贪心分解（每档面值 ≥ 上一档的 2 倍，贪心即最优），输出 FILLER 清单；若 \( r \) 不能被 0.005 整除则报「该边长下缝隙无法精确填满，需调整边长或 pad 数」。
4. **格点校验**：检查清单中所有单元宽度之和等于 \( U \)，且每个摆放坐标都是 0.005 的整数倍（IOSite 格点，4.2）。
5. **附加报告**：对 4.3 的 diff 结果做分组统计，把 46 处 `USE GROUND` 修正按宏名列在输出末尾，作为该 ring 使用的 LEF 版本说明（提醒使用者应配 _ecos 版）。

**验证算例**（手工可核）：

- \( L = 2000 \)：\( U = 1740 \)，\( n = 26 \)，\( r = 50 \) → 26 个 pad + 1 个 FILLER50；
- \( L = 2020 \)：\( U = 1760 \)，\( n = 27 \)，\( r = 5 \) → 27 个 pad + 1 个 FILLER5；
- \( L = 1980 \)：\( U = 1720 \)，\( n = 26 \)，\( r = 30 \) → 26 个 pad + FILLER20 + FILLER10。

**预期结果**：脚本对上述三个边长输出与手算一致；整环规模为 4 个 CORNER + \( 4n \) 个常规 pad + 四份 FILLER 分解。脚本运行输出待本地验证。

## 6. 本讲小结

- IO LEF 的 23 个宏全部 `CLASS PAD`，尺寸三档：130×130（CORNER/PWE）、65×130（常规 pad）、0.005~50×130（九档 FILLER）；`SYMMETRY X Y R90` 支撑四边复用。
- `ORIGIN/FOREIGN` 的 20μm 偏移对应压焊开口越出 SIZE 框（OBS 中 y = −20..−5 的负坐标矩形），把抽象坐标与 GDS 版图坐标对齐。
- 普通版 23 个宏都引用 `SITE IOSite` 却无处定义；ecos 版在文件头补上 `IOSite`（0.005×130，格点宽 = 最小 FILLER 宽）与 `IOCorner`（130×130）共 12 行，使文件自洽。
- ecos 版另修正 48 处引脚属性：46 处地引脚 `USE SIGNAL→USE GROUND`（影响电源地网络识别与 PDN）、2 处核心侧输出 `DIRECTION INPUT→OUTPUT`（PBMUX 的 C、PWE 的 XC）。
- pad ring 可用「边长减两拐角、按 65 分摊、1-2-5 面值贪心补缝」一阶估算，全部宽度均是 0.005 格点的整数倍保证了可分解性。

## 7. 下一步学习建议

下一讲 u4-l3「IO 电学模型」将横向对照同一批 pad 的 liberty、Verilog 与 CDL 三种视图，看电学参数（双电压域、驱动电流）如何补充本讲的纯几何抽象；学完后可回到 u6-l1，用 OpenROAD 实际读入 `_ecos` 版 tech LEF + IO LEF，验证本讲预判的 site 与报错行为。延伸阅读：`prtech/techLEF/N551P6M_ecos.lef` 的 L658-L675（三个 core SITE 与 IO LEF 两个 pad SITE 同存的完整 site 表）。
