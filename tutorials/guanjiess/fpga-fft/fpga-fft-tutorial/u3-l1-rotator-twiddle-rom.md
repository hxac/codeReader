# 旋转因子 ROM：Rotator16 / RotatorMemory8 / Rotator_address

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「旋转因子」在硬件里到底存在哪里、怎么被读出来；
- 区分项目里两种旋转因子存储方式：小点数层用**硬编码常量**（`RotatorMemory8`），大点数层用**厂商 ROM IP**（`Rotator16`、以及 `fft_32` 之后的参数化方案）；
- 看懂 `Rotator_address` 如何用一个 `layer` 参数生成 ROM 读地址和 `select` 选择信号；
- 理解 `select` 信号的真正作用：让一个周期内**前半段读真实旋转因子、后半段补一个 \(W=1\)**；
- 把 `fft_32` 这一层「地址生成 + ROM + select 复用 + 复数乘法」的拼装方式讲清楚，为后面 `butterfly_general` 的参数化复用打基础。

## 2. 前置知识

本讲承接前两讲的内容，请确认你已经理解下面三点：

1. **定点量化与 \(W=1\) 默认值（来自 u2-l3）。** 旋转因子是 \([-1,1]\) 的小数，硬件里把它放大 \(2^{16}=65536\) 倍当整数存（Q1.16）。其中 \(W_N^0=e^{j0}=1+j0\)，量化后实部是 `1<<16`（即 65536）、虚部是 `0`。这个 `(1<<16, 0)` 在本讲会反复出现，它就是「不旋转」的默认旋转因子。
2. **复数乘法器的接口（来自 u2-l2）。** `multiplier` 的 `.c/.d` 两个 18 位端口接的就是旋转因子的实部/虚部，它把蝶形输出 \((a+jb)\) 乘以旋转因子 \((c+jd)\)。本讲要回答的问题是：这个 \((c+jd)\) 从哪里来、每一拍该取哪一个。
3. **DIF radix-2 的每级只需要 \(N/2\) 个旋转因子（来自 u1-l3）。** 第 \(n\) 级（\(N=2^n\) 点）做蝶形时，下支（差支）要乘的旋转因子是 \(W_N^{k},\ k=0,1,\dots,N/2-1\)，一共 \(N/2=2^{n-1}\) 个。上支（和支）乘的是 \(W=1\)。这就是「一半数据乘真实因子、一半数据乘 1」的算法根源。

另外补充一个硬件基础概念：**ROM（Read-Only Memory，只读存储器）**。ROM 里预先放好一张「地址 → 数值」的表，给一个地址，若干个时钟后吐出对应的数值。本项目用的 ROM 是 FPGA 厂商工具（Xilinx Vivado / Anlogic TD）生成的 IP 核，用 `.coe` 文件做初始化——把旋转因子的量化值按地址顺序写进 `.coe`，综合时固化成电路。

> ⚠️ 重要：仓库里**并没有** `.coe` 文件，`rotator_16_real`、`rotator_32_real` 这类 ROM IP 也没有对应的 `.xci`/`.v` 源码——它们是厂商工具生成的外部依赖。所以本讲只讲源码里**能看到的寻址与选择逻辑**，ROM 内部具体存了哪些数属于「待本地验证」的部分（你需要在 Vivado 工程里打开对应 IP 才能看到 `.coe` 内容）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| [src/RotatorMemory8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v) | 8 点层旋转因子：用 `case` 把 4 个因子**硬编码**成常量 | 最简单的存储方式样例 |
| [src/Rotator16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v) | 16 点层旋转因子：用 **ROM IP** 存 8 个因子，select 复用内置 | 第一个用 ROM 的模块，地址/select 逻辑自包含 |
| [src/Rotator_address.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v) | **参数化**的地址 + select 生成器，供 `fft_32` 及以上所有层复用 | 本讲的核心 |
| [src/fft_32.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v) | 32 点层：把 `butterfly_general` + `Rotator_address` + ROM + select 复用 + `multiplier` 拼起来 | 三者如何协同的完整范例 |
| [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) | 产生 `rotator_valid` 信号（告诉旋转因子模块「现在开始有效输出」） | 只看 `rotator_valid` 的产生部分 |

