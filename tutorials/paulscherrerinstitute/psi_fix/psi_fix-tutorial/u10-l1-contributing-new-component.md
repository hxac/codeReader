# 贡献新组件的完整流程

## 1. 本讲目标

本讲是 psi_fix 学习手册的「收口」讲义。前面九个单元我们已经分别读懂了单个组件——它的 VHDL 实现、Python 位真模型、自检测试台、协同仿真和命名规则。本讲不再讲某一个具体算法，而是回答一个工程问题：

> **如果我要往 psi_fix 库里新增一个组件，从写第一行代码到合并入库，到底要交哪些「作业」？**

学完本讲，读者应该能够：

1. 说清楚贡献一个新组件必须提交的「五件套」，以及它们之间的依赖顺序。
2. 理解为什么「位真 Python 模型」和「自检测试台」是不可妥协的入库门槛，违反它们组件就不会被接受。
3. 看懂 `sim/config.tcl` 的三段式声明（源码 / 测试台 / TB Run），能把自己写的新测试台正确挂进回归脚本，并验证它真的被跑到了。

本讲不再重复前面讲过的算法细节（CORDIC 迭代、FIR 架构、CIC 位增长等），而是把零散的知识点收拢成一份「贡献清单」。

---

## 2. 前置知识

本讲假设读者已经掌握以下概念（均在前面单元建立，这里只做一句话回顾）：

- **位真双模型 (bittrue dual model)**：每个可综合 VHDL 组件必须配套一个逐位一致的 Python 模型，作为「黄金参考 (golden reference)」，由自检测试台逐位比对 VHDL 输出，不一致就报错。详见 u1-l1、u3-l1。
- **定点格式 [s,i,f] 与位增长**：总位宽 `W=s+i+f`；加减整数位 +1，有符号相乘整数位相加后再 +1。详见 u1-l4、u2-l1。
- **两段式编码风格 (two-process)**：组合进程 `p_comb`（算下一拍、写 `r_next`）+ 时序进程 `p_seq`（仅打拍 `r <= r_next` + 同步复位），用 record 封装流水线寄存器。详见 u3-l3。
- **协同仿真闭环**：Python 模型 → `psi_fix_get_bits_as_int` 把定点值写成整数位模式文本 → 测试台 `ApplyTextfileContent` 喂激励、`CheckTextfileContent` 比对输出 → 不符打印 `###ERROR###`。详见 u3-l2。
- **PsiSim 回归框架**：`config.tcl` 用 `add_sources -tag` 声明编译清单、`create_tb_run` / `tb_run_add_arguments` / `add_tb_run` 声明测试台及其参数矩阵、`tb_run_add_pre_script` 在运行前钩挂数据生成脚本。详见 u1-l3。
- **代码生成式组件**：当一族组件共享同一个计算内核、只在「表」上不同（如 sin/sqrt/inv/gaussify），用 Python 代码生成器从数学函数一次性算出表 + 生成 `.vhd` + 生成测试台。详见 u8-l2。

如果上述任何一项让你感到陌生，建议先回看对应单元再继续。

---

## 3. 本讲源码地图

本讲引用的关键文件如下：

| 文件 | 作用 |
|:--|:--|
| `doc/files/introduction.md` | 库的总入口文档，其中「Contribute to PSI VHDL Libraries」一节列出贡献硬性规则。 |
| `doc/files/design_flow.md` | 七阶段设计流程，定义了「先设计文档、先 Python 后 VHDL」的工作顺序。 |
| `README.md` | 顶层说明，明确「无位真模型的组件不应进库」「一个 .vhd 一个实体」等纪律。 |
| `model/psi_fix_mov_avg.py` | 手写型位真模型的样板（多数组件采用此风格）。 |
| `model/psi_fix_lin_approx.py` | 代码生成型模型的样板（组件族采用此风格）。 |
| `testbench/psi_fix_mov_avg_tb/Scripts/preScript.py` | 协同仿真数据生成脚本样板。 |
| `testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd` | 自检测试台结构样板（DUT + stim + check）。 |
| `sim/config.tcl` | 回归脚本，新测试台必须在这里注册。 |
| `doc/files/psi_fix_mov_avg.md` | 单个组件的文档样板。 |
| `doc/README.md` | 文档索引表，新组件文档要在这里加一行链接。 |

