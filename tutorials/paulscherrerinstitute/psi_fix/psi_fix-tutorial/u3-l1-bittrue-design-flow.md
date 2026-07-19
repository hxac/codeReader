# 位真双模型设计流程

## 1. 本讲目标

前两个单元（u1、u2）我们已经把 psi_fix 的「**位真双模型**」理念、目录结构、回归框架和定点包都拆解过了，但一直没回答一个更上层的问题：**当一个新组件从零开始做时，应该按什么顺序推进，才能让「位真」真正成立？**

本讲来自 psi_fix 官方的方法论文档 [doc/files/design_flow.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md)，它把一个 DSP 组件从想法到上线拆成**七个阶段**。学完后你应当能够：

- 完整复述 psi_fix 推荐的**七阶段设计流程**，并解释每个阶段的产出物与验收标准。
- 说清为什么流程**先做 Python、后写 VHDL**——位真验证思想的核心是「让 Python 模型当黄金参考，VHDL 只负责向它对齐」。
- 理解**刺激（stimuli）选择**对验证有效性的决定性影响：为什么白噪声不够、为什么要用 dirac 冲激和「符号匹配系数的最坏情况」刺激。

本讲**不**重复 u2-l3 已经讲过的 `psi_fix_pkg.py` 内部实现，也不重复 u1-l3 讲过的 PsiSim 回归框架细节，只在需要时引用。本讲是一篇**方法论讲义**，源码以文档为主，但会落到真实组件（`psi_fix_mov_avg` 的 preScript、`model/psi_fix_pkg.py` 的精度限制）上验证。

## 2. 前置知识

阅读本讲前，建议你已经掌握（来自 u1-l1、u1-l3、u2-l3）：

- **位真双模型**：每个可综合 VHDL 组件必须配套一个**逐位一致**的 Python 模型，由自检测试台逐位比对，不一致即打印 `###ERROR###`（u1-l1、u1-l3）。
- **preScript 协同仿真机制**：测试台运行前，先由 `preScript.py` 跑 Python 模型，把输入/输出写成 `Data/*.txt` 的**整数位表示**文本，VHDL 测试台再读回逐位比对（u1-l3）。
- **Python 位真模型的精度限制**：IEEE 754 双精度只有 53 位有效位，总位宽 `W > 53` 时无法逐位保真；中间结果（如 `cumsum`）也可能悄悄丢位（u2-l3）。
- **位增长三规则**：加减整数位 +1，舍入再 +1，两个有符号数相乘整数位相加后再 +1（u1-l4、u2-l2）。

补充一个本讲要用到的 DSP 基础概念：

- **FIR 滤波器的冲激响应**：给 FIR 喂一个「除第 0 个样本为 1、其余全 0」的 **dirac 冲激**，它的输出序列就**恰好等于滤波器的抽头系数**。这是 FIR 最容易调试的刺激。
- **FIR 的直流增益**：所有抽头系数之和 \(\sum h[k]\)，即输入恒定直流时输出与输入的比值。

## 3. 本讲源码地图

本讲主要涉及两份文档，并用一个真实组件做落地验证：

| 文件 | 作用 |
|------|------|
| [doc/files/design_flow.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md) | **本讲主线**：七阶段设计流程，以及 FIR 刺激选择的样例说明。 |
| [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) | 设计技巧：中间结果精度、Python 内建函数与位真、定点范围与「只在该量化的地方量化」。 |

落地参考（看流程在真实组件里如何体现）：

| 文件 | 作用 |
|------|------|
| [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) | Phase 3/4 的落地样例：用 Python 模型生成刺激与黄金输出，再用 `psi_fix_get_bits_as_int` 写成协同仿真文本。 |
| [model/psi_fix_pkg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py) | Python 位真模型包；其中的 53 位精度限制是 Phase 3「测试位真模型」要警惕的坑（u2-l3 已详解）。 |

> 提醒：`design_flow.md` 与 `tips.md` 都是 `scripts/hdl2md.py` 之外、人工维护的方法论文档，不在自动生成的组件文档之列；它们描述的是**整个库共守的工程纪律**，而非单个组件。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**七阶段流程** → **位真验证思想** → **刺激设计**。三者层层递进——流程给出骨架，位真思想解释骨架「为什么这么排」，刺激设计则是其中最易被忽视、却决定验证成败的关键细节。