一条主线：**旋转因子从「写死」走向「ROM」再走向「参数化」**。`RotatorMemory8`（写死）→ `Rotator16`（ROM，但地址/选择逻辑写死在模块里）→ `Rotator_address` + 外部 ROM（地址/选择逻辑参数化，可被任意大点数层复用）。这条主线也是下一单元 `butterfly_general` 把整级「参数化封装」的前奏。

## 4. 核心概念与源码讲解

### 4.1 旋转因子的存储问题：为什么需要 ROM 与寻址

#### 4.1.1 概念说明

回顾 DIF radix-2：第 \(n\) 级（\(N=2^n\)）的每个蝶形下支要乘 \(W_N^{k}\)。这一级一共有 \(N/2=2^{n-1}\) 个**不同的**旋转因子：

\[
W_N^{k}=e^{-j\frac{2\pi}{N}k}=\cos\!\left(\tfrac{2\pi k}{N}\right)-j\sin\!\left(\tfrac{2\pi k}{N}\right),\quad k=0,1,\dots,\tfrac{N}{2}-1
\]

每个因子是一个复数（实部 + 虚部）。所以存储旋转因子本质上是存一张表：

| 地址 \(k\) | 实部（量化） | 虚部（量化） |
| --- | --- | --- |
| 0 | \(\cos 0 = 1.0 \rightarrow\) `1<<16` | \(-\sin 0 = 0\) |
| 1 | \(\cos(2\pi/N)\) | \(-\sin(2\pi/N)\) |
| … | … | … |
| \(N/2-1\) | \(\cos(2\pi(N/2-1)/N)\) | \(-\sin(\cdot)\) |

存储方式有两种极端，本项目两种都用了：

- **硬编码常量**：点数小（如 8 点层只要 4 个因子）时，直接用 `parameter` + `case` 把数值写进代码。优点是直观、不依赖任何 IP；缺点是点数一大（如 16384 点层要 8192 个因子）代码会膨胀到无法维护。
- **ROM IP**：把整张表烧进一块 ROM，运行时按地址读。优点是容量大、代码短；缺点是依赖厂商 IP，移植要重新生成（见 u5-l3）。

无论哪种方式，实部和虚部都**分开存**（两个 ROM，或两组常量），因为它们要分别送给 `multiplier` 的 `.c`（实部）和 `.d`（虚部）端口。

#### 4.1.2 核心流程：一周期内「读因子 / 补 1」的节奏

一级要处理的蝶形数据在一个 `PERIOD`（\(=2^{layer}\)）内轮转。每个 `PERIOD` 里，旋转因子模块要做的事是固定的：

```
一个 PERIOD = 2^layer 拍：
  前半段（第 0 ~ 2^(layer-1)-1 拍）：select = 0  → 依次输出 W_N^0, W_N^1, ..., W_N^(N/2-1)
  后半段（第 2^(layer-1) ~ 2^layer-1 拍）：select = 1 → 输出 W=1（实部 1<<16，虚部 0）
```

这正好对应 DIF 的算法：一半蝶形输出（差支）要乘真实旋转因子，另一半（和支）乘 \(W=1\)。`select` 信号就是用来在「读 ROM」和「强制输出 \(W=1\)」之间二选一的总开关。

> 直觉：旋转因子模块像一个「自动售货机」——前半段依次把货架上 \(N/2\) 种旋转因子递出来，后半段什么也不递（给个「1」表示「不旋转」），节奏由 `select` 把控。

#### 4.1.3 源码精读：`RotatorMemory8` 的硬编码方式

先看最简单的 `RotatorMemory8`。它用 `case` 把 8 点层的 4 个旋转因子 \(W_8^{0\sim3}\) 直接写死。

量化常量声明（实部/虚部都是 18 位 Q1.16）：

[src/RotatorMemory8.v:16-29](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L16-L29) —— 把 `cos45°` 量化成 `46341`（二进制 `0_0_1011_0101_0000_0101`），`1.0` 量化成 `one = 0_1_0000_...`（即 `1<<16`），再组合成 `W0~W3` 四个复数因子。`W0=(1,0)` 就是 \(W_8^0=1\)。

