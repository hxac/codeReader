# 标准单元库命名与三阈值家族

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 ICS55 为什么同时发布 H7CH/H7CL/H7CR 三套标准单元库，以及 HVT/LVT/RVT 三种阈值电压各自的速度—功耗取舍。
2. 拿到任意一个单元名（例如 `AOI2BB1X1P4H7L`），能逐段拆解出「功能助记符 + 输入配置与变体字母 + 驱动强度 + 库后缀」四段信息。
3. 用脚本对三个 `cell_list` 做功能类别与驱动强度的归类统计，并找出三库之间唯一的覆盖差异单元。

本讲是单元三的第一讲。前面两个单元我们看的是「工艺」——金属层、过孔、SITE；从本讲开始，我们进入「库里到底装了哪些单元」。**cell_list 是整个标准单元库的目录页**，读懂它才能读懂后续的 LEF、verilog、CDL 和 liberty。

## 2. 前置知识

### 2.1 阈值电压（Vth）是什么

MOS 晶体管有一个「导通门槛」：栅极电压必须超过阈值电压 \( V_{th} \)，沟道才会形成、器件才会导通。\( V_{th} \) 不是工艺常数——同一套光刻和掺杂流程，通过不同的沟道掺杂注入，可以造出阈值高低不同的晶体管系列。于是同一个 55nm 工艺可以同时提供：

- **HVT（High-Vth，高阈值）**：不容易导通，开关速度慢，但关断时的漏电流极小。
- **LVT（Low-Vth，低阈值）**：很容易导通，开关快，但漏电流大。
- **RVT（Regular/Standard-Vth，常规阈值）**：介于两者之间，也叫 SVT。

漏电流随阈值电压的变化是指数关系，一阶近似为：

\[
I_{off} = I_0 \, e^{-\,qV_{th}/(n k T)}
\]

其中 \( q \) 是电子电荷，\( k \) 是玻尔兹曼常数，\( T \) 是绝对温度，\( n \) 是亚阈值斜率系数。这个指数关系意味着 **Vth 只提高零点几伏，静态漏电就可能下降几个数量级**——这正是多阈值设计的价值所在。

### 2.2 多阈值设计（MTCMOS）的基本套路

数字芯片的时序路径总有紧有松：

- **关键路径**（critical path）：时序快撑不住了 → 换 LVT 单元换速度；
- **非关键路径**（slack 很大）→ 换 HVT 单元省漏电；
- **中间地带** → 用 RVT。

综合工具（如 yosys + ABC、或商业工具）在多阈值库里做这种交换，术语叫 multi-Vt optimization。因此 PDK 通常把三种阈值的库做成**单元名单几乎相同的平行三套**，让工具可以「同名换型」——`INVX1H7L` 与 `INVX1H7H` 逻辑功能、引脚完全一样，只是速度和漏电不同。本讲后面会用真实数据验证 ICS55 正是这样组织的。

### 2.3 驱动强度（drive strength）与 X 编号

同名功能单元会按输出驱动能力分成多档，命名上用 `X` 加数字表示**相对驱动倍数**：

- `X1` 是基准档；
- `X0P5` = 0.5 倍（`P` 代替小数点，因为点号在文件名和许多 EDA 格式里有特殊含义）；
- `X1P4` = 1.4 倍；`X2`、`X4`、`X8`、`X20` 依次类推。

一阶近似下，驱动电流与 X 编号成正比，而门延迟近似为：

\[
t_p \approx \frac{C_L \, V_{DD}}{2 I_{drive}}
\]

所以负载 \( C_L \) 越大（扇出多、连线长），越需要选高 X 档；但高 X 档晶体管更宽、面积更大、输入电容也更大，会加重前一级的负担——选型是综合工具的权衡问题。

### 2.4 cell_list 回顾

