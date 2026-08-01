# 浮点单元 FPU 与 vFPU

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Ventus SM 流水线中浮点执行通路的**四层结构**：`fpuexe`（顶层壳）→ `vfpu`（向量并行）→ `scalar_fpu`（单 lane 分派）→ `fma/fadd/fmul/fcmp/...`（具体运算核），并理解每一层各自解决什么问题。
- 掌握浮点功能码 `FN_F*` 的**两级编码**：高 3 位选功能子单元、低 3 位选子运算，并能据此判断任意一条浮点指令走哪条数据通路。
- 理解 `fma` 模块如何用「乘法器 + 加法器 + FIFO」共享同一套硬件，同时支持纯加/减、纯乘、融合乘加（FMA）三类运算。
- 说清舍入模式（RNE/RTZ/RDN/RUP/RMM）如何从 CSR 或指令编码流向每一个运算核。
- 能够跟踪一条 `VFADD_VV` 指令从发射到写回的完整浮点通路。

本讲承接 [u4-l1 操作数采集 operand_collector 与寄存器堆](u4-l1-operand-collector-and-regfile.md)：操作数采集器把 `alu_src1/src2/src3` 与 `active_mask` 准备好后，由 `issue` 按 `fp` 位把浮点指令路由到本讲的 `fpuexe` 入口。

## 2. 前置知识

### 2.1 IEEE 754 单精度（FP32）格式

