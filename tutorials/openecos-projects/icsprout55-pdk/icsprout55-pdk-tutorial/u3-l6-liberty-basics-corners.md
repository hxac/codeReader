# u3-l6 Liberty 时序库基础（以 IO 库为例）

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂一份 liberty（`.lib`）文件的库头：单位、标称条件（nom_process / nom_temperature / nom_voltage）、slew 与延时的测量阈值、`delay_model : table_lookup` 的含义。
2. 只看文件名就解出一份 liberty 的工艺角（tt/ff/ss）、核电压、IO 电压与温度，并用文件内容反查验证。
3. 独立解析 `cell / pin / capacitance / timing` 四层结构：知道 PAD 引脚的三个电容值（capacitance / rise_capacitance / fall_capacitance）分别是什么、二维查找表（NLDM 表）的两个轴是什么、一条 timing 弧是怎么组织的。
4. 写脚本对比 tt / ff / ss 三个 corner 下同一单元的电容差异，并理解为什么开源流程里「选哪个 corner 的 liberty」直接决定时序结论。

本讲全部数据取自仓库自带的 IO 库 liberty（每份仅 1150 行，是最适合入门的 liberty 样本）；标准单元库的 liberty 不在 git 内，需 `make unzip` 下载，本讲综合实践会走到那一步。

## 2. 前置知识

**liberty 是什么。** 在 u1-l1 我们建立过「PDK = 多视图文件族」的认知：LEF 告诉布线器单元「长什么样」（几何抽象），liberty 告诉综合器和时序分析器单元「跑多快、耗多少电、输入端有多重」。liberty 由 Synopsys 定义、如今是事实行业标准格式（ liberty 文件规范 ），开源工具链同样消费它：yosys 综合映射用它，OpenSTA / OpenROAD 做时序分析用它。

**延迟不是常数。** 一个门从输入变化到输出变化所花的时间，取决于两件事：输入信号本身有多「陡」（输入转换时间，slew / transition），以及输出要驱动多大的负载电容。所以 liberty 里不存「延迟 = 0.1ns」这样的常数，而是存一张**二维查找表**：横轴负载电容、纵轴输入 slew，表里填延迟。工具在两点之间做**双线性插值**。这种建模方式俗称 NLDM（Non-Linear Delay Model）。

**corner（工艺角）是什么。** 晶圆制造有偏差，同一张版图流出来的芯片有快有慢；电压和温度也会改变晶体管速度。于是 PDK 把「工艺快慢 × 电压 × 温度」组合成若干个**corner**，每个 corner 单独给一份 liberty：

- `tt`（typical-typical）：典型工艺，用于功耗估计、功能仿真；
- `ss`（slow-slow）：最慢 —— 慢工艺 + 低电压 + 高温，用来查 **setup（建立时间）违例**；
- `ff`（fast-fast）：最快 —— 快工艺 + 高电压 + 低温，用来查 **hold（保持时间）违例**。

一句话记忆：**setup 用最悲观的慢角保命，hold 用最快角防钻空子，tt 做日常估算。**

**单位先行。** liberty 头部声明所有数值的单位。本库：时间 `1ns`、电容 `1pF`、电压 `1V`、电流 `1mA`、漏电功耗 `1nW`。所以后文看到 `capacitance : 2.726000` 就是 2.726 pF，`cell_rise` 表里的 `1.0864291` 就是 1.086 ns。

**双线性插值**（读表时会用到）：对两个轴各做一次线性插值，延迟

\[
\begin{aligned}
t(x,y) = &\ t_{11}\cdot\frac{x_2-x}{x_2-x_1}\cdot\frac{y_2-y}{y_2-y_1}
        + t_{21}\cdot\frac{x-x_1}{x_2-x_1}\cdot\frac{y_2-y}{y_2-y_1} \\
        + &\ t_{12}\cdot\frac{x_2-x}{x_2-x_1}\cdot\frac{y-y_1}{y_2-y_1}
        + t_{22}\cdot\frac{x-x_1}{x_2-x_1}\cdot\frac{y-y_1}{y_2-y_1}
\end{aligned}
\]

其中 \((x_1,x_2)\)、\((y_1,y_2)\) 是包围查询点的相邻网格坐标，\(t_{11}\dots t_{22}\) 是对应四个表项。

## 3. 本讲源码地图

IO 库的 `liberty/` 目录下共有 **6 份 liberty**，全部 1150 行、结构完全平行，只有数值不同：

| 文件 | 角色 |
| --- | --- |
| [ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib) | 典型角（本讲精读的主样本） |
| [ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib) | 快角 / 高温 |
| [ICSIOA_N55_3P3_ss_1p08_2p97_125c.lib](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ss_1p08_2p97_125c.lib) | 慢角 / 高温 |
| ICSIOA_N55_3P3_ff_1p32_3p63_m40c.lib / ICSIOA_N55_3P3_ss_1p08_2p97_m40c.lib / ICSIOA_N55_3P3_ff_1p32_3p63v_0c.lib | 另外三个 corner，用于 4.2 节解码练习 |

配套文件：