### 4.1 七阶段设计流程

#### 4.1.1 概念说明

psi_fix 把一个 DSP 组件的完整生命周期划分为七个阶段。这套流程不是 ps_fix 的发明，而是 PSI 在多个 FPGA 项目里沉淀下来的工程纪律；它的核心诉求是：**把「想清楚」和「写代码」分开**，避免「边写边想」导致返工。

七个阶段分别是：

1. **算法可行性检查**（Algorithm Feasibility Check）
2. **设计与文档化**（Design & Documentation）
3. **实现并测试 Python 模型**（Implement and Test Python Models）
4. **Python 协同仿真**（Python Co-Simulations）
5. **VHDL 实现与协同仿真**（VHDL Implementation and Co-Simulation）
6. **硬件验证**（Test on HW）
7. **维护**（Maintenance）

一个关键认知：这七个阶段**不是线性的瀑布**，而是「前重后轻」——越靠前的阶段越值得投入，因为前面的决策会影响后面所有阶段；越靠后的阶段，问题越应该已经被前面解决掉了。

#### 4.1.2 核心流程

七阶段的流转与每个阶段的产出物，可以用下面这张图概括：

```
Phase 1  算法可行性          ──►  确定"实现哪个算法"、熟悉它
   │        (Python 探索)            产出: 算法定型
   ▼
Phase 2  设计与文档化        ──►  框图 / RTL / 定点格式 / 资源估算
   │        (纸面, 禁止编码!)          产出: 活文档(living document)
   ▼
Phase 3  实现+测试 Python    ──►  位真模型 + 最坏情况刺激测试
   │        (位真黄金参考)            产出: 已验证的 Python 模型
   ▼
Phase 4  Python 协同仿真     ──►  把刺激/响应写文件, 挂到回归 pre-script
   │        (数据落盘)                产出: Data/*.txt
   ▼
Phase 5  VHDL 实现+协同仿真  ──►  写 VHDL, 测试台逐位比对 Python
   │        (向黄金参考对齐)          产出: 通过位真比对的 VHDL
   ▼
Phase 6  硬件验证            ──►  上板, (理想情况下)无新问题
   │        (上板)                    产出: 硬件可用
   ▼
Phase 7  维护                ──►  任何修改都须经测试台, 禁止 dirty fix
            (纪律)                   产出: 回归持续可信
```

注意 Phase 2 的特殊地位：它是**唯一一个明确禁止写任何 Python 或 VHDL 代码**的阶段。这一点我们会在 4.2 节展开。

#### 4.1.3 源码精读

下面按 design_flow.md 的原文顺序，逐阶段精读。

**Phase 1 —— 算法可行性检查**：目标是「确定要实现哪个算法并熟悉它」，如果客户没有完全指定算法，可以在这里尝试不同方案。见 [doc/files/design_flow.md:9-10](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L9-L10)。这一阶段产出的是**决策**，不是代码。

**Phase 2 —— 设计与文档化**：这一阶段产出框图、RTL 图、定点格式，全部写进「活文档」（living document）；对宽加法、宽乘法、大容量存储要估算资源（至少算 DSP slice 和 BRAM）。见 [doc/files/design_flow.md:12-16](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L12-L16)。文档原文特别强调要记录**所有定点格式，包括内部格式**——逐个想清楚每个数的格式，正是为了避免漏掉 CIC、FIR 这类滤波器的位增长。

Phase 2 最重要的一句话是那条 `IMPORTANT` 警告：

> This phase does not include any Python or VHDL coding. Try to avoid start coding before everything is designed.

见 [doc/files/design_flow.md:17-18](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L17-L18)。理由是：如果设计某个具体模块时才发现路障，可能会影响整个系统的架构，已经写好的代码可能作废。

**Phase 3 —— 实现并测试 Python 模型**：实现位真 Python 模型**并测试**它。原文重音落在「tested」上——位真模型只有在真的被验证过正确性之后才有意义，而且必须用**最坏情况刺激**测试。见 [doc/files/design_flow.md:20-22](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L20-L22)。关于最坏情况刺激的具体例子，留到 4.3 节展开。

**Phase 4 —— Python 协同仿真**：与 Phase 3 的测试非常接近（甚至同一个脚本），区别只在于**把刺激和响应数据写到文件**。原文建议把 Python 仿真的调用直接挂进 Modelsim 回归脚本的 pre-script。见 [doc/files/design_flow.md:32-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L32-L35)。这正是 u1-l3 讲过的「数据生成型 pre_script」机制。

