# 二维脉动阵列 SystolicArray2D

## 1. 本讲目标

本讲是「矩阵乘法引擎 MMALU」单元的第二讲，承接 [u4-l1 处理单元 PE](u4-l1-processing-element.md) 中单个 PE 的乘累加（MAC）行为，把视角从「一个 PE」提升到「n×n 个 PE 组成的阵列」。

学完本讲，读者应该能够：

1. 说清 `SystolicArray2D` 在整个 MMALU 数据通路中的位置——它夹在 `DataFeeder`（数据馈送器）和 `n×n` 个 PE 之间，只负责「把输入向量错拍地分发到每个 PE」。
2. 理解水平移位寄存器 `reg_h` 与垂直移位寄存器 `reg_v` 如何实现数据的脉动（systolic）传播：`vec_a` 从左侧水平流入、`vec_b` 从顶部垂直流入。
3. 手推 n=2 时前两拍数据在阵列里的位置，并解释「为什么处于同一反对角线（i+j 相同）的 PE 会在同一拍拿到同一组输入」——这正是脉动阵列能用 O(n) 拍完成 O(n³) 次乘加的关键。
4. 把阵列的「数据扭曲（skew）」延迟 \(n-1\) 拍，和整条 MMALU 流水线的总延迟 \(3n-2\)（加流水寄存器后为 \(3n-1\)）对应起来。

---

## 2. 前置知识

在阅读本讲前，请确认你已经理解以下概念（均来自前置讲义）：

- **PE 的乘累加语义**：每个 PE 内部有一个累加器寄存器 `res`，每拍计算 `in_a * in_b`；`keep=true` 时 `res := res + in_a*in_b`（累加），`keep=false` 时 `res := in_a*in_b`（覆盖）。详见 [u4-l1](u4-l1-processing-element.md)。本讲只关心 `in_a`/`in_b`「从哪里来、何时到达」，不关心 PE 内部如何累加。
- **全局参数 N/L/K**：`n` 是脉动阵列的边长（等于 K，等于 PE 的 `nbits` 的 N），`nbits` 是单个数据元素的位宽（默认 8）。详见 [u1-l4](u1-l4-params-nlk-and-regclass.md)。
- **什么是脉动阵列（直觉）**：传统做法是把整个矩阵一次性「摊开」接到 n×n 个乘法器上，布线爆炸；脉动阵列的做法是只从阵列的两条边（左、上）喂入向量，让数据像心脏跳动（systole）一样一拍一拍地在阵列内部「流动」，在流动过程中被复用，从而用很少的输入端口喂饱 n² 个计算单元。

> 术语提示：**systolic**（脉动）借用自医学「心脏收缩」，强调数据像血液一样按节拍流动；**skew/扭曲** 指把矩阵的各行/列在时间上错开，使它们能在不同拍到达同一个 PE；**wavefront/波前** 指同一拍内在阵列里推进的「一层」数据。

---

## 3. 本讲源码地图

本讲涉及的源码文件如下：

| 文件 | 作用 | 本讲用到哪部分 |
|:---|:---|:---|
| [src/main/scala/alu/mma/sa/systolicArray.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala) | **本讲主角**：定义 `SystolicArray2D`，即 n×n 脉动阵列的数据分发网络。 | 全文（仅 45 行） |
| [src/main/scala/alu/mma/mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala) | MMALU 顶层，把 `DataFeeder`、`SystolicArray2D`、`n×n` 个 PE、`ControlUnit`、`DataCollector` 连起来。 | 看 SA 如何被实例化与连线（L68–L84） |
| [src/main/scala/alu/mma/sa/dataFeeder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala) | 数据馈送器，负责在喂入 SA **之前** 对输入做时间扭曲。 | 理解 `vec_a`/`vec_b` 为何已经是「错拍」的（L21–L53） |
| [docs/implementations/SystolicArray.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md) | 设计文档：功能模型、bubble、4×4 时序、M×K 流式归约。 | 时序与延迟的权威说明 |
| [src/test/scala/alu/mma/sa/DataFeederSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala) | 用 `EphemeralSimulator` 验证馈送器扭曲模式的测试，可作为写 SA 小测试的范本。 | 测试写法（poke/peek 模式） |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 脉动阵列的核心思想与 `SystolicArray2D` 顶层结构**：它是什么、IO 长什么样、在 MMALU 里的位置。
- **4.2 水平/垂直移位寄存器 `reg_h` / `reg_v`**：数据如何在阵列内部一拍一拍流动。
- **4.3 边界输入逻辑与对角线波前**：`vec_a`/`vec_b` 从哪两条边进入，为什么反对角线 PE 同拍拿到同一组输入。