Ventus 的浮点单元固定为 32 位单精度，对应参数 `EXPWIDTH=8`、`PRECISION=24`、`LEN=32`（见 [vfpu.v:18-21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v#L18-L21)）。一个 FP32 数由 1 位符号、8 位指数、23 位尾数组成：

\[ v = (-1)^{S} \times 1.\text{frac} \times 2^{E-127} \]

`PRECISION=24` 是把隐含的最高位 `1` 也算进去的尾数总宽度。

### 2.2 RISC-V 舍入模式

RISC-V 浮点定义了 5 种舍入模式，项目在 [define.v:1212-1216](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1212-L1216) 定义为 3 位编码：

| 编码 | 宏名 | 含义 |
|------|------|------|
| 3'd0 | RNE | 就近舍入， ties to even |
| 3'd1 | RTZ | 向零舍入 |
| 3'd2 | RDN | 向下舍入（向 -∞） |
| 3'd3 | RUP | 向上舍入（向 +∞） |
| 3'd4 | RMM | 就近舍入， ties away |

舍入模式可以来自指令字段（静态），也可以来自 CSR `frm`（动态），后文会看到 `pipe.v` 如何选择。

### 2.3 SIMT lane 级并行

回顾 [u4-l2 向量 ALU valu](u4-l2-vector-alu.md)：一条向量指令被广播给整个 warp，由 `NUM_THREAD` 个 lane 各自独立执行。浮点通路沿用完全相同的思路——把同一个单 lane 浮点核复制 `NUM_THREAD` 份。整数 ALU 用「一个 `alu` 内核 + `generate for`」实现 lane 阵列；浮点则多了一层「单 lane 分派器 `scalar_fpu`」，因为浮点运算种类多，需要先分派到具体的子运算核。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [fpu/fpuexe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v) | 浮点执行单元**顶层壳**：处理操作数换序/向量-标量 FMA 重映射、用 `FPU_NOT_FOLD` 宏在全并行 `vfpu` 与折叠 `vfpu_v2` 间切换、把结果导向标量/向量两条写回口。 |
| [fpu/vfpu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v) | **向量浮点**：把同一份控制信号广播、把每 lane 独立的操作数切片，例化 `NUM_THREAD` 个 `scalar_fpu` 构成 lane 阵列。 |
| [fpu/fpu/scalar_fpu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v) | **单 lane 分派器**：按 `op_i[5:3]` 把指令分派到 fma/fcmp/fpmv/f2i/i2f 五个子单元，再用固定优先级仲裁器选出有效结果。 |
| [fpu/fpu/fma.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v) | **融合乘加核**：用 `fmul_pipe` 做乘、`fadd_pipe` 做加，靠 FIFO 衔接，同时支持 FADD/FSUB/FMUL/FMADD/FMSUB/FNMSUB/FNMADD。 |
| [fpu/fpu/fadd_pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fadd_pipe.v) | 流水化浮点加/减核（也充当 FMA 的加法级）。 |
| [fpu/fpu/fmul_pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fmul_pipe.v) | 流水化浮点乘核，输出乘积的符号/指数/尾数与特殊值标志。 |
| [fpu/fpu/fcmp.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fcmp.v) | 浮点比较（FMIN/FMAX/FLE/FLT/FEQ）。 |
| [define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L513-L542) | 浮点功能码 `FN_F*`、舍入模式、`NUMBER_FPU` 等宏定义。 |

> 说明：项目中另有 `fpu/fpu_no_ctrl/` 目录，里面的 `*_no_ctrl.v` 是去掉控制信号透传的「瘦版」子核，供 `vfpu` 的非第 0 lane 例化以节省面积；逻辑运算核与 `fpu/` 下带 ctrl 的版本一致。本讲以带 ctrl 的版本讲解。

## 4. 核心概念与源码讲解

### 4.1 浮点功能码 FN_F* 的两级编码

#### 4.1.1 概念说明

与整数 ALU 共用同一套 6 位 `FN_*` 命名空间不同（见 [u4-l3 乘法器](u4-l3-vector-multiplier.md)），浮点指令在 `issue` 阶段被 `fp` 位单独路由到 vFPU 端口（见 [issue.v:482-487](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L482-L487)），其 6 位功能码 `ctrl_alu_fn` 携带的是 `FN_F*` 系列宏。

`scalar_fpu` 把这 6 位拆成两段来用：**高 3 位 `fu = op_i[5:3]` 选功能子单元，低 3 位 `op_i[2:0]` 选该单元内的具体运算**。这是一种「两级地址」译码，让一个分派器能统一管理 5 类异构运算。

#### 4.1.2 FN_F* 编码表

`FN_F*` 全部定义在 [define.v:513-542](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L513-L542)。按下表把它们按 `fu` 分组（`fu = 编码 >> 3`）：

| fu（op[5:3]） | 子单元 | 包含的指令（FN 码 / 低 3 位） |
|---------------|--------|------------------------------|
| 0 | **fma** | FADD(0/000)、FSUB(1/001)、FMUL(2/010)、FMADD(4/100)、FMSUB(5/101)、FNMSUB(6/110)、FNMADD(7/111) |
| 1 | **fcmp** | FMIN(8/000)、FMAX(9/001)、FLE(10/010)、FLT(11/011)、FEQ(12/100) |
| 2 | **fpmv** | FSGNJX(20/100)、FSGNJN(21/101)、FSGNJ(22/110)（符号注入/搬运） |
| 3 | **f2i** | F2IU(24/000)、F2I(25/001)（浮点转整数） |
| 4 | **i2f** | IU2F(32/000)、I2F(33/001)（整数转浮点） |

注意 `FN_FMADD=4` 而不是 3——低 3 位 `100` 的最高位 `op[2]` 是「是否为融合乘加」的标志位，这是 `fma` 内部分流的关键（见 4.4.2）。

> 另外，`define.v` 还有 `FN_VFMADD/VFMSUB/VFNMSUB/VFNMADD`（14~17），它们是**向量-标量**融合乘加，不会直接进 `fu` 译码，而是在 `fpuexe` 顶层被重映射成普通的 `FN_FMADD` 系列，详见 4.2.3。

### 4.2 fpuexe：浮点执行顶层壳

#### 4.2.1 概念说明

`fpuexe` 是 `pipe.v` 直接例化的浮点入口（实例名 `fpu`，见 [pipe.v:2017-2024](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2017-L2024)）。它本身不做任何浮点运算，只承担三件事：

1. **操作数换序与重映射**：处理 `reverse`（反向操作数）和向量-标量 FMA 的操作数位置调整；
2. **并行度切换**：用 `FPU_NOT_FOLD` 宏在「全并行 `vfpu`」与「折叠 `vfpu_v2`」间二选一；
3. **结果分流**：根据 `wvd/wxd` 把结果导向向量写回口或标量写回口。

#### 4.2.2 核心流程

```text
issue_out_vFPU (in1/in2/in3, mask, rm, alu_fn, reverse, wid, wvd/wxd)
        │
        ▼
  ┌─ fpuexe 操作数整形单元 ─┐
  │  reverse?            → 交换 a/b
  │  VF*MADD 系列?       → op=fn-10, 交换 b/c   (见 4.2.3)
  │  其它                 → a=in1,b=in2,c=in3
  └──────────┬─────────────┘
             │  op/a/b/c 广播给所有线程
             ▼
   `ifdef FPU_NOT_FOLD  → vfpu   (NUM_THREAD 个 lane 全并行)
   `else                → vfpu_v2(物理 lane 数 < 线程数, 迭代)
             │
             ▼  vfpu_result_out (NUM_THREAD×64), wvd/wxd, ...
   按 wvd/wxd 选择 out_v_ready 或 out_x_ready 作为反压
   out_v_wb_wvd_rd : 取每个 64 位结果的低 32 位
```

#### 4.2.3 源码精读：操作数换序与 VFMA 重映射

这段组合逻辑是 `fpuexe` 最需要读懂的部分，位于 [fpuexe.v:91-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L91-L111)：

```verilog
always@(*) begin
  if(ctrl_reverse_i) begin              // 反向指令：交换被减数/被除数位置
    vfpu_op_in = ctrl_alu_fn_i;
    vfpu_a_in  = in2_i;  vfpu_b_in = in1_i;  vfpu_c_in = in3_i;
  end
  else if((ctrl_alu_fn_i == `FN_VFMADD) | (ctrl_alu_fn_i == `FN_VFMSUB) |
          (ctrl_alu_fn_i == `FN_VFNMADD) | (ctrl_alu_fn_i == `FN_VFNMSUB)) begin
    vfpu_op_in = ctrl_alu_fn_i - 10;    // 14→4=FMADD, 15→5=FMSUB, ...
    vfpu_a_in  = in1_i;  vfpu_b_in = in3_i;  vfpu_c_in = in2_i;
  end
  else begin
    vfpu_op_in = ctrl_alu_fn_i;
    vfpu_a_in  = in1_i;  vfpu_b_in = in2_i;  vfpu_c_in = in3_i;
  end
end
```

要点：

- **`reverse`**：某些指令（如反向减/除）需要在操作数采集器层面把源 1、源 2 对调，此处把 `in1/in2` 交换送入。
- **VFMA 重映射**：向量-标量融合乘加（如 `VFMADD.VF`，语义是 `vd = vs1 × vd + vs2`，标量 `vs1` 在前）与向量-向量 FMA（`FMADD.VV`，`vd = vs1 × vs2 + vd`）的操作数顺序不同。这里把 `FN_VFMADD(14)` 减 10 还原成 `FN_FMADD(4)`，并把标量操作数从 `in2` 挪到乘法位 `b`、把原乘数挪到加数位 `c`，使下游 `fma` 核能复用同一套「乘 a×b 再加 c」的逻辑。

#### 4.2.4 源码精读：并行度切换与结果分流

`FPU_NOT_FOLD` 宏控制并行度，见 [fpuexe.v:113-194](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L113-L194)：

```verilog
`ifdef FPU_NOT_FOLD
  vfpu #(.SOFT_THREAD(SOFT_THREAD), .HARD_THREAD(HARD_THREAD)) U_vfpu (...);
`else
  vfpu_v2 #(... .MAX_ITER(MAX_ITER)) U_vfpu_v2 (...);
`endif
```

- `SOFT_THREAD` = 软件可见线程数（= `NUM_THREAD`），`HARD_THREAD` = 实际物理 lane 数。
- 文件第 17 行 `` `define FPU_NOT_FOLD `` 默认生效，且 `pipe.v` 例化时 `HARD_THREAD = NUMBER_FPU = NUM_THREAD`（[define.v:268](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L268)），即「每线程一个物理 lane，单拍完成一个 warp」，是性能优先配置。
- 折叠版 `vfpu_v2` 则在 `HARD_THREAD < SOFT_THREAD` 时用 `MAX_ITER = SOFT_THREAD/HARD_THREAD` 拍迭代完一个 warp，以面积换性能，思路与 [u4-l2 valu_top 的 ALU_NOT_FOLD](u4-l2-vector-alu.md) 同构。

结果分流与反压选择在 [fpuexe.v:196-217](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L196-L217)：

```verilog
assign vfpu_out_ready_in = vfpu_wvd_out ? out_v_ready_i : out_x_ready_i; // 按目的类型选反压
assign out_x_valid_o     = vfpu_out_valid_out && vfpu_wxd_out;           // 标量口
assign out_v_valid_o     = vfpu_out_valid_out && vfpu_wvd_o;             // 向量口
genvar i; generate
  for(i=0;i<`NUM_THREAD;i=i+1) begin : A1
    // 每个 lane 的 64 位结果只取低 32 位（FP32）
    assign out_v_wb_wvd_rd_o[(i+1)*`XLEN-1-:`XLEN] = vfpu_result_out[(2*i+1)*`XLEN-1-:`XLEN];
  end