---

## 4. 核心概念与源码讲解

整个 psi_fix 库把「贡献一个新组件」固化成了固定的五件套，它们的产出顺序与依赖关系如下：

```text
(0) 设计文档/活文档  ── 不写代码，先定格式与资源
        │
        ▼
(1) Python 位真模型   ── 黄金参考，先于 VHDL 完成
        │
        ▼
(2) VHDL 实现         ── 两段式，对齐模型的每一个量化点
        │
(3) 自检测试台        ── DUT + stim + check，preScript 协同仿真
        │  （二者配套提交）
        ▼
(4) 文档 + 索引        ── doc/files/<comp>.md + doc/README.md 一行
        │
        ▼
(5) 回归注册           ── sim/config.tcl 三段声明
```

本节按「Python 模型 → VHDL 与测试台 → 文档与回归注册」三个最小模块展开，分别对应五件套中的 (1)、(2)+(3)、(4)+(5)。

### 4.1 Python 模型：位真黄金参考

#### 4.1.1 概念说明

psi_fix 库有一条不可妥协的纪律：**任何 psi_fix 组件都必须配套一个位真 Python 模型，否则不会被接受**。这条规则同时写在两处：

- 顶层说明里指出，没有位真模型的代码「最好另开一个非位真库」，不要塞进 psi_fix：[README.md:27-29](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L27-L29)。
- 贡献规则里把它列为强制项：[introduction.md:86-87](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L86-L87)。

Python 模型的角色是**黄金参考**：它不是理想浮点算法，而是主动约束到硬件的定点格式（含舍入、饱和、位增长），由 u3-l1 介绍的「先 Python 后 VHDL」箭头单向原则——出现偏差时改 VHDL，而不是改 Python 凑结果。

psi_fix 的 Python 模型在工程上有**两种风格**：

1. **手写型**：一个组件一个 `.py` 文件、一个类，构造函数里推导中间格式、`Process()` 方法跑数据流。绝大多数组件（mov_avg、cordic、fir、cic、dds……）都是这种。样板是 `psi_fix_mov_avg.py`。
2. **代码生成型**：一族组件共享同一个计算内核、只在「表」上不同，于是用一个「模型 + 代码生成器二合一」的脚本，从一个数学函数一次性算出表、生成 `.vhd` 实体、生成测试台。sin/sqrt/inv/gaussify 这一族就是这样做的。样板是 `psi_fix_lin_approx.py`。

**默认走手写型**；只有当你的新组件属于「同内核、不同表」的族时，才考虑代码生成（u8-l2 已详细讲过）。本节重点讲手写型。

#### 4.1.2 核心流程

一个手写型位真模型类通常长这样：

```text
class psi_fix_<name>:
    # —— 构造函数：把"编译期就该定下来"的东西算好 ——
    def __init__(self, inFmt, outFmt, <参数>, rnd, sat):
        # 1. 存输入/输出格式与配置
        # 2. 用位增长规则推导所有中间格式（与 VHDL 用同一组算式）
        # 3. 量化硬件常量（如增益系数、抽头）

    # —— 数据处理：对齐硬件数据流，每一步都过 psi_fix_* ——
    def Process(self, inData) -> np.ndarray:
        # 用 psi_fix_from_real / resize / add / sub / mult / shift ...
        # 镜像硬件的每一个量化点，宁可主动降精度贴硬件
```

三个关键纪律：

- **中间格式与 VHDL 同源**：构造函数里推导出的 `diffFmt`、`sumFmt` 等中间格式，VHDL 侧用**同一组算式**推导，这是位真契约的硬约束。
- **库级默认 trunc/wrap，组件级默认 round/sat**：建模时必须显式对齐——组件层的模型应使用 round/sat（偏安全），与 psi_fix_pkg 库函数的默认值相反（详见 u2-l3）。
- **入口加 `psi_fix_from_real`，量化输入**：`Process` 第一步几乎总是把浮点输入量化到 `inFmt`，否则等于在理想浮点上算，失去位真意义。