- [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile) —— 标准单元 liberty 的下载与解压规则（L11-L13、L62-L66）；
- [.gitignore](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1) 第 1 行解释了为什么标准单元 liberty 不在 git 里；
- [cell_list/ICSIOA_N55_3P3.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt) —— 23 个单元名单，用来和 liberty 的 12 个 cell 对账。

> 提示：这些 `.lib` 是纯文本，但部分编辑器/工具会按扩展名误判。仓库里直接用 `grep -n "" <file>` 带行号阅读最方便。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**4.1 库头结构**、**4.2 corner 命名规则**、**4.3 cell/pin/capacitance**、**4.4 查找表与 timing 弧**。

### 4.1 liberty 库头结构

#### 4.1.1 概念说明

liberty 是**分组嵌套语法**：`library(...) { cell(...) { pin(...) { timing(...) {...} } } }`，用大括号分层、用 `属性 : 值;` 赋值。最外层 `library` 组的头部（第一个 cell 之前）声明**全库通用的「度量衡」与「测量口径」**：

- 单位：时间、电容、电压、电流、电阻、漏电功耗各是什么单位；
- 标称条件：nom_process / nom_temperature / nom_voltage —— 本份数据是在什么环境下表征（characterize）出来的；
- 测量口径：slew 阈值（转换时间从波形的百分之几量到百分之几）、延时阈值（从输入的百分之几量到输出的百分之几）；
- 计算模型：`delay_model : table_lookup`，即 NLDM 查找表模型。

这些头部信息必须**先于任何数值**被理解——同样写 `2.726`，电容单位是 pF 还是 fF，含义差一千倍。

#### 4.1.2 核心流程

工具读入一份 liberty 的顺序：

1. 读 `library` 名与 `define()` 自定义属性；
2. 读单位与阈值，建立数值解释框架；
3. 读 `lu_table_template` / `power_lut_template` 表模板（只定维度与形状）；
4. 逐个读 `cell` → `pin` → `timing` / `internal_power`，把实表的数值按单位换算入库；
5. 读文件尾部的 `default_*` 收尾。

#### 4.1.3 源码精读

以 tt 角文件为样本。文件开头 1–15 行是 Apache-2.0 license header（u1-l1 讲过逐文件保留要求），第 17 行进入正题：

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L17-L36](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L17-L36) —— 库名 `ICSIOA_N55_3P3_tt_1p2_3p3_25c`（与文件名一致）；5 条 `define(...)` 声明本库用到的**自定义属性**（后面 4.4 会看到 `three_state_pulldn_res` 就是在这里「注册」的）；`delay_model : "table_lookup"` 宣布用查找表模型；`nom_process/nom_temperature/nom_voltage = 1.0 / 25.0 / 3.3` 给出标称条件；随后 6 行把电容（pF）、电压（V）、电流（mA）、时间（ns）、上拉电阻（kohm）的单位全部钉死。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L46-L54](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L46-L54) —— **测量口径**：slew 用 10%–90% 量（`slew_lower/upper_threshold_pct_rise/fall`），输入输出延时都用 50% 交叉点量（`input/output_threshold_pct_*`）。也就是：**延迟 = 输入越过 50% 的时刻 到 输出越过 50% 的时刻**；转换时间 = 波形从 10% 爬到 90% 的时间。不同库口径不同（现代库常用 30%–70%），跨库对比时必须先看这里。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L61-L72](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L61-L72) —— `output_voltage` / `input_voltage` 两组表：vol/voh = 1.42/1.88 V（输出低/高电平），vil/vih = 1.42/1.88 V（输入判 0/判 1 阈值），摆幅区间 vomin/vomax = 0–3.3 V。注意本库的 vol/voh 与 vil/vih 数值完全相同且并非「0/3.3 满摆幅」，这组数值的具体物理口径（推测与 3.3V IO 域的中间阈值约定有关）**待确认**，读库时先照单全收。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L73-L122](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L73-L122) —— `wire_load "ForQA"` 与 `wire_load_selection`：这是**互连负载的预估模型**（按扇出数估线长线电容）。注意它的 `slope`、全部 `fanout_length` 都是 0 —— 明显是个占位/QA 用模型（组名都叫 `ForQA`），说明本库默认不靠 wire load 估互连，实际流程应从布线后提取的真实寄生（SPEF）来（参见 u2-l3 讲的 `_ecos` 版 tech LEF 补 RC 参数的动机）。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L123-L128](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L123-L128) —— `operating_conditions`：process 1.0 / temperature 25 / voltage 3.3，与 L28-L30 的三个 `nom_*` 一一对应。这个组在多 corner 库里会被 `default_operating_conditions`（见文件尾 L1147）引用。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L129-L140](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L129-L140) —— 两张**表模板**：`lu_table_template "del_1_5_6"`（第 1 轴 `input_net_transition` 5 个点、第 2 轴 `total_output_net_capacitance` 6 个点）与 `power_lut_template "pwr_tin_oload_5_6"`。**模板只定义维度与形状**，模板里的 `index_1("1, 2, 3, 4, 5")` 是占位值，真正的轴刻度由每张实表自带的 `index_1/index_2` 覆盖（见 4.4）。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L1147-L1150](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1147-L1150) —— 文件尾部三个 default 把前面的命名组串起来并收上大括号。