用一个 3 位 `counter` 当地址，`case` 查表输出：

[src/RotatorMemory8.v:31-78](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L31-L78) —— `counter` 在 `rotator_valid` 有效时自增（[L31-L41](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L31-L41)），`case(counter)` 选出对应因子（[L51-L72](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L51-L72)）。注意 `rotator_valid` 无效或 `default` 时都输出 `(1<<16, 0)`，即默认 \(W=1\)。

这里没有显式的 `select` 信号——因为 8 点层只覆盖了「读因子」那半段，\(W=1\) 的处理由更上层的时序和默认值兜底。这是「写死」方式最直白的写法，但只适合小点数。

> 说明：模块里那个 `WAIT_FOR_ROTATOR = 5` 参数（[L19](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L19)）在本文件内并未被引用，属于历史遗留参数，阅读时可以忽略。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解硬编码因子的「地址 → 复数」对应关系。
2. **步骤**：打开 `RotatorMemory8.v`，对照下表把 `counter` 取值与输出的 \((real, img)\) 填全：

   | counter | 对应因子 | real（量化值） | img（量化值） |
   | --- | --- | --- | --- |
   | 000 | \(W_8^0\) | `one`（1<<16） | 0 |
   | 001 | \(W_8^1\) | `cos45_18`（46341） | `m_cos45_18` |
   | 010 | \(W_8^2\) | 0 | -65536 |
   | 011 | \(W_8^3\) | `m_cos45_18` | `m_cos45_18` |

3. **观察现象**：`W2=(0,-65536)` 对应 \(W_8^2=e^{-j\pi/2}=-j\)，量化后实部 0、虚部 \(-1\times65536\)；`W3` 的实部虚部都是负的 cos45°，对应第三象限角。把每个因子除以 65536 还原成小数，验证它们是否落在单位圆上（\(\cos^2+\sin^2=1\)）。
4. **预期结果**：4 个因子正好是单位圆上等间隔的 4 个点（0°、45°、90°、135°），即 \(W_8^0\sim W_8^3\)，数量 \(=8/2=4\)，符合 \(N/2\)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RotatorMemory8` 只硬编码了 4 个因子（W0~W3），而不是 8 个？

> **答**：8 点层是 radix-2 的某一级，\(N=8\)，需要的不同旋转因子是 \(N/2=4\) 个（\(W_8^0\sim W_8^3\)）。另一半蝶形输出乘 \(W=1\)，由默认值 `(1<<16,0)` 提供，不需要单独存。

**练习 2**：如果把点数提到 1024 点层，硬编码方式会遇到什么问题？

> **答**：1024 点层需要 \(N/2=512\) 个不同的复数因子，硬编码意味着写 512 个 `parameter` 和一个 512 分支的 `case`，代码量爆炸、极易出错、且每改一次点数就要重写。所以大点数层必须改用 ROM IP，这正是 `Rotator16` 之后的做法。

---

### 4.2 Rotator16：第一个用 ROM IP 的旋转因子模块

#### 4.2.1 概念说明

16 点层需要 \(N/2=8\) 个旋转因子 \(W_{16}^{0\sim7}\)。`Rotator16` 不再用 `case` 写死，而是例化两个厂商 ROM IP——`rotator_16_real` 存实部、`rotator_16_img` 存虚部——运行时按地址读出。同时它把「读 ROM / 输出 \(W=1\)」的 `select` 复用逻辑**内置**在模块里，是一个自包含的旋转因子模块。

#### 4.2.2 核心流程

用一个 4 位计数器 `r_addra` 同时承担两个职责（关键巧思）：

- **低 3 位**（`r_addra[2:0]`）：ROM 地址，范围 \(0\sim7\)，依次读出 8 个因子；
- **最高位**（`r_addra[3]`）：`select` 来源——前 8 拍为 0、后 8 拍为 1。

```
r_addra 计数：0,1,...,7, 8,9,...,15, 0,1,...
              |_______|  |________|
               bit3=0      bit3=1
               读 ROM      select=1 → 输出 W=1
            （8 个真实因子 W_16^0..7）
