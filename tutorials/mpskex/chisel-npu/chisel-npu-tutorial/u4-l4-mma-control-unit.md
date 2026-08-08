# 控制单元 ControlUnit

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出为什么 MMALU 的**控制信号**也要走一条「脉动阵列」，而不是直接广播给每个 PE。
- 看懂 `ControlUnit` 这个模块：一个深度为 \(2n-1\) 的一维移位寄存器，外加一个 `ORGate`。
- 解释 `cbus_out(n*i+j) := reg(i+j)` 这行代码如何把控制信号按**反对角线** \(i+j\) 分发给 \(n\times n\) 个 PE，使控制与数据波前对齐。
- 理解为什么用 **OR 门**把所有在飞的 `keep` 信号汇总成 `dat_clct`（收集信号）：只要任一拍还在累加，就继续收集。
- 区分三类输出：`cbus_dat_clct`（OR 出来的「还在算」窗口）、`clct`/`cbus_use_accum`（取自最末级寄存器的「全局完成」标志）。

本讲承接 u4-l2（二维脉动阵列 `SystolicArray2D`），回答一个它没解决的问题：**数据是脉动流动的，那发给每个 PE 的控制位（尤其 `keep`）凭什么能在正确的拍到达正确的 PE？**

## 2. 前置知识

阅读本讲前，请确认你已经理解下面这些来自前面讲义的概念：

- **PE 与 keep 控制（u4-l1）**：每个 PE 是一个乘累加单元，控制位 `keep=true` 时 `res += in_a*in_b`（累加），`keep=false` 时 `res := in_a*in_b`（覆盖）。`keep` 就是 ControlUnit 要分发到每个 PE 的核心控制位。
- **波前与反对角线（u4-l2）**：在 `SystolicArray2D` 中，波前 \(m\) 在第 \(m+i+j\) 拍抵达 \(\mathrm{PE}(i,j)\)；同一反对角线（\(i+j\) 相同）的 PE 在同一拍处理同一个波前。这是本讲「控制按 \(i+j\) 延迟」要与之对齐的目标。
- **数据馈送与收集（u4-l3）**：`DataFeeder` 用阶梯延迟（skew）把输入扭曲成反对角波前；`DataCollector` 用模 \(n\) 计数器逐列回收 PE 输出，其中 `dat_clct` 是计数器的使能信号——本讲要讲的正是这个 `dat_clct` 如何产生。
- **NCoreMMALUCtrlBundle（u3-l1）**：MMALU 的控制包只有三个字段 `keep / use_accum / busy`。ControlUnit 的工作就是在时间轴上把它正确地搬移与汇总。

一条贯穿全讲的直觉：**数据走二维脉动，控制走一维脉动，两者的「延迟量」刻意做得相同，于是控制永远与它要配对的数据同时到达同一个 PE。**

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/alu/mma/cu/controlUnit.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala) | `ControlUnit` 模块本体：一维移位寄存器 + 对角线广播 + OR 汇总。本讲主角。 |
| [src/main/scala/utils/gates.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/utils/gates.scala) | `ORGate`：一个通用的「\(n\) 路 Bool 输入 → 1 路 OR 输出」组合门。ControlUnit 用它汇总 `keep`。 |
| [src/main/scala/isa/micro_op/MMALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala) | `NCoreMMALUCtrlBundle`：被移位、被广播、被 OR 的那个三字段控制包。 |
| [src/main/scala/alu/mma/mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala) | `MMALU` 顶层：实例化 `ControlUnit`，把 `cbus_out` 经流水寄存器 `pipe_ctrl` 喂给 PE，把 `cbus_dat_clct` 经 `pipe_dat_clct` 喂给 `DataCollector`。看 ControlUnit 如何被「挂」进整条流水线。 |
| [src/test/scala/alu/mma/cu/CUSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/cu/CUSpec.scala) | 唯一直接测试 ControlUnit 的仿真，断言 `cbus_out(i,j).keep == 输入 i+j 拍前的 keep`，是对角线广播的权威佐证。 |

## 4. 核心概念与源码讲解

### 4.1 ORGate：把多个 `keep` 汇总成一个「收集」信号