另一个值得留意的细节：L25 的 `date` 字段在 tt 和 ss 两份里写的是 `CST 2014`，在 ff 那份里却是 `CST 2023`（[ff L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib#L25)）。`date` 只是表征工具打的时间戳，**不能当作 PDK 发布日期使用**；三份文件日期不一致也说明它们可能出自不同批次的表征运行（原因**待确认**）。

#### 4.1.4 代码实践

1. **实践目标**：验证「文件名里的温度 == `nom_temperature` == `operating_conditions.temperature`」三处一致，证明文件名不是随便起的。
2. **操作步骤**（本讲已实际运行）：

   ```bash
   grep -n -E 'library \(|nom_temperature|nom_voltage' \
     IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/*.lib
   grep -n -A4 'operating_conditions' \
     IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib
   ```

3. **需要观察的现象**：每份文件的 `nom_temperature` 与文件名末尾的温度段（25c/125c/m40c/0c）一致；`nom_voltage` 与文件名里**第二个**电压段（3p3/3p63/2p97）一致。
4. **预期结果**（实测）：tt→25/3.3，ff_125c→125/3.63，ff_m40c→-40/3.63，ff_v_0c→0/3.63，ss_125c→125/2.97，ss_m40c→-40/2.97；tt 的 `operating_conditions` 为 process 1.0 / temperature 25 / voltage 3.3 / balanced_tree。

#### 4.1.5 小练习与答案

1. **练习**：`capacitive_load_unit(1.000000, "pf")` 改成 `(1.0, "ff")` 而其他数值一个不动，PAD 的 2.726 会差多少倍？
   **答案**：数值不变但物理含义差 1000 倍：从 2.726 pF 变成 2.726 fF。liberty 里**没有默认单位**，一切以头部声明为准。
2. **练习**：为什么 `wire_load "ForQA"` 全是 0 也不能删？
   **答案**：它是被 L1148-L1149 的 `default_wire_load_selection/default_wire_load` 引用的命名组；一些工具要求被引用的组必须存在。全 0 表示「不做基于扇出的互连预估」，互连负载应来自真实提取（SPEF）或由工具自行计算。
3. **练习**：`nom_voltage : 3.3` 对应的是核电压 1.2V 还是 IO 电压 3.3V？
   **答案**：IO 电压。文件名 `tt_1p2_3p3` 里有两个电压，库头只记录了第二个（3.3，IO 域）；核电压 1.2V 只出现在文件名里（详见 4.2）。

### 4.2 corner 命名规则

#### 4.2.1 概念说明

IO 库 6 份 liberty 的文件名共享一个模板：

```
ICSIOA_N55_3P3_{工艺角}_{核电压}_{IO电压}_{温度}.lib
```

- `ICSIOA`：IO 库名（与 LEF 文件 `ICSIOA_N55_3P3_1P6M1TM.lef` 同族）；
- `N55`：55nm 节点；`3P3`：3.3V IO 器件族；
- 工艺角 `tt/ff/ss`：typical / fast / slow；
- 两个电压用 `p` 代替小数点（和 u3-l1 讲过的单元命名里 `X0P5` 用 `P` 代替小数点是同一习惯）：`1p2`=1.2V **核电压**、`3p3`=3.3V **IO 电压**；
- 温度：`25c`=25℃、`125c`=125℃、`m40c`=−40℃（minus 缩写成 m）。

这是**双电压域 IO 单元**的直接体现：一个 pad 内部同时有接 1.2V 核电源的逻辑和接 3.3V IO 电源的驱动级，两个电源各有一个 corner 偏移，文件名必须把两个都写出来。

#### 4.2.2 核心流程

拿到一个 corner 文件名后的解码流程：

```
读文件名 → 拆出 工艺角 / V_core / V_io / T
        → 打开文件核对 nom_temperature、nom_voltage（= V_io）、operating_conditions
        → 确认无误后，按用途选库：
             setup 分析  → ss（低压高温最慢）
             hold  分析  → ff（高压低温最快）
             功耗/典型   → tt
```

注意一个反直觉点：**ff 配的是高电压（3.63V），ss 配的是低电压（2.97V）**。这不是笔误——corner 的目标是组合出「最快」和「最慢」的极端：电压越高晶体管越快，所以「快工艺 + 高电压」是速度上限，「慢工艺 + 低电压 + 高温」是速度下限。

#### 4.2.3 源码精读

6 份文件的解码与库头核对结果（`nom_*` 均实测）：

| 文件名 | 工艺角 | 核电压 | IO 电压 | 温度 | nom_temperature | nom_voltage |
| --- | --- | --- | --- | --- | --- | --- |
| `tt_1p2_3p3_25c` | tt | 1.2 V | 3.3 V | 25 ℃ | 25.0 | 3.30 |
| `ff_1p32_3p63_125c` | ff | 1.32 V | 3.63 V | 125 ℃ | 125.0 | 3.63 |
| `ff_1p32_3p63_m40c` | ff | 1.32 V | 3.63 V | −40 ℃ | −40.0 | 3.63 |
| `ff_1p32_3p63v_0c` | ff | 1.32 V | 3.63 V | 0 ℃ | 0.0 | 3.63 |
| `ss_1p08_2p97_125c` | ss | 1.08 V | 2.97 V | 125 ℃ | 125.0 | 2.97 |
| `ss_1p08_2p97_m40c` | ss | 1.08 V | 2.97 V | −40 ℃ | −40.0 | 2.97 |

两点值得注意：

1. `ff_1p32_3p63v_0c` 的 IO 电压段多了一个字母 `v`（`3p63v`），库内电压仍是 3.63V。这个 `v` 是命名瑕疵还是另有含义**待确认**——这正好是 4.2.5 的练习。
2. 库头 `nom_voltage` 只记录 IO 域电压，**核电压只存在于文件名**。所以自动化脚本选库时，解析文件名比只读库头更可靠。

相应的库头证据：[ff_1p32_3p63_125c.lib: L28-L30](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib#L28-L30)（nom_temperature 125 / nom_voltage 3.63）、[ff L61-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib#L61-L66)（ff 角的 vol/voh 抬高到 1.49/2.01、vomax 3.63）、[ff L123-L128](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib#L123-L128)（operating_conditions 与文件名一致）。对比 [ss_1p08_2p97_125c.lib: L61-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ss_1p08_2p97_125c.lib#L61-L66)：ss 角 vol/voh 压低到 1.36/1.78、vomax 2.97——**同一个 pad 在不同 corner 的输出电平判据也在跟着电压走**。

corner 覆盖策略上：ss/ff 各有 125℃ 和 −40℃ 两档（setup 看高温慢角、hold 看低温快角，这正是经典组合），tt 只给 25℃ 一档用于典型估算。

#### 4.2.4 代码实践

1. **实践目标**：用一条命令生成「文件名 ↔ 库头」核对表，作为以后写选库脚本的雏形。
2. **操作步骤**（已在仓库实际运行）：

   ```bash
   cd IP/IO/ICsprout_55LLULP1233_IO_251013/liberty
   for f in *.lib; do
     printf '%-32s ' "${f%.lib}"
     grep -m1 'nom_temperature' "$f"
   done
   ```

3. **需要观察的现象**：6 行输出，每行文件名段的温度编码与 `nom_temperature` 数值一一对应。
4. **预期结果**：见 4.2.3 的表格（25 / 125 / −40 / 0 / 125 / −40）。
5. 进阶（可选）：用 `grep -m1 'nom_voltage'` 再跑一遍，验证文件名第二个电压段；尝试解释 `3p63v` 中的 `v`（**待确认**）。

#### 4.2.5 小练习与答案

1. **练习**：做 setup 签核应该选 6 份中的哪一份？做 hold 签核呢？
   **答案**：setup 选 `ss_1p08_2p97_125c`（最慢：慢工艺+最低电压+高温）；hold 选 `ff_1p32_3p63_m40c`（最快：快工艺+最高电压+低温）。若担心低温下老化的反向效应，工程上有时还要补 `ff_..._125c`，但本仓库的 corner 集合里最保守的 hold 角是 m40c。
2. **练习**：为什么文件名要写两个电压，而 `nom_voltage` 只有一个？
   **答案**：IO pad 是双电压域器件（核 1.2V 逻辑 + IO 3.3V 驱动级），两个域都有 corner 偏移，文件名必须同时编码；liberty 的 `nom_voltage` 是**库级单值**属性，本库用它记录 IO 域电压，核域电压因此只剩文件名这一处信息。
3. **练习**：`1p2` 与 `1p32` 差了 10%，这正常吗？
   **答案**：正常，这是同一条 1.2V 标称电源的两个 corner 偏移：tt 取典型 1.20V，ff 取 +10%（1.32V），ss 取 −10%（1.08V）；IO 域同理 3.3/3.63(+)10%/2.97(−)10%。电压偏移方向与工艺角方向一致（ff=快=高压）。

### 4.3 cell 与 pin：电容参数怎么读

#### 4.3.1 概念说明

`cell` 是 liberty 的基本建模单位。但**不是 cell_list 里的每个单元都需要 liberty 模型**：IO 库 cell_list 有 23 个单元（u1-l2/u4-l1），而每份 liberty 只有 **12 个 cell**——缺的 11 个是 `CORNER`、`CUT` 和 9 个 `FILLER*`，它们是纯物理/结构单元，没有电学行为，自然没有时序模型。

12 个 cell 分两类：

- **4 个电学 pad**：`P65_1233_PAR`、`P65_1233_PAR_5`（并联电阻 pad，只有电容数据）、`P65_1233_PBMUX`（可编程双向 IO，最完整）、`P65_1233_PWE`（焊线使能 pad）；
- **8 个电源 pad**：`VDD1/VDD1A/VDD3/VDDIO3/VSS1/VSS1A/VSS3/VSSIO3`，每个只有一个 inout 电源引脚，没有任何时序表。

pin 层面最重要的三个电容属性（单位 pF）：

- `capacitance`：总体等效输入电容；
- `rise_capacitance` / `fall_capacitance`：输入**上升沿/下降沿**时呈现的等效电容。二者与 `capacitance` 不同，是因为输入管密勒效应（栅漏电容被放大）随方向不同——上升和下降时被驱动的是不同极性的管子。

#### 4.3.2 核心流程

工具消费 pin 电容的方式：

```
对每个 net：总负载 = Σ(下游各输入引脚的 rise/fall_capacitance)
          + 互连寄生（SPEF 或 wire load）
该负载作为查找表的第 2 轴查驱动单元的延迟（见 4.4）
```

所以引脚电容会**沿着逻辑锥逐级放大影响**：一个 2.7pF 的 PAD 引脚 ≈ 几十个标准单元输入引脚，谁驱动它谁就慢。

#### 4.3.3 源码精读

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L141-L159](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L141-L159) —— `P65_1233_PAR`：`area : 8450`（单位 μm²，与 IO LEF 里该宏 `SIZE 65 BY 130` 的 65×130=8450 完全一致，跨视图印证）；`pad_cell : true` 标记这是压焊盘单元；PAD 引脚 `is_pad : true`、`drive_current : 4.0`（4mA），随后三个电容值 2.726/2.938/1.508 pF；核侧 A 引脚三个电容 2.714/2.883/1.509 pF。**没有 timing 组**——它是无源并联电阻结构，不需要延迟表。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L179-L263](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L179-L263) —— `P65_1233_PBMUX`，全库最丰富的单元：12 个引脚。9 个普通输入（OE/I/OD/DS1/DS0/PU/PD/IE/CS，各自带 `fanout_load` 与三个电容，量级仅 0.008–0.027 pF），核侧 inout A，pad 侧 inout PAD。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L252-L263](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L252-L263) —— PBMUX 的 PAD 引脚属性群：`function : "I"`（输出功能 = 核侧输入 I 直驱）；`three_state : "(!OE)"`（OE 低时高阻）；`pull_up_function : "(PU&!PD)"` / `pull_down_function : "(!PU&PD)"`（上拉/下拉电阻使能逻辑）；`drive_current : 12.0`（12mA，是 PAR 的 3 倍，因为可通过 DS0/DS1 配置驱动强度）；`capacitance : 1.45889`。这些布尔表达式就是 pad 的「行为说明书」，STA 工具据此判断哪些弧在什么条件下有效。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L645-L648](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L645-L648) —— 接收方向输出引脚 `C`：`direction : "output"`、`function : "(PAD&IE)"`——从 pad 收到的信号在 IE（输入使能）有效时送进核。发送走 `I→PAD`，接收走 `PAD→C`，一个双向 pad 的两条通路在 liberty 里就是两个引脚的两堆表。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L1074-L1091](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1074-L1091) —— 电源 pad `VDD1` / `VDD1A`：只有一个 inout 引脚。两个可注意的细节：其一，这里写的是 `is_pad : "true"`（带引号的**字符串**），而信号 pad 写 `is_pad : true`（布尔）——liberty 两种写法解析器都收，但同一份文件里风格不统一，写脚本时两种都要匹配；其二，单元名叫 `VDD1A`，引脚名却是 `VDDA1`（字母顺序不同），`VSS1A` 的引脚也叫 `VSSA` 而非 `VSS1A`——做 u5-l2 的跨视图一致性检查时这类命名差异要特别小心。

