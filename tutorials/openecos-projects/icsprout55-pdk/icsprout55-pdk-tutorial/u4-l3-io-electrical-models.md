# IO 电学模型：liberty、Verilog 与 CDL

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 IO 库 liberty 中 pad 专用属性（`pad_cell`、`is_pad`、`drive_current`、`function`、`three_state`、上/下拉函数等），并说清它们与标准单元 liberty 的差异。
2. 用「双电压域」（1.2 V 核 / 3.3 V IO）这一条主线，把 liberty 文件名、`vil/vih` 阈值、CDL 器件模型名和电平移位子电路串联起来。
3. 读懂 IO 库 Verilog 模型的组织方式：电源 pad 的空壳模块、`tran/rtran` 无源连接、`(0,0)` 占位 specify 块与 `ifdef NOTIMING`。
4. 打开 CDL 网表，逐行数出 PBMUX 的晶体管，并标注每个端子所属的电源域。

本讲是单元四的收官：u4-l1 盘点了「有哪些 pad」，u4-l2 讲了「pad 长什么样（几何）」，本讲回答「pad 电气上如何建模、物理上如何实现」。

## 2. 前置知识

- **三个视图的分工**（承接 u1-l2）：liberty 给综合器/静态时序分析用，Verilog 给门级仿真用，CDL 给晶体管级仿真与 LVS 用。同一个 pad 在三个视图里名字相同、引脚相同，但抽象层次依次降低。
- **liberty 基础**（承接 u3-l6）：库头定单位与阈值，`cell/pin/timing` 三层结构，NLDM 二维查找表，corner 命名规则 `tt_1p2_3p3_25c` = 典型工艺角 / 核 1.2 V / IO 3.3 V / 25 ℃。本讲只复用这些结论，不再重复推导。
- **pad ring 与双电压**（承接 u4-l1）：IO 单元工作在两个电压域——内核逻辑用 1.2 V（VDD/VSS），压焊盘一侧用 3.3 V（VDDIO/VSSIO），模拟成员用 VDDA/VSSA。信号要从核域走到 pad，必须经过**电平移位器（level shifter）**。
- **SPICE/CDL 语法最小集**（本讲首次系统接触，u5-l1 会展开）：
  - `.SUBCKT 名字 端子列表` … `.ENDS` 定义一个子电路；
  - `*.PININFO` 注释行标注每个端子方向：`I` 输入、`O` 输出、`B` 双向（B 表示 "Both"/总线式双向，电源端子也标 B）；
  - MOS 器件语句：`M名字 漏 栅 源 衬底 模型名 W=.. L=.. m=..`，其中 `W/L` 是宽长（米制后缀 `u`=1e-6、`n`=1e-9），`m` 是并联倍数；
  - `X名字 节点... / 子电路名` 实例化一个子电路；`DD名字` 是二极管，`re_*` 是多晶/扩散电阻模型。
- **天线效应与 ESD**（承接 u3-l4）：pad 内大量二极管和巨宽晶体管不是逻辑，是**静电放电（ESD）保护**——压焊时人体静电可达千伏，需要泄放通路。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib` | 1150 | 典型角 liberty：12 个 pad 的时序/功耗/电容模型（另有 ff/ss 等共 6 个 corner，结构相同） |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v` | 219 | 13 个模块的 Verilog 仿真模型，以无源连接和零延迟 specify 为主 |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl` | 659 | 晶体管级网表：69 个 `.SUBCKT`，含电阻/二极管器件库、辅助逻辑单元与 13 个 pad 主体 |

另有 `doc/ICSIOA_N55_3P3_Application_Datasheet_1P6M.pdf`（数据手册，串联电阻绝对值等参数以它为准，本讲不引用未核实的数值）。

## 4. 核心概念与源码讲解

### 4.1 pad 专用 liberty 属性

#### 4.1.1 概念说明

liberty 是给工具看的「电学名片」。标准单元的 liberty 回答「这个门多快、耗多少电」；pad 的 liberty 还要多回答三个问题：

1. **这是 pad 吗**——`pad_cell : true` 和 `is_pad : true` 让综合器、布局器和时序工具把它当压焊盘对待（比如扇出到 bond wire 而不是金属线）。
2. **能驱动多大电流**——`drive_current` 是 pad 的直流驱动能力标称值，单位由库头 `current_unit : "1mA"` 决定。
3. **接口电平是什么**——`input_voltage/output_voltage` 块给出 3.3 V CMOS 的 `vil/vih` 阈值。

另外，全功能双向 pad（PBMUX）还用一组**行为函数**描述输出结构：`function`、`three_state`（三态条件）、`pull_up_function/pull_down_function`（弱上/下拉条件），让工具不用看晶体管就知道 pad 的逻辑行为。

#### 4.1.2 核心流程

一个 liberty pad 单元的阅读顺序：

```text
library 头（单位、阈值、电压域）
  └─ cell（pad_cell : true、area）
       ├─ 输入 pin：direction/capacitance/rise_capacitance/fall_capacitance/fanout_load
       ├─ PAD pin：is_pad、drive_current、function、three_state、上下拉函数、电容
       │    ├─ internal_power()（按 DS0/DS1 组合分档的功耗表）
       │    └─ timing()（组合弧 + 三态使能/关断弧，全部是二维表）
       └─ 输出 pin（C/XC）：function + 查表弧
