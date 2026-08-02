# 发射与执行单元总览

## 1. 本讲目标

本讲是 SM 流水线「后端」的第一篇，承接 u4 单元（取指→译码→ibuffer→记分板→操作数收集）。学完本讲你应当掌握：

- 理解 Ventus 的**双发射（dual-issue）**机制：为什么用两个 `Issue` 模块（`issueX`/`issueV`）而非一个，标量指令与向量指令如何分流。
- 读懂 `Issue` 模块内部按控制信号（`tc/sfu/fp/csr/mul/mem/isvec/barrier`）的**优先级分发**逻辑，以及 `out_warpscheduler` 这条用于 `barrier`/`endprg` 的旁路。
- 在 `pipe.scala` 中定位全部 8 类执行单元（`ALUexe`/`vALUv2`/`FPUexe`/`LSUexe`/`SFUexe`/`vMULv2`/`vTCexe` 及 `CSRexe`）的实例化与连接，并能说出每类指令「经哪个 Issue 端口→进哪个执行单元→从哪个写回端口出去」。
- 建立「向量执行单元 = 把标量运算单元复制 `num_thread` 份、每 lane 并行」的核心直觉。

本讲是**总览**：把每个执行单元的「门面」（输入输出 Bundle、周期特性、写回端口）交代清楚，为后续 u5-l2（ALU 详解）、u5-l3（浮点/乘法/SFU）、u5-l4（LSU）、u5-l5（SIMT 分支）、u5-l6（写回/CSR）铺好地图，但不深入每个单元的算法细节。

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 u4 单元）：

- **CtrlSigs 控制信号包**：译码器把 32 位指令翻译成的一组字段，其中 `isvec/fp/branch/mem/mul/sfu/csr/barrier/tc` 等布尔位是本讲的「路由标签」，`alu_fn` 是运算类型，`simt_stack/simt_stack_op` 与分支有关（见 u4-l2）。
- **vExeData / sExeData**：操作数收集器（operandCollector）送出来的执行数据包。向量包 `vExeData` 含 `in1/in2/in3`（各 `num_thread` 个 lane）+ `mask`（每 lane 一个有效位）+ `ctrl`；标量包 `sExeData` 只有标量 `in1/in2/in3` + `ctrl`。
- **warp / lane / mask**：一个 warp 有 `num_thread`（默认 32）个线程，硬件上叫 lane；`mask` 标记本拍哪些 lane 真正参与运算（SIMT 分支分歧时会屏蔽部分 lane）。
- **DecoupledIO 握手**：`valid`/`ready` 同时为真时 `fire`（成交一拍），本讲大量出现。