#### 4.3.4 代码实践

1. **实践目标**：数出这份 liberty 的结构骨架，并与 cell_list 对账。
2. **操作步骤**（已实际运行）：

   ```bash
   lib=IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib
   grep -c 'cell ('        "$lib"   # cell 数
   grep -c 'pin ('        "$lib"   # pin 数
   grep -c 'timing ()'    "$lib"   # timing 弧数
   grep -c 'internal_power()' "$lib"
   comm -23 <(sort IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt) \
            <(grep -o 'cell ("[^"]*")' "$lib" | sed 's/cell ("//;s/")//' | sort)
   ```

3. **需要观察的现象**：cell/pin/timing 计数；最后一列输出 cell_list 里有、liberty 里没有的单元。
4. **预期结果**（实测）：**12 个 cell、28 个 pin、13 个 timing 组、7 个 internal_power 组**；差集为 `P65_1233_CORNER`、`P65_1233_CUT` 与 9 个 `P65_1233_FILLER*`（共 11 个物理单元无 liberty 模型）。
5. `comm` 那一步若提示进程替换不可用，可把两个 `sort` 结果分别重定向到临时文件再 `comm -23 f1 f2`。

#### 4.3.5 小练习与答案

1. **练习**：PBMUX 的 OE 引脚 `capacitance : 0.007812` 与 PAD 引脚 `capacitance : 1.458890` 差了近 200 倍，物理原因是什么？
   **答案**：OE 是片内普通逻辑输入（小栅电容）；PAD 焊盘要直接暴露给外部世界，焊盘金属本身大、且必须挂 ESD 保护二极管，电容自然以 pF 计。这也是 4.4 里 pad 延迟表量级达到 ns 的根源。