```

`select` 要经过两级寄存器（`select_1d`、`select_2d`）打拍，是为了和 ROM 的读出延迟对齐——ROM 给了地址后要过 1～2 拍才吐出数据，`select` 必须等同样的拍数才能正确地「选」到那一拍的数据。

#### 4.2.3 源码精读

模块端口：输入 `rotator_valid`，输出 18 位实部/虚部：

[src/Rotator16.v:2-8](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L2-L8)

计数器 `r_addra`：`rotator_valid` 有效则自增，否则清零：

[src/Rotator16.v:16-26](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L16-L26)

`select` 信号：取 `r_addra[3]`，两级寄存存器打拍对齐 ROM 延迟：

[src/Rotator16.v:28-36](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L28-L36)

输出复用（本模块的核心两行）：`select_2d=1` 时强制输出 \(W=1\)，否则输出 ROM 读出的真实因子：

[src/Rotator16.v:39-40](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L39-L40) —— `rotator_real = select_2d ? 1<<16 : w_rotator_real_tmp;` `rotator_img = select_2d ? 0 : w_rotator_img_tmp;`

两个 ROM IP 的例化：实部和虚部分开存，共用同一个地址 `r_addra`：

[src/Rotator16.v:60-70](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L60-L70) —— `rotator_16_real` / `rotator_16_img` 各自把 `r_addra` 作地址、`clk` 作时钟，`douta` 输出 18 位量化值。

> 注意代码里还保留了一段被注释掉的旧版 ROM 例化（[L43-L58](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L43-L58)），端口名（`doa/addra/ocea/rsta`）和现在启用的版本（`douta/addra/clka`）不同，这是从 Anlogic 版迁移到 Xilinx 版 IP 时留下的痕迹，移植细节在 u5-l3 详讲。

#### 4.2.4 代码实践（源码阅读型 + 波形观察）

1. **目标**：验证「计数器高位作 select、前半段读 ROM」的设计。
2. **步骤**：
   - 打开 `Rotator16.v`，沿 `r_addra` 从 0 开始列出前 16 拍的 `r_addra[3]` 值；
   - 标出哪些拍 `select_2d=0`（读 ROM）、哪些拍 `select_2d=1`（输出 \(W=1\)）；
   - 若有 Vivado 工程，可仿真 `Rotator16`：让 `rotator_valid` 持续为 1，观察 `rotator_real`/`rotator_img` 的波形。
3. **观察现象**：前 8 拍 `rotator_real/rotator_img` 应依次出现 8 个不同的值（\(W_{16}^{0\sim7}\) 的量化值），后 8 拍应恒为 `(1<<16, 0)`，如此循环。
4. **预期结果 / 待本地验证**：波形上前 8 拍实部应在 `1<<16=65536` 附近递减（cos 从 1 降到 0），后 8 拍恒为 65536。由于 ROM IP 与 `.coe` 不在仓库内，具体数值需在本地工程打开 IP 后确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `r_addra[3]` 而不是单独再开一个计数器来生成 `select`？

> **答**：因为 `PERIOD=16`，正好把一个周期对半分。`r_addra` 是 4 位计数器，计满 16，其最高位 `r_addra[3]` 天然在「前 8 拍为 0、后 8 拍为 1」地翻转，正好就是 select 需要的节拍。一个计数器同时提供「ROM 地址（低位）」和「select（高位）」，省资源又自然对齐。

**练习 2**：`select_1d` 和 `select_2d` 两级寄存器能去掉吗？

> **答**：不能随便去掉。ROM 从给地址到出数据有固定的读延迟（同步 ROM 至少 1 拍）。`select` 必须和 ROM 数据在同一拍到达输出复用器（[L39-L40](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L39-L40)），少打一拍就会「选错拍」，把 \(W=1\) 选成真实因子或反之。两级延迟的具体拍数要和所用 ROM IP 的读延迟匹配（这也是 anlogic 与 vivado 版本差异的根源，见 u3-l3）。

---

### 4.3 Rotator_address：参数化地址与 select 生成器

#### 4.3.1 概念说明

`Rotator16` 把「地址生成 + select + ROM + 复用」全塞进一个模块。如果 32、64、…、16384 点层都这么写，就要写十几个几乎一模一样的 `RotatorN` 模块，只有位宽和 ROM 名字不同。`Rotator_address` 的思路是：**把与具体点数无关的「地址 + select 生成」逻辑抽出来参数化**，让任意大点数层复用，ROM 则交给上层模块自己去例化（因为每层 ROM 名字、深度不同）。

它只有一个参数 `layer`（层数，\(N=2^{layer}\)），对外输出两样东西：

- `rotator_addr`：ROM 读地址；
- `select`：选择「读 ROM」还是「输出 \(W=1\)」。

#### 4.3.2 核心流程

关键参数与「计数器两用」思想：

\[
\text{MAX\_ADDR} = 2^{\,layer-1}\quad(\text{本级不同的旋转因子个数 }=N/2)
\]

用一个位宽足够大的计数器 `r_addra`（这里固定 13 位，能支撑到 `layer=14` 即 16384 点层的 8192 个因子）：

```
r_addra 在 rotator_valid 有效时持续 +1
  rotator_addr = r_addra[layer-1 : 0]        // 取低 layer 位当地址
  select_1d    = r_addra[layer-1]            // 最高位作 select 来源
  select       = select_2d（再打一拍对齐 ROM 延迟）
