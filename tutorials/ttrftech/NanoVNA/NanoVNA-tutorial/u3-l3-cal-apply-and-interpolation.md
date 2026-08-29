# 误差修正应用与校准插值

## 1. 本讲目标

上一讲（u3-l2）我们搞清楚了「误差项是怎么算出来的」：`cal_collect` 采集标准件数据，`cal_done` 解出 Ed/Es/Er/Et/Ex 五个误差项。本讲回答接下来的两个问题：

1. **误差项算出来之后，怎么用？** —— 精读 `apply_error_term_at()`：sweep 过程中每个频点上，如何用五个误差项把「仪器测到的原始值」还原成「DUT 的真实值」，以及为什么 Et 在存储时就预先取了倒数。
2. **扫描范围变了怎么办？** —— 误差项是与校准时的频点表逐点对齐的。一旦用户改了 start/stop，频点表变了，旧的误差项就「错位」了。精读 `cal_interpolate()`：如何从 flash 里的校准槽读出旧误差项，线性插值到新频点表上，并在跨越谐波模式边界（300MHz）时做「取一边」而不是线性过渡的特殊处理。

学完本讲，你应当能：

- 读懂 `apply_error_term_at` 里的复数除法与复数乘法，并解释 Et 取倒数的效率考量。
- 说出 `CALSTAT_APPLY` 与 `CALSTAT_INTERPOLATED` 两个状态位的含义，以及屏幕左上角 `C*` / `c0` 标记的意思。
- 独立用 Python 复现 `cal_interpolate`，并验证谐波边界处的「跳变吸附」行为。

## 2. 前置知识

本讲要用到的前置概念，多数在前面讲义已建立，这里做一次快速回顾和少量补充：

- **误差盒模型（u3-l2）**：一端口测量值与真实反射系数的关系为
  \[ S_{11m} = E_d + \frac{E_r \cdot \Gamma}{1 - E_s \cdot \Gamma} \]
  其中 Ed 直接性、Es 源匹配、Er 反射跟踪。二端口再引入 Et 传输跟踪与 Ex 隔离。反向求解（修正）的公式本讲会反复用到：
  \[ \Gamma = \frac{S_{11m} - E_d}{E_r + E_s\,(S_{11m} - E_d)} \]
- **复数四则运算的展开**：固件里没有复数类型，全部用实部/虚部两个 float 手工展开。两个基本式子要熟记：
  - 乘法：\( (a+bj)(c+dj) = (ac-bd) + (ad+bc)j \)
  - 除法（分子乘分母共轭、分母取模方，可省去开方）：
    \[ \frac{x+yj}{u+vj} = \frac{(xu+yv) + (yu-xv)j}{u^2+v^2} \]
- **`measured` 数组（u2-l1）**：`measured[2][101][2]`，第一维是通道（0=CH0 反射、1=CH1 传输），第二维是频点索引 i，第三维是实部/虚部。sweep 每测完一个频点就把原始 Γ 写进 `measured[ch][i]`。
- **`cal_data` 与频点对齐（u3-l2）**：`cal_data[5][101][2]` 是 5 组误差项 × 频点 × 实虚部，索引 i 与 `frequencies[i]` 一一对应。这是本讲「插值」存在的根本原因。
- **谐波模式（u2-l2）**：超过 `harmonic_freq_threshold`（配置项，典型值 300MHz）后 si5351 改用谐波输出。判断宏 `IS_HARMONIC_MODE(f)` 即 `f > FREQ_HARMONICS`。
- **线性插值**：在两个已知点 \((x_0,y_0)\)、\((x_1,y_1)\) 之间，用
  \[ y = y_0\,(1-k_1) + y_1\,k_1,\qquad k_1 = \frac{x-x_0}{x_1-x_0} \]
  估计中间值。对复数误差项，实部和虚部分别做一次即可。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | `sweep()` 中误差修正的调用点、`apply_error_term_at`、`apply_edelay_at`、`cal_interpolate`、`cmd_cal`、`cmd_edelay`、`IS_HARMONIC_MODE` 宏 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `CALSTAT_*` 状态位、`ETERM_*` 索引、`properties_t` 结构、`cal_data` 等别名宏 |
| [flash.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c) | `caldata_ref()`：只读引用 flash 中的校准槽（含 magic/checksum 校验） |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c) | `draw_cal_status()`：屏幕上 `C`/`c` 校准状态标记的绘制 |

## 4. 核心概念与源码讲解

本讲的三个最小模块在数据流上的位置如下：

```
sweep() 每个频点 i：
  测 CH0 ──► measured[0][i]  （原始 S11m）
  测 CH1 ──► measured[1][i]  （原始 S21m）
      │
      ├─ cal_status 有 CALSTAT_APPLY ──► apply_error_term_at(i)   ← 模块 4.1
      ├─ electrical_delay != 0      ──► apply_edelay_at(i)        ← 模块 4.2
      └─ 下一个频点

（频率改变时，且已开启校准）:
  set_sweep_frequency() / cmd_scan() / cmd_resume()
      └─► cal_interpolate(lastsaveid)                              ← 模块 4.3
```

### 4.1 apply_error_term_at 误差修正

#### 4.1.1 概念说明

`apply_error_term_at(i)` 是「校准的下半场」：把误差盒模型反过来用。校准求误差项是「已知 DUT、求误差盒」；修正是「已知误差盒、求 DUT」。它运行在 sweep 的热路径上——每扫一遍 101 个频点，这个函数就要执行 101 次，而且扫描是循环往复的，所以它的每一行都被写成了尽量便宜的操作（这解释了后面 Et 取倒数的做法）。

要注意它修正的对象是 `measured` 数组本身，即**原地覆盖**：修正后的值直接顶替原始值。原始数据不保留，想看未修正的数据只能关掉 `CALSTAT_APPLY` 重新扫。