2. **练习**：`rise_capacitance : 2.938` 与 `fall_capacitance : 1.508` 差了约一倍，哪个方向更「重」？工具会怎么用？
   **答案**：上升方向更重（2.938 > 1.508）。STA 在计算某条 net 的负载时按信号方向选用：驱动沿为上升时累加下游的 rise_capacitance，下降时累加 fall_capacitance；`capacitance` 是综合/概算用的单值代表。
3. **练习**：`drive_current : 4` 与 `drive_current : 12`（单位 mA）在流程里有什么用？
   **答案**：它描述 pad 的直流/峰值驱动能力，用于压摆率（di/dt）与 SSN（同步开关噪声）评估、以及驱动大负载的可行性判断；它与查找表里的延迟数据是互补关系，前者是「能扛多大」，后者是「跑多快」。

### 4.4 查找表与 timing 弧

#### 4.4.1 概念说明

一条 **timing 弧** = 从某个相关输入引脚（`related_pin`）到本输出引脚的一条延迟路径。每条弧用**四张二维表**刻画：

| 表名 | 含义 |
| --- | --- |
| `cell_rise` | 输出上升时的单元延迟 |
| `cell_fall` | 输出下降时的单元延迟 |
| `rise_transition` | 输出上升沿的转换时间（作为下一级的输入 slew） |
| `fall_transition` | 输出下降沿的转换时间 |