```

对 `layer=5`（32 点层）：`rotator_addr = r_addra[4:0]`（5 位，0~31），`select` 由 `r_addra[4]` 决定——前 16 拍（0~15）`select=0` 读 ROM，后 16 拍（16~31）`select=1` 输出 \(W=1\)。与 `Rotator16` 的逻辑完全同构，只是位宽随 `layer` 变化。

#### 4.3.3 源码精读

模块声明与参数：`layer` 默认 5，`MAX_ADDR = 1<<(layer-1)`：

[src/Rotator_address.v:7-15](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L7-L15)

计数器 `r_addra`：`rotator_valid` 有效时自增，否则清零：

[src/Rotator_address.v:22-32](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L22-L32)

`select` 生成：取 `r_addra[layer-1]`，两级寄存存器打拍：

[src/Rotator_address.v:34-42](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L34-L42)

最终输出：地址取低 `layer` 位，`select` 用打两拍后的版本：

[src/Rotator_address.v:44-45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L44-L45) —— `rotator_addr = r_addra[layer-1:0];` `select = select_2d;`

对比 `Rotator16`：两者计数器和 select 逻辑**逐行对应**，区别仅在于 `Rotator_address` 把固定位宽（4 位、`r_addra[3]`）换成了参数化（13 位、`r_addra[layer-1]`），并且**不再内置 ROM 和复用 mux**——这两件事下放给了上层 `fft_32`。

> 设计意图：`Rotator_address` 只管「数地址、发 select」，是纯数字逻辑、与厂商无关；ROM 和 mux 留在 `fft_32` 里。这样参数化的「脑子」可复用，依赖厂商的「存储」按层定制。

#### 4.3.4 代码实践（本讲主实践，源码阅读型）

对照 `fft_32.v` 里 `Rotator_address` 的例化（`layer=5`），手工推导计数器与 `select`：

1. **目标**：写出 `rotator_valid` 有效时地址计数器的走向，以及 `select` 在哪半段为 1。
2. **操作步骤**：
   - 确认 `layer=5` 时 `MAX_ADDR = 1<<4 = 16`，`rotator_addr = r_addra[4:0]`，`select` 取自 `r_addra[4]`。
   - 假设 `rotator_valid` 持续为 1，从 `r_addra=0` 起，逐拍列出 `r_addra[4:0]` 和 `r_addra[4]`：

     | 拍 | r_addra[4:0] | r_addra[4] | select（打两拍后） | ROM 读到的因子 | 实际输出 |
     | --- | --- | --- | --- | --- | --- |
     | 0 | 00000 (0) | 0 | （延迟填充） | \(W_{32}^{0}\) | 真实因子 |
     | 1 | 00001 (1) | 0 | … | \(W_{32}^{1}\) | 真实因子 |
     | … | … | 0 | 0 | … | 真实因子 |
     | 15 | 01111 (15) | 0 | 0 | \(W_{32}^{15}\) | 真实因子 |
     | 16 | 10000 (16) | 1 | 0→1 | （被忽略） | \(W=1\) |
     | … | … | 1 | 1 | （被忽略） | \(W=1\) |
     | 31 | 11111 (31) | 1 | 1 | （被忽略） | \(W=1\) |
     | 32 | 00000 (0) | 0 | 1→0 | \(W_{32}^{0}\) | 真实因子 |

   - 注意：`rotator_addr` 取的是 `r_addra[4:0]`，会一直计到 31 再回绕；但只有 `select=0` 的前半段（地址 0~15）才会真正用到 ROM 读出的值，后半段（16~31）`select=1` 直接输出 \(W=1\)，ROM 读出的值被丢弃。
3. **观察现象**：一个完整 `PERIOD=32` 拍里，前 16 拍轮转输出 16 个真实旋转因子 \(W_{32}^{0\sim15}\)，后 16 拍恒输出 \(W=1\)。
4. **预期结果**：32 点层共需 \(N/2=16\) 个不同因子，正好在前半段全部输出；后半段的 \(W=1\) 对应另一半（和支）蝶形输出。地址「从 0 计到 15」就是读真实因子的那 16 拍。
5. **待本地验证**：ROM `rotator_32_real/img` 的具体深度与 `.coe` 内容不在仓库内，上表「ROM 读到的因子」一列的具体量化数值需在本地 Vivado 工程打开 IP 后核对。

#### 4.3.5 小练习与答案

**练习 1**：`Rotator_address` 的 `r_addra` 为什么固定声明成 13 位？

> **答**：因为本项目的最大点数层是 16384 点（`layer=14`），需要 \(N/2=2^{13}=8192\) 个旋转因子，地址至少要 13 位才能覆盖。固定 13 位是为了让同一个模块从 `layer=5` 一直复用到 `layer=14`，不用每层改位宽。

**练习 2**：`Rotator_address` 输出的 `rotator_addr` 是 `r_addra[layer-1:0]`，包含了最高位 `r_addra[layer-1]`。但这个最高位同时又被用作 `select`。会不会冲突？

> **答**：不会。虽然地址线里带着最高位，但本级 ROM 真正有效的地址范围是 \(0\sim 2^{layer-1}-1\)（即 `MAX_ADDR` 个），只需低 `layer-1` 位。最高位进入 ROM 地址后，要么被 ROM 的实际位宽截掉（ROM 深度只有 \(2^{layer-1}\)），要么读到的值在 `select=1` 时被 mux 丢弃。所以最高位「当地址是冗余的、当 select 才是它真正的用途」。

---

### 4.4 fft_32 如何把三者拼起来：地址生成 + 外部 ROM + select 复用

#### 4.4.1 概念说明

`fft_32` 是第一个用上 `butterfly_general` + `Rotator_address` 组合的层。它的旋转因子部分由三块拼成：

1. **`butterfly_general`** 产生 `rotator_valid`（「数据来了，旋转因子开始有效输出」）；
2. **`Rotator_address`** 产生 `rotator_addr`（地址）和 `select`（选择信号）；
3. **两个 ROM IP**（`rotator_32_real/img`）按地址读出因子；
4. **一个 select 复用 mux**（写在 `fft_32` 内部的 `always` 块里）决定最终送 `multiplier` 的是 ROM 值还是 \(W=1\)。

也就是说，`Rotator16` 自包含的那套逻辑，在 `fft_32` 里被**拆成三份**分布在不同位置。这是为了参数化复用付出的代价（结构稍碎），换来的好处是 32~16384 点层都用同一套写法。

#### 4.4.2 核心流程

```
butterfly_general ──rotator_valid──┐
                                   ├─→ Rotator_address ──rotator_addr──→ ROM ──┐
                                   └──────────────── select ──────────────┐    │
                                                                          ▼    ▼
                                                                    select mux（fft_32 内）
                                                                    r_rotator_real/img
                                                                          │
                                                                          ▼
                                                                     multiplier (.c/.d)
