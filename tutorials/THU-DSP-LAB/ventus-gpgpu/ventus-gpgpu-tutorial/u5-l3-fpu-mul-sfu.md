# 浮点、乘法与 SFU 特殊运算单元

## 1. 本讲目标

本讲聚焦 Ventus SM 后端中三类专业运算单元，学完后你应当能够：

- 说清 **vMULv2**（向量整数乘法/乘加）如何用「2 拍流水 + 全 lane 并行」高效完成一条向量乘法指令。
- 说清 **FPUexe** 如何把控制信号桥接给外部依赖 `fpuv2`，完成向量/标量单精度浮点运算。
- 说清 **SFUexe** 作为「特殊运算单元」为何要做成多周期、为何要按 `num_sfu` 分组串行执行（即「chime 变长」的由来）。
- 读懂底层两个迭代算法核 **IntDivMod**（整数除法/取余）与 **FloatDivSqrt**（浮点除法/平方根）的状态机。
- 能够定量对比「一条向量乘法」与「一条浮点除法」在执行周期与吞吐上的巨大差异。

## 2. 前置知识

阅读本讲前，建议你已建立以下认知（来自 u5-l1、u5-l2）：

- **lane 复制**：Ventus 用 RVV 向量指令充当 SIMT 指令，向量化执行单元会把同一个标量运算核复制 `num_thread`（默认 32）份，让一条向量指令的所有线程（lane）同拍并行算完。
- **alu_fn**：译码阶段（u4-l2）为每条指令生成的 5 位（或更宽）运算功能码，是各执行单元选择具体运算的依据。
- **双发射与执行单元路由**：`Issue` 模块依据 `CtrlSigs` 里的 `tc/sfu/fp/mul/mem` 等标志位，把指令分发到 TC、SFU、FPU、MUL 等端口（详见本讲 4.3 的路由说明）。
- **写回端口**：所有执行单元的结果最终汇入 `Writeback` 模块的 `in_x`/`in_v` 各 6 路，本讲的 FPU/MUL/SFU 各占其中若干路。

此外补充三个本讲会用到的底层概念：