### 4.1 脉动阵列的核心思想与 SystolicArray2D 顶层结构

#### 4.1.1 概念说明

矩阵乘法 \(C = B^{\mathsf T} A\)（其中 \(A, B \in \mathbb{Z}^{M\times K}\)）需要计算大量形如 \(A_{m,i}\cdot B_{m,j}\) 的乘积并按 \(m\) 求和。如果用「一个乘法器」串行算，时间是 \(O(MK^2)\)；如果用「\(K\times K\) 个独立乘法器」但每个都单独接输入，输入端口数是 \(O(K^2)\)，布线不可行。

脉动阵列是这两者的折中：仍然摆放 \(K\times K\)（本项目中即 \(n\times n\)）个 PE，但**只从阵列的左侧和顶部各喂 n 个输入**，让数据在阵列内部通过移位寄存器**逐拍流动并复用**。这样输入端口只有 \(O(n)\) 个，却能驱动 \(O(n^2)\) 个 PE，每个数据元素在被读入后会被沿途的多个 PE 使用，从而逼近 \(O(n^3)\) 次乘加只需 \(O(n)\) 拍 feed 的理想效率。

> 注意职责边界：`SystolicArray2D` **本身不做任何乘法或累加**。它纯粹是一个「数据分发网络」——把 `vec_a`（左输入）和 `vec_b`（顶输入）错拍地送到 n×n 个 PE 的 `in_a`/`in_b` 端口。真正的乘累加发生在 PE 内部（[u4-l1](u4-l1-processing-element.md)）。

#### 4.1.2 核心流程

从数据的角度，`SystolicArray2D` 做三件事：

1. **接收**：每拍从左侧接收长度为 n 的向量 `vec_a`，从顶部接收长度为 n 的向量 `vec_b`。
2. **错拍分发**：把 `vec_a` 的每个元素沿**水平方向**逐拍右移（经 `reg_h`），把 `vec_b` 的每个元素沿**垂直方向**逐拍下移（经 `reg_v`）。
3. **输出**：产生 `out_a`（长度 \(n^2\)）和 `out_b`（长度 \(n^2\)），分别接到 n×n 个 PE 的 `in_a` 与 `in_b`。

伪代码（仅示意，非项目代码）：

```text
每拍：
  for 每个 PE(i, j) in n×n:
      PE(i,j).in_a = vec_a(i) 经过 j 拍水平移位后的值
      PE(i,j).in_b = vec_b(j) 经过 i 拍垂直移位后的值
```

关键直觉：到达 `PE(i,j)` 的 `in_a` 比原始 `vec_a(i)` 晚了 j 拍（要穿过 j 个水平寄存器），`in_b` 比原始 `vec_b(j)` 晚了 i 拍（要穿过 i 个垂直寄存器）。这种「位置越深、延迟越大」的错拍，正是波前对齐的基础（详见 4.3）。

#### 4.1.3 源码精读

先看 `SystolicArray2D` 的类定义与 IO：

[systolicArray.scala:L10-L16](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L10-L16) —— 定义一个参数化模块，默认 `n=8`、`nbits=8`；输入是两个长度为 n 的有符号向量 `vec_a`/`vec_b`，输出是两个长度为 \(n^2\) 的向量 `out_a`/`out_b`。注意输入向量元素是 `SInt(nbits.W)`，与 PE 的 `in_a`/`in_b` 位宽一致。

再看它在 MMALU 顶层里是如何被实例化与连线的：

[mma.scala:L68-L70](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L68-L70) —— `val sarray = Module(new sa.SystolicArray2D(n, nbits))`，`vec_a`/`vec_b` 直接接自 `DataFeeder` 的输出 `reg_a_out`/`reg_b_out`。这说明：**进入 SA 的 `vec_a`/`vec_b` 已经被 DataFeeder 预先扭曲过**（见 4.3.3），SA 的移位寄存器只是把这种扭曲继续向阵列深处推进。

