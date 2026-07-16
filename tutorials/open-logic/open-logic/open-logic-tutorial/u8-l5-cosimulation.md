# 协同仿真：olo_fix_cosim 与 sim_stimuli/checker

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「定点协仿真（co-simulation）」到底在解决什么问题，以及它为什么必须是**位真（bit-true）**的。
- 用 Python 工具 `olo_fix_cosim` 生成协仿真文件，并解释文件里每一行的含义（格式头 + 十六进制样本）。
- 读懂 `olo_fix_sim_stimuli`（回放激励）与 `olo_fix_sim_checker`（比对输出）两个 VHDL 实体的工作流程，理解 **timing-master / timing-slave** 的分工。
- 把 Python 黄金模型 → 文件 → VUnit `pre_config` → 测试台 → DUT → checker 这条端到端链路串起来，并理解「文件必须在仿真启动前生成」这一关键时序约束。
- 自己动手用协仿真验证一个定点实体（以 `olo_fix_mult` 为例）。

## 2. 前置知识

本讲是 fix 区域的收尾课，默认你已经学过：

- **u8-l1 / u8-l2**：定点格式三元组 `(S,I,F)`、`en_cl_fix` 的 `FixFormat_t`、以及 Open Logic 用「字符串泛型」对外暴露定点类型的模式。
- **u8-l3**：`olo_fix_*` 实体与 `en_cl_fix` 函数的**位真等价**关系——同一套数学，HDL 实体和 Python 函数算出的比特位完全一致。
- **u8-l4**：`sim/run.py` 在 VUnit 扫描源文件之前会先调用 `codegen_generate()` 生成代码；本讲会用到一个非常类似的「仿真前先跑 Python」的时机点。

补充几个本讲用到、但前面讲义没有重点展开的概念：

- **协仿真（co-simulation）**：用一段「绝对正确」的参考模型（这里是 Python + `en_cl_fix`）算出期望结果，再让 HDL 仿真跑出实际结果，二者逐拍比对。它不是让 Python 和 HDL 在同一个仿真器里实时对话，而是用**文件**做中介：Python 先把激励和期望写进文件，HDL 仿真时再读出来。
- **位真（bit-true）**：参考模型与 HDL 在**二进制位级别**完全一致，而不是「数值近似」。定点运算的舍入、饱和、位宽截断都会改变最低几位，只有位真比对才能抓住这些 bug。
- **AXI-S 握手 / 反压**：参见 u1-l5、u2-l2。本讲的 stimuli/checker 都遵循 Valid/Ready 握手。
- **VUnit `pre_config`**：VUnit 在**每个测试用例开始仿真之前**调用的 Python 钩子函数，正好用来「按需生成协仿真文件」。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/fix/python/olo_fix/olo_fix_cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py) | Python 端：把定点数组写成协仿真文件（格式头 + 十六进制样本）。 |
| [src/fix/vhdl/olo_fix_sim_stimuli.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd) | VHDL 端：读协仿真文件，把样本当作激励施加到 DUT 输入。 |
| [src/fix/vhdl/olo_fix_sim_checker.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd) | VHDL 端：读协仿真文件，把样本当作期望，逐拍比对 DUT 输出，不一致即报错。 |
| [src/fix/vhdl/olo_fix_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd) | 提供 `fixFileCheckHeader`、`fixFileReadSample`、`fixFmtWidthFromString` 等文件读写辅助函数。 |
| [test/fix/olo_fix_mult/cosim.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py) | `olo_fix_mult` 的协仿真脚本：用 Python 位真模型算出 A、B、Result 三个文件。 |
| [test/fix/olo_fix_mult/olo_fix_mult_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd) | `olo_fix_mult` 的测试台：回放 A/B、比对 Result（用 VUnit VC 变体）。 |
| [sim/test_configs/olo_fix.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py) | 把每个实体的 `cosim()` 注册成 VUnit `pre_config`，决定「先生成文件、再仿真」。 |

> 提示：协仿真文件就是 stimuli 与 checker 之间约定的「公共数据格式」，Python 写、VHDL 读，二者必须对格式（头）和编码（十六进制补码）达成一致。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① Python 端生成文件；② stimuli 施加激励；③ checker 比对输出；④ 把三者串成端到端位真流程。

### 4.1 cosim 文件生成（olo_fix_cosim）

#### 4.1.1 概念说明

