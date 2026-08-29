# u3-l2 SOL 校准：一端口/二端口误差模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **Ed（直接性）、Es（源匹配）、Er（反射跟踪）、Et（传输跟踪）、Ex（隔离）** 五个误差项各自的物理来源与含义。
2. 读懂 `cal_collect()` 如何把「接标准件时的一次完整扫频结果」原样快照进 `cal_data` 槽位，以及 CALSTAT_* 状态位在其中扮演的角色。
3. 读懂 `cal_done()` 与 `eterm_calc_es()/eterm_calc_er()/eterm_calc_et()` 中纯手工展开的复数运算，理解 50fF 开路边缘电容模型的推导，以及「只测 OPEN」等降级路径的处理。
4. 在 PC 上用 Python 复现整个一端口 SOL 校准的求解过程，验证「已知误差 → 仿真测量 → 反解误差」闭环成立。

本讲承接着 u2-l5（sweep 线程产出的 `measured[][]` 数据）与 u3-l1（`frequencies[]` 频点表）：校准数据与测量数据是**逐频点对齐**的，这正是上一讲强调「频点表与校准数据对齐」的原因。

## 2. 前置知识

### 2.1 反射系数与 S11 的回顾

u2-l1 讲过：把被测件（DUT）接到 NanoVNA 的 CH0 口，激励信号一部分被反射回来，反射系数

\[
\Gamma = \frac{Z_{DUT} - Z_0}{Z_{DUT} + Z_0}, \quad Z_0 = 50\,\Omega
\]

\(\Gamma\) 是复数，模长反映反射多少、辐角反映反射波的相位。S11 就是端口 1 的反射系数，S21 是端口 1→2 的传输系数。

### 2.2 「误差盒」：测量值为什么不等于真实值

u2-l4 里 `calculate_gamma()` 输出的 `measured` 是**接收机读到的比值**，而不是 DUT 的真实 \(\Gamma\)。激励信号从信号源到 DUT 再回接收机，沿途要经过电桥、电缆、混频器，每一环都不完美：

- 电桥的定向性有限，激励信号会**不经 DUT 直接漏进**反射接收机；
- 激励源内阻不是理想 50Ω，信号会在「源 ↔ DUT」之间**多次往返反射**；
- 整条「源 → DUT → 接收机」路径有随频率变化的**损耗与相移**。

习惯上把这些缺陷集中抽象成一个串在理想仪器与 DUT 之间的「误差盒」（error box）。校准的任务就是：用几个**反射系数已知**的标准件测出误差盒的参数，之后每次测量再用这些参数把误差「除掉」。

### 2.3 SOL 三件标准件

- **S**hort（短路）：\(\Gamma = -1\)（理想情况下幅值 1、相位 180°）。
- **O**pen（开路）：\(\Gamma \approx +1\)。注意真实开路件有边缘电容，相位会随频率略微滞后——本讲 4.3 会看到固件用 50fF 电容建模它。
- **L**oad（负载）：匹配 50Ω，\(\Gamma = 0\)，接上后理论上什么都不反射。

### 2.4 复数四则与共轭（读源码必备）

固件没有用复数类型，全部复数运算都手工展开成实部/虚部的加减乘除。你需要熟悉：

- 乘法：\((a+jb)(c+jd) = (ac-bd) + j(ad+bc)\)
- 除法：分子分母同乘分母共轭，\(\frac{a+jb}{c+jd} = \frac{(a+jb)(c-jd)}{c^2+d^2}\)，分母变成模方 \(c^2+d^2\)，省去开方。
- 模为 1 的复数 \(u\) 满足 \(1/u = \bar{u}\)（共轭即倒数）——4.3 中开路模型正好用到这一性质。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `nanovna.h` | L43-L47 | `CAL_LOAD/OPEN/SHORT/THRU/ISOLN`：五个校准数据槽位下标 |
| `nanovna.h` | L49-L60 | `CALSTAT_*` 状态位（含 `CALSTAT_ED == CALSTAT_LOAD` 等别名） |
| `nanovna.h` | L62-L66 | `ETERM_ED/ES/ER/ET/EX`：五个误差项下标 |
| `nanovna.h` | L363-L385 | `properties_t`，其中 `_cal_data[5][101][2]` 是校准数据本体 |
| `nanovna.h` | L398-L400 | `cal_status`、`cal_data` 别名宏 |
| `main.c` | L842-L852 | `ensure_edit_config()`：编辑前把 `active_props` 切回 SRAM |
| `main.c` | L857-L897 | `sweep()`，其中 L884-L885 是误差修正的调用点 |
| `main.c` | L1135-L1149 | `eterm_set()/eterm_copy()`：误差项填充/拷贝的小工具 |
| `main.c` | L1178-L1211 | `eterm_calc_es()`：源匹配求解（含 50fF 开路模型） |
| `main.c` | L1213-L1239 | `eterm_calc_er()`：反射跟踪求解 |
| `main.c` | L1241-L1258 | `eterm_calc_et()`：传输跟踪求解（存倒数） |
| `main.c` | L1294-L1321 | `apply_error_term_at()`：误差修正应用（注释即前向/反向模型，下一讲主角） |
| `main.c` | L1338-L1357 | `cal_collect()`：标准件数据采集 |
| `main.c` | L1359-L1392 | `cal_done()`：误差项求解的决策树 |
| `main.c` | L1458-L1516 | `cmd_cal()`：`cal` shell 命令 |
| `ui.c` | L438-L458, L843-L871 | 校准菜单与回调（触摸屏入口） |
| `plot.c` | L1654-L1681 | `draw_cal_status()`：屏幕左侧的 C/D/R/S/T/X 状态字 |
| `flash.c` | L171-L197 | `caldata_recall()`：理解 `ensure_edit_config` 的背景 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **一端口误差模型**（Ed/Es/Er 从哪里来、前向与反向公式）；
2. **cal_collect 数据采集**（把标准件测量快照进槽位）；
3. **cal_done 误差项求解**（eterm_calc_es/er/et 的公式实现与降级路径）。