**Phase 5 —— VHDL 实现与协同仿真**：实现 VHDL，并与 Python 模型做**位真比对**。见 [doc/files/design_flow.md:37-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L37-L38)。这一阶段的验收标准非常明确：VHDL 输出必须与 Python 黄金参考逐位一致。

**Phase 6 —— 硬件验证**：原文半开玩笑地说，如果前几步都做对了，「这里不应该有问题……好吧，老实说『没问题』在现实中从未观察到，但至少问题更少」。见 [doc/files/design_flow.md:40-42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L40-L42)。这其实是个严肃的工程判断：上板调试的成本远高于仿真，前期投入越多，上板问题越少。

**Phase 7 —— 维护**：原文用近乎咆哮的语气强调——**不要**交付那种「只在 VHDL 里改、只在硬件上测、不写/不改测试台」的脏补丁（dirty quick-fix），「就算赶时间也不行」「别想！」。见 [doc/files/design_flow.md:44-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L44-L48)。理由是：一旦某个测试台失败却被容忍，整个回归套件的可信度就会崩塌——很快就没有人再相信回归结果，前期投入付诸东流。

#### 4.1.4 代码实践

**实践目标**：把七阶段流程**对照一个真实组件**走一遍，确认每个阶段的产出物在 psi_fix 仓库里确实有对应物，从而理解流程不是空话。

**操作步骤**：

1. 打开滑动平均组件的协同仿真脚本 [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py)。
2. 把它对应到七阶段：
   - **Phase 3（测试 Python 模型）**：脚本里 `result[gc] = ms.Process(sigFix)`（[第 44 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L44)）就是用位真模型跑出黄金输出。
   - **Phase 4（协同仿真落盘）**：脚本里 `np.savetxt(...)` 把结果写成 `Data/*.txt`（[第 58-60 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L58-L60)），完成「数据写到文件」。
   - **Phase 5（VHDL 协同仿真）**：对应的 `psi_fix_mov_avg_tb.vhd` 测试台会读回这些 txt，逐位比对（由 u3-l2 详解）。
3. 注意脚本里的刺激构造 `sigDbg`（[第 29 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L29)）：它用了 `0.99 / -0.99` 这种**接近满量程**的值——这正是 Phase 3「最坏情况刺激」思想的体现（详见 4.3 节）。

**需要观察的现象**：stimuli 文本里会出现接近正/负满量程的整数（`inFmt = psi_fix_fmt_t(1, 0, 10)` 即 11 位有符号，`±0.99` 落在 `±1023` 附近），而不是均匀分布的小随机数。

**预期结果**：你能用一句话说清 preScript.py 同时承担了 Phase 3 与 Phase 4 两个阶段的职责——这与 design_flow.md「Phase 4 通常与 Phase 3 在同一脚本里」的描述完全吻合。

#### 4.1.5 小练习与答案

**练习 1**：design_flow.md 说 Phase 4 与 Phase 3「通常很接近，甚至同一个脚本」。结合 mov_avg 的 preScript.py，指出哪几行属于 Phase 3、哪几行属于 Phase 4。

> **答案**：Phase 3 是「跑模型得到结果」——`ms.Process(sigFix)`（第 44 行）及其周围循环；Phase 4 是「把结果写到文件」——`np.savetxt(...)`（第 58-60 行）。两者确实在同一脚本里，仅靠职责区分。

**练习 2**：Phase 7 为什么对「赶时间也要写测试台」如此执着？用一句话说明回归套件可信度的崩塌机制。

> **答案**：一旦容忍某个失败的测试台，人们就会停止相信回归结果，进而不再跑回归或继续容忍新失败，整个回归投入在短期内作废——所以任何修改都必须经过测试台。

---

### 4.2 位真验证思想

#### 4.2.1 概念说明

七阶段流程里有一个反复出现的关键词：**位真（bit-true）**。Phase 3 要位真模型，Phase 5 要 VHDL 与模型位真比对。理解「位真验证思想」是看懂整套流程为什么这么排的钥匙。

核心思想可以浓缩成一句话：**Python 模型是黄金参考（golden reference），VHDL 只负责向它对齐，而不是反过来。**

