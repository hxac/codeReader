# 数据馈送器与收集器

## 1. 本讲目标

本讲聚焦 MMALU 中夹在「脉动阵列」与「外部接口」之间的两个数据搬运模块:`DataFeeder`(数据馈送器)与 `DataCollector`(数据收集器)。它们本身不做任何乘加运算,却决定了「寄存器向量如何被排布进 n×n 阵列」「n×n 个 PE 的输出如何被回收成 n 个结果」「累加器(偏置)在什么时机被加上去」。

学完本讲你应当能够:

- 说出 `DataFeeder` 如何用「阶梯型(skew)延迟」把一列列喂入的向量扭曲成脉动阵列需要的反对角波前。
- 说出 `DataCollector` 如何用一个模 n 计数器加「反向阶梯延迟」把 n×n 个 PE 输出回收成对齐的 n 个结果。
- 追踪累加器路径:`in_accum` 如何穿过 `DataFeeder` 的统一长延迟、MMALU 的 1 拍流水寄存器,最终在 `DataCollector` 里被 `use_accum` 选通加到结果上。
- 解释 `dat_clct`(收集使能)为何来自 `ControlUnit` 对所有 keep 信号的 OR,以及它为何决定了「何时把 PE 输出写成结果」。
- 参考现有 `DataCollectorSpec` / `DataFeederSpec`,设计一个最小仿真用例验证收集器的输出顺序。

## 2. 前置知识

本讲假设你已经学过 **u4-l1(处理单元 PE)** 与 **u4-l2(二维脉动阵列 SystolicArray2D)**,并记得以下几点:

- **PE** 是带累加器寄存器的乘累加单元:`keep=true` 时 `res := res + in_a*in_b`,否则覆盖。阵列里有 n×n 个 PE。
- **SystolicArray2D** 用 `reg_h`(水平)、`reg_v`(垂直)两组移位寄存器把 `vec_a` 从左、`vec_b` 从顶送入每个 PE,使波前 `m` 在第 `m+i+j` 拍抵达 `PE(i,j)`。它内部最长传播 n−1 拍。
- **Pipe 原语**:`chisel3.util.Pipe(gen, depth)` 是深度为 `depth` 的移位寄存器链,把输入延迟 `depth` 个时钟周期后输出(`depth=1` 即一个寄存器)。
- 全局参数 **N(bits)=8、K(通道数,在阵列里记作 n)、L(寄存器数)** 的含义见 u1-l4;本讲里 `n` 就是阵列边长(测试态常用 4 或 8,上板 32/64)。

> 术语提示:**skew / 扭曲 / 阶梯延迟** 指给不同数据通道施加递增的延迟,使原本同时到达的数据错拍;**chainsaw layout(链锯布局)** 是本仓库源码注释里对这种「逐 lane 递增延迟」布线的称呼,因为延迟量像链锯齿一样逐级上升。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/main/scala/alu/mma/sa/dataFeeder.scala` | `DataFeeder`:把外部一列列喂入的 `reg_a_in/reg_b_in` 阶梯扭曲成阵列输入 `reg_a_out/reg_b_out`;并把 `reg_accum_in` 统一长延迟成 `reg_accum_out`。 |
| `src/main/scala/alu/mma/sa/dataCollector.scala` | `DataCollector`:用模 n 计数器与反向阶梯延迟,把 n×n 个 PE 输出 `reg_in` 回收成对齐的 n 个 `reg_out`,并按 `use_accum` 选通加上 `accum_in`。 |
| `src/main/scala/alu/mma/mma.scala` | `MMALU` 顶层:实例化 `DataFeeder`、`SystolicArray2D`、PE、`ControlUnit`、`DataCollector` 并连线;累加器路径在此多一拍 `pipe_accum`。 |
| `src/main/scala/alu/mma/cu/controlUnit.scala` | `ControlUnit`:`cbus_dat_clct`(OR 全体 keep)、`cbus_use_accum`、`clct` 都由它产生,是收集器的控制源。 |
| `src/test/scala/alu/mma/sa/DataFeederSpec.scala` | 验证馈送器扭曲后输出等于反对角模式,并验证累加器在 `2n−2` 拍后才出现。 |
| `src/test/scala/alu/mma/sa/DataCollectorSpec.scala` | 验证收集器 `n−1` 拍启动后,每拍吐出一个对齐的 n 元结果列。 |

数据流全景(本讲关心的粗框部分):

```
        reg_a_in ─┐
        reg_b_in ─┤  DataFeeder (阶梯 skew)  ──>  reg_a_out / reg_b_out  ──>  SystolicArray2D ──> out_a/out_b ──> PE(n×n)
        reg_accum_in ─┘                              │                                                                  │
                                                     └─(统一 2n-1 延迟)─> reg_accum_out ─> pipe_accum ─> accum_in ─┐        │
                                                                                                                    ▼        ▼