### 4.1 一端口误差模型：Ed、Es、Er 从哪里来

#### 4.1.1 概念说明

把「仪器 + 连接线」的全部缺陷抽象成一个误差盒，一端口（反射）测量的信号流图给出**前向模型**：

\[
S_{11m} = E_d + \frac{E_r \,\Gamma}{1 - E_s\,\Gamma}
\]

其中 \(S_{11m}\) 是接收机读到的测量值，\(\Gamma\) 是 DUT 的真实反射系数。三个误差项：

| 误差项 | 名称 | 物理来源 | 作用 |
| --- | --- | --- | --- |
| \(E_d\) | 直接性 directivity | 电桥泄漏：激励不经 DUT 直接窜入反射接收机 | 加性偏置，与 \(\Gamma\) 无关 |
| \(E_s\) | 源匹配 source match | 源内阻偏离 50Ω，波在源与 DUT 间多次往返 | 分母项，测量值与 \(\Gamma\) 呈非线性关系 |
| \(E_r\) | 反射跟踪 reflection tracking | 「源→DUT→接收机」整条路径的频响 | 乘性缩放 |

传输（S21）测量再补两项：**Et** 传输跟踪（激励口→接收口路径的频响）与 **Ex** 隔离（两口间的直接串扰）。这就是 `nanovna.h` 注释里的五个名字：

[main.c 引用前先看常量定义 nanovna.h:L62-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L62-L66)：ETERM_ED 到 ETERM_EX 五个宏，注释写明 directivity / source match / refrection tracking / transmission tracking / isolation（注意源码把 reflection 拼成了 refrection）。

校准的思路：三个未知数 \(E_d, E_s, E_r\)，用三个**已知 \(\Gamma\) 的标准件**各测一次，得到三个方程，解出未知数。之后每次测量用**反向模型**还原真实值：

\[
\Gamma = \frac{S_{11m}'}{E_r + E_s\,S_{11m}'}, \qquad S_{11m}' = S_{11m} - E_d
\]

#### 4.1.2 核心流程

三个标准件各自代入前向模型：

```
LOAD  (Γ=0)  :  S11ml = Ed                                  ← 直接得到 Ed
SHORT (Γ=-1) :  S11ms = Ed - Er / (1 + Es)
OPEN  (Γ=Γo) :  S11mo = Ed + Er·Γo / (1 - Es·Γo)   （Γo 含边缘电容，见 4.3）

消元（推导见 4.3.1）：
  Es = ( S11mo'/Γo + S11ms' ) / ( S11mo' - S11ms' )    其中 S11m*' = S11m* - Ed
  Er = -(1 + Es)·S11ms'
```

关键直觉：**LOAD 一步就把 Ed 拿到手**（\(\Gamma=0\) 使分式归零）；OPEN 与 SHORT 的读数差异里同时包含 Es 与 Er 的信息，需要解联立方程。

#### 4.1.3 源码精读

前向/反向模型并不是写在注释文档里，而是作为公式注释**内嵌在应用函数**中。[main.c:L1294-L1321](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1294-L1321)（`apply_error_term_at`，每个频点修正一次）：

```c
    // S11m' = S11m - Ed
    // S11a = S11m' / (Er + Es S11m')
    float s11mr = measured[0][i][0] - cal_data[ETERM_ED][i][0];
    float s11mi = measured[0][i][1] - cal_data[ETERM_ED][i][1];
    float err = cal_data[ETERM_ER][i][0] + s11mr * cal_data[ETERM_ES][i][0] - s11mi * cal_data[ETERM_ES][i][1];
    ...
```

