# 高层模块 fft_32~fft_16k 与参数化复用

## 1. 本讲目标

上一讲（u4-l3）我们读完了参数化黑盒 `butterfly_general.v`：它把「状态机 + S 控制 + RAM 延时 + 下一级启动 + rotator_valid」五段逻辑收进一个 `layer` 参数控制的模块，供高层复用。本讲就来看**复用的成果**——从 `fft_32` 一直到 `fft_16k` 的所有高层模块。

学完本讲，你应当能够：

- 认识到 `fft_32`、`fft_1k`、`fft_16k` 这三个模块在结构上是「几乎逐字相同」的同构体，区别仅集中在三处：`layer` 参数、`PERIOD` 写法、旋转因子 ROM 实例名。
- 看懂 `fft_top.v` 中 14 级流水线的级联方式，并能说清楚「为什么是 14 个模块、其中哪些是同构高层模块」。
- 辨析 `delay.v` 与 `delay_1k_plus.v` 在计数器位宽上的真实差异，并澄清「大点数延时到底用的是哪一个」这个容易踩的坑。

> ⚠️ 一个需要提前澄清的误解：本讲规格里提到「大点数延时为何用 delay_1k_plus」，但**真实源码里大点数层（包括 fft_16k）用的就是普通的 `delay.v`，`delay_1k_plus.v` 当前并没有被任何模块例化**。我们在 4.4 节会用源码证据讲清这一点，而不是照着名字臆测。

## 2. 前置知识

本讲建立在以下已学认知之上（不重复推导，只承接）：

- **SDF（单路延迟反馈）流水线**：每一级把蝶形下支 B 存入 RAM，延时半周期后当上支 C 喂回蝶形；延时深度 \(2^{\text{layer}-1}\) 随层级翻倍（来自 u3-l2）。
- **butterfly_general.v 的封装**：它内部已经集成了 `delay #(.layer(current_layer))`、状态机、`butterfly`，对外暴露 `D_real/D_img`（前向出口）、`next_level_start`（驱动下一级）、`rotator_valid`（放行真实旋转因子）（来自 u4-l3）。
- **旋转因子寻址无法参数化的原因**：`Rotator_address`、ROM 实例、`multiplier` 实例的名字随层变化，所以它们留在各 `fft_*` 包装层里手工接线（来自 u4-l3、u3-l1）。
- **DIF 路线**：先蝶形后乘旋转因子，因此输出是 bit-reverse 倒序（来自 u1-l3、u1-l4）。

一个关键数字先记下：16384 点 FFT 的级数为

\[
\log_2(16384)=\log_2(2^{14})=14
\]

这就是 `fft_top` 里「14 个模块」的算法根源。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来 |
| --- | --- | --- |
| `src/fft_32.v` | 32 点层（layer=5），首个采用 `butterfly_general` 的同构模板 | 作为「原型」对比基准 |
| `src/fft_1k.v` | 1024 点层（layer=10） | 与 fft_32 对比，展示参数化后的「干净写法」 |
| `src/fft_16k.v` | 16384 点层（layer=14），流水线最前级 | 验证最大点数层也只是改参数 |
| `src/fft_top.v` | 顶层纯连线模块 | 展示 14 级级联与同构复用 |
| `src/butterfly_general.v` | 参数化蝶形层（内部含 `delay`） | 确认延时到底用的是哪个模块 |
| `src/delay.v` | 标准延时单元（计数器较宽） | 与 delay_1k_plus 做位宽对比 |
| `src/delay_1k_plus.v` | 延时变体（计数器较窄，**未被例化**） | 澄清命名与实际使用的出入 |

## 4. 核心概念与源码讲解

### 4.1 高层模块的同构性：fft_32 / fft_1k / fft_16k 是「三胞胎」

#### 4.1.1 概念说明

在 u4-l3 里我们看到，`butterfly_general` 把所有「随层变化之外」的逻辑都吃掉了。那么剩下的 `fft_32`、`fft_1k`、`fft_16k` 这些包装层还做什么？

答案是：**它们几乎什么都不「算」，只做三件事的「接线」**——

1. 例化一个 `butterfly_general`（传入本层的 `layer`）；
2. 例化旋转因子通路（`Rotator_address` + 两块 ROM + 一个 select 多路选择）；
3. 例化一个 `multiplier` 把蝶形 D 输出与旋转因子相乘，得到 `data_out`。

这三步对每一层都一样，所以三个文件读起来像「同一个文件改了几个数字」。这种高度同构正是参数化设计的直接收益：**新增一个点数层，不需要重写逻辑，只要复制模板、改三处常量**。