dat_clct, use_accum ──(ControlUnit)──> DataCollector : reg_in(n×n PE 输出) + accum_in ──(反向 skew)──> reg_out(n 个结果)
```

## 4. 核心概念与源码讲解

### 4.1 DataFeeder:把寄存器向量排布进阵列

#### 4.1.1 概念说明

`SystolicArray2D` 要求每个 PE(i,j) 在**恰好第 i+j 拍**同时拿到属于自己那一拍的 `a` 与 `b`,才能正确累加点积。可后端/测试侧「喂数据」时,最自然的做法是每拍给整条向量喂入**矩阵的某一列**(lane i 喂 A 的第 i 行、当前列)。如果直接这样送进 SA,波前就对不齐。

`DataFeeder` 解决的正是这个「自然喂法 ↔ 阵列所需反对角」之间的错位:它在硬件里给 lane i 施加 i 拍延迟,把「每拍一整列」的输入**扭曲**成「每拍一条反对角线」。这样软件只需简单地逐列喂,扭曲由硬件吸收。源码头注释里的「takes N ticks to consume all data」即指需要 n 拍才能把一整组数据喂完。

它还承担第二件事:把累加器(偏置)`reg_accum_in` 做**统一的长延迟**(2n−1 拍),让它在结果被收集的那一刻才到达收集器——这是第 4.3 节「累加器路径」的起点。

#### 4.1.2 核心流程

`DataFeeder` 内部有三条独立的延迟链:

1. **a/b 的阶梯延迟(chainsaw)**:
   - lane 0 直通(0 拍延迟);
   - lane i(i≥1)经一条深度为 i 的 `Pipe`;
   - 效果:在 tick t,输出 lane i = 输入 lane i 在 tick t−i 时的值。
2. **accum 的统一长延迟**:每条 lane 各自经一条深度为 `2n−1` 的 `Pipe`,所有 lane 延迟相同。

用伪代码描述 a 路径的扭曲(以 n=4 为例,矩阵按行主序,每拍 lane i 喂 A[i][t]):

```
tick 0 输入: [A0,0 ; A1,0 ; A2,0 ; A3,0]      # 一整列,延迟 [0,1,2,3]
tick 0 输出: [A0,0 ;  -  ;  -  ;  -  ]
tick 1 输出: [A0,1 ; A1,0 ;  -  ;  -  ]
tick 2 输出: [A0,2 ; A1,1 ; A2,0 ;  -  ]
tick 3 输出: [A0,3 ; A1,2 ; A2,1 ; A3,0]      # 一条反对角线
...
```

输出向量在任一拍呈现的就是矩阵的一条反对角,正好交给 `SystolicArray2D` 继续传播。

#### 4.1.3 源码精读

类与端口定义:[src/main/scala/alu/mma/sa/dataFeeder.scala:11-19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L11-L19) —— `n`/`nbits`/`accum_nbits` 三个参数,输入 `reg_a_in/reg_b_in`(Vec(n, SInt(nbits)))与 `reg_accum_in`(Vec(n, SInt(accum_nbits))),输出三组对应 `*_out`。注意 `a/b` 用数据位宽 `nbits`(8),而累加器用 `accum_nbits`(32),位宽更宽以容纳点积累加和不溢出(见 u4-l1)。

a/b 的阶梯延迟链:[src/main/scala/alu/mma/sa/dataFeeder.scala:21-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L21-L22) —— `(1 until n map(x => Module(new Pipe(..., x))))` 生成深度为 1, 2, …, n−1 的 n−1 条 Pipe。也就是说第 i 条(index i−1)深度为 i,正好让 lane i 延迟 i 拍。

accum 的统一长延迟:[src/main/scala/alu/mma/sa/dataFeeder.scala:23-28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L23-L28) —— `buffer_accum` 为每条 lane 各建一条深度 `2*n-1` 的 Pipe。注释解释了**为什么是每条 lane 一条、而不是一条管整个 Vec**:用单条 `Pipe(Vec(n,...), 2n-1)` 会产生一个扇出高达 `n*(2n-1)`(n=32 时 1025)、横跨整个 die 的 valid 触发器,250 MHz 下时序违例;改成每 lane 独立的一条 Pipe,扇出降到 ≤ 2n−1(=63),Vivado 可把每条副本就近摆放到消费者旁边,网延迟降低约 6×。这是一处典型的「为时序而增加面积」的取舍。

accum 的入队/出队接线:[src/main/scala/alu/mma/sa/dataFeeder.scala:35-39](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L35-L39) —— `enq.valid` 恒为 `true.B`,`enq.bits := reg_accum_in(i)`,`reg_accum_out(i) := deq.bits`,即无条件把累加器送进长延迟链。

a/b 的链锯扭曲(chainsaw layout):[src/main/scala/alu/mma/sa/dataFeeder.scala:42-53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L42-L53) —— lane 0 直通 `reg_a_out(0) := reg_a_in(0)`;lane i(i≥1)走 `buffer_a(i-1)`:`enq.bits := reg_a_in(i)`、`reg_a_out(i) := deq.bits`。这正是 4.1.2 里描述的「lane i 延迟 i 拍」。

> 提示:`DataFeederSpec` 用 `n=4`、跑 `3n-2=10` 拍,每拍把矩阵某列 poke 进 `reg_a_in/reg_b_in`,然后 `expect` 输出等于反对角 `_a_in_t(_i) = _mat_a(_i*_n + (i_tick - _i))`(见 [DataFeederSpec.scala:88-93](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala#L88-L93)),可作为扭曲效果的参照。

#### 4.1.4 代码实践

**目标**:亲眼看到「逐列喂入 → 反对角输出」的扭曲,以及累加器的长延迟。

**步骤**:

1. 运行现有馈送器测试,观察打印:
   ```bash
   tool/test-specific-spec.sh alu.mma.sa.DataFeederSpec
   ```
2. 阅读测试中每拍打印的 `Output Vector A tick @ k`,把前 4 拍的输出向量抄下来,确认 lane i 的值确实比输入晚了 i 拍。
3. 关注测试末尾对 `reg_accum_out` 的断言:[DataFeederSpec.scala:97-103](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataFeederSpec.scala#L97-L103) —— `i_tick >= 2*n-2` 才期望 `reg_accum_out == _vec`,否则为 0。

**需要观察的现象**:
- a/b 输出向量随时间向右下「滑动」,任一拍都呈反对角形态;
- 累加器输出在前若干拍恒为 0,直到第 `2n-2` 拍(对 n=4 即第 6 拍)才出现你 poke 的 `_vec`。

**预期结果**:测试通过,且你能口头解释「lane i 延迟 i 拍」「accum 延迟 2n−1 拍」。**待本地验证**:若你的容器内 `tool/test-specific-spec.sh` 行为不同,以你实际看到的 tick 编号为准。

#### 4.1.5 小练习与答案

**练习 1**:为什么 a/b 用**逐 lane 递增**(0,1,…,n−1)的延迟,而 accum 用**所有 lane 统一**(2n−1)的延迟?

> **答案**:a/b 的每条 lane 要对齐到不同波前(各 lane 进入阵列的时刻不同),所以延迟必须逐 lane 细粒度变化;accum 是在管子末端(收集时)一次性加到结果上,对所有 lane 是同一拍生效,所以只需统一的长延迟把它「憋」到结果出现的那一刻。

**练习 2**:若把 `buffer_accum` 的深度从 `2*n-1` 改成 `n-1`,直觉上累加器会「过早」还是「过晚」到达收集器?

> **答案**:过晚到达的反而被提前,即累加器会**过早**出现在收集器入口,与 PE 结果对不齐,导致偏置加到了错误的输出帧上(功能错误)。正确深度由端到端 `MMALUSpec` 校准。

### 4.2 DataCollector:收集 PE 输出与累加器

#### 4.2.1 概念说明

n×n 个 PE 各自算出一个点积结果(存于各自 `res` 寄存器),构成一个 n×n 的结果矩阵。但 MMALU 对外只暴露 `out: Vec(n, SInt(accum_nbits))`——每拍 n 个值。`DataCollector` 的职责是:在 `n` 拍内把这 n×n 个结果**逐列回收**,并经反向阶梯延迟把它们**重新对齐**成一条稳定的 n 元输出向量。

它还有两个控制开关:
- `dat_clct`:收集使能。拉高时内部模 n 计数器自增,开始逐列读结果;拉低时计数器复位为 0。
- `use_accum`:选通是否把 `accum_in(i)` 加到输出 lane i(即实现 \(Y = B^{\mathsf T}A + C\) 里的加偏置 \(C\))。

源码头注释概括了它的时序:「takes N ticks to collect all data; takes N−1 ticks to boot up(with data)」——即前 n−1 拍是填充延迟链的启动期,之后每拍吐出一个对齐结果。

#### 4.2.2 核心流程

`DataCollector` 用「模 n 计数器 + 反向阶梯延迟」回收结果。设当前计数 `cnt`(0..n−1):

1. 对每个输出 lane i,计算它当前该读结果矩阵的哪一列:
   \[ \text{col}_i = (\text{cnt} - i) \bmod n \]
   于是 lane i 读 `reg_in[i*n + col_i]`,即第 i 行、第 col_i 列。
2. lane i 的读取结果再经一条深度为 `n−i−1` 的 Pipe( lane n−1 直通、lane 0 延迟最大)。
3. 这一「反向阶梯」与读取时的 `(cnt-i) mod n` 偏移相互抵消,使各 lane 的输出重新对齐:每过一拍,`cnt` 自增,吐出结果矩阵的下一列。
4. 若 `use_accum` 为真,输出再 `+ accum_in(i)`。

之所以叫「反向」:馈送器是 lane i 延迟 i(逐增),收集器是 lane i 延迟 n−1−i(逐减),二者方向相反,合在一起才能既送得对、又收得齐。

#### 4.2.3 源码精读

类与端口:[src/main/scala/alu/mma/sa/dataCollector.scala:12-19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataCollector.scala#L12-L19) —— 控制输入 `dat_clct`/`use_accum`,数据输入 `accum_in`(Vec(n))与 `reg_in`(Vec(n*n),即 n×n 个 PE 输出),输出 `reg_out`(Vec(n))。

反向阶梯延迟链与模 n 计数器:[src/main/scala/alu/mma/sa/dataCollector.scala:21-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataCollector.scala#L21-L22) —— `buffer` 第 x 条深度为 `n-x-1`(lane 0 最深 n−1、lane n−2 深 1);`Counter(0 until n, true.B, !io.dat_clct)` 表示「使能恒为真、但当 `dat_clct` 为低时复位」,所以 `dat_clct` 拉低 → `cnt` 归零,拉高 → `cnt` 在 0..n−1 循环自增。

读取列偏移与加偏置:[src/main/scala/alu/mma/sa/dataCollector.scala:25-42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataCollector.scala#L25-L42) —— 关键三行:
```scala
val col = (cnt - i.U) % n.U                       // lane i 当拍读的列
buffer(i).io.enq.bits := io.reg_in((i*n).U + col) // 喂入反向延迟链
io.reg_out(i) := buffer(i).io.deq.bits + io.accum_in(i)  // use_accum 时加偏置
```
其中 lane n−1 走 `if (i == n-1)` 分支无延迟链(直通),其余 lane 经 `buffer(i)`。`use_accum` 为假时省去 `+ accum_in(i)`。

> 提示:`DataCollectorSpec`([DataCollectorSpec.scala:31-66](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataCollectorSpec.scala#L31-L66))始终把 `dat_clct` 置真,每拍把整个 `_mat` poke 进 `reg_in`,断言 `i_tick >= n-1` 后 `reg_out(_i) == _mat(_i*_n + valid_cnt)`,`valid_cnt` 每拍自增——这就是「n−1 拍启动后,每拍吐一列」的实证。

#### 4.2.4 代码实践

**目标**:亲手跑收集器,确认「n−1 拍启动 + 每拍一列」的回收顺序。

**步骤**:

1. 运行收集器单测,观察每拍输出:
   ```bash
   tool/test-specific-spec.sh alu.mma.sa.DataCollectorSpec
   ```
2. 对照 [DataCollectorSpec.scala:57-63](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/sa/DataCollectorSpec.scala#L57-L63),记录 `i_tick` 从 0 到 `2n` 的 `reg_out` 向量。
3. 设计一个**最小变体用例**(示例代码,需你自行加入测试文件并跑):把 `use_accum` 也置真,并给 `accum_in` 喂一个固定的非零向量(例如全 1),验证输出比原测试正好多了这个偏置:
   ```scala
   // 示例代码:在 DataCollectorSpec 基础上新增一段
   dut.io.use_accum.poke(true)
   for (_i <- 0 until _n) dut.io.accum_in(_i).poke(1)  // 偏置全 1
   // 预期:reg_out(_i) == _mat(_i*_n + valid_cnt) + 1
   ```
   > 注意:本仓库测试文件请勿直接改动源码;可在 `src/test/scala/alu/mma/sa/` 下新建一个临时 spec 来跑这段,验证完删掉。

**需要观察的现象**:前 `n−1` 拍输出尚不对齐(或为 0/默认),从第 `n−1` 拍起每拍输出一条完整的、按列推进的结果;加偏置后整条向量整体 +1。

**预期结果**:回收顺序与 `_mat` 的列序一致;偏置选通生效。**待本地验证**:实际启动拍号以你容器内输出为准。

#### 4.2.5 小练习与答案

**练习 1**:`col = (cnt - i.U) % n.U` 里的减法是 UInt 减法,当 `cnt < i` 时会下溢。为什么这里仍然正确?

> **答案**:UInt 减法在硬件上是模 \(2^{\text{bits}}\) 回绕,再对 n 取模,等价于数学上的 `(cnt - i) mod n`(当无符号位宽足够时)。因此 `cnt < i` 时得到的正是 n−(i−cnt) 这一列,与预期负数取模一致。这正是收集器「旋转读列」的核心。

**练习 2**:为什么 lane n−1 没有延迟链(直通),而 lane 0 延迟最大?

> **答案**:收集器是「反向阶梯」,要让最晚读到的 lane(n−1,因为读取偏移 (cnt−(n−1)) 最靠后)不被再延迟,而最早/需要等待对齐的 lane 0 用最大延迟把数据「压住」,这样所有 lane 最终在同一拍对齐输出。与馈送器的「正向阶梯」方向相反。

### 4.3 累加器路径与 dat_clct / use_accum 时序

#### 4.3.1 概念说明

把 4.1、4.2 串起来,看两个关键时序信号是如何产生、又是如何决定「结果何时被写出来」「偏置何时被加上」的:

- `dat_clct`(收集使能):来自 `ControlUnit`,是**所有在飞波前的 keep 信号的大或(OR)**。只要还有任何一个波前处于累加(keep=true)状态,`dat_clct` 就为真,收集器就继续吐结果;一旦全部波前都 keep=false(点积收尾),`dat_clct` 拉低、计数器复位。
- `use_accum`(加偏置使能):同样来自 `ControlUnit`,由控制移位寄存器最末级 `reg(2n-2).use_accum` 给出,表示「当前被收集的这一帧要不要加偏置 C」。

这条「累加器路径」之所以重要,是因为它实现了矩阵乘的加偏置 \(Y = B^{\mathsf T}A + C\),也是 u7 量化流水线里 vfma 加缩放/偏置的硬件基础。

#### 4.3.2 核心流程

完整的累加器(偏置)通路:

```
MMALU.io.in_accum(i)
   └─ DataFeeder.buffer_accum(i)   深度 2n-1   ─>  reg_accum_out(i)
                                                      │
                          MMALU.pipe_accum(i) +1 拍  │
                                                      ▼
                                       DataCollector.accum_in(i)
                                                      │
                            use_accum ? +accum_in(i) : +0
                                                      ▼
                                           reg_out(i)  (n 个结果之一)