#### 4.1.1 概念说明

`keep` 是**每个 PE 私有**的控制位：某个波前是否累加，取决于它到达该 PE 时配对的那一拍 `keep`。但对 `DataCollector` 来说，它不需要知道「哪一个 PE 在算」，它只需要知道一个**全局**问题的答案：

> 此刻阵列里**还有任何一个 PE 正在累加**吗？如果有，就继续收集（`dat_clct=1`）；如果全部停了，就可以停止。

这正是「或」（OR）的语义：把所有「在飞」的 `keep` 信号做一次 OR，只要其中**任意一个**为真，结果就为真。`ORGate` 就是把这件事封装成一个可复用的组合模块：\(n\) 路 Bool 输入、1 路 Bool 输出，输出等于全部输入的按位或。

为什么用专门的模块而不是随手写一行 `|`？因为 `ControlUnit` 要 OR 的路数等于阵列规模（\(2n-1\) 路，上板 \(n=64\) 时达 127 路），把它包成 `Module` 后，综合器能把它当作一棵规整的 OR 树来放置布线，比散落在各处的 `|` 更可控。这也是它被放在 `utils/gates.scala` 作为通用工具的原因。

#### 4.1.2 核心流程

`ORGate` 是**纯组合逻辑**（无寄存器、单拍输出），流程极简：

1. 输入 `in: Vec(n, Bool())`，即 \(n\) 个 Bool。
2. 用 `reduce((a, b) => a | b)` 把它们折叠成单个 Bool。
3. 输出 `out` 同拍等于这 \(n\) 个值的或。

写成数学即：

\[
\text{out} \;=\; \bigvee_{k=0}^{n-1} \text{in}(k)
\]

#### 4.1.3 源码精读

`ORGate` 全部实现只有两行有意义的逻辑：