#### 4.1.2 核心流程

每一层（以 `fft_1k` 为代表）的数据通路可以画成下面这条链：

```
data_in_real/img ──► butterfly_general ──► D_real/D_img ──┐
                         │                                 │
                         ├─ rotator_valid ──► Rotator_address ──► w_rotator_addr, w_select
                         │                                          │
                         │                                  ┌───────▼────────┐
                         │                                  │ rotator_*_real  │
                         │                                  │ rotator_*_img   │ ──► twiddle
                         │                                  └─────────────────┘
                         │                                          │
                         │                           (select: 真实因子 / W=1)
                         │                                          │
                         └──────────────────────────────► multiplier ──► data_out_real/img
                              next_level_start ──► start_next（驱动下一级）
```

- `butterfly_general` 完成 SDF 蝶形 + RAM 延时反馈，吐出前向 `D` 与门控 `rotator_valid`、`next_level_start`。
- `Rotator_address` 在 `rotator_valid` 有效时生成 ROM 地址与前/后半段 `select`（前半段读真实因子、后半段补 \(W=1\)，详见 u3-l1）。
- 两块 ROM 分别给出旋转因子的实部 / 虚部（Q1.16 定点，详见 u2-l3）。
- `always` 块根据 `select` 在「真实因子」与「\(W=1\)（实部 `1<<16`、虚部 0）」之间二选一，得到 `r_rotator_real/img`。
- `multiplier` 算 \((D_{re}+jD_{im})(W_{re}+jW_{im})\)，输出截断后的 32 位 `data_out`。

这条链对 `fft_32`/`fft_1k`/`fft_16k` **完全一致**。

#### 4.1.3 源码精读

先看三个模块的**端口声明**——逐字相同（都是 10 个端口：clk/rst/start/over/data_in_real/data_in_img/data_out_real/data_out_img/start_next/end_next）：

- [src/fft_32.v:6-17](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L6-L17)：fft_32 端口。
- [src/fft_1k.v:9-21](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L9-L21)：fft_1k 端口（与上者结构一致）。
- [src/fft_16k.v:9-20](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L9-L20)：fft_16k 端口（同样一致）。

> 对比要点：`fft_32`/`fft_1k`/`fft_16k` 三个模块的对外接口**完全相同**。这意味着 `fft_top` 可以用同一种连线套路把它们首尾相接——这是级联能写得这么整齐的前提。

再看「旋转因子 select 多路选择」的 `always` 块，三份几乎逐字相同：复位与 `select=1` 时输出 \(W=1\)（`1<<16`, 0），否则输出 ROM 读出的真实因子：

- [src/fft_32.v:74-92](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L74-L92)：fft_32 的 select 多路逻辑。
- [src/fft_1k.v:79-97](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L79-L97)：fft_1k 的同一段——**完全相同**。
- [src/fft_16k.v:78-96](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L78-L96)：fft_16k 的同一段——**完全相同**。

这段逻辑等价于：

\[
(W_{re}, W_{im}) =
\begin{cases}
(2^{16},\ 0) & \text{当 } \text{select}=1 \text{ 或 rotator\_valid 无效（即 } W=1 \text{）}\\
(\text{ROM}_{re},\ \text{ROM}_{im}) & \text{否则（真实旋转因子）}
\end{cases}
\]

`multiplier` 的例化也三处一致：数据接 `D_real/D_img`，旋转因子接 `r_rotator_real/img`，复位写成 `~rst`（把顶层高有效 `rst` 翻成乘法器低有效 `rstn`），只取截断输出：

- [src/fft_32.v:97-109](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L97-L109)：fft_32 的 multiplier 例化。
- [src/fft_1k.v:102-114](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L102-L114)：fft_1k 的 multiplier 例化。
- [src/fft_16k.v:101-113](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L101-L113)：fft_16k 的 multiplier 例化。

**小结**：除了「三处参数/名字」之外，三个文件就是同一份模板。下一节我们把这三处差异单独拎出来。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用肉眼确认「同构性」，不靠记忆。

**步骤**：

1. 分别打开 `src/fft_32.v`、`src/fft_1k.v`、`src/fft_16k.v`，按「端口声明 → butterfly_general 例化 → Rotator_address 例化 → ROM 例化 → select always 块 → multiplier 例化 → output assign」的顺序逐段对照。
2. 把每一段在三份文件里**完全相同**的打勾，**有差异**的圈出来。

**需要观察的现象**：