#### 4.1.3 源码精读

以 `psi_fix_mov_avg` 模型为样板。

类的常量区定义了三档增益校正模式，供构造函数做参数校验：[psi_fix_mov_avg.py:23-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L23-L25)。

构造函数在「存配置」之后，用位增长规则把所有中间格式算出来，并把增益系数量化成硬件常量：[psi_fix_mov_avg.py:49-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L49-L61)。注意 `gcCoefFmt = (0,1,16)` 与封顶 25 位的 `gcInFmt` 是为了让 EXACT 校正的乘法恰好落入单个 DSP48 slice——这正是 u4-l1 讲过的资源-精度权衡，VHDL 侧用同一组算式复刻。

`Process()` 方法严格镜像硬件数据流：量化输入 → 差分 → 累加 → 三档增益校正，每一步都过 `psi_fix_*` 并显式指定 round/sat：[psi_fix_mov_avg.py:66-91](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L66-L91)。注意累加那行注释强调「既不取整也不饱和，故位真」——这呼应 u3-l1 的原则：内部只在必要时量化。

代码生成型模型的结构则不同：它把「配置」集中在一个 `CONFIGS` 类里，每个配置对应一个函数组件：[psi_fix_lin_approx.py:78-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L78-L114)；而 `GenerateEntity()` 读取模板、用 `str.replace` 替换占位符、把量化后的表内容写进 `.vhd`：[psi_fix_lin_approx.py:251-297](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L251-L297)。文件末尾的 `__main__` 块遍历所有配置、一次性重生成全部实体与测试台：[psi_fix_lin_approx.py:365-370](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L365-L370)。改精度只动配置重跑脚本、手写 RTL 零修改——这是生成式组件族的维护方式（u8-l2）。

#### 4.1.4 代码实践

**实践目标**：通过阅读一个真实模型，理解「构造算中间格式、Process 跑数据流」的标准结构。

**操作步骤**：

1. 打开 [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py)。
2. 在构造函数（30–61 行）里找到 `diffFmt`、`sumFmt`、`gcInFmt` 三个中间格式，分别写出它们各自的位增长算式。
3. 在 `Process()`（66–91 行）里数一数一共调用了几次 `psi_fix_*` 函数，每次的 round/sat 参数是什么。

**需要观察的现象**：

- `diffFmt` 比 `inFmt` 多 1 个整数位（减法 +1），`sumFmt` 比 `inFmt` 多 `additionalBits=⌈log2(taps)⌉` 个整数位（累加）。
- 内部步骤（差分、累加）用 `trunc/wrap`，唯独末端输出到 `outFmt` 时才用模型保存的 `round/sat`。

**预期结果**：你会看到模型把「内部不溢出、末端才量化」的设计意图完整复刻，这正是位真的前提。

> 待本地验证：若本地已装 NumPy/SciPy 且按 u1-l2 的目录结构并排摆放了 `en_cl_fix`，可在 `model/` 目录下 `python3 -c "from psi_fix_mov_avg import psi_fix_mov_avg; ..."` 实例化并跑一段数据；否则按上面纯阅读方式完成即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Process()` 的第一行必须是 `psi_fix_from_real(inData, self.inFmt)`，而不是直接用浮点 `inData` 计算？

> **答**：因为位真模型要镜像硬件。硬件拿到的是已经量化到 `inFmt` 的输入，若模型用未量化的浮点算，就等于在一个比硬件精度更高的起点上计算，结果不可能逐位一致。必须先把输入量化，再走与硬件完全相同的定点数据流。

**练习 2**：在什么情况下你应该选择「代码生成型」而非「手写型」模型？

> **答**：当你要贡献的不是单个组件，而是一族共享同一计算内核、只在数据表上不同的组件（如同一套分段线性近似内核驱动 sin/sqrt/inv/gaussify）。此时手写多个几乎相同的 `.vhd` 是重复劳动，改用代码生成器从一个 `CONFIGS` 配置批量生成，改精度只动配置、手写 RTL 零修改（详见 u8-l2）。

---

### 4.2 VHDL 与测试台：位真验证闭环

#### 4.2.1 概念说明

Python 模型完成后，五件套的第二、三件是配套提交的：**VHDL 实现** + **自检测试台**。贡献规则把「自检测试台」也列为强制项，并要求测试台覆盖全部功能、跑完自动停机、出错时以 `###ERROR###` 开头报告：[introduction.md:88-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L88-L94)。对 psi_fix 而言，测试台还必须**调用 Python 模型并逐位比对 VHDL 与 Python**。