这两行注释就是上一节的反向模型；`err/eri` 手工展开复数乘法计算分母 \(E_r + E_s S_{11m}'\)，随后除以模方完成复数除法。同函数后半段处理 S21：

```c
    // CAUTION: Et is inversed for efficiency
    // S21a = S21m' (1-EsS11a)Et
```

注意「Et 存的是倒数」这个伏笔——4.3 的 `eterm_calc_et` 会呼应它。该函数在 `sweep()` 中的调用点在 [main.c:L884-L885](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L884-L885)：只要 `cal_status` 带上 `CALSTAT_APPLY` 位，每个频点测完立即就地修正 `measured`。

数据结构方面，[nanovna.h:L363-L385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L363-L385) 的 `properties_t` 内嵌 `_cal_data[5][POINTS_COUNT][2]`——5 个槽位 × 101 频点 × (实部,虚部)，float 存储。[nanovna.h:L398-L400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L398-L400) 的别名宏让全项目直接写 `cal_data`、`cal_status`。

**一个必须理解的存储设计**：`CAL_LOAD~CAL_ISOLN`（0~4）与 `ETERM_ED~ETERM_EX`（0~4）共用同一组数组下标（对比 [nanovna.h:L43-L47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L43-L47) 与 [nanovna.h:L62-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L62-L66)）。这不是巧合：

| 槽位 | 采集阶段存 | 求解后存 | 说明 |
| --- | --- | --- | --- |
| 0 | LOAD 实测 | **Ed** | Ed 就等于 LOAD 读数，原地不动 |
| 1 | OPEN 实测 | **Es** | 被计算结果覆盖 |
| 2 | SHORT 实测 | **Er** | 被计算结果覆盖 |
| 3 | THRU 实测 | **Et**（倒数） | 被计算结果覆盖 |
| 4 | ISOLN 实测 | **Ex** | Ex 就等于 ISOLN 读数，原地不动 |

因为接 LOAD 时 \(\Gamma=0\)、前向模型退化为 \(S_{11m}=E_d\)，接 ISOLN（断开直通）时 \(S_{21m}=E_x\)，所以 Ed/Ex **不需要计算**，原始测量本身就是误差项——状态位也因此直接别名：`#define CALSTAT_ED CALSTAT_LOAD`、`#define CALSTAT_EX CALSTAT_ISOLN`（[nanovna.h:L49-L60](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L49-L60)）。

#### 4.1.4 代码实践：亲手玩一玩前向模型

**实践目标**：在 PC 上直观感受三个误差项各自如何扭曲测量值。

**操作步骤**：新建 `errmodel_demo.py`（示例代码，非项目文件）：

```python
# errmodel_demo.py —— 一端口前向误差模型实验（示例代码）
def forward(g, Ed, Es, Er):
    return Ed + Er * g / (1 - Es * g)   # S11m = Ed + Er*g/(1-Es*g)

g = 0.2   # 真实 DUT：75Ω 负载接 50Ω 系统，Γ=(75-50)/(75+50)=0.2
print("真实 Γ          =", g)
print("只有 Ed=0.05    :", forward(g, 0.05, 0.0, 1.0))
print("只有 Es=0.1     :", forward(g, 0.0,  0.1, 1.0))
print("只有 Er=0.9     :", forward(g, 0.0,  0.0, 0.9))
print("三项全开         :", forward(g, 0.05, 0.1, 0.9))
```

**需要观察的现象 / 预期结果**（按公式手算，待本地验证）：

- 只有 Ed：0.25（整体抬了 0.05 的「底噪」）。
- 只有 Es：0.2/0.98 ≈ 0.204082（缩放与 \(\Gamma\) 本身有关，是非线性项）。
- 只有 Er：0.18（纯比例缩放，曲线形状不变）。
- 三项全开：0.05 + 0.18/0.98 ≈ 0.233673（相对真实值 0.2 偏了约 17%）。

#### 4.1.5 小练习与答案

**练习 1**：为什么接上匹配 LOAD 后的读数恰好就是 Ed？
答：LOAD 的 \(\Gamma=0\)，代入前向模型 \(S_{11m} = E_d + E_r \cdot 0/(1-0) = E_d\)，分式项消失。

**练习 2**：从 \(S_{11m}' (1-E_s\Gamma) = E_r \Gamma\) 出发，两步推导反向公式。
答：展开得 \(S_{11m}' = \Gamma(E_r + E_s S_{11m}')\)，两边除以括号项即 \(\Gamma = S_{11m}'/(E_r + E_s S_{11m}')\)。这正是 `apply_error_term_at` 前两行注释。

**练习 3**：若仪器只有 Er 一项误差（Ed=Es=0），Smith 圆图上的轨迹形状会变吗？
答：不会。\(S_{11m}=E_r\Gamma\) 只是整体缩放（含旋转），|Γ|<1 的区域仍映射到圆内，只是幅度标尺变了；而 Es 的分母项才会造成轨迹变形（练习 4.1.4 中 0.2 → 0.204082 的比例依赖于 \(\Gamma\) 本身取值）。

### 4.2 cal_collect：把标准件的测量快照进 cal_data 槽位

#### 4.2.1 概念说明

`cal_collect()` 回答的问题是：**「误差项的方程组右边那些 S11m 从哪来？」** 答案很简单——把每种标准件接到仪器上，让 `sweep()` 完完整整跑一遍 101 个频点，然后把 `measured` 数组**原样拷贝**到对应的 `cal_data` 槽位里。没有任何运算，纯粹的「快照」。

#### 4.2.2 核心流程