这跟很多人直觉里的「先用浮点 MATLAB 算个理想结果，再让 HDL 尽量接近」截然不同。psi_fix 的位真模型**不是理想浮点算法**，而是主动约束到硬件定点格式（含舍入、饱和、位增长）的模型——也就是说，Python 模型**预言了硬件会输出的每一个位**。只有这样，VHDL 才能用「逐位比对」这种最严格的验收方式。

这个思想解释了流程里两个看似奇怪的规定：

- **为什么先 Python、后 VHDL**：因为 Python 是参考，参考必须先定稿、先验证。如果先写 VHDL，就没有黄金参考可比，位真比对无从谈起。
- **为什么 Phase 2 禁止编码**：因为一旦开始编码，思路就会被「实现细节」绑架，难再做架构层的设计决策；而架构决策（定点格式、流水级数）恰恰是位真模型的地基。

#### 4.2.2 核心流程

位真验证的闭环可以画成一条单向链：

```
   [Phase 2] 定点格式定稿  ──►  (格式是位真的地基)
              │
              ▼
   [Phase 3] Python 位真模型  ──►  = 黄金参考 (golden)
              │                      必须用最坏情况刺激验证自身正确
              ▼
   [Phase 4] 落盘 Data/*.txt  ──►  把黄金输入/输出写成整数位表示
              │
              ▼
   [Phase 5] VHDL + 测试台    ──►  读回 txt, 逐位比对 ###ERROR###
                                     VHDL 唯一任务: 向 Python 对齐
```

关键点：**箭头是单向的**。如果 VHDL 与 Python 不一致，应该去**修 VHDL**（除非有证据证明 Python 模型本身错了，那要回到 Phase 3 重验）。绝不能因为「VHDL 跑出来的数也能用」就反过来改 Python 去凑 VHDL——那样黄金参考就失去了意义。

> 注意一个边界情况：位真成立的前提是 **Python 模型本身是正确的**。所以 Phase 3 的「测试」不可省略——如果一个有 bug 的 Python 模型被当作黄金参考，VHDL 会忠实地复现这个 bug，位真比对照样通过，但整个组件是错的。这正是 design_flow.md 在 Phase 3 反复强调「tested」的原因。

#### 4.2.3 源码精读

位真思想在 psi_fix 源码里有几处直接体现。

**第一，Python 模型必须先验证自身正确**。design_flow.md 的 Phase 3 原文：

> Bit-true Python models only make sense if the models are really evaluated for their correctness.

见 [doc/files/design_flow.md:20-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L20-L21)。紧接着它说，如果只用「标准刺激」，「只在 Python 模型完全验证之后才进入 VHDL」的整个理念就被破坏了——见 [doc/files/design_flow.md:21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L21)。

**第二，位真模型要警惕 Python 自身的精度限制**。tips.md 的 *Precision of intermediate results* 一节举了一个反例：用 `out = mod(cumsum(input), 1.0)` 实现一个累加再取模的电路，当输入接近 1、向量超过 16 个元素时，`cumsum` 的结果就超过双精度能精确表示的 52 位，累加误差会被 `mod` 带回，结果悄悄出错。见 [doc/files/tips.md:112-127](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L112-L127)。这说明位真模型不是「写完就真」，中间结果的丢位（u2-l3 讲过的 53 位限制）会直接破坏位真性——这正属于 Phase 3 必须排查的范畴。

**第三，Python 内建函数要谨慎用于位真**。tips.md 的 *Built-in Python Functions* 一节指出，`numpy`/`scipy` 的内建函数（如 `filter`）速度最快，但它们**总是按全精度计算**；只有当电路也按全精度计算、只在最后量化时，才能用内建函数做位真模型。见 [doc/files/tips.md:131-147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L131-L147)。原文还特别提醒：IIR 滤波器**天然需要**内部量化，因此不能用 `filter` 函数做位真模型。这进一步印证：位真模型的实现方式，是被「电路实际怎么算」**反向约束**的。

**第四，定点范围必须基于理论最大值**。tips.md 在 *Get ranges correct* 一节强调，估算定点范围要基于「输入格式能推出的理论最大值」，**不要**依赖「上游保证信号不超过 0.7」这类跨模块假设——上游一改，设计就崩。见 [doc/files/tips.md:170-186](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L170-L186)。这条规则只在**单个处理块内部**允许放宽——位真模型的格式推导必须自洽，不能靠外部承诺。