```

`select` 在 mux 里二选一：`select=0` 用 ROM 读出的 `w_rotator_real_tmp/img_tmp`；`select=1` 或 `rotator_valid=0` 时输出默认 \(W=1\)（`1<<16, 0`）。

#### 4.4.3 源码精读

`Rotator_address` 的例化（`layer=5`）：

[src/fft_32.v:54-61](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L54-L61) —— 把 `w_rotator_valid` 接到 `rotator_valid`，输出 `w_rotator_addr` 和 `w_select`。

两个 ROM IP 的例化（注意地址接的是 `Rotator_address` 输出的 `w_rotator_addr`）：

[src/fft_32.v:63-73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73)

select 复用 mux（这就是 `Rotator16` 内置那两行的「外置版」）：

[src/fft_32.v:74-92](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L74-L92) —— `w_select=1` 或 `w_rotator_valid=0` 时输出 `(1<<16, 0)`；否则把 ROM 的 `w_rotator_real_tmp/img_tmp` 寄存到 `r_rotator_real/img`。

最终把 `r_rotator_real/img` 喂给 `multiplier` 的 `.c/.d`：

[src/fft_32.v:97-109](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L97-L109) —— 旋转因子在这里和蝶形输出 `w_D_real/img` 做复数乘法。

`rotator_valid` 本身来自 `butterfly_general`：它在本级状态机进入 `PROCESSING`、且等待计数 `r_count_rotator` 达到 `WAIT_FOR_ROTATOR-1` 后置 1，表示「旋转因子可以开始有效输出了」：

[src/butterfly_general.v:148-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L148-L180) —— `WAIT_FOR_ROTATOR = PERIOD-2`（[L148](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L148)），计数到这个值后 `r_rotator_valid` 置 1（[L169-L180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L169-L180)）。这个「等几拍再让旋转因子有效」的时序对齐细节是下一讲 u3-l3 的主题。

#### 4.4.4 代码实践（跟踪调用链）

1. **目标**：把 `fft_32` 里旋转因子从「产生」到「被乘」的完整数据通路走一遍。
2. **步骤**：
   - 从 [fft_32.v:30-44](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L30-L44) 的 `butterfly_general` 例化找到 `w_rotator_valid` 的来源；
   - 跟到 [fft_32.v:54-61](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L54-L61) 的 `Rotator_address`，确认它吃 `w_rotator_valid`、吐 `w_rotator_addr` 和 `w_select`；
   - 跟到 [fft_32.v:63-73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73) 的 ROM，确认它吃 `w_rotator_addr`、吐 `w_rotator_real_tmp/img_tmp`；
   - 跟到 [fft_32.v:74-92](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L74-L92) 的 mux，确认 `w_select` 决定输出 ROM 值还是 \(W=1\)；
   - 最后到 [fft_32.v:97-109](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L97-L109) 的 `multiplier`，确认 `r_rotator_real/img` 接到 `.c/.d`。
3. **观察现象**：整条链路里，`w_rotator_valid` 是「总开关」，`w_select` 是「读 ROM / 补 1」的分相开关，二者都由 `butterfly_general` 的状态机驱动。
4. **预期结果**：你能画出上面 4.4.2 的框图并标注每个信号的来源模块。

#### 4.4.5 小练习与答案

**练习 1**：对比 `fft_16` 和 `fft_32` 的旋转因子部分，最大的结构差异是什么？

> **答**：`fft_16` 直接例化自包含的 `Rotator16`（地址、select、ROM、mux 全在一个模块里，见 [fft_16.v:230-236](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L230-L236)）；`fft_32` 把这些职责拆成 `Rotator_address` + 外部 ROM + 自己写的 mux 三部分。前者简洁但每层要单独写，后者稍碎但能被所有大点数层复用。

**练习 2**：`fft_32` 的 mux 里，`w_rotator_valid=0` 时为什么也要强制输出 \(W=1\)？

> **答**：`rotator_valid=0` 表示本级还没到有效输出阶段（或已结束）。此时蝶形仍在输出数据，乘法器若乘上垃圾旋转因子会产生错误结果；强制 \(W=1\)（乘 1）相当于「不旋转」，让数据安全直通，等 `rotator_valid` 拉高后再开始真正的旋转。这是一个稳妥的默认值设计。

---

## 5. 综合实践

**任务：给一个「假想的 64 点层」设计旋转因子寻址方案，并与代码对照。**

假设你要新增一个 `fft_64`（`layer=6`，\(N=64\)），复用现有的 `Rotator_address`。请完成：

1. **计算参数**：
   - `PERIOD` = ?（答：\(2^6=64\)）
   - `MAX_ADDR` = ?（答：\(2^{6-1}=32\)，即需要 32 个不同旋转因子）
   - `rotator_addr` 取 `r_addra[?:?]` 的哪几位？（答：`r_addra[5:0]`）
   - `select` 取自哪一位？（答：`r_addra[5]`）
2. **推导节奏**：在一个 `PERIOD=64` 拍内，前 32 拍 `select=0` 读出 \(W_{64}^{0\sim31}\)，后 32 拍 `select=1` 输出 \(W=1\)。
3. **对照源码**：打开 `fft_32.v`，把 `layer=5` 改成 `layer=6`、把 ROM 实例名从 `rotator_32_real/img` 改成 `rotator_64_real/img`（**仅在脑中或草稿上修改，不要真的改源码**），其余结构应完全一致。这正是 u4-l4 要讲的「高层模块同构、只差 `layer` 和 ROM 名」。
4. **反思**：写一段话说明——为什么 `Rotator_address` 的设计让你「几乎不用改代码」就能从 32 点扩到 64 点？参数化（`layer`）和职责分离（地址逻辑 vs ROM 存储）各自起了什么作用？

> 预期收获：你会真切体会到「把不变的部分（地址/select 节奏）参数化、把变化的部分（ROM 深度与名字）留给上层」是这套设计能从 32 点一路复用到 16384 点的关键。这也是下一讲 u3-l3（时序对齐）和下一单元 u4（逐级解析、`butterfly_general`）要展开的主线。

## 6. 本讲小结

- 旋转因子在硬件里是**一张「地址 → 复数量化值」的表**，实部和虚部分开存储；每级需要 \(N/2=2^{layer-1}\) 个不同因子。
- 项目有两种存储方式：小点数层 `RotatorMemory8` 用 `case` **硬编码常量**；大点数层 `Rotator16` 起改用**厂商 ROM IP**（`.coe` 初始化，注意 `.coe` 不在仓库内）。
- 核心巧思是「**计数器两用**」：一个 `r_addra` 的低位当 ROM 地址、最高位当 `select` 来源，天然把一个 `PERIOD` 对半切成「读真实因子 / 补 \(W=1\)」两段。
- `select` 信号是总开关：`select=0` 读 ROM、`select=1` 强制输出 \(W=1\)（`1<<16, 0`）；`select` 要经两级寄存器打拍以对齐 ROM 读延迟。
- `Rotator_address` 把地址/select 逻辑**参数化**抽出（`layer` 参数、13 位计数器），供 `fft_32` 及以上所有层复用；ROM 实例和 select mux 则留在各层模块里（如 `fft_32`）。
- `fft_32` 的旋转因子通路 = `butterfly_general`(产生 `rotator_valid`) → `Rotator_address`(地址+select) → ROM IP → select mux → `multiplier`，是后续所有高层模块的同构模板。

## 7. 下一步学习建议

- **紧接本讲（u3-l2）**：去看 `delay.v`——旋转因子解决了「乘什么」，延时单元解决「上一级数据攒够 N/2 个再放行」的存储反馈，二者共同构成 SDF 流水线的一级。
- **再下一讲（u3-l3）**：本讲反复提到的「`select` 打两拍对齐 ROM 延迟」「`rotator_valid` 要等 `WAIT_FOR_ROTATOR` 拍才有效」「`HALT_FOR_NEXT_LAYER` 的 -2(anlogic)/-3(vivado) 差异」都属于**时序对齐**，u3-l3 会把整条流水线最难的跨级握手时序讲透。
- **下一单元（u4）**：`butterfly_general.v` 把本讲的 `Rotator_address` + ROM + mux + 蝶形 + 延时整体封装成一个参数化模块，理解了本讲你就能很快看懂 u4-l3、u4-l4 的高层复用。
- **延伸阅读**：若手边有 Vivado 工程，打开任意一个 `rotator_*_real` IP，查看它的 `.coe` 文件，把里面的量化值除以 65536 还原，验证它们是否就是 \(W_N^{k}\) 在单位圆上的取值——这是把本讲「待本地验证」部分补全的最佳途径。