- 端口、select always 块、multiplier 例化、output assign——四段完全一致。
- 圈出来的差异只会落在：`layer`/`current_layer` 的取值、`PERIOD` 的写法、ROM 实例名、实例命名风格。

**预期结果**：差异项不超过 4~5 处，且都属于「常量/名字」层面，没有任何逻辑层面的差别。

#### 4.1.5 小练习与答案

**练习 1**：既然三个模块结构相同，为什么不能用一个 `fft_general #(.layer(L))` 把它们彻底合并成一个模块？

**参考答案**：因为旋转因子 ROM 的**实例名**随层变化（`rotator_32_real`、`rotator_1k_real`、`rotator_16k_real`……），而 Verilog 的模块例化名是「写死」的标识符，无法用参数在 `case` 里动态选择「例化哪一个 IP」。所以「寻址 + ROM + 乘法」这三段因实例名不可参数化，只能留在各包装层里手工接线。这正是 u4-l3 强调的「参数化的边界」。

**练习 2**：`end_next` 这个输出端口在三份文件里都被声明了，但你看各模块内部，它有没有被赋值（接到任何 `assign` 或 `reg`）？

**参考答案**：没有。三个模块都声明了 `output end_next`，但模块体内没有任何对它的驱动，综合时它会是常量 `z`（高阻）或 `0`。这与 u1-l4 指出的「over/end 链基本未贯通」一致——真正在用的跨级握手是 `start_next → start`。

---

### 4.2 参数化只改三处：layer、PERIOD、ROM 实例名

#### 4.2.1 概念说明

如果要把模板复制成一个新点数层（例如 4096 点、layer=12），你需要改的只有三处。理解这三处，就理解了整个高层模块家族的「参数化骨架」。

#### 4.2.2 核心流程

三处差异一览：

| 差异点 | fft_32（原型，写法较「原始」） | fft_1k / fft_16k（写法更「干净」） |
| --- | --- | --- |
| ① 层参数 | 直接写字面量 `butterfly_general #(.layer(5))` | 先定义 `parameter current_layer = 10/14;`，再用 `#(.layer(current_layer))` |
| ② PERIOD | `parameter PERIOD = 32;`（魔数） | `parameter PERIOD = 1<<current_layer;`（由层推导） |
| ③ ROM 实例名 | `rotator_32_real / rotator_32_img` | `rotator_1k_real / rotator_1k_img`、`rotator_16k_real / rotator_16k_img` |

附带的小差异（不影响功能）：`fft_32` 的实例名带后缀（`butterfly_32`、`multiplier16`、`rotator_address_32`、`w_out_real_32`），并保留了 Xilinx 模板自带的英文注释；`fft_1k`/`fft_16k` 把实例名简化为 `butterfly`、`multiplier`、`rotator_address`、`w_out_real`，注释也清成了 `//`。从文件头日期看，`fft_32` 创建于 2023/11/21，`fft_1k`/`fft_16k` 创建于 2023/11/28——晚了一周，正是「模板打磨干净后再批量复制」的痕迹。

#### 4.2.3 源码精读

**差异①+②：layer 与 PERIOD 的写法**

- fft_32 用字面量：[src/fft_32.v:19-20](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L19-L20) 定义 `PERIOD = 32`；[src/fft_32.v:30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L30) 用 `butterfly_general #(.layer(5))`。

- fft_1k 用层推导：[src/fft_1k.v:23-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L23-L25) 定义 `current_layer = 10`、`PERIOD = 1<<current_layer`；[src/fft_1k.v:35](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L35) 用 `butterfly_general #(.layer(current_layer))`。

- fft_16k 同样用层推导，只是层值更大：[src/fft_16k.v:22-24](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L22-L24) 定义 `current_layer = 14`；[src/fft_16k.v:34](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L34) 用 `butterfly_general #(.layer(current_layer))`。

> 注意：`PERIOD` 在三个包装层里**都没有被真正使用**（真正使用 `PERIOD` 的是 `butterfly_general` 内部，它自己重新算了一遍 `PERIOD = 1<<layer`，见 [src/butterfly_general.v:24-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L24-L25)）。同理，包装层里的 `HALT_FOR_NEXT_LAYER` 也是「遗留参数」，本模块没用、真正生效的副本在 `butterfly_general` 内。这是阅读时容易混淆的点：**包装层的 `PERIOD/HALT_FOR_NEXT_LAYER` 是历史遗留，不影响行为**。

**差异③：ROM 实例名**