定点实体最难测的不是「算得对不对」，而是「最低几位对不对」——舍入模式、饱和、位宽收剑都会在末位引入差异。靠人手算几个期望值既慢又容易错。Open Logic 的做法是：**让 Python 当裁判**。Python 里已经有和 HDL 位真等价的 `en_cl_fix` 函数（u8-l3），于是可以用它跑出大量随机/边界激励的期望结果，再让 HDL 仿真逐拍比对。

`olo_fix_cosim` 就是这个流程里「Python 把数据写到文件」的那一环。它是一个极简的 Python 类：构造时给一个目录，调用 `write_cosim_file(data, format, file_name)` 就能在该目录下生成一个 `.fix` 文件。文件内容长这样（来自项目自带的 Python 单元测试）：

```text
(1,12,2)
0004
0008
0044
7FF8
```

- **第 1 行**：定点格式字符串 `(S,I,F)`，这里是 `(1,12,2)`。
- **后续每一行**：一个样本，写成定宽大写十六进制（这里是 4 位 hex）。

> ⚠️ 文档 [`doc/fix/olo_fix_cosim.md`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_cosim.md) 里写的是 `write_cosim_files(...)`（复数）且引用了旧路径，**与当前源码不一致**。以源码为准：方法是单数 `write_cosim_file`，路径在 `src/fix/python/olo_fix/olo_fix_cosim.py`。这一点也提醒我们：Open Logic 的 doc 偶尔滞后于代码，遇到不一致永远以源码/测试为真相。

#### 4.1.2 核心流程

把一个定点样本写成十六进制，要经过三步换算。给定格式 `(S,I,F)`：