- **流水线延迟（latency）与吞吐（throughput）**：一条运算从输入到结果可用所需的拍数叫延迟；单位时间能接受新输入的速率叫吞吐。理想流水线在填满后，吞吐可达「每拍一条」，与延迟无关。
- **FMA（乘加）**：\(a \times b + c\) 三操作数融合运算，乘法与加法不分别舍入，精度更高、也更快。
- **迭代除法**：硬件除法无法像加法那样一拍完成，经典做法是每拍求出商的若干位（如 SRT 算法每拍 2 位），多次迭代逐步逼近结果，因此是「多周期」单元。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ventus/src/pipeline/Multiplier.scala` | 整数乘法数据通路 `ArrayMulDataModule`（Booth+Wallace 树）与 2 拍流水封装 `ArrayMultiplier` |
| `ventus/src/pipeline/execution.scala` | 包装层：`vMULv2`、`FPUexe`、`SFUexe` 三个执行单元都在此定义，并完成 lane 复制与结果组装 |
| `ventus/src/pipeline/fpu_utils.scala` | 浮点工具：`FPUOps` 功能码、`Float32` 格式、`FPUSubModule` 接口、舍入单元、SRT 查表等公共件 |
| `ventus/src/pipeline/FloatDivSqrt.scala` | 浮点除法/平方根迭代核 `FracDivSqrt`（SRT）与外层 `FloatDivSqrt` 状态机 |
| `ventus/src/pipeline/IntDivMod.scala` | 整数除法/取余迭代核 `IntDivMod`（移位减除式状态机） |
| `ventus/src/pipeline/mul_utils.scala` | `MulModule` 抽象基类与 `HasPipelineReg` 流水线寄存器 trait（乘法器复用） |
| `ventus/src/top/parameters.scala` | `num_thread`、`num_lane`、`num_sfu` 等规模参数 |
| `ventus/src/pipeline/pipe.scala` | 例化 `FPUexe`/`SFUexe`/`vMULv2` 并连接 Issue 与 Writeback |

## 4. 核心概念与源码讲解

### 4.1 vMULv2 与 ArrayMultiplier：向量整数乘法/乘加

#### 4.1.1 概念说明

整数乘法是一条「贵」但仍可流水的运算。Ventus 的整数乘法由两层构成：

- 底层 `ArrayMultiplier`（在 `Multiplier.scala`）：一个**单 lane** 的 32 位乘法器，采用 Booth 基 4 编码 + Wallace 压缩树（代码源自香山 XiangShan），并封装为 **2 拍流水线**。
- 上层 `vMULv2`（在 `execution.scala`）：把 `ArrayMultiplier` **复制 `num_thread` 份**，一份服务一个 lane，使一条向量乘法指令的所有 lane 同拍并行算完。

这与 u5-l2 讲过的「ScalarALU 在 1 份与 32 份上的复用」是完全一致的设计哲学。注意 `vMULv2` 还带 `softThread`/`hardThread` 两个参数，当硬件 lane 数少于软件线程数时会分组串行（见 4.1.2），但默认配置下二者相等，走全并行快路径。

#### 4.1.2 核心流程

一条整数乘法指令（如 `mul`、`mulh`、`macc`）进入 `vMULv2` 后：

1. **取低位 lane**：当 `softThread == hardThread`（默认 32==32）时，对每个 lane `x` 把 `in1(x)/in2(x)/in3(x)` 与控制信号分别喂给对应的 `mul(x)`。
2. **MAC 操作数重排**：若是乘加类（`FN_MADD`/`FN_NMSUB`），把第三操作数 `c` 换到乘法输入位，原 `a` 换到累加位。
3. **2 拍流水计算**：每个 `ArrayMultiplier` 内部经 Booth 编码、部分积压缩、最终加法，2 拍后给出结果；流水线填满后可每拍接收新输入。
4. **结果选择**：依据 `alu_fn` 选择低 32 位（`FN_MUL`）、高 32 位（`FN_MULH/MULHU`）或乘加结果（`isMAC` 类）。
5. **双出口**：标量结果走 `out_x`，向量结果走 `out_v`，由 `wxd`/`wvd` 决定有效。

当 `softThread != hardThread` 时，`vMULv2` 用 `maxIter = softThread/hardThread` 次迭代（`sendCS`/`recvCS` 两个小状态机）分批处理，机制与 4.3 的 SFU 分组类似，但默认配置不触发。

#### 4.1.3 源码精读

`ArrayMultiplier` 声明 2 拍流水，这是本讲「2 拍乘法」的由来：

[Multiplier.scala:149-154](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L149-L154) —— `class ArrayMultiplier` 继承 `MulModule` 与 `HasPipelineReg`，`override def latency = 2`。

乘加操作数重排（普通乘法用 a×b，乘加把 c 顶到乘法位）：

[Multiplier.scala:156-158](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L156-L158) —— `mul_in1`/`mul_in3` 在 `FN_MADD|FN_NMSUB` 时互换 a 与 c。

最终结果选择——普通乘取高低位，乘加走 `mac_out`：

[Multiplier.scala:200-206](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L200-L206) —— `mac_out = Mux(FN_MACC|FN_MADD, result+c, c-result)`，`res` 经 `isMAC`/`FN_MULH` 路由后用 `PipelineReg(latency)` 打一拍输出。

`HasPipelineReg` 这个 trait 用 `valids` 数组实现「可反压的流水线握手」，是乘法器 2 拍流水的支撑：

[mul_utils.scala:38-63](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/mul_utils.scala#L38-L63) —— `valids` 随 `io.in.valid` 逐级下传，当输出端反压（`!io.out.ready`）且后续各级全满时冻结寄存器；`io.in.ready` 与 `io.out.valid` 据此推导。

上层 `vMULv2` 在默认全并行分支里，逐 lane 复制乘法器并组装向量结果：

[execution.scala:188-190](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L188-L190) —— `val mul = VecInit(Seq.fill(hardThread)(Module(new ArrayMultiplier(softThread, xLen)).io))`，例化 `hardThread`（默认 32）个乘法器。

[execution.scala:192-206](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L192-L206) —— `softThread == hardThread` 快路径：每个 lane 的 a/b/c/mask/ctrl 分别接线，`reverse` 时互换 a、b。

乘法相关功能码定义在 `ALUOps`，可用 `isMAC` 一并判定乘加族：

[ALU.scala:40-56](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L40-L56) —— `FN_MUL=20`、`FN_MULH=21`、`FN_MULHU=22`、`FN_MULHSU=23`、`FN_MACC=24`、`FN_MADD=26`、`FN_NMSUB=27`；`isMAC(cmd) = cmd(4,2)==="b110"`。

#### 4.1.4 代码实践

**实践目标**：确认 vMULv2 是「全 lane 并行 + 2 拍流水」，并读懂 lane 到乘法器的映射。

**操作步骤**：

1. 在 [execution.scala:188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L188) 处确认例化了 `hardThread` 个 `ArrayMultiplier`。
2. 跟着 [execution.scala:193-199](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L193-L199) 的 `foreach`，写下「lane x 的 in1/in2/in3 → mul(x).in.bits.a/b/c」的对应关系。
3. 在 [Multiplier.scala:152](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L152) 确认 `latency = 2`。

**需要观察的现象**：一个 warp（32 lane）的乘法指令只需 2 拍即出结果，且 32 个 lane 互不干扰地并行计算。

**预期结果**：相对单 lane 串行 32 拍，vMULv2 用 32 倍硬件换来约 16 倍加速（2 拍 vs 64 拍），这正是 GPU 用面积换性能的体现。

> 待本地验证：若有仿真环境，可在乘法输入端打点，观察同 warp 的 32 个乘法器是否同拍 `fire`。

#### 4.1.5 小练习与答案

**练习 1**：`FN_MULH`（有符号高位乘）与 `FN_MULHU`（无符号高位乘）在数据通路上有何区别？

**答案**：见 [Multiplier.scala:178-179](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L178-L179)。`mulDataModule.io.a` 在 `FN_MULH|FN_MULHSU` 时对 in1 做符号扩展（`signext`），否则零扩展（`zeroext`）；`io.b` 仅在 `FN_MULH` 时符号扩展。两者最终都取结果高 32 位（`result(2*len-1,len)`，见第 205 行）。

**练习 2**：为什么 `vMULv2` 要同时保留 `out_x` 与 `out_v` 两个出口？

**答案**：同一条乘法指令可能写回标量寄存器（`wxd`，如 RV32M 的标量 `mul`）或向量寄存器（`wvd`，如 RVV 向量乘法）。由 [execution.scala:222-223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L222-L223) 的 `ctrl.wvd`/`ctrl.wxd` 分别驱动两个出口的 valid，写回模块也据此分别接到 `in_x(5)` 与 `in_v(4)`。

---

### 4.2 FPUexe：对接 fpuv2 的浮点运算单元

#### 4.2.1 概念说明

浮点运算（加、减、乘、FMA、比较、取整、类型转换等）的 RTL 实现非常繁琐（阶码对齐、规格化、舍入、异常标志）。Ventus **不自己重写**这套逻辑，而是直接例化姊妹项目 **`fpuv2`**（在 `dependencies/` 下，参见 u1-l3）提供的 `FPUv2.VectorFPU`。`FPUexe` 的角色是一个**适配层/包装器**：把 Ventus 内部的 `vExeData`（操作数 + `CtrlSigs`）翻译成 `fpuv2` 期望的输入格式，再把 `fpuv2` 的输出翻译回 Ventus 的写回格式。

`FPUexe` 同样是 lane 复用思想，但具体运算核在 `fpuv2` 内部，本讲不深入其实现，只讲清「桥接」与「op 编码」。

#### 4.2.2 核心流程

1. **例化**：`FPUexe` 例化一个 `FPUv2.VectorFPU(8, 24, softThread, hardThread, ctrl)`，其中 `8`/`24` 是单精度浮点的指数位/尾数位（IEEE754 float32 是 8 位阶码 + 23 位尾数，这里 24 含隐含位）。
2. **逐 lane 喂数据**：对每个 lane，把 `in1/in2/in3` 映射到 `fpu.io.in.bits.data(x).a/b/c`，舍入模式 `rm` 来自 CSR 文件。
3. **op 编码**：把 `CtrlSigs.alu_fn` 直接当作浮点 op 送给 fpuv2；FMA 类指令（`FN_VFMADD` 等）做一次 op 与操作数重排。
4. **双出口**：标量结果取 lane 0，走 `out_x`；向量结果走 `out_v`，由 `wxd`/`wvd` 决定。

#### 4.2.3 源码精读

`FPUexe` 例化 `fpuv2.VectorFPU` 并声明 `softThread % hardThread == 0` 的断言：

[execution.scala:746-758](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L746-L758) —— `class FPUexe(softThread, hardThread)`，内部 `val fpu = Module(new FPUv2.VectorFPU(8, 24, softThread, hardThread, new TestFPUCtrl(...)))`。

逐 lane 桥接操作数与 op，FMA 类做特判重排：

[execution.scala:759-775](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L759-L775) —— `data(x).a/b/c` 赋值；`data(x).op := ctrl.alu_fn`；当 `alu_fn` 属于 `FN_VFMADD|FN_VFMSUB|FN_VFNMADD|FN_VFNMSUB` 时，`op := alu_fn - 10.U` 并把 b、c 位置互换。

控制信号与双出口 valid 的桥接：

[execution.scala:776-803](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L776-L803) —— `ctrl.wvd/wxd/regIndex/warpID/vecMask` 传给 fpu；输出端 `out_v.valid := fpu.io.out.valid && ctrl.wvd`，`out_x.valid := ... && ctrl.wxd`，反压 `fpu.io.out.ready` 据二者选择。

浮点 op 功能码在 `FPUOps` 中定义，与 fpuv2 约定一致：

[fpu_utils.scala:16-43](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L16-L43) —— `FN_FADD=0`、`FN_FSUB=1`、`FN_FMUL=2`、`FN_FMADD=4`、`FN_FMSUB=5`、`FN_FNMSUB=6`、`FN_FNMADD=7`，以及比较类 `FN_MIN/MAX/FLE/FLT/FEQ/FNE`、符号注入类、类型转换类 `FN_F2I/FN_I2F` 等。

`Float32` 与 `FPUSubModule` 是浮点工具的公共定义，`FloatDivSqrt`/`Classify` 等都继承自 `FPUSubModule`：

[fpu_utils.scala:74-103](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L74-L103) —— `object Float32 extends FloatPoint(8, 23)`；`FPUSubModule` 定义了 `Decoupled` 的 `in`/`out` 接口。

#### 4.2.4 代码实践

**实践目标**：理解 `alu_fn` 如何同时承载整数与浮点运算，以及 FMA 指令的操作数重排。

**操作步骤**：

1. 对照 [fpu_utils.scala:19-25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L19-L25) 的 `FN_FADD`~`FN_FNMADD`，写出一条 `vfmadd.vf` 指令译码后 `alu_fn` 的大致取值。
2. 在 [execution.scala:769-774](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L769-L774) 追踪：为何 FMA 类要把 `b` 换到 `c` 位、`c` 换到 `b` 位？（提示：fpuv2 内部 FMA 约定操作数顺序与 RVV 不同。）
3. 在 [pipe.scala:403](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L403) 确认 `fpu.io.rm` 的来源（CSR 文件 rm(0)，可被 `force_rm_rtz` 强制为向零舍入）。

**需要观察的现象**：浮点指令的路由完全由 `ctrl.fp` 标志位触发，与整数乘法（`ctrl.mul`）互斥。

**预期结果**：`Issue` 仅把 `ctrl.fp` 为真的指令发往 `out_vFPU`（见 [issue.scala:104-106](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L104-L106)），确保 FPU 与 MUL 不会同时收到指令冲突。

#### 4.2.5 小练习与答案

**练习 1**：`FPUexe` 的标量出口 `out_x` 取的是哪个 lane 的结果？为什么？

**答案**：取 lane 0，见 [execution.scala:793](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L793) `io.out_x.bits.wb_wxd_rd := fpu.io.out.bits.data(0).result(31,0)`。因为标量浮点指令（如 RV32F 的 `fadd.s`）所有 lane 的操作数相同，结果也相同，取任一 lane 即可，约定取 lane 0。

**练习 2**：为什么 FPU 的舍入模式 `rm` 要专门从 CSR 文件读，而不在指令字里？

**答案**：RISC-V 把动态舍入模式存在 `frm` 寄存器里（属于 CSR），同一条指令在不同 `frm` 下结果不同。[pipe.scala:403-404](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L403-L404) 在每拍把当前 FPU 指令所属 warp 的 `wid` 回报给 CSR 文件（`rm_wid(0)`），从而查出该 warp 的 `frm`。

---

### 4.3 SFUexe：多周期特殊运算单元（除法/取余/浮点除法/平方根）

#### 4.3.1 概念说明

SFU（Special Function Unit）专门处理那些「太慢、不值得每个 lane 都配一个」的运算：**整数除法、整数取余、浮点除法、浮点平方根**。这些运算的本质是迭代算法，单次执行需要几十拍，若像乘法那样复制 32 份会浪费巨大面积。因此 Ventus 采取折中：

- 只配置 **`num_sfu = (num_thread >> 2).max(1)`** 个 SFU 单元（默认 `num_thread=32` → `num_sfu=8`），远少于 32 个 lane。
- 把一个 warp 的 32 个 lane 按 `num_sfu` 个一组分成 **`num_grp = num_thread/num_sfu`** 组（默认 4 组），**串行**地一组一组处理。
- 当某组内存在被 mask 选中的有效 lane 时才计算，全部组处理完毕后才向写回端口输出一次结果。

这就是本讲核心结论之一：**SFU 单元数少于 lane 数，导致一条带满 mask 的向量除法指令需要「chime」（多轮迭代）才能跑完，执行时间远长于乘法**。「chime」一词借自向量机的传统概念——指一个运算槽把所有元素轮一遍所需的拍数。

#### 4.3.2 核心流程

SFUexe 用三态有限状态机 `s_idle → s_busy → s_finish` 驱动：

1. **s_idle**：收到指令（`io.in.fire`），把 mask 存入 `mask` 寄存器，置 `i_valid`，进入 `s_busy`。
2. **s_busy**（核心循环）：
   - 用 `PriorityEncoder(mask_grp)` 选出最低编号的「还有有效 lane」的组 `i_cnt`。
   - 把该组的 `num_sfu` 个 lane 操作数喂给 `num_sfu` 个 `IntDivMod` 或 `FloatDivSqrt`（由 `ctrl.fp` 二选一）。
   - 当该组结果 `fire`，把结果写回 `out_data` 对应位置，并**清除该组在 mask 中的位**，置 `i_valid` 处理下一组。
   - 当 `!next_mask.orR`（所有有效组都算完）→ 进入 `s_finish`。
3. **s_finish**：把 `out_data` 一次性发给 `out_v`/`out_x`，下游 ready 后回到 `s_idle`。

整型与浮点型的区分、商/余数的选择、有符号/无符号的选择，都通过对 `alu_fn` 不同位进行解码完成。

#### 4.3.3 源码精读

SFU 单元数量由全局参数决定，这是「chime 变长」的根因：

[parameters.scala:91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/parameters.scala#L91) —— `def num_sfu = (num_thread >> 2).max(1)`。同时 [parameters.scala:57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L57) `def num_lane = num_thread`，故默认 `num_sfu=8`、`num_lane=32`。

分组与有效组检测：

[execution.scala:819-822](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L819-L822) —— `num_grp = num_thread/num_sfu`；`mask_grp(i)` 把每 `num_sfu` 个 lane 的 mask「或」成一位，表示该组是否有活线程。

挑下一组与例化 `num_sfu` 个除法核：

[execution.scala:828-847](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L828-L847) —— `i_cnt = PriorityEncoder(mask_grp)` 选最低有效组；`intDiv` 与 `floatDiv` 各 `Seq.fill(num_sfu)(...)` 例化 8 个，外加一个二选一仲裁器 `alu_out_arbiter`。

`alu_fn` 各位的语义解码（整型）：

[execution.scala:870](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L870) —— `alu_fn(0)` 为 1 取余数 `r`、为 0 取商 `q`。
[execution.scala:889](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L889) —— `signed := !alu_fn(1)`，bit1 为 0 表示有符号。
[execution.scala:893-894](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L893-L894) —— `ctrl.fp` 决定启动 `intDiv` 还是 `floatDiv`。
[execution.scala:891](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L891) —— 浮点 op 取 `alu_fn(2,0)` 三位。

状态机与「算完一组清一组 mask」的核心循环：

[execution.scala:908-933](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L908-L933) —— `s_busy` 中当 `alu_out_fire`，把当前组 `i_cnt` 对应的 `num_sfu` 个 mask 位清零（`next_mask`），写回 `out_data`，若 `!next_mask.orR` 则进入 `s_finish`。

SFU 在流水线中的路由与连接：仅 `ctrl.sfu` 为真的向量指令发往 SFU，写回分别占 `in_x(4)` 与 `in_v(3)`：

[pipe.scala:384-385](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L384-L385) —— `issueV.io.out_SFU <> sfu.io.in`，`issueX.io.out_SFU.ready := false.B`（标量侧禁用）。
[pipe.scala:420-425](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L420-L425) —— `wb.io.in_x(4) <> sfu.io.out_x`、`wb.io.in_v(3) <> sfu.io.out_v`。

#### 4.3.4 代码实践

**实践目标**：定量对比一条向量乘法（vMULv2）与一条浮点除法（SFU）在执行时间上的差异，理解 `num_sfu` 如何影响带 mask 的多线程执行时间。

**操作步骤**：

1. 在 [execution.scala:846-847](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L846-L847) 确认 SFU 只例化了 `num_sfu=8` 个除法核，而 vMULv2 在 [execution.scala:188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L188) 例化了 `num_lane=32` 个乘法核。
2. 假设一条向量指令 mask 全有效（32 个 lane 都要算）：
   - **vMULv2**：32 个乘法核同拍并行，2 拍出结果 → 约 **2 拍**。
   - **SFU**：32 个 lane 分 4 组（`num_grp=4`），每组 8 个核并行但组间串行；单次浮点除法约需 14+ 拍迭代（见 4.5），故总时间约为 **4 组 × 除法延迟**。
3. 在 [execution.scala:912-924](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L912-L924) 跟踪「清一组 mask → 处理下一组」的循环，确认这是串行 chime 的来源。

**需要观察的现象**：同样是一条向量指令，乘法几乎「瞬间」完成，而除法会让该 warp 在 SFU 内滞留数十拍；在此期间 `issueV.io.out_SFU.ready` 长时间为低（因为 `data_buffer.ready := state===s_finish & o_ready`，见 [execution.scala:897](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L897)），表现为该 warp 的后续指令被记分板/发射阻塞。

**预期结果**：把 `num_sfu` 调大（例如改为 `num_thread>>1`）会让 `num_grp` 减半，SFU 吞吐提升但面积增加；这正是「面积 vs 性能」的可配置权衡点。

> 待本地验证：可尝试修改 [parameters.scala:91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L91) 的 `num_sfu` 表达式，重新 `make verilog` 并对比生成 RTL 中 `IntDivMod`/`FloatDivSqrt` 的例化数量变化。

#### 4.3.5 小练习与答案

**练习 1**：若一条 SFU 指令的 mask 只有第 0 个 lane 有效（其余 31 个 lane 被 mask 掉），SFU 需要处理几组？

**答案**：只需 1 组。因为 `mask_grp` 第 0 组为真、其余组为假，`PriorityEncoder` 选第 0 组，算完后 `next_mask` 全零直接进 `s_finish`。可见 mask 能有效减少 SFU 的无效工作。

**练习 2**：为什么 `alu_out_arbiter.foreach(x=>x.out.ready := alu_out_arbiter.map(_.out.valid).reduce(_&_))`（[execution.scala:849](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L849)）要求所有 SFU 单元的输出同时 ready？

**答案**：同一组的 `num_sfu` 个 lane 是一组并行计算、一起提交的语义。只有当所有 `num_sfu` 个除法核都算完（`out.valid` 全真）且下游能整体接收时，才一起 `fire` 写入 `out_data`，保证组内结果对齐。

---

### 4.4 IntDivMod：整数除法/取余迭代核

#### 4.4.1 概念说明

`IntDivMod` 是 SFU 内部整数除法的底层算法核（注意它不直接面向流水线，而是被 `SFUexe` 例化 `num_sfu` 份）。它实现 32 位整数除法，同时输出商 `q` 与余数 `r`，支持有符号/无符号，并处理 `除零`、`INT_MIN / -1` 溢出等特殊情形。

算法本质是**带预对齐的迭代移位减除**（radix-2 SRT 风格）：先对被除数和除数做前导零归一化（左移对齐），再每拍根据余数寄存器高两位的形态选出商位（+1/0/-1，用 `Q`/`QN` 两套寄存器分别记录正负商位），迭代次数由前导零差值决定，最后做结果恢复。

#### 4.4.2 核心流程

`IntDivMod` 用 5 态状态机：

```
s_idle → (fire) → s_pre → s_compute(迭代 iter 次) → s_recovery → s_finish → s_idle
```

- **s_idle**：锁存输入，计算符号位；若是除零或溢出特例，直接跳 `s_finish`。
- **s_pre**：计算被除数/除数的前导零，得到对齐移位量与迭代次数 `iter`；若被除数 < 除数（商为 0）直接跳 `s_finish`。
- **s_compute**：每拍移位余数、按 `aReg.tail(1).head(2)` 选商位、移入 `Q`/`QN`，计数 `cnt` 减到 0。
- **s_recovery**：合并 `Q`/`QN` 得到最终商，恢复余数。
- **s_finish**：按符号位修正结果，处理特例，输出 `q`/`r`。

#### 4.4.3 源码精读

状态枚举与符号处理：

[IntDivMod.scala:25-35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L25-L35) —— 5 态枚举；`aSign/dSign` 取符号；`overflow` 检测 `INT_MIN / -1`；`divByZero` 检测除数为 0。

预对齐与迭代次数计算：

[IntDivMod.scala:43-54](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L43-L54) —— `aLez/dLez` 为前导零个数；`iter = Mux(aLez > dLez, 0, dLez - aLez + 1)` 决定迭代拍数。

迭代核心——商位选择与余数更新：

[IntDivMod.scala:62-66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L62-L66) —— `sel_pos`/`sel_neg` 根据余数高两位判断本拍商位；`aNext` 据此加上/减去除数或仅移位。
[IntDivMod.scala:123-128](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L123-L128) —— `s_compute` 内每拍把商位移入 `Q`/`QN`，`cnt` 递减。

结果恢复与特例输出：

[IntDivMod.scala:68-80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L68-L80) —— `commonQReg` 合并 Q/QN；`signedQ/signedR` 按符号取补；特例下 `specialQ`（除零返回全 1，溢出返回 INT_MIN）、`specialR`（除零返回原被除数）。

#### 4.4.4 代码实践

**实践目标**：通过状态机转移条件理解一次整数除法的拍数构成。

**操作步骤**：

1. 在 [IntDivMod.scala:85-104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L85-L104) 列出 5 个状态的进入条件。
2. 假设 `a=100, d=7`（无符号），估算 `iter` 约为多少（提示：`d` 的前导零约 29，`a` 的前导零约 25，`iter = dLez - aLez + 1 ≈ 5`），加上 pre/recovery/finish 各 1 拍，总延迟约 `iter + 3`。

**需要观察的现象**：除数越小（前导零越多），迭代次数越多，除法越慢；这与「除以小数商很大」的直觉一致。

**预期结果**：32 位整数除法的典型延迟在十几到三十几拍之间，远高于 2 拍的乘法。

> 待本地验证：精确拍数取决于具体操作数，可用仿真在 `io.in.fire` 与 `io.out.fire` 之间计数验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `Q` 和 `QN` 两套寄存器？

**答案**：SRT 风格算法每拍商位可能是 -1（`sel_neg`）。用 `Q` 记录到当前为止的正商序列、`QN` 记录比 Q 小 1 的备用序列，就能避免在出现负商位时做昂贵的借位传播；最终在 `s_recovery` 用 `Mux(remIsNeg, Q+(~QN), Q-QN)` 一次合并（见 [IntDivMod.scala:69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L69)）。

**练习 2**：`INT_MIN / -1` 为何要特判？

**答案**：数学上 \( -2^{31} / -1 = 2^{31} \)，超出 32 位有符号数能表示的最大值 \( 2^{31}-1 \)，发生溢出。[IntDivMod.scala:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L35) 检测该情形并在 [IntDivMod.scala:75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/IntDivMod.scala#L75) 返回 `INT_MIN`，符合 RISC-V 规范。

---

### 4.5 FloatDivSqrt：浮点除法/平方根迭代核

#### 4.5.1 概念说明

`FloatDivSqrt` 是 SFU 内部的浮点除法/平方根核，同样被例化 `num_sfu` 份。它实现 IEEE754 单精度的 `div.s` 与 `sqrt.s`，采用 **SRT 基 4 迭代**（每拍求出 2 位结果），辅以「on-the-fly conversion」（边迭代边把商位拼起来，无需独立恢复步骤）。

外层 `FloatDivSqrt` 负责浮点格式拆解、特例处理（NaN、Inf、0、除零）、规格化与舍入；内层 `FracDivSqrt` 只对尾数做迭代除/开方。二者构成「外层格式 + 内层尾数」的两级结构。

#### 4.5.2 核心流程

外层 6 态状态机：

```
s_idle → [特例] → s_finish
       → [次正规] → s_norm → s_start → s_compute → s_round → s_finish
       → [正常] → s_start → s_compute → s_round → s_finish