另一个容易忽略的细节：误差项并不住在 `current_props`（RAM 里的可编辑配置）里，而是由别名宏指向 `active_props`：

```c
#define cal_data active_props->_cal_data
```

见 [nanovna.h:400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L400)。`active_props` 平时可以指向 flash 中的某个校准槽（recall 之后），这样应用误差修正时直接读 flash、无需先复制到 RAM——这是 u3-l4 要展开的存储设计，本讲先记住「cal_data 可能指向 flash」即可。

#### 4.1.2 核心流程

对频点 i，修正分两段：

**第一段：S11（反射）**

1. 减去直接性：\( S_{11m}' = S_{11m} - E_d \)。
2. 算分母：\( D = E_r + E_s \cdot S_{11m}' \)（复数乘加）。
3. 复数除法 \( \Gamma = S_{11m}' / D \)，用「乘共轭、除模方」展开，写回 `measured[0][i]`。

**第二段：S21（传输）**

1. 减去隔离：\( S_{21m}' = S_{21m} - E_x \)。
2. 源匹配项：\( (1 - E_s \cdot S_{11a}) \)，注意这里用的是**刚修正出来的 S11a**（第一段的结果）——反射与传输修正是串联的。
3. 乘上 Et 槽里的值。注意注释里的警告：**Et 槽里存的不是 Et，而是 1/Et**（原因见下）。

为什么存倒数？对比两条路径上的运算量：

- 若存 Et 本体：每个频点做一次复数除法 \( S_{21a} = S_{21m}'(1-E_s S_{11a}) / E_t \)。
- 若存倒数：校准时一次性除一次（`eterm_calc_et`），之后每个频点只做复数乘法。

STM32F072 是 Cortex-M0，**没有硬件浮点单元**，浮点除法是软件模拟的，比乘法贵得多。把除法从「每频点每扫描」挪到「校准一次」，是用一点存储约定换热路径性能的典型手法。代价是阅读时必须时刻记住这个约定——源码作者也特意写了两遍 `CAUTION: Et is inversed for efficiency` 注释（[main.c:1241](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1241) 和 [main.c:1308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1308)）。

#### 4.1.3 源码精读

先看调用点——在 `sweep()` 主循环里，两个通道都测完后、进入下一个频点前：

```c
    if (cal_status & CALSTAT_APPLY)
      apply_error_term_at(i);

    if (electrical_delay != 0)
      apply_edelay_at(i);
```

见 [main.c:884-888](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L884-L888)。两个 `if` 说明：修正只在校准开启（`CALSTAT_APPLY` 置位）时进行；电延迟是独立开关，与校准互不依赖。

`apply_error_term_at` 本体（S11 部分）：

```c
static void apply_error_term_at(int i)
{
    // S11m' = S11m - Ed
    // S11a = S11m' / (Er + Es S11m')
    float s11mr = measured[0][i][0] - cal_data[ETERM_ED][i][0];
    float s11mi = measured[0][i][1] - cal_data[ETERM_ED][i][1];
    float err = cal_data[ETERM_ER][i][0] + s11mr * cal_data[ETERM_ES][i][0] - s11mi * cal_data[ETERM_ES][i][1];
    float eri = cal_data[ETERM_ER][i][1] + s11mr * cal_data[ETERM_ES][i][1] + s11mi * cal_data[ETERM_ES][i][0];
    float sq = err*err + eri*eri;
    float s11ar = (s11mr * err + s11mi * eri) / sq;
    float s11ai = (s11mi * err - s11mr * eri) / sq;
    measured[0][i][0] = s11ar;
    measured[0][i][1] = s11ai;
```

见 [main.c:1294-1306](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1294-L1306)。逐行对照公式：

- `err/eri` 是分母复数 \( E_r + E_s S_{11m}' \) 的实部/虚部：`s11mr*Es.re - s11mi*Es.im` 正是复数乘法 \( S_{11m}' \cdot E_s \) 的实部 \( (ac - bd) \)，`+ s11mr*Es.im + s11mi*Es.re` 是虚部 \( (ad + bc) \)。
- `sq = err² + eri²` 是分母模方，除以模方 + 分子乘分母共轭（`(s11mr*err + s11mi*eri)` 与 `(s11mi*err - s11mr*eri)`）合起来就是标准复数除法。

S21 部分：

```c
    // CAUTION: Et is inversed for efficiency
    // S21m' = S21m - Ex
    // S21a = S21m' (1-EsS11a)Et
    float s21mr = measured[1][i][0] - cal_data[ETERM_EX][i][0];
    float s21mi = measured[1][i][1] - cal_data[ETERM_EX][i][1];
    float esr = 1 - (cal_data[ETERM_ES][i][0] * s11ar - cal_data[ETERM_ES][i][1] * s11ai);
    float esi = - (cal_data[ETERM_ES][i][1] * s11ar + cal_data[ETERM_ES][i][0] * s11ai);
    float etr = esr * cal_data[ETERM_ET][i][0] - esi * cal_data[ETERM_ET][i][1];
    float eti = esr * cal_data[ETERM_ET][i][1] + esi * cal_data[ETERM_ET][i][0];
    float s21ar = s21mr * etr - s21mi * eti;
    float s21ai = s21mi * etr + s21mr * eti;
    measured[1][i][0] = s21ar;
    measured[1][i][1] = s21ai;
}
```

见 [main.c:1308-1321](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1308-L1321)。注意三点：

1. `esr/esi` 计算 \( 1 - E_s \cdot S_{11a} \)，用的是上面刚算出的 `s11ar/s11ai`——两段修正有数据依赖。
2. `etr/eti` 是 \( (1 - E_s S_{11a}) \cdot E_t^{(存)} \) 的复数乘法，其中 \( E_t^{(存)} = 1/E_t \)。
3. 整段没有任何除法（唯一一次除法在 S11 的 `sq` 上，不可避免），这就是存倒数的收益。

Et 的倒数是在 `cal_done` 阶段由 `eterm_calc_et` 预先算好的：

```c
// CAUTION: Et is inversed for efficiency
static void
eterm_calc_et(void)
{
  int i;
  for (i = 0; i < sweep_points; i++) {
    // Et = 1/(S21mt - Ex)
    float etr = cal_data[CAL_THRU][i][0] - cal_data[CAL_ISOLN][i][0];
    float eti = cal_data[CAL_THRU][i][1] - cal_data[CAL_ISOLN][i][1];
    float sq = etr*etr + eti*eti;
    float invr = etr / sq;
    float invi = -eti / sq;
    cal_data[ETERM_ET][i][0] = invr;
    cal_data[ETERM_ET][i][1] = invi;
  }
```

见 [main.c:1241-1258](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1241-L1258)。\( 1/(x+yj) = (x-yj)/(x^2+y^2) \)，即实部 `etr/sq`、虚部 `-eti/sq`。校准时每个频点除一次，换来修正时永远只乘。

顺带一提，[main.c:1260-1292](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1260-L1292) 有一段被 `#if 0` 注释掉的 `apply_error_term(void)`——一次修正整个数组的旧版本，逻辑与逐点版完全相同。现在改为在 sweep 内逐点调用，是为了让修正后的数据尽早可用于绘制流水线。

#### 4.1.4 代码实践

**实践目标**：在 PC 上验证 `apply_error_term_at` 的 S11 修正公式确实能从带误差的测量值还原出真实 Γ（正向模型 → 反向修正的闭环）。

**操作步骤**（以下为示例代码，非项目原有代码）：

1. 新建 `cal_apply_demo.py`，用 Python 复数实现「正向误差盒 + 固件反向修正」：

```python
#!/usr/bin/env python3
# cal_apply_demo.py —— apply_error_term_at 的 S11 部分闭环验证（示例代码）
import cmath

# 假想的仪器误差（真值，仅用于生成"测量值"）
Ed = complex(0.05, 0.02)   # 直接性
Es = complex(0.10, -0.03)  # 源匹配
Er = complex(0.90, 0.01)   # 反射跟踪

def forward_model(Gamma):
    """u3-l2 的一端口误差盒：S11m = Ed + Er*Gamma/(1 - Es*Gamma)"""
    return Ed + Er * Gamma / (1 - Es * Gamma)

def apply_error_term_s11(S11m, Ed_, Es_, Er_):
    """按 main.c:1296-1297 的注释公式反解：
       S11m' = S11m - Ed ; S11a = S11m' / (Er + Es*S11m')"""
    Sp = S11m - Ed_
    return Sp / (Er_ + Es_ * Sp)

if __name__ == "__main__":
    for g in [complex(0, 0),          # 匹配负载  Γ=0
              complex(1, 0),          # 理想开路  Γ=+1
              complex(-1, 0),         # 理想短路  Γ=-1
              complex(0.3, -0.4)]:    # 某个任意负载
        m = forward_model(g)
        a = apply_error_term_s11(m, Ed, Es, Er)
        print(f"真值 Γ={g:.4f}  测量值 S11m={m:.4f}  修正后={a:.4f}  误差={abs(a-g):.2e}")
```

2. 运行 `python3 cal_apply_demo.py`。

**需要观察的现象**：每行「修正后」与「真值 Γ」几乎完全一致（误差在 1e-16 量级，仅浮点舍入）。

**预期结果**：验证了固件公式 \( \Gamma = S_{11m}'/(E_r + E_s S_{11m}') \) 确实是误差盒 \( S_{11m} = E_d + E_r\Gamma/(1-E_s\Gamma) \) 的精确逆运算。你在源码里看到的十几行实虚部展开，数学上就做这一件事。

（有真机的读者可以加一步：连接天线或负载，串口里分别执行 `cal off` 和 `cal on` 后各 `scan` 一次，对比输出数据的差异——那就是误差修正被开关的效果，对应 [main.c:1493-1498](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1493-L1498) 的两个分支。待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`apply_error_term_at` 中 S21 段用到的 `s11ar/s11ai` 是原始测量值还是修正后的值？这有什么含义？

**答案**：是修正后的值（S11a）。二端口修正公式里源匹配项 \( 1 - E_s S_{11a} \) 依赖的是 DUT 的真实反射系数，因此 S11 必须先修正、S21 后修正，两段有严格的顺序依赖，不能交换或并行。

**练习 2**：如果把 `eterm_calc_et` 改成存 Et 本体（不取倒数），`apply_error_term_at` 的 S21 段要怎么改？性能上差在哪？

**答案**：`etr/eti` 与 `s21ar/s21ai` 的两步复数乘法要合并成一次复数除法：\( S_{21a} = S_{21m}'(1-E_s S_{11a}) / E_t \)，即先乘出分子复数，再按「乘共轭、除模方」除以 Et，多算一次模方和一次浮点除法。Cortex-M0 无 FPU、浮点除法由软件模拟，而这段代码在 101 个频点 × 每次扫描都执行，累积代价可观——这就是「校准除一次、应用只乘」的设计动机。

**练习 3**：阅读 [main.c:884-885](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L884-L885)。为什么修正放在 `(*sample_func)(measured[1][i])` 之后、而不是 sweep 全部结束后统一做？

**答案**：逐点修正让 `measured` 在扫描过程中始终持有「已修正」数据，绘制流水线（plot_into_index / draw_all）和 shell 的 `data` 命令拿到的就是最终值；如果扫完再统一修正，中断退出（`break_on_operation`）时屏幕上就会出现半新半旧的数据。被 `#if 0` 掉的整批版本 `apply_error_term`（[main.c:1260-1262](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1260-L1262)）正是历史遗留的反例。

### 4.2 apply_edelay_at 电延迟

#### 4.2.1 概念说明

电延迟（electrical delay）解决的问题：被测件前面如果接了一段电缆（或夹具），信号来回要多走一段路，相位就多转一个与频率成正比的角。想单独看被测件的相位，就要把这个线性相位斜率「拧」回去。

数学上，一段延时为 \( \tau \) 的传输路径给测量值乘上 \( e^{-j2\pi f\tau} \)（相位滞后）。补偿就是在测量结果上乘 \( e^{+j2\pi f\tau} \)。看固件怎么算这个旋转角：

```c
float w = 2 * VNA_PI * electrical_delay * frequencies[i] * 1E-12;
```

`electrical_delay` 的单位是**皮秒**（`properties_t` 里的注释 `// picoseconds`，见 [nanovna.h:372](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L372)），乘 `1E-12` 才变成秒，于是 \( w = 2\pi f \tau \)。为什么用皮秒这么小的单位？因为电缆延时本身就很小——光速下 100ps 只对应 3cm 的行程，用秒做单位得写一堆零。

一个实用的换算：长度 L（米）、速率因子 VF 的电缆，单程延时 \( \tau = L/(c \cdot VF) \)；反射测量要拧两倍（信号走个来回），传输测量拧一倍。

#### 4.2.2 核心流程

```
对频点 i：
  w   = 2π · f[i] · τ           （τ = electrical_delay × 1e-12 秒）
  s, c = sin(w), cos(w)
  e^{jw} = c + j s              （单位旋转因子）
  CH0: measured[0][i] *= e^{jw} （复数乘法展开）
  CH1: measured[1][i] *= e^{jw}
```

因为 \( e^{jw} \) 的模恒为 1，这一步**只旋转相位、不改变幅度**——所以它对 LogMag 曲线没有任何影响，只影响相位/史密斯圆图/极坐标显示。它是独立于 SOL 校准的后处理步骤，放在 `apply_error_term_at` 之后执行。

#### 4.2.3 源码精读

```c
static void apply_edelay_at(int i)
{
  float w = 2 * VNA_PI * electrical_delay * frequencies[i] * 1E-12;
  float s = sin(w);
  float c = cos(w);
  float real = measured[0][i][0];
  float imag = measured[0][i][1];
  measured[0][i][0] = real * c - imag * s;
  measured[0][i][1] = imag * c + real * s;
  real = measured[1][i][0];
  imag = measured[1][i][1];
  measured[1][i][0] = real * c - imag * s;
  measured[1][i][1] = imag * c + real * s;
}
```

见 [main.c:1323-1336](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1323-L1336)。`real*c - imag*s` 与 `imag*c + real*s` 正是复数乘法 \( (real + j\,imag)(c + j\,s) \) 的展开；CH0/CH1 各做一次，代码结构完全对称。

设置接口与 shell 命令：

```c
void set_electrical_delay(float picoseconds)
{
  if (electrical_delay != picoseconds) {
    electrical_delay = picoseconds;
    force_set_markmap();
  }
  redraw_request |= REDRAW_MARKER;
}
```

见 [main.c:1710-1717](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1710-L1717)。`force_set_markmap()` 强制整屏轨迹重画（相位变了，轨迹必然移动）。shell 命令 `edelay {ps}` 见 [main.c:1724-1733](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1724-L1733)，无参数时打印当前值。

`VNA_PI` 定义在 [nanovna.h:38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L38)；`electrical_delay` 是 `current_props._electrical_delay` 的别名宏（[nanovna.h:401](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L401)），随校准槽一起保存到 flash。

#### 4.2.4 代码实践

**实践目标**：直观感受「电延迟 = 与频率成正比的相位旋转」，并算出常用电缆对应的皮秒数。

**操作步骤**（示例代码）：

```python
#!/usr/bin/env python3
# edelay_demo.py —— 电延迟的相位旋转（示例代码）
import math

def rot(phi_deg, edelay_ps, f_hz):
    """在相位 phi_deg 上叠加 edelay 造成的旋转，返回新相位（度）"""
    w = 2 * math.pi * edelay_ps * f_hz * 1e-12
    return math.degrees(math.radians(phi_deg) + w)   # +w：与固件同号

# 1) 100ps 延迟在不同频点的旋转角
for f in [1e6, 10e6, 100e6, 900e6]:
    w = math.degrees(2 * math.pi * 100 * f * 1e-12)
    print(f"f={f/1e6:>5.0f} MHz 时 100ps 旋转 {w:8.2f} 度")

# 2) 1 米 RG-316 电缆（速率因子 0.69）的单程延时
c = 299_792_458.0
tau_ps = 1.0 / (c * 0.69) * 1e12
print(f"1m RG-316 单程延时 = {tau_ps:.0f} ps（反射测量应设 2 倍 = {2*tau_ps:.0f} ps）")
```

**需要观察的现象**：第 1 段输出里旋转角随频率线性增长——900MHz 时 100ps 已转过 32.4 度，说明电延迟对高频段相位读数影响很大；第 2 段算出的 1 米电缆单程约 4834ps。

**预期结果**：理解为什么 UI 里电延迟以 ps 为单位输入、以及为什么反射测量要设两倍单程延时。若把上述 `rot` 用于真实数据：对一组 |S11| 数据分别用 edelay=0 和 edelay=4829 处理，幅度列完全不变、相位列整体倾斜。待本地验证（可用第 5 节综合实践的数据管线一起做）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_edelay_at` 对 LogMag（对数幅度）轨迹完全没有影响？

**答案**：它乘的因子 \( e^{jw} \) 模长恒为 1，只改变相位、不改变幅度，所以任何只取幅度的显示格式（LogMag、SWR、线性幅度）都不受影响，只有 Phase、Smith、Polar、Real/Imag 等含相位的格式会变。

**练习 2**：`apply_edelay_at` 里对 CH0 和 CH1 用的是**同一个** w。反射和传输的物理路径通常不同长，这样合理吗？

**答案**：从物理上说确实不精确——反射路径是被测端口前的电缆来回两倍，传输路径是两端口各自的电缆之和。固件选择只提供一个全局 edelay 参数（精度与 UI 复杂度的取舍），实践中用户主要用它做「相位斜率归零」这类相对操作（例如把长电缆的相位拉平后再读群时延），而不是严格的去嵌入。这是仪器分级带来的简化。

### 4.3 cal_interpolate 插值

#### 4.3.1 概念说明

回忆 u3-l2 的关键结论：**校准数据与频点表逐频点对齐**。`cal_data[eterm][i]` 的下标 i 对应校准那一刻的 `frequencies[i]`。于是有个现实问题：

> 用户先在 50kHz~900MHz 上做了校准，存进 0 号槽。第二天把扫描范围改成 10MHz~40MHz——现在 `frequencies[]` 是新的 101 个点，而 flash 里 0 号槽的误差项还挂在旧的 101 个频点上，下标对不上了。

`cal_interpolate(s)` 就是解决这个错位的：以 flash 中 s 号槽（`lastsaveid` 记录的当前槽）的旧频点表 `_frequencies[]` 和旧误差项 `_cal_data[]` 为**只读源**，把 5 组误差项逐一映射到当前频点表上，写进可编辑的 `cal_data`。映射规则是经典的「钳制—插值—钳制」三段式：

```
当前频点 f 相对源表 [f_src[0], f_src[m-1]] 的位置：
  f <  f_src[0]          → 直接取源表第 0 点的误差项        （头部钳制）
  f_src[j] <= f < f_src[j+1] → 在 j 与 j+1 之间线性插值      （线性插值）
  f >= f_src[m-1]        → 直接取源表最后一点的误差项        （尾部钳制）
```

还有一个专属于 NanoVNA 的特殊处理：**谐波模式边界的跳变吸附**。误差项不是频率的光滑函数——跨过 `FREQ_HARMONICS`（默认 300MHz）时，仪器切换到谐波工作方式，驱动强度从 2mA 跳到 8mA（[main.c:362-365](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L362-L365)）、codec 增益也按频段跳档（[main.c:347-357](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L347-L357) 的 `adjust_gain` 以 `f / FREQ_HARMONICS` 分档）。边界两侧的误差项属于两套不同的物理工作点，线性混合出来的「中间值」不对应任何真实仪器状态。所以当源表相邻两点分属两侧时，固件放弃线性过渡，直接按 f 自己在哪一侧就整取那一边的值。

最后，插值的结果毕竟不是精确校准，固件会置起 `CALSTAT_INTERPOLATED` 标志提醒用户——屏幕左上角的小写 `c` 就是它（精确校准显示大写 `C`），见 [plot.c:1663-1668](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1663-L1668)：

```c
  if (cal_status & CALSTAT_APPLY) {
    c[0] = cal_status & CALSTAT_INTERPOLATED ? 'c' : 'C';
    c[1] = active_props == &current_props ? '*' : '0' + lastsaveid;
```

`c[1]` 的两个分支：`*` 表示正在编辑 RAM 里的配置（尚未保存），数字表示当前生效的 flash 槽号。

#### 4.3.2 核心流程

`cal_interpolate` 的触发点遍布所有会改频点表的入口（`cal_auto_interpolate` 在本固件中恒为真，见 [main.c:85](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L85)）：

| 触发位置 | 场景 |
| --- | --- |
| [main.c:1074-1076](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1074-L1076) | `set_sweep_frequency()` 尾部——用户改 start/stop/center/span/CW 的必经之路 |
| [main.c:923-925](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L923-L925) | `cmd_scan`——scan 命令临时借频点表前先重算误差项 |
| [main.c:302-305](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L302-L305) | `cmd_resume`——从暂停恢复时重装频点表后 |
| [main.c:1509-1511](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1509-L1511) | `cal in {id}`——手动从指定槽插值 |

算法本体用伪代码表示（s 为槽号）：

```
src = caldata_ref(s)                # 只读指针，指向 flash 中的槽；magic/checksum 不过则返回
if src == NULL: return              # 空槽/损坏槽：静默放弃，保持现状
ensure_edit_config()                # active_props 切到 RAM，cal_status 清零（放弃直接改 flash）

# 第一段：头部钳制
for i in 0..sweep_points-1:
    if frequencies[i] >= src._frequencies[0]: break
    cal_data[*][i] = src._cal_data[*][0]

# 第二段：双指针线性插值（j 只前进不回退）
j = 0
for ; i < sweep_points; i++:
    f = frequencies[i]
    if f == 0: goto finish          # 频点表尾部哨兵（set_frequencies 清零的尾巴）
    for ; j < src._sweep_points-1; j++:
        if src._frequencies[j] <= f < src._frequencies[j+1]:
            k1 = (f - f[j]) / (f[j+1] - f[j])
            if IS_HARMONIC_MODE(f[j]) != IS_HARMONIC_MODE(f[j+1]):
                k1 = 1.0 if IS_HARMONIC_MODE(f) else 0.0     # 谐波边界：吸附到 f 所在的一侧
            k0 = 1 - k1
            cal_data[e][i] = src._cal_data[e][j]*k0 + src._cal_data[e][j+1]*k1   # 5 组误差项、实虚部分别插值
            break
    if j == src._sweep_points-1: break   # f 已越过源表终点

# 第三段：尾部钳制
for ; i < sweep_points; i++:
    cal_data[*][i] = src._cal_data[*][src._sweep_points-1]

finish:
cal_status |= src._cal_status | CALSTAT_APPLY | CALSTAT_INTERPOLATED
```

几个值得咀嚼的细节：

- **双指针单调扫描**：当前表和源表都按频率升序排列，所以内层指针 j 不需要每个 i 都从头找，整体复杂度 \( O(n+m) \) 而非 \( O(n \times m) \)。
- **`f == 0` 哨兵**：u3-l1 讲过 `set_frequencies` 会把频点表尾部清零，这里正是消费这个约定的地方——遇到 0 直接跳到收尾，连尾部钳制都不做（后面的点本来就不参与扫描）。
- **状态位的重建**：`ensure_edit_config()` 会把 `cal_status` 清零（见 [main.c:842-852](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L842-L852)，它把 `active_props` 从 flash 指针切回 RAM 副本，避免写坏 flash），收尾时再从源槽 OR 回 `_cal_status`——**误差项的有效性信息跟着数据走，而不是留在原地**。

#### 4.3.3 源码精读

函数头与只读源的获取：

```c
static void
cal_interpolate(int s)
{
  const properties_t *src = caldata_ref(s);
  int i, j;
  int eterm;
  if (src == NULL)
    return;

  ensure_edit_config();
```

见 [main.c:1394-1403](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1394-L1403)。`caldata_ref` 在 flash.c 中，校验 `magic == CONFIG_MAGIC` 和 checksum 后返回 flash 槽的只读指针（[flash.c:199-212](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L199-L212)）——插值永远**只读源槽、写 RAM**，绝不写 flash。

头部钳制与主插值循环：

```c
  // lower than start freq of src range
  for (i = 0; i < sweep_points; i++) {
    if (frequencies[i] >= src->_frequencies[0])
      break;

    // fill cal_data at head of src range
    for (eterm = 0; eterm < 5; eterm++) {
      cal_data[eterm][i][0] = src->_cal_data[eterm][0][0];
      cal_data[eterm][i][1] = src->_cal_data[eterm][0][1];
    }
  }

  j = 0;
  for (; i < sweep_points; i++) {
    uint32_t f = frequencies[i];
    if (f == 0) goto interpolate_finish;
    for (; j < src->_sweep_points-1; j++) {
      if (src->_frequencies[j] <= f && f < src->_frequencies[j+1]) {
        // found f between freqs at j and j+1
        float k1 = (float)(f - src->_frequencies[j])
                        / (src->_frequencies[j+1] - src->_frequencies[j]);
```

见 [main.c:1405-1425](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1405-L1425)。注意 `k1` 的分子分母都是 `uint32_t` 减法（两个升序表保证非负），转 float 后相除。

谐波边界处理——本讲最值得品味的 4 行：

```c
        // avoid glitch between freqs in different harmonics mode
        if (IS_HARMONIC_MODE(src->_frequencies[j]) != IS_HARMONIC_MODE(src->_frequencies[j+1])) {
          // assume f[j] < f[j+1]
          k1 = IS_HARMONIC_MODE(f) ? 1.0 : 0.0;
        }

        float k0 = 1.0 - k1;
        for (eterm = 0; eterm < 5; eterm++) {
          cal_data[eterm][i][0] = src->_cal_data[eterm][j][0] * k0 + src->_cal_data[eterm][j+1][0] * k1;
          cal_data[eterm][i][1] = src->_cal_data[eterm][j][1] * k0 + src->_cal_data[eterm][j+1][1] * k1;
        }
        break;
      }
    }
    if (j == src->_sweep_points-1)
      break;
```

见 [main.c:1427-1442](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1427-L1442)。注释 `avoid glitch`（避免毛刺）直说了动机：边界两侧若做线性混合，误差项会出现一个不存在于任何工作点的假值，修正出来的数据在边界频点会突跳。处理办法简单粗暴——k1 非零即一：f 在谐波区就整取 j+1（`k1=1`），在基波区就整取 j（`k1=0`）。

其中 `IS_HARMONIC_MODE` 的定义：

```c
#define FREQ_HARMONICS (config.harmonic_freq_threshold)
#define IS_HARMONIC_MODE(f) ((f) > FREQ_HARMONICS)
```

见 [main.c:82-83](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L82-L83)，阈值本身是配置项 `harmonic_freq_threshold`（字段声明在 [nanovna.h:233](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L233)，默认 300MHz）。注意判断是严格大于：恰好等于 300MHz 算基波侧。

尾部钳制与收尾：

```c
  // upper than end freq of src range
  for (; i < sweep_points; i++) {
    // fill cal_data at tail of src
    for (eterm = 0; eterm < 5; eterm++) {
      cal_data[eterm][i][0] = src->_cal_data[eterm][src->_sweep_points-1][0];
      cal_data[eterm][i][1] = src->_cal_data[eterm][src->_sweep_points-1][1];
    }
  }
interpolate_finish:
  cal_status |= src->_cal_status | CALSTAT_APPLY | CALSTAT_INTERPOLATED;
  redraw_request |= REDRAW_CAL_STATUS;
```

见 [main.c:1444-1456](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1444-L1456)。收尾一行做了三件事：继承源槽的状态位（哪些标准件测过、哪些误差项有效）、强制开启修正（插值的目的就是用）、打上 INTERPOLATED 印记。`CALSTAT_*` 各位的定义集中在 [nanovna.h:49-60](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L49-L60)：

| 位 | 名称 | 含义 |
| --- | --- | --- |
| bit0~4 | LOAD/OPEN/SHORT/THRU/ISOLN | 对应标准件数据**已采集**（兼作 cal_data 槽位下标，值 0~4 见 [nanovna.h:43-47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L43-L47)） |
| bit5~7 | ES/ER/ET | 误差项**已求解**（ED 复用 bit0、EX 复用 bit4，见 [nanovna.h:57-58](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L57-L58)） |
| bit8 | APPLY | 修正**正在应用**（sweep 里的开关） |
| bit9 | INTERPOLATED | 当前误差项来自**插值**而非精确校准 |

#### 4.3.4 代码实践

**实践目标**：用 Python 实现 `cal_interpolate` 的等价函数，并构造一个跨越 300MHz 谐波阈值的例子，亲眼验证「边界吸附」与朴素线性插值的差别。

**操作步骤**（示例代码）：

1. 新建 `cal_interp_demo.py`：

```python
#!/usr/bin/env python3
# cal_interp_demo.py —— cal_interpolate 的 Python 等价实现（示例代码）
FREQ_HARMONICS = 300_000_000     # 对应 config.harmonic_freq_threshold 默认值

def is_harmonic_mode(f):
    return f > FREQ_HARMONICS    # 严格大于，与 main.c:83 一致

def cal_interpolate(src_freqs, src_eterms, new_freqs, naive=False):
    """src_freqs:  源表频点（升序）
       src_eterms: [5][m][2] 的误差项（5 组 × m 点 × (re, im)）
       new_freqs:  当前频点表（升序，尾部可能有 0 哨兵）
       naive=True 时关闭谐波边界吸附，用于对照"""
    n, m = len(new_freqs), len(src_freqs)
    out = [[[0.0, 0.0] for _ in range(n)] for _ in range(5)]
    i = 0
    # 第一段：头部钳制
    while i < n and new_freqs[i] < src_freqs[0]:
        for e in range(5):
            out[e][i] = list(src_eterms[e][0])
        i += 1
    # 第二段：双指针线性插值
    j = 0
    while i < n:
        f = new_freqs[i]
        if f == 0:                       # 哨兵：直接收尾
            return out
        while j < m - 1:
            if src_freqs[j] <= f < src_freqs[j + 1]:
                k1 = (f - src_freqs[j]) / (src_freqs[j + 1] - src_freqs[j])
                if (not naive
                        and is_harmonic_mode(src_freqs[j]) != is_harmonic_mode(src_freqs[j + 1])):
                    k1 = 1.0 if is_harmonic_mode(f) else 0.0   # 边界吸附
                k0 = 1.0 - k1
                for e in range(5):
                    out[e][i][0] = src_eterms[e][j][0] * k0 + src_eterms[e][j + 1][0] * k1
                    out[e][i][1] = src_eterms[e][j][1] * k0 + src_eterms[e][j + 1][1] * k1
                break
            j += 1
        if j == m - 1:
            break
        i += 1
    # 第三段：尾部钳制
    while i < n:
        for e in range(5):
            out[e][i] = list(src_eterms[e][m - 1])
        i += 1
    return out

if __name__ == "__main__":
    # 源表：100M~500M、步进 50M（在 300M/350M 之间出现谐波边界）
    src_f = [100_000_000 + 50_000_000 * k for k in range(9)]
    m = len(src_f)
    # 构造 ETERM_ER 实部：基波区随频率缓变 0.90->0.84，谐波区因增益/驱动突变跌到 ~0.45
    src_er = [0.90 - 0.06 * (f / 500e6) if not is_harmonic_mode(f)
              else 0.45 - 0.05 * ((f - 300e6) / 200e6) for f in src_f]
    src_eterms = [[[v, 0.01 * v] for v in src_er]] + \
                 [[[0.0, 0.0]] * m for _ in range(4)]     # 其余 4 组误差项置零即可

    # 新表：290M 与 320M——分别落在边界窗口 (250M,300M) 与 (300M,350M) 的内侧
    new_f = [290_000_000, 320_000_000]
    snap = cal_interpolate(src_f, src_eterms, new_f, naive=False)
    lin  = cal_interpolate(src_f, src_eterms, new_f, naive=True)

    for idx, f in enumerate(new_f):
        w = next(k for k in range(m - 1) if src_f[k] <= f < src_f[k + 1])
        print(f"f = {f/1e6:.0f} MHz  (窗口 {src_f[w]/1e6:.0f}M~{src_f[w+1]/1e6:.0f}M)")
        print(f"  固件逻辑(吸附): ER.re = {snap[2][idx][0]:.4f}")
        print(f"  朴素线性      : ER.re = {lin[2][idx][0]:.4f}")
```

2. 运行 `python3 cal_interp_demo.py`。

**需要观察的现象**：

- f=290MHz 落在 (250M, 300M) 窗口内，两侧都是基波模式 → 两种版本一致，做正常线性插值。
- f=320MHz 落在 (300M, 350M) 窗口内，300M 是基波侧、350M 是谐波侧，**窗口跨界**：
  - 朴素线性版给出 `k1 = 20/50 = 0.4`，结果是 0.864×0.6 + 0.4375×0.4 ≈ 0.693 ——一个两边的仪器状态都不对应的假值；
  - 固件逻辑版因 320M 处于谐波模式取 `k1 = 1.0`，直接整取 350M 点的 0.4375。

**预期结果**：跨界窗口内两个版本的输出明显不同，非跨界窗口完全相同——这就复现了 [main.c:1427-1431](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1427-L1431) 那 4 行代码的全部行为。可以再试试把 `new_f` 改成 `[299_000_000, 301_000_000]`，观察仅隔 2MHz 的两个频点分别吸附到边界两侧、中间没有任何过渡。

#### 4.3.5 小练习与答案

**练习 1**：为什么收尾是 `cal_status |= src->_cal_status | ...`（从源槽 OR 进来），而不是保留当前 `cal_status`？

**答案**：进入函数时 `ensure_edit_config()` 已把 `cal_status` 清零（当前 RAM 状态作废），而「哪些误差项有效」这一事实属于源槽的数据——插值只是把源槽的误差项搬了家，有效性信息必须跟着数据一起搬过来。若保留清零前的本地状态，可能出现「本地标记说 Er 有效、数据却是从没有 SHORT 的槽插来的」这类自相矛盾。

**练习 2**：用户在 50kHz~300MHz 上做了校准并存槽，之后把扫描范围设为 400MHz~900MHz。`cal_interpolate` 会怎么处理？结果可信吗？屏幕上有什么提示？

**答案**：所有新频点都大于源表终点，全部走尾部钳制——统一使用源表最后一点（300MHz 处）的误差项，本质是「常数外推」。400~900MHz 已是谐波模式，误差项与 300MHz 基波点的差异很大，精度基本不可信，只能当粗略参考。提示有两个：`CALSTAT_INTERPOLATED` 置位使屏幕显示小写 `c`（而非 `C`），且 `active_props == &current_props` 使槽号位显示 `*`（编辑态、未保存）。

**练习 3**：`cal_interpolate` 的内层循环 `for (; j < src->_sweep_points-1; j++)` 中，j 在外层循环之间**不**复位。如果当前频点表不是升序（比如乱序），会发生什么？

**答案**：双指针单调扫描依赖「两张表都升序」这一前提。当前表若乱序，某个 i 找到窗口后 j 停在该处，下一个 i 的频率若更小，内层条件 `src->_frequencies[j] <= f` 恒假，j 会一路推进到 `m-1` 然后外层 break，剩余点全部落入尾部钳制——结果错误但不越界、不崩溃。实际上 `set_frequencies`（u3-l1）保证了升序 + 尾部清零两个不变量，这个算法才能成立；这也是「数据生产者与消费者之间的契约」的一个好例子。

## 5. 综合实践

**任务：在 PC 上搭一条「虚拟 NanoVNA 校准管线」，把本讲三个模块串起来。**

到目前为止我们分别验证了正向/反向误差模型、电延迟旋转和插值。综合实践把它们连成固件真实执行的顺序：

1. **造一台「有误差的虚拟仪器」**：取一串频点（建议 101 点、50k~450MHz，故意跨过 300MHz），选一组与频率相关的误差项（Ed、Es、Er、Ex，以及要取倒数的 Et），对每个频点用正向模型把「DUT 真实 Γ」变成「仪器测量值」。谐波区可以把 Er 幅度压低 30%、叠加额外相移，模拟增益切换。
2. **执行固件顺序的逆运算**：对每个频点依次做 `apply_error_term_at`（记得 S21 段用修正后的 S11a，Et 用存倒数的约定）→ `apply_edelay_at`（给 DUT 前面加 5000ps 的「虚拟电缆」再拧回去，验证幅度不变、相位复原）。
3. **制造一次插值**：把第 1 步的误差项当作「源槽」，另取一张频点数不同、范围更窄且跨 300MHz 的新表，调用你在 4.3.4 写的 `cal_interpolate`，用插值出的误差项再做一次第 2 步。
4. **量化对比**：三条曲线画在同一张图上——DUT 真实 Γ、精确校准修正结果、插值校准修正结果。分别统计 |Δ| 在 300MHz 两侧的平均误差，观察插值版在边界附近是否出现台阶（源表点距越稀，台阶越明显；把源表从 101 点抽稀到 26 点再跑一遍，体会「插值精度取决于源表密度」）。

**验收标准**：精确校准版与真值的误差应在 1e-12 量级（纯浮点舍入）；插值版在源表点距内平滑、但在边界窗口呈现「吸附」而非线性过渡；edelay 加入前后幅度轨迹完全重合。

## 6. 本讲小结

- `apply_error_term_at` 在 sweep 热路径上逐频点执行，把误差盒模型反过来用：\( \Gamma = (S_{11m}-E_d)/(E_r + E_s(S_{11m}-E_d)) \)；S21 段依赖刚修正出的 S11a，顺序不可换。
- **Et 槽里存的是 1/Et**：校准时 `eterm_calc_et` 除一次，修正时永远只乘——Cortex-M0 无 FPU，浮点除法昂贵的环境下典型的热路径优化。
- `apply_edelay_at` 给两个通道乘模长为 1 的旋转因子 \( e^{j2\pi f\tau} \)，只拧相位不动幅度；`electrical_delay` 以皮秒为单位持久化在 `properties_t` 中。
- 误差项与频点表逐点对齐是插值存在的原因：`cal_interpolate` 以 flash 槽为只读源（`caldata_ref` 校验 magic/checksum），经「头部钳制 → 双指针线性插值 → 尾部钳制」把 5 组误差项搬到当前频点表，写 RAM 不写 flash。
- 跨 `FREQ_HARMONICS`（默认 300MHz）的源表窗口不做线性混合，而是按 f 所在侧吸附（k1=0 或 1），因为边界两侧是两套不同的物理工作点，混合值是假值。
- 状态位跟着数据走：收尾 `cal_status |= src->_cal_status | CALSTAT_APPLY | CALSTAT_INTERPOLATED`，屏幕上以 `C*`/`c0` 等标记呈现（大写=精确、小写=插值，`*`=RAM 编辑态、数字=flash 槽号）。

## 7. 下一步学习建议

- **u3-l4（flash.c：配置与校准槽的掉电保存）**：本讲多次出现 `caldata_ref`、`active_props`、`lastsaveid`，它们背后的 flash 槽布局、checksum 校验与 `ensure_edit_config` 的「只读引用 vs 可编辑副本」机制正是下一讲的主角。
- **u3-l5（时域变换）**：误差修正后的 `measured` 数据接下来会流入时域变换模块；电延迟与 Kaiser 窗在时域里是近亲，学完 FFT 视角再回看 `apply_edelay_at` 会有新体会。
- **源码延伸阅读**：对照 [main.c:1458-1516](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1458-L1516) 的 `cmd_cal`，把 `cal load/open/short/thru/isoln/done/on/off/reset/data/in` 每个子命令在脑中映射到本讲和上一讲的函数；再翻 [ui.c:460-475](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L460-L475) 的 `menu_cal2_cb`，看看菜单上的 CAL 按钮如何翻转 `CALSTAT_APPLY`——固件、shell、UI 三条路径操作的是同一个状态字。