[mma.scala:L74-L84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L74-L84) —— 双重循环把 `out_a(n*i+j)` / `out_b(n*i+j)` 经一级流水寄存器 `pipe_a`/`pipe_b` 接到第 `(i,j)` 个 PE 的 `in_a`/`in_b`。即 PE 在阵列中的二维坐标 `(i,j)` 与平坦索引 `n*i+j` 一一对应：`out_a(n*i+j) → PE(i,j).in_a`。

#### 4.1.4 代码实践

**实践目标**：不读循环细节，仅凭 IO 与 mma.scala 的连线，推出 n=2 时 4 个 PE 各自的 `in_a`/`in_b` 取自哪个信号，建立「二维坐标 ↔ 平坦索引 ↔ 信号来源」的映射表。

**操作步骤**：

1. 打开 [mma.scala:L74-L84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L74-L84)，确认 `PE(i,j)` 的平坦索引是 `n*i+j`。
2. 打开 [systolicArray.scala:L10-L16](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L10-L16)，确认 `out_a`/`out_b` 长度都是 \(n^2=4\)。
3. 仿照下表，把「PE 位置 / 平坦索引 / 取自 out_a / 取自 out_b」四列填出来（先不关心 out_a/out_b 内部从哪来，那是 4.2 的事）。

**预期结果**（映射表，待 4.2 填上信号来源后即为完整连线表）：

| PE 位置 (i,j) | 平坦索引 `n*i+j` | `in_a` 取自 | `in_b` 取自 |
|:---:|:---:|:---|:---|
| (0,0) | 0 | `out_a(0)` | `out_b(0)` |
| (0,1) | 1 | `out_a(1)` | `out_b(1)` |
| (1,0) | 2 | `out_a(2)` | `out_b(0)` 的同列… 即 `out_b(2)` |
| (1,1) | 3 | `out_a(3)` | `out_b(3)` |

> 提示：`out_a` 与 `out_b` 共享同一套平坦索引 `n*i+j`，所以 `PE(i,j)` 同时消费 `out_a(n*i+j)` 与 `out_b(n*i+j)`，二者是一一配对的。

#### 4.1.5 小练习与答案

**练习 1**：`SystolicArray2D` 的输出为什么是长度 \(n^2\) 的向量，而不是 \(n\times n\) 的二维数组？

**参考答案**：Chisel 的硬件 `Vec` 本质是一维的；`n*n` 个端口用一维 `Vec(n*n, ...)` 表达最直接，二维坐标 `(i,j)` 通过 `n*i+j` 折算成一维下标。这也和 PE 用 `Seq.fill(n*n)` 平坦摆放一致（见 [mma.scala:L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L34)）。

**练习 2**：如果把 `n` 从 8 改成 32，`out_a` 的长度、以及需要被驱动的 PE 数量分别变成多少？

**参考答案**：`out_a` 长度 \(n^2=1024\)；PE 数量也是 \(n^2=1024\)。这正是「改参数即改 Verilog 规模」（[u1-l3](u1-l3-source-layout-and-top.md)）在阵列层的体现。

---

### 4.2 水平/垂直移位寄存器 reg_h / reg_v

#### 4.2.1 概念说明

`reg_h` 和 `reg_v` 是两排**移位寄存器**（shift register），是实现「数据脉动流动」的物理载体：

- `reg_h`（horizontal）：水平方向，把 `vec_a` 从阵列**左侧**向右逐列传递。
- `reg_v`（vertical）：垂直方向，把 `vec_b` 从阵列**顶部**向下逐行传递。

它们都是 `RegInit`（带复位初值 0 的寄存器），所以数据每穿过一级就**延迟一拍**。阵列越深（`i` 或 `j` 越大），数据到达得越晚——这正是「错拍」的来源。

#### 4.2.2 核心流程

两排寄存器的规模都是 \((n-1)\times n\)：

- 长度 \((n-1)\times n\)，因为只有「内部」的 \(n-1\) 行/列需要寄存器来传递数据；最外侧那一行/列直接接输入，不需要寄存。
- 对 n=8：各有 \(7\times 8=56\) 个寄存器；对 n=32：各有 \(31\times 32=992\) 个。

数据流动的节拍（以 n=4 为例，对应设计文档的 4×4 时序）：

```text
t=0: vec_a(0) 进入 → 只有 PE(*,0) 的最左列能立刻拿到（j=0 无水平延迟）
t=1: vec_a(0) 被 reg_h 推到第 1 列 → PE(*,1) 拿到（延迟 1 拍）
t=2: 推到第 2 列 → PE(*,2) 拿到（延迟 2 拍）
t=3: 推到第 3 列 → PE(*,3) 拿到（延迟 3 拍 = n-1）
```