endgenerate
```

注意 `vfpu_result_out` 是 `NUM_THREAD×64` 位，但 FP32 只用低 32 位，所以 `generate` 里取每个 64 位段的低 32 位拼接成写回数据。

#### 4.2.5 fpuexe 在 pipe.v 中的连接

`pipe.v` 把 `issue` 的 vFPU 端口接入 `fpuexe`，把输出接入 `writeback`，见 [pipe.v:2028-2058](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2028-L2058)。其中**舍入模式**的来源特别值得注意，[pipe.v:895](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L895)：

```verilog
assign fpu_rm = issue_out_vFPU_warps_control_Signals_force_rm_rt ? 'h1        // 强制 RTZ（类型转换指令）
              : (issue_out_vFPU_warps_control_Signals_rm_is_static ? issue_out_vFPU_..._rm  // 指令编码里的静态 rm
                                                                  : csrfile_rm[2:0]);         // 否则取 CSR frm
```

即舍入模式有**三种来源，优先级从高到低**：

1. 某些浮点↔整数转换指令（如 `VFCVT_RTZ_X_F_V`，见 [define.v:779-780](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L779-L780)）硬件强制 RTZ（向零），`force_rm_rt=1`；
2. 指令 funct3 字段直接给出的静态舍入模式（`rm_is_static=1`，即非 `111`）；
3. 否则用 CSR 的 `frm` 字段（动态舍入）。

写回侧，`fpu_out_x_*` 进标量写回口 `writeback_in_x[1]`，`fpu_out_v_*` 进向量写回口 `writeback_in_v[1]`，见 [pipe.v:880-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L891)；反压来自 `writeback_in_x_ready[1]` 与 `writeback_in_v_ready[1]`。

#### 4.2.6 代码实践：阅读 fpuexe 的操作数整形的三种情况

1. **实践目标**：确认三种指令（普通浮点、reverse、向量-标量 FMA）在 `fpuexe` 入口的操作数与功能码映射。
2. **操作步骤**：打开 [fpuexe.v:91-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L91-L111)，对照本节 4.2.3 的讲解，画出一张表：列分别为「指令类型」「`vfpu_op_in`」「`vfpu_a_in`/`b_in`/`c_in` 的来源」，填入三种分支。
3. **需要观察的现象**：`FN_VFMADD(14)` 经 `-10` 后变为 `4`，对应表中 `fu=0` 的 `FMADD`，验证「向量-标量 FMA 复用向量-向量 FMA 硬件」这一复用关系。
4. **预期结果**：三种分支的功能码与操作数来源应与本讲 4.2.3 的代码注释完全一致。
5. 若需确认 `FN_VFMADD=14` 等数值，对照 [define.v:522-525](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L522-L525)。

#### 4.2.7 小练习与答案

**练习 1**：为什么 `fpuexe` 要把 64 位的 lane 结果再切片成 32 位写回？
**答案**：因为运算核 `result_o` 设计成 64 位以兼容未来更宽的浮点格式，但当前 `EXPWIDTH+PRECISION=32`（FP32），写回寄存器堆只需低 32 位，所以 [fpuexe.v:212-217](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L212-L217) 用 `generate` 取每段低 32 位。

**练习 2**：如果把 [fpuexe.v:17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L17) 的 `` `define FPU_NOT_FOLD `` 注释掉，会发生什么？
**答案**：综合时会例化 `vfpu_v2`（折叠版），需要 `HARD_THREAD < SOFT_THREAD` 且 `MAX_ITER` 拍才能完成一个 warp；若仍保持 `HARD_THREAD=NUM_THREAD` 则失去折叠意义。这是面积/性能的旋钮。

