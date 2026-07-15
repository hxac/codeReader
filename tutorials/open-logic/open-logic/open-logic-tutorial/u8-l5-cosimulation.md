# 协同仿真：olo_fix_cosim 与 sim_stimuli/checker

## 1. 本讲目标

本讲是 `fix` 区域（定点运算）验证体系的收尾课。学完后你应当能够：

- 说清「Python 位真模型 + HDL 仿真」这种协同仿真（co-simulation）的整体流程，以及它为什么是验证定点电路的正确方式。
- 用 Python 工具 `olo_fix_cosim` 把一组定点样本写成 `.fix` 文件，并解释文件里每一行的含义。
- 读懂 `olo_fix_sim_stimuli`（读文件、施加激励）与 `olo_fix_sim_checker`（读文件、比对输出）两个仿真专用实体的源码，理解它们如何复用 AXI-S 握手与随机 stall。
- 把这三件套串成一条完整链路：Python 算期望 → 文件交换 → HDL 驱动 DUT → 自动比对，并用 VUnit 的 `pre_config` 钩子在每次仿真前自动生成文件。

本讲承接 u8-l3（定点运算的三段分解）与 u8-l4（Python 代码生成），把「Python 单一真相源」的思想从「生成常量包」推进到「生成激励与期望」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，为什么定点电路特别需要「位真」验证。** 浮点运算的精度几乎是「无限的」，用 Python 算一遍再和 HDL 比对，差异通常只来自舍入误差。定点运算不是这样：一个 `(1,8,4)` 格式只有 13 位、分辨率 Δ=2⁻⁴=0.0625，任何超出该网格的值都会被**截断或饱和**，且截断/饱和的方式（`Trunc_s`、`NonSymPos_s`、`Sat_s` …）会逐位改变结果。所以「正确」不是一个浮点数，而是一个**确定的比特模式**。要验证 HDL 实现，就必须用一个与 HDL 完全同构的「位真模型」算出同样的比特模式来比对——这就是协同仿真的根本动机。

**第二，文件是最简单的跨语言桥梁。** Python（算期望）和 VHDL（跑仿真）跑在不同的进程里，没有共享内存。最稳的交换方式就是写一个纯文本文件：Python 写、VHDL 读。本讲的 `.fix` 文件就是这座桥。

**第三，VUnit 的 `pre_config` 钩子。** VUnit 允许在「每个测试用例仿真开始之前」运行一段 Python 回调，并把本次运行的输出目录传给它。Open Logic 用这个钩子在仿真前**刚刚生成** `.fix` 文件，TB 再从同一目录读回——既保证文件总是和当前 generic 匹配，又不污染仓库。

关键术语：**位真（bit-true）**指逐比特一致；**DUT**（Design Under Test，被测设计）；**激励（stimuli）**指喂给 DUT 的输入；**期望（expected/reference）**指用来比对输出的标准答案；**stall** 指握手时故意拉低 Valid/Ready 制造反压。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/fix/python/olo_fix/olo_fix_cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py) | Python 端：把定点样本数组写成 `.fix` 文件 |
| [src/fix/vhdl/olo_fix_sim_stimuli.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd) | HDL 端：读 `.fix` 文件，按 AXI-S 把激励喂给 DUT |
| [src/fix/vhdl/olo_fix_sim_checker.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd) | HDL 端：读 `.fix` 文件，逐拍比对 DUT 输出 |
| [src/fix/vhdl/olo_fix_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd) | 提供文件读写辅助函数（表头校验、读样本、由字符串推位宽） |
| [test/fix/olo_fix_mult/cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py) | `olo_fix_mult` 的协仿真脚本：算 A、B 与期望 Result |
| [test/fix/olo_fix_mult/olo_fix_mult_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd) | `olo_fix_mult` 的测试台，示范三件套如何围绕 DUT 接线 |
| [sim/test_configs/olo_fix.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py) | 把每个实体的 `cosim` 注册为 VUnit `pre_config` |
| [sim/test_configs/utils.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py) | `named_config` 工具：把 generic 绑定进 `pre_config` |
| [doc/fix/olo_fix_cosim.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_cosim.md) | 官方说明（注意：其中的示例 API 已过时，以源码为准，见 4.1.3） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**①cosim 文件生成 → ②sim_stimuli 施加激励 → ③sim_checker 比对 → ④位真协仿真流程**。前三块是零件，第四块把它们装成一台机器。

### 4.1 cosim 文件生成

#### 4.1.1 概念说明

`olo_fix_cosim` 是一个纯 Python 类，职责唯一：把一串定点样本写成一个 `.fix` 文本文件。它本身不做任何运算——样本是调用方（位真模型）算好传进来的，它只负责「按格式落盘」。

为什么需要一个专门的类，而不是直接 `np.savetxt`？因为定点数的落盘有两个特殊要求：