两个轴：

- 第 1 轴 `input_net_transition`：输入信号转换时间（ns）；
- 第 2 轴 `total_output_net_capacitance`：输出 net 的**总**电容（pF）——**含本引脚自身电容与全部外接负载**。

工具查询时若点落在网格中间就双线性插值，落在网格外就外推（各大工具对超出表范围的 slew/负载会告警）。

状态相关（state-dependent）表：当单元行为取决于配置引脚时，用 `when : "<布尔表达式>"` 给出该表生效的条件，`sdf_cond` 是同一条件的 SDF 注释形式。PBMUX 有 DS0/DS1 两个驱动强度选择脚，2 位共 4 种组合，于是 `I→PAD` 的组合逻辑弧有 **4 组平行表**——这正是「可编程驱动强度」在 liberty 里的形态。

#### 4.4.2 核心流程

一次 STA 延迟计算的简化流程：

```
取弧 (related_pin=I → 输出 PAD)，按 DS0/DS1 当前取值选 when 匹配的那组表
取输入 slew（上一级 rise/fall_transition 的插值结果）
取输出负载 C = Σ下游引脚电容 + 互连寄生
在 cell_rise/cell_fall 表中按 (slew, C) 双线性插值 → 得本单元延迟
再用 rise/fall_transition 表插值 → 得输出 slew，传给下一级
```

#### 4.4.3 源码精读

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L368-L413](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L368-L413) —— PBMUX 的第一条 timing 弧：`timing_sense : positive_unate`（输入上升引起输出上升，非反相）；`related_pin : "I"`；`when : "!DS0 * !DS1"`（两种驱动选择都拉低时生效）；随后依次是 `cell_rise` / `rise_transition` / `cell_fall` / `fall_transition` 四张 5×6 表。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L373-L381](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L373-L381) —— `cell_rise` 实表：`index_1` 是 0.5→5 ns 的输入 slew 轴（pad 域信号很慢），`index_2` 是 2.45889→11.45889 pF 的负载轴。注意第 2 轴起点 2.45889 = **PAD 引脚自身电容 1.45889 + 1.0 pF 外接负载起点**——印证了 `total_output_net_capacitance` 是「自电容+外部负载」的总量口径。表左上角 1.0864 ns 的含义：输入 slew 0.5ns、总负载 2.46pF 时，I 到 PAD 的上升延迟约 1.09ns——pad 延迟是**纳秒级**，比标准单元（几十皮秒）大一到两个数量级，因为它要驱动 pF 级的焊盘与封装负载。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L552-L557](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L552-L557) —— `timing_type : "three_state_enable"` 的弧（OE→PAD）：输出从高阻变为驱动的使能时间，并引用了库头 `define` 过的自定义属性 `three_state_pulldn_res/pullup_res`。对应的 [L599-L603](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L599-L603) 是 `three_state_disable`（进入高阻的时间）。三态 pad 的 OE 弧是双向 IO 时序收敛的关键路径之一。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L675-L690](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L675-L690) —— 接收方向 `PAD→C` 的弧（`timing_type : combinational`，`when : "!CS"`）：注意两个轴的量级立刻变小——输入 slew 0.1→1 ns、负载 0.01→0.5 pF（核域小负载）。同一份文件里 pad 域与核域表的轴范围差异，本身就是双电压域行为的写照。

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib: L845-L854](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L845-L854) —— PWE 的 `XOUT` 引脚 `rise_power` 表，其中出现大量**负值**（如 −18.11）。liberty 的 internal_power 是相对无翻转基线的能量增量，某些测量窗口下可以为负，所以**不能拿单点表值当「功耗」直接累加**，需按规范换算。这是初读 liberty 最容易误读的地方之一。

#### 4.4.4 代码实践

1. **实践目标**：手工（用脚本）完成一次「查表」，体会 NLDM 的读法。
2. **操作步骤**：

   ```bash
   lib=IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib
   # 1) 列出 PBMUX 内所有 timing 弧的首行属性
   grep -n -A4 'timing ()' "$lib" | sed -n '1,80p'
   ```

   然后人工完成一次插值：用 L374-L375 的轴与 L377-L381 的表值，取输入 slew = 1.5 ns、总负载 = 4.45889 pF，求 cell_rise。