#### 4.2.4 代码实践

**实践目标**：亲手体验「Python 先行、VHDL 对齐」的单向关系，理解为什么不能反过来。

**操作步骤**：

1. 打开 [model/psi_fix_pkg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py)，定位 `psi_fix_from_real` 与 `psi_fix_get_bits_as_int`（u2-l3 已详解其实现）。
2. 在本地 Python 里写一段最小脚本（**示例代码，非项目原有**）：

   ```python
   # 示例代码：演示位真黄金参考的生成
   from psi_fix_pkg import *
   import numpy as np

   inFmt  = psi_fix_fmt_t(1, 0, 4)   # 5 位有符号, 范围 [-1, 0.9375]
   x_real = np.array([0.6, -0.8, 1.0, -1.0])
   x_fix  = psi_fix_from_real(x_real, inFmt)        # 黄金参考(定点值)
   x_int  = psi_fix_get_bits_as_int(x_fix, inFmt)   # 协同仿真用的整数位表示
   print(x_int)   # 这就是写到 Data/*.txt、给 VHDL 逐位比对的值
   ```

3. 观察输出：`1.0` 与 `-1.0` 在 `[1,0,4]` 里会落到**不同的整数**（因为 `[1,0,x]` 能表示 `-1.0` 但不能表示 `+1.0`，见 u1-l4 的不对称性）。

**需要观察的现象**：`+1.0` 会被饱和到 `+0.9375`（即整数 `15`），而 `-1.0` 精确表示为整数 `-16`。两者绝对值不等。

**预期结果**：你应当意识到——这套整数位表示就是 Phase 4 写进 `Data/*.txt` 的内容，VHDL 测试台必须**逐位**复现它（包括 `+1.0→15` 这个饱和行为）。如果 VHDL 漏了饱和，比对就会失败——而修正方向只能是「补上 VHDL 的饱和」，不能是「改 Python 让它输出 16」。

> 说明：上述脚本依赖 `model/psi_fix_pkg.py` 与并排摆放的 `en_cl_fix`（u2-l3）。若本地未摆好依赖，运行会报 `ModuleNotFoundError`——此时按 u1-l1 的目录约定把 `en_cl_fix` 放到同级目录即可；无法确认运行结果时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设 Phase 5 发现 VHDL 输出与 Python 黄金参考在第 1023 个样本上差了 1 个 LSB。列出两种可能的处置方向，并指出哪种符合位真验证思想。

> **答案**：方向 A 是「修 VHDL，让它对齐 Python」；方向 B 是「改 Python，让它对齐 VHDL」。符合位真思想的是 A——Python 是黄金参考，除非能证明 Python 自身有 bug（需回 Phase 3 重验），否则 VHDL 必须向 Python 对齐。

**练习 2**：为什么 tips.md 反对用 `filter` 函数给 IIR 滤波器做位真模型，却允许给某类 FIR 用？

> **答案**：`filter` 总按全精度计算；那类 FIR 在整个加法链里也按全精度计算、只在最后量化，与 `filter` 行为一致，故可位真。而 IIR 天然需要内部量化（反馈支路），用 `filter` 会跳过这些量化点，模型不再位真。

---

### 4.3 刺激设计

#### 4.3.1 概念说明

七阶段流程里，Phase 3 有一句极易被忽略、却决定验证成败的话：位真模型必须用**最坏情况刺激（worst-case stimuli）**测试，而不能只用「标准刺激」。本模块专门讲清楚：什么是好的刺激、什么是差的刺激、为什么。

设计刺激的核心目标有两个，且两者经常冲突：

- **覆盖边界**：刺激要能触发饱和、溢出、最大位增长等极端行为。如果刺激永远温柔，饱和逻辑就从未被执行，等于没测。
- **易于调试**：当比对失败时，刺激要让你一眼看出「正确答案应该是什么」。如果刺激是一段看不出规律的随机数，失败时你连对错都判断不了。

psi_fix 给出的样板答案是用两类刺激组合：**dirac 冲激**（用于调试）和**符号匹配系数的 ±1 序列**（用于覆盖最坏情况）。

#### 4.3.2 核心流程

以 design_flow.md 给出的 FIR 样例为蓝本，刺激设计的推理过程如下：