```

控制通路则独立走来:

```
ControlUnit.io.cbus_in.keep  ──[1D 移位 reg(2n-1),对角广播]──> 各 PE 的 keep
                                └──[OR 全体 keep]──> cbus_dat_clct ──+1拍──> DataCollector.dat_clct
ControlUnit reg(2n-2).use_accum ────────────────────────────+1拍──> DataCollector.use_accum
ControlUnit reg(2n-2).busy     ────────────────────────────+1拍──> MMALU.io.clct (全局完成)
```

`dat_clct` 与 `keep` 的配合是流式归约的关键:连续保持 `keep=true` 时,`dat_clct` 一直为真,收集器每 K 拍吐出一个**累积部分和帧**——这正是 u4-l5 要展开的 M×K 流式归约的物理来源。

#### 4.3.3 源码精读

MMALU 顶层把馈送器、收集器连起来,并给累加器多加 1 拍 `pipe_accum`:[src/main/scala/alu/mma/mma.scala:60-66](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L60-L66) —— `pipe_accum(i) := dfeed.io.reg_accum_out(i)`、`dclct.io.accum_in(i) := pipe_accum(i)`,而 `dclct.io.dat_clct := pipe_dat_clct`、`dclct.io.use_accum := pipe_use_accum`。这组 `pipe_*` 寄存器是为打断长组合路径以修 200 MHz 时序(见 [mma.scala:9-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L9-L22) 的时序说明),代价是延迟从 3n−2 变为 3n−1。

收集器把 PE 输出接进来:[src/main/scala/alu/mma/mma.scala:74-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L74-L84) —— `dclct.io.reg_in(n*i+j) <> pe_io(n*i+j).out`,即 n×n 个 PE 的 `out` 直接铺成收集器的 n×n 输入。

控制端的 `dat_clct` 来源:`ControlUnit` 用一个 `ORGate(2*n-1)` 把所有控制槽的 keep 大或成 `cbus_dat_clct`:[src/main/scala/alu/mma/cu/controlUnit.scala:22-33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L22-L33) —— `or_g.io.in(0) := io.cbus_in.keep`,其余 `or_g.io.in(i+1) := reg(i).keep`,`io.cbus_dat_clct := or_g.io.out`;而 `cbus_use_accum` 与全局完成 `clct` 取自最末级 `reg(2*n-2)`。这正解释了「只要任一波前在累加,就继续收集」。

> 综合:`dat_clct` 拉高那一拍,收集器才把 PE 输出回写成结果;`use_accum` 同拍为真时,结果再加上 `accum_in`。两者都经 `pipe_*` 延迟 1 拍,与数据路径的延迟一致。

#### 4.3.4 代码实践

**目标**:回答本讲开篇的两个问题——「in_accum 如何进入 collector」「dat_clct 在哪一拍拉高才写结果」。

**步骤**:

1. **追踪 in_accum → collector**:从 [mma.scala:60-63](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L60-L63) 出发,依次定位 `io.in_accum → dfeed.reg_accum_in → dfeed.buffer_accum(2n-1) → dfeed.reg_accum_out → pipe_accum(+1) → dclct.accum_in`,最后在 [dataCollector.scala:36-37](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataCollector.scala#L36-L37) 看到 `reg_out(i) := deq.bits + accum_in(i)`。在一张纸上画出这条链,标注每段延迟。
2. **追踪 dat_clct**:从 [controlUnit.scala:22-33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L22-L33) 的 OR 门,到 [mma.scala:86-88](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L86-L88) 的 `pipe_dat_clct := ctrl_array.io.cbus_dat_clct`,再到 `dclct.io.dat_clct`。说明:dat_clct 不是软件在某一拍手动拉高,而是**只要 keep 在飞就自动为真**。
3. 运行端到端验证流式收集行为(它会同时驱动 keep 与收集):
   ```bash
   tool/test-specific-spec.sh alu.mma.MMALUStreamReduceSpec
   ```

**需要观察的现象**:`in_accum` 经历 `2n-1 + 1` 拍后才到收集器入口;`dat_clct` 在 keep 持续期间维持高电平,每 K 拍吐一帧累积和。

**预期结果**:你能用一句话回答两个问题——「`in_accum` 经 DataFeeder 的 2n−1 拍延迟与 MMALU 的 1 拍 `pipe_accum` 进入 collector,在 `use_accum` 选通下加到结果」;「`dat_clct` 由 ControlUnit 对全体 keep 的 OR 产生,keep 在飞期间持续为真、由收集器自动回收,无需软件在某一拍手动拉高」。**待本地验证**:具体波形以你仿真输出为准。

#### 4.3.5 小练习与答案

**练习 1**:如果 `dat_clct` 在中途意外拉低一拍,收集器会发生什么?

> **答案**:`Counter` 的复位端 `!io.dat_clct` 会变真,`cnt` 立即归零,正在进行的逐列回收被打断、从第 0 列重新开始;同时反向延迟链里残存的数据会和新的读取错位,导致接下来 n−1 拍输出不对齐。所以 `dat_clct` 必须在整个收集窗口内稳定为真。

**练习 2**:`pipe_accum`、`pipe_dat_clct`、`pipe_use_accum` 都是 1 拍寄存器。它们存在的唯一目的是功能正确吗?

> **答案**:不是。它们是为**时序收敛**而加的流水寄存器(打断 `reg_h → PE 乘加` 这条长组合路径,把 WNS=−0.151 ns 修掉)。功能上数据与控制都统一多延迟 1 拍,彼此对齐,所以总延迟从 3n−2 变为 3n−1——用一拍延迟换时序闭合,是 NPU 设计里的常见取舍。

## 5. 综合实践

把本讲三个最小模块串成一个端到端的小任务:**在 n=4 下,用一段连贯的推理预测 MMALU 输出一帧结果的全过程**,并到源码里逐处核对。

1. **画时序图**:取 n=4,设 `keep=true` 连续 4 拍、`use_accum=true`、`in_accum=[c0,c1,c2,c3]`。请在坐标系上画出:
   - `reg_a_in/reg_b_in` 逐列喂入(4 拍);
   - `DataFeeder` 把它们扭曲成反对角输出(参考 4.1.2 的表);
   - `in_accum` 在 `DataFeeder` 里走 2n−1=7 拍、再在 MMALU 里走 1 拍,标注它在第几拍到达 `dclct.accum_in`;
   - `dat_clct` 因 keep 持续为真,收集器在第 n−1=3 拍启动后逐列回收。
2. **定位证据**:在源码里为图上每一段延迟找到对应行——`DataFeeder` 的阶梯 Pipe(L21-22)、accum 长延迟(L28)、MMALU 的 `pipe_accum`(L60-63)、`DataCollector` 的反向阶梯与计数器(L21-22)、加偏置(L36-37)。
3. **核对结论**:用 `MMALUSpec`(端到端 K-burst)与 `MMALUStreamReduceSpec`(流式)的通过情况,印证你的时序图是否与真实行为一致。
4. **回答本讲核心问题**:`in_accum` 如何进入 collector?`dat_clct` 在哪一拍拉高才会把 PE 输出写成结果?(答案见 4.3.4)

> 完成后,你就具备了阅读 u4-l5(MMALU 顶层集成与 M×K 流式归约)所需的全部「数据搬运」背景:那里会把本讲的收集器时序与 PE 的 keep 控制拼成完整的「每 K 拍一帧累积和」协议。

## 6. 本讲小结

- `DataFeeder` 用**逐 lane 递增的阶梯延迟**(lane i 延迟 i 拍)把「逐列喂入」扭曲成脉动阵列需要的**反对角波前**;`a/b` 用数据位宽,`accum` 用累加位宽。
- `DataFeeder` 给累加器施加**统一的 2n−1 拍长延迟**,且每条 lane 独立一条 Pipe,这是为 250 MHz 时序收敛、降低扇出的刻意取舍。
- `DataCollector` 用**模 n 计数器 + 反向阶梯延迟**(lane i 延迟 n−1−i 拍),把 n×n 个 PE 输出在 n 拍内**逐列回收、重新对齐**成 n 元输出,前 n−1 拍为启动期。
- 加偏置由 `use_accum` 选通:`reg_out(i) = 收集值(i) + accum_in(i)`,实现 \(Y = B^{\mathsf T}A + C\)。
- `dat_clct` 不是软件手动拉高,而是 `ControlUnit` 对所有在飞波前 keep 信号的 **OR**;keep 在飞期间持续为真、收集器自动回收,这正是流式归约的物理来源。
- MMALU 顶层的 `pipe_accum/pipe_dat_clct/pipe_use_accum` 是为时序收敛加的 1 拍流水寄存器,把总延迟从 3n−2 提到 3n−1。

## 7. 下一步学习建议

- **紧接的下一讲 u4-l4(ControlUnit)**:本讲反复提到的 `dat_clct`/`use_accum`/`clct` 全部产自 `ControlUnit` 的 1D 控制移位阵列与 OR 门,建议立刻读它,把「控制通路」补齐。
- **然后 u4-l5(MMALU 顶层集成与流式归约)**:把本讲的数据通路、PE 的 keep 控制、ControlUnit 的控制通路三者拼成完整的 MMALU,并推导 3n−1 的总延迟与「每 K 拍一帧累积和」的 M×K 流式协议。
- **延伸阅读**:`docs/implementations/SystolicArray.md` 里的「M×K Streaming Reduction」一节有阶梯输出帧的数学定义与波形,可作为本讲收集器行为的权威参照。