u1-l2 已经建立：每个库的 `cell_list/` 下有一个纯文本名单，一行一个单元名，按字母排序；它不是全集（LEF 里还有 37 个未列入的 ANT 天线单元）。本讲我们就以这个「目录页」为数据源做全库盘点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md:L78-L103](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L78-L103) | Contents 目录树，注释里明确标注 H7CH=HVT、H7CL=LVT、H7CR=RVT |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L748) | HVT 库单元名单，748 行 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt#L1-L747) | LVT 库单元名单，747 行 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt#L1-L747) | RVT 库单元名单，747 行 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L23-L24) | HVT 库晶体管网表，器件模型名带 `hvt` 字样（佐证） |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95-L102) | 仿真模型，用于抽查单元的真实端口与功能 |

库名 `ics55_LLSC_H7C_V1p10C100` 中，README 只解释了「55nm LLSC H7C standard cell library version 1.10」；`LLSC`、`H7C` 的全称与 `C100` 的含义仓库未说明，**待确认**。

## 4. 核心概念与源码讲解

### 4.1 阈值电压家族（HVT/LVT/RVT）

#### 4.1.1 概念说明

ICS55 的标准单元区不是一套库，而是**三套平行的库**：`ics55_LLSC_H7CH`、`ics55_LLSC_H7CL`、`ics55_LLSC_H7CR`。三者共用同一个工艺（u2 讲过的 N551P6M 金属栈）、同一种行高（core7 site，1.4μm），单元的逻辑功能与版图框架一致，区别在于晶体管的阈值注入——这直接决定了速度与漏电的取舍。

这种「三套平行库」就是 2.2 节所说的多阈值设计的物质基础。

#### 4.1.2 核心流程

一个典型的多阈值使用流程：

1. 综合时把三个库的 liberty 都读入工具（liberty 需 `make unzip` 下载，见 u1-l3）；
2. 初始映射先用 RVT；
3. 时序不满足的路径，工具把其中的单元换成**同名 LVT 单元**（例如 `AOI21X2H7R` → `AOI21X2H7L`），路径变快；
4. slack 富余的路径换 **HVT 单元**，降低静态漏电；
5. 迭代直到时序与功耗折中收敛。

关键前提是：**三库的单元名除了最后三个字符（H7H/H7L/H7R）外完全一致**，工具才能「换型不换名」。下面马上用数据验证这一点。

#### 4.1.3 源码精读

README 的 Contents 目录树给三套库下了官方定义：

> [README.md:L80](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L80) — `├── ics55_LLSC_H7CH  # HVT standard cells`

> [README.md:L88](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L88) — `├── ics55_LLSC_H7CL  # LVT standard cells`

> [README.md:L96](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L96) — `└── ics55_LLSC_H7CR  # RVT standard cells`

目录树本身还说明：三套库内部结构完全相同（cdl、cell_list、doc、gds、lef、liberty、verilog 七个视图目录一一对应）。

阈值差异在 CDL 网表里有直接证据。三个 CDL 的第 23–24 行都是库开头的 INV 模板单元的两只 MOS，但器件模型名不同：

> [ics55_LLSC_H7CH.cdl:L23-L24](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L23-L24) — HVT 库的 NMOS/PMOS 用 `nm1p2_hvt_lp` / `pm1p2_hvt_lp` 模型。

H7CL 的 CDL（文件名为 `ics55_LLSC_H7CL.cdl`）同一位置是 `nm1p2_lvt_lp` / `pm1p2_lvt_lp`；H7CR 是 `nm1p2_svt_lp` / `pm1p2_svt_lp`（svt = standard Vth，即 README 所说 RVT）。模型名中的 `1p2` 一般指 1.2V 核压器件族、`lp` 指低功耗槽氧化（业界惯例读法，**待确认**）。CDL 的详细语法留到 u5-l1，这里只需要它作为「三库 = 三种阈值器件」的物证。

单元名层面，库后缀是名字的最后三个字符，按尾字符区分家族：

| 后缀 | 库目录 | 阈值 | 例 |
| --- | --- | --- | --- |
| `H7H` | ics55_LLSC_H7CH | HVT（高阈值） | `INVX1H7H` |
| `H7L` | ics55_LLSC_H7CL | LVT（低阈值） | `INVX1H7L` |
| `H7R` | ics55_LLSC_H7CR | RVT（常规阈值） | `INVX1H7R` |