这对应设计流程的 Phase 5「VHDL 实现与协同仿真」：[design_flow.md:37-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L37-L38)。

psi_fix 的测试台不是手写从零起的，而是由一个外部工具 `TbGen.py`（见测试台头部注释）按统一模板生成骨架，再在 `stim` / `check` 两个进程里填业务逻辑。这就是为什么全库测试台长得高度同构。

#### 4.2.2 核心流程

一个完整自检测试台的运行闭环：

```text
[编译前/运行前]  config.tcl 的 tb_run_add_pre_script 调一次 preScript.py
                       │
                       ▼
            preScript 实例化 Python 位真模型（黄金参考）
            用「最坏情况 + 固定随机种子」刺激跑 Process()
            经 psi_fix_get_bits_as_int 把输入/期望输出写成
            Data/input.txt 与 Data/output_*.txt（有符号整数=位模式）
                       │
                       ▼
[仿真运行]    VHDL 测试台：
   - p_stim  : ApplyTextfileContent 按 duty_cycle 节拍把 input.txt 重放喂 DUT
   - p_check : 每个 OutVld 把 to_integer(signed(OutData)) 与
               output_*.txt 逐行比对，不符打印 ###ERROR###
   - p_tb_control : 所有 ProcessDone 位凑齐后停机
                       │
                       ▼
[CI 判定]     ciFlow.py 扫描 Transcript，###ERROR### 即功能失败(exit -1)
```

这套闭环的数学基础是 u3-l2 讲过的「整数即位模式」：同一组二进制位按有符号整数解读相等 ⟺ 每一位都相等，于是逐位比对可简化为整数比对。

#### 4.2.3 源码精读

以 `psi_fix_mov_avg_tb` 为样板。

**DUT 例化**：测试台把待测组件按 generics + 端口连好，其中端口严格遵循 `dat_i/dat_o/vld_i/vld_o` 与 `_i/_o` 命名后缀：[psi_fix_mov_avg_tb.vhd:73-88](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L73-L88)。

**stim 进程**：调用 `psi_tb` 的 `ApplyTextfileContent`，把 `input.txt` 按 `duty_cycle_g` 节拍重放成时序喂 DUT，`IgnoreLines => 1` 跳过文件头：[psi_fix_mov_avg_tb.vhd:134-151](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L134-L151)。

**check 进程**：先把 `OutData` 转成有符号整数，再用 `CheckTextfileContent` 与 `output_<gain_corr>.txt` 逐行比对，文件名通过 `to_lower(gain_corr_g)` 自动对齐 generic，无需 `if/else`：[psi_fix_mov_avg_tb.vhd:155-171](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L155-L171)。

**preScript.py**：这是闭环的 Python 半边。它先把 `model` 目录加进搜索路径，从而能 `import psi_fix_pkg` 和组件模型：[preScript.py:6-13](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L6-L13)。然后构造「全幅跳变 + `np.random.seed(0)` 固定随机段 + 零段」的最坏情况刺激，对每一档增益校正实例化模型并跑 `Process()`：[preScript.py:29-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L29-L45)。最后用 `psi_fix_get_bits_as_int` 把定点输入/输出转成有符号整数位模式，用 `np.savetxt(fmt="%i")` 落盘成 `Data/*.txt`：[preScript.py:58-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L58-L60)。

> 关于 preScript 的「数据生成型 vs 代码生成型」两种类别，以及为何代码生成型（如 `lut_gen`）必须在编译前更早运行，详见 u1-l3。本节聚焦最常见的「数据生成型」preScript。

#### 4.2.4 代码实践