[utils/gates.scala:L6-L12](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/utils/gates.scala#L6-L12) 定义模块与 IO，其中 `n` 默认 8、`in` 是 `Vec(n, Bool())`、`out` 是单个 Bool；

[utils/gates.scala:L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/utils/gates.scala#L11) 这一行 `io.out := io.in.reduce((a, b) => a | b)` 就是全部：对 `in` 这个 Vec 做 reduce，运算符是 `|`，得到所有元素的或。

> 提示：Chisel 的 `Vec.reduce` 与 Scala 集合的 `reduce` 用法一致，左结合地把二元运算折叠成一个值。这里因为元素都是 `Bool`，`|` 就是逻辑或。

#### 4.1.4 代码实践

**目标**：确认 `ORGate` 的行为就是「任一为真即为真」，并理解它为何适合做 collect 信号的汇总。

**操作步骤（源码阅读型实践）**：

1. 打开 [utils/gates.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/utils/gates.scala)，确认 `ORGate(n)` 的输入路数由参数 `n` 决定。
2. 手算三组输入的 `out`：
   - \(n=3\)，`in = [0,0,0]` → `out = 0`
   - \(n=3\)，`in = [0,1,0]` → `out = 1`
   - \(n=3\)，`in = [1,1,1]` → `out = 1`
3. 把 `ORGate` 的语义翻译成一句话：「只要这 \(n\) 个 `keep` 里**有一个**是 1，`dat_clct` 就是 1，收集器就继续工作。」

**需要观察的现象 / 预期结果**：只要存在一个 1，输出就是 1；全 0 才输出 0。这正好对应「只要任一拍在累加就继续收集」。本步为纯推导，无需运行；若想跑通，可仿照 u9-l1 用 `EphemeralSimulator` 写一个 3 行 poke/expect 的小测（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `ORGate` 的某个输入 `in(k)` 因为未连接而保持默认值（Chisel 中未显式驱动的 Wire 默认 0），对 `out` 有什么影响？

**参考答案**：默认 0 不改变 OR 的结果（\(x \lor 0 = x\)）。所以哪怕个别 `keep` 没被驱动，只要其余有 1，`out` 仍为 1——这是 OR 相比 AND 在「汇总使能」上的健壮性优势。

**练习 2**：为什么用 OR 而不是 AND 来产生 `dat_clct`？

**参考答案**：`dat_clct` 语义是「是否**还有**波前在算」，是一个「存在性」判断（existential），对应 OR；AND 是「全部都在算」（universal），会过早拉低，导致收集器提前停止。

---

### 4.2 ControlUnit：控制信号的一维脉动移位

#### 4.2.1 概念说明

`SystolicArray2D` 让数据以反对角线波前的方式流动，结果是：同一个波前到达不同 PE 的时刻不同——波前 \(m\) 在第 \(m+i+j\) 拍到达 \(\mathrm{PE}(i,j)\)。那么，控制这个波前是否累加的 `keep` 位，也必须**在不同的拍**送到不同位置的 PE，才能与数据在同一个拍相会。

最直接的办法是给每个 PE 单独拉一组控制线、由前端逐拍调度——但这会让控制变得极其复杂，前端要为 \(n^2\) 个 PE 各算一个时间表。`ControlUnit` 的解法更优雅：**既然数据的延迟是「沿对角线 \(i+j\) 递增」的，那就让控制信号也走一条延迟随 \(i+j\) 递增的移位寄存器**。把控制包从一端喂入，它每过一拍向前移一格；PE\((i,j)\) 恰好读取「移了 \(i+j\) 格」之后的那个寄存器。于是控制和数据的延迟量相同，自动对齐，前端只需逐拍喂一个控制包即可。

这就是代码注释里那句「Control unit also uses systolic array to pass instructions」的含义：**控制也脉动**。只不过数据是**二维**脉动（`reg_h`/`reg_v`），而控制是**一维**脉动（一条深度 \(2n-1\) 的移位线），因为控制只需按对角线索引 \(i+j\)（取值 \(0\dots 2n-2\)，共 \(2n-1\) 个不同值）来区分时刻，一维就够。

#### 4.2.2 核心流程

设阵列规模为 \(n\)，控制包为 `cbus_in`（一个 `NCoreMMALUCtrlBundle`，含 `keep/use_accum/busy`）。

1. 维护一个深度为 \(2n-1\) 的寄存器向量 `reg`（每个元素都是一个完整控制包），初值全 0。
2. 每个时钟沿：`reg(0)` 采样本拍输入 `cbus_in`；`reg(i) := reg(i-1)`（即整体向右移一格）。
3. 因此 `reg(k)` 存放的就是「\(k\) 拍前」输入的控制包。

写成状态转移：

\[
\text{reg}_0(t+1) = \text{cbus\_in}(t),\qquad
\text{reg}_k(t+1) = \text{reg}_{k-1}(t),\quad k=1,\dots,2n-2
\]

于是 \(\text{reg}_k(t) = \text{cbus\_in}(t-k)\)：第 \(k\) 级寄存器存放 \(k\) 拍前的输入。这一条移位线就是「控制的时间轴」。

#### 4.2.3 源码精读

先看 `NCoreMMALUCtrlBundle`——被移位的就是这个三字段小包：

[isa/micro_op/MMALUMicroCode.scala:L7-L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala#L7-L11) `keep / use_accum / busy` 三个 Bool 字段；它没有地址、没有数据，纯粹的瘦控制包。

再看 `ControlUnit` 的 IO 与移位线：

[alu/mma/cu/controlUnit.scala:L11-L18](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L11-L18) IO 定义：`cbus_in` 是单路输入控制包；`cbus_out` 是 `Vec(n*n, ...)`——给每个 PE 一份控制包；另有 `cbus_dat_clct / cbus_use_accum / clct` 三个标量输出。

[alu/mma/cu/controlUnit.scala:L20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L20) 这一行声明深度为 \(2n-1\) 的移位线 `reg`，每个元素是一个 `NCoreMMALUCtrlBundle`，初值全 0。注释里的 "diagnal"（diagonal）点明了它服务于对角线分发。

[alu/mma/cu/controlUnit.scala:L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L28) `reg(0) := io.cbus_in`——移位线的首级采样本拍输入。

[alu/mma/cu/controlUnit.scala:L34-L36](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L34-L36) 这个 `for` 循环把 `reg(1..2n-2)` 依次接成移位链：`reg(i) := reg(i-1)`。由于 Chisel 的 `:=` 是「下一拍生效」的非阻塞赋值，整条链在同一时钟沿整体右移一格。

> 关键认知：`ControlUnit` **不计算** `use_accum` 和 `busy`，它只是把它们与 `keep` 一起移位，从而在时间上对齐。`use_accum/busy` 的真正来源是顶层 `io.ctrl`（见 4.3.3），ControlUnit 只负责把它们延迟到正确的拍。

#### 4.2.4 代码实践

**目标**：用现成的 `CUSpec` 仿真，亲眼确认「`reg(k)` 存放 \(k\) 拍前的输入」这一移位关系。

**操作步骤**：

1. 打开 [src/test/scala/alu/mma/cu/CUSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/cu/CUSpec.scala)，阅读它如何维护一个 `history` 数组：每次把新输入 `_cbus_in` 前插到 `history(0)`，`step()` 后断言 `cbus_out(i,j).keep == history(i+j)`。
2. 用项目的单测快捷脚本运行它（参见 u9-l2）：

   ```
   tool/test-specific-spec.sh alu.mma.cu.CUSpec
   ```

3. 观察打印的 "Input tick" 与 "Control tick" 序列。

**需要观察的现象 / 预期结果**：仿真通过，断言全部成立——这等价于「控制包输入后，恰好经过 \(i+j\) 拍出现在 \(\mathrm{PE}(i,j)\) 的 `cbus_out` 上」。若你的环境无 firtool/verilator，本步为「待本地验证」；可改为纯纸面推导（见 4.3.4 的逐拍表）。

#### 4.2.5 小练习与答案

**练习 1**：移位线深度为什么是 \(2n-1\) 而不是 \(n-1\) 或 \(n^2\)？

**参考答案**：因为按对角线索引 \(i+j\) 的取值范围是 \(0\) 到 \(2n-2\)，共 \(2n-1\) 个不同值。控制只需区分这 \(2n-1\) 个「时刻」，故移位线深度等于 \(2n-1\)。它远小于 PE 数 \(n^2\)——这正是「按对角线广播」省下的连线与寄存器。

**练习 2**：`ControlUnit` 的 `reg` 是寄存器（`RegInit`），意味着控制信号相比输入会延迟。这是否会让控制比数据晚到？

**参考答案**：不会。数据通路（`DataFeeder` 的 skew + `SystolicArray2D` 的 `reg_h/reg_v`）也带有同样随 \(i+j\) 递增的寄存器延迟。两条路径的「延迟量」刻意相等，所以控制与对应数据同时到达同一 PE（CUSpec 与流式归约测试共同验证）。

---

### 4.3 对角线广播与「还在算 / 已完成」两类输出

#### 4.3.1 概念说明

移位线 `reg` 解决了「控制的时间轴」，但 PE\((i,j)\) 需要读取的是 `reg(i+j)`，而不是 `reg(0)`。把 `reg` 的每一级**广播**给「所有 \(i+j\) 等于该级下标」的 PE，就是对角线广播：反对角线 \(\{(i,j)\mid i+j=k\}\) 上的 PE 共享同一个 `reg(k)`。这与 u4-l2 中「同一反对角线的 PE 同拍处理同一波前」完全对应——它们本就该共享同一个控制包。

在广播之外，`ControlUnit` 还要向上输出两类汇总信号：

- **`cbus_dat_clct`（收集使能）**：由 `ORGate` 把「当前输入的 `keep`」与「`reg(0)` 到 `reg(2n-3)` 的 `keep`」做 OR 得到。它的语义是「是否还有波前正在累加」——只要任一在飞的波前 `keep=1`，就继续让 `DataCollector` 收集。这正是 OR 而非 AND 的原因。
- **`clct` 与 `cbus_use_accum`（全局完成与累加使能）**：二者都取自**最末级** `reg(2n-2)`，分别引出其中的 `busy` 与 `use_accum`。最末级寄存器存放的是「最老」的、已穿过整条对角线的控制包，因此它代表「这一波已经走完全程」。`io.clct := reg(2n-2).busy` 即「`busy` 延迟 \(2n-2\) 拍」，文档据此指出 `io.clct` 会在整个流式喂入加排空期间保持高电平。

一句话区分：`cbus_dat_clct` 回答「**现在**还要不要收」（OR，向前看在飞的 keep）；`clct` 回答「整批**是否**还在处理」（取最末级 busy，向后看是否排空）。

#### 4.3.2 核心流程

设 \(n\) 为阵列边长，输入控制包序列为 \(\text{cbus\_in}(0), \text{cbus\_in}(1), \dots\)。

1. **对角线广播**：对每个 PE\((i,j)\)，把它的控制接成

\[
\text{cbus\_out}(n\cdot i + j) \;=\; \text{reg}(i+j) \;=\; \text{cbus\_in}(t-(i+j))
\]

即 PE\((i,j)\) 在第 \(t\) 拍收到 \(i+j\) 拍前输入的控制包。数据波前 \(m\)（第 \(m\) 拍输入）也在第 \(m+i+j\) 拍到达 PE\((i,j)\)；令 \(t=m+i+j\)，则控制收到的恰是 \(\text{cbus\_in}(m)\)，与数据同源同拍——**对齐达成**。

2. **收集使能**：把 \(2n-1\) 路 `keep` 做 OR：

\[
\text{cbus\_dat\_clct}(t) \;=\; \text{cbus\_in}(t).\text{keep} \;\lor\; \bigvee_{k=0}^{2n-3} \text{reg}(k).\text{keep}
\]

注意它汇总的是「当前输入」加「`reg(0)` 到 `reg(2n-3)`」，**共 \(2n-1\) 路，恰好不含最末级 `reg(2n-2)`**。

3. **全局完成与累加使能**：

\[
\text{clct}(t) = \text{reg}(2n-2).\text{busy},\qquad
\text{cbus\_use\_accum}(t) = \text{reg}(2n-2).\text{use\_accum}
\]

即把入口处随 `cbus_in` 进来的 `busy / use_accum` 各延迟 \(2n-2\) 拍输出。

#### 4.3.3 源码精读

**对角线广播**：

[alu/mma/cu/controlUnit.scala:L38-L42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L38-L42) 双重 `for` 循环，`io.cbus_out(n*i+j) := reg(i+j)`。同一个 `reg(i+j)` 被多条 `cbus_out` 引用（例如 PE\((0,1)\) 与 PE\((1,0)\) 都读 `reg(1)`），这就是「反对角线共享同一级控制」的物理实现。

**OR 汇总成 `cbus_dat_clct`**：

[alu/mma/cu/controlUnit.scala:L22-L33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L22-L33) 这里实例化 `ORGate(2*n-1)`，并把它的 \(2n-1\) 路输入分别接成：`in(0) := io.cbus_in.keep`（当前输入），`in(i+1) := reg(i).keep`（\(i=0\dots 2n-3\)）。注意循环上界是 `2*n-2`，故 `reg` 索引止于 \(2n-3\)，最末级 `reg(2n-2)` 不进 OR。`or_g.io.out` 经 `:<>=` 送给 `io.cbus_dat_clct`。

> 关于 `:<>=`：它是 Chisel 的连接算子之一。本讲这些用法都是「单个 Bool 信号接到单个 Bool 端口」，其效果与大家熟悉的 `:=`（右驱动左）一致——即把右边信号的值驱动到左边的端口。在更复杂的聚合类型/位宽场景下它才有别于 `:=`，本讲可暂按 `:=` 理解。

**全局完成与累加使能**：

[alu/mma/cu/controlUnit.scala:L24-L25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L24-L25) `io.clct :<>= reg(2*n-2).busy` 与 `io.cbus_use_accum :<>= reg(2*n-2).use_accum`，二者都取自最末级寄存器。结合 `reg(2n-2) = cbus_in(t-(2n-2))`，可知 `clct` 就是入口 `busy` 延迟了 \(2n-2\) 拍——这正是文档 [SystolicArray.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md) 里「`io.clct` is `busy` delayed by \(2K-2`」的来源。

**测试佐证**：

[src/test/scala/alu/mma/cu/CUSpec.scala:L30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/cu/CUSpec.scala#L30) 断言 `dut.io.cbus_out(n*i+j).keep.expect(history(i+j))`，其中 `history(i+j)` 正是 \(i+j\) 拍前的输入。这是对角线广播 \( \text{cbus\_out}(i,j) = \text{cbus\_in}(t-(i+j)) \) 的直接验证。

**ControlUnit 如何挂进 MMALU**：

[alu/mma/mma.scala:L44-L45](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L44-L45) `MMALU` 实例化 `ControlUnit(n)` 并把顶层 `io.ctrl` 接到 `cbus_in`——注意 `io.ctrl`（即 `NCoreMMALUCtrlBundle`）的 `keep/use_accum/busy` 全部来自这里，由测试或前端逐拍驱动。

[alu/mma/mma.scala:L78](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L78) `pipe_ctrl(n*i+j) := ctrl_array.io.cbus_out(n*i+j)`——控制包经一级流水寄存器 `pipe_ctrl` 再喂给 PE，这是为修 200 MHz 时序而加（见 u4-l5 / mma.scala 顶部注释）。

[alu/mma/mma.scala:L86-L88](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L86-L88) `cbus_dat_clct / cbus_use_accum / clct` 各经一级 `pipe_*` 寄存器送给 `DataCollector` 与顶层。`dat_clct`（u4-l3 里驱动收集器计数器的使能）的最终来源，正是本讲的 OR 门。

> 关于「为何 OR 不含最末级 `reg(2n-2)`」的精确边界：本讲能确定的依据是——OR 汇总的是「最近 \(2n-1\) 个 keep 采样」（当前输入 + `reg(0..2n-3)`），它产生一个覆盖「喂入期 + 排空期」的高电平窗口；而最末级的完成信号由 `reg(2n-2).busy` 单独给出。这两者在 `MMALU` 中又各叠加一级 `pipe_*` 流水寄存器，与 `DataCollector` 的模 \(n\) 计数器协同对齐，最终由 `MMALUStreamReduceSpec` 端到端验证（见 u4-l5）。精确到单拍的边界理由需对照该测试本地确认（待本地验证）。

#### 4.3.4 代码实践（本讲主实践）

**目标**：对 \(n=2\) 手推 `ControlUnit` 的 `reg` 在前 3 拍的内容，标出每拍哪些 `cbus_out` 被更新，并解释「OR 汇总 keep → `dat_clct`」为何正确。

**设定**：\(n=2\)，故 \(2n-1=3\)，`reg` 有 3 级（下标 0/1/2）。`cbus_out` 共 4 路：

| 下标 | PE | 取自 |
|:---:|:---:|:---|
| `cbus_out(0)` | PE(0,0) | `reg(0)` |
| `cbus_out(1)` | PE(0,1) | `reg(1)` |
| `cbus_out(2)` | PE(1,0) | `reg(1)` |
| `cbus_out(3)` | PE(1,1) | `reg(2)` |

注意 PE(0,1) 与 PE(1,0) **共享 `reg(1)`**（同属反对角线 \(i+j=1\)），这就是对角线广播。

**输入**（模仿流式归约：连续两拍 `keep=1`，再拉低）：`K0=1, K1=1, K2=0`，其余 `use_accum/busy` 设 0 以聚焦 `keep`。`reg` 初值全 0。

**操作步骤**（逐拍推导，每拍先移位再读出）：

- **tick 0**：输入 `K0=1`。移位后 `reg=[1,0,0]`。
  - `cbus_out` 的 keep：`[PE00=1, PE01=0, PE10=0, PE11=0]`。
  - 本拍**新输入** \(K_0\) 只到达 PE(0,0)；其余对角线还未收到。
- **tick 1**：输入 `K1=1`。移位后 `reg=[1,1,0]`。
  - `cbus_out` keep：`[1,1,1,0]`。
  - \(K_0\) 推进到 `reg(1)`，于是反对角线 PE(0,1)、PE(1,0) 都拿到 \(K_0=1\)；新输入 \(K_1\) 到达 PE(0,0)。
- **tick 2**：输入 `K2=0`。移位后 `reg=[0,1,1]`。
  - `cbus_out` keep：`[0,1,1,1]`。
  - \(K_0\) 推进到 `reg(2)`，到达最后一个 PE(1,1)。

**`dat_clct`（组合输出，等于 `OR(cbus_in.keep, reg(0..1).keep)`，\(n=2\) 时共 3 路）**：

| 拍 | cbus_in.keep | reg(0).keep | reg(1).keep | dat_clct = OR |
|:---:|:---:|:---:|:---:|:---:|
| tick 0 | 1 | 0 | 0 | **1** |
| tick 1 | 1 | 1 | 0 | **1** |
| tick 2 | 0 | 1 | 1 | **1** |
| tick 3（输入 0，reg=[0,0,1]） | 0 | 0 | 1 | **1** |
| tick 4（输入 0，reg=[0,0,0]） | 0 | 0 | 0 | **0** |

**需要观察的现象 / 预期结果**：

1. 每拍 `reg` 整体右移一格，`reg(0)` 永远是最新输入。
2. 同一个 `keep` 值随拍数推进，依次出现在 PE(0,0) → 反对角线{PE(0,1),PE(1,0)} → PE(1,1)，恰是数据波前到达这些 PE 的顺序——控制与数据对齐。
3. `dat_clct` 在 tick 0~tick 3 持续为 1，直到 tick 4（最后一个 `keep=1` 的波前也已排出 `reg(1)`）才掉到 0。

**解释「为何 OR 汇总 keep 能正确产生 `dat_clct`」**：`keep=1` 标记「这一拍进入的波前要累加」。阵列中同时存在多个处于不同对角线阶段的波前，它们的 `keep` 分布在 `cbus_in` 与 `reg(0..2n-3)` 这 \(2n-1\) 个位置上。只要**任意一个**位置为 1，就说明「还有波前正在被某个 PE 累加」，收集器就必须继续收（`dat_clct=1`）；只有全部为 0（没有任何波前在累加）时才停止。这正是 OR 的「存在性」语义——「只要任一拍在累加就继续收集」。若误用 AND，则会在仅剩最后一个波前在算时提前关停收集，漏掉结果。

> 本步为纸面推导（可手工复算）；如需仿真核对，可仿照 `CUSpec` 的写法，对 `new ControlUnit(2)` 逐拍 `poke` keep、`peek` `cbus_out` 与 `cbus_dat_clct`，比对上表（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：对 \(n=2\)，PE(0,1) 和 PE(1,0) 为何读同一个 `reg(1)`？这带来了什么好处？

**参考答案**：因为它们位于同一反对角线 \(i+j=1\)，按广播规则都取 `reg(1)`。好处是：反对角线上的 PE 在同一拍处理同一波前，本就该共享同一控制包；用一条移位线 + 对角线广播，\(2n-1\) 级寄存器就够驱动 \(n^2\) 个 PE，极大节省了控制寄存器与连线。

**练习 2**：`clct` 取自 `reg(2n-2).busy` 而不是 OR 出来的 `dat_clct`，二者语义有何不同？

**参考答案**：`dat_clct`（OR）是「是否还有波前在累加」，驱动收集器**逐拍**工作；`clct`（取自最末级 `busy`）是入口 `busy` 延迟 \(2n-2\) 拍的结果，代表「整批数据是否仍在阵列中处理（含排空）」，是一个更长的全局窗口标志，常用于告知外部「这一批 GEMM 还没结束」。

**练习 3**：如果把 `ORGate` 的输入漏接了 `cbus_in.keep`（只 OR 了 `reg(0..2n-3)`），会对流式归约造成什么影响？

**参考答案**：`dat_clct` 会比正确值**晚一拍**才升高（要等到第一波 keep 进入 `reg(0)` 后下一拍才被 OR 到），导致收集器在第一波最前面的若干输出上漏收/错位。这正说明把「当前输入」也纳入 OR 是必要的——它让收集窗口与喂入同步开始。

## 5. 综合实践

把本讲三个模块（ORGate、一维脉动移位、对角线广播）串起来，完成下面这个「源码阅读 + 仿真」小任务：

**任务**：为 `ControlUnit` 增加一条「`dat_clct` 下降沿即整批完成」的观察，验证它是 `busy` 的延迟版本。

**步骤**：

1. 阅读 [controlUnit.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala) 与 [CUSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/cu/CUSpec.scala)，确认：
   - `cbus_dat_clct = OR(cbus_in.keep, reg(0..2n-3).keep)`；
   - `clct = reg(2n-2).busy`。
2. 仿照 `CUSpec`，对 `new ControlUnit(4)`（即 \(n=4\)，\(2n-2=6\)）写一段仿真：连续 8 拍同时 `poke` `cbus_in.keep=1` 与 `cbus_in.busy=1`，之后两者都置 0；逐拍 `peek` `io.clct`。
3. 预测并核对：`io.clct` 应在 `busy` 输入**延迟 6 拍**后升高，并在最后一个 `busy=1` 输入延迟 6 拍后下降；`cbus_dat_clct` 则在第一次 `keep=1` 当拍就升高、在最后一次 `keep=1` 离开 `reg(2n-3)` 后下降。
4. 把观测到的 `clct` 与 `dat_clct` 高电平区间画成简易时序图，标注二者谁先升起、谁后落下，并解释差异（提示：`clct` 走最末级寄存器，`dat_clct` 走 OR 组合 + 后续 `pipe_dat_clct`）。

**预期结果**：`clct` 比 `dat_clct` 的有效窗口整体偏后（因为它取最末级、延迟更大），且二者都完整覆盖「喂入 + 排空」期。若你的环境没有 firtool/verilator，可只做纸面推导并标注「待本地验证」。

## 6. 本讲小结

- `ControlUnit` 让**控制信号也走脉动**：一条深度 \(2n-1\) 的一维移位寄存器 `reg`，`reg(k)` 存放 \(k\) 拍前的输入控制包。
- **对角线广播** `cbus_out(n*i+j) := reg(i+j)`：反对角线 \(i+j\) 上的 PE 共享同一级控制，使控制延迟量与数据波前延迟量相等，二者同拍到达同一 PE。
- **ORGate** 把「当前输入 + `reg(0..2n-3)`」共 \(2n-1\) 路 `keep` 做 OR，得到 `cbus_dat_clct`：只要任一波前还在累加就继续收集（OR = 存在性语义）。
- `clct` 与 `cbus_use_accum` 取自**最末级** `reg(2n-2)` 的 `busy / use_accum`，即把入口信号延迟 \(2n-2\) 拍，代表全局「整批是否仍在处理」。
- `ControlUnit` **只移位、不计算** `use_accum/busy`；它把控制复杂度吸收在自身，使 PE 侧只需一个简单的 `keep` 位（承接 u3-l1 的「译码层吸收复杂性、执行层保持简单」）。
- 在 `MMALU` 中，`cbus_out` 经 `pipe_ctrl`、`cbus_dat_clct` 经 `pipe_dat_clct` 各加一级流水，再分别喂给 PE 与 `DataCollector`，这是 200 MHz 时序收敛的一部分（详见 u4-l5）。

## 7. 下一步学习建议

- **u4-l5 MMALU 顶层集成与流式归约**：本讲的 `dat_clct / clct / cbus_out` 最终都汇入 `MMALU`，并与 `pipe_*` 流水寄存器、`DataCollector` 协同。建议接着读 `mma.scala` 看这些信号如何被「挂」成完整流水线，并理解 `ctrl.keep=true` 连续喂入如何实现 M×K 流式归约（端到端由 `MMALUStreamReduceSpec` 验证）。
- **u4-l3 数据收集器**：若想更清楚 `dat_clct` 这个「使能」下游如何驱动模 \(n\) 计数器逐列回收 PE 输出，可回看 `dataCollector.scala`。
- **u3-l1 控制契约**：若想追溯 `keep / use_accum / busy` 三位在译码器侧的来源（`keep` 复用 funct7 的 sat 位），可回看 `MMALUMicroCode.scala` 与 `instrDecoder.scala`。