1. **要记录格式**：一串裸十六进制数离开上下文就没有意义。文件第一行必须写明它属于哪个 `(S,I,F)` 格式，读回时才能正确解释。
2. **负数要按无符号十六进制写**：`np.savetxt` 不懂定点，必须先把负的定点整数补码化（转成等价的无符号大整数），才能写出正确的十六进制。

#### 4.1.2 核心流程

设格式 `format = (S,I,F)`，位宽 \( W = S+I+F \)。一个实数值 \( v \) 先由 en_cl_fix 转成**定点整数**（即量化到分辨率网格上的整数刻度）：

\[
x_{\text{int}} = \text{round}(v / \Delta), \quad \Delta = 2^{-F}
\]

由于 VHDL 侧用 `hread`（按无符号十六进制读）再交回 en_cl_fix 解释，负数必须转成 \( W \) 位二补码对应的无符号值：

\[
x_{\text{hex}} = \begin{cases} x_{\text{int}} & x_{\text{int}} \ge 0 \\ x_{\text{int}} + 2^{W} & x_{\text{int}} < 0 \end{cases}
\]

每个样本写成 `ceil(W/4)` 位十六进制、零填充、大写。最终文件长这样：

```
(1,12,2)      <- 第 1 行：格式头（无 # 前缀）
0004          <- 第 2..N 行：每行一个样本，十六进制
0008
0044
7FF8
```

#### 4.1.3 源码精读

整个类只有两个方法：构造函数记目录，`write_cosim_file` 落盘。