**实践目标**：跟踪一条「Python 浮点结果 → 整数文本 → VHDL 比对」的完整路径，确认你理解协同仿真闭环。

**操作步骤**：

1. 打开 [preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py)，定位第 60 行 `np.savetxt(... output_{}.txt ..., psi_fix_get_bits_as_int(result[gc], outFmt) ...)`。
2. 回答：`psi_fix_get_bits_as_int(result[gc], outFmt)` 把一个定点数变成了什么？为什么写成「整数」而不是「小数」？
3. 打开 [psi_fix_mov_avg_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd)，找到第 154 行 `SigOut(0) <= to_integer(signed(OutData))` 和第 165 行的 `CheckTextfileContent(... "/output_" & to_lower(gain_corr_g) & ".txt" ...)`，确认 VHDL 侧也是用「有符号整数」与同一份文本比对。

**需要观察的现象**：

- preScript 写出的文件名 `output_<gc>.txt`（`gc` 取 `.lower()`）与测试台 `to_lower(gain_corr_g)` 拼出的文件名**完全一致**，于是同一个测试台换不同 generic（NONE/EXACT/ROUGH）就能复用，无需 `if/else` 分支。
- 比对双方都是「位模式的有符号整数」，所以相等即逐位相等。

**预期结果**：你能向别人讲清楚——为什么把一个 `[1,1,12]` 的定点数 `-0.5` 写成整数 `-2048` 后，VHDL 把同样的位模式 `signed(OutData)` 也读成 `-2048`，于是整数比对等价于逐位比对。

> 待本地验证：若本地有仿真器，可参考 u1-l3 在 `sim/` 下 `source ./run.tcl`，观察 mov_avg 三档增益各跑一轮、无 `###ERROR###`；否则按上面纯阅读方式完成。

#### 4.2.5 小练习与答案

**练习 1**：贡献规则要求测试台「出错时报告以 `###ERROR###` 开头」。这条约定的下游消费者是谁？

> **答**：是 CI 脚本 `ciFlow.py`（详见 u10-l2）。它扫描仿真器输出的 `Transcript.transcript`，把 `###ERROR###` 字符串当作**功能失败**的唯一判据（`exit -1`）。所以测试台里任何 `report ... severity error` 若不以该前缀开头，CI 不会判它失败，缺陷会被悄悄放过。

**练习 2**：preScript.py 为什么用 `np.random.seed(0)` 固定随机种子，而不是每次真随机？

> **答**：为了**确定性 / 可复现**。回归测试要求每次跑出完全一样的 `Data/*.txt`，这样失败可重现、结果可 diff。种子固定后，preScript 只要跑一次、生成的多份输出文件可供测试台各轮 generic 复用；真随机会导致每次比对基线都变，无法定位是 VHDL 回归还是随机波动（详见 u3-l1「确定性可复现方法论」）。

---

### 4.3 文档与回归注册：让组件可被发现、可被自动测试

#### 4.3.1 概念说明

五件套的最后两件，决定了一个组件「能不能被别人找到、能不能被 CI 自动跑到」。

**文档（第四件）**：贡献规则要求「通过 md 文件扩展文档，并把链接登记到 README」：[introduction.md:95-96](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L95-L96)。这里的 README 指的是文档索引 `doc/README.md`（因为该规则写在 `doc/files/introduction.md` 里，相对路径 `../README.md` 正是 `doc/README.md`）。索引表把「组件描述 + 文档 md」与「`.vhd` 源码」成对列出，是用户发现组件的入口。

**回归注册（第五件）**：贡献规则明确「新测试台必须加进回归测试脚本，改 `sim/config.tcl`，合并前必须确认回归真的跑到了新测试台且无错」：[introduction.md:98-100](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L98-L100)。设计流程 Phase 7「维护」也反复警告：绝不要做只在 VHDL 改、不更新测试台的「脏补丁」，否则一个测试台失败会让整套回归形同虚设：[design_flow.md:44-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L44-L48)。

#### 4.3.2 核心流程

`sim/config.tcl` 是一个声明式配置，分三段，新组件要在这三段里各加一笔：