```
给定 FIR 抽头系数 h = [h0, h1, ..., h_{N-1}]
   │
   ├─► 调试用刺激: dirac 冲激
   │      x = [1, 0, 0, ..., 0]
   │      输出 y = h  (冲激响应 = 抽头系数本身, 一眼对错)
   │
   ├─► 最坏情况刺激: 符号匹配系数的 ±1 序列
   │      x[k] = sign(h[k])      (系数为正取 +1, 为负取 -1)
   │      输出 y = Σ |h[k]|      (理论最大输出, 必然触发饱和)
   │
   └─► 反例: 白噪声
          x = 随机数
          输出 y = <某个看不出规律的值, 且极难恰好命中 Σ|h[k]|>
```

为什么「符号匹配系数的 ±1 序列」是最坏情况？因为 FIR 的输出是输入与系数的卷积：

\[
y[n] \;=\; \sum_{k=0}^{N-1} h[k]\,x[n-k]
\]

要让 \(y\) 最大，就要让每一项 \(h[k]\,x[n-k]\) 都同号且绝对值最大。输入限制在 \(\pm 1\) 时，取 \(x[n-k] = \mathrm{sign}(h[k])\) 即可让每一项都等于 \(|h[k]|\)，于是：

\[
y_{\max} \;=\; \sum_{k=0}^{N-1} |h[k]|
\]

而白噪声的样本符号是随机的，各项有正有负、相互抵消，**在合理数据量内几乎不可能恰好让 N 个样本的符号全部对齐**，因此几乎不可能命中 \(y_{\max}\)——饱和边界就成了「测不到的死角」。

design_flow.md 给了一个量化直觉：一个直流增益恰好为 1.0 的滤波器，用最坏情况刺激时输出可以到 **1.4**（因为 \(\sum|h[k]| > \sum h[k] = 1.0\)）；白噪声大概率测不出这个 1.4。见 [doc/files/design_flow.md:24-27](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L24-L27)。

#### 4.3.3 源码精读

design_flow.md 在 Phase 3 给出的 FIR 刺激样板原文：

> a good idea to stimulate the filter with a dirac impulse since it should output exactly its coefficients for this stimuli signal. … One worst-case could be stimulating the filter with a signal consisting of only +1 and -1 that perfectly matches the signs of the coefficients (e.g. [+1 -1 +1 +1 +1 -1 +1] for coefficients [0.05 -0.1 0.25 0.6 0.25 -0.1 0.05]). This signal leads to the absolute maximum output and stimulates any output saturation.

见 [doc/files/design_flow.md:23-24](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L23-L24)。注意这个例子：系数 `[0.05 -0.1 0.25 0.6 0.25 -0.1 0.05]` 的直流增益 \(\sum h[k] = 1.0\)，但 \(\sum |h[k]| = 1.4\)，所以最坏情况输出是 1.4，正好对应原文 [第 25-26 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L25-L26) 说的「output can reach 1.4」。

原文紧接着给了一句关键判断：

> White noise would most likely not include this worst-case within a reasonable amount of stimuli data.

见 [doc/files/design_flow.md:24](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L24)。这就是「白噪声不够」的官方依据。

这套思想在真实组件的 preScript 里有落地：mov_avg 的 `sigDbg` 用了 `0.99 / -0.99` 的近满量程方波（[preScript.py:29](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L29)），就是要逼出累加器的最大值；再用 `np.random.seed(0)` 配合 `np.random.randn` 生成一段**固定种子**的随机段（[preScript.py:35-36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L35-L36)）覆盖一般情况。固定种子保证了刺激**可复现**——这同样是 Phase 3/4 的隐含要求（详见 u1-l3 关于确定性数据重生的说明）。

tips.md 还提供了 dirac 的另一个用途——**估算频率响应**：对一个不完全线性或无限冲激响应的系统，可以注入 dirac 冲激、采集（足够长的）冲激响应，再对冲激响应调用 `freqz` 得到频率响应。见 [doc/files/tips.md:151-152](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L151-L152)。这是 dirac「易于调试」特性在分析层面的延伸。

#### 4.3.4 代码实践

**实践目标**：为一个**假想的 4 抽头 FIR** 设计最坏情况刺激，并亲手算出理论最大输出，从而理解为什么白噪声覆盖不到饱和边界。（本实践对应本讲规格里的实践任务。）

**操作步骤**：