```
用户操作（两条入口殊途同归）
├── 触摸屏: MENU → CALIBRATE → OPEN/SHORT/LOAD/ISOLN/THRU   (ui.c menu_calop)
└── USB shell: cal open / cal short / cal load / cal thru / cal isoln  (cmd_cal)
        │
        ▼
cal_collect(type)
  1. ensure_edit_config()     ← active_props 若指向 flash 里的存档，切回 SRAM
  2. 按类型置位 CALSTAT_*，选好源通道 src（反射类=0，传输类=1）
     并清除会因此失效的派生位（OPEN→清 ES，SHORT→清 ER，均连带清 APPLY）
  3. sweep(false)             ← break_on_operation=false，一口气跑完不被 UI 打断
  4. memcpy(cal_data[dst], measured[src], sizeof measured[0])
  5. 请求重绘校准状态区
```

#### 4.2.3 源码精读

主体在 [main.c:L1338-L1357](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1338-L1357)：

```c
  switch (type) {
    case CAL_LOAD:  cal_status|= CALSTAT_LOAD;  dst = CAL_LOAD;  src = 0; break;
    case CAL_OPEN:  cal_status|= CALSTAT_OPEN;  dst = CAL_OPEN;  src = 0; cal_status&= ~(CALSTAT_ES|CALSTAT_APPLY); break;
    case CAL_SHORT: cal_status|= CALSTAT_SHORT; dst = CAL_SHORT; src = 0; cal_status&= ~(CALSTAT_ER|CALSTAT_APPLY); break;
    case CAL_THRU:  cal_status|= CALSTAT_THRU;  dst = CAL_THRU;  src = 1; break;
    case CAL_ISOLN: cal_status|= CALSTAT_ISOLN; dst = CAL_ISOLN; src = 1; break;
  ...
  // Run sweep for collect data
  sweep(false);
  // Copy calibration data
  memcpy(cal_data[dst], measured[src], sizeof measured[0]);
```

逐点解读：

- **src 通道选择**：LOAD/OPEN/SHORT 是反射测量，取 `measured[0]`（CH0）；THRU/ISOLN 是传输测量，取 `measured[1]`（CH1）。这与 u2-l1 讲过的双通道采样一一对应。
- **`sweep(false)`**：u2-l5 讲过 `break_on_operation=false` 意味着整次扫频不会被 UI 操作打断，保证采集到的是**完整、原子**的一组数据。
- **状态位副作用**：重新测 OPEN 会让旧的 Es 失效（Es 是由 OPEN 数据算出来的），所以连带清除 `CALSTAT_ES|CALSTAT_APPLY`；SHORT 对 Er 同理。而 LOAD 不清任何位——Ed 的槽位**就是** LOAD 测量本身，重测即自动更新，没有「派生项」需要作废。
- **快照而非指针**：`memcpy` 拷贝整块 `101×2` 个 float，之后 `measured` 继续被正常扫频覆盖，校准数据安然不动。

开头的 `ensure_edit_config()` 在 [main.c:L842-L852](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L842-L852)：若 `active_props` 指向 flash 中的存档槽（`caldata_recall()` 之后如此，见 [flash.c:L171-L197](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L171-L197)，recall 时已把数据复制进 SRAM 的 `current_props`），就先把指针切回 SRAM 再修改——否则会把误差项写到只读的 flash 地址上；同时 `cal_status = 0`，表示「在召回的校准之上继续采集 = 从头开始一次新校准」。

UI 侧入口在 [ui.c:L843-L852](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L843-L852)（`menu_calop` 表：OPEN/SHORT/LOAD/ISOLN/THRU/DONE 六项），回调 [ui.c:L438-L445](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L438-L445) 直接 `cal_collect(data)`；shell 侧入口在 [main.c:L1458-L1516](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1458-L1516)（`cmd_cal`，子命令 `load|open|short|thru|isoln|done|on|off|reset|data|in`）。

采集的进度随时可见：屏幕左边缘会按状态位画出 D/R/S/T/X 与 C 字符，见 [plot.c:L1654-L1681](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1654-L1681)。

#### 4.2.4 代码实践：亲手采一组数据并观察状态位

**实践目标**：走通「采集 → 查看状态 → 查看槽位数据」的完整路径。

**操作步骤（有真机）**：

1. USB 连接 NanoVNA，打开串口终端（波特率任意，CDC 虚拟串口不敏感）。
2. 依次执行：`cal reset` → 接 LOAD 标准件，`cal load` → 接 OPEN，`cal open` → 接 SHORT，`cal short`。
3. 每步之后执行 `cal`（无参数），记录输出的状态字。
4. 执行 `cal data`，记录 5 行输出（分别是槽位 0~4 在**第一个频点**的复数值）。

**需要观察的现象 / 预期结果（待本地验证）**：

- 采集完三件后 `cal` 应输出 `load open short`（位 0、1、2）。
- `cal data` 第一行（LOAD 槽）数值应接近 0（理想 50Ω 负载 |Γ| 很小）；第二行（OPEN 槽）模长接近 1、虚部为负（容性开路，见 4.3）；第三行（SHORT 槽）模长接近 1、相位与 OPEN 大致反号。

**操作步骤（无硬件替代方案）—— 状态位推演**：对照 [main.c:L1344-L1348](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1344-L1348) 与 [main.c:L1366-L1390](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1366-L1390)，在纸上逐位推演 `cal_status`：