```

关键区别：**纯被动 pad（PAR/PAR_5/8 个电源 pad）只有电容，没有任何 `timing()` 弧**——它们不产生延迟，只是负载；只有 PBMUX 和 PWE 有时序弧。

#### 4.1.3 源码精读

库头先看两行：`nom_voltage : 3.300000` 只登记了 IO 电压，核电压 1.2 V 只出现在文件名 `tt_1p2_3p3_25c` 里（u3-l6 已有结论）。阈值定义了 3.3 V CMOS 接口电平：

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L61-L72](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L61-L72) —— `input_voltage(cmos)` 给出 \(v_{il}=1.42\,\text{V}\)、\(v_{ih}=1.88\,\text{V}\)，约为 3.3 V 的 43%/57%；注意 `output_voltage` 里的 `vol/voh` 也是 1.42/1.88 的镜像值（本 preview 库生成时的排版习惯，读表时以 vil/vih 理解即可）。

最简单的 pad 单元 PAR（串联电阻 pad），是认识 pad 属性的最佳样本：

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L141-L159](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L141-L159) —— 单元级 `pad_cell : true` + `area : 8450`；`pin(PAD)` 带 `drive_current : 4`（即 4 mA）与 `is_pad : true`，电容约 2.7 pF；`pin(A)` 是核侧端子，电容 2.714 pF。**注意 2.7 pF 意味着什么**：它比标准单元输入脚（约 0.01 pF 量级）大两个数量级——pad 上挂着 ESD 器件和巨宽晶体管，这正是 pad 的物理代价。整段没有任何 `timing()`，因为一颗电阻不产生逻辑延迟。

`area : 8450` 还可以和 u4-l2 的 LEF 互相印证：65 × 130 = 8450 μm²，PWE 的 130 × 130 = 16900 μm²，见 [L818](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L816-L819)。liberty 面积与 LEF SIZE 完全一致，这是跨视图一致性的一个小证据。

PBMUX 的 PAD 引脚是全库属性最丰富的引脚：

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L252-L263](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L252-L263) —— `function : "I"`（输出数据来自 I）、`three_state : "(!OE)"`（OE 为 0 时输出关断）、`pull_up_function : "(PU&!PD)"` / `pull_down_function : "(!PU&PD)"`（弱上/下拉的条件）、`drive_current : 12`（12 mA，是 PAR 的 3 倍）、`is_pad : true`、电容 1.459 pF。

时序弧的组织方式与标准单元相同，但多了两种 pad 特有弧：

- [L368-L382](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L368-L382) —— `I → PAD` 组合弧，用 `when : "!DS0 * !DS1"` 区分驱动强度档位。DS0/DS1 共四种组合，每种都有一套 `cell_rise/rise_transition/cell_fall/fall_transition` 表（u3-l6 讲过 when 的含义，这里恰好是四档驱动强度的实例）。
- [L552-L557](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L552-L557) —— `timing_type : "three_state_enable"`（OE 打开输出到 Z→1/Z→0 的延迟），并带库头 `define()` 扩展的 `three_state_pullup_res/pulldn_res : "1"`（上/下拉 1 kΩ，单位由 [L36](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L36) `pulling_resistance_unit : "1kohm"` 决定）；紧接着 [L599-L602](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L599-L602) 是对称的 `three_state_disable` 弧。
- 接收方向的 `PAD → C` 弧在 [L645-L647](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L645-L647)（`function : "(PAD&IE)"`）与 [L675-L681](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L675-L681)（组合弧，`when : "!CS"` 选接收路径档）。

电源 pad 单元则退化到极致，例如 VDD1：

[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L1074-L1082](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1074-L1082) —— 只有 `pad_cell`、`area` 和一个 inout 引脚；注意这里 `is_pad : "true"` 写成了**字符串**，而信号 pad 处是布尔 `is_pad : true`（[L148](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L148)），缩进也混用了空格——写解析脚本时不能只匹配布尔写法。

最后看库头两行元数据：[L58-L60](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L58-L60) 的 `driver_model : "snps_predriver"`、`simulator : "GSI"` 表明该库由 Synopsys 类特征化流程生成；`date` 字段写着 2014 年（[L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L25)），是模板沿用的痕迹，不代表 PDK 发布时间。

#### 4.1.4 代码实践

**实践目标**：用脚本盘点全部 12 个 pad 单元的 `area`、`pad_cell`、`is_pad`、`drive_current`，并与 LEF 尺寸互验。

1. 阅读理解后，在仓库根目录执行下面脚本（示例代码，可存为任意临时文件或直接 `python3 - <<'PY'` 运行）：

```python
import re
path = "IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib"
text = open(path).read()
for m in re.finditer(r'cell \("(\w+)"\) \{([^{}]*)\n\t\}', text):
    name, body = m.group(1), m.group(2)
    if 'pad_cell' not in body:
        continue
    area = re.search(r'area : ([\d.]+)', body).group(1)
    dc = re.findall(r'drive_current : ([\d.]+)', body)
    print(f"{name:20s} area={area:>9s} drive_current(mA)={dc or '-'}")