3. **需要观察的现象**：13 条弧中 PAD 引脚占 6 条（4 条 DS 组合 + enable + disable），C 引脚 3 条（PAD→C 两种 CS 态 + IE→C），PWE 的 XOUT/XC 各 2 条；每条弧都有四张表。
4. **预期结果**：slew=1.5 落在 1 与 2 之间（权重各 0.5），负载 4.45889 落在 3.45889 与 5.45889 之间（权重 0.5/0.5），四点取 `1.1272546, 1.1922125; 1.1205970, 1.1851598`，双线性结果 ≈ **1.1563 ns**（四点平均，本题恰好两轴都是中点）。若你的结果偏差超过 0.001，多半是抄错了行或列。
5. 以上插值为本讲手工演算结果，属「待本地验证」的算术，欢迎用脚本复核。

#### 4.4.5 小练习与答案

1. **练习**：`timing_sense : positive_unate` 与 `negative_unate` 各对应什么单元？本库哪里出现了 negative？
   **答案**：positive_unate = 输入输出同向（与门/或门类），negative_unate = 反向（与非/或非类）。本库 [PWE 的 XOUT](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L867-L869)（`function : "(!(XIN&E))"`，与非逻辑）那条弧是 negative_unate。工具用 unateness 决定用输入的哪个沿去查输出的哪张表。
2. **练习**：为什么 `I→PAD` 的弧有 4 组 `when` 平行表，而 `PAD→C` 只有 2 组？
   **答案**：发送方向的输出驱动强度由 DS0/DS1 两位配置（2²=4 组），每组驱动能力不同、延迟不同，需分别表征；接收方向（PAD→C）的接收器不受 DS0/DS1 影响，只按 CS 状态分成 2 组。
3. **练习**：如果 STA 报告「slew 超出表范围」，L374 的轴（0.5–5 ns）意味着什么？
   **答案**：输入 net 的转换时间比表的最大轴值 5 ns 还大（或比 0.5 还小），工具只能外推并给出精度告警。工程上要么修驱动（加大 cell 或降负载），要么向库供应商要更大范围的表。

## 5. 综合实践

**任务：量化三个 corner 下同一 pad 的电容漂移，并把标准单元 liberty 下载到位。**

这是本讲的主实践，把 4.1–4.4 串起来。

### 第一步：提取并对比 P65_1233_PAR 的电容

1. **实践目标**：写出可复用的 corner 对比脚本，得到 ff 相对 ss 的电容偏差百分比。
2. **操作步骤**：
   - 快速通道（本讲已实际运行，直接可用）：

     ```bash
     grep -A18 'cell ("P65_1233_PAR")' \
       IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib \
       IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ff_1p32_3p63_125c.lib \
       IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_ss_1p08_2p97_125c.lib \
       | grep -E 'liberty|pin \(|capacitance'
     ```

   - 通用通道（**示例代码**，写成 `par_caps.py` 之类的脚本放仓库外或教程目录外运行皆可）：

     ```python
     #!/usr/bin/env python3
     # 示例代码：提取三个 corner 中 P65_1233_PAR 的引脚电容并计算 ff 相对 ss 的偏差
     import re, sys

     BASE = "IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_%s.lib"
     CORNERS = ["tt_1p2_3p3_25c", "ff_1p32_3p63_125c", "ss_1p08_2p97_125c"]
     KEYS = ["capacitance", "rise_capacitance", "fall_capacitance"]

     def pin_caps(text, cell, pin):
         seg = text[text.index('cell ("%s")' % cell):]        # 截取 cell 块
         seg = seg[:seg.index("\n\tcell (", 1)] if "\n\tcell (" in seg else seg
         p = seg[seg.index("pin (%s) {" % pin):]              # 截取 pin 块
         p = p[:p.index("\n\t\tpin (", 1)] if "\n\t\tpin (" in p else p
         return {k: float(re.search(r"%s : ([0-9.eE+-]+)" % k, p).group(1))
                 for k in KEYS}

     data = {}
     for c in CORNERS:
         with open(BASE % c) as f:
             data[c] = {p: pin_caps(f.read(), "P65_1233_PAR", p) for p in ("PAD", "A")}

     for p in ("PAD", "A"):
         for k in KEYS:
             ff, ss = data[CORNERS[1]][p][k], data[CORNERS[2]][p][k]
             print("%-4s %-17s ff=%7.3f  ss=%7.3f  偏差=%+.1f%%"
                   % (p, k, ff, ss, (ff - ss) / ss * 100))
     ```