- [src/fft_32.v:63-73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73)：例化 `rotator_32_real` / `rotator_32_img`。
- [src/fft_1k.v:68-78](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L68-L78)：例化 `rotator_1k_real` / `rotator_1k_img`。
- [src/fft_16k.v:67-77](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L67-L77)：例化 `rotator_16k_real` / `rotator_16k_img`。

每块 ROM 存的是「该层 N/2 个旋转因子」的实部或虚部，深度 \(=N/2=2^{\text{layer}-1}\)：fft_32 存 16 个、fft_1k 存 512 个、fft_16k 存 8192 个。地址由 `Rotator_address #(.layer(...))` 生成（[src/fft_32.v:54](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L54)、[src/fft_1k.v:59](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_1k.v#L59)、[src/fft_16k.v:58](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16k.v#L58)），地址位宽随 `layer` 参数化（[src/Rotator_address.v:45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator_address.v#L45) 取 `r_addra[layer-1:0]`）。

#### 4.2.4 代码实践（源码阅读型 + 规格任务）

**目标**：完成规格里的对比任务——「列出 fft_32.v 与 fft_1k.v 除了 layer 参数和 ROM 实例名之外的不同，并解释 fft_top 里为什么需要 14 个模块」。

**步骤**：

1. 打开 `src/fft_32.v` 与 `src/fft_1k.v`，逐行对照，把所有差异列成清单。
2. 把差异分类为「功能相关（影响 layer/PERIOD/ROM）」与「纯风格（命名、注释）」。

**预期差异清单（参考答案）**：

| 类别 | fft_32 | fft_1k | 是否影响功能 |
| --- | --- | --- | --- |
| 层参数写法 | 字面量 `5` | `current_layer=10` 后引用 | 否（最终都传给 `.layer`） |
| PERIOD 写法 | `PERIOD = 32`（魔数） | `PERIOD = 1<<current_layer` | 否（本层未用） |
| `butterfly_general` 实例名 | `butterfly_32` | `butterfly` | 否 |
| `Rotator_address` 实例名 | `rotator_address_32` | `rotator_address` | 否 |
| `multiplier` 实例名 | `multiplier16` | `multiplier` | 否 |
| 输出 wire 名 | `w_out_real_32` | `w_out_real` | 否 |
| ROM 实例名 | `rotator_32_real/img` | `rotator_1k_real/img` | **是**（必须匹配各自 IP） |
| ROM 注释 | 保留 Xilinx 模板英文注释 | 清成 `//` | 否 |
| 文件创建日期 | 2023/11/21 | 2023/11/28 | — |

> **关于「为什么 fft_top 需要 14 个模块」**：因为目标点数 \(N=16384=2^{14}\)，radix-2 FFT 的级数 \(\log_2 N = 14\)，SDF 流水线把每一级做成一个独立硬件模块，所以一共 14 级。其中 10 个（`fft_32`~`fft_16k`，layer 5~14）是用 `butterfly_general` 的同构高层模块；另外 4 个（`fft_2/4/8/16`，layer 1~4）是早期手写、接口非标准的低层模块（见 4.3）。**并非 14 个都是「同构高层模块」，准确说法是「14 级流水线，其中 10 级同构」**。

#### 4.2.5 小练习与答案

**练习 1**：如果要把本设计改成支持 8192 点（\(2^{13}\)）FFT，需要在 `fft_top` 里做哪些改动？

**参考答案**：8192 点需要 13 级。可以删掉最前面的 `fft_16k`（layer=14），从 `fft_8k`（layer=13）开始级联，一直到 `fft_2`，正好 13 级。或者保留 14 级结构但用 `data_config` 选择有效级数——不过 `data_config` 目前未接线（见 u1-l4、u5-l4），所以现成做法是「删级」。注意删级后外部仍需做 bit-reverse 倒序。

**练习 2**：`fft_1k` 的 `PERIOD = 1<<current_layer` 算出来是多少？它和该层延时 RAM 的深度是什么关系？

**参考答案**：`PERIOD = 1<<10 = 1024`。该层 `delay #(.layer(10))` 的延时深度 `DELAY_TIME = 1<<(layer-1) = 1<<9 = 512`，正好是 `PERIOD/2`——即「半个周期」。这正是 SDF「攒满半周期再放行」的体现。

---

### 4.3 fft_top 的 14 级级联：为什么需要这么多模块

#### 4.3.1 概念说明

`fft_top.v` 本身**不含任何运算逻辑**，它只是一个「纯连线」模块：用一连串 `wire` 把 14 个级从 `fft_16k`（最前）串到 `fft_2`（最后）。因为所有同构高层模块的端口完全相同（4.1 已证），所以这 14 段连线代码读起来高度重复——这正是参数化带来的「级联整齐」。

但要留意：14 级里**只有 10 级是同构高层模块**，最末 4 级（`fft_2/4/8/16`）是早期手写、端口名不标准的模块，连线写法不同。

#### 4.3.2 核心流程

14 级流水线全貌（从输入到输出）：

| 级序 | 模块 | layer | 延时深度 \(2^{L-1}\) | 类型 | 端口风格 |
| --- | --- | --- | --- | --- | --- |
| 1（最前） | fft_16k | 14 | 8192 | 同构高层 | 标准 |
| 2 | fft_8k | 13 | 4096 | 同构高层 | 标准 |
| 3 | fft_4k | 12 | 2048 | 同构高层 | 标准 |
| 4 | fft_2k | 11 | 1024 | 同构高层 | 标准 |
| 5 | fft_1k | 10 | 512 | 同构高层 | 标准 |
| 6 | fft_512 | 9 | 256 | 同构高层 | 标准 |
| 7 | fft_256 | 8 | 128 | 同构高层 | 标准 |
| 8 | fft_128 | 7 | 64 | 同构高层 | 标准 |
| 9 | fft_64 | 6 | 32 | 同构高层 | 标准 |
| 10 | fft_32 | 5 | 16 | 同构高层（首个模板） | 标准 |
| 11 | fft_16 | 4 | 8 | 手写（分水岭） | 非标准（start16/A_real/...） |
| 12 | fft_8 | 3 | 4 | 手写 | 非标准（start8/...） |
| 13 | fft_4 | 2 | 2 | 手写 | 非标准（start4/...） |
| 14（最后） | fft_2 | 1 | 1 | 手写 | 非标准（start2/...） |

- **同构高层模块（10 个）**：`fft_32`~`fft_16k`，layer 5~14，端口是 `start/over/data_in_*/data_out_*/start_next/end_next`，内部都是 `butterfly_general`。
- **手写低层模块（4 个）**：`fft_2/4/8/16`，layer 1~4，端口名各异（如 `fft_16` 用 `start16/end16/A_real/out_real_16/start8`），来自参数化提取之前的早期代码（见 u4-l1、u4-l2）。

级联的两个不变套路：
- **数据流**：上一级 `data_out_real/img` → 下一级 `data_in_real/img`，始终「大点数层 → 小点数层」。
- **启动握手**：上一级 `start_next` → 下一级 `start`。

#### 4.3.3 源码精读

**同构高层模块的级联写法（标准端口，整齐重复）**——以最前级 `fft_16k` 和中间级 `fft_1k` 为例：

- [src/fft_top.v:26-37](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L26-L37)：`fft_16k` 接顶层输入 `data_real/img`、`start/over`，输出 `w_out_real_16k` 与 `w_start_8k`。
- [src/fft_top.v:95-106](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L95-L106)：`fft_1k` 的输入数据来自上一级 `w_out_real_2k`，`start` 来自 `w_start_1k`，输出 `w_out_real_1k` 与 `w_start_512`。

可以看到，这两段代码除 wire 名的后缀（`_16k`/`_8k`/`_1k`/`_512`）外**结构完全相同**——把其中一段复制粘贴、改后缀，就能写出任意一级。

**手写低层模块的级联写法（非标准端口，明显不同）**——以 `fft_16` 为例，它的端口名跟高层完全两样：

- [src/fft_top.v:199-210](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L199-L210)：`fft_16` 用的是 `.start16(...)/.end16(...)/.A_real(...)/.out_real_16(...)/.start8(...)`，而不是 `.start(...)/.data_in_real(...)/...`。

这就是「分水岭」：layer≥5 走参数化标准端口，layer≤4 是手写遗留端口。`fft_top` 在 `fft_16` 这一级做了端口风格的「翻译」。

**最后的输出**：

- [src/fft_top.v:251-265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L251-L265)：末级 `fft_2` 的 `out_real2/out_img2` 直接赋给顶层 `out_real/out_img`，`out_start` 赋给 `out_first`。注意顶层声明了 `out_last`（[src/fft_top.v:19](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L19)）却没有驱动它——再次印证「end 链未贯通」。

#### 4.3.4 代码实践（源码阅读型）

**目标**：在 `fft_top.v` 中数清楚「14 级」，并标记风格切换点。

**步骤**：

1. 在 `fft_top.v` 里依次定位 `fft_16k / fft_8k / ... / fft_2` 共 14 个例化。
2. 找到端口风格从「标准」切换到「非标准」的那一级（即 `fft_16`）。

**需要观察的现象**：

- 第 1~10 级（`fft_16k`~`fft_32`）的例化代码长得几乎一样，只是 wire 后缀递减。
- 第 11 级 `fft_16` 开始，端口名突变（`start16`、`A_real`、`out_real_16`、`start8`）。

**预期结果**：能清晰指出「同构区段 = fft_16k..fft_32 共 10 级」「手写区段 = fft_16..fft_2 共 4 级」。

#### 4.3.5 小练习与答案

**练习 1**：为什么最大的延时层（`fft_16k`，延时 8192）要排在流水线**最前面**，而最小的（`fft_2`，延时 1）排在最后？

**参考答案**：这是 DIF（频率抽取）流水线的自然结果。DIF 先对输入做蝶形、逐级把时域样本配对，第一级需要把「相隔 N/2」的样本配对，因此延时最大（\(N/2=8192\)），排在最前；越往后配对间隔越短，延时越小。到 `fft_2` 只需配对相邻样本，延时 1。这也正是输出呈 bit-reverse 倒序的原因（详见 u1-l3、u1-l4）。

**练习 2**：`fft_top` 里除了第一级 `fft_16k`，后面各级的 `over` 端口都接了什么？这说明了什么？

**参考答案**：除了 `fft_16k` 的 `.over(over)` 接了顶层外部信号，`fft_8k` 起各级的 `.over` 都接 `0`（如 [src/fft_top.v:47](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L47)、[src/fft_top.v:99](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L99)）。说明「over/end 结束链」基本没有贯通，跨级真正依赖的是 `start_next → start` 启动链。

---

### 4.4 delay 与 delay_1k_plus：大点数延时的位宽之争

#### 4.4.1 概念说明

这一节专门澄清一个**名字会骗人**的陷阱。

规格与本讲的标题都提到「大点数延时为何用 delay_1k_plus」。但读源码会发现一个反直觉的事实：

- **所有高层模块（包括 fft_16k 这个最大点数层）的延时，用的都是普通的 `delay.v`**，不是 `delay_1k_plus.v`。
- **`delay_1k_plus.v` 当前没有被任何模块例化**（你可以用 `Grep` 全仓搜 `delay_1k_plus` 验证：只有它自己的定义，没有例化）。

那 `delay_1k_plus` 是什么？它是 `delay` 的一个**变体**，逻辑状态机完全相同，但**计数器位宽更窄**——名字里的「1k_plus」容易让人以为它「支持 1k 及以上」，但位宽事实恰恰相反。

#### 4.4.2 核心流程

先确认「延时到底用谁」：`butterfly_general` 内部的延时例化——

[src/butterfly_general.v:208-219](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L219) 例化的是 `delay #(.layer(current_layer))`，**不是** `delay_1k_plus`。因为所有高层模块的延时都经由 `butterfly_general`，所以它们无一例外都用 `delay.v`。

再对比两个模块的计数器位宽（决定能支持多大延时深度）：

| 信号 | `delay.v` | `delay_1k_plus.v` | 含义 |
| --- | --- | --- | --- |
| 默认 `layer` | 1 | 11 | 默认层级 |
| `r_addra`（写地址） | `[13:0]`（14 位，最大 16383） | `[12:0]`（13 位，最大 8191） | RAM 写地址 |
| `r_addrb`（读地址） | `[13:0]`（14 位） | `[12:0]`（13 位） | RAM 读地址 |
| `r_halt` | `[13:0]` | `[12:0]` | 停顿计数 |
| `r_tail_cnt`（排空计数） | `[13:0]`（14 位） | **`[8:0]`（9 位，最大 511）** | 尾部排空计数 |
| `r_delay_cnt` | `[15:0]` | `[15:0]` | 建立期计数（两者相同） |

对 layer=14（fft_16k），延时深度 `DELAY_TIME = 1<<13 = 8192`，状态机内部 `required_delay_in_state_machine = DELAY_TIME-5 = 8187`（见 [src/delay.v:18](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L18)）。要让 `r_tail_cnt` 数到 8187，至少需要 13 位。`delay.v` 的 14 位 `r_tail_cnt` 够用；而 `delay_1k_plus.v` 的 9 位 `r_tail_cnt` 最大只能到 511，**远不足以支撑大 layer**。

所以结论很明确：**`delay.v` 才是「计数器够宽、能扛大点数」的那个；`delay_1k_plus.v` 计数器更窄，反而是为大点数「不够用」的版本**。它目前没有挂到任何地方，更像是一次未完成的尝试或更早的版本。

#### 4.4.3 源码精读

**证据①：butterfly_general 用的是 delay，不是 delay_1k_plus**

[src/butterfly_general.v:208](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208)：`delay #(.layer(current_layer))`——一字不差就是 `delay`。

**证据②：两份延时代码的状态机逐行相同，只有位宽不同**

对照状态机部分——[src/delay.v:45-86](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L45-L86) 与 [src/delay_1k_plus.v:50-91](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L50-L91)：`IDLE→DELAY→OUT→TAIL→END` 五状态、`r_write_trig = r_wea_1d ^ wea` 边沿检测、`required_delay_in_state_machine = DELAY_TIME-1-3-1`（即 −5 补偿）——完全一致。差别只在前面声明的寄存器位宽。

**证据③：位宽差异**

- [src/delay.v:20-26](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L20-L26)：`r_halt/r_addra/r_addrb` 都是 `[13:0]`，`r_tail_cnt` 也是 `[13:0]`。
- [src/delay_1k_plus.v:24-31](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L24-L31)：`r_halt/r_addra/r_addrb` 是 `[12:0]`，`r_tail_cnt` 更窄到 `[8:0]`。

> 这也修正了一个直觉：不能从模块名 `delay_1k_plus` 推断「它用于 ≥1k 点的大延时」。在硬件工程里，命名常常滞后于实现，**以源码位宽和实际例化关系为准**才是可靠做法。

#### 4.4.4 代码实践（源码阅读型）

**目标**：亲手验证「delay_1k_plus 未被例化」，并算清它在大 layer 下会出什么问题。

**步骤**：

1. 用 `Grep` 在整个仓库搜索字符串 `delay_1k_plus`（搜索结果应只命中 `src/delay_1k_plus.v` 自身的定义，没有任何例化点）。
2. 用 `Grep` 搜索 `delay #` 或 `delay #(`，确认例化点都在 `butterfly_general.v:208`（以及 u4-l2 讲过的 fft_16 等手写层）。
3. 计算：若强行把 `delay_1k_plus` 用在 layer=14，`required_delay_in_state_machine = 8187`，而 `r_tail_cnt` 只有 9 位（最大 511），在 `STATE_TAIL` 里它永远数不到 8187——状态机会卡死在 TAIL。

**需要观察的现象**：

- `delay_1k_plus` 没有任何例化。
- `delay` 是实际生效的延时单元，位宽足以支撑 layer=14。

**预期结果**：能写出一句准确结论——「大点数延时用的是 `delay.v`（计数器 14 位），`delay_1k_plus.v`（`r_tail_cnt` 仅 9 位）当前未被例化，且其位宽不足以支撑大 layer」。

> 说明：第 3 步的「状态机卡死」是基于位宽的静态推断，属于代码阅读结论；若要在仿真里真的看到卡死现象，需要搭建 layer=14 的 testbench，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `delay_1k_plus` 的 `r_tail_cnt` 从 `[8:0]` 扩到 `[13:0]`，它就和 `delay.v` 等价了吗？

**参考答案**：基本等价。两者状态机、`required_delay_in_state_machine`、边沿检测、RAM 例化都相同，差别只剩 `r_addra/r_addrb/r_halt` 的位宽（13 vs 14）。若把这几个也扩到 14 位，两者就逻辑等价。这也说明 `delay_1k_plus` 大概率是 `delay` 的一个「缩窄版」早期分支，后来主分支选了位宽更宽的 `delay`。

**练习 2**：为什么延时单元的写地址 `r_addra` 在 fft_16k（layer=14）需要 14 位？

**参考答案**：fft_16k 的延时深度 `DELAY_TIME = 1<<13 = 8192`，写地址要遍历 `0..8191`，需要 \(\lceil\log_2 8192\rceil = 13\) 位；但状态机里地址计数还涉及 OUT/TAIL 阶段的延续，留 14 位（`delay.v` 的选择）有余量、更稳妥。`delay_1k_plus` 只给 13 位属于「刚刚好卡在上限」，鲁棒性更差。

---

## 5. 综合实践

**任务**：仿照 `fft_1k.v`，手工「装配」一个本仓库**尚不存在**的中间层 `fft_128`（layer=7），并把它的「接线清单」写成一张表（不要求真的创建文件，写成设计说明即可）。

**要求**：

1. 列出 `fft_128` 应声明哪些端口（提示：与 fft_1k 完全相同的 10 个标准端口）。
2. 指出三处必须改动的常量/名字：
   - `current_layer = ?`
   - `PERIOD = ?`
   - ROM 实例名应为 `rotator_128_real` / `rotator_128_img`。
3. 算出该层的延时深度 `DELAY_TIME`，并说明它由 `butterfly_general` 内部的 `delay #(.layer(7))` 提供（**不是** `delay_1k_plus`）。
4. 在 `fft_top.v` 里，`fft_128` 应插在哪两级之间？它的 `start` 应接上一级的哪个信号？

**参考要点**：

1. 端口：`clk, rst, start, over, data_in_real, data_in_img, data_out_real, data_out_img, start_next, end_next`。
2. 三处改动：`current_layer = 7`；`PERIOD = 1<<7 = 128`；ROM 实例名 `rotator_128_real` / `rotator_128_img`（需事先用 .coe 生成对应的 64 个旋转因子 ROM IP）。
3. `DELAY_TIME = 1<<(7-1) = 64`，由 `butterfly_general` 内的 `delay #(.layer(7))` 提供。
4. 在 `fft_top.v` 中，`fft_128` 应位于 `fft_256`（上一级）与 `fft_64`（下一级）之间（参见 [src/fft_top.v:130-159](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L130-L159)），其 `start` 接 `fft_256` 的 `start_next`（即 `w_start_128`），`data_in_real/img` 接 `fft_256` 的 `w_out_real_256` / `w_out_img_256`。

> 说明：本仓库其实已经存在 `fft_128.v`，本练习把它当成「未知层」来反推，目的是训练「拿到一个 layer 值就能装配出一层」的参数化复用能力。真实操作时若要新增一个仓库里没有的点数层（如 fft_2k 已有，但假设要 fft_3 的非 2 的幂则不适用——本设计只支持 2 的幂点数），按同样套路即可。

## 6. 本讲小结

- `fft_32`、`fft_1k`、`fft_16k` 是**结构同构的三胞胎**：端口、select 多路逻辑、multiplier 例化、output 赋值逐字相同，差异仅落在 `layer` 参数、`PERIOD` 写法、ROM 实例名（以及实例命名风格）。
- 包装层里的 `PERIOD` 与 `HALT_FOR_NEXT_LAYER` 是**遗留参数**，本模块未用，真正生效的副本在 `butterfly_general` 内部。
- `fft_top` 是纯连线模块，把 **14 级**（= \(\log_2 16384\)）从 `fft_16k` 串到 `fft_2`；其中 **10 级**（`fft_32`~`fft_16k`，layer 5~14）是同构高层模块，**4 级**（`fft_2/4/8/16`，layer 1~4）是手写低层模块，端口风格在 `fft_16` 处切换。
- 级联的两个套路：数据「上一级 out → 下一级 in」、启动「上一级 `start_next` → 下一级 `start`」；`over/end` 结束链基本未贯通。
- **大点数延时用的是 `delay.v`（计数器 14 位），不是 `delay_1k_plus.v`**；后者计数器更窄（`r_tail_cnt` 仅 9 位）、当前未被任何模块例化，且位宽不足以支撑大 layer——名字会骗人，以源码为准。
- 参数化复用的边界在「旋转因子 ROM 实例名不可参数化」，所以高层模块无法合并成单一通用模块，只能「模板 + 改三处」地复制。

## 7. 下一步学习建议

本讲是「逐级解析」单元（u4）的收尾。到这里你已经看完了从 `fft_2` 到 `fft_16k` 的全部层级，以及它们在 `fft_top` 里的级联。接下来建议进入第 5 单元（验证、仿真与平台移植）：

- **u5-l1（MATLAB 黄金参考）**：用 `matlab/FFT_iterative_DIF.m` 生成各级中间结果 `X_FFT_middle_result`，与本讲高层模块的逐级输出做比对，验证「同构层级」真的算对了。
- **u5-l2（仿真与 testbench）**：读 `tb/fft_general_tb.v`，看 testbench 如何例化 `fft_1k` 这类高层模块、用 `data_gen` 喂激励。
- **u5-l3（平台移植与 IP 依赖）**：本讲提到的 ROM（`rotator_*_real/img`）、延时 RAM（`Delay`）、乘法器（`mult2`）都是厂商 IP，换 FPGA 平台时要重新生成——这一讲会给出完整 checklist。
- **u5-l4（架构反思）**：回顾整条流水线，理解 SDF 取舍与「bit-reverse 倒序尚未实现」「`data_config` 未启用」等已知缺陷。

如果想继续深挖源码，可以对照阅读 `src/butterfly_general.v`（u4-l3）与本讲的三个包装层，体会「黑盒内部」与「外部接线」的分工边界。