```text
(A) 编译源码  : add_sources "../hdl" { ... 你的 psi_fix_<name>.vhd \ ... } -tag src
                 （注意 psi_fix_pkg.vhd 必须最先编译，单独一段）
(B) 测试台源码: add_sources "../testbench" { ... psi_fix_<name>_tb/psi_fix_<name>_tb.vhd \ ... } -tag tb
(C) TB Run    : create_tb_run "psi_fix_<name>_tb"
                  tb_run_add_pre_script "python3" "preScript.py" "../testbench/psi_fix_<name>_tb/Scripts"
                  set dataDir [file normalize "../testbench/psi_fix_<name>_tb/Data"]
                  tb_run_add_arguments "-gfile_folder_g=$dataDir ..." \   ← 参数矩阵，每个串=一轮
                                        ...
                  add_tb_run
```

文档侧两步：

```text
(D) 单组件文档 : doc/files/psi_fix_<name>.md
                 （描述 + Generics 表 + Interfaces 表；可由 scripts/hdl2md.py 生成骨架）
(E) 索引登记   : doc/README.md 的 "List of compoments" 表加一行：
                 [描述](files/psi_fix_<name>.md)  |  [psi_fix_<name>.vhd](../hdl/psi_fix_<name>.vhd)
```

#### 4.3.3 源码精读

**config.tcl 三段**：

(A) 可综合源码集中在一个 `add_sources "../hdl"` 块里，mov_avg 出现在其中：[config.tcl:64-105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L64-L105)（mov_avg 具体在第 84 行）。注意 `psi_fix_pkg.vhd` 因为被所有实体依赖，必须**最先**单独编译：[config.tcl:50-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L50-L53)。

(B) 测试台源码在 `add_sources "../testbench"` 块里，mov_avg 测试台在第 135 行：[config.tcl:108-160](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L108-L160)。

(C) mov_avg 的 TB Run 是「数据生成型 preScript + 参数矩阵」的标准写法：先 `create_tb_run`、挂 preScript、设 dataDir、用 `tb_run_add_arguments` 给出三组 generic（每组对应一轮，分别 NONE/EXACT/ROUGH），最后 `add_tb_run` 收尾：[config.tcl:298-304](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L298-L304)。