3. **需要观察的现象**：三个 corner 的同名电容值都不一样，且 ff < tt < ss。
4. **预期结果**（数值经 grep 提取并人工核算）：

   | 引脚.属性 | tt | ff | ss | ff 相对 ss 偏差 |
   | --- | --- | --- | --- | --- |
   | PAD.capacitance | 2.726 | 2.512 | 3.471 | **−27.6%** |
   | PAD.rise_capacitance | 2.938 | 2.738 | 3.702 | −26.0% |
   | PAD.fall_capacitance | 1.508 | 1.325 | 1.929 | **−31.3%** |
   | A.capacitance | 2.714 | 2.389 | 3.475 | **−31.3%** |
   | A.rise_capacitance | 2.883 | 2.536 | 3.687 | −31.2% |
   | A.fall_capacitance | 1.509 | 1.324 | 1.933 | −31.5% |

   脚本输出应与上表一致（尾数 ±0.1% 以内）。值得注意的规律：电容排序 ff < tt < ss 与三份库的 IO 电压排序 3.63 > 3.3 > 2.97 **严格反相关**——焊盘电容以 ESD 二极管/金属为主，其耗尽层电容随偏置电压升高而减小，这个定性解释与数据吻合（机理属定性推断，**待确认**，可查 `doc/ICSIOA_N55_3P3_Application_Datasheet_1P6M.pdf` 求证）。
5. 若脚本报 `ValueError`，优先检查仓库相对路径与缩进（liberty 用 Tab 缩进，示例中的 `\t` 需按字面 Tab 输入）。

### 第二步：下载标准单元 liberty 并数单元

1. **实践目标**：把 git 里没有的标准单元 liberty 落到本地，确认它覆盖 H7CH 的全部单元。
2. **操作步骤**：

   ```bash
   make -n unzip          # 先 dry-run，看清会下载/解压哪些目标（u1-l3 已详述）
   make unzip RELEASE_TAG=v1.10.100   # 需要网络；代理环境加 PROXY_USE=true
   ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/
   ```

   下载规则来自 [Makefile: L11-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L13)（三个 `*_liberty.tar.bz2`）与 [Makefile: L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66)（解压到 `ics55_LLSC_H7CH/liberty/`），目录被 [.gitignore: L1](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1) 忽略，所以解压后 `git status` 依旧干净。
3. **需要观察的现象**：解压出的 liberty 文件名与数量（**待确认**——tar 包内部文件名只有下载后才能知道）；然后用第一步的思路统计：

   ```bash
   grep -c 'cell (' <解压出的 .lib 文件>
   ```

4. **预期结果**：cell 数应不小于 cell_list 的 748（u3-l1），且大于等于 LEF 的 785 个 MACRO 才能覆盖含 ANT 在内的全部单元（具体数值**待本地验证**）。若无网络环境，本步骤退化为 `make -n unzip` 的 dry-run 分析 + 记录「文件名待确认」，不影响第一步的结论。
5. 思考题（不计分）：标准单元 liberty 大到要放 Release、IO liberty 却留在 git 里（u1-l3 的结论），结合本讲看数据量差在哪儿？——IO 库 12 个 cell / 13 条弧 vs 标准单元 700+ 个 cell、每个 cell 多条弧多 corner，这就是体积差的数量级来源。

## 6. 本讲小结

- liberty 是单元的「电学说明书」：**库头定度量衡与口径**（单位、nom 条件、slew 10%–90%、延时 50%–50%、`delay_model : table_lookup`），cell/pin/timing 三层装数据。
- IO 库 6 份 liberty 的文件名 `ICSIOA_N55_3P3_{角}_{核压}_{IO压}_{温}` 可直接解码；**库头只记 IO 电压，核电压只在文件名里**；ff 配高压、ss 配低压不是笔误，而是「最快/最慢」的构造方式。
- cell_list 23 个单元只有 12 个进了 liberty：**CORNER/CUT/FILLER 是物理单元，无电学模型**；电源 pad 只有引脚没有弧。
- 引脚三个电容（capacitance / rise_ / fall_）按方向区分密勒效应；一条 timing 弧 = `related_pin` 到输出引脚的四张 5×6 二维表（cell_rise/fall + rise/fall_transition），双线性插值读表。
- 状态相关表用 `when`/`sdf_cond` 区分配置：PBMUX 的 4 组 DS0/DS1 组合表就是「可编程驱动强度」的 liberty 形态；三态 pad 还有 `three_state_enable/disable` 弧。
- 实测结论：P65_1233_PAR 的电容在 ff 与 ss 之间漂移 **约 −26% 到 −31%**，排序与 IO 电压反相关——**选错 corner 的 liberty，时序结论直接失效**。

## 7. 下一步学习建议

- 下一讲（u4-l1 IO 单元家族盘点）会把这 12 个电学/电源 pad 与 11 个物理单元放回 pad ring 的语境，建议先记住本讲的 `area : 8450` ↔ LEF `SIZE 65 BY 130` 这对跨视图证据。
- 想「亲手用上」liberty：u6-l2 将用 yosys `read_liberty -lib` + `dfflibmap` + `abc -liberty` 把 RTL 映射到 H7C 门级网表，本讲的 corner 选择会直接变成那一步的参数。
- 延伸阅读方向（本仓库之外）：liberty 参考手册中 `lu_table_template`、`when`、`three_state_*` 三节；OpenSTA 的 `read_liberty` 与 delay calculation 文档，可对照本讲 4.4 的查表流程。
- 若你完成了第 5 节第二步的下载，可提前做一个小实验：对比下载的标准单元 liberty 与本讲 IO liberty 的库头差异（尤其 slew 阈值是否同为 10%–90%），差异会提醒你**两库混用时工具如何统一测量口径**。