### 4.3 vfpu：向量浮点的 lane 阵列

#### 4.3.1 概念说明

`vfpu` 解决的问题是「如何把单 lane 的 `scalar_fpu` 变成向量并行的」。它的输入是 `NUM_THREAD` 个 lane 的操作数**拼接成的大位宽总线**（如 `a_i` 是 `SOFT_THREAD*32` 位），输出也是拼接的。`vfpu` 负责：把操作数按 lane 切片、把同一份控制信号广播、例化 `NUM_THREAD` 个 `scalar_fpu`、再把各 lane 结果拼回去。

#### 4.3.2 核心流程

```text
op_i[SOFT_THREAD*6-1:0]  ──┬── lane0 取 op_i[5:0]            ──→ scalar_fpu[0] (CTRLGEN 开)
a_i/b_i/c_i/rm_i (拼接)   ──┼── lane i 取 [(i+1)*W-1-:W]      ──→ scalar_fpu[i] (CTRLGEN 关, no_ctrl)
控制信号(regindex/wid/...) ──┘   只接给 lane 0
                                                                       │
result_o/fflags_o ◄── 按 lane 拼接 ◄─────────────────────────────────┘
out_valid_o/in_ready_o/控制信号输出 ◄── 全部取自 lane 0
```

#### 4.3.3 源码精读