构造函数接收目录（[olo_fix_cosim.py:L23-L29](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py#L23-L29)），所有文件都写到那里。

核心方法是 `write_cosim_file`（[olo_fix_cosim.py:L30-L45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py#L30-L45)），逐行说明它做了什么：

```python
hex_digits = (format.width + 3) // 4          # 每个样本占几个十六进制位（向上取整）
fmt = str(format).replace(" ", "")            # 格式头字符串，如 "(1,12,2)"
data_int = cl_fix_to_integer(data, format)    # 实数/定点 -> 定点整数刻度
# 负数补码化：加上 2^W，变成等价的无符号整数
data_int = np.where(data_int < 0, data_int + 2**cl_fix_width(format), data_int)
np.savetxt(join(self._directory, file_name), data_int,
           fmt=f"%0{hex_digits}X", header=fmt, comments='')  # 头行无 # 前缀
```

四个细节值得圈出：

- `header=fmt, comments=''`：`np.savetxt` 默认会在头行前加 `#`，这里用空 `comments` 去掉它，保证第一行就是纯格式字符串——这与 VHDL 侧 `fixFileCheckHeader` 直接读第一行的约定吻合。
- `fmt=f"%0{hex_digits}X"`：`%0NX` 表示「大写十六进制、补零到 N 位」，所以宽度不足时左边补 0（如 `0004`）。
- 负数的补码化（`+ 2**width`）正是上一节公式的实现。
- 整个文件**不存任何实数**，只存十六进制——这就是「位真」：文件里写的就是最终出现在硬件数据线上的比特。

> **注意：文档与源码不一致。** [doc/fix/olo_fix_cosim.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_cosim.md) 的示例写的是 `writer.write_cosim_files(...)`（复数）并指向旧路径 `src/fix/python/olo_fix_cosim.py`。但真实源码只有一个**单数**方法 `write_cosim_file`，路径在 `src/fix/python/olo_fix/olo_fix_cosim.py`。**以源码为准。**

#### 4.1.4 代码实践

实践目标：亲手生成一个 `.fix` 文件，肉眼确认每一行符合预期。

操作步骤（在仓库根目录）：

1. 写一个三行脚本 `gen_demo.py`（**示例代码**，模仿官方单元测试）：

```python
import sys, os
sys.path.append(os.path.abspath("src/fix/python"))
from olo_fix import olo_fix_cosim        # 导入会顺带把 en_cl_fix 加入路径
from en_cl_fix_pkg import *

w = olo_fix_cosim(".")                    # 写到当前目录
w.write_cosim_file([1.0, 2.0, 17.0, -2.0], FixFormat(1, 12, 2), "demo.fix")
```

2. 运行：`python gen_demo.py`

3. 查看生成文件：`cat demo.fix`

需要观察的现象与预期结果：

- 第一行应为 `(1,12,2)`。格式位宽 \( W=1+12+2=15 \)，`hex_digits=(15+3)//4=4`，故每行 4 个十六进制位。
- 四个样本预期为：

| 实数值 | 定点整数（×2²） | 十六进制 |
| --- | --- | --- |
| 1.0 | 4 | `0004` |
| 2.0 | 8 | `0008` |
| 17.0 | 68 = 0x44 | `0044` |
| -2.0 | -8 → 2¹⁵-8 = 32760 = 0x7FF8 | `7FF8` |

这组期望值与官方单元测试 [test_olo_fix_cosim.py:L31-L37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/tests/test_olo_fix_cosim.py#L31-L37) 完全一致，可作为基准。若运行环境缺少 numpy 或 en_cl_fix，则**待本地验证**（命令本身正确，取决于依赖是否就绪）。

#### 4.1.5 小练习与答案

**练习 1**：把格式换成无符号 `FixFormat(0, 8, 4)`，样本仍为 `[1.0, 2.0, 17.0]`，每行应是几位十六进制？`17.0` 写成什么？

**答案**：\( W=0+8+4=12 \)，`hex_digits=(12+3)//4=3`，每行 3 位。`17.0×2⁴=272=0x110` → `110`（无符号无需补码）。

**练习 2**：为什么文件第一行不写 `# (1,12,2)` 而写裸的 `(1,12,2)`？

**答案**：VHDL 侧 `fixFileCheckHeader` 直接 `readline` 后用 `cl_fix_format_from_string(trim(Line_v.all))` 解析，若带 `#` 前缀会解析失败；Python 端用 `comments=''` 正是为了去掉 `np.savetxt` 默认的 `#`。

---

### 4.2 sim_stimuli 施加激励

#### 4.2.1 概念说明

`olo_fix_sim_stimuli` 是一个**仅用于仿真**的实体（架构名就叫 `sim`，不可综合）。它读取 `olo_fix_cosim` 写出的 `.fix` 文件，逐拍把样本当作 AXI-S 数据「播放」给 DUT。你可以把它理解成一个「文件 → AXI-S 流」的播放器。

它复用了全库通用的两件事：AXI-S 的 Valid/Ready 握手（u1-l5、u2-l2），以及「由字符串泛型推导端口位宽」的模式（u8-l2）。后者让它能在端口声明里直接写 `std_logic_vector(fixFmtWidthFromString(Fmt_g)-1 downto 0)`，使一个实体适配任意定点宽度。

#### 4.2.2 核心流程

```
等复位释放 (Rst='0')
打开文件，校验第一行格式 == Fmt_g
while 文件未读完:
    读一行样本 -> DataSlv_v
    Data <= DataSlv_v                # 把样本送上数据线
    若是 TimingMaster:
        Valid <= '1'
        等待握手 (rising_edge(Clk) 且 Ready='1')   # 数据被 DUT 收下
        以概率 StallProbability_g 随机拉低 Valid 制造反压,
          停顿 StallMinCycles_g..StallMaxCycles_g 拍
    否则 (slave):
        等待 (rising_edge(Clk) 且 Ready='1' 且 Valid='1')   # 被外部 master 驱动
关文件, Valid<='0', 结束 (wait)
```

「TimingMaster」是关键概念：当 `IsTimingMaster_g=true`（默认），本实体**自己驱动** `Valid`，节奏完全由它掌握；当为 `false`，它把 `Valid` 设成高阻 `'Z'`，退化为「数据源」，由外部 master 决定何时真正发送——这正是 `olo_fix_mult` 有两路输入时的用法（见 4.4.3）。

#### 4.2.3 源码精读

实体声明（[olo_fix_sim_stimuli.vhd:L36-L52](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L36-L52)）：

```vhdl
entity olo_fix_sim_stimuli is
    generic (
        FilePath_g         : string;
        IsTimingMaster_g   : boolean  := true;
        Fmt_g              : string;
        StallProbability_g : real     := 0.0;
        StallMaxCycles_g   : positive := 1;
        StallMinCycles_g   : positive := 1
    );
    port (
        Clk   : in    std_logic;
        Rst   : in    std_logic;
        Ready : in    std_logic := '1';
        Valid : inout std_logic; -- master 时输出, slave 时高阻
        Data  : out   std_logic_vector(fixFmtWidthFromString(Fmt_g)-1 downto 0)
    );
end entity;
```

注意 `Valid` 是 `inout`：master 模式下当输出驱动，slave 模式下赋 `'Z'` 让外部驱动。`Data` 宽度在** elaboration 期**由 `fixFmtWidthFromString(Fmt_g)` 决定——该函数把字符串 `"(1,12,2)"` 解析成 `FixFormat_t` 再返回 `cl_fix_width`（[olo_fix_pkg.vhd:L118-L122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L118-L122)）。字符串泛型让同一实体可被 Verilog 实例化（u8-l1 的动机）。

主体是一个单一进程 `p_main`。先等复位释放（[L83-L84](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L83-L84)），再开文件并校验表头（[L87-L88](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L87-L88)）：

```vhdl
file_open(DataFile, FilePath_g, read_mode);
fixFileCheckHeader(DataFile, Fmt_c);
```

`fixFileCheckHeader` 读第一行、解析成格式、`assert` 它等于 `Fmt_c`（[olo_fix_pkg.vhd:L237-L249](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L237-L249)）。这条断言是「Python 与 HDL 格式一致性」的守门员：如果 Python 写的是 `(1,8,4)` 而 TB 的 `Fmt_g` 是 `(1,12,2)`，仿真一启动就会报 `Format mismatch`，避免拿错格式的数据瞎比对。

主循环逐样本播放（[L91-L122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L91-L122)）。master 模式下，先把 `Data` 送上线、`Valid<='1'`，然后等一次满足 `Ready='1'` 的上升沿——数据被收下后才读下一个样本，保证不丢。随机 stall 用 `ieee.math_real` 的 `uniform` 生成概率与停顿拍数（[L104-L118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L104-L118)）。注意它是「先发一个，再决定要不要停顿」，停顿时把 `Valid` 拉低、`Data` 设成 `'U'`，避免悬空数据被 DUT 误采。

#### 4.2.4 代码实践

实践目标：运行官方对 `olo_fix_sim_stimuli` 自身的测试台，观察它在 master/slave 两种模式与随机反压下的行为。

操作步骤（在 `sim/` 目录，承接 u1-l4 的运行方式）：

1. 只跑这个实体的测试：`python run.py *olo_fix_sim_stimuli*`
2. 打开 [olo_fix_sim_stimuli_tb.vhd:L116-L145](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_sim_stimuli/olo_fix_sim_stimuli_tb.vhd#L116-L145)，对照实例化：一个 `IsTimingMaster_g` 默认（true）、一个显式 `false`，两者读同一文件、喂各自的 checker。

需要观察的现象：

- master 实例自己驱动 `Valid`，节奏受 `StallProbability_g=0.5` 控制；slave 实例的 `Valid` 由外部（这里 checker VC 当 master）驱动。
- 两种模式最终都被 checker 比对通过（因为读的是同一份 `Data.fix`）。
- 由于 stall 是随机的，每次仿真波形会不同，但「全部样本都正确送达」不变。

预期结果：测试用例 `Test` 通过。具体通过/失败**待本地验证**（需 VUnit + GHDL 环境，命令本身正确）。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `fixFileCheckHeader(DataFile, Fmt_c)` 这一行，最坏会发生什么？

**答案**：表头行 `(1,12,2)` 会被 `fixFileReadSample` 当成第一个数据样本去 `hread`，解析失败报 `Failed to read sample from file`；即便侥幸解析，后续所有样本也会错位一格。表头校验是用错误格式数据自检的廉价保险。

**练习 2**：master 模式下，为什么 `Data <= DataSlv_v` 要在 `wait until ... Ready='1'` **之前**赋值？

**答案**：这样数据在 `Valid` 拉高的同一拍就已稳定在线上，握手成功的那一上升沿 DUT 直接采到正确数据；若先等再赋值，会多延迟一拍甚至采到旧值。

---

### 4.3 sim_checker 比对

#### 4.3.1 概念说明

`olo_fix_sim_checker` 是 `sim_stimuli` 的镜像：它同样读一个 `.fix` 文件，但不把样本送出去，而是**逐拍拿样本和 DUT 的实际输出比对**，不一致就报错。它是这套体系的「裁判」。

它和 `sim_stimuli` 共享同一组泛型（连默认值都一样）和同一个文件格式，因此一个 `.fix` 文件既能当激励也能当期望——只是接在不同的端口上。

#### 4.3.2 核心流程

```
若是 TimingMaster: Ready<='0' (自己控制消费节奏)
打开文件, 校验表头 == Fmt_g
while 文件未读完:
    若是 TimingMaster:
        以概率 StallProbability_g 拉低 Ready 制造反压
        Ready<='1', 等待 (rising_edge(Clk) 且 Valid='1')
    否则:
        等待 (rising_edge(Clk) 且 Ready='1' 且 Valid='1')
    读一行期望样本 -> DataSlv_v
    assert Data = DataSlv_v, 否则报 "Data mismatch: expected ..., got ... line N"
关文件, 结束
```

注意一个对称性差异：stimuli 在 master 时驱动 **Valid**，checker 在 master 时驱动 **Ready**——因为前者是「发」，后者是「收」。

#### 4.3.3 源码精读

实体声明（[olo_fix_sim_checker.vhd:L36-L51](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L36-L51)），`Ready` 是 `inout`（master 时输出反压）、`Valid`/`Data` 是输入（接 DUT 输出）。`Data` 宽度同样由 `fixFmtWidthFromString(Fmt_g)` 决定。

主循环与 stimuli 几乎对称（[olo_fix_sim_checker.vhd:L88-L127](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L88-L127)）。核心是比对断言（[L114-L125](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L114-L125)）：

```vhdl
DataSlv_v := fixFileReadSample(DataFile, Fmt_c);
-- pragma translate_off
assert Data = DataSlv_v
    report errorMessage(EntityName_c, "Data mismatch: expected " & to_string(DataSlv_v) &
           ", got " & to_string(Data) & " - file " & FilePath_g &
           " - line " & to_string(LineNumber_v))
    severity error;
-- pragma translate_on
LineNumber_v := LineNumber_v + 1;
```

三个要点：

1. **severity 是 `error` 而非 `failure`**：不匹配会打印错误信息但**不会立即停止仿真**，而是继续比对其余样本——这样一次运行能报出所有出错位置，而不是只看第一个。
2. **`-- pragma translate_off` 包裹**：因为 `to_string` 在某些综合工具里有问题，这段只在仿真期生效，综合时被剔除。
3. **报错带行号**：`LineNumber_v` 从 2 开始（第 1 行是表头），出错时直接告诉你「期望文件第几行」对不上，方便定位。

读样本用 `fixFileReadSample`（[olo_fix_pkg.vhd:L251-L269](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L251-L269)）：`hread` 读到一个「按 4 位向上取整」的向量，再截取低 `Width_c` 位返回——与 Python 侧「写满 `ceil(W/4)` 个十六进制位」精确对应，正是位真的落点。

#### 4.3.4 代码实践

实践目标：人为制造一次比对失败，观察 checker 的报错信息长什么样。

操作步骤：

1. 先按 4.1.4 生成一个正确的 `demo.fix`（4 个样本）。
2. 用编辑器把 `demo.fix` 的某一行（例如第 3 行 `0044`）改成 `0045`，另存为 `demo_bad.fix`。这相当于「故意写错期望」。
3. 在一个临时 TB 里把 `olo_fix_sim_checker` 的 `FilePath_g` 指向 `demo_bad.fix`，喂给它与正确流一致的数据（或直接复用 stimuli 把正确的 `demo.fix` 喂进一个直通 `olo_fix_resize`，再把 checker 指向 `demo_bad.fix`）。

需要观察的现象：

- 仿真日志应出现一条 `Data mismatch: expected 0045, got 0044 - file .../demo_bad.fix - line 3`，`severity error`。
- 由于 severity 是 `error`，仿真不会停，其余 3 行若匹配则不报错。

预期结果：看到带行号的 mismatch 报告。是否能在你的工具里复现**待本地验证**，但报错文本由上面源码逐字决定，可据此对照。

#### 4.3.5 小练习与答案

**练习 1**：checker 用 `LineNumber_v` 从 2 开始计数，为什么不是从 1？

**答案**：文件第 1 行是格式表头（被 `fixFileCheckHeader` 消费），第一个数据样本在第 2 行，所以数据行号从 2 起，报错行号与文本编辑器里看到的行号一致。

**练习 2**：为什么 checker 把断言 severity 设成 `error` 而不是 `failure`？

**答案**：`failure` 会立刻中止仿真，只能看到第一个错误；`error` 只报告不中止，一次跑完可列出所有出错样本，调试效率更高。

---

### 4.4 位真协仿真流程

#### 4.4.1 概念说明

前三块是零件，本块讲怎么把它们装成「Python 算 → 文件交换 → HDL 跑 → 自动比对」的闭环。Open Logic 的做法是：**为每个 `olo_fix_*` 实体配一个同名 `cosim.py` 脚本**，脚本里既算输入、也算期望；再用 VUnit 的 `pre_config` 钩子在每次仿真前自动跑这个脚本、把 `.fix` 文件写进本次运行的输出目录；TB 用 `output_path(runner_cfg)` 读回。这样每换一组 generic，期望文件就自动重新生成，永远匹配。

#### 4.4.2 核心流程

```
[Python 单一真相源]
   olo_fix_mult.py (位真模型, dut.process)
            │  cosim.py 调用它, 生成 A/B/Result
            ▼
[pre_config 钩子]   named_config(..., pre_config=cosim)
   VUnit 仿真前调用 cosim(output_path, generics), 写出 *.fix
            │
            ▼
[HDL 测试台]
   A.fix ──► olo_fix_sim_stimuli ──► In_A ┐
                                          ├─► olo_fix_mult (DUT) ──► Out_Result ──► olo_fix_sim_checker ──► Result.fix (期望)
   B.fix ──► olo_fix_sim_stimuli ──► In_B ┘
```

关键时序：**`pre_config` 在该测试用例仿真开始之前、但在 VUnit 已创建好输出目录之后运行**，所以写出的文件 TB 一定能读到。

#### 4.4.3 源码精读

先看 `olo_fix_mult` 的协仿真脚本 [test/fix/olo_fix_mult/cosim.py:L19-L55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py#L19-L55)，它体现了所有 `cosim.py` 的通用骨架：

```python
def cosim(output_path : str = None, generics : dict = None, cosim_mode : bool = True):
    # 1. 把字符串泛型解析回类型对象（u8-l2 的反向操作）
    AFmt_g   = olo_fix_utils.fix_format_from_string(generics["AFmt_g"])
    ...
    Round_g     = FixRound[generics["Round_g"]]
    Saturate_g  = FixSaturate[generics["Saturate_g"]]
    # 2. 生成输入（固定随机种子保证可复现）
    np.random.seed(42)
    in_a = cl_fix_from_real(np.linspace(cl_fix_min_value(AFmt_g), cl_fix_max_value(AFmt_g), 100), AFmt_g)
    ...
    # 3. 用位真模型算期望
    dut = olo_fix_mult(AFmt_g, BFmt_g, ResultFmt_g, Round_g, Saturate_g)
    out_data = dut.process(in_a, in_b)
    # 4. 把输入与期望都写成 .fix 文件
    if cosim_mode:
        writer = olo_fix_cosim(output_path)
        writer.write_cosim_file(in_a, AFmt_g, "A.fix")
        writer.write_cosim_file(in_b, BFmt_g, "B.fix")
        writer.write_cosim_file(out_data, ResultFmt_g, "Result.fix")
    return True
```

四个要点：

- 函数签名 `cosim(output_path, generics, cosim_mode)` 是**全库统一约定**：每个实体的 `cosim.py` 都长这样，`output_path` 由 VUnit 传入，`generics` 是本测试用例的泛型字典，`cosim_mode=False` 时只画图调试、不写文件。
- 输入用固定种子 `np.random.seed(42)` 与 `np.linspace`，保证每次生成**完全相同**的激励——可复现是验证的基本要求。
- 期望 `out_data` 由 `olo_fix_mult` 的 Python 位真模型 `dut.process` 算出，与 HDL 实体共享同一套 en_cl_fix 运算（u8-l3），故二者必须逐位一致。
- 一共写三个文件：`A.fix`、`B.fix`（激励）、`Result.fix`（期望）。

再看 VUnit 如何把这个脚本挂上去。[sim/test_configs/olo_fix.py:L161-L195](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L161-L195) 为 add/sub/mult/addsub 四个实体统一配置：

```python
fix_addsubb_tbs = {'olo_fix_mult_tb' : olo_fix_mult.cosim.cosim, ...}
for tb_name, cosim in fix_addsubb_tbs.items():
    tb = olo_tb.test_bench(tb_name)
    default_generics = {'AFmt_g':'(1,8,4)', 'BFmt_g':'(1,5,7)', 'ResultFmt_g':'(0,6,3)', ...}
    for Round in ['NonSymPos_s', 'Trunc_s']:
        for Sat in ['Sat_s', 'None_s']:
            named_config(tb, default_generics | {'Round_g': Round, 'Saturate_g': Sat},
                            pre_config=cosim)
```

`named_config`（[utils.py:L15-L21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21)）是关键的粘合层：

```python
def named_config(tb, map : dict, pre_config = None, short_name = None):
    ...
    if pre_config is not None:
        pre_config = partial(pre_config, generics=map)   # 把 generic 绑死进回调
    tb.add_config(name=cfg_name, generics=map, pre_config=pre_config)
```

`partial(pre_config, generics=map)` 把当前这组 generic 绑进 `cosim`，于是 VUnit 在仿真前调用 `pre_config(output_path)` 时，实际执行的就是 `cosim(output_path, generics=map, cosim_mode=True)`——写出与本用例 generic 完全匹配的 `.fix` 文件。

最后看 TB 怎么读回这些文件。[olo_fix_mult_tb.vhd:L79-L81](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L79-L81)：

```vhdl
constant AFile_c      : string := output_path(runner_cfg) & "A.fix";
constant BFile_c      : string := output_path(runner_cfg) & "B.fix";
constant ResultFile_c : string := output_path(runner_cfg) & "Result.fix";
```

`output_path(runner_cfg)` 是 VUnit 提供的函数，返回本次运行专属的输出目录——正是 `pre_config` 写文件的同一个目录。两边用同一个目录，文件就「接上了」。

> **关于 VC 包装层。** 仓库的 `olo_fix_mult_tb` 并没有直接实例化 `olo_fix_sim_stimuli`/`olo_fix_sim_checker`，而是用了更上层的验证组件 `olo_test_fix_stimuli_vc`/`olo_test_fix_checker_vc`（通过 VUnit 的消息机制 `fix_stimuli_play_file`/`fix_checker_check_file` 驱动）。这些 VC 内部包裹的就是本讲这两个实体。对学习者而言，直接实例化 `olo_fix_sim_stimuli`/`olo_fix_sim_checker` 更能看清机制，综合实践的示例 TB 就采用这种直接写法。

#### 4.4.4 代码实践

实践目标：跟踪一条完整的「generic → 文件 → 比对」调用链，画出数据流。

操作步骤（源码阅读型实践）：

1. 从 [olo_fix.py:L165](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L165) 找到 `'olo_fix_mult_tb' : olo_fix_mult.cosim.cosim`。
2. 跟进 [cosim.py:L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py#L19)，确认它把 `generics["AFmt_g"]` 等解析后写出 `A.fix`/`B.fix`/`Result.fix`。
3. 跟进 [named_config](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21) 的 `partial(pre_config, generics=map)`，说明 generic 如何绑进回调。
4. 跟进 [olo_fix_mult_tb.vhd:L79-L81](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L79-L81)，确认 TB 用 `output_path(runner_cfg)` 读回同名文件。

需要观察的现象：四段代码引用的目录是同一个 `output_path`，文件名（`A.fix` 等）在 Python 与 VHDL 两侧拼写一致。

预期结果：你能用一句话讲清「为什么改一组 generic，期望文件就会自动跟着变」——因为 `named_config` 把该 generic 绑进了 `pre_config`，VUnit 在仿真前用新 generic 重跑了 `cosim`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `olo_fix_mult_tb.vhd` 里 `ResultFile_c` 的文件名改成 `"Result2.fix"`，但 `cosim.py` 仍写 `"Result.fix"`，会发生什么？

**答案**：checker 找不到 `Result2.fix`，`file_open` 失败或读到空文件，仿真报错。两侧文件名与目录必须完全对应，这正是用 `output_path(runner_cfg)` 统一目录、用统一命名约定的原因。

**练习 2**：为什么 `cosim` 里要用 `np.random.seed(42)` 固定随机种子？

**答案**：协仿真要求可复现——同一组 generic 每次都生成完全相同的激励与期望，否则失败无法复现、也无法判断是 DUT 真有 bug 还是输入变了。固定种子把「随机」变成「确定的伪随机」。

---

## 5. 综合实践

把三件套真正串起来：**用 `olo_fix_cosim` 生成激励与期望，在仿真中用 `sim_stimuli` 驱动 `olo_fix_mult`，用 `sim_checker` 比对，验证全部通过。**

### 步骤 1：写一个最小 cosim 脚本（示例代码）

模仿 `test/fix/olo_fix_mult/cosim.py`，新建 `my_cosim.py`：

```python
import sys, os, numpy as np
sys.path.append(os.path.abspath("src/fix/python"))
from olo_fix import olo_fix_cosim, olo_fix_mult
from en_cl_fix_pkg import *

AFmt, BFmt, RFmt = FixFormat(1,4,4), FixFormat(1,4,4), FixFormat(1,8,4)
in_a = cl_fix_from_real(np.linspace(-5, 5, 50), AFmt)
in_b = cl_fix_from_real(np.linspace(-2, 2, 50), BFmt)
out  = olo_fix_mult(AFmt, BFmt, RFmt, Trunc_s, Sat_s).process(in_a, in_b)

w = olo_fix_cosim(".")
w.write_cosim_file(in_a, AFmt, "A.fix")
w.write_cosim_file(in_b, BFmt, "B.fix")
w.write_cosim_file(out,  RFmt, "Result.fix")
```

运行 `python my_cosim.py`，确认生成三个 `.fix` 文件，第一行分别是 `(1,4,4)`/`(1,4,4)`/`(1,8,4)`。

### 步骤 2：写一个直接实例化三件套的测试台（示例代码）

下面是一个**直接实例化** `olo_fix_sim_stimuli`/`olo_fix_sim_checker` 的极简 TB（仓库自带 TB 走 VC 包装层，这里为教学改写成直连）：

```vhdl
-- 示例代码：olo_fix_mult 的最小直连协仿真 TB
library ieee; use ieee.std_logic_1164.all;
library olo;   use olo.en_cl_fix_pkg.all; use olo.olo_fix_pkg.all;

entity my_mult_cosim_tb is
end entity;

architecture sim of my_mult_cosim_tb is
    signal Clk : std_logic := '0';
    signal Rst : std_logic := '1';
    signal InA_Valid, InB_Valid, Out_Valid, In_Ready : std_logic;
    signal In_A : std_logic_vector(fixFmtWidthFromString("(1,4,4)")-1 downto 0);
    signal In_B : std_logic_vector(fixFmtWidthFromString("(1,4,4)")-1 downto 0);
    signal Out_Result : std_logic_vector(fixFmtWidthFromString("(1,8,4)")-1 downto 0);
begin
    Clk <= not Clk after 5 ns;
    Rst <= '0' after 30 ns;
    In_Ready <= '1';                       -- DUT 输入满吞吐接收

    -- 激励 A：自己当 TimingMaster 驱动 Valid
    stim_a : entity olo.olo_fix_sim_stimuli
        generic map (FilePath_g => "A.fix", Fmt_g => "(1,4,4)")
        port map (Clk, Rst, In_Ready, InA_Valid, In_A);

    -- 激励 B：跟随 A 的 Valid（IsTimingMaster=false）
    stim_b : entity olo.olo_fix_sim_stimuli
        generic map (FilePath_g => "B.fix", Fmt_g => "(1,4,4)", IsTimingMaster_g => false)
        port map (Clk, Rst, In_Ready, InA_Valid, In_B);

    -- 被测设计
    dut : entity olo.olo_fix_mult
        generic map (AFmt_g=>"(1,4,4)", BFmt_g=>"(1,4,4)", ResultFmt_g=>"(1,8,4)",
                     Round_g=>"Trunc_s", Saturate_g=>"Sat_s")
        port map (Clk, Rst, InA_Valid, In_A, In_B, Out_Valid, Out_Result);

    -- 比对：读 Result.fix，逐拍断言
    chk : entity olo.olo_fix_sim_checker
        generic map (FilePath_g => "Result.fix", Fmt_g => "(1,8,4)")
        port map (Clk, In_Ready, Out_Valid, Out_Result);
end architecture;
```

要点：

- `stim_a` 当 master 驱动 `InA_Valid`；`stim_b` 设 `IsTimingMaster_g=false`，复用同一个 `InA_Valid`，保证 A/B 同步送数（与真实 `olo_fix_mult_tb` 的做法一致，见 [olo_fix_mult_tb.vhd:L186-L198](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L186-L198)）。
- checker 的 `Fmt_g` 必须等于 `ResultFmt_g`，否则 `fixFileCheckHeader` 会报格式不匹配。
- 把 `A.fix`/`B.fix`/`Result.fix` 放在该 TB 能读到的路径（直接用相对路径，或改为绝对路径）。

### 步骤 3：运行与判断

用 GHDL 跑这个 TB（具体命令取决于你的工程组织，最简单是把它加进 `olo_tb` 库后用 `sim/run.py` 选择运行）。

需要观察的现象与预期结果：

- 若 DUT 与 Python 位真模型完全一致，仿真日志**没有** `Data mismatch` 报告，正常结束。
- 若人为把 `Result.fix` 某行改错（如 4.3.4），则会看到带行号的 mismatch，且因 `severity error` 不中止。

整个综合实践能否在你本地一次跑通**待本地验证**（依赖 VUnit + GHDL + numpy + en_cl_fix 是否就绪），但每一环节的行为都由本讲引用的源码精确决定，可据此排查。

## 6. 本讲小结

- **位真是定点验证的本质要求**：正确答案是确定的比特模式，必须用与 HDL 同源的 Python 模型算期望，再逐位比对。
- **`olo_fix_cosim` 只做落盘**：把定点样本补码化、写成「第 1 行格式头 + 后续每行一个十六进制样本」的文本文件。注意真实方法是单数 `write_cosim_file`，文档里的复数写法已过时。
- **`olo_fix_sim_stimuli` 是文件→AXI-S 播放器**：校验表头后逐样本播放，master 模式自驱 Valid、可随机 stall 制造反压，端口位宽由字符串泛型 elaboration 期推导。
- **`olo_fix_sim_checker` 是镜像裁判**：逐拍比对 DUT 输出与期望，`severity error` 保证一次报出所有出错行号，断言用 `pragma translate_off` 包裹仅仿真生效。
- **`pre_config` 是闭环粘合剂**：每个实体的 `cosim.py` 经 `named_config` 绑定 generic 后挂为 VUnit 钩子，在仿真前把匹配当前 generic 的 `.fix` 文件写进 `output_path`，TB 用 `output_path(runner_cfg)` 读回。
- **统一目录 + 统一命名**是文件能「接上」的保证：Python 写、HDL 读，两侧用同一 `output_path` 与同名文件。

## 7. 下一步学习建议

- **进入第 9 单元（高级 DSP）**：CORDIC、CIC、FIR 等复杂实体全部沿用本讲的协仿真范式，每个都带 `cosim.py`。读 [test/fix/olo_fix_cordic_vect/cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_cordic_vect/cosim.py) 等脚本，体会「复杂模型如何生成多维期望」。
- **学习 VC 包装层**：阅读 `test/tb/olo_test_fix_stimuli_vc.vhd`、`olo_test_fix_checker_vc.vhd`（及其 pkg），理解项目实际 TB 如何用 VUnit 消息机制（`fix_stimuli_play_file`/`fix_checker_check_file`）驱动本讲这两个底层实体，以及 u10-l1 将系统讲解的验证组件约定。
- **为自定义实体写协仿真**：参照 `olo_fix_mult/cosim.py` 的四段骨架（解析 generic → 生成输入 → 模型算期望 → 写文件），为你自己的定点实体配一个 `cosim.py`，并仿照 `olo_fix.py` 用 `named_config(..., pre_config=cosim)` 注册几组 generic，体会「单一真相源」带来的维护便利。
- **回顾 u8-l4**：`olo_fix_pkg_writer` 生成的是「常量包」，本讲的 `olo_fix_cosim` 生成的是「数据文件」——两者都是 Python 单一真相源思想在不同场景的体现，对照阅读能加深理解。