**文档样板**：单组件文档 `psi_fix_mov_avg.md` 的标准结构是「描述 + Generics 表 + Interfaces 表 + 架构图」：[psi_fix_mov_avg.md:7-49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_mov_avg.md#L7-L49)。注意表里的 `$$ constant=... $$` / `$$ export=true $$` 标记是 `scripts/hdl2md.py` 自动提取 generic 默认值的钩子（u10-l2 会讲）。

**索引登记**：`doc/README.md` 的 `List of compoments` 表，每行成对链接「文档 md ↔ VHDL 源码」，mov_avg 在其中：[doc/README.md:15-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/README.md#L15-L48)。

#### 4.3.4 代码实践

**实践目标**：在 `config.tcl` 里注册一个「假想新组件」的测试台，体会三段声明的配合。

**操作步骤**：假设新组件叫 `psi_fix_gain`（第 5 节综合实践会完整设计它）。在 `sim/config.tcl` 中找出 mov_avg 的三处出现位置，仿照它的格式，在对应位置各加一行/一组（**仅在本讲义里描述，不要真的改源码仓库的 config.tcl**）：

1. 在 (A) 源码块 mov_avg 那行下方加：`  psi_fix_gain.vhd \`。
2. 在 (B) 测试台块 mov_avg 那行下方加：`  psi_fix_gain_tb/psi_fix_gain_tb.vhd \`。
3. 在 (C) 处仿照 mov_avg 的 TB Run 加一组 `create_tb_run "psi_fix_gain_tb"` ... `add_tb_run`。

**需要观察的现象**：

- 三段缺一不可：漏 (A) 编译不过；漏 (B) 测试台找不到；漏 (C) 测试台能编译但**回归根本不会运行它**（这是最隐蔽、最危险的遗漏，也是贡献规则特别强调「确认回归真的跑到了新测试台」的原因）。
- `tb_run_add_pre_script` 的第三个参数是 preScript 所在目录、`tb_run_add_arguments` 的 `file_folder_g` 指向 `Data` 目录，两者必须与实际目录结构一致。

**预期结果**：你能在本地（或脑中）跑一次回归，在 Transcript 里看到 `psi_fix_gain_tb` 被执行、且无 `###ERROR###`。

> 待本地验证：本地有仿真器时，注册后 `source ./run.tcl`，在输出里搜索 `gain_tb` 确认它被运行；无仿真器时按上面阅读 + 推演完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `psi_fix_pkg.vhd` 在 config.tcl 里要被单独成段、放在所有实体之前编译？

> **答**：因为 `psi_fix_pkg` 定义了全库通用的类型（`psi_fix_fmt_t` 等）和运算函数，**所有**可综合实体都 `use work.psi_fix_pkg.all`。VHDL 要求被依赖的库单元先编译；若把它和实体混在一段、顺序不当，依赖它的实体就会编译失败。把它单独前置是最稳妥的做法（详见 u1-l2、u2-l1）。

**练习 2**：贡献规则警告「不要做只在 VHDL 改、不更新测试台的脏补丁」。如果违反，最坏后果是什么？

> **答**：一个测试台失败会让整套回归挂掉；如果此时有人选择「不跑回归」或「容忍报错」，其他测试台就会悄悄过时而不被发现，整套回归投资在短时间内作废（design_flow.md Phase 7 的原话）。所以 Phase 7 反复强调：任何修改必须同步更新测试台。这是把「位真双模型」纪律延伸到维护阶段的体现。

---

## 5. 综合实践

把本讲五件套串起来，完成下面的「定点增益组件」贡献清单。这是本讲的主实践任务。

**场景**：你要往 psi_fix 贡献一个最小但有意义的组件 `psi_fix_gain`——把输入乘以一个综合期常量增益（纯乘常数，不含系数 RAM、不含抽取）。它正好能用前面所有单元的知识覆盖，且足够简单到可以徒手推完。

**输入/输出约定**：

- `in_fmt_g`、`out_fmt_g`：输入/输出定点格式（generic）。
- `gain_g`：实数增益（generic），综合期量化为硬件常量。
- `round_g` / `sat_g`：末端舍入/饱和（generic，默认 round/sat）。
- 端口：`clk_i / rst_i / vld_i / dat_i / vld_o / dat_o`（无反压，仿 mov_avg）。

**请交付下列五件套的设计（写在本讲义外即可，不需要真的提交 PR）**：

1. **Python 模型** `model/psi_fix_gain.py`：写出 `class psi_fix_gain` 的骨架——构造函数里把 `gain_g` 量化到某个系数格式（例如 `(0,1,16)`，参考 mov_avg 的 `gcCoefFmt`），`Process()` 里用 `psi_fix_from_real` 量化输入、再用一次 `psi_fix_mult` 乘到 `outFmt`。给出中间格式 `coefFmt` 的选择理由。
2. **VHDL 实体** `hdl/psi_fix_gain.vhd`：用两段式风格（u3-l3），组合进程里调一次 `psi_fix_mult`，valid 走 record 流水；端口与 generic 严格 `snake_case` + `_i/_o/_g` 后缀（u1-l4、u3-l3）。
3. **测试台 preScript** `testbench/psi_fix_gain_tb/Scripts/preScript.py`：实例化模型，用「全幅 +1/-1 跳变 + `np.random.seed(0)` 随机段 + 零段」刺激，经 `psi_fix_get_bits_as_int` 写 `Data/input.txt` 与 `Data/output.txt`（`np.savetxt(fmt="%i")`）。说明为什么 +1/-1 跳变能逼出饱和边界（参考 u3-l1 最坏情况刺激）。
4. **文档** `doc/files/psi_fix_gain.md`：仿 `psi_fix_mov_avg.md` 写「描述 + Generics 表 + Interfaces 表」，并在 `doc/README.md` 的索引表里加一行链接。
5. **回归注册** `sim/config.tcl`：写出 (A) 源码、(B) 测试台、(C) TB Run 三段需要新增的行，TB Run 至少给两组 generic（如 `duty_cycle_g=1` 与 `duty_cycle_g=5`），并挂上 `tb_run_add_pre_script`。

**参考答案要点**（用于自检，不是要你照抄）：

- 模型 `Process` 只需一行核心：`psi_fix_mult(dataFix, self.inFmt, self.coef, self.coefFmt, self.outFmt, self.rnd, self.sat)`；`coefFmt=(0,1,16)` 让系数与 18×25 的 DSP 输入对齐（u4-l1、u9-l3 思路）。
- VHDL 是 mov_avg 的「单乘法器」极简版，可直接参考 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) 的两段式骨架。
- preScript 的整体结构与 [preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) 几乎一致，差别只在「实例化哪个模型、跑哪些 generic」。
- config.tcl 的 TB Run 写法直接照搬 [config.tcl:298-304](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L298-L304) 的 mov_avg 模式。

> 待本地验证：若本地环境齐全，可真的把五件套落到对应目录、在 `sim/` 下 `source ./run.tcl`，确认 `psi_fix_gain_tb` 被运行且无 `###ERROR###`——这就是一次合格的贡献。

---

## 6. 本讲小结

- 贡献一个 psi_fix 组件要交「五件套」：Python 位真模型、VHDL 实现、自检测试台、文档、回归注册，顺序与依赖固定（先设计 → Python → VHDL+测试台 → 文档 → 注册）。
- **位真 Python 模型**是入库门槛与黄金参考；默认走手写型（构造算中间格式、`Process` 跑数据流），仅当贡献「同内核、不同表」的组件族时才用代码生成型（u8-l2）。模型必须镜像硬件每一个量化点，库级默认 trunc/wrap 与组件级默认 round/sat 相反，建模要显式对齐。
- **VHDL + 自检测试台**构成位真验证闭环：VHDL 用两段式风格；测试台由 TbGen.py 生成骨架，含 DUT + stim + check；preScript 用最坏情况 + 固定随机种子刺激跑模型，经 `psi_fix_get_bits_as_int` 写整数文本，测试台 `ApplyTextfileContent`/`CheckTextfileContent` 逐位比对，不符报 `###ERROR###`（CI 唯一功能失败判据）。
- **文档 + 回归注册**让组件可被发现、可被自动测试：单组件 `doc/files/<name>.md` + `doc/README.md` 索引一行；`sim/config.tcl` 三段缺一不可（源码 / 测试台 / TB Run），漏 TB Run 会导致测试台能编译但回归根本不运行它。
- 维护纪律：任何修改必须同步更新测试台，绝不做「只在 VHDL 改」的脏补丁——否则一个测试台失败会让整套回归形同虚设（design_flow.md Phase 7）。

---

## 7. 下一步学习建议

- **紧接着读 [u10-l2](u10-l2-ci-docs-dependencies.md)**：本讲只讲了「人怎么贡献」，u10-l2 讲「CI 怎么自动验收」——`scripts/ciFlow.py` 如何据 `###ERROR###` 判功能失败、`scripts/dependencies.py` 如何检出依赖、`scripts/hdl2md.py` 如何自动生成文档骨架、`unittest/psi_fix_pkg_test.py` 覆盖了库的哪一层。两讲合起来才是完整的「贡献 → 验收」闭环。
- **动手前的样板阅读顺序**：若真要贡献，建议按 mov_avg 这条最短路径通读一遍五件套——[hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd)、[model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py)、[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd)、[preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py)、[doc/files/psi_fix_mov_avg.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_mov_avg.md)——它是全库最小、最完整的贡献样板。
- **若贡献的是滤波器族组件**：先读 `hdl/FirNaming.txt` / `hdl/CicNaming.txt` 与 [doc/files/FirNaming.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/FirNaming.md)、[doc/files/CicNaming.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/CicNaming.md)（u6-l1、u7-l1），确认你的新组件命名落在既有命名规则内，避免与族内其他组件混淆。
- **若贡献的是函数近似族组件**：直接走 [model/psi_fix_lin_approx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py) 的代码生成路线，在 `CONFIGS` 里加一个配置、重跑 `__main__` 即可生成实体与测试台，手写 RTL 零修改（u8-l2）。