一个贯穿全讲的关键参数（来自 `parameters.scala`）：`num_thread = 32`、`num_lane = num_thread = 32`、`num_sfu = (num_thread >> 2).max(1) = 8`、`num_issue = 1`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ventus/src/pipeline/issue.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala) | 定义 `Issue`（发射/分发器）、`XVDualIssue`、`IssueV2`，以及执行数据 Bundle（`vExeData`/`sExeData` 等）。本讲主角。 |
| [ventus/src/pipeline/execution.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala) | 定义标量/向量执行单元：`ALUexe`、`vALUv2`、`vMULv2`、`vTCexe`、`FPUexe`、`SFUexe`（及遗留的 `vMULexe`/`vALUexe`）。 |
| [ventus/src/pipeline/pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | SM 流水线总装：例化两个 `Issue` 与全部执行单元，并把它们的 `in/out` 连起来、汇聚到写回 `wb`。 |
| [ventus/src/pipeline/ALU.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala) | `ScalarALU`：单 lane 的算术逻辑单元，是 `ALUexe` 与 `vALUv2` 的共用零件。 |
| [ventus/src/pipeline/operandCollector.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala) | `WriteVecCtrl`/`WriteScalarCtrl` 写回 Bundle 定义（执行单元的输出格式）。 |
| [ventus/src/pipeline/SIMT_STACK.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala) | `simtExeData`/`vec_alu_bus` Bundle 定义，`vALU` 与 SIMT 分支栈交互用。 |

---

## 4. 核心概念与源码讲解

### 4.1 Issue 模块：双发射与按类型分发

#### 4.1.1 概念说明

「发射（Issue）」是流水线里「把一条已经备齐操作数、且通过了记分板冒险检查的指令，送到对应执行单元入口」的那一拍。在 Ventus 里，这一步要解决两个问题：

1. **送到哪个执行单元？** 一条指令可能是标量算术、向量算术、浮点、访存、分支、CSR、张量核……执行单元是「各管一摊」的独立硬件，必须按指令类型分发（demux）。
2. **能不能一个周期同时发两条？** GPU 指令流里标量指令（如地址计算、循环计数）和向量指令（如真正的数据运算）经常相邻，让它们同拍并行进入不同执行单元，能显著提升吞吐。这就是**双发射**。

Ventus 的做法很直白：**例化两个完全相同的 `Issue` 模块**，一个专门收标量指令（`issueX`），一个专门收向量指令（`issueV`）。因为标量和向量走两条独立的物理通路，天然可以同拍各发一条。

> 注意：`issue.scala` 里还有一个 `IssueV2` 类（用 `RRArbiter` 在多输入间仲裁），但 `pipe.scala` **并没有使用它**，实际生效的是最朴素的 `Issue` 类。本讲以实际生效的 `Issue` 为准。

#### 4.1.2 核心流程

标量/向量的分流其实发生在 `Issue` 之前：`ibuffer2issue` 用一个 `inst_is_vec` 判据把每条指令分到 `out_x`（标量）或 `out_v`（向量）：

```
ibuffer2issue.out_x ──► exe_dataX(直通) ──► issueX.io.in ──┬─► out_sALU  ─► alu     (标量算术/跳转)
                                                           ├─► out_CSR   ─► csrfile (CSR 读写)
                                                           └─► out_warpscheduler ─► warp_sche (barrier/endprg)

ibuffer2issue.out_v ──► exe_dataV(直通) ──► issueV.io.in ─┬─► out_vALU ─► valu        (向量算术)
                                                          ├─► out_vFPU ─► fpu         (浮点)
                                                          ├─► out_MUL  ─► mul         (向量乘法)
                                                          ├─► out_TC   ─► tensorcore  (张量核)
                                                          ├─► out_SFU  ─► sfu         (除法/开方等)
                                                          ├─► out_LSU  ─► lsu         (访存)
                                                          └─► out_SIMT ─► simt_stack  (SIMT 分支)
```

`inst_is_vec` 判据（来自 `XVDualIssue`，与 `ibuffer2issue` 一致）：`tc/fp/mul/sfu/mem` 或 `isvec` 为真 → 向量；`csr/barrier` 或默认 → 标量。

进入 `Issue` 内部后，再用一条**优先级 `when` 链**从 10 个输出端口里挑一个驱动（其余 `valid` 保持 `false.B`）。优先级从高到低：

```
tc > sfu > fp > csr > mul > mem > isvec(再看 simt_stack) > barrier > 否则 sALU
```

被选中的端口 `valid := inputBuf.valid`，并把输入就绪信号 `inputBuf.ready` 接到该端口的 `ready`，形成标准 DecoupledIO 握手。

#### 4.1.3 源码精读

**执行数据 Bundle**（[issue.scala:17-38](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L17-L38)）：定义了三种执行包。`vExeData` 是向量包（lane 数组），`sExeData` 是标量包，`warpSchedulerExeData`/`csrExeData` 是给 `barrier` 和 CSR 的精简包（只带 `ctrl`）。

```scala
class vExeData extends Bundle{
  val in1=Vec(num_thread,UInt(xLen.W))   // 源操作数1，每 lane 一个
  val in2=Vec(num_thread,UInt(xLen.W))
  val in3=Vec(num_thread,UInt(xLen.W))
  val mask=Vec(num_thread,Bool())        // 每 lane 有效位
  val ctrl=new CtrlSigs()
}
class sExeData extends Bundle{ val in1=UInt(xLen.W); val in2=UInt(xLen.W); val in3=UInt(xLen.W); val ctrl=new CtrlSigs() }
```

**`Issue` 的 10 个输出端口**（[issue.scala:41-53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L41-L53)）：注意 `in` 是 `vExeData`（统一用向量包承载，标量端口只取 `in1(0)`），输出按执行单元类型分了 10 路。

**优先级分发链**（[issue.scala:98-139](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L98-L139)）：这是 `Issue` 的核心。先默认所有 `valid` 为假、`inputBuf.ready` 为假，再用 `when/elsewhen` 链命中第一个匹配的类型：

```scala
when(inputBuf.bits.ctrl.tc){ io.out_TC.valid:=inputBuf.valid; inputBuf.ready:=io.out_TC.ready }
.elsewhen(inputBuf.bits.ctrl.sfu){ ... io.out_SFU ... }
.elsewhen(inputBuf.bits.ctrl.fp){ ... io.out_vFPU ... }
.elsewhen(inputBuf.bits.ctrl.csr.orR){ ... io.out_CSR ... }
.elsewhen(inputBuf.bits.ctrl.mul){ ... io.out_MUL ... }
.elsewhen(inputBuf.bits.ctrl.mem){ ... io.out_LSU ... }
.elsewhen(inputBuf.bits.ctrl.isvec){
  when(inputBuf.bits.ctrl.simt_stack){ /* beqv 需 vALU+SIMT 同拍，见 4.4 */ }.otherwise{ ... io.out_vALU ... }
}
.elsewhen(inputBuf.bits.ctrl.barrier){ ... io.out_warpscheduler ... }
.otherwise({ ... io.out_sALU ... })   // 兜底：标量算术/普通跳转
```

**`out_warpscheduler` 旁路**（[issue.scala:83](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L83) 与 [issue.scala:76-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L76-L81)）：`barrier`（栅栏同步）和 `endprg`（warp 结束）这两类指令**不写寄存器、也不走运算单元**，它们要直接通知 warp 调度器（`warp_sche`）去阻塞/回收 warp。所以 `Issue` 专门留了 `out_warpscheduler` 端口，只透传 `ctrl`。开启 `SPIKE_OUTPUT` 时还会在这里打印 `"barrier"` 或 `"endprg"` 日志，方便对拍调试。

**双实例在 `pipe.scala` 的连接**（[pipe.scala:74-75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L74-L75) 与 [pipe.scala:374-401](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374-L401)）：两个 `Issue` 各喂一个执行单元子集，**未使用的「跨侧」端口被强制 `ready:=false.B`**（等于禁用）：

```scala
val issueX = Module(new Issue)   // 标量侧
val issueV = Module(new Issue)   // 向量侧
...
issueX.io.out_sALU <> alu.io.in          // issueX 真正用到的：sALU
issueV.io.out_sALU.ready := false.B      // issueV 的 sALU 端口禁用
issueX.io.out_CSR <> csrfile.io.in       // issueX：CSR
issueX.io.out_warpscheduler <> warp_sche.io.warp_control  // issueX：barrier/endprg 旁路
issueV.io.out_vALU <> valu.io.in         // issueV 真正用到的：vALU/LSU/SIMT/SFU/MUL/TC/vFPU
issueX.io.out_vALU.ready := false.B
...
```

> **为什么不让一个 `Issue` 干所有事？** 因为这样标量指令和向量指令会争同一个分发器的同一拍，只能二选一。拆成 `issueX`/`issueV` 后，两条独立通路可以同拍各发一条，实现真正的双发射。代价是连线略繁、跨侧端口要显式禁用。

**双发射的停顿信号**（[pipe.scala:429](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L429)）：

```scala
issue_stall := (~issueX.io.in.ready).asBool | (~issueV.io.in.ready).asBool
```

只要任意一侧的入口没就绪（下游执行单元没吃完），整条发射就停顿——这会被记分板/ibuffer 用到。

#### 4.1.4 代码实践

**目标**：亲手验证「两个 `Issue` + 跨侧禁用」的双发射结构。

**步骤**：
1. 打开 `ventus/src/pipeline/pipe.scala`，定位第 74–75 行的两个 `Module(new Issue)`。
2. 向下读到第 374–401 行，把每一条 `<>` 连线和每一条 `.ready := false.B` 分别抄到两张表里：一张「issueX 实际驱动」，一张「issueV 实际驱动」。
3. 对每条 `ready := false.B`，确认它对应的正是「另一侧才会用到」的端口（例如 `issueX.io.out_vALU.ready := false.B`，因为 vALU 只归 issueV）。

**预期结果**：你会得到一张清晰的归属表——issueX 只真正驱动 `sALU/CSR/warpscheduler` 三路，issueV 驱动 `vALU/vFPU/MUL/TC/SFU/LSU/SIMT` 七路，没有任何一个执行单元被两侧同时驱动。

**待本地验证**：若你想确认 `IssueV2` 真的没被使用，可在仓库根目录执行 `grep -rn "new IssueV2" ventus/src`，应无输出。

#### 4.1.5 小练习与答案

**练习 1**：一条 `csr.orR` 为真、且 `isvec` 也为真的指令，会被 `Issue` 分发到哪个端口？
**答案**：`out_CSR`。因为优先级链里 `csr`（第 4 级）排在 `isvec`（第 7 级）之前，先命中者胜。

**练习 2**：为什么 `issueX` 和 `issueV` 用的是**同一个** `Issue` 类，而不是写两个不同的类？
**答案**：`Issue` 类内部是纯组合的优先级 demux，与「标量/向量」无关；标量/向量的区分在更上游（`ibuffer2issue` 的 `inst_is_vec`）就完成了。用同一个类可以复用代码，靠 `pipe.scala` 里「跨侧端口 `ready:=false`」来约束每个实例实际驱动的子集。

---

### 4.2 执行单元阵列与写回汇聚

#### 4.2.1 概念说明

`Issue` 把指令分发出去后，真正干活的是一排**执行单元（Execution Unit, EU）**。Ventus 的 SM 后端有这些执行单元：

| 执行单元 | 模块类 | 处理的指令 | 关键特性 |
|----------|--------|-----------|----------|
| 标量 ALU | `ALUexe` | 标量算术/逻辑/移位/比较/跳转 | 1 拍，单 lane |
| 向量 ALU | `vALUv2` | 向量整型算术/逻辑/比较 | `num_thread` 个 lane 并行 |
| 浮点 FPU | `FPUexe` | 单精度浮点加减乘乘加 | 对接 `fpuv2` 依赖，多拍流水 |
| 向量乘法 | `vMULv2` | 向量整型乘/乘加 | `num_thread` 个 `ArrayMultiplier` |
| 张量核 TC | `vTCexe` | FP32 矩阵乘（TensorCore） | 对接 `fpuv2.TensorCoreFP32` |
| 特殊运算 SFU | `SFUexe` | 整除/取余、浮点除法/开方 | 多周期，单元数 = `num_sfu` |
| 访存 LSU | `LSUexe` | load/store/fence | 地址计算+合并+路由 cache（详见 u5-l4） |
| CSR | `CSRexe` | CSR 读写、warp 启动初始化 | 详见 u5-l6 |

所有执行单元的**结果**最后都要汇到写回模块 `wb`，再写回寄存器堆。`wb` 有两组入口：标量 `in_x`（6 路）和向量 `in_v`（6 路）。

#### 4.2.2 核心流程

```
                           ┌─ ALUexe.out ─────────────────────► wb.in_x(0)
issueX ─► {sALU,CSR,ws} ──►├─ CSRexe.out ────────────────────► wb.in_x(3)
                           └─ (warpscheduler 直达 warp_sche，不写回)

                           ┌─ vALUv2.out ─────────────────────► wb.in_v(0)
                           ├─ FPUexe.out_x / out_v ───────────► wb.in_x(1) / wb.in_v(1)
issueV ─► {vALU,vFPU,MUL,  ├─ vMULv2.out_x / out_v ───────────► wb.in_x(5) / wb.in_v(4)
   TC,SFU,LSU,SIMT} ──────►├─ vTCexe.out_v ───────────────────► wb.in_v(5)
                           ├─ SFUexe.out_x / out_v ───────────► wb.in_x(4) / wb.in_v(3)
                           └─ LSUexe.lsu_rsp ─► lsu2wb ────────► wb.in_x(2) / wb.in_v(2)
```

注意几个细节：
- 能同时产生标量和向量结果的单元（FPU/MUL/SFU）有两个输出端口 `out_x`/`out_v`，分别接 `wb` 的标量/向量入口。具体走哪路由 `ctrl.wxd`（写标量）/`ctrl.wvd`（写向量）决定。
- LSU 的结果先过一个 `lsu2wb` 适配器再进 `wb`（因为访存返回时序较复杂）。
- `warpscheduler`（barrier/endprg）和 SIMT 分支栈的结果**不进 `wb`**，而是回送给 `warp_sche` / `simt_stack`（控制通路，非数据写回）。

#### 4.2.3 源码精读

**执行单元实例化**（[pipe.scala:76-82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L76-L82) 与 [pipe.scala:131](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L131)）：

```scala
val alu       = Module(new ALUexe)
val valu      = Module(new vALUv2(num_thread, num_lane))   // softThread=32, hardThread=32
val fpu       = Module(new FPUexe(num_thread, num_lane))
val lsu       = Module(new LSUexe)
val sfu       = Module(new SFUexe)
val mul       = Module(new vMULv2(num_thread, num_lane))
val tensorcore= Module(new vTCexe)
val csrfile   = Module(new CSRexe())
```

**舍入模式 `rm` 的分发**（[pipe.scala:403-408](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L403-L408)）：浮点类单元（FPU/SFU/TC）需要舍入模式，它从 `csrfile` 按「哪个单元在用」动态取：`fpu.io.rm` 来自 `csrfile.io.rm(0)`，`sfu` 来自 `rm(1)`，`tensorcore` 来自 `rm(2)`，并用 `rm_wid` 反向告知 csrfile「当前 wid」，以便 csrfile 给出该 warp 的舍入模式。

**写回汇聚**（[pipe.scala:416-427](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416-L427)）：

```scala
wb.io.in_x(0)<>alu.io.out        // 标量写回 6 路
wb.io.in_x(1)<>fpu.io.out_x
wb.io.in_x(2)<>lsu2wb.io.out_x
wb.io.in_x(3)<>csrfile.io.out
wb.io.in_x(4)<>sfu.io.out_x
wb.io.in_x(5)<>mul.io.out_x
wb.io.in_v(0)<>valu.io.out       // 向量写回 6 路
wb.io.in_v(1)<>fpu.io.out_v
wb.io.in_v(2)<>lsu2wb.io.out_v
wb.io.in_v(3)<>sfu.io.out_v
wb.io.in_v(4)<>mul.io.out_v
wb.io.in_v(5)<>tensorcore.io.out_v
```

这张表是本讲的「总纲」，后续每个执行单元的讲解都会回到这里对号入座。

#### 4.2.4 代码实践

**目标**：建立「指令类型 → Issue 端口 → 执行单元 → 写回端口」的完整映射。

**步骤**：在 `pipe.scala` 第 374–427 行范围内，按下表逐行核对并填满它（部分已给出）：

| 指令类型 | 命中 ctrl 位 | 经 Issue 端口 | 进执行单元 | 出写回端口 |
|----------|-------------|--------------|-----------|-----------|
| 标量加法 | (默认) | `issueX.out_sALU` | `alu` | `wb.in_x(0)` |
| 向量加法 | `isvec` | `issueV.out_vALU` | `valu` | `wb.in_v(0)` |
| 浮点乘加 | `fp` | ？ | ？ | ？ |
| 向量 load | `mem` | ？ | ？ | ？ |
| 整数除法 | `sfu` | ？ | ？ | ？ |
| CSR 写 | `csr.orR` | ？ | ？ | ？ |
| barrier | `barrier` | `issueX.out_warpscheduler` | (直达 `warp_sche`) | （不写回） |

**预期结果**：你能不查源码地背出这张映射表，并且理解为什么 `barrier` 那一行的「写回端口」是空的。

#### 4.2.5 小练习与答案

**练习**：`FPUexe` 同时挂在 `wb.in_x(1)` 和 `wb.in_v(1)`，会不会出现「同拍既写标量又写向量」？
**答案**：不会冲突。`FPUexe` 用 `ctrl.wxd`/`ctrl.wvd` 互斥地置 `out_x.valid`/`out_v.valid`（见 [execution.scala:801-802](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L801-L802)），同一条浮点指令要么写标量寄存器、要么写向量寄存器，二者只居其一。

---

### 4.3 标量 ALU 与向量 ALU：lane 复制的核心直觉

#### 4.3.1 概念说明

`ALUexe`（标量）和 `vALUv2`（向量）是理解「GPU 怎么并行计算」的钥匙。它们的**运算核心是同一个零件 `ScalarALU`**，区别只在于数量：

- `ALUexe`：例化 **1 个** `ScalarALU`，只算 lane 0（标量值），1 拍出结果。
- `vALUv2`：例化 **`hardThread`（默认 32）个** `ScalarALU`，每个 lane 一个，**同拍并行**算完整个 warp 的 32 个线程。

这就是「lane 复制」：把同一个单线程运算单元铺成 `num_thread` 份，用面积换并行度。`vALUv2` 还有一个 `softThread`/`hardThread` 参数对：当硬件 lane 数（`hardThread`）少于软件线程数（`softThread`）时，需要分多个 chime（迭代）轮流算完。默认配置下 `num_lane = num_thread = 32`，两者相等，走最简单的「一拍全算」路径。

此外，`ALUexe` 还兼职**标量跳转**：比较/分支的结果通过 `out2br` 送给 `branch_back`，再回传给 warp 调度器改 PC。`vALUv2` 则有 `out2simt_stack`：当指令是 SIMT 分支（`simt_stack` 位为真）时，把各 lane 的比较结果汇成一个 `if_mask` 送给 SIMT 分支栈（详见 u5-l5）。

#### 4.3.2 核心流程

`ScalarALU` 的运算是纯组合的，用 5 位 `func`（取自 `ctrl.alu_fn` 的低 5 位）选择运算：

```
func → { ADD/SUB, SL/SR/SRA(移位), XOR/OR/AND(逻辑), SLT/SLTU/SEQ/SNE(比较), MIN/MAX, A1ZERO/A2ZERO }
```

`vALUv2`（简单路径，`softThread==hardThread`）的流程：

```
for x in 0 until num_thread:
    alu(x).in1 := in1(x); alu(x).in2 := in2(x); alu(x).func := alu_fn[4:0]
    result.wb_wvd_rd(x) := alu(x).out          // 每 lane 各自的结果
result.wvd_mask := in.mask                       // 带上 mask 写回
if (simt_stack): result2simt.if_mask := ~(各 lane cmp_out)   // 喂给 SIMT 栈
```

#### 4.3.3 源码精读

**`ALUexe` 结构**（[execution.scala:26-65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L26-L65)）：例化 1 个 `ScalarALU`，结果过两个深度 1 的流水寄存器 `result`/`result_br` 再输出。注意它依据 `ctrl.branch`（`B_B`/`B_N`/`B_J`/`B_R`）决定要不要产生跳转信号：

```scala
val alu = Module(new ScalarALU())
alu.io.func := io.in.bits.ctrl.alu_fn(4,0)        // 只用低5位
...
result_br.io.enq.bits.jump := MuxLookup(io.in.bits.ctrl.branch, false.B)(Seq(B_B->alu.io.cmp_out, B_J->true.B, B_R->true.B))
```

`ALUexe` 有两个输出：`out`（写回标量寄存器，`WriteScalarCtrl`）和 `out2br`（跳转控制，`BranchCtrl`，接到 `branch_back`）。在 `pipe.scala:392` 处 `alu.io.out2br <> branch_back.io.in0`。

**`ScalarALU` 运算本体**（[ALU.scala:61-117](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L61-L117)）：用加法器（`adder_out = in1 + ~in2 + isSub`）同时支持 ADD/SUB 和比较（SLT 借用加法器符号位），移位用桶形移位器，最后用 `Mux` 按 `func` 选出最终 `out`。运算函数编码定义在 [ALU.scala:17-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L17-L57)（如 `FN_ADD=0`、`FN_SUB=10`、`FN_SLT=12`）。

**`vALUv2` 的 lane 复制**（[execution.scala:502](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L502) 与 [execution.scala:507-543](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507-L543)）：

```scala
val alu = VecInit(Seq.fill(hardThread)((Module(new ScalarALU())).io))   // 32 个 ScalarALU
...
(0 until num_thread).foreach(x => {
  alu(x).in1 := io.in.bits.in1(x)
  alu(x).func := io.in.bits.ctrl.alu_fn(4,0)
  result.io.enq.bits.wb_wvd_rd(x) := alu(x).out          // 每 lane 一份
  when(io.in.bits.ctrl.alu_fn === FN_VID){ result.io.enq.bits.wb_wvd_rd(x) := x.asUInt }  // vid: 写 lane 号
  when(io.in.bits.ctrl.alu_fn === FN_VMERGE){ ... Mux(mask(x), in1(x), in2(x)) ... }
})
```

注意几个向量专有运算：`FN_VID`（写 lane 序号，常用于获取 thread id）、`FN_VMERGE`（按 mask 在两源间选择）、`writemask`（把逐 lane 的 1 bit 结果压成一个字）。这些都靠「每 lane 独立算 + mask 选择」实现。

`vALUv2` 的 SIMT 分支输出（[execution.scala:562-564](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L562-L564)）：当 `ctrl.simt_stack` 为真，把 32 个 lane 的 `cmp_out` 取反汇成 `if_mask`，从 `out2simt_stack` 送出，在 `pipe.scala:387` 接到 `simt_stack.io.if_mask`。

#### 4.3.4 代码实践

**目标**：对比标量与向量 ALU 的「面积 vs 吞吐」。

**步骤**：
1. 在 `execution.scala` 第 32 行（`ALUexe`）确认只有 `Module(new ScalarALU())` 一个实例。
2. 在 `execution.scala` 第 502 行（`vALUv2`）确认是 `Seq.fill(hardThread)(...)`，默认即 32 个实例。
3. 数一下：同样执行一条加法，`ALUexe` 动用 1 个加法器算 1 个数；`vALUv2` 动用 32 个加法器同拍算 32 个数。

**预期结果**：你直观理解了「向量指令之所以一条顶 32 条，是因为硬件铺了 32 套运算单元」，以及为什么 mask 只屏蔽结果而不省功耗（硬件照算，只是写回时丢弃）。

#### 4.3.5 小练习与答案

**练习 1**：`vALUv2` 为什么要 `assert(softThread % hardThread == 0)`？
**答案**：当 `hardThread < softThread` 时，warp 的线程要分 `softThread/hardThread` 个迭代（chime）轮流送进硬件 lane，必须整除才能均分；默认 `32%32==0` 走单拍路径，该断言为后续「软线程多于硬 lane」的可配置设计做合法性约束。

**练习 2**：`FN_VID` 指令（取 lane 号）为什么不需要读任何源操作数？
**答案**：它的结果就是 lane 序号 `x`（见 [execution.scala:536-538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L536-L538)），直接由硬件位置决定，软件常用它获取 `thread id`。

---

### 4.4 浮点 FPU、向量乘法 vMULv2 与张量核 vTCexe

#### 4.4.1 概念说明

这三个执行单元都依赖外部子模块 `fpuv2`（见 u1-l3 的 `dependencies/`），且都支持「能写标量也能写向量」的双出口：

- **`FPUexe`**：包装 `fpuv2.VectorFPU`，做单精度浮点加减乘、乘加（FMADD/FMSUB 等）。构造参数 `(8, 24)` 是 FP32 的指数位和尾数位。
- **`vMULv2`**：整型向量乘法/乘加，每个 lane 一个 `ArrayMultiplier`（2 拍流水）。结构与 `vALUv2` 高度对称（同样有 `softThread/hardThread` 参数和 lane 复制）。
- **`vTCexe`**：包装 `fpuv2.TensorCoreFP32`，做 FP32 矩阵乘（张量核），维度由 `tc_dim` 决定。只有向量输出口 `out_v`。

它们的舍入模式 `rm` 都来自 `csrfile`（见 4.2.3），且 `Issue` 内部有一条特殊规则：**SIMT 分支指令 `beqv`（`simt_stack_op===0`）需要 `vALU` 与 `SIMT` 同拍成交**（见 [issue.scala:120-124](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L120-L124)），因为分支判定依赖 vALU 的比较结果。

#### 4.4.2 核心流程

以 `FPUexe` 为例：

```
for x in 0 until num_thread:
    fpu.in.data(x).a := in1(x); .b := in2(x); .c := in3(x)
    fpu.in.data(x).op := alu_fn            // 浮点运算类型
    (FMADD 系列特殊处理 a/b/c 的对应关系)
fpu.in.valid := in.valid
out_v.valid := fpu.out.valid & ctrl.wvd    // 写向量
out_x.valid := fpu.out.valid & ctrl.wxd    // 写标量（取 lane0）
```

`vMULv2` 同理，只是把 `ScalarALU` 换成 `ArrayMultiplier`。`vTCexe` 把 `a/b/c` 三组数据按 `(0 until num_thread)` 灌进 `TensorCoreFP32`，结果从 `out_v` 出。

#### 4.4.3 源码精读

**`FPUexe`**（[execution.scala:746-804](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L746-L804)）：核心是包装 `VectorFPU`，并把 `alu_fn` 映射成 fpu 的 `op`。乘加类指令（`FN_VFMADD` 等）做一次 `op := alu_fn - 10.U` 的重映射并对调操作数来源（[execution.scala:769-774](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L769-L774)）。双出口互斥见 [execution.scala:801-803](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L801-L803)。

**`vMULv2`**（[execution.scala:164-413](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L164-L413)）：结构是 `vALUv2` 的翻版——`Seq.fill(hardThread)(Module(new ArrayMultiplier(softThread, xLen)))`（[execution.scala:188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L188)），简单路径下每 lane 一个乘法器，支持 `reverse`（交换 a/b）和乘加。`ArrayMultiplier` 本体定义在 [Multiplier.scala:149](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/Multiplier.scala#L149)。

**`vTCexe`**（[execution.scala:115-162](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L115-L162)）：例化 `TensorCoreFP32(num_thread, tc_dim(0), tc_dim(1), tc_dim(2), ...)`，把 `in1/in2/in3` 当矩阵 a/b/c 喂入，结果写回向量寄存器（`out_v`）。它没有标量输出口。

#### 4.4.4 代码实践

**目标**：理解 `beqv` 这条 SIMT 分支指令为何要「vALU + SIMT 同拍」。

**步骤**：阅读 [issue.scala:118-132](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L118-L132)。当 `isvec && simt_stack` 同时成立时：
- 若 `simt_stack_op === 0`（即 `beqv`），要求 `io.out_vALU.ready & io.out_SIMT.ready` 同时为真才成交，二者绑定一起发；
- 否则（`join` 等纯 SIMT 操作）只发 `out_SIMT`。

**预期结果**：你能解释「为什么 `beqv` 必须等 vALU」——因为分支是否成立要靠 vALU 算出各 lane 的比较结果（`if_mask`），SIMT 栈拿到这个 mask 才能决定压栈/出栈。这条数据依赖强制两者同拍绑定。

#### 4.4.5 小练习与答案

**练习**：`vTCexe` 为什么没有 `out_x`（标量输出口），而 `FPUexe`/`vMULv2` 都有？
**答案**：张量核的语义是矩阵乘，结果天然是整条向量（一组 lane），不存在「只写一个标量」的用法；而浮点和整型乘法指令既可能写向量寄存器（`wvd`）也可能写标量寄存器（`wxd`，如标量乘法），所以需要双出口。参见 [pipe.scala:421-427](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L421-L427)：`mul`/`fpu`/`sfu` 都占 `in_x` 和 `in_v` 两格，而 `tensorcore` 只占 `in_v(5)`。

---

### 4.5 SFU 多周期单元与 LSU 访存单元

#### 4.5.1 概念说明

这两个单元是 SM 后端里「慢」的一类：

- **`SFUexe`（Special Function Unit）**：处理**多周期、低吞吐**的运算——整数除法/取余（`IntDivMod`）、浮点除法/开方（`FloatDivSqrt`）。因为除法器很贵，硬件只放 `num_sfu = num_thread/4 = 8` 个（不是 32 个），所以一条向量除法指令要把 32 个 lane **分成 `num_grp = num_thread/num_sfu = 4` 组**，每组 8 个 lane 轮流算，占用多个周期（即 chime 变长）。它用三态有限状态机 `s_idle → s_busy → s_finish` 控制整个多周期流程。
- **`LSUexe`（Load/Store Unit）**：访存单元，处理向量 load/store/fence。它要做地址计算、把逐 lane 的地址**合并（coalesce）到 cacheline**、按地址范围路由到 sharedmem 或 dcache、用 MSHR 记录在途请求。这是后端最复杂的单元，本讲只讲它的「门面」，细节留给 u5-l4。

两者都是双出口（`out_x`/`out_v`），结果经 `wb.in_x(4)/in_v(3)`（SFU）和 `wb.in_x(2)/in_v(2)`（LSU 经 `lsu2wb`）写回。

#### 4.5.2 核心流程

`SFUexe` 的状态机：

```
s_idle:  收到指令 → 锁存 mask，进入 s_busy，i_valid 拉高
s_busy:  用 PriorityEncoder 选当前有效组 i_cnt，把该组 num_sfu 个 lane 的数据
         喂给 IntDivMod/FloatDivSqrt；算完一组 → 清该组 mask 位，i_cnt 前进
         全部组算完 → 进入 s_finish
s_finish: 把 32 lane 结果一次性送上 out_x/out_v，下游 ready 后回 s_idle
```

`LSUexe` 的接口（仅门面）：

```
lsu_req (来自 issueV.out_LSU) ─► LSU ─┬─ dcache_req/dcache_rsp  (全局内存)
                                      ├─ shared_req/shared_rsp  (共享内存)
                                      └─ lsu_rsp ─► lsu2wb ─► wb.in_x(2)/in_v(2)
```

#### 4.5.3 源码精读

**`SFUexe` 的分组与状态机**（[execution.scala:806-944](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L806-L944)）：

```scala
val num_grp = num_thread/num_sfu                      // 32/8 = 4 组
val intDiv  = VecInit(Seq.fill(num_sfu)(Module(new IntDivMod(xLen)).io))      // 8 个整除器
val floatDiv= VecInit(Seq.fill(num_sfu)(Module(new FloatDivSqrt).io))         // 8 个浮点除/开方
val s_idle :: s_busy :: s_finish :: Nil = Enum(3)
...
is(s_busy){
  when(alu_out_fire){
    ... mask := next_mask; out_data(i*num_sfu+j) := 结果 ...   // 收一组
    when(!next_mask.orR){ state := s_finish }                  // 组算完
  }
}
```

`SFUexe` 依据 `ctrl.fp` 选择走 `floatDiv` 还是 `intDiv`，依据 `ctrl.alu_fn` 决定取商（`q`）还是余数（`r`）、以及浮点是除法还是开方。`IntDivMod` 与 `FloatDivSqrt` 本体分别在 `IntDivMod.scala`、`FloatDivSqrt.scala`（详见 u5-l3）。

**`LSUexe` 的接口连接**（[pipe.scala:410-414](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L410-L414)）：

```scala
lsu.io.dcache_rsp <> io.dcache_rsp
lsu.io.dcache_req <> io.dcache_req
lsu.io.lsu_rsp    <> lsu2wb.io.lsu_rsp
lsu.io.shared_rsp <> io.shared_rsp
lsu.io.shared_req <> io.shared_req
```

可以看到 LSU 是 SM 与缓存层次（dcache/sharedmem）的接口，输入由 `issueV.out_LSU` 驱动（[pipe.scala:376](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L376)），它的内部机制（地址计算、coalesce、MSHR、fence）是 u5-l4 的主题。

#### 4.5.4 代码实践

**目标**：量化 SFU 的「分组串行」对吞吐的影响。

**步骤**：
1. 读 `parameters.scala` 确认 `num_sfu = (num_thread >> 2).max(1)`（[parameters.scala:91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/parameters.scala#L91)），默认 `num_thread=32` 故 `num_sfu=8`。
2. 在 `SFUexe` 中确认 `num_grp = num_thread/num_sfu = 4`。
3. 推理：一条 32 lane 全有效的向量除法，需要算几组？答：4 组，每组占用一次除法器延迟，因此一条向量除法的 chime ≈ 4 × 单次除法延迟；而一条向量加法（vALU）只需 1 拍。

**预期结果**：你理解了为什么编译器要尽量避免除法/开方，以及 `num_sfu` 这个参数如何直接影响 SFU 类指令的吞吐。若把 `num_thread` 改为 8（则 `num_sfu=2`、`num_grp=4`），分组数不变但单组 lane 数变少——可对比资源量变化（待本地验证）。

#### 4.5.5 小练习与答案

**练习**：`SFUexe` 用 `PriorityEncoder(mask_grp)` 选当前组，为什么不简单地从 0 顺序数到 `num_grp`？
**答案**：因为 mask 可能有空洞——某些 lane 被分支屏蔽后对应的组可能整组无效。用 `PriorityEncoder(mask_grp)` 直接跳到第一个有效组，并在算完后用 `mask & ~(1<<i_cnt)` 清掉它、`next_i_cnt` 再找下一个有效组，可以跳过全无效的组，省去无谓的空转周期。

---

## 5. 综合实践

**任务**：为 Ventus SM 后端画一张完整的「指令生命周期」流程图，并完成一张路由总表。

1. **画数据流图**：以一条「向量浮点乘加 + 紧随其后的标量地址加法」为例，画出它们从 `ibuffer2issue` 开始，分别经 `out_v`/`out_x` → `exe_dataV`/`exe_dataX` → `issueV`/`issueX` → 哪个 `out_*` 端口 → 哪个执行单元 → 哪个 `wb.in_*` 写回端口的完整路径。在图上标出两路**同拍并行**的双发射关系。

2. **填路由总表**（综合 4.2.4 的练习，补全全部行）：

| 指令类型 | ctrl 命中 | Issue 实例 | Issue 端口 | 执行单元 | 写回端口 |
|----------|----------|-----------|-----------|---------|---------|
| 标量算术/跳转 | 默认 | issueX | out_sALU | ALUexe | wb.in_x(0)（+out2br→branch_back） |
| CSR 读写 | csr.orR | issueX | out_CSR | CSRexe | wb.in_x(3) |
| barrier/endprg | barrier | issueX | out_warpscheduler | (warp_sche) | （不写回，控制旁路） |
| 向量算术 | isvec | issueV | out_vALU | vALUv2 | wb.in_v(0)（+out2simt_stack） |
| 向量乘法 | mul | issueV | out_MUL | vMULv2 | wb.in_v(4)/in_x(5) |
| 浮点 | fp | issueV | out_vFPU | FPUexe | wb.in_v(1)/in_x(1) |
| 张量核 | tc | issueV | out_TC | vTCexe | wb.in_v(5) |
| 除法/开方 | sfu | issueV | out_SFU | SFUexe | wb.in_v(3)/in_x(4) |
| load/store | mem | issueV | out_LSU | LSUexe | wb.in_v(2)/in_x(2)（经 lsu2wb） |
| SIMT 分支 | isvec+simt_stack | issueV | out_SIMT(+out_vALU for beqv) | SIMT_STACK | （不写回，控制旁路） |

3. **验证**：打开 `pipe.scala` 第 374–427 行，逐行核对你填的表是否与源码连线一致；特别确认每个执行单元的写回端口号与你表中相同。

完成此实践后，你应当能在不看源码的情况下，说出任意一条指令在 SM 后端的完整走向。

## 6. 本讲小结

- Ventus 的**双发射**靠例化两个相同的 `Issue` 模块实现：`issueX` 收标量指令、`issueV` 收向量指令，二者独立、可同拍各发一条；标量/向量的分流在 `ibuffer2issue` 的 `inst_is_vec` 处就完成了。
- `Issue` 内部是一条**优先级 `when` 链**（`tc>sfu>fp>csr>mul>mem>isvec>barrier>sALU`），按控制信号把指令 demux 到 10 个输出端口之一；`pipe.scala` 再用「跨侧端口 `ready:=false`」约束每个 Issue 实例实际驱动的执行单元子集。
- `out_warpscheduler` 是一条**控制旁路**：`barrier`/`endprg` 不写寄存器、不经运算单元，直达 `warp_sche` 去阻塞或回收 warp。
- SM 后端共 8 类执行单元，结果汇聚到写回模块 `wb` 的 `in_x`(6 路)/`in_v`(6 路)；FPU/MUL/SFU 有双出口（按 `wxd`/`wvd` 互斥），LSU 经 `lsu2wb` 适配。
- **lane 复制**是 GPU 并行的核心：`vALUv2`/`vMULv2` 把标量运算单元铺成 `num_thread` 份同拍并行；`SFUexe` 因除法器昂贵只铺 `num_sfu=num_thread/4` 份，需分 `num_grp` 组串行，是低吞吐的「慢单元」。
- `beqv` 这类 SIMT 分支指令需要 **vALU 与 SIMT 同拍成交**，因为分支判定依赖 vALU 的比较结果。

## 7. 下一步学习建议

本讲是后端总览，每个执行单元的细节都在后续讲义展开：

- **u5-l2 标量 ALU 与向量 ALU**：深入 `ScalarALU` 的 `alu_fn` 译码与 `vALUv2` 的逐 lane 运算、mask 归约指令。
- **u5-l3 浮点、乘法与 SFU**：拆解 `FPUexe` 对接 `fpuv2`、`vMULv2` 的乘加流水、`SFUexe` 的 `IntDivMod`/`FloatDivSqrt` 多周期机制。
- **u5-l4 LSU 访存单元**：地址计算、coalesce 合并、sharedmem/dcache 路由、MSHR 与 fence（本讲只点到为止的难点全在这里）。
- **u5-l5 SIMT stack 与分支汇合**：本讲提到的 `if_mask`、`beqv` 同拍绑定、分支压栈出栈的完整算法。
- **u5-l6 写回与 CSR**：`wb` 如何用 Arbiter 把 12 路结果写回寄存器堆，以及 `CSRexe` 在 warp 启动时的初始化。

建议先做本讲的「综合实践」路由总表，再带着它进入 u5-l2，这样每读一个执行单元都能立刻在表里定位它的位置。