1. `cal load`：置位 `CALSTAT_LOAD`(1<<0) → 0x0001。
2. `cal open`：置 `CALSTAT_OPEN`(1<<1) → 0x0003。
3. `cal short`：置 `CALSTAT_SHORT`(1<<2) → 0x0007。
4. `cal done`：`eterm_calc_es()` 清 OPEN 置 ES(1<<5)；`eterm_calc_er(-1)` 清 SHORT 置 ER(1<<6)；无 THRU/ISOLN；最后 `|= CALSTAT_APPLY`(1<<8) → 0x0161。
5. 此时 `cal` 的输出应为 **`load Es Er cal'ed`**；若五件全测再 done，输出为 **`load isoln Es Er Et cal'ed`**（open/short 位被 Es/Er 替换，thru 位被 Et 替换）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 THRU/ISOLN 的 src 是 1 而其他是 0？
答：THRU/ISOLN 属于 S21 传输测量，`sweep()` 中由 CH1（传输通道）采样写入 `measured[1]`；LOAD/OPEN/SHORT 属于 S11 反射测量，写入 `measured[0]`（u2-l1 的双通道路由）。

**练习 2**：`cal_collect(CAL_OPEN)` 为什么要清 `CALSTAT_ES|CALSTAT_APPLY`，而 `CAL_LOAD` 什么都不清？
答：Es 是由 OPEN（和 SHORT）的槽位数据**派生**出来的，OPEN 重测后旧 Es 与新数据不再自洽，必须作废并退出修正状态；而 Ed 与 LOAD 槽位是同一份数据，重测即自动生效，无派生项可作废。

**练习 3**：采集过程中用户按了暂停键，会发生什么？
答：不会中断采集。`cal_collect` 内部调用的是 `sweep(false)`，`break_on_operation=false` 使扫频循环不检查 `operation_requested`（u2-l5），保证快照完整；UI 输入要等快照结束才被处理。

### 4.3 cal_done：从快照解出五个误差项

#### 4.3.1 概念说明

`cal_done()` 是「解方程器」。Ed 与 Ex 前面说过直接复用槽位数据，真正要算的是 Es、Er、Et。核心数学有三块：

**(a) 开路件的 50fF 边缘电容模型**。理想开路 \(\Gamma=+1\)，但真实开路标准件的端口边缘有杂散电容 \(C\)（固件取 50fF），其归一化阻抗 \(z = 1/(j\omega C Z_0)\)，代入 \(\Gamma=(z-1)/(z+1)\) 整理得：

\[
\Gamma_{open} = \frac{1 - j\omega C Z_0}{1 + j\omega C Z_0}
\]

它仍在单位圆上（纯电抗、无损），但辐角 \(-2\arctan(\omega C Z_0)\) 随频率增长——900MHz 时约 -1.6°，2.6GHz 时约 -4.7°。若把它当理想 +1 处理，高频段 Es/Er 会带上系统误差。