1. 假设 4 抽头 FIR 的系数为（**示例数据，非项目原有**）：

   \[
   h = [0.10,\ -0.20,\ 0.30,\ -0.40]
   \]

2. **设计 dirac 调试刺激**：取 \(x = [1, 0, 0, 0, 0, \ldots]\)。预期输出序列就是系数本身：`[0.10, -0.20, 0.30, -0.40, 0, ...]`。如果 VHDL 输出与系数逐位一致，说明抽头系数与 MAC 结构正确——这是最易调试的刺激。

3. **设计最坏情况刺激**：令每个样本的符号匹配对应系数：

   \[
   x[k] = \mathrm{sign}(h[k]) \;\Rightarrow\; x = [+1,\ -1,\ +1,\ -1]
   \]

   即 `x = [1, -1, 1, -1]`。当这 4 个样本对齐到 4 个抽头上时，输出为：

   \[
   y_{\max} = \sum_{k=0}^{3} |h[k]| = 0.10 + 0.20 + 0.30 + 0.40 = 1.00
   \]

   注意：该滤波器直流增益 \(\sum h[k] = 0.10-0.20+0.30-0.40 = -0.20\)，而最坏情况输出却是 **1.00**——是直流增益绝对值的 5 倍。若输出格式按「直流增益量级」选（比如 `[1,0,4]`），最坏情况必然饱和。

4. **解释为何白噪声不够**：白噪声每个样本符号独立、各以 0.5 概率为正/负。要让 4 个样本的符号**恰好**全部匹配系数符号，概率是 \(0.5^4 = 6.25\%\)；而且还要让它们在时间上恰好对齐到 4 个抽头窗口里。即便喂 10000 个样本（mov_avg preScript 的 `RAND_SAMPLES=10000`），命中精确最坏情况的窗口也寥寥无几，且永远达不到理论 \(y_{\max}\)——饱和边界因此成为测不到的死角。

**需要观察的现象**：把 `x = [1,-1,1,-1]` 与 `h = [0.10,-0.20,0.30,-0.40]` 逐项相乘再求和，每一项都是正数（`0.10·1=0.10`、`-0.20·-1=0.20`、`0.30·1=0.30`、`-0.40·-1=0.40`），没有一项抵消——这正是「符号匹配」的威力。

**预期结果**：你能用一句话回答规格里的问题——**白噪声之所以不足以覆盖饱和边界，是因为它的样本符号随机，在合理数据量内极难让 N 个样本的符号同时匹配 N 个系数的符号，因而几乎不可能命中理论最大输出 \(\sum|h[k]|\)，饱和路径就成了未被覆盖的死角。**

> 说明：本实践为「源码阅读 + 手算」型，无需运行仿真即可完成。若想用 psi_fix 的位真模型验证手算结果，可参照 4.2.4 的示例代码，把 `psi_fix_from_real` 用在 `h` 与 `x` 上，再用 `psi_fix_mult`/手写卷积对比——但 FIR 卷积建议直接用 `numpy.convolve` 配合「最后才量化」的原则（见 tips.md *Built-in Python Functions*）。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：给系数 `h = [0.2, 0.2, 0.2, 0.2, 0.2]`（5 抽头）设计最坏情况刺激，并算出 \(y_{\max}\)。这组系数有什么特别之处？

> **答案**：所有系数同号，故最坏情况刺激是全 +1：`x = [1,1,1,1,1]`。\(y_{\max} = 5 \times 0.2 = 1.0\)。特别之处：此时直流增益 \(\sum h[k]\) 与 \(\sum|h[k]|\) 相等（都是 1.0），最坏情况与直流情况重合——这种「全同号系数」的滤波器，白噪声反而更容易逼近边界，但仍不如显式最坏情况刺激可靠。

**练习 2**：design_flow.md 说 dirac 冲激适合调试 FIR，是因为「它应该恰好输出系数」。请说明如果 dirac 输出的第 3 个样本与系数 `h[2]` 差了 1 个 LSB，最可能的故障定位在哪里。

> **答案**：dirac 输出第 k 个样本应等于 `h[k]`。第 3 个样本（`h[2]`）出错，最可能定位到**第 3 个抽头的系数加载/乘法路径**——要么系数写错位、要么该抽头的舍入/位宽设置有问题。dirac 的价值就是把「哪个抽头错」一眼暴露出来。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿任务**：为一个假想的「定点增益 + 4 抽头 FIR」组合块，按 psi_fix 的七阶段流程写出一份**设计文档骨架**（纸面，不写代码）。