因此数据从进入阵列到到达最远的 `PE(n-1,n-1)`，要经过 \(n-1\) 级水平 + \(n-1\) 级垂直寄存器中较长的那条路径（二者并行，所以仍是 \(n-1\) 拍的传播延迟）。这与设计文档给出的「整条 MMALU 流水线延迟 \(3n-2\)」（[SystolicArray.md:L5](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md#L5)）是一致的：\(3n-2\) = feed 的 \(n\) 拍 + SA 内传播 + collect 的收集开销；插入 SA→PE 流水寄存器后变成 \(3n-1\)（见 [mma.scala:L13-L22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L13-L22) 的时序注释）。

#### 4.2.3 源码精读

寄存器声明：

[systolicArray.scala:L18-L20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L18-L20) —— `reg_h` 与 `reg_v` 都是 `RegInit(VecInit(Seq.fill((n - 1) * n)(0.S(nbits.W))))`，即各有 \((n-1)\times n\) 个 `SInt(nbits.W)` 寄存器，初值为 0。

整个分发逻辑藏在一个双重 `for` 循环里（[systolicArray.scala:L22-L44](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L22-L44)）。循环对每个 `(i,j)` 同时处理垂直和水平两路。以**水平路（`out_a`）**为例：

[systolicArray.scala:L36-L42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L36-L42) —— 读取侧（组合）：

- `j==0`（最左列）：`out_a(n*i) := vec_a(i)`，直接取左侧输入，不经寄存器。
- `j>0`（内部列）：`out_a(n*i+j) := reg_h((n-1)*i + (j-1))`，取上一级水平寄存器的值。

写入侧（寄存器更新，下一拍生效）：

- `if (i < n && j < n-1) reg_h((n-1)*i + j) := out_a(n*i+j)`，即**只有 `j < n-1` 的列**才把当前 `out_a` 写进下一级 `reg_h`；最右列（`j=n-1`）的数据不再向右传（已经到边了）。

垂直路（`out_b`）是完全对称的镜像（[systolicArray.scala:L27-L33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L27-L33)），只是把 `i`/`j`、`vec_a`/`vec_b`、`reg_h`/`reg_v` 角色互换：`i==0` 直接取顶部输入，`i>0` 取 `reg_v`，且只有 `i<n-1` 的行才向下传。

> 小贴士：这段循环用 `if` 在 Scala 层做**展开期**判断（elaboration-time），不是硬件 `when`。所以 `i==0` / `j==0` 这些分支在生成的 Verilog 里是「定死」的连线，不会变成运行时多路选择。每个 `(i,j)` 根据自己的坐标被静态地连到输入或某一级寄存器。

#### 4.2.4 代码实践

**实践目标**：手推 n=2 时 `reg_h`/`reg_v` 在前两拍的更新值，验证「寄存器 = 上一拍的输入」。

**操作步骤**：

1. 对 n=2，确认 `reg_h`、`reg_v` 各有 \((2-1)\times 2=2\) 个寄存器：`reg_h(0)`、`reg_h(1)`、`reg_v(0)`、`reg_v(1)`。
2. 由 4.2.3 的写入规则：`j<n-1`（即 j=0）时 `reg_h((n-1)*i+j) := out_a(n*i+j)`，而 `out_a(n*i) := vec_a(i)`，所以 `reg_h((n-1)*i) := vec_a(i)`。对 i=0,1 得到：`reg_h(0) ← vec_a(0)`、`reg_h(1) ← vec_a(1)`。
3. 同理垂直：`reg_v(0) ← vec_b(0)`、`reg_v(1) ← vec_b(1)`。
4. 设两拍输入为：t0 `vec_a=[1,2], vec_b=[3,4]`；t1 `vec_a=[5,6], vec_b=[7,8]`。填写下表。

**预期结果**（每拍的「当前寄存器值」是该拍开始时寄存器持有的值，等于**上一拍**的输入）：

| 拍 | `reg_h(0),reg_h(1)` 当前值 | `reg_v(0),reg_v(1)` 当前值 | 说明 |
|:---:|:---|:---|:---|
| t0 | 0, 0（复位初值） | 0, 0 | 第一拍，寄存器还没存过数据 |
| t1 | 1, 2（=t0 的 vec_a） | 3, 4（=t0 的 vec_b） | t0 的输入被锁进寄存器 |
| t2 | 5, 6（=t1 的 vec_a） | 7, 8（=t1 的 vec_b） | t1 的输入被锁进寄存器 |

**可验证（可选，示例代码）**：参考 [DataFeederSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala) 的写法，用 `EphemeralSimulator` 写一个针对 `SystolicArray2D(n=2)` 的最小测试，poke `vec_a`/`vec_b`，`clock.step()` 后 peek `out_a`/`out_b` 的 `litValue`，核对你的手推表。下面是一段示例骨架（**示例代码，仓库中并不存在此文件**）：

```scala
// 示例代码：可选练习，自行新建到 src/test/scala/alu/mma/sa/SystolicArray2DSpec.scala
package alu.mma.sa
import chisel3._
import chisel3.simulator.EphemeralSimulator._
import org.scalatest.flatspec.AnyFlatSpec

class SystolicArray2DSpec extends AnyFlatSpec {
  "SystolicArray2D n=2" should "shift vec_a/vec_b by one cycle" in {
    simulate(new SystolicArray2D(2, 8)) { dut =>
      // t0: 用正数标识符，便于 litValue 直接解读（避开 SInt 负数位模式歧义）
      dut.io.vec_a(0).poke(1); dut.io.vec_a(1).poke(2)
      dut.io.vec_b(0).poke(3); dut.io.vec_b(1).poke(4)
      dut.io.out_a(1).expect(0)   // reg_h(0) 仍为复位值 0
      dut.clock.step()
      // t1: out_a(1)=reg_h(0)=上一拍的 vec_a(0)=1
      dut.io.out_a(1).expect(1)
    }
  }
}
```

> 运行此测试需在容器内 `sbt test`（[u1-l2](u1-l2-build-and-run.md)）。若暂时不便运行，上面的手推表本身即为「源码阅读型实践」的结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reg_h` 的长度是 \((n-1)\times n\) 而不是 \(n\times n\)？

**参考答案**：最左列（`j=0`）的 PE 直接接 `vec_a`，不需要水平寄存器暂存；只有 `j=1..n-1` 这 \(n-1\) 个内部列需要从上一级寄存器取值。每一行（共 n 行）都有 \(n-1\) 个这样的内部列，所以是 \((n-1)\times n\)。

**练习 2**：循环里水平写入的守卫是 `if (i < n && j < n - 1)`，垂直写入是 `if (i < n - 1 && j < n)`。为什么最右列（`j=n-1`）和最底行（`i=n-1`）不再写寄存器？

**参考答案**：最右列/最底行已经是阵列边缘，数据传到那里后没有「下一级」PE 需要再接收，再写一份寄存器既无消费者也浪费面积。这也保证 `reg_h`/`reg_v` 不会出现「写了没人读」的悬挂寄存器。

---

### 4.3 边界输入逻辑与对角线波前

#### 4.3.1 概念说明

4.2 讲清楚了「数据怎么流」，本节回答两个更深层的问题：

1. **`vec_a`/`vec_b` 分别从哪条边进入？** `vec_a` 是左输入（水平），`vec_b` 是顶输入（垂直）。这条约定写死在 [systolicArray.scala:L12-L13](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L12-L13) 的注释里。
2. **为什么反对角线（i+j 相同）的 PE 会在同一拍拿到「同一组」输入？** 这是脉动阵列最关键的特性，称为**波前（wavefront）对齐**。它保证了：同一个乘积项 \(A_{m,i}\cdot B_{m,j}\) 的两个操作数，虽然分别走水平路和垂直路、路径长度不同，却能在同一拍抵达同一个 PE，从而被正确地乘在一起并累加。

#### 4.3.2 核心流程

把「到达 `PE(i,j)` 的总延迟」写成公式。一条数据要到达 `PE(i,j)`，水平路（`in_a`）与垂直路（`in_b`）的延迟分别为：

\[
\text{delay}_a(i,j) = \underbrace{i}_{\text{DataFeeder 对 lane }i\text{ 的预扭曲}} + \underbrace{j}_{\text{SA 内 }j\text{ 级 reg\_h}}
\]

\[
\text{delay}_b(i,j) = \underbrace{j}_{\text{DataFeeder 对 lane }j\text{ 的预扭曲}} + \underbrace{i}_{\text{SA 内 }i\text{ 级 reg\_v}}
\]

二者都等于 \(i+j\)！这就是对齐的数学根源：**同一个波前 m 的两个操作数，无论走哪条路，到 `PE(i,j)` 的总延迟都是 \(i+j\)**，因此必然同拍到达。于是波前 m 抵达 `PE(i,j)` 的物理拍号为：

\[
t = m + i + j
\]

凡 \(i+j\) 相同的 PE（即同一条反对角线），都会在**同一拍** \(t\) 处理同一个波前 m。这就是「对角线 PE 在同一拍拿到同一组输入」。

> 关键提醒：上式里的「\(i\) 级 DataFeeder 预扭曲」**不是 `SystolicArray2D` 自己做的**，而是上游 `DataFeeder` 做的。`SystolicArray2D` 单独看，只贡献了 \(j\)（水平）和 \(i\)（垂直）这部分延迟。波前对齐是 DataFeeder + SA **两者合起来**才成立的性质。这也是为什么 SA 的 `vec_a`/`vec_b` 必须接自 DataFeeder 的输出（[mma.scala:L69-L70](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L69-L70)），而不是直接接寄存器堆原始向量。

#### 4.3.3 源码精读

边界输入（哪条边、怎么接）：

[systolicArray.scala:L12-L13](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L12-L13) —— 注释明确：`vec_a` 是 left input（左输入），`vec_b` 是 top input（顶输入）。

[systolicArray.scala:L27-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L27-L31) —— 垂直边界：`i==0`（最顶行）的 `out_b(j) := vec_b(j)`，直接接顶输入；`i>0` 才取 `reg_v`。即 `vec_b` 从**顶行**进入，逐行下传。

[systolicArray.scala:L36-L39](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L36-L39) —— 水平边界：`j==0`（最左列）的 `out_a(n*i) := vec_a(i)`，直接接左输入；`j>0` 才取 `reg_h`。即 `vec_a` 从**左列**进入，逐列右传。

DataFeeder 的预扭曲（理解波前对齐为何需要它）：

[dataFeeder.scala:L21-L22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L21-L22) —— `buffer_a(i-1)` 是深度为 `i` 的 `Pipe`（`i` 从 1 到 n-1），即 `reg_a_out(i)` 比 `reg_a_in(i)` 延迟 i 拍。所以 lane 越靠右（i 越大），DataFeeder 给它的预延迟越大——这正是 4.3.2 公式里那个 \(i\) 级预扭曲的来源。

[dataFeeder.scala:L42-L53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L42-L53) —— 注释称之为 chainsaw layout（链锯布局），形象地描述了「各 lane 延迟递增、形成锯齿状时序」的扭曲方式。

#### 4.3.4 代码实践

**实践目标**：手推 n=2 的 `SystolicArray2D` 前两拍，验证反对角线 `PE(0,1)` 与 `PE(1,0)` 在 t1 同拍处理同一个波前 m=0。这是本讲的核心实践。

**操作步骤**：

1. 设两个 2×2 输入矩阵（行 = 波前 m）：\(A=\begin{bmatrix}A_{00} & A_{01}\\ A_{10} & A_{11}\end{bmatrix}\)、\(B=\begin{bmatrix}B_{00} & B_{01}\\ B_{10} & B_{11}\end{bmatrix}\)。其中 `vec_a(i)` 应携带 A 的第 i 列、`vec_b(j)` 携带 B 的第 j 列。
2. 由于 DataFeeder 会把 lane i 预延迟 i 拍，**进入 SA 的 `vec_a`/`vec_b` 已经是扭曲后的**。按 DataFeeder 的扭曲规则推算 SA 输入：
   - t0：`vec_a=[A00, 0]`、`vec_b=[B00, 0]`
   - t1：`vec_a=[A10, A01]`、`vec_b=[B10, B01]`
   - t2：`vec_a=[0, A11]`、`vec_b=[0, B11]`
3. 用 4.2.3 推出的 n=2 连线表（`out_a(0)=vec_a(0)`、`out_a(1)=reg_h(0)`、`out_a(2)=vec_a(1)`、`out_a(3)=reg_h(1)`；`out_b` 对称），逐拍填出 4 个 PE 的 `(in_a, in_b)`。
4. 标出每个 PE 在 t1 拿到的是哪个波前 m 的数据，观察反对角线。

**预期结果**：

t0（寄存器初值 0）：

| PE | in_a | in_b | 波前 |
|:---:|:---:|:---:|:---:|
| (0,0) | A00 | B00 | m=0 ✓ 两个操作数齐 |
| (0,1) | reg_h(0)=0 | B01 | — |
| (1,0) | A01 | reg_v(0)=0 | — |
| (1,1) | 0 | 0 | — |

> t0 末，寄存器锁存：`reg_h(0)=A00`、`reg_v(0)=B00`。

t1（寄存器已持有 t0 的输入）：

| PE | in_a | in_b | 波前 |
|:---:|:---:|:---:|:---:|
| (0,0) | A10 | B10 | m=1 ✓ |
| **(0,1)** | **reg_h(0)=A00** | **B01** | **m=0 ✓** |
| **(1,0)** | **A01** | **reg_v(0)=B00** | **m=0 ✓** |
| (1,1) | reg_h(1)=0 | reg_v(1)=0 | — |

**观察现象**：在 t1，`PE(0,1)` 与 `PE(1,0)`（它们位于同一条反对角线 \(i+j=1\)）**同时**处理波前 m=0 的数据——一个拿到 \(A_{00}\cdot B_{01}\)，另一个拿到 \(A_{01}\cdot B_{00}\)。而位于反对角线 \(i+j=0\) 的 `PE(0,0)` 已经超前一步在处理 m=1。

**结论（为何对角线 PE 同拍拿到同一组输入）**：由 4.3.2 的公式 \(t=m+i+j\)，波前 m 抵达 `PE(i,j)` 的拍号只取决于 \(i+j\)。所以同一反对角线（\(i+j\) 相同）上的所有 PE，必然在同一拍处理同一个 m；不同反对角线则错开一拍，形成一条沿阵列对角线推进的「计算波前」。这就是脉动阵列能用 O(n) 拍 feed 完成 O(n³) 乘加、且每个输入元素被多个 PE 复用的根本原因。

> 待本地验证：上面的扭曲输入与拍号是依据 DataFeeder 的 `Pipe` 深度规则（[dataFeeder.scala:L21-L22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L21-L22)）与 SA 的移位规则手推得到的。若要 100% 确认，可在容器内运行 4.2.4 的示例测试或 [DataFeederSpec](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala)，对照 peek 出的逐拍 `out_a`/`out_b` 值。

#### 4.3.5 小练习与答案

**练习 1**：对 n=4 的阵列，波前 m=0 最先到达哪个 PE？最晚到达哪个 PE？分别在第几拍？

**参考答案**：由 \(t=m+i+j\)，m=0 最先到达 `PE(0,0)`（\(i+j=0\)，t=0）；最晚到达 `PE(3,3)`（\(i+j=6\)，t=6=n+n-2）。这与设计文档「4×4 阵列延迟 \(3\times4-2=10\)」（[SystolicArray.md:L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md#L31)）一致——最后一个结果元素要等数据传到 `PE(3,3)` 再经 collect 收集。

**练习 2**：如果绕过 DataFeeder，把寄存器堆的原始向量**直接**接到 SA 的 `vec_a`/`vec_b`（即不做 lane 预扭曲），波前还能对齐吗？

**参考答案**：不能。此时 `in_a` 到 `PE(i,j)` 只剩 j 级延迟、`in_b` 只剩 i 级延迟，二者不再相等（除非 i=j），同一个 m 的两个操作数会错拍到达 PE，乘出来的就是错配的乘积项。波前对齐**依赖** DataFeeder 的预扭曲补上差额，使两路总延迟都等于 \(i+j\)。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的小任务。

**任务**：以 n=2 为例，画出一张「从 `DataFeeder.reg_a_in`/`reg_b_in` 到 4 个 PE 的 `in_a`/`in_b`」的完整数据流图，标注前两拍每一级信号的值，并在图上用同一种颜色标出 t1 拍同时处理波前 m=0 的两个反对角线 PE。

**建议步骤**：

1. **左侧（DataFeeder 扭曲）**：画出 `reg_a_in(i)` 经深度为 i 的 `Pipe` 变成 `reg_a_out(i)` 的链锯布局（参考 [dataFeeder.scala:L42-L53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L42-L53)）。
2. **中间（SA 边界）**：画出 `vec_a` 从左、`vec_b` 从顶进入 2×2 阵列的边界（参考 [systolicArray.scala:L27-L39](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/systolicArray.scala#L27-L39)）。
3. **阵列内部（移位寄存器）**：画出 `reg_h(0)`、`reg_h(1)`（水平）与 `reg_v(0)`、`reg_v(1)`（垂直）的位置，以及它们如何把数据从 `(0,j)` 传到 `(1,j)`、从 `(i,0)` 传到 `(i,1)`（参考 4.2.4 的连线表）。
4. **右侧（PE）**：标出 4 个 PE，填入 4.3.4 推出的 t0、t1 两拍的 `(in_a, in_b)` 值。
5. **高亮**：把 t1 拍的 `PE(0,1)` 与 `PE(1,0)` 涂同色，旁边注一句「\(i+j=1\)，同拍处理 m=0」。
6. **自检**：在图上数一下，一个输入元素（例如 `A00`）从 t0 进入后，在 t0 被 `PE(0,0)` 用、在 t1 被 `PE(0,1)` 用——确认它被**两个** PE 复用了，这就是「数据复用」的直观证据。

**预期成果**：一张能解释「为什么 n 个输入端口能喂饱 n² 个 PE」的图，以及一句话总结——「波前按反对角线逐拍推进，每个输入元素在流经阵列时被沿途多个 PE 复用」。

> 进阶（可选）：把图里 t0/t1 的值改成随机 INT8，对照 [MMALUSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/MMALUSpec.scala) 的期望矩阵计算方式，验证你手推的 PE 输入确实能乘加出正确的矩阵积。

---

## 6. 本讲小结

- `SystolicArray2D` 是一个**纯数据分发网络**，不做乘加：它把左输入 `vec_a` 和顶输入 `vec_b` 错拍地送到 n×n 个 PE 的 `in_a`/`in_b`。
- 数据流动靠两排移位寄存器：`reg_h`（水平，规模 \((n-1)\times n\)）把 `vec_a` 从左向右逐列传，`reg_v`（垂直，同规模）把 `vec_b` 从上向下逐行传；每穿一级延迟一拍。
- `vec_a` 从**最左列**进入（`j==0` 直连）、`vec_b` 从**最顶行**进入（`i==0` 直连）；内部列/行才从 `reg_h`/`reg_v` 取值，且最右列、最底行不再向下/向右写寄存器。
- 到达 `PE(i,j)` 的总延迟，水平路与垂直路都等于 \(i+j\)（其中 DataFeeder 贡献 lane 索引那部分、SA 贡献坐标那部分），所以波前 m 在拍 \(t=m+i+j\) 到达——**同一反对角线的 PE 在同一拍处理同一个波前**。
- 这种对角线波前推进使每个输入元素被多个 PE 复用，从而以 O(n) 个输入端口、O(n) 拍 feed 驱动 n² 个 PE 完成 O(n³) 次乘加。
- SA 内部最长传播路径为 \(n-1\) 拍，与 feed/collect 合起来构成 MMALU 的 \(3n-2\)（加 SA→PE 流水寄存器后 \(3n-1\)）总延迟。

---

## 7. 下一步学习建议

本讲只解决了「数据如何分发到 PE」，还没有讲「PE 的输出如何被收回来」以及「控制信号如何同步到达」。建议按以下顺序继续：

1. **[u4-l3 数据馈送器与收集器](u4-l3-data-feeder-collector.md)**：精读 `DataFeeder`（本讲多次提到的预扭曲来源）与 `DataCollector`，把数据的「进」和「出」补齐，理解 `in_accum`/`use_accum`/`dat_clct` 的收集时机。
2. **[u4-l4 控制单元 ControlUnit](u4-l4-mma-control-unit.md)**：看 `keep`/`use_accum` 等控制信号本身是如何也走一条「一维脉动」通路、按对角线广播到 n×n 个 PE 的——你会发现控制通路与本讲的数据通路是同构的。
3. **[u4-l5 MMALU 顶层集成与流式归约](u4-l5-mma-top-and-streaming.md)**：回到 [mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala)，把 SA、PE、Feeder、Collector、ControlUnit 五件套拼成完整的矩阵乘引擎，并理解 `ctrl.keep=true` 如何实现 M×K 流式归约。
4. 阅读设计文档 [SystolicArray.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md) 的「Timing of systolic array: 4×4」「M×K Streaming Reduction」两节，用更大的 n=4 例子巩固本讲的对角线波前直觉。