**(b) Es 的求解公式**。记 \(S_{11mo}' = S_{11mo}-E_d\)、\(S_{11ms}' = S_{11ms}-E_d\)，由前向模型分别对 OPEN、SHORT 列式并消去 \(E_r\)（把 SHORT 式 \(E_r = -(1+E_s)S_{11ms}'\) 代入 OPEN 式）可得：

\[
E_s = \frac{S_{11mo}'/\Gamma_{open} + S_{11ms}'}{S_{11mo}' - S_{11ms}'}
\]

源码在变量命名上玩了一个「省除法」的小技巧，见下节。

**(c) Er 与 Et**：

\[
E_r = \mathrm{sign}\cdot(1 - \mathrm{sign}\cdot E_s)\cdot S_{11ms}', \qquad
E_t^{stored} = \frac{1}{S_{21mt} - E_x}
\]

sign 取 -1（把 SHORT 槽数据按 \(\Gamma=-1\) 处理）或 +1（降级路径，把拷贝到 SHORT 槽的 OPEN 数据按理想 \(\Gamma=+1\) 处理）。Et 存倒数纯粹是为了应用时把复数除法换成乘法（呼应 `apply_error_term_at` 的 CAUTION 注释）。

#### 4.3.2 核心流程

`cal_done()` 是一棵决策树（[main.c:L1359-L1392](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1359-L1392)）：

```
cal_done()
├── 没测 LOAD ?      → Ed 清零（eterm_set(ED,0,0)，无 Ed 修正）
├── OPEN 和 SHORT 都测了？
│     ├── 是 → eterm_calc_es()        完整求解 Es（含 50fF 模型）
│     │        eterm_calc_er(-1)      按真实 SHORT(Γ=-1) 求 Er
│     ├── 只测 OPEN → eterm_copy(SHORT, OPEN)   把 OPEN 数据当 SHORT 用
│     │              Es 清零，eterm_calc_er(+1)  假定 Γ=+1（不再用电容模型）
│     ├── 只测 SHORT → Es 清零，eterm_calc_er(-1)
│     └── 都没测   → Er=1, Es=0（无修正的恒等变换）
├── 没测 ISOLN ?     → Ex 清零
├── 测了 THRU ?      → eterm_calc_et() : 否则 Et=1（存倒数后仍为 1）
└── cal_status |= CALSTAT_APPLY        从此 sweep 逐点调用 apply_error_term_at
```

降级路径的意义：标准件不齐时仍能给出一个「可用的近似校准」——例如只有 LOAD+OPEN 时，Ed 准确、Er 近似为开路读数（把真实开路当成理想 +1），Es 视为 0。精度受损但仪器可用。

#### 4.3.3 源码精读

**eterm_calc_es()**（[main.c:L1178-L1211](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1178-L1211)）：

```c
    // z=1/(jwc*z0) = 1/(2*pi*f*c*z0)  Note: normalized with Z0
    // s11ao = (z-1)/(z+1) = (1-1/z)/(1+1/z) = (1-jwcz0)/(1+jwcz0)
    // prepare 1/s11ao for effeiciency
    float c = 50e-15;
    float z0 = 50;
    float z = 2 * VNA_PI * frequencies[i] * c * z0;
    float sq = 1 + z*z;
    float s11aor = (1 - z*z) / sq;
    float s11aoi = 2*z / sq;
```

这段计算 \(\Gamma_{open}=\frac{(1-z^2)-j\,2z}{1+z^2}\)（其中 \(z=\omega C Z_0\)），但注意虚部取的是 **+2z**：变量名虽然叫 `s11ao`，存的实际是 \(\overline{\Gamma_{open}} = 1/\Gamma_{open}\)——因为模为 1 的复数共轭即倒数，这样 Es 公式里的除法 \(S_{11mo}'/\Gamma_{open}\) 就变成了乘法。这正是注释 "prepare 1/s11ao for efficiency" 的含义。

随后按公式手工展开复数运算：

```c
    // S11mo’= S11mo - Ed     S11ms’= S11ms - Ed
    float s11or = cal_data[CAL_OPEN][i][0] - cal_data[ETERM_ED][i][0];
    ...
    // Es = (S11mo'/s11ao + S11ms’)/(S11mo' - S11ms’)
    float numr = s11sr + s11or * s11aor - s11oi * s11aoi;   // 分子 = S11ms' + S11mo'·(1/Γo)
    ...
    cal_data[ETERM_ES][i][0] = (numr*denomr + numi*denomi)/sq;
```

末尾两行是经典的「乘分母共轭除以模方」复数除法。**注意槽位复用**：读的是槽 0/1/2（LOAD/OPEN/SHORT 快照），写入槽 1（覆盖 OPEN 快照）——所以 `cal_done` 内 Es 必须先于 Er 计算，且算完后原始 OPEN 数据即被销毁。

**eterm_calc_er(int sign)**（[main.c:L1213-L1239](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1213-L1239)）：

```c
    // Er = sign*(1-sign*Es)S11ms'
    float s11sr = cal_data[CAL_SHORT][i][0] - cal_data[ETERM_ED][i][0];
    ...
    if (sign > 0) { esr = -esr; esi = -esi; }   // 先构造 (1 - sign*Es)
    esr = 1 + esr;
    ...                                          // 复数乘 S11ms'
    if (sign < 0) { err = -err; eri = -eri; }   // 再乘 sign
    cal_data[ETERM_ER][i][0] = err;
```

公式 \(E_r = \mathrm{sign}\,(1-\mathrm{sign}\cdot E_s)\,S_{11ms}'\)：sign=-1 时即 \(E_r=-(1+E_s)S_{11ms}'\)（与 SHORT 式 \(S_{11ms}'=-E_r/(1+E_s)\) 互逆）；sign=+1、Es=0 时退化为 \(E_r=S_{11mo}'\)（把开路当理想 +1）。它读槽 2 写槽 2，循环内先读后写、按点覆盖，安全。

**eterm_calc_et()**（[main.c:L1241-L1258](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1241-L1258)）：

```c
// CAUTION: Et is inversed for efficiency
    // Et = 1/(S21mt - Ex)
    float etr = cal_data[CAL_THRU][i][0] - cal_data[CAL_ISOLN][i][0];
    ...
    cal_data[ETERM_ET][i][0] = invr;   // 存 1/(S21mt-Ex)
```

直通测量减去隔离串扰后取复数倒数存入槽 3（覆盖 THRU 快照）。「存倒数」是嵌入式省周期的典型取舍：应用时（4.1.3 的 `S21a = S21m' (1-EsS11a)Et`）每点省一次复数除法，代价是任何人读这段数据都必须记得它被反转过——所以源码在两处都留下 CAUTION 注释。

小工具 `eterm_set()/eterm_copy()` 在 [main.c:L1135-L1149](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1135-L1149)：前者把某误差项所有频点填成同一常复数（降级路径用），后者整块拷贝一个槽位（把 OPEN 快照复制进 SHORT 槽当替身）。

顺带一提：源码里还有一段被 `#if 0` 禁用的 `adjust_ed()`（[main.c:L1160-L1176](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1160-L1176)），是作者对 LOAD 误差建模的历史实验，读代码时可以跳过，但它侧面说明「标准件本身不完美」一直是这套模型的已知短板。

#### 4.3.4 代码实践：Python 复现一端口 SOL 校准（本讲核心实践）

**实践目标**：构造已知误差的「虚拟仪器」，按固件公式反解误差项，验证闭环成立；再体验「只测 OPEN」降级路径的精度损失。

**操作步骤**：新建 `sol_cal_sim.py`（示例代码，非项目文件）：

```python
#!/usr/bin/env python3
# sol_cal_sim.py —— 在 PC 上复现 NanoVNA 的一端口 SOL 校准
import cmath

Z0, C_OPEN = 50.0, 50e-15            # 对应 main.c eterm_calc_es 中的 c / z0

def open_gamma(f):
    """带 50fF 边缘电容的开路反射系数（对应 main.c:1183-1192 的推导）"""
    z = 2 * cmath.pi * f * C_OPEN * Z0
    return (1 - 1j*z) / (1 + 1j*z)

def forward(g, Ed, Es, Er):
    """一端口前向误差模型"""
    return Ed + Er * g / (1 - Es * g)

def collect(f, truth):
    """模拟 cal_collect：三种标准件各测一次（LOAD:Γ=0, OPEN:Γo(f), SHORT:Γ=-1）"""
    ml = forward(0.0,        *truth)
    mo = forward(open_gamma(f), *truth)
    ms = forward(-1.0,       *truth)
    return ml, mo, ms

def cal_done_full(f, ml, mo, ms):
    """复现 eterm_calc_es + eterm_calc_er(-1) 的完整 SOL 路径"""
    Ed = ml                                   # Γ=0 ⇒ Ed 直接就是 LOAD 读数
    mop, msp = mo - Ed, ms - Ed               # S11mo', S11ms'
    inv_go = open_gamma(f).conjugate()        # 1/Γo：|Γo|=1 ⇒ 倒数=共轭（对应 s11aor/s11aoi）
    Es = (msp + mop * inv_go) / (mop - msp)   # main.c:1200 注释的 Es 公式
    Er = -(1 + Es) * msp                      # eterm_calc_er(-1): Er = sign*(1-sign*Es)*S11ms'
    return Ed, Es, Er

def cal_done_open_only(f, ml, mo):
    """复现只测 OPEN 的降级路径：Es=0，开路被当作理想 Γ=+1（不再用电容模型）"""
    Ed = ml
    Er = 1.0 * (1 - 0.0) * (mo - Ed)          # eterm_calc_er(+1) 且 Es=0 ⇒ Er=S11mo'
    return Ed, 0.0, Er

TRUTH = (0.05 + 0.0j, 0.1 + 0.0j, 0.9 + 0.0j)   # 题设真值 Ed=0.05, Es=0.1, Er=0.9
for f in (50e6, 900e6, 2.6e9):
    ml, mo, ms = collect(f, TRUTH)
    sol = cal_done_full(f, ml, mo, ms)
    deg = cal_done_open_only(f, ml, mo)
    print(f"f = {f/1e6:6.0f} MHz")
    print(f"  全 SOL 解: Ed={sol[0]:.6f}  Es={sol[1]:.6f}  Er={sol[2]:.6f}")
    print(f"  仅 OPEN : Ed={deg[0]:.6f}  Es={deg[1]:.6f}  Er={deg[2]:.6f}")
```

**需要观察的现象 / 预期结果**（按公式手算，待本地验证）：

1. **全 SOL 路径**：三个频率点的输出都应精确回到真值 `Ed=0.050000 Es=0.100000 Er=0.900000`（误差在 1e-12 量级）。因为仿真用的开路模型与求解用的完全一致，闭环理论上无残差——这验证你对固件公式的转译是逐项正确的。
2. **仅 OPEN 路径**：Er 明显偏大且带负虚部，且频率越高越糟（这正是 50fF 电容模型被丢弃的代价）：
   - 50MHz：Er ≈ 0.999998 - j0.001745
   - 900MHz：Er ≈ 0.999443 - j0.031407
   - 2.6GHz：Er ≈ 0.995479 - j0.090533（辐角约 -5.2°）
3. Es 被强制为 0，而真实值是 0.1——降级校准对源匹配完全「视而不见」。

**预期结果的意义**：如果第 1 项恢复不出真值，说明你转译的公式与固件不一致（最常见的错误是把 `1/Γo` 写成 `Γo`，或弄错 Er 的符号）；第 2 项给出一个量化结论——缺 SHORT 件时高频段的幅度误差约 10%、相位误差随频率增长到数度。

#### 4.3.5 小练习与答案

**练习 1**：把 `C_OPEN` 从 50fF 改成 0（理想开路）再跑 `cal_done_full`，恢复结果会变吗？这说明什么？
答：不变，仍精确恢复真值。因为前向仿真与求解用的是**同一个**开路模型，模型本身在闭环中相互抵消。但真机上「真实边缘电容 ≠ 固件假设的 50fF」时，差值会直接转化为 Es/Er 的系统误差——模型的价值取决于它与真实标准件的吻合度，而不是公式形式。

**练习 2**：`eterm_calc_er(sign)` 的 sign 参数为什么全 SOL 时传 -1、只测 OPEN 时传 +1？
答：SHORT 槽里的数据对应的真实 \(\Gamma=-1\)，代入 \(E_r=\mathrm{sign}(1-\mathrm{sign}\,E_s)S_{11ms}'\) 用 sign=-1；降级路径把 OPEN 数据拷进 SHORT 槽、按理想开路 \(\Gamma=+1\) 解释，故 sign=+1（此时 Es 已被清零，公式退化为 \(E_r=S_{11mo}'\)）。

**练习 3**：`eterm_calc_et` 为什么存 \(1/(S_{21mt}-E_x)\) 而不存原值？有什么代价？
答：应用修正时（`apply_error_term_at` 的 S21 分支）只需要乘法，每个频点省一次复数除法——101 点 × 每秒多次扫频，累积收益可观；代价是数据不自描述，任何读取 `cal_data[ETERM_ET]` 的代码都必须知道这一约定，源码用两处 CAUTION 注释来防止误用（`cal data` 命令打印的槽 3 也是倒数，读数时留意）。

## 5. 综合实践

把本讲三个模块串成一条完整链路：**仿真采集 → 求解误差项 → 用误差项修正未知 DUT**。

在 `sol_cal_sim.py` 基础上追加（示例代码）：

```python
def apply_cal(s11m, Ed, Es, Er):
    """复现 apply_error_term_at 的一端口分支: Γ = S11m'/(Er + Es·S11m')"""
    p = s11m - Ed
    return p / (Er + Es * p)

# 1) 用全 SOL 校准修正一个"未知" DUT（75Ω 负载，Γ=0.2；再试一个复数负载 0.3+0.2j）
for dut in (0.2, 0.3 + 0.2j):
    m = forward(dut, *TRUTH)                       # 虚拟仪器测它
    print("DUT", dut, " 全SOL修正后:", apply_cal(m, *sol_terms))
    print("DUT", dut, " 仅OPEN修正后:", apply_cal(m, *deg_terms))
```

**要求与预期（待本地验证）**：

1. 用 `cal_done_full` 的结果修正，恢复值与真实 Γ 的偏差应小于 1e-12——测量、校准、修正三步构成精确闭环。
2. 换用 `cal_done_open_only` 的降级结果修正同一个测量值，偏差应在百分之几量级且随频率增大（与 4.3.4 第 2 项观察一致）。
3. 把降级修正的偏差随频率画成曲线（50M~2.6G 取 10 个点），你得到的就是「缺 SHORT 标准件」的误差代价曲线。

有真机的读者可以做对照实验：在 `cal off` / `cal on`（或仅 LOAD+OPEN 的降级校准）三种状态下测量同一个 75Ω 负载，用 `data 0` 命令读回数据对比 |Γ| 读数差异，与 PC 仿真结论相互印证。

## 6. 本讲小结

- NanoVNA 用经典 **一端口三项误差模型** \(S_{11m}=E_d+\frac{E_r\Gamma}{1-E_s\Gamma}\) 描述仪器缺陷，二端口再补 **Et/Ex**；Ed/Es/Er/Et/Ex 分别对应直接性、源匹配、反射跟踪、传输跟踪、隔离。
- **Ed 与 Ex 零成本获得**：接 LOAD（Γ=0）与 ISOLN 时的读数本身就是这两项，因此 `ETERM_ED` 与 `CAL_LOAD` 共用槽位与状态位，`ETERM_EX` 与 `CAL_ISOLN` 同理；`cal_data[5][101][2]` 的 5 个槽位在「采集」与「求解」两阶段被复用。
- **cal_collect = 快照**：置状态位（连带作废派生位）→ `sweep(false)` 原子扫频 → `memcpy` 拷贝 `measured[src]`；反射类取 CH0、传输类取 CH1。
- **cal_done = 解方程的决策树**：OPEN+SHORT 齐全时用 50fF 开路电容模型精确解 Es、按 Γ=-1 解 Er；缺件时走降级路径（Es=0、开路按理想 +1），精度换可用性。
- 源码两处「嵌入式风味」：把 \(1/\Gamma_{open}\) 直接以共轭形式解析写出以省去除法；**Et 存倒数**让应用时只乘不除。
- 校准数据与 `frequencies[]` 逐频点对齐——这是上一讲频点表设计的伏笔，也是下一讲「校准插值」要解决的问题。

## 7. 下一步学习建议

下一讲 **u3-l3 误差修正应用与校准插值** 将沿着本讲的 `apply_error_term_at()` 继续深入：误差项如何在 `sweep()` 中逐点反演（含 Et 存倒数的乘法化实现）、`apply_edelay_at` 的电延迟相位旋转，以及当扫频范围与校准范围不一致时 `cal_interpolate()` 如何对 5 组误差项做线性插值（包括谐波模式边界的特殊处理）。建议先重读 [main.c:L1294-L1321](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1294-L1321) 与 [main.c:L1394-L1456](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1394-L1456)，带着「Et 是倒数」「槽位复用」两个本讲结论去读会非常顺畅。若对校准数据的掉电保存感兴趣，可提前浏览 u3-l4 将精读的 `flash.c` 中 `caldata_save/caldata_recall`（[flash.c:L171-L197](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L171-L197)），看看 `properties_t._cal_data` 如何整体写入 flash 槽位。