```

2. 观察输出的 `area` 与 `drive_current` 列。
3. **预期结果**：12 行输出；4 个信号 pad 中 PAR/PAR_5/PWE 的 `drive_current=['4.000000']`、PBMUX 为 `['12.000000']`；PWE 的 area 是 16900，其余 11 个均为 8450；8 个电源 pad 的 drive_current 列为 `-`（没有该属性）。结合 u4-l2 的 LEF `SIZE 65 BY 130` / `130 BY 130`，验证 65×130=8450、130×130=16900。
4. 如果正则没匹配到电源 pad（因为 `is_pad : "true"` 的缩进怪异），检查单元闭合是否 `\t}`，按需放宽模式——这本身就是本实践的收获之一。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PAR 的 liberty 里没有 `timing()` 弧，而 PBMUX 有十几组？
**答案**：PAR 只是一颗串联电阻（加 ESD），对工具而言是纯负载，延迟由 RC 积分决定而不是查表弧；PBMUX 内部有输出驱动、三态控制、接收施密特触发等多级逻辑，每条信号路径都要给 STA 提供查找表。

**练习 2**：`drive_current : 12` 的单位是什么？依据是哪一行？
**答案**：12 mA。依据是库头 `current_unit : "1mA"`（[L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L33-L35)）。

**练习 3**：PBMUX 的 `when : "!DS0 * !DS1"` 弧和 `when : "!CS"` 弛分别建模什么？
**答案**：前者是输出方向 `I→PAD` 按 DS0/DS1 四种驱动强度组合分档的延迟/功耗表；后者是接收方向 `PAD→C` 按接收路径选择 CS 分档的表。工具在 STA 时按当前 DS/CS 绑定值选表。

### 4.2 双电压域：从文件名到晶体管

#### 4.2.1 概念说明

「双电压域」是 IO 库一切复杂性的根源：内核晶体管又小又快，用 1.2 V；外部世界（PCB、其他芯片）用 3.3 V 信号。于是一颗 pad 内部必然同时存在：

- **1.2 V 器件**：接收/发送控制逻辑（OE、IE、DS0/DS1 这些信号来自核域）；
- **3.3 V 器件**：驱动 bond pad 的大晶体管输出级、ESD 结构；
- **电平移位器**：把 1.2 V 逻辑信号翻译成能摆到 3.3 V 的信号；
- **跨域电源网络**：VDD/VSS（核域）、VDDIO/VSSIO（IO 域）、VDDA/VSSA（模拟域）。

这条主线在三个视图里各有体现：liberty 里是文件名的 `1p2_3p3` 和 `vil/vih`；CDL 里是两套器件模型名；Verilog 里则几乎不可见（因为 Verilog 模型不关心电压）。

#### 4.2.2 核心流程

以 PBMUX 发送一路数据为例：

```text
核域 1.2V：I / OE / OD / DS0 / DS1 信号
   │
   ├─ invio / nand2 / nor2（nm1p2_lvt_lp / pm1p2_lvt_lp 器件，接 VDD/VSS）
   │      产生输出使能与数据选择
   ├─ 9 个 level_shifter*（输入级 1p2 器件 + 输出级 3p3 器件，接 VDD/VDDIO）
   │      把 1.2 V 逻辑抬到 3.3 V 摆幅
   ▼
IO 域 3.3V：MUX_PAD（nm3p3_lp / pm3p3_lp，49 个 MOS，接 VDDIO/VSSIO）
   │      并联大管子按档位驱动 PAD
   ▼