- 位宽 \(W = S + I + F\)，分辨率 \(\Delta = 2^{-F}\)。
- 实数值 \(v\) 的**整数编码**：\(\text{code} = \mathrm{round}(v / \Delta) = \mathrm{round}(v \cdot 2^{F})\)（由 `en_cl_fix` 的 `cl_fix_from_real` / `cl_fix_to_integer` 完成）。
- 写十六进制前，把负数转成**无符号二进制补码**：\(\text{u} = \text{code} \bmod 2^{W}\)，即负数加上 \(2^{W}\)。
- 十六进制位数：\(d = \lceil W/4 \rceil = (W+3)//4\)。

伪代码：

```text
hex_digits = (W + 3) // 4
header     = str(format) 去空格            # 例如 "(1,12,2)"
code       = cl_fix_to_integer(data, fmt)  # 定点 -> 整数编码
code[code<0] += 2**W                        # 负数转无符号补码
savetxt(file, code, fmt="%0{hex_digits}X", header=header, comments='')
```

以上面的 `(1,12,2)`（\(W=15, F=2, \Delta=0.25, d=4\)）验证 `[1.0, 2.0, 17.0, -2.0]`：

| 实数值 | 整数编码 | 无符号补码 | 十六进制 |
| ---: | ---: | ---: | :--- |
| 1.0 | 4 | 4 | `0004` |
| 2.0 | 8 | 8 | `0008` |
| 17.0 | 68 | 68 | `0044` |
| -2.0 | -8 | -8 + 2¹⁵ = 32760 | `7FF8` |

与单元测试的期望完全一致。

#### 4.1.3 源码精读

整个类非常短，核心只有一个方法：

- [olo_fix_cosim.py:17-28](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py#L17-L28)：类定义与构造函数，构造时只记住目标目录 `self._directory`。

- [olo_fix_cosim.py:30-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_cosim.py#L30-L45)：`write_cosim_file`，三步换算 + 写文件。关键几句：

  ```python
  hex_digits = (format.width + 3) // 4            # L38：十六进制位数
  fmt = str(format).replace(" ", "")              # L39：格式头，如 "(1,12,2)"
  data_int = cl_fix_to_integer(data, format)      # L41：定点 -> 整数编码
  data_int = np.where(data_int < 0,               # L44：负数转无符号补码
                      data_int + 2**cl_fix_width(format), data_int)
  np.savetxt(join(self._directory, file_name),    # L45：写头 + 十六进制样本
             data_int, fmt=f"%0{hex_digits}X", header=fmt, comments='')
  ```

  `np.savetxt` 的 `header=fmt, comments=''` 会把格式串作为第一行写入（默认 `comments` 是 `# `，这里置空让它成为纯净的格式头），随后每行一个样本。`%0{hex_digits}X` 保证高位补零、定宽、大写。

#### 4.1.4 代码实践

**目标**：亲手生成一个 `.fix` 文件并看清它的内容。

**步骤**：

1. 进入仓库根目录，把 Python 工具路径加进来（与项目测试脚本相同的手法）：

   ```bash
   cd <仓库根>
   PYTHONPATH=src/fix/python python3 -c "
   from olo_fix import olo_fix_cosim
   from en_cl_fix_pkg import *
   w = olo_fix_cosim('/tmp/cosim_demo')
   w.write_cosim_file([1.0, 2.0, 17.0, -2.0], FixFormat(1,12,2), 'demo.fix')
   "
   ```

2. 查看生成的文件：

   ```bash
   cat /tmp/cosim_demo/demo.fix
   ```

**需要观察的现象**：第一行是 `(1,12,2)`，随后四行依次是 `0004`、`0008`、`0044`、`7FF8`。

**预期结果**：与本讲 4.1.2 的表格逐行一致（也是项目单元测试 `test_olo_fix_cosim.py` 的期望）。若你的 `en_cl_fix` 版本不同导致个别位不同，以你本地版本为准（**待本地验证**）。

#### 4.1.5 小练习与答案

**Q1**：格式 `(0,8,4)` 的位宽是多少？写一个值 `1.5` 会得到几位 hex、内容是什么？

**答**：\(W=0+8+4=12\)，\(d=\lceil 12/4\rceil=3\) 位 hex。\(\Delta=2^{-4}=0.0625\)，\(1.5/0.0625=24\)，即 `018`。

**Q2**：为什么负数要加 \(2^{W}\) 再写 hex，而不是直接写负号？

**答**：硬件里定点数以二进制补码存放，没有符号位标记。十六进制只是把这串比特按 4 位一组显示出来，因此必须先转成无符号补码表示，才能与 HDL 里的 `std_logic_vector` 一一对应。

---

### 4.2 sim_stimuli 施加激励

#### 4.2.1 概念说明

`olo_fix_sim_stimuli` 是「文件 → DUT 输入」的回放器：它逐行读 `.fix` 文件，把每个样本送上 `Data` 端口，并用 AXI-S 的 `Valid`/`Ready` 与 DUT 握手。它只回放一次，读完即 `wait;` 挂起（若需多次回放/循环，改用它的 VUnit 变体 `olo_test_fix_stimuli_vc`）。

关键设计是 **timing-master / timing-slave** 两种角色（由 `IsTimingMaster_g` 选择）：

- **timing-master**：自己驱动握手节奏。对 stimuli 而言就是它**输出** `Valid`，并可按 `StallProbability_g` 随机地把 `Valid` 拉低制造反压。
- **timing-slave**：不驱动节奏，只**跟随**。此时 `Valid` 是输入，stimuli 在「别人驱动的 `Valid=1` 且 `Ready=1`」的时钟沿上推进。

为什么要分主从？当 DUT 有**多个输入**（例如 `olo_fix_mult` 有 A、B 两路）时，各路必须**同拍推进**才能配对。于是约定：每个方向只设**一个** timing-master 驱动 `Valid`，其余全是 slave，共享同一个 `Valid` 信号，从而天然同步。

#### 4.2.2 核心流程

stimuli 的主进程是一个纯顺序进程：

```text
初始化 Valid（master=>0 / slave=>'Z'）
等待复位释放（rising_edge(Clk) and Rst='0'）
打开文件；fixFileCheckHeader(...)            # 校验第 1 行格式 == Fmt_g
while 文件未读完:
    sample = fixFileReadSample(...)          # 读一行 hex -> std_logic_vector
    Data <= sample                            # 施加到端口
    if master:
        Valid <= '1'
        wait until rising_edge(Clk) and Ready='1'    # 这拍样本被消费
        以概率 StallProbability_g 随机拉低 Valid 制造停顿
    else: # slave
        wait until rising_edge(Clk) and Ready='1' and Valid='1'
关闭文件；Valid <= '0'（master）；wait;        # 一次性回放结束
```

注意一个细节：master 模式下，stimuli **先读样本并施加，再等握手**；这保证了在握手成功的那一拍，`Data` 上就是当前样本。

#### 4.2.3 源码精读

- [olo_fix_sim_stimuli.vhd:36-52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L36-L52)：实体声明。泛型 `FilePath_g` / `IsTimingMaster_g` / `Fmt_g` / 三个停顿参数；端口里 `Data` 的宽度由编译期函数 `fixFmtWidthFromString(Fmt_g)` 直接推出（字符串泛型模式的典型用法，见 u8-l2），`Valid` 是 `inout`——master 时当输出、slave 时当输入。

- [olo_fix_sim_stimuli.vhd:57](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L57)：把字符串泛型 `Fmt_g` 在编译期还原成类型常量 `Fmt_c : FixFormat_t`，供进程使用。

- [olo_fix_sim_stimuli.vhd:84-93](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L84-L93)：等复位释放后打开文件，第一件事就是 `fixFileCheckHeader(DataFile, Fmt_c)`——校验文件头格式与实体期望一致，**这是 Python 与 HDL 对齐的第一道闸门**；随后循环用 `fixFileReadSample` 读样本。

- [olo_fix_sim_stimuli.vhd:96-121](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_stimuli.vhd#L96-L121)：施加数据 + 握手。master 分支拉高 `Valid`、等 `Ready`，并用 `ieee.math_real` 的 `uniform()` 产生随机数决定是否停顿；slave 分支则等「`Ready=1` 且 `Valid=1`」。

- 两个文件辅助函数都在 `olo_fix_pkg` 里：[olo_fix_pkg.vhd:237-249](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L237-L249) 的 `fixFileCheckHeader` 读首行、解析格式、`assert` 比对；[olo_fix_pkg.vhd:251-269](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L251-L269) 的 `fixFileReadSample` 用 `hread` 读一行 hex（按 4 位对齐），再截取恰好 `cl_fix_width(fmt)` 位返回。

#### 4.2.4 代码实践

**目标**：通过 stimuli 的自测测试台，看清 master/slave 的接线差异。

**步骤**：

1. 阅读 [test/fix/olo_fix_sim_stimuli/olo_fix_sim_stimuli_tb.vhd:116-145](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_sim_stimuli/olo_fix_sim_stimuli_tb.vhd#L116-L145)。注意它实例化了**两个** stimuli：`i_dut_timing_master`（`IsTimingMaster_g` 取默认 `true`）和 `i_dut_timing_slave`（`IsTimingMaster_g => false`），二者**共享同一个 `Valid` 与 `Ready`**，只是各自的 `Data` 输出（`Data` / `DataSlave`）不同。
2. 在 `sim/` 下运行这个自测（GHDL）：

   ```bash
   cd sim
   python3 run.py --ghdl -v olo_fix_sim_stimuli_tb
   ```

**需要观察的现象**：两个 stimuli 输出的 `Data` 与 `DataSlave` 在每个握手沿上完全相同——因为 slave 跟随 master 的 `Valid` 同步推进。

**预期结果**：测试通过（两个 checker VC 都不报 mismatch）。若本地未装 GHDL，则改为「源码阅读型实践」：在 stimuli 源码里指出 master 与 slave 两个分支分别在哪几行（**待本地验证**运行结果）。

#### 4.2.5 小练习与答案

**Q1**：如果 DUT 有 3 路输入，应该配几个 timing-master stimuli、几个 slave？

**答**：1 个 master、2 个 slave。每个方向只能有一个 master 驱动 `Valid`，其余 slave 共享该 `Valid` 以保证同拍配对。

**Q2**：`StallProbability_g` 在 slave 模式下生效吗？

**答**：不生效。源码里停顿逻辑只在 `IsTimingMaster_g=true` 的分支内；slave 完全跟随外部 `Valid`/`Ready`，自己不制造停顿。

---

### 4.3 sim_checker 比对

#### 4.3.1 概念说明

`olo_fix_sim_checker` 是「DUT 输出 → 文件」的裁判：它逐行读 `.fix` 文件作为**期望值**，每当 DUT 产出一个有效输出（握手成功），就拿期望值和 DUT 实际 `Data` 比对，不一致就 `assert ... severity error`。它同样只检查一次。

它的 timing-master/slave 角色与 stimuli **对偶**：

- 对 checker，timing-master 是驱动 **`Ready`**（拉低 `Ready` 制造反压，模拟下游慢消费者）。
- timing-slave 则跟随别人驱动的 `Ready`/`Valid`。

> 术语对照：stimuli 的 master 管 `Valid`（我何时给数据），checker 的 master 管 `Ready`（我何时收数据）。多个 checker 比对多路输出时，同样只设一个 master、其余 slave。

另一个要点：checker 里的 `assert` 比对包在 `-- pragma translate_off` … `-- pragma translate_on` 里，确保这段只用于仿真的代码不会进综合（见 [olo_fix_sim_checker.vhd:117-124](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L117-L124)）。

#### 4.3.2 核心流程

```text
初始化 Ready（master=>0 / slave=>'Z'）
打开文件；fixFileCheckHeader(...)
while 文件未读完:
    if master:
        以概率 StallProbability_g 随机拉低 Ready 制造停顿
        Ready <= '1'
        wait until rising_edge(Clk) and Valid='1'
    else: # slave
        wait until rising_edge(Clk) and Ready='1' and Valid='1'
    expected = fixFileReadSample(...)          # 握手成功后再读下一个期望
    assert Data = expected  else severity error # 逐位比对
关闭文件；wait;
```

注意与 stimuli 的顺序差异：checker 是**先等握手（一个真实输出到达）、再读期望**。因为文件期望与 DUT 输出都是按序排列的，只要「一次握手消费一个」的节奏一致，二者就能逐样本对齐。

#### 4.3.3 源码精读

- [olo_fix_sim_checker.vhd:36-51](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L36-L51)：实体声明。端口与 stimuli 对偶：`Valid`/`Data` 都是 `in`（被动接收 DUT 输出），`Ready` 是 `inout`（master 时输出、slave 时输入）。

- [olo_fix_sim_checker.vhd:82-85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L82-L85)：同样先 `fixFileCheckHeader` 校验格式头，并记下行号 `LineNumber_v`（从 2 开始，第 1 行是头），便于报错时指出是文件第几行不匹配。

- [olo_fix_sim_checker.vhd:88-111](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L88-L111)：握手循环。master 分支用 `uniform()` 决定是否拉低 `Ready` 制造停顿（停顿时等的是 `Valid='1'`，模拟下游不收但数据仍在），随后 `Ready<='1'` 并等 `Valid='1'`。

- [olo_fix_sim_checker.vhd:113-125](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sim_checker.vhd#L113-L125)：读期望 + 比对核心：

  ```vhdl
  DataSlv_v := fixFileReadSample(DataFile, Fmt_c);
  -- pragma translate_off
  assert Data = DataSlv_v
      report errorMessage(EntityName_c, "Data mismatch: expected " & ...)
      severity error;
  -- pragma translate_on
  ```

  报错信息里带文件路径与行号，定位极快。

#### 4.3.4 代码实践

**目标**：人为制造一次比对失败，观察 checker 如何报错。

**步骤**：

1. 阅读 [test/fix/olo_fix_sim_checker/olo_fix_sim_checker_tb.vhd:117-144](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_sim_checker/olo_fix_sim_checker_tb.vhd#L117-L144)：它把 `olo_fix_sim_checker` 本身当 DUT，用 stimuli VC 回放同一个 `Data.fix`，于是「期望 == 实际」，应当通过。
2. 想观察失败：复制该 TB，把喂给 DUT 输入的文件与 checker 读取的文件改成**内容不同**的两个文件（例如第二个样本改一个 hex 位），再运行。

**需要观察的现象**：仿真日志里出现 `Data mismatch: expected ..., got ... - file ... - line 3` 这类带行号的 `assert` 报错（`severity error`）。

**预期结果**：错位的样本所在行号会被精确报出。这是 checker 相比「自己写比对进程」最大的工程红利（**待本地验证**）。

#### 4.3.5 小练习与答案

**Q1**：checker 的 `assert` 为什么必须包在 `pragma translate_off` 里？

**答**：checker 是仿真专用实体，`to_string`、`assert`、文件 IO 都不可综合。`pragma translate_off/on` 告诉综合工具跳过这段，避免综合报错或推断出多余硬件。

**Q2**：checker 在 master 模式下停顿时，等的是 `Valid='1'` 还是 `Ready='1'`？

**答**：等 `Valid='1'`。停顿是 checker 主动拉低 `Ready`（下游不收），此时它仍需观察 DUT 是否在持续给 `Valid`，故停在 `rising_edge(Clk) and Valid='1'` 上计数停顿周期。

---

### 4.4 位真协仿真流程（端到端 + pre_config 时机）

#### 4.4.1 概念说明

把前三块积木拼起来，就是一条完整的位真验证流水线：

```text
  Python (en_cl_fix 黄金模型)          VHDL 仿真
  ┌──────────────────────────┐        ┌──────────────────────────┐
  │ 算出激励 A/B 与期望 Result │        │ stimuli 回放 A/B -> DUT   │
  │ olo_fix_cosim 写成 .fix   │ ──文件──▶ │ DUT(olo_fix_mult) 出输出 │
  │  A.fix B.fix Result.fix   │        │ checker 用 Result.fix 比对│
  └──────────────────────────┘        └──────────────────────────┘
        ▲                                    │
        │   pre_config: 仿真前先跑 Python      │
        └────────────────────────────────────┘
```

这里有两个工程关键点：

1. **文件必须在仿真启动前就存在**。VUnit 的 `pre_config` 钩子正是为这个时机设计的：它在每个测试用例**开始仿真之前**被调用，用来按当前用例的 generics 生成对应的 `.fix` 文件。这与 u8-l4 讲过的「`codegen_generate()` 必须在 VUnit 扫描文件之前跑」是同一类时序约束。
2. **每个测试用例的文件落到各自的输出目录**。VUnit 给每个用例一个独立的 `output_path`，Python 把文件写进去，TB 用 `output_path(runner_cfg) & "A.fix"` 读出来，互不干扰。

#### 4.4.2 核心流程

每个被协仿真的实体在自己的测试目录下有一个 `cosim.py`，约定函数签名：

```python
def cosim(output_path: str = None, generics: dict = None, cosim_mode: bool = True):
    # 1. 解析 generics（格式、舍入、饱和……）
    # 2. 用 en_cl_fix / olo_fix_* Python 模型算出激励与期望
    # 3. if cosim_mode: writer = olo_fix_cosim(output_path); 写 A/B/Result 文件
    return True
```

在 `sim/test_configs/olo_fix.py` 里，把这个 `cosim` 函数挂成某个 `named_config` 的 `pre_config`：

```python
cosim = olo_fix_mult.cosim.cosim
named_config(tb, default_generics | {'Round_g': Round, 'Saturate_g': Sat},
             pre_config=cosim)
```

于是 VUnit 对每个配置：先调 `cosim(output_path=<用例输出目录>, generics=<本配置泛型>, cosim_mode=True)` 生成文件 → 再启动仿真 → TB 读文件跑 stimuli/checker。

#### 4.4.3 源码精读

- 以 `olo_fix_mult` 为例。Python 模型侧 [test/fix/olo_fix_mult/cosim.py:19-55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py#L19-L55)：解析 `AFmt_g/BFmt_g/ResultFmt_g/Round_g/Saturate_g`，用 `cl_fix_from_real` 造激励，用 `olo_fix_mult(...).process(in_a, in_b)` 这个**位真 Python 模型**算期望 `out_data`，最后写 `A.fix`/`B.fix`/`Result.fix`（[L50-L54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/cosim.py#L50-L54)）。

- 注册侧 [sim/test_configs/olo_fix.py:162-195](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L162-L195)：把 `olo_fix_mult_tb` 在多种格式/舍入/饱和/寄存器组合下注册，每一组都用 `pre_config=cosim`。注意 add/sub/mult/addsub 共用同一段循环代码。

- TB 侧 [test/fix/olo_fix_mult/olo_fix_mult_tb.vhd:79-81](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L79-L81)：文件路径用 `output_path(runner_cfg) & "A.fix"` 拼出，正好读 `pre_config` 写进来的文件。

  > 说明：`olo_fix_mult_tb` 实际用的是 stimuli/checker 的 **VUnit VC 变体**（`olo_test_fix_stimuli_vc` / `olo_test_fix_checker_vc`，见 [L174-L209](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L174-L209)），它把「一次性回放」升级为「可用过程调用多次回放、每次单独配停顿」。但底层读的是**同一种 `.fix` 文件格式**——本讲的 stimuli/checker 是更原始、更易理解的版本，二者文件格式完全兼容。

#### 4.4.4 代码实践

**目标**：亲眼看到 `pre_config` 在仿真前生成了文件。

**步骤**：

1. 只跑 `olo_fix_mult_tb` 的一个配置，并保留其输出目录：

   ```bash
   cd sim
   python3 run.py --ghdl -v -p olo_fix_mult_tb.*NonSymPos_s-Sat_s*
   ```

2. 到该用例的输出目录（VUnit 通常在 `vunit_out/.../` 下）查找 `A.fix`、`B.fix`、`Result.fix`。

**需要观察的现象**：三个文件在仿真**开始之前**就已生成；每个文件第一行是对应格式串，后续是十六进制样本。

**预期结果**：文件存在且格式正确；测试通过表示 DUT 输出与 Python 期望逐位一致。若找不到输出目录，可在 `run.py` 加 `--output-path` 指定（**待本地验证**具体路径）。

#### 4.4.5 小练习与答案

**Q1**：如果把 `cosim` 注册成 `post_config` 而不是 `pre_config`，会发生什么？

**答**：`post_config` 在仿真**之后**才跑，文件尚未生成时 TB 就已经开始读文件，会因找不到文件或读到空而失败。必须用 `pre_config` 保证「先生成、再仿真」。

**Q2**：为什么 `olo_fix_mult_tb` 里 A、B 两路 stimuli 可以共用同一个 `In_Valid`？

**答**：A 路 stimuli 是 timing-master 驱动 `In_Valid`，B 路是 timing-slave 跟随同一个 `In_Valid`，二者同拍推进，因此每个握手沿上 A、B 给出的是同一组配对样本。这正是 master/slave 机制的用途。

---

## 5. 综合实践

**任务**：仿照 `olo_fix_mult` 的官方测试，用本讲的三个积木（`olo_fix_cosim` + `olo_fix_sim_stimuli` + `olo_fix_sim_checker`）搭一个最小协仿真，验证 `olo_fix_mult` 输出与 Python 位真模型一致。

### 步骤 1：用 Python 生成激励与期望

直接复用现成的位真模型（`olo_fix_mult` 的 Python 类，参见 `test/fix/olo_fix_mult/cosim.py`）。把它跑一遍，把三个文件写到一个目录：

```python
# generate.py （示例代码）
import sys, os
sys.path.append(os.path.abspath("src/fix/python"))
from olo_fix import olo_fix_cosim, olo_fix_mult
from en_cl_fix_pkg import *
import numpy as np

AFmt, BFmt, RFmt = FixFormat(1,4,4), FixFormat(0,4,4), FixFormat(1,10,4)
in_a = cl_fix_from_real(np.linspace(-5, 5, 50), AFmt)
in_b = cl_fix_from_real(np.linspace( 0, 3, 50), BFmt)
out  = olo_fix_mult(AFmt, BFmt, RFmt, NonSymPos_s, Sat_s).process(in_a, in_b)

w = olo_fix_cosim("./cosim")
w.write_cosim_file(in_a, AFmt, "A.fix")
w.write_cosim_file(in_b, BFmt, "B.fix")
w.write_cosim_file(out,  RFmt, "Result.fix")
```

> 注意：`olo_fix_mult` 的 Python 模型接口以 `test/fix/olo_fix_mult/cosim.py` 为准；若你本地版本签名不同，请以本地源码为准（**待本地验证**）。

### 步骤 2：写一个最小测试台（示例代码）

`olo_fix_mult` 是无反压的直通实体（端口只有 `In_Valid/In_A/In_B/Out_Valid/Out_Result`，无 `Ready`）。因此：A 路 stimuli 当 timing-master 驱动 `In_Valid`，B 路 stimuli 当 timing-slave 跟随同一 `In_Valid`，checker 当 timing-master（`StallProbability_g=0.0`，其 `Ready` 输出悬空即可）。

```vhdl
-- cosim_mult_tb.vhd （示例代码，仅示意接线，省略 VUnit 框架与复位细节）
library ieee; use ieee.std_logic_1164.all;
library olo;  use olo.en_cl_fix_pkg.all; use olo.olo_fix_pkg.all;

entity cosim_mult_tb is end;
architecture sim of cosim_mult_tb is
  signal Clk : std_logic := '0';
  signal Rst : std_logic := '1';
  signal In_Valid : std_logic;
  signal In_A : std_logic_vector(fixFmtWidthFromString("(1,4,4)")-1 downto 0);
  signal In_B : std_logic_vector(fixFmtWidthFromString("(0,4,4)")-1 downto 0);
  signal Out_Valid : std_logic;
  signal Out_Result : std_logic_vector(fixFmtWidthFromString("(1,10,4)")-1 downto 0);
begin
  Clk <= not Clk after 5 ns;

  -- A 路：timing master，驱动 In_Valid
  stim_a : entity olo.olo_fix_sim_stimuli
    generic map (FilePath_g => "./cosim/A.fix", Fmt_g => "(1,4,4)")
    port map (Clk=>Clk, Rst=>Rst, Ready=>'1', Valid=>In_Valid, Data=>In_A);

  -- B 路：timing slave，跟随 In_Valid
  stim_b : entity olo.olo_fix_sim_stimuli
    generic map (FilePath_g => "./cosim/B.fix", Fmt_g => "(0,4,4)", IsTimingMaster_g=>false)
    port map (Clk=>Clk, Rst=>Rst, Ready=>'1', Valid=>In_Valid, Data=>In_B);

  -- DUT
  dut : entity olo.olo_fix_mult
    generic map (AFmt_g=>"(1,4,4)", BFmt_g=>"(0,4,4)", ResultFmt_g=>"(1,10,4)",
                 Round_g=>"NonSymPos_s", Saturate_g=>"Sat_s")
    port map (Clk=>Clk, Rst=>Rst, In_Valid=>In_Valid, In_A=>In_A, In_B=>In_B,
              Out_Valid=>Out_Valid, Out_Result=>Out_Result);

  -- checker：timing master，Ready 悬空，逐拍比对
  chk : entity olo.olo_fix_sim_checker
    generic map (FilePath_g => "./cosim/Result.fix", Fmt_g => "(1,10,4)")
    port map (Clk=>Clk, Valid=>Out_Valid, Data=>Out_Result);  -- Ready 不连
end;
```

### 步骤 3：运行并观察

1. `python3 generate.py` 生成三个文件；`cat ./cosim/Result.fix` 确认第一行是 `(1,10,4)`。
2. 用你顺手的仿真器（GHDL/VUnit）编译并跑这个 TB。

**需要观察的现象**：

- 仿真期间**没有** `Data mismatch` 报错，进程正常跑到 stimuli/checker 各自 `wait;` 结束。
- 即使给 A 路 stimuli 加 `StallProbability_g => 0.3`（制造随机停顿），checker 仍不应报错——因为握手节奏变了，但「一次握手消费一个样本」的配对关系不变，DUT 输出顺序与期望一致。

**预期结果**：全部通过，证明 `olo_fix_mult` 的 HDL 实现与 Python 位真模型在所有生成的样本上逐位相等。若报 mismatch，最常见原因是 TB 里 `Fmt_g` 与文件头格式不一致（会先被 `fixFileCheckHeader` 拦下）或舍入/饱和模式与 Python 模型不一致（**待本地验证**）。

## 6. 本讲小结

- 协仿真用「Python 黄金模型 + 文件中介 + HDL 逐拍比对」实现**位真**验证，能抓住舍入/饱和/位宽收剑造成的末位错误。
- `olo_fix_cosim.write_cosim_file()` 把定点数组写成 `.fix` 文件：第 1 行格式串 `(S,I,F)`，后续每行一个定宽大写十六进制（负数已转无符号补码）。
- `olo_fix_sim_stimuli` 回放文件到 DUT 输入，`olo_fix_sim_checker` 把文件当期望比对 DUT 输出；二者都靠 `fixFileCheckHeader` 先校验格式，再以 AXI-S 握手逐样本推进。
- **timing-master / timing-slave** 机制解决多路同拍配对：每个方向一个 master（stimuli 管 `Valid`、checker 管 `Ready`），其余 slave 共享握手信号。
- 端到端时序的关键是 VUnit **`pre_config`**：在每个用例仿真前先跑 Python 生成 `.fix` 到 `output_path`，TB 再读回来——文件必须「先生成、再仿真」。
- 项目用同一套 `.fix` 格式贯穿原始实体（stimuli/checker）与 VUnit VC 变体（`olo_test_fix_*_vc`），后者支持多次回放、每次单独配停顿。

## 7. 下一步学习建议

- **进入第 9 单元（高级 DSP）**：CORDIC、CIC、FIR 等复杂实体正是靠本讲的协仿真保证位真——它们的 `cosim.py` 比乘法器更复杂，建议对照 `test/fix/olo_fix_cordic_vect/cosim.py` 看协仿真如何描述迭代算法。
- **深入验证工程化（u10-l1/u10-l2）**：本讲的 stimuli/checker 属于「验证组件（VC）」。第 10 单元会系统讲解 VUnit VC 的命名约定、`run.py` 的 `pre_config`/coverage 选项，以及 `test_configs` 如何组织大量 generic 组合。
- **动手扩展**：给某个还没被协仿真覆盖的小实体（如 `olo_fix_abs`）照着本讲流程补一个 `cosim.py` 与最小 TB，跑通后你就完整掌握了一条位真验证链路。