要求你的文档包含以下小节，并填入具体内容：

1. **Phase 1 算法可行性**：用一句话说明该组合块要实现的功能（输入信号乘以常数增益，再做 4 抽头低通）。
2. **Phase 2 设计与文档化**（重点）：
   - 给定输入格式 `[1,0,8]`、增益 `G=0.5`、FIR 系数 `h=[0.1,-0.2,0.3,-0.4]`，用位增长规则（u1-l4、u2-l2）推导：增益后的格式、乘法累加后的全精度格式、最终输出格式。
   - 标注**只在哪些点量化**（遵循 tips.md *Minimize Rounding/Truncation/Saturation*，见 [doc/files/tips.md:188-194](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L188-L194)）。
   - 估算资源：4 次乘法是否够一个 DSP slice（提示：结合 tips.md *Get ranges correct* 对「省 bit 即省 DSP」的论述，见 [doc/files/tips.md:182-184](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L182-L184)）。
3. **Phase 3 Python 模型测试**：列出你要用的三类刺激——dirac、符号匹配最坏情况（直接用 4.3.4 的结论）、固定种子白噪声——并说明每类刺激**要覆盖什么**。
4. **Phase 4 协同仿真**：参照 [preScript.py:58-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L58-L60)，说明你会用 `psi_fix_get_bits_as_int` 把黄金输入/输出写成哪几个 txt。
5. **Phase 5 验收标准**：写明「VHDL 输出必须与 Python 黄金参考逐位一致，否则修 VHDL 不修 Python」（呼应 4.2 的单向关系）。
6. **Phase 7 维护承诺**：写一句「任何参数调整都必须改 preScript 并重跑回归，禁止 dirty fix」（呼应 [design_flow.md:44-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/design_flow.md#L44-L48)）。

完成后，你应当得到一份**可直接指导后续编码**的设计文档——它正是 Phase 2 要求的「活文档」。如果某个小节你填不出来，就说明你对那个阶段的理解还有缺口，应回到对应模块复习。

## 6. 本讲小结

- psi_fix 把一个组件的生命周期分为**七阶段**：算法可行性 → 设计与文档化 → Python 模型 → Python 协同仿真 → VHDL+协同仿真 → 硬件验证 → 维护；流程「前重后轻」，越靠前越值得投入。
- **Phase 2 是唯一禁止编码的阶段**——先把框图、定点格式、资源估算写进活文档，避免边写边想导致架构级返工。
- **位真验证思想**的核心是「Python 模型是黄金参考，VHDL 只负责向它对齐」，箭头单向——不一致时修 VHDL，而非改 Python 凑 VHDL。
- 位真模型必须先用**最坏情况刺激**验证自身正确，否则一个有 bug 的 Python 模型会被 VHDL 忠实复现，位真比对照样通过但组件是错的。
- **刺激设计**要用 dirac（易调试，输出即系数）+ 符号匹配系数的 ±1 序列（覆盖理论最大输出 \(\sum|h[k]|\)，逼出饱和）；白噪声符号随机，在合理数据量内几乎不可能命中最坏情况，会留下饱和死角。
- tips.md 补充了三条位真纪律：中间结果有 53 位精度限制、内建函数只在「电路全精度计算」时才位真、定点范围只基于理论最大值（不靠跨模块假设）。

## 7. 下一步学习建议

本讲建立的是**方法论骨架**，下一步应当看这套流程在真实代码里如何落地：

- **u3-l2 测试台与协同仿真流程**：以 `psi_fix_mov_avg` 为完整样例，拆解 VHDL 自检测试台的 stim/check 双进程、preScript 如何生成 `Data/*.txt`、测试台如何读回逐位比对并打印 `###ERROR###`——这是本讲 Phase 4/5 的代码级展开。
- **u3-l3 两段式编码风格与命名约定**：进入 Phase 5 的 VHDL 实现细节，讲 two-process 编码模式与 record 流水封装，回答「Phase 2 定稿的定点格式如何落到可读的 VHDL」。
- 想加深对「位真的精度陷阱」的理解，可重读 [doc/files/tips.md:112-127](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L112-L127) 的 *Precision of intermediate results*，并对照 u2-l3 的 `BittruenessNotGuaranteed` 机制。