PAD（bond pad，3.3 V 摆幅）── A（核侧端子）
```

#### 4.2.3 源码精读

CDL 是观察双电压域的最佳位置。PBMUX 顶层实例化的辅助单元一览：

[ICSIOA_N55_3P3.cdl:L422-L477](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L422-L477) —— `.SUBCKT P65_1233_PBMUX A C CS DS0 DS1 I IE OD OE PAD PD PU VDD VDDIO VSS VSSIO`。端口表本身就是一张电源域地图：`VDD/VSS` 是核域电源，`VDDIO/VSSIO` 是 IO 域电源，`A/C/I/OE/IE/CS/OD/PU/PD/DS0/DS1` 是核域信号，`PAD` 是 IO 域信号。看三行典型实例：

- [L464](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L464) `XI23 VDD VSS DS1 N33 / invio`——反相器接 VDD/VSS，纯核域；
- [L470](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L470) `XI10 N37 VDD VDDIO VSS N29 / level_shifter`——同时接 VDD 与 VDDIO，跨域；
- [L455](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L455) `XI0 ... PAD VDDIO VSSIO / MUX_PAD`——输出级只接 IO 域电源。

打开 `level_shifter` 看它的「两套器件」：

[ICSIOA_N55_3P3.cdl:L396-L406](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L396-L406) —— 输入级 M23/M163 用 `nm1p2_lvt_lp`/`pm1p2_lvt_lp`（接 VDD/VSS），锁存与输出级 M62/M59/M61/M198/M199/M200 用 `nm3p3_lp`/`pm3p3_lp`（接 VDDIO）。命名规则直译即器件规格：`n/p` 沟道类型 + `m` MOS + `3p3`/`1p2` 耐压 + `lvt` 阈值 + `lp` 低功耗工艺选项。**核域辅助单元清一色用 lvt 器件**——pad 内逻辑要快，且这点漏电相对 pad 驱动功耗可以忽略（对照 u3-l1 的阈值权衡）。

纯 IO 域的大输出级 MUX_PAD：

[ICSIOA_N55_3P3.cdl:L258-L312](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L258-L312) —— 49 个 MOS 全部是 `nm3p3_lp/pm3p3_lp`，`W=20u` 起步、并联 m=1，按 N0…N6 七根控制线分组开关——这就是「按 DS 档位增减并联管子」的物理实现，对应 liberty 里的四组 when 表。

模拟域成员（PAR/PAR_5，见 4.4.3）的端口则是 `VDDA/VSSA/VDD/VSS`，符合 u4-l1 归纳的「模拟域换 VDDA/VSSA」规律。最后回看 liberty 侧的证据链：文件名 `tt_1p2_3p3_25c` 两个电压 → 库头 `nom_voltage : 3.3` 只记 IO 域 → `vil/vih = 1.42/1.88` 定义 3.3 V 接口阈值 → CDL 两套器件 + 电平移位器。三个视图说的是同一件事。

#### 4.2.4 代码实践

**实践目标**：用 grep 把 CDL 里的器件模型按电压域分类计数。

1. 操作步骤：

```bash
cd IP/IO/ICsprout_55LLULP1233_IO_251013/cdl
grep -oE '(nm|pm)(3p3|1p2_lvt)_lp' ICSIOA_N55_3P3.cdl | sort | uniq -c
grep -n 'level_shifter' ICSIOA_N55_3P3.cdl | grep SUBCKT
```

2. 观察两类器件总出现次数，以及 `level_shifter` 家族有几种变体。
3. **预期结果**：四个模型名都有出现（`nm1p2_lvt_lp`、`pm1p2_lvt_lp`、`nm3p3_lp`、`pm3p3_lp`），其中 `*3p3*` 合计次数明显多于 `*1p2*`（pad 内大部分晶体管是 IO 域的大管子和 ESD）；level_shifter 家族共 4 个：`level_shifter`、`level_shifter_invn1u`、`level_shifter_invn2u`、`level_shifter_invn8u`（后缀 1u/2u/8u 对应输出驱动宽度档）。
4. 再抽查任意一个 `inv*`/`nand2`/`nor2` 辅助单元，确认其器件只出现在 VDD/VSS 网络——核域纯逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么电源 pad 家族里 VDD1 的引脚叫 VDD1 而 VDD3 的引脚叫 VDD？
**答案**：命名尾数标识电源域（u4-l1）：VDD1/VSS1 直接供给核域 1.2 V（引脚名带 1），VDD3/VDDIO3 是 3.3 V IO 域电源（VDD3 的压焊端子沿用 VDD 名字，另一侧接 VDDIO）。可对照 CDL [L551](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L551-L558) 与 [L580](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L580-L587) 的端口表验证：两者内部结构相同（M0 pm3p3 + M1 nm3p3 + 二极管），只是「把哪个域的电源送到压焊盘」不同。

**练习 2**：1.2 V 逻辑信号直接驱动 3.3 V 大管子会有什么问题？
**答案**：1.2 V 摆幅低于 3.3 V PMOS 的阈值裕度，PMOS 无法可靠关断，输出级会同时导通上下管形成直通电流。所以必须先用电平移位器把摆幅抬到 VDDIO。

**练习 3**：`nm1p2_lvt_lp` 这个名字里每个字段分别是什么含义？
**答案**：`n`=NMOS，`m`=MOSFET，`1p2`=1.2 V 耐压器件，`lvt`=低阈值（对照 u3-l1 的 HVT/LVT/RVT 概念），`lp`=低功耗工艺选项。`pm3p3_lp` 同理：3.3 V PMOS。

### 4.3 IO Verilog 仿真模型：无源连接与零延迟 specify

#### 4.3.1 概念说明

IO 库的 Verilog 模型目的非常克制：**让门级网表能仿真**，而不是精确复现模拟行为。它的三个设计选择：

1. **能不建模就不建模**：8 个电源 pad 是只有端口声明的空壳；串联电阻 pad 用一个开关原语表达；只有 PBMUX/PWE 有真正的门级逻辑。
2. **延迟全部写 0**：所有 specify 路径延迟都是 `(0, 0)`。真实延迟由 liberty 提供，后仿时用 SDF 反标（`$sdf_annotate`）把库特征化结果注入这些占位路径。
3. **用强度（strength）表达弱结构**：弱上/下拉用 `bufif1 (weak0, weak1)`，串联电阻用 `rtran`（阻性开关）而不是 `tran`（理想开关）。

这种「零延迟 + SDF 反标」是工业 IO 模型的标准做法，读懂它就读懂了门级仿真的延迟来源。

#### 4.3.2 核心流程

文件里 13 个模块分三类：

```text
① 空壳（9 个）：CUT、VDD1/3、VSS1/3、VDDIO3、VSSIO3、VDD1A、VSS1A
     只有 inout 端口，无任何语句 —— 仅为让网表通过编译、电源网络有落点
② 无源开关（2 个）：PAR（rtran：阻性）、PAR_5（tran：近似理想）
     双向传递逻辑值，rtran 额外降低强度以体现串联电阻
③ 门级逻辑（2 个）：PWE（nand+and）、PBMUX（上下拉/驱动/接收三段）
     功能用原语描述，specify 全部 (0,0) 占位