`vfpu` 显式例化第 0 个 lane（带控制信号，`CTRLGEN` 开），见 [vfpu.v:67-104](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v#L67-L104)；再用 `generate for` 例化剩余 `HARD_THREAD-1` 个 lane（用 `scalar_fpu_no_ctrl`，不带控制信号），见 [vfpu.v:109-143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v#L109-L143)：

```verilog
// lane 0：CTRLGEN 开，承载控制信号
scalar_fpu U_scalar_fpu_with_ctrl (
  .op_i(op_i[5:0]), .a_i(a_i[LEN-1:0]), ... ,
  .ctrl_regindex_i(ctrl_regindex_i), .ctrl_wvd_i(ctrl_wvd_i), ...
);
assign result_o[63:0] = fpu_result[0];

// lane 1..N-1：CTRLGEN 关，只搬运数据
genvar i; generate
  for(i=1;i<HARD_THREAD;i=i+1) begin : A1
    scalar_fpu_no_ctrl U_scalar_fpu_without_ctrl (
      .op_i(op_i[(i+1)*6-1-:6]), .a_i(a_i[(i+1)*LEN-1-:LEN]), ...
    );
    assign result_o[(i+1)*64-1-:64] = fpu_result[i];
  end
endgenerate
```

关键设计：**控制信号（写回寄存器号、warp id、掩码、wvd/wxd）只需在第 0 lane 产生一份**，因为整条向量指令的所有 lane 共用同一组控制信号；其余 lane 用 `no_ctrl` 版本只做运算、省掉控制信号的寄存器与连线。最终的 `out_valid_o`、`in_ready_o`、`ctrl_*_o` 全部取自 lane 0，见 [vfpu.v:145-151](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v#L145-L151)。这与 [u4-l2 valu](u4-l2-vector-alu.md) 中「控制信号全广播、数据各 lane 独立」的思想完全一致。

#### 4.3.4 代码实践：数清 lane 数与位宽

1. **实践目标**：验证 `vfpu` 例化的 lane 数与每 lane 操作数位宽。
2. **操作步骤**：在 [vfpu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v) 中数 `scalar_fpu` 与 `scalar_fpu_no_ctrl` 的例化总数；确认 `a_i[(i+1)*LEN-1-:LEN]` 中 `LEN=EXPWIDTH+PRECISION=32`。
3. **需要观察的现象**：lane 总数 = `HARD_THREAD`；在 `FPU_NOT_FOLD` 默认配置下 `HARD_THREAD=NUM_THREAD`。
4. **预期结果**：若 `NUM_THREAD=4`，则 `vfpu` 例化 4 个单 lane 浮点核，`a_i` 总宽 `4*32=128` 位。

#### 4.3.5 小练习与答案

**练习**：为什么只有 lane 0 需要例化带 `CTRLGEN` 的 `scalar_fpu`，而其余 lane 用 `no_ctrl` 版？
**答案**：因为同一条向量浮点指令的所有 lane 共享同一组控制信号（写回号、warp、掩码等），只需在一个 lane 上产生并向上输出即可；其余 lane 只需独立做运算，去掉控制信号透传逻辑能减少面积与连线负担。

### 4.4 scalar_fpu：单 lane 分派器与五大子单元

#### 4.4.1 概念说明

`scalar_fpu` 是「单 lane 上的浮点指令调度中心」。一条浮点指令到达单 lane 后，先由 `fu = op_i[5:3]` 判定它属于 fma/fcmp/fpmv/f2i/i2f 中的哪一类，再把输入只送给那一个子单元（其余子单元的 `in_valid` 为 0）。各子单元并行挂着，谁的 `out_valid` 先拉起，就用固定优先级仲裁器选出它的结果。

#### 4.4.2 核心流程

```text
                 ┌── fu==0 ──→ fma   ──→ out_valid ──┐
                 ├── fu==1 ──→ fcmp  ──→ out_valid ──┤
op_i[5:3]=fu ────┼── fu==2 ──→ fpmv  ──→ out_valid ──┼── fixed_pri_arb(5) ──→ choose_oh
                 ├── fu==3 ──→ f2i   ──→ out_valid ──┤         │
                 └── fu==4 ──→ i2f   ──→ out_valid ──┘    one2bin → choose_bin
                                                                │
                          case(choose_bin) 选 result/fflags/ctrl/out_valid/in_ready ◄┘
```

五个子单元的功能：

- **fma**：加减乘与融合乘加，是体量最大、流水最深的子单元（见 4.5）。
- **fcmp**：浮点比较与取极值（FMIN/FMAX/FLE/FLT/FEQ），产生布尔/极值结果。
- **fpmv**：符号注入与搬运（FSGNJ/FSGNJN/FSGNJX），不运算数值、只改符号位。
- **f2i**（`fp_to_int`）：浮点转整数（含向零截断 F2IU 与舍入 F2I）。
- **i2f**（`int_to_fp`）：整数转浮点。

#### 4.4.3 源码精读：fu 分派与输入多路选择

`fu` 的提取与向 fma 的输入选择见 [scalar_fpu.v:193-199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L193-L199)：

```verilog
assign fu           = op_i[5:3];
assign fma_op_in    = (fu == 'd0) ? op_i[2:0] : 'd0;   // 低 3 位作为 fma 子运算码
assign fma_rm_in    = (fu == 'd0) ? rm_i      : 'd0;
assign fma_a_in     = (fu == 'd0) ? a_i       : 'd0;
assign fma_b_in     = (fu == 'd0) ? b_i       : 'd0;
assign fma_c_in     = (fu == 'd0) ? c_i       : 'd0;
assign fma_in_valid = (fu == 'd0) && in_valid_i;       // 只有命中时才给 valid
```

fcmp/fpmv/f2i/i2f 的输入选择结构与 fma 完全对称，只是判别条件换成 `fu=='d1/'d2/'d3/'d4`，分别见 [scalar_fpu.v:210-216](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L210-L216)、[226-232](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L226-L232)、[242-249](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L242-L249)、[259-266](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L259-L266)。这是一种「五选一的多路分发」：用条件三目运算把输入只喂给命中的子单元，未命中的子单元 `in_valid=0`，自然不会有有效输出。

五个子单元的例化见 [scalar_fpu.v:275-436](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L275-L436)（fma/fcmp/fpmv/f2i/i2f 依次例化）。

#### 4.4.4 源码精读：输出仲裁与结果选择

五个子单元的 `out_valid` 经一个 5 位固定优先级仲裁器选出唯一有效者，见 [scalar_fpu.v:453-460](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L453-L460)：

```verilog
fixed_pri_arb #(.ARB_WIDTH(5)) U_arbiter (
  .req  ({i2f_out_valid, f2i_out_valid, fpmv_out_valid, fcmp_out_valid, fma_out_valid}),
  .grant(choose_oh)
);
one2bin #(.ONE_WIDTH(5), .BIN_WIDTH(3)) U_one2bin ( .oh(choose_oh), .bin(choose_bin) );
```

注意 `req` 的拼接顺序：MSB 是 `i2f`、LSB 是 `fma`，而 `fixed_pri_arb` 优先编码的优先级取决于其实现（项目 `common_cell` 版本通常以最低位为最高优先级）。`choose_bin` 随后驱动一个 `case` 把对应子单元的 `result/fflags/控制信号/out_valid/in_ready` 选出，见 [scalar_fpu.v:475-561](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L475-L561)。由于 `fu` 分派保证了同一时刻只有一个子单元收到 `in_valid`，所以正常情况下也只有一个 `out_valid` 会拉起，仲裁器在此主要是「结果多路选择」的统一入口。

#### 4.4.5 代码实践：把一条指令映射到子单元

1. **实践目标**：用 `fu = FN>>3` 规则判断若干浮点指令归属哪个子单元。
2. **操作步骤**：对照 [define.v:513-542](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L513-L542)，对 `FN_FMUL(2)`、`FN_FEQ(12)`、`FN_FSGNJ(22)`、`FN_F2I(25)`、`FN_I2F(33)` 分别计算 `fu` 与 `op_i[2:0]`，填入下表。
3. **需要观察的现象**：`fu` 应分别为 0/1/2/3/4，一一对应 fma/fcmp/fpmv/f2i/i2f。
4. **预期结果**：

   | FN 码 | 值 | fu | 子单元 | 低 3 位 |
   |-------|----|----|--------|---------|
   | FN_FMUL | 2 | 0 | fma | 010 |
   | FN_FEQ | 12 | 1 | fcmp | 100 |
   | FN_FSGNJ | 22 | 2 | fpmv | 110 |
   | FN_F2I | 25 | 3 | f2i | 001 |
   | FN_I2F | 33 | 4 | i2f | 001 |

#### 4.4.6 小练习与答案

**练习 1**：`FN_FADD` 与 `FN_FMADD` 都属于 `fu=0` 的 fma 子单元，`scalar_fpu` 如何区分它们？
**答案**：靠低 3 位 `op_i[2:0]`：FADD 是 `000`、FMADD 是 `100`。在 `fma` 内部进一步用 `op[2]`（=1 表示融合乘加）和 `op[2:1]`（=00 表示纯加减）分流（见 4.5.2）。

**练习 2**：`fpmv`（符号注入）为什么不需要 `rm_i` 舍入模式输入？
**答案**：符号注入类指令（FSGNJ/N/X）只复制或翻转操作数的符号位，不涉及数值运算与精度损失，因而不需要舍入；在 [scalar_fpu.v:226-227](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L226-L227) 可见其 `rm_in` 被注释掉。

### 4.5 fma：融合乘加核（乘加共享）

#### 4.5.1 概念说明

`fma` 是浮点通路里最精巧的模块。它要同时支持 7 种运算：纯加 FADD、纯减 FSUB、纯乘 FMUL，以及 4 种融合乘加 FMADD/FMSUB/FNMSUB/FNMADD。如果为每种各做一套硬件，面积巨大；`fma` 的做法是**「一个乘法器 + 一个加法器」共享**：

- 纯乘（FMUL）：只用乘法器，结果直出；
- 纯加减（FADD/FSUB）：只用加法器（把乘法器旁路）；
- 融合乘加（FMADD 等）：乘法器算出 `(a×b)` 的「未舍入内部积」，直接喂给加法器与 `c` 相加，**中间不舍入**，从而保证融合乘加的精度（只舍入一次）。

这个设计受香山（XiangShan）浮点单元启发，`fmul_pipe`/`fadd_pipe` 与 `common_cells/`（far_path/near_path/lza/rounding 等）都是典型的单精度浮点流水线构件。

#### 4.5.2 核心流程：三类运算的分流

子运算码 `in_op_i[2:0]` 把进来的指令分成三类，判定式见 [fma.v:63-66](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L63-L66)：

```verilog
assign is_fma    = in_op_i[2] == 1'b1;   // 1xx: FMADD/FMSUB/FNMSUB/FNMADD
assign is_fmul   = in_op_i == 3'b010;    // 010: FMUL
assign is_addsub = in_op_i[2:1] == 2'b00;// 0x:  FADD(000)/FSUB(001)
```

整体数据流如下（`fmul_pipe` = 乘法流水，`fadd_pipe` = 加法流水，方框 `F` 为深度 1 的 `stream_fifo_pipe_true`）：

```text
                       ┌─────────── fmul_pipe (a×b) ───────────┐
is_fmul ──────────────►│                                       │── mul_fifo ──────────────────────►┐
is_fma  ──────────────►│  (产出未舍入积: sign/exp/sig + 标志)   │── multoadd_fifo ──┐                ▼
                       └───────────────────────────────────────┘                   │          输出仲裁
                                                                                   ▼          (mul 优先)
is_addsub ── intoadd_fifo(op/a/b/rm) ──► toadd_arb ◄──────────────────────────► fadd_pipe ──► add_fifo ──►┘
                                          (fifo_0=fma优先 > fifo_1=addsub)
```

三类运算各走一条路径：

1. **FMUL**：进 `fmul_pipe`，结果经 `mul_fifo` 直接输出（不进加法器）。
2. **FADD/FSUB**：操作数经 `intoadd_fifo` 缓存，经 `toadd_arb`（fifo_1 路）送 `fadd_pipe`。
3. **FMADD 等**：先 `fmul_pipe` 出未舍入积，经 `multoadd_fifo` 缓存，经 `toadd_arb`（fifo_0 路，**优先级更高**）送 `fadd_pipe`，与加数 `c` 相加。

`fadd_pipe` 因此被「纯加减」与「FMA 的加法级」复用。

#### 4.5.3 源码精读：fmul_pipe 与「未舍入积」传递

`fmul_pipe` 例化见 [fma.v:93-134](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L93-L134)。它除了输出最终舍入的 `out_result_o`（供 FMUL 用）之外，还并行输出一组**未舍入的乘积内部表示**给加法器：

```verilog
.mul_output_fp_prod_sign_o (mul_toadd_fp_prod_sign),  // 积的符号
.mul_output_fp_prod_exp_o  (mul_toadd_fp_prod_exp ),  // 积的指数
.mul_output_fp_prod_sig_o  (mul_toadd_fp_prod_sig ),  // 积的尾数(未舍入, 2*PRECISION-1 位)
.mul_output_is_nan_o       (mul_toadd_is_nan      ),  // 特殊值标志
.mul_output_is_inf_o       (mul_toadd_is_inf      ),
.mul_output_is_inv_o       (mul_toadd_is_inv      ),
.mul_output_overflow_o     (mul_toadd_overflow    ),
.add_another_o             (mul_toadd_add_another ),  // 加数 c(经符号处理后)
.op_o                      (mul_toadd_op          )   // 透传的子运算码
```

把乘积以「未舍入的高精度内部表示」直接送加法器，是 FMA 只舍入一次、保证精度的关键。这组信号经 `multoadd_fifo`（[fma.v:288-320](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L288-L320)）对齐拍位后送入 `fadd_pipe`。

#### 4.5.4 源码精读：toadd 仲裁与 fadd_pipe 复用

进入 `fadd_pipe` 的请求有两路，由 `toadd_arb` 仲裁，见 [fma.v:230-243](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L230-L243)：

```verilog
// fifo_0: mul→add (FMA 路), fifo_1: input→add (加减路)
assign toaddarb_out_valid        = toaddarb_fifo_0_deq_valid || toaddarb_fifo_1_deq_valid;
assign toaddarb_fifo_0_deq_ready = toaddarb_out_ready;                              // FMA 路优先
assign toaddarb_fifo_1_deq_ready = !toaddarb_fifo_0_deq_valid && toaddarb_out_ready; // 加减路退让
assign toaddarb_out_op           = toaddarb_fifo_0_deq_valid ? toaddarb_fifo_0_deq_op : toaddarb_fifo_1_deq_op;
```

即 **FMA 的乘→加路优先级高于纯加减路**：当两路同时就绪时，先服务 FMA。`fadd_pipe` 接收经仲裁的 `add_in_valid/op/a/b/rm`，并通过 `from_mul_*` 一组端口接收来自乘法器的未舍入积（[fma.v:385-426](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L385-L426)）。`fadd_pipe` 内部据此选择「加普通操作数」还是「加乘积」。

#### 4.5.5 源码精读：输出仲裁与反压

`fma` 出口同样有两路：`mul_fifo`（纯乘结果）与 `add_fifo`（加减/FMA 结果），由输出仲裁器二选一，`mul_fifo` 优先，见 [fma.v:531-546](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L531-L546)：

```verilog
assign mul_fifo_deq_ready = out_ready_i;                                  // 纯乘优先出
assign add_fifo_deq_ready = !mul_fifo_enq_valid && out_ready_i;           // 加法结果退让
assign out_result_o = mul_fifo_deq_valid ? mul_fifo_result : add_fifo_result;
assign out_valid_o  = mul_fifo_deq_valid || add_fifo_deq_valid;
```

整条 `fma` 大量使用深度为 1 的 `stream_fifo_pipe_true`（如 [fma.v:165-178](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L165-L178)），它们既用来**对齐乘法级与加法级之间的拍位差**（乘法流水与加法流水深度不同），又充当反压传递与组合路径切割点——这与 [u4-l2 valu 用 stream_fifo_pipe_true 切断长组合路径](u4-l2-vector-alu.md) 的用法一致。`in_ready_o` 的产生见 [fma.v:429](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L429)：加减路取 `intoadd` 入口反压，乘路取 `fmul_pipe` 反压。

#### 4.5.6 代码实践：跟踪 FMUL 与 FMADD 的不同路径

1. **实践目标**：对比 FMUL（纯乘）与 FMADD（融合乘加）在 `fma` 内部走过的路径差异。
2. **操作步骤**：打开 [fma.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v)。对 `in_op_i=3'b010`（FMUL），追踪 `is_fmul=1` → `fmul_pipe` → `mul_fifo` → 输出；对 `in_op_i=3'b100`（FMADD），追踪 `is_fma=1` → `fmul_pipe` 出未舍入积 → `multoadd_fifo` → `toadd_arb(fifo_0)` → `fadd_pipe` → `add_fifo` → 输出。
3. **需要观察的现象**：FMUL 完全不经过 `fadd_pipe`；FMADD 既用乘法器又用加法器，且乘积以未舍入形式传递。
4. **预期结果**：能画出两条不同的数据通路图，验证「乘加共享」机制。

#### 4.5.7 小练习与答案

**练习 1**：为什么 FMA 要把乘积以「未舍入的内部表示」传给加法器，而不是先舍入成 FP32 再加？
**答案**：RISC-V 的融合乘加（FMADD 等）语义要求**全精度运算、只舍入一次**。若乘法后先舍入一次、加法后再舍入一次，会引入两次舍入误差，结果与标准 FMA 不符。所以 [fma.v:125-133](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L125-L133) 把 `fmul_pipe` 的未舍入积直接送 `fadd_pipe`。

**练习 2**：`toadd_arb` 为什么让 FMA 路（fifo_0）优先于加减路（fifo_1）？
**答案**：FMA 路的乘积来自 `fmul_pipe`，其下游 `multoadd_fifo` 深度只有 1，若不及时被 `fadd_pipe` 取走会反压住乘法流水；让 FMA 路优先可避免乘法级被堵住，保护吞吐。

### 4.6 整条 VFADD_VV 通路串讲（综合）

把前面四个模块串起来，一条 `VFADD.VV`（向量浮点加，`vd = vs1 + vs2`）的完整通路如下：

1. **译码**：`decodeUnit` 识别出浮点加，置 `fp=1`、`alu_fn = FN_FADD = 6'd0`、`wvd=1`；舍入模式字段决定 `rm_is_static`。
2. **操作数采集**：`operand_collector` 读出 `vs1`、`vs2` 拼成 `in1`、`in2`（各 `NUM_THREAD×32` 位），连同 `mask`、`rm`、`alu_fn=0`、`wvd=1` 送给 `issue`。
3. **发射路由**：`issue` 见 `fp=1`，把指令接到 `issue_out_vFPU` 端口（[issue.v:482-487](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L482-L487)）。
4. **舍入选择**：`pipe.v` 算出 `fpu_rm`（[pipe.v:895](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L895)）：非强制、非静态时取 `csrfile_rm`（CSR `frm`）。
5. **fpuexe 整形**：`reverse=0`、非 VFMA，故 `vfpu_op_in=0`、`a=in1`、`b=in2`（[fpuexe.v:105-110](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L105-L110)），广播给所有 lane。
6. **vfpu 分发**：每 lane 各取自己的 `a/b/op/rm` 切片，送入各自的 `scalar_fpu`（[vfpu.v:110-143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/vfpu.v#L110-L143)）。
7. **scalar_fpu 分派**：`fu=op[5:3]=0` → 命中 `fma`，`fma_op=op[2:0]=000`（[scalar_fpu.v:193-199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/scalar_fpu.v#L193-L199)）。
8. **fma 执行**：`is_addsub=1` → `intoadd_fifo` → `toadd_arb(fifo_1)` → `fadd_pipe` 做加法并舍入 → `add_fifo` → 经 `choose_bin=0` 选回 `scalar_fpu` 输出。
9. **结果回流**：`vfpu` 拼接各 lane 结果 → `fpuexe` 取每 lane 低 32 位 → `wvd=1` 走向量写回口 `out_v_*` → `writeback_in_v[1]` → 写回向量寄存器堆。

整个过程中，舍入模式 `rm` 在第 4 步确定后，经 `fpuexe`→`vfpu`→`scalar_fpu`→`fma` 一路透传到 `fadd_pipe`，由其内部的 `rounding` 单元（`common_cells/rounding.v`）最终实施。

## 5. 综合实践

**任务**：选择一条融合乘加指令 `VFMADD.VV`（语义 `vd = vs1 × vs2 + vd`，对应的 `alu_fn` 在 `fpuexe` 入口会被重映射），完整跟踪它从 `issue` 到写回的通路，并对比它与 `VFADD.VV` 的异同。

建议步骤：

1. 在 [define.v:522](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L522) 确认 `FN_VFMADD=14`，对照 [fpuexe.v:98-104](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpuexe.v#L98-L104) 写出重映射后的 `vfpu_op_in`（应为 4=`FN_FMADD`）与 `a/b/c` 的来源（`b=in3`、`c=in2`）。
2. 在 [fma.v:63-66](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/fpu/fpu/fma.v#L63-L66) 确认 `is_fma=1`，画出「`fmul_pipe` → `multoadd_fifo` → `toadd_arb(fifo_0)` → `fadd_pipe` → `add_fifo`」的通路。
3. 列出与 `VFADD` 的三处关键差异：① `fpuexe` 多一步 VFMA 重映射；② `fma` 内部走「乘→加」而非纯加；③ 中间传递未舍入积、最终只舍入一次。
4. （可选，待本地验证）在 `testcase/test_gpgpu_axi_top/tc_vecadd` 框架下，仿照现有用例编写一段含 `vfmadd` 的小 kernel，按 [u1-l4 仿真环境搭建与用例运行](u1-l4-simulation-and-testcases.md) 的 `make run-vcs-4w4t` 流程仿真，用 Verdi 观察上述信号通路，确认 `fma` 内部 `is_fma` 与 `toadd_arb` 的选择。

## 6. 本讲小结

- 浮点执行通路呈**四层结构**：`fpuexe`（顶层壳/整形/分流）→ `vfpu`（lane 阵列）→ `scalar_fpu`（单 lane 五选一分派）→ `fma/fcmp/fpmv/f2i/i2f`（具体运算核）。
- 浮点功能码 `FN_F*` 采用**两级编码**：高 3 位 `fu` 选子单元、低 3 位选子运算；据此可判定任意浮点指令的归属。
- `fpuexe` 用 `FPU_NOT_FOLD` 宏在**全并行 `vfpu`**（默认，每线程一 lane）与**折叠 `vfpu_v2`**（面积优先）间切换，与整数 `valu_top` 的 `ALU_NOT_FOLD` 同构。
- `vfpu` 把控制信号只在 lane 0 产生一份、其余 lane 用 `no_ctrl` 版只做运算，体现「控制广播、数据独立」的 SIMT 思想。
- `fma` 用「一个乘法器 + 一个加法器 + 若干深度 1 的 FIFO」共享同一套硬件，支持纯加/减、纯乘、融合乘加三类运算；FMA 通过传递**未舍入乘积**保证只舍入一次的精度。
- 舍入模式有三种来源（强制 RTZ > 指令静态 rm > CSR `frm`），在 `pipe.v:895` 选定后一路透传到运算核的 `rounding` 单元。

## 7. 下一步学习建议

- **横向对比**：回到 [u4-l2 valu](u4-l2-vector-alu.md) 与 [u4-l3 vmul](u4-l3-vector-multiplier.md)，对比整数执行单元与浮点执行单元在「lane 阵列组织」「折叠宏」「FIFO 切割」上的异同，体会项目复用同一套 SIMT 设计模式。
- **深入乘加核**：若对浮点算法感兴趣，可精读 `fpu/common_cells/` 下的 `far_path.v`/`near_path.v`/`lza.v`/`rounding.v`，理解 `fadd_pipe` 的双路径（near/far）与前置零检测（LZA）原理。
- **继续流水线**：本讲完成后，建议进入 [u4-l5 SFU 特殊功能单元](u4-l5-sfu.md)，学习高延迟运算（除法、开方）如何用另一套执行单元处理，以及 scoreboard 如何在长延迟期间阻塞依赖指令。
- **访存与控制**：随后可进入第 5 单元（LSU/CSR/SIMT/张量核），其中张量核（u5-l4）会复用本讲的浮点乘加思想实现矩阵乘。