对比三个名单的开头三行，可以看到除后缀外逐字相同：

> [ics55_LLSC_H7CH.txt:L1-L3](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L3) — `ADDFX1H7H`、`ADDFX1P4H7H`、`ADDFX2H7H`

> [ics55_LLSC_H7CL.txt:L1-L3](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt#L1-L3) — 同上，后缀换成 `H7L`

> [ics55_LLSC_H7CR.txt:L1-L3](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt#L1-L3) — 同上，后缀换成 `H7R`

#### 4.1.4 代码实践

**实践目标**：用 CDL 器件模型名独立验证「三库三阈值」，不依赖 README 的文字。

**操作步骤**：

```bash
cd icsprout55-pdk
grep -c 'hvt' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl
grep -c 'lvt' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cdl/ics55_LLSC_H7CL.cdl
grep -c 'svt' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cdl/ics55_LLSC_H7CR.cdl
grep -m2 -n 'hvt' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl
```

**需要观察的现象**：三条计数命令分别输出 5822、5822、5818（本讲撰写时实际运行的结果）；`-m2` 样例命令打印出 `nm1p2_hvt_lp` / `pm1p2_hvt_lp` 两条 MOS 语句。

**预期结果**：每个 CDL 只包含本库阈值类型的模型引用——`grep -c 'lvt'` 在 H7CH 的 CDL 上应为 0（反之亦然），说明三套库的网表按阈值严格分流，不存在混用。

**待本地验证**：H7CH/H7CL 与 H7CR 的计数差（5822 对 5818）与 4.3 节发现的「H7CH 多一个 SDFFRX0P5 单元」之间的对应关系，读者可自行数出该单元在 CDL 中的 MOS 行数加以印证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ICS55 要同时发布三套库，而不是只发布一套「最好的」？

**参考答案**：因为速度和漏电不可兼得。LVT 快但漏电大，HVT 省电但慢，RVT 居中。芯片里不同路径的时序压力不同，设计者需要按路径逐个单元地选择阈值，才能在满足时序的前提下把静态功耗压到最低——这只有三套平行库同时可用才做得到。

**练习 2**：只看单元名 `DFFQX2H7L`，如何判断它属于哪个阈值库？

**参考答案**：看最后三个字符的尾字母：`H7L` 以 `L` 结尾 → H7CL → LVT 库。同理 `H7H` → HVT，`H7R` → RVT。

**练习 3**：CDL 器件模型 `pm1p2_svt_lp` 出现在哪个库的网表里？

**参考答案**：H7CR（RVT 库）。`svt` 即 standard Vth，对应 README 标注的 RVT。

### 4.2 单元命名语法

#### 4.2.1 概念说明

把任意单元名从左到右切，可以分成四段：

```
AOI2BB1X1P4H7L
└┬┘└┬┘└─┬─┘└┬┘
功能  输入  驱动  库
助记符 配置+ 强度  后缀
      变体
```

1. **功能助记符**：INV、BUF、AND、OR、NAND、NOR、XOR、XNOR、AO/OA 系、MUX、DFF、LATCH、ICG 等，说明这个单元「干什么」。
2. **输入配置与变体字母**：数字说明输入分组（如 `AO21` = 一组 2 输入与 + 一组 1 输入直通）；穿插的字母是变体标记，常见有：
   - `B`：该组输入带反相气泡（bubble），如 `NAND2B`、`AOI2BB1`（`BB` = 该组两个输入都取反）；
   - `R`：异步复位（`DFFR`）；
   - `S`：扫描端口（`SDFF`）或置位（`DFFS`）；
   - `N`：下降沿触发（`DFFN`）或低有效（`ICGN`）；
   - `Q`/`QN`：输出 Q / 反相输出 QN；
   - `T`、`E`：见于 `DFFTRQ`、`ESDFFQ`，推断分别为测试与使能端口（业界惯例读法，**待确认**，可在 u3-l5 读 verilog 模型时验证）。
3. **驱动强度**：`X` + 相对倍数，`P` 代替小数点（`X0P5` = 0.5×，`X1P4` = 1.4×）。
4. **库后缀**：`H7H` / `H7L` / `H7R`。

少数单元没有驱动段：`FILLCAP4H7H`（去耦电容填充）、`TIEHIH7H` / `TIELOH7H`（常量电平钳位）——它们不驱动逻辑负载，无档位可言。

#### 4.2.2 核心流程

解码算法可用下面的伪代码描述：

```
输入: 单元名 name
1. 若 name 不以 H7H / H7L / H7R 结尾 → 报错（不是标准单元命名）
2. vt ← 尾字符 {H,L,R} 对应的家族; core ← 去掉最后 3 个字符
3. 在 core 中从右向左找第一个「X 后跟数字/P」的子串 → drive
   若找不到 → drive ← 无（FILLCAP/TIE 类）
4. func ← core 去掉 drive 后的剩余部分
5. 输出 (func, drive, vt)
```

对应的正则（Python 语法，示例代码）：

```python
m = re.match(r"^(?P<func>.+?)(?P<drive>X[0-9P]+)?(?P<suffix>H7[HLR])$", name)
```

注意 `func` 用非贪婪 `.+?`，让驱动段尽可能靠右匹配，避免把功能名里的字母吞进驱动段。

#### 4.2.3 源码精读

用几个真实条目走一遍上面的算法（均出自 H7CH 名单）：

> [ics55_LLSC_H7CH.txt:L7-L17](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L7-L17) — AND2 的 11 个驱动档：`AND2X0P5H7H`、`AND2X0P7H7H`、`AND2X12H7H`、`AND2X16H7H`、`AND2X1H7H`、`AND2X1P4H7H`、`AND2X2H7H`、`AND2X3H7H`、`AND2X4H7H`、`AND2X6H7H`、`AND2X8H7H`（文件按字母排序，所以 `X12`、`X16` 排在 `X1` 前面——比较的是字符串而非数值）。

> [ics55_LLSC_H7CH.txt:L43-L51](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L43-L51) — AO21 的 9 个驱动档，`AO21X0P5H7H` 到 `AO21X8H7H`。

功能编码的含义可以拿 verilog 模型的端口来佐证：

> [ics55_LLSC_H7CH.v:L95-L102](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95-L102) — `ADDFX1H7H` 的模块：端口 `(CO, S, A, B, CI)`，内部 `xor I0(S, A, B, CI)` 与三条 and 加一条 or 生成 CO。可见 `ADDF` = full adder（全加器，输出和 S 与进位 CO），`ADDH`（L4–L6）即 half adder 半加器。

> [ics55_LLSC_H7CH.v:L1596](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L1596) — `AO21X1H7H` 的模块端口 `(Y, A0, A1, B0)`：两个 A 输入先进与门，再与 B0 相或——`AO` = AND-OR，`21` = 与组 2 输入 + 或组 1 输入。`AOI` 系只是输出再取反（INvert），`OA` 系顺序对调（OR-AND）。

> [ics55_LLSC_H7CH.txt:L264-L267](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L264-L267) — `FILLCAP16H7H`、`FILLCAP32H7H`、`FILLCAP4H7H`、`FILLCAP8H7H`：无 X 驱动段的填充去耦电容单元，数字 4/8/16/32 表示容量/宽度档位。

> [ics55_LLSC_H7CH.txt:L704-L705](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L704-L705) — `TIEHIH7H`、`TIELOH7H`：输出常 1 / 常 0 的钳位单元，用于把不用的输入接到确定电平，同样无驱动档。

各家族的驱动档位覆盖并不相同，BUF/INV 最全（17 档，含 `X2P5`、`X3P5`、`X5`、`X7`、`X10`、`X20`），2 输入基础门 11 档，XOR/XNOR 与 MUXI2 只有 8 档，时序单元（DFF/SDFF/LATH 系）通常只有 `X0P5` ~ `X4` 中的一小段——扇出需求越多样的家族，档位越密。

#### 4.2.4 代码实践

**实践目标**：写一个通用解码器，把三个名单的每个单元名拆成 `(func, drive, vt)` 三元组。

**操作步骤**：将下面脚本存为 `parse_cells.py`（示例代码，放在仓库外的任意目录运行均可，不要写进 PDK 仓库）：

```python
#!/usr/bin/env python3
"""解析 ICS55 cell_list：单元名 -> (功能, 驱动, 阈值家族)。示例代码"""
import re
import sys

LIBS = {
    "HVT": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt",
    "LVT": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt",
    "RVT": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt",
}
PAT = re.compile(r"^(?P<func>.+?)(?P<drive>X[0-9P]+)?(?P<suffix>H7[HLR])$")
VT = {"H": "HVT", "L": "LVT", "R": "RVT"}

def parse(name):
    m = PAT.match(name)
    if not m:
        raise ValueError(f"无法解析: {name}")
    vt = VT[m.group("suffix")[-1]]          # 尾字符 H/L/R -> 家族
    drive = m.group("drive") or "(无档位)"
    return m.group("func"), drive, vt

if __name__ == "__main__":
    for vt_label, path in LIBS.items():
        with open(path) as f:
            names = [ln.strip() for ln in f if ln.strip()]
        bad = [n for n in names if not PAT.match(n)]
        print(f"{vt_label}: {len(names)} 个单元, 解析失败 {len(bad)} 个")
        for n in names[:5]:
            print("   ", n, "->", parse(n))
```

在仓库根目录运行 `python3 parse_cells.py`。

**需要观察的现象**：每个库打印「748/747/747 个单元，解析失败 0 个」，以及前五个单元的三元组，例如 `ADDFX1H7H -> ('ADDF', 'X1', 'HVT')`。

**预期结果**：正则应能覆盖全部 2242 个名字（含 `FILLCAP4H7H`、`TIEHIH7H` 这类无驱动档的名字，`drive` 组输出 `(无档位)`）。若出现解析失败，多半是遇到了新的变体字母组合，把失败名字打印出来补充正则即可。

**待本地验证**：本脚本未在撰写环境中运行（环境限制），输出数字请以本地运行为准；「解析失败 0 个」是依据本讲逐条核对名单得出的预期。

#### 4.2.5 小练习与答案

**练习 1**：解码 `AOI2BB2X1P4H7L`。

**参考答案**：功能 `AOI2BB2`（与或非门：2 输入与组 × 2，其中一组的两个输入 `BB` 均带反相气泡，输出取反），驱动 `X1P4`（1.4 倍），库 `H7L` → LVT。

**练习 2**：为什么驱动强度写成 `X0P5` 而不是 `X0.5`？

**参考答案**：单元名会出现在文件名、liberty 单元名、verilog 模块名、网表实例名等许多场合，点号在这些上下文里是分隔符或特殊字符（如文件扩展名、Verilog 的层级引用），用 `P` 代替小数点可以保证名字在任何工具里都是合法标识符。

**练习 3**：`INVX20H7H` 和 `INVX1H7H` 在物理上有什么差别？

**参考答案**：`X20` 的输出管宽度约为基础档的 20 倍，能驱动约 20 倍的负载电容（或长互连）而保持延迟可控；代价是面积约 20 倍，且输入电容也大增，会加重前一级。见 [ics55_LLSC_H7CH.txt:L278-L294](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L278-L294) 的 17 个 INV 档位。

### 4.3 功能类别盘点

#### 4.3.1 概念说明

知道命名规则后，就可以对整库做归类统计，回答「这个库到底提供了哪些类型的单元」。这不仅是好奇——综合工具能映射出什么样的电路、物理设计要处理什么样的约束，都由这份清单决定。例如：有 `ICG` 才能做时钟门控省功耗；有 `FILLCAP` 才能在布局后填补间隙并补充去耦电容；DFF 变体越多，时序优化越灵活。

#### 4.3.2 核心流程

统计流程：

1. 读入三个 cell_list；
2. 用 4.2 的解码器取功能段；
3. 按功能大类（基础门 / 复合门 / 加法器 / MUX / 触发器 / 锁存器 / 时钟辅助 / 物理辅助）聚合计数；
4. 三库横向对比，找只出现在某一个库的功能。

本讲撰写时对 H7CH 实际运行 `grep -oE '^[A-Z]+' | sort | uniq -c` 得到的家族计数，归并成大类如下：

| 功能大类 | 包含前缀 | H7CH 数量 | 说明 |
| --- | --- | --- | --- |
| 与或复合门 | AO、AOI、OA、OAI、AOA、AOAI、OAO、OAOI | 304 | 46+92+46+92+7+7+7+7，**占全库 40.6%** |
| 基础逻辑门 | NAND、NOR、AND、OR、INV、BUF、XOR、XNOR | 254 | 71+61+28+28+17+17+16+16 |
| 触发器 | DFF\*、SDFF\*、ESDFFQ | 84 | 49+32+3 |
| 锁存器 | LATH\*、LATL\* | 28 | 高电平/低电平锁存各 14 |
| MUX | MUX2、MUX4、MUXI2 | 26 | 11+8+7，MUXI 输出取反 |
| 三态与钳位 | TBUF、TINV、TIEHI、TIELO | 24 | 11+11+1+1 |
| 时钟辅助 | ICG、ICGN、DLY | 18 | 时钟门控 5+5、延迟单元 8 |
| 加法器 | ADDF、ADDH | 6 | 全加/半加 |
| 物理填充 | FILLCAP | 4 | 去耦电容填充 |
| **合计** | | **748** | |

两个值得注意的结构性事实：

- **复合门是第一大群体**。AOI/OAI 这类与或非一体门用一级晶体管网络实现两级逻辑，比「AND + OR + INV」三级级联快得多，所以库会不惜成本地把各种输入分组（`AO21`/`AO22`/`AO211`/`AO221`/`AO222`/`AO31`/`AO32`/`AO33`…）和反相输入变体（`B`/`BB`/`XB`）全铺开，让综合工具尽量把逻辑吸收进单级门。
- **三库覆盖差异极小**。H7CL 与 H7CR 的 747 个名字严格一一对应（仅后缀不同）；H7CH = 这 747 个 + 1 个独有单元：

> [ics55_LLSC_H7CH.txt:L676](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L676) — `SDFFRX0P5H7H`：带异步复位的扫描 D 触发器（0.5× 驱动），是三库之间唯一的覆盖差异。

对照 H7CR 的同段（[ics55_LLSC_H7CR.txt:L673-L691](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt#L673-L691)）可以看到 `SDFFRQX2` 之后直接就是 `SDFFSRQX1`，没有 `SDFFRX` 条目。这个单元为什么只在 HVT 库提供，仓库未说明，**待确认**。

另外提醒（承接 u1-l2 的发现）：`grep -c 'ANT'` 在三个 cell_list 上都是 0——天线二极管单元（`ANT2H7H` 等 37 个）只存在于 LEF/CDL 视图，不在名单里。做全库工具（如自写一致性检查器）时不能只拿 cell_list 当全集，这个坑会在 u5-l2 正式处理。

#### 4.3.3 源码精读

各功能段在名单中的位置（H7CH，文件按字母序）：

| 功能段 | 行号范围 |
| --- | --- |
| 加法器 ADDF/ADDH | [L1-L6](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L6) |
| 基础门 AND | [L7-L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L7-L34) |
| 复合门 AO 系 | [L35-L186](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L35-L186) |
| BUF | [L187-L203](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L187-L203) |
| DFF 系 | [L204-L252](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L204-L252) |
| 延迟 DLY | [L253-L260](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L253-L260) |
| 时钟门控 ICG/ICGN | [L268-L277](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L268-L277) |
| INV | [L278-L294](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L278-L294) |
| 锁存器 LATH/LATL | [L295-L322](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L295-L322) |
| MUX | [L323-L348](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L323-L348) |
| NAND/NOR | [L349-L480](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L349-L480) |
| OA 系 | [L481-L632](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L481-L632) |
| OR | [L633-L660](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L633-L660) |
| SDFF 系 | [L661-L692](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L661-L692) |
| TBUF/TIE/TINV | [L693-L716](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L693-L716) |
| XNOR/XOR | [L717-L748](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L717-L748) |

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写脚本解析三个 cell_list，按功能前缀和驱动强度生成统计表，比较三库覆盖差异，并找出只在一个库出现的单元。

**操作步骤**：

1. 在仓库根目录创建 `survey.py`（示例代码，建议放在仓库外的自建目录）：

```python
#!/usr/bin/env python3
"""三库普查：功能前缀 x 驱动强度统计 + 覆盖差异。示例代码"""
import re
from collections import Counter

BASE = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100"
LIBS = {
    "H7CH": f"{BASE}/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt",
    "H7CL": f"{BASE}/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt",
    "H7CR": f"{BASE}/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt",
}
SUFFIX = {"H7CH": "H7H", "H7CL": "H7L", "H7CR": "H7R"}

def load(path):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip()]

cells = {k: load(v) for k, v in LIBS.items()}
for k, v in cells.items():
    print(f"{k}: {len(v)} 个单元")

# 1) 驱动强度统计：剥掉库后缀后取最右侧 X<数字> 段
drive_pat = re.compile(r"X[0-9P]+$")
for lib, names in cells.items():
    core = [n[:-3] for n in names]              # 去 H7H/H7L/H7R
    drives = Counter((drive_pat.search(c).group(0) if drive_pat.search(c)
                      else "(无档位)") for c in core)
    print(f"\n{lib} 驱动档位: {dict(sorted(drives.items()))}")

# 2) 功能家族统计：行首连续大写字母
fam_pat = re.compile(r"^[A-Z]+")
fams = {lib: Counter(fam_pat.match(n).group(0) for n in names)
        for lib, names in cells.items()}

# 3) 覆盖差异：归一化掉库后缀后做集合运算
norm = {lib: set(n[:-3] for n in names) for lib, names in cells.items()}
h, l, r = norm["H7CH"], norm["H7CL"], norm["H7CR"]
print("\nH7CH 独有:", sorted(h - l - r))
print("H7CL 独有:", sorted(l - h - r))
print("H7CR 独有:", sorted(r - h - l))
print("三库共有:", len(h & l & r))
```

2. 运行 `python3 survey.py`。
3. 追加一步交叉验证（可直接在 shell 完成）：

```bash
wc -l IP/STD_cell/ics55_LLSC_H7C_V1p10C100/*/cell_list/*.txt
grep -n '^SDFFR' IP/STD_cell/ics55_LLSC_H7C_V1p10C100/*/cell_list/*.txt
```

**需要观察的现象**：

- 三库计数分别为 748、747、747；
- 驱动档位表覆盖 `X0P5`、`X0P7`、`X1`、`X1P4`、`X2`、`X2P5`、`X3`、`X3P5`、`X4`、`X5`、`X6`、`X7`、`X8`、`X10`、`X12`、`X16`、`X20` 以及 `(无档位)`，且高档位只出现在 BUF/INV/基础门家族；
- 「H7CH 独有」打印 `['SDFFRX0P5']`，「H7CL 独有」「H7CR 独有」为空列表，「三库共有」为 747；
- `grep -n '^SDFFR'` 只在 H7CH 名单的第 676 行命中 `SDFFRX0P5H7H`。

**预期结果**：与 4.3.2 节的表格互相印证——三库是「同一份目录 + 一个例外」的组织方式，多阈值换型在任何单元上几乎都可行（除了那一个 HVT 独有的扫描触发器）。

**待本地验证**：脚本本身未在撰写环境中运行（环境限制），上述输出数字是撰写时用 `wc -l`、`grep -oE '^[A-Z]+' | sort | uniq -c`、`grep -n '^SDFF'` 等只读命令逐步核对得到的；请以本地运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：哪类单元只在一个库里出现？是哪个库、哪个单元？

**参考答案**：`SDFFRX0P5H7H`（带异步复位的扫描 D 触发器，0.5× 驱动）只出现在 H7CH（HVT 库）名单的第 676 行；H7CL 与 H7CR 均无 `SDFFRX` 系列。这是三库之间唯一的覆盖差异。

**练习 2**：三库合计有多少个互不相同的「功能 + 驱动」组合？

**参考答案**：747 个为三库共有，加上 H7CH 独有的 1 个，共 748 个。三种阈值只是同一批逻辑实现在不同器件上的平行版本，不引入新的功能。

**练习 3**：为什么与或复合门（AO/OA 系）在库里占的条目最多（304 个，约 40.6%）？

**参考答案**：因为输入分组的组合空间大（`21`/`22`/`211`/`221`/`222`/`31`/`32`/`33`…）还要叠加反相输入变体（`B`、`BB`、`XB`）和输出是否取反（AO/AOI、OA/OAI），每种组合又配多档驱动。库愿意铺这么全，是因为单级复合门比多级级联快、省面积，铺得越全综合工具能吸收进单级的逻辑就越多。

## 5. 综合实践

**任务：为 ICS55 标准单元库制作一份「三库普查报告」。**

把本讲三个模块的能力串起来，产出一页报告，包含：

1. **库身份表**：每个库的目录名、单元数、阈值家族、CDL 器件模型名（从 `grep -m2 -n 'vt' .../cdl/*.cdl` 提取）、库后缀。
2. **功能大类分布表**：用 4.3.4 的脚本输出，按 4.3.2 的九个大类归并，给出数量与占比。
3. **驱动档位矩阵**：行 = 驱动档位（`X0P5` … `X20`），列 = 若干代表家族（INV、AND2、XOR2、DFF、AOI21），格子里填「有/无」，观察档位覆盖随家族的变化。
4. **差异清单**：归一化后缀后的三库集合差异，以及 cell_list 与 LEF 的单元数差异（LEF 的 785 个 MACRO 中多出的 37 个 ANT 单元，可用 `grep -c '^MACRO' .../lef/ics55_LLSC_H7CH.lef` 复核，为 u5-l2 埋点）。
5. **一页结论**：用三句话概括「这个库用什么命名规则、提供什么功能、三种阈值怎么选」。

全部数据只能来自仓库内文件与 `make unzip` 之后的下载产物，报告中对无法从文件确证的说法（如 `ESDFFQ` 中 `E` 的含义）明确标注「待确认」。

## 6. 本讲小结

- ICS55 的标准单元区是 H7CH/H7CL/H7CR 三套平行库，分别对应 HVT/LVT/RVT 三种阈值电压；README 的目录注释与三个 CDL 的器件模型名（`hvt`/`lvt`/`svt`）互相印证。
- 阈值决定速度—漏电取舍（漏电随 Vth 指数变化），三库同名换型是多阈值优化的基础；只看单元名尾字符 H/L/R 即可判断阈值家族。
- 单元名是四段编码：功能助记符 + 输入配置与变体字母 + `X` 驱动强度（`P` 代替小数点）+ 库后缀；`FILLCAP`、`TIEHI/TIELO` 无驱动档。
- 全库 748 个单元（H7CH 口径）中，与或复合门占 40.6%，是第一大群体；三库覆盖差异只有 `SDFFRX0P5H7H` 一个单元。
- cell_list 不含 37 个 ANT 天线单元，做全库工具时不能把它当全集。

## 7. 下一步学习建议

名单只是目录——下一讲 **u3-l2「单元 LEF 抽象视图解剖」** 将打开 [ics55_LLSC_H7CH.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef)，看每个单元名对应的 MACRO：SIZE 怎么和 u2-l2 讲过的 core7 site 对齐、引脚矩形怎么分布、OBS 障碍区是什么。建议先预习：在本讲名单里任选 `ADDFX1H7H`，再到 LEF 里找到它的 MACRO 段读一遍，带着「这份抽象会怎样被布线器使用」的问题进入下一讲。之后再按 u3-l3（_ecos 版电源轨道）、u3-l5（verilog 模型）的顺序把每种视图过完。