```

#### 4.3.3 源码精读

文件骨架与 license 头之后，第一个模块 CUT 展示了 `celldefine` 与条件编译：

[icsIOA_N55_3P3.v:L17-L40](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L17-L40) —— `` `celldefine`` 把模块标记为库单元（供综合/仿真器识别），`` `timescale 1 ns / 10 ps`` 定时间精度；`ifdef NOTIMING ... `else specify ... `endif` 表示：默认保留 specify 块，定义 `NOTIMING` 宏时彻底去掉时序检查（快速功能仿真常用）。CUT 的 specify 里只有两个 `specparam`：`cell_count=0`、`Transistors=0`——纯占位元数据。

电源 pad 的空壳样子（VDD1 与最短的 VSS1A）：

[icsIOA_N55_3P3.v:L44-L49](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L44-L49)、[L119-L123](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L119-L123) —— 只有端口列表。注意 VDD1 的端口表 `(VDD1, VDDIO, VSSIO)` 与 CDL/LEF 一致，电源 pad 在网表里充当「电源网络的连接点」，仿真时不产生行为。

PWE（晶振 pad）的功能建模正好对应 liberty 的两个 function：

[icsIOA_N55_3P3.v:L130-L146](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L130-L146) —— `nand U0 (XOUT, E, XIN)` 实现 `XOUT = !(XIN&E)`，`and U1 (XC, XIN, E)` 实现 `XC = E&XIN`，与 liberty [L841](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L836-L842) 的 `function : "(!(XIN&E))"` 和 [L958](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L956-L958) 的 `function : "(E&XIN)"` 逐字对应——**跨视图一致性可以直接肉眼验证**。specify 块 [L139-L144](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L139-L144) 用 `(XIN -=> XOUT)=(0, 0)` 等占位延迟（`-=>` 表示负单态路径，对应 nand 的反相关系）。

PBMUX 是全文件最精细的模型，分四段读：

[icsIOA_N55_3P3.v:L150-L197](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L150-L197) ——

- 弱上/下拉 [L164-L165](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L164-L165)：`bufif1 (weak0, weak1) (PAD_I, 1'b1, PU)`——PU=1 时把 PAD_I 弱驱动到 1；PD 同理弱驱动到 0。这实现 liberty 的 `pull_up_function/pull_down_function`；
- 接收路径 [L167-L169](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L167-L169)：`buf (C, C0); and (C0, C_BUF, IE); pmos (C_BUF, PAD, 1'b0)`——常通 pmos 作模拟串联电阻，整体即 `C = PAD & IE`，对应 liberty `function : "(PAD&IE)"`；
- 发送驱动 [L171-L179](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L171-L179)：`nand/nor` 算出 DATA_P/DATA_N，pmos/nmos 组成输出开关，`pmos (PAD, PAD_O, 1'b0)` 常通接入；
- 无源桥 [L180](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L180)：`rtran (A, PAD)`——核侧端子 A 与 PAD 之间是阻性双向通路。

specify [L186-L195](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L186-L195) 里 `if ((DS1 == 1'b0)&&(DS0 == 1'b0)) (I => PAD)=(0, 0);` 等四行条件路径与 liberty 的四组 when 表一一对应；`(OE => PAD) = (0, 0, 0, 0, 0, 0)` 是三态路径的六元组延迟（含 Z 边沿），对应 liberty 的 enable/disable 两条弧。另外 [L182-L184](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L182-L184) 的 DS0_tmp/DS1_tmp/CS_tmp 三个 pmos 输出悬空——它们只为表达输入负载存在，不参与功能。

两个串联电阻 pad 的对比浓缩在两行：

[icsIOA_N55_3P3.v:L206](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L201-L208) `rtran (PAD,A)`（PAR，阻性开关） vs [L217](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L212-L219) `tran (PAD,A)`（PAR_5，理想开关）。

#### 4.3.4 代码实践

**实践目标**：用 iverilog 验证 PAR 的无源模型确实双向导通、且 PWE 功能与 liberty function 一致。

1. 写一个小 testbench（示例代码）：

```verilog
`timescale 1ns/1ps
module tb;
  reg A;
  wire PAD;
  P65_1233_PAR u_par (.A(A), .PAD(PAD), .VDDA(), .VSSA(), .VDD(), .VSS());
  initial begin
    A = 1'b0; #10 A = 1'b1; #10 A = 1'b0; #10 $finish;
  end
  always #5 $display("%0t A=%b PAD=%b", $time, A, PAD);
endmodule
```

2. 运行：`iverilog -o par_sim tb_par.v IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v && vvp par_sim`（iverilog 未安装时执行 `sudo apt-get install iverilog`，或改用任意 Verilog 仿真器；无法运行则做源码阅读型验证）。
3. **需要观察的现象**：PAD 跟随 A 变化（rtran 双向传递逻辑值），且跳变与 A 同时刻发生（无延迟）。
4. **预期结果**：日志中每行 `A` 与 `PAD` 相同。反向驱动 PAD（把 PAD 设为 reg、A 设为 wire）也应成立。波形边沿无延迟佐证了「延迟靠 SDF 反标」的设计。**待本地验证**（不同仿真器对 `rtran` 强度处理略有差异）。
5. 加做对照实验：把实例换成 `P65_1233_PAR_5`，观察行为一致（tran 与 rtran 在逻辑仿真层面都导通，区别只在强度）。

#### 4.3.5 小练习与答案

**练习 1**：为什么电源 pad 的 Verilog 模型是空壳，而 CDL 里它们却有晶体管和二极管？
**答案**：Verilog 模型服务于数字门级仿真，电源 pad 没有逻辑行为，只需让网表可编译、电源端口可连接；CDL 服务于电路级仿真和 LVS，ESD 二极管/钳位管的物理实现必须保留。

**练习 2**：`(0, 0)` 的 specify 延迟在完整后仿流程中如何变成真实延迟？
**答案**：STA 工具基于 liberty 计算 SDF 文件，仿真时 `$sdf_annotate` 把每个路径的 `(rise, fall)` 延迟反标到对应 specify 路径上，覆盖 0 占位值。

**练习 3**：`rtran` 和 `tran` 的区别是什么？为什么 PAR 用前者、PAR_5 用后者？
**答案**：`tran` 是理想开关，`rtran` 是阻性开关（信号通过时强度被削弱）。PAR 串联电阻大（见 4.4.3 的 W/L 计算），用 rtran 体现电阻对驱动强度的衰减；PAR_5 电阻小约 81 倍，近似理想导通，用 tran。

### 4.4 CDL 晶体管级网表：pad 的物理实现

#### 4.4.1 概念说明

CDL（Circuit Description Language）是 SPICE 方言的连接表。IO 库 CDL 的组织分四层：

1. **控制语句与器件库桩件**（约 28–140 行）：`*.RESI`、`*.SCALE METER` 等控制注释 + 38 个空 `.SUBCKT`（`re_*` 电阻、`var*` 可变电阻、`mom_*` 电容）。它们是「代工厂器件占位」——版图/LVS 工具认得这些名字，网表本身不给内容。
2. **辅助逻辑单元**：反相器、NAND/NOR、电平移位器、输出级 MUX_PAD——pad 内部的小标准单元。
3. **13 个 pad 主体**：4 个信号 pad + CUT + 8 个电源 pad（CORNER 与 9 个 FILLER 是纯结构件，不进 CDL，与 u4-l1 的 liberty 覆盖结论一致）。
4. **ESD 器件**：`DD*` 二极管与钳位 MOS 散布在各 pad 内。

读懂 CDL 的钥匙是一条 MOS 语句的六个位置：`名字 漏 栅 源 衬底 模型 W L m`。衬底接哪个电源，就说明这只管子属于哪个域。

#### 4.4.2 核心流程

以 PAR（串联电阻 pad）为例，它虽然名义上是「一颗电阻」，物理上却有四类器件：

```text
PAD ──[X2: re_ppo_sab_2t 多晶电阻 W=8u L=3.34u]── A     ← 信号串联电阻本体
PAD ── M0 (pm3p3, m=18) ── VDDA                      ← 上钳位（ESD）
PAD ── M1 (nm3p3, m=20) ── VSSA                      ← 下钳位（ESD）
N0 ──[X0/X1 电阻]── VSSA，DD0 二极管                  ← 触发/泄放网络
```

ESD 事件（ns 级千伏尖峰）到来时，二极管先导通、钳位管泄放大电流，把 PAD 电压钳制在安全范围；正常信号下这些结构只表现为寄生电容——这正是 liberty 里 2.7 pF 的来源。

#### 4.4.3 源码精读

文件开头把器件库桩件与量纲交代清楚：

[ICSIOA_N55_3P3.cdl:L15-L26](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L15-L26) —— `*.RESI = 2000`（默认方块电阻 2000 Ω/sq 的抽取提示）、`*.SCALE METER`（SI 单位后缀）等都是工具控制注释；随后 [L76-L80](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L76-L80) 的 `re_ppo_sab_2t` 就是后面反复出现的 P 型多晶电阻桩件（2t=两端子，另有 3t 三端子带衬底端版本）。

CUT 单元展示「纯 ESD 结构件」长什么样：

[ICSIOA_N55_3P3.cdl:L148-L158](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L148-L158) —— 8 只 `dio_3p3_pp_nw_lp` 二极管（DD0–DD9）在 VDDA/VSSA/VDDIO/VSSIO 与内部节点之间交叉连接，没有一个晶体管。这解释了 u3-l6 的结论：CUT 在 liberty 的 12 个单元之外——没有逻辑，就没有时序模型（但它在 Verilog/CDL 里都存在）。

PAR 与 PAR_5 的差异浓缩为一行电阻的几何：

[ICSIOA_N55_3P3.cdl:L164-L172](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L164-L172) —— PAR 的串联电阻 `X2 PAD A re_ppo_sab_2t W=8u L=3.34u M=1`；[L178-L186](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L178-L186) 中 PAR_5 为 `W=78u L=400n`。同种材料的电阻满足

\[ R = R_s \cdot \frac{L}{W}, \qquad \frac{R_{\text{PAR}}}{R_{\text{PAR\_5}}} = \frac{3.34/8}{0.4/78} \approx 81.4 \]

两挡串联电阻相差约 81 倍，与 u4-l1 的盘点结论吻合（绝对阻值需查数据手册，`*.RESI` 只是抽取默认值）。其余器件：M0/M1 是 `W=25u`、`m=18/20` 的钳位管（一条语句顶 18/20 只并联管），DD0 是触发二极管。

PBMUX 主体是层次化设计的范例：

[ICSIOA_N55_3P3.cdl:L422-L449](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L422-L449) —— `*.PININFO` 用两行标注 16 个端子的方向（全部信号 `:I`/`:O`，电源 `:B`）；顶层 22 只 MOS 全是 `nm3p3/pm3p3`（IO 域的 ESD/输出结构）+ X3/X4 两只多晶电阻；[L450-L476](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L450-L476) 实例化 28 个辅助单元：6 个 `invio`、2 个 `nor2`、各 1 个 `nand2`/`nor2_w2`/`inv_NW08` 等核域逻辑，9 个 `level_shifter*`（[L470-L475](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L470-L475)）负责 OE/OD/DS0/DS1/PU/PD/CS 的跨域，最后 [L455](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L455) 的 MUX_PAD（49 只 3.3 V MOS）做真正的输出级。

PWE 同样是层次结构（顶层 4 只 MOS + 电平移位/施密特/大 NAND 三个子电路）：

[ICSIOA_N55_3P3.cdl:L535-L545](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L535-L545) —— `XI3/XI1/XI4` 分别引用 `P65_1233_PWE_lever_shift`（注意原文件拼写 lever_shift）、`P65_1233_PWE_shimit`（施密特触发整形，见 [L499-L507](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L499-L507)）与 `P65_1233_PWE_nand`（[L513-L529](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L513-L529)，内含 `W=520u/440u` 的巨型晶体管——晶振 pad 要驱动晶振负载）。

电源 pad 的两类实现对比：

- [ICSIOA_N55_3P3.cdl:L551-L558](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L551-L558) VDD1：pm3p3（m=18）+ nm3p3（m=20）钳位管 + 二极管 + 两只电阻——有源钳位；
- [ICSIOA_N55_3P3.cdl:L637-L643](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L637-L643) VSS3：四只二极管（DD0–DD3），无晶体管——纯二极管钳位的星形连接（VSS 对 VDD/VDDIO/VSSIO 各方向都有泄放路径）。

#### 4.4.4 代码实践

**实践目标**：统计 CDL 中所有 `.SUBCKT` 的器件数，找出「pad 主体」与「辅助单元」的规模差异。

1. 操作步骤（逐条执行）：

```bash
cd IP/IO/ICsprout_55LLULP1233_IO_251013/cdl
grep -c '^\.SUBCKT' ICSIOA_N55_3P3.cdl
grep -n '^\.SUBCKT' ICSIOA_N55_3P3.cdl
sed -n '422,477p' ICSIOA_N55_3P3.cdl | grep -c '^M'
sed -n '422,477p' ICSIOA_N55_3P3.cdl | grep -c '^X'
```

2. **预期结果**：共 69 个 `.SUBCKT`；PBMUX 区间内 `^M` 计 22、`^X` 计 30。其中 16 个 `P65_*` 是 pad/子电路主体，其余 53 个是电阻桩件与辅助单元。
3. 需要观察的现象：器件库桩件区间（`re_*`/`var*`）的 `.SUBCKT` 内部为空；辅助逻辑单元只有 2–8 只管；MUX_PAD 有 49 只；pad 顶层 + 子电路合计规模上百——层次化命名（`XI` 前缀实例）让每个单元可独立复用与验证。
4. 若想进一步展开层次，把每个 `X... / 子电路名` 的目标子电路器件数累加（递归），可得到 PBMUX 总 MOS 语句数约 195（不含 `m=` 并联系数、二极管与电阻），见第 5 节综合实践的参考答案。

#### 4.4.5 小练习与答案

**练习 1**：`.PININFO` 里电源端子为什么标 `B` 而不是 `I`/`O`？
**答案**：`B` 表示双向。电源端子既灌入电流（供电时）也可能流出（ESD 泄放、被钳位时），方向不定，因此按双向处理；信号输入 `I`、输出 `O`。

**练习 2**：`M0 VDDA VDDA PAD VDDA pm3p3_lp W=25u L=650n m=18`（PAR 内）这只管子的栅接在哪？它什么时候导通？
**答案**：栅接 VDDA（电源），衬底接 VDDA，漏/源是 VDDA 与 PAD。它是 PMOS 上钳位管：正常工作时栅极是高电平、截止；ESD 使 PAD 电压超过 VDDA+阈值时，寄生/本体二极管路径导通泄放。`m=18` 表示 18 只并联以满足 ESD 电流容量。

**练习 3**：为什么 CORNER 和 9 个 FILLER 不出现在 CDL？
**答案**：它们没有任何有源器件——CORNER 只是拐角几何连接件，FILLER 是填充件，只有金属连续性需求，由 LEF/GDS 描述即可；无电路行为就没有 CDL（也没有 liberty），这与 u3-l6/u4-l1 的视图覆盖矩阵一致。

## 5. 综合实践

把本讲三个视图串起来，完成规格指定的两个任务。

### 任务一：三工艺角电容对比

**目标**：提取 tt/ff/ss 三个 corner 下 `P65_1233_PAR` 的引脚电容与驱动电流字段，计算工艺角偏差。

**操作步骤**（示例代码）：

```python
import re
base = "IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_{}.lib"
corners = ["tt_1p2_3p3_25c", "ff_1p32_3p63_125c", "ss_1p08_2p97_125c"]
data = {}
for c in corners:
    text = open(base.format(c)).read()
    cell = re.search(r'cell \("P65_1233_PAR"\) \{(.*?)cell \("', text, re.S).group(1)
    pin = re.search(r'pin \(PAD\) \{(.*?)\}', cell, re.S).group(1)
    cap = re.search(r'\n\t\t\t\tcapacitance : ([\d.]+)', pin).group(1)
    dc  = re.search(r'drive_current : ([\d.]+)', pin).group(1)
    data[c] = (float(cap), float(dc))
for c, (cap, dc) in data.items():
    print(f"{c:18s} PAD_cap={cap:.3f} pF  drive={dc:.0f} mA")
tt, ff, ss = (data[c][0] for c in corners)
print(f"ff/ss = {(ff-ss)/ss:+.1%}   ss/tt = {(ss-tt)/tt:+.1%}   ff/tt = {(ff-tt)/tt:+.1%}")
```

**预期结果**（已按当前 HEAD 核对）：

| corner | 电压（核/IO） | 温度 | PAD 电容 (pF) | drive_current |
| --- | --- | --- | --- | --- |
| tt_1p2_3p3_25c | 1.20 / 3.30 V | 25 ℃ | 2.726 | 4 mA |
| ff_1p32_3p63_125c | 1.32 / 3.63 V | 125 ℃ | 2.512 | 4 mA |
| ss_1p08_2p97_125c | 1.08 / 2.97 V | 125 ℃ | 3.471 | 4 mA |

偏差：ff 相对 ss 约 **−27.6%**，ss 相对 tt 约 **+27.3%**，ff 相对 tt 约 **−7.9%**；引脚 A 的电容（tt 2.714 / ff 2.389 / ss 3.475 pF）ff 相对 ss 约 −31.2%。而 `drive_current` 三个 corner 都是 4 mA——它是**标称规格值**，不随工艺角缩放；真正随角变化的是延迟表和电容。结论：SS 慢角下 pad 电容偏大近三成，若误用 tt 角数据做负载预算，时序会系统性乐观。

### 任务二：PBMUX 晶体管清点与电源域标注

**目标**：从 CDL 定位 `P65_1233_PBMUX` 的 `.SUBCKT`，数晶体管并标注端子电源域。

**操作步骤**：

1. `grep -n 'SUBCKT P65_1233_PBMUX' IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl` 定位到 [L422](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L422)。
2. 按 4.4.4 的方法统计：顶层 22 只 MOS（12 nm3p3 + 10 pm3p3）+ 30 个 X 实例（含 2 只顶层多晶电阻）。
3. 递归展开 28 个逻辑子电路（invio×6、level_shifter×6、level_shifter_invn1u×3、nor2×2、inv2/inv4/inv_5p_2n/inv_4p_2n/inv_1p_2n/inv_NW08/nand2/nor2_w2/level_shifter_invn2u/level_shifter_invn8u 各 1）与 MUX_PAD，累计 MOS 语句 **约 195 条**（不含 m= 并联、二极管与电阻；自行展开时若差 1–2 只，先检查是否漏了 MUX_PAD 内 3 只 X 电阻桩件或把 DD 二极管计入了 MOS）。

**端子电源域标注表**（依据 4.2.3 的实例连接关系）：

| 端子 | 域 | 依据（典型实例） |
| --- | --- | --- |
| VDD / VSS | 1.2 V 核域 | `XI23 VDD VSS DS1 N33 / invio` 等 8 个核域逻辑单元 |
| VDDIO / VSSIO | 3.3 V IO 域 | `XI0 ... PAD VDDIO VSSIO / MUX_PAD`、顶层 22 只 3p3 MOS |
| A、I、OE、IE、OD、PU、PD、CS、DS0、DS1、C | 1.2 V 核域信号 | 进入 invio/nand2/nor2 或 level_shifter 输入级 |
| PAD | 3.3 V IO 域 | MUX_PAD 输出、X3/X4 ESD 电阻所接 |

对照电源 pad 的域：VDD1 pad 的 `VDD1` 端子属核域 1.2 V（把核电源送到压焊盘）、`VDDIO/VSSIO` 属 IO 域；VSS3/VSS1 的压焊端子属各自地域。至此，「一个信号从核域 1.2 V 出发，经核域逻辑、电平移位器、3.3 V 输出级到达 PAD」的完整物理通路在网表层面得到确认。

## 6. 本讲小结

- **liberty 视角**：pad 单元用 `pad_cell/is_pad` 自我标识，`drive_current`（PAR 系 4 mA、PBMUX 12 mA）是标称值、不随 corner 缩放；被动 pad 只有电容没有时序弧，PBMUX/PWE 的弧用 `when` 按驱动档位分表，另有 `three_state_enable/disable` 两类 pad 特有弧；`area` 与 LEF SIZE 精确互验（8450=65×130）。
- **双电压域**：文件名 `1p2_3p3`、库头 `nom_voltage 3.3` 与 `vil/vih=1.42/1.88`、CDL 两套器件（`*1p2_lvt_lp` 核域 / `*3p3_lp` IO 域）与 9 个电平移位器，是同一事实在三个视图的投影。
- **Verilog 视角**：模型「能省则省」——电源 pad 空壳、串联电阻用 `tran/rtran`、逻辑 pad 零延迟 specify 占位，真实延迟靠 SDF 反标；`ifdef NOTIMING` 可整体摘除时序块。
- **CDL 视角**：网表分「器件桩件 / 辅助逻辑 / pad 主体 / ESD」四层；PBMUX 顶层 22 只 IO 域 MOS + 30 个实例、递归约 195 条 MOS 语句；串联电阻 PAR/PAR_5 仅电阻几何不同（L/W 之比约 81 倍）；CUT 与部分电源 pad 是纯二极管结构件。
- **跨视图覆盖**：23 个 cell_list 单元中，13 个进 Verilog/CDL（4 信号 pad + CUT + 8 电源 pad），12 个进 liberty（无 CUT），CORNER/FILLER 只有 LEF——每多一种视图，就多回答一类问题。

## 7. 下一步学习建议

本讲之后，单元四（IO 库）完结。建议：

1. 进入 **u5-l1（CDL 晶体管级网表）**：本讲只读了 IO 库的 69 个 `.SUBCKT`，下一讲系统解读标准单元库 CDL 的 `*.PININFO`、`W/L/m` 语法与 HVT/LVT/RVT 三套器件模型名（`hvt/lvt/svt`，注意与 IO 库的 `3p3/1p2` 命名维度不同）。
2. 随后做 **u5-l2（多视图一致性检查）**：本讲已两次肉眼验证一致性（PWE 的 function 对应、area 对 SIZE），下一讲把 cell_list/LEF/verilog/CDL/liberty 五视图的单元与引脚对照写成自动化脚本。
3. 延伸阅读：`doc/ICSIOA_N55_3P3_Application_Datasheet_1P6M.pdf`（串联电阻绝对值、ESD 等级等规格以数据手册为准）；liberty 语法可对照 Synopsys Liberty 用户手册的 `pad_cell/is_pad/three_state_*` 章节。