```

1. **s_idle**：用 `Classify` 判别 a/b 类别（NaN/Inf/Zero/次正规等），若是特例（除零、0/0、Inf/Inf、sqrt 负数等）直接算出 `specialResult` 跳 `s_finish`；否则锁存阶码/尾数。
2. **s_norm**（仅次正规数）：左移尾数使其规格化，相应调整阶码。
3. **s_start**：启动 `FracDivSqrt`，计算结果阶码（除法为 `aExp-bExp`，开方为 `aExp/2`）。
4. **s_compute**：等待 `FracDivSqrt` 完成尾数迭代（内部 `s_recurrence` 跑 `len/2` 拍）。
5. **s_round**：依 `rm` 做舍入，处理上溢/下溢。
6. **s_finish**：输出最终 float32 结果与异常 flags。

内层 `FracDivSqrt` 自身又是 4 态 `s_idle → s_recurrence → s_recovery → s_finish`，用 `SrtTable`（商位选择表）、`OnTheFlyConv`（商位即时拼接）、`CSA32`（进位保存加法器）三件套完成 SRT 迭代。

#### 4.5.3 源码精读

外层状态机与特例判别：

[FloatDivSqrt.scala:119-125](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L119-L125) —— 6 态枚举；`isDiv = io.in.bits.op===0.U`（op 为 0 是除法，非 0 是开方）。
[FloatDivSqrt.scala:177-198](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L177-L198) —— `divInvalid`（0/0、Inf/Inf 等返回 NaN）、`divInf`（非零/零返回 Inf）、`sqrtInvalid`（负数开方）等特例；`specialCase` 为真时短路到 `s_finish`。

状态转移与寄存器更新（两段 `switch(state)`）：

[FloatDivSqrt.scala:285-304](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L285-L304) —— 控制流：特例直接进 `s_finish`，次正规进 `s_norm`，其余进 `s_start`。
[FloatDivSqrt.scala:306-338](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L306-L338) —— 数据流：各阶段更新 `aExpReg/aFracReg`，`s_round` 写入舍入后的阶码与尾数。

最终结果与 flags 装配：

[FloatDivSqrt.scala:340-354](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L340-L354) —— `commonResult = Cat(resSign, exp, frac)`；上溢时按舍入模式置 Inf 或 maxNorm；flags 含 invalid/underflow/overflow/infinite/inexact。

内层尾数迭代 `FracDivSqrt`：

[FloatDivSqrt.scala:32-51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L32-L51) —— 4 态枚举；`cnt_next` 从 `len/2` 递减，到 0 时退出 `s_recurrence`，这正是「每拍 2 位、共 len/2 拍」的 SRT 基 4 循环。

SRT 商位选择表与即时拼接：

[fpu_utils.scala:178-210](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L178-L210) —— `SrtTable` 依除数高 3 位与余数高 8 位查表得本拍商位 `q ∈ {-2,-1,0,1,2}`。
[fpu_utils.scala:212-294](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L212-L294) —— `OnTheFlyConv` 维护 `Q`/`QM` 两套寄存器，边迭代边把商位拼入，免除独立恢复阶段。

#### 4.5.4 代码实践

**实践目标**：估算法浮点除法的总延迟，理解为何 SFU 是流水线瓶颈。

**操作步骤**：

1. 在 [FloatDivSqrt.scala:34-36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L34-L36) 确认 `FracDivSqrt` 的迭代计数器从 `(len+1)/2` 起（`len=28+...`，约 14~15 拍）。
2. 累加外层各阶段：`s_idle(1) + s_start(1) + s_compute(≈14) + s_round(1) + s_finish(1)`，单次浮点除法约 **18 拍以上**（次正规数还要多 1 拍 `s_norm`）。
3. 结合 4.3 的分组机制：满 mask 时 SFU 要串行 4 组，故一条向量浮点除法指令的执行时间可达 **4 × 18 ≈ 72 拍**量级，是向量乘法（约 2 拍）的 30 倍以上。

**需要观察的现象**：浮点除法/开方是整个 SM 中最慢的运算，编译器通常会避免在高频循环里使用，或用乘以倒数（`1.0/b` 预计算）等技巧替代。

**预期结果**：从算法层面解释了「为什么 GPU 也怕除法」——不是不会算，而是迭代算法本质上慢，且受面积限制无法全 lane 并行。

> 待本地验证：精确拍数随操作数与是否次正规而变；可在仿真中对 `FloatDivSqrt.io.in.fire` 与 `io.out.fire` 间拍数打点统计。

#### 4.5.5 小练习与答案

**练习 1**：`FloatDivSqrt` 如何区分执行除法还是开方？

**答案**：由输入 `op` 决定，`isDiv = (op === 0.U)`（[FloatDivSqrt.scala:124](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L124)）。在 SFUexe 中 `floatDiv.in.bits.op := i_ctrl.alu_fn(2,0)`（[execution.scala:891](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L891)），译码时为除法与开方分配不同的 `alu_fn(2,0)` 编码。

**练习 2**：为什么 `FracDivSqrt` 用 `CSA32`（进位保存加法器）而不是普通加法器？

**答案**：SRT 每拍要做「余数 ± 除数」的加减，用进位保存形式（保留 sum 与 carry 两路）可避免每拍全宽度加法的进位传播延迟，把关键路径压缩成查表 + 少量位加法，从而提升主频。`CSA32` 在 [fpu_utils.scala:296-306](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/fpu_utils.scala#L296-L306) 定义，由 [FloatDivSqrt.scala:88-90](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/FloatDivSqrt.scala#L88-L90) 每拍消费。

---

## 5. 综合实践

**综合任务**：画一张「Ventas SM 后端运算单元资源与性能对照表」，并据此解释 GPU 编程中「除法昂贵」的硬件根因。

请完成以下步骤：

1. **统计资源**：在 [pipe.scala:76-84](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L76-L84) 列出所有执行单元，结合本讲与 u5-l1/u5-l2，填写下表（示例骨架）：

   | 单元 | 例化数量 | 单次延迟（拍） | 全 lane 并行？ | 写回端口 |
   | --- | --- | --- | --- | --- |
   | ALUexe（标量） | 1 | 1（直通+1拍FIFO） | 否（标量） | in_x(0) |
   | vALUv2（向量） | num_lane=32 | 1 | 是 | in_v(0) |
   | vMULv2（向量乘） | num_lane=32 | 2 | 是 | in_x(5)/in_v(4) |
   | FPUexe（浮点） | 1（fpuv2 内部 lane） | fpuv2 决定 | 是 | in_x(1)/in_v(1) |
   | SFUexe（除法等） | num_sfu=8 | 多周期（十几~三十拍） | **否，分组串行** | in_x(4)/in_v(3) |

2. **追踪路由**：在 [issue.scala:99-112](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L99-L112) 确认指令经 `tc/sfu/fp/mul` 标志分发；在 [pipe.scala:394-399](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L394-L399) 确认 mul/fpu 仅接 issueV、标量侧 ready 恒假。

3. **定量对比**：基于上表，写一段话说明：为什么同一份「32 线程」的向量代码，把 `c = a / b` 改写成 `c = a * (1.0f/b)`（预先算倒数）往往能显著提速——因为前者命中 SFU 的「num_sfu 分组串行 + 多周期迭代」双重惩罚，后者只命中 FPU/MUL 的全并行流水。

4. **延伸思考**：若要把 SFU 做成「每拍能接收新指令」的流水化设计，需要改动哪些地方？（提示：当前 `SFUexe` 在 `s_busy` 期间 `io.in.ready` 为假，是结构性阻塞；要流水化需在输入端加 FIFO 并允许多条 SFU 指令交错迭代。）

## 6. 本讲小结

- **vMULv2** 用「2 拍流水的 `ArrayMultiplier` × `num_thread` 份」实现全 lane 并行的整数乘法/乘加，吞吐近每拍一条，支持 `FN_MUL/MULH/MULHU/MULHSU/MACC/MADD/NMSUB` 等。
- **FPUexe** 是适配层，把 `vExeData`+`alu_fn` 桥接给外部依赖 `fpuv2.VectorFPU`，完成单精度浮点全功能，`alu_fn` 直接充当浮点 op，FMA 类做操作数重排。
- **SFUexe** 用三态状态机 + `PriorityEncoder(mask_grp)` 把 32 个 lane 按 `num_sfu=8` 分组串行处理，是「chime 变长」的直接来源；`num_sfu = (num_thread>>2).max(1)` 是面积与性能的权衡旋钮。
- **IntDivMod** 是 5 态移位减除式整数除法核，用 `Q`/`QN` 双寄存器处理负商位，处理除零与 `INT_MIN/-1` 溢出特例。
- **FloatDivSqrt** 是 6 态浮点除/开方核，内层 `FracDivSqrt` 用 SRT 基 4 + OnTheFlyConv + CSA32 迭代求尾数，外层负责格式拆解、特例、舍入。
- **核心结论**：乘法「全并行 + 2 拍」、除法「分组串行 + 多周期」，二者执行时间可差一两个数量级——这就是「GPU 也怕除法」的硬件根因，也是编译器用「乘倒数」优化除法的动力。

## 7. 下一步学习建议

- **u5-l4（LSU 访存单元）**：本讲讲完了「运算」，下一讲进入「访存」，LSU 同样面临向量地址合并、MSHR 在途管理等结构性问题，可与本讲 SFU 的分组/串行思想对照阅读。
- **u5-l6（写回与 CSR）**：本讲反复提到的 `out_x/out_v`、`wxd/wvd`、`rm` 舍入模式如何在 `Writeback` 与 `CSRFile` 汇合收口，将在该讲闭环。
- **延伸阅读**：若对除法算法感兴趣，可对照 `dependencies/` 下 `fpuv2` 的源码，理解 `VectorFPU` 内部如何把多条浮点指令流水化；SRT 算法可进一步阅读 radix-4 SRT 经典论文，本讲的 `SrtTable` 即其查表实现。
- **实践建议**：尝试修改 `parameters.scala` 中 `num_sfu` 或 `num_thread`，重新 `make verilog`，观察生成 RTL 中除法核与乘法核例化数量的变化，建立「参数 → 面积 → 性能」的直观映射。
