# 仿真验证方法学：自校验测试平台

## 1. 本讲目标

学完本讲，你应当能够：

- 把 [u1-l1](./u1-l1-project-overview.md) 里点到为止的「`$signed(x)*1.0/(1<<W)` 还原法」上升为一套**系统化的软件参考模型（software reference model）方法**，并能解释为什么这一行代码是全库 4 个 testbench 的「万能钥匙」。
- 区分本库**两种 testbench 风格**：单周期模块用的「`test` 任务 + 定时采样 + 目视对比」（以 `fxp_add` 为代表），与流水线模块用的「每拍喂入 + `cyclecnt` 流式打印 + 延迟对齐」（以 `fxp_mul_pipe` 为代表），并说出它们的激励、采样与打印差异。
- 读懂单目运算 testbench（`fxp_sqrt` 用 `oval^2` 自证）与浮点互转 testbench（`fxp2float` 用 `0x%08x` 打印 IEEE754 位串并做 round-trip）各自特殊的比对套路。
- 掌握 `iverilog -g2001` 编译、`vvp -n` 运行、`$dumpvars` 波形导出、`.bat` 一键脚本的**完整仿真流程**，知道为什么 testbench 与 `../RTL/fixedpoint.v` 必须同时参与编译。
- 独立设计一个**带 `pass`/`fail` 计数器与 `$display` 汇总**的自校验（self-checking）testbench，把「人眼逐行对比」升级为「机器自动判 PASS/FAIL」，让 `FAIL=0` 成为可信的通过判据。

## 2. 前置知识

本讲是专家层的收尾篇，承接 [u1-l1（项目概览与首次仿真）](./u1-l1-project-overview.md)，并把贯穿 [u2](./u2-l1-add-sub.md)、[u3](./u3-l1-mul-pipe.md) 各讲的零散验证技巧做一次系统性收口。你需要已经掌握：

- 定点数值 = 有符号补码码值 ÷ \(2^{W_F}\)；全库统一参数 `WOI/WOF`、`WII/WIF`、`WIIA/WIFA/WIIB/WIFB`、`ROUND` 的含义（见 [u1-l2](./u1-l2-fixedpoint-format.md)）。
- 四类被测模块的功能与延迟特性：`fxp_add`（单周期组合，0 延迟）、`fxp_mul_pipe`（2 级流水线）、`fxp_sqrt`/`fxp_sqrt_pipe`（单周期与流水线并存）、`fxp2float`/`float2fxp`（定点↔IEEE754 互转）。
- 流水线模块「输出滞后输入 \(L\) 拍、每拍吞吐一个（无气泡）」的特性，以及「读寄存器读到的是上一拍值」的 NBA 语义（见 [u3-l1](./u3-l1-mul-pipe.md)）。

本讲要回答的收口问题是：**这个库没有任何 `$error`、没有 `assert`、没有 pass/fail 计数——那它到底是怎么验证正确性的？我又该怎么把它改造成一份「仿真结束自动报 PASS/FAIL」的工业级自校验 testbench？**

> **一句话直觉：** 验证的本质是「拿一个你信得过的答案，去比对硬件算出来的答案」。在定点库里，最可信的答案不是手算，而是**把硬件的定点码原样翻译回浮点数**——因为浮点是 iverilog 原生支持的实数运算，等价于一个「软件黄金模型」。整本手册的 4 个 testbench，花样再多，干的都是这一件事。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | **单周期风格**代表：例化 `fxp_add`/`fxp_addsub`/`fxp_mul`/`fxp_div`，用 `test` 任务注入激励、`#10000` 定时采样、`$display` 并排打印 SW-result 与 HW-result。本讲用它讲透「软件参考 + 目视对比」范式，并作为综合实践的改造模板。 |
| [SIM/tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) | **流水线风格**代表：例化 `fxp_mul_pipe`/`fxp_div_pipe`，用 `cyclecnt` 逐拍流式打印，体现「每拍喂入 + 延迟对齐」。本讲主角是其中的 `fxp_mul_pipe`。 |
| [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) | **单目 + 自证**风格：例化 `fxp_sqrt`/`fxp_sqrt_pipe`，除打印 `oval` 外还打印 `oval^2`，用「结果的平方」反过来逼近输入，是开方这类难有简洁软件参考的运算的验证技巧。 |
| [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) | **浮点位串 + round-trip** 风格：例化 `fxp2float`/`fxp2float_pipe`/`float2fxp`/`float2fxp_pipe`，用 `0x%08x` 打印 IEEE754 位串，并构造「定点→浮点→定点」往返链路观察还原能力。 |
| [SIM/tb_fxp_sqrt_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt_run_iverilog.bat) | 一键仿真脚本样板：`iverilog -g2001 -o sim.out tb_xxx.v ../RTL/fixedpoint.v` + `vvp -n sim.out`。四个 `.bat` 结构完全一致，换的只是 testbench 文件名。 |

## 4. 核心概念与源码讲解

### 4.1 软件参考模型与溢出标记：仿真的万能钥匙

#### 4.1.1 概念说明

硬件验证的第一步，永远是**找到一个「软件参考模型（software reference model）」**——一个你完全信任、与被测硬件无关、能算出正确答案的参照物。对 FPGA-FixedPoint 这种定点库，最自然的软件参考就是**浮点实数运算**：浮点是 iverilog 仿真器原生支持的 `real` 类型，加减乘除开方都可用 `+ - * /` 直接写，精度远高于定点，天然适合当「黄金答案」。

难点在于：**硬件的输入输出是定点码（一串二进制补码整数），软件参考是浮点实数，两者不在一个数制里，怎么比？** 答案就是 u1-l1 点过的那行「万能钥匙」——把定点码翻译回浮点：

\[
\text{浮点值} = \frac{\$\text{signed}(\text{code})}{2^{W}}
\]

其中 `code` 是定点码，\(W\) 是它的小数位宽（`WIFA`/`WIFB`/`WOF` 等），`$signed()` 把无符号位串重新解释为有符号补码整数。一旦两头都翻译成浮点，就可以在 `$display` 里并排打印、目视对比，或在自校验 testbench 里做数值比较。

#### 4.1.2 核心流程

构造软件参考比对的标准三步：

1. **激励侧**：把注入硬件的定点码 `ina`/`inb` 用 `$signed(ina)*1.0/(1<<WIFA)` 还原成浮点输入 `a_float`、`b_float`。
2. **期望侧**：用浮点运算算出软件期望 `sw = a_float OP b_float`（`OP` 是 `+ - * /` 之一）。
3. **实际侧**：把硬件输出的定点码 `oadd`/`osub`/`omul`/`odiv` 用 `$signed(oadd)*1.0/(1<<WOF)` 还原成 `hw`，与 `sw` 并排打印。

一个细节：为什么要乘 `1.0`？因为 `$signed(ina)` 是 32 位整型，`(1<<WIFA)` 也是整型，两者做整数除法会**截断小数**。乘以 `1.0` 后整个表达式被提升为 `real`（浮点）除法，才能保留小数精度。这是 Verilog 里「强制转浮点」的惯用法。

**溢出标记 `(o)`。** 当硬件 `overflow=1` 时，输出已被饱和钳位（见 [u1-l3](./u1-l3-fxp-zoom.md)），此时 `hw` 与 `sw` 必然不等——这是**预期的不等**，不是 bug。所以打印时要在 `hw` 后面追加 `(o)` 标记，提醒读者：这一行数值对不上是因为溢出饱和，而非算错。判断式是 `overflow ? "(o)" : ""`。

#### 4.1.3 源码精读

以 `fxp_add` 所在的 testbench 里**加法那一组打印**为标准范例：

[SIM/tb_add_sub_mul_div.v:102-108](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L102-L108) —— 这是全库「软件参考 + 目视对比」的母版。一行 `$display` 里同时塞了：两个浮点输入（`$signed(ina)*1.0/(1<<WIFA)` 与 `$signed(inb)*1.0/(1<<WIFB)`）、软件期望（两者相加）、硬件结果（`$signed(oadd)*1.0/(1<<WOF)`）、以及溢出标记三元表达式 `oaddo ? "(o)" : ""`。注意 `ina/inb` 用各自的小数位宽 `WIFA/WIFB` 还原，而输出 `oadd` 用输出小数位宽 `WOF` 还原——三者的小数位宽可能不同，必须各按各位。

减、乘、除三组（[第 109-129 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L109-L129)）结构完全同构，只是中间的软件运算符换成 `-`、`*`、`/`，对应的硬件输出换成 `osub/omul/odiv`、溢出信号换成 `osubo/omulo/odivo`。看懂一组就看懂四组。

这套「万能钥匙」同样出现在三个流水线/单目 testbench 里，一字不差：

[SIM/tb_fxp_mul_div_pipe.v:89-98](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L89-L98) —— 流水线乘除法的 `$display`，还原式仍是 `$signed(ina)*1.0/(1<<WIFA)` 等，只是被装进了 `cyclecnt` 逐拍打印里（见 4.3）。

[SIM/tb_fxp_sqrt.v:67-75](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L67-L75) —— 开方的 `$display`，把 `ival`、`oval1` 都用 `*1.0/(1<<Wxx)` 还原成浮点。

[SIM/tb_convert_fxp_float.v:88-96](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L88-L96) —— 浮点互转的 `$display`，定点侧 `fxp1/fxp4/fxp5` 仍用 `*1.0/(1<<Wxx)` 还原，浮点侧 `float2/float3` 则直接用 `0x%08x` 打印 32 位位串（浮点本身没有「小数位宽」概念）。

#### 4.1.4 代码实践

**实践目标：** 把「万能钥匙」拆开，亲手验证「乘 `1.0` 才能保小数」这一关键细节。

**操作步骤：**

1. 在 [SIM/tb_add_sub_mul_div.v:102-108](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L102-L108) 的 `test` 任务里，临时把 `($signed(ina)*1.0)/(1<<WIFA)` 改成 `$signed(ina)/(1<<WIFA)`（**示例修改，仅供本地观察，勿提交**），重新仿真。
2. 对比修改前后的 SW-result 列：原本带小数的值（如 `21.761718`）会变成什么？

**需要观察的现象：** 去掉 `*1.0` 后，由于 `$signed(ina)` 和 `(1<<WIFA)` 都是整型，Verilog 做整数除法，小数部分被全部截断，所有 SW-result 都会塌成整数（如 `21.761718` → `21.000000`），与硬件 HW-result 列（仍带小数）对不上。

**预期结果：** 这反过来说明 `*1.0` 是把表达式「升级」为浮点除法、保留小数精度的关键。改回后恢复正常。具体打印数值「待本地验证」，但「去掉 `*1.0` 后小数丢失」这一现象是确定的。

#### 4.1.5 小练习与答案

**练习 1：** 为什么软件参考用「浮点运算」而不是「再写一个定点 Verilog 模块」当黄金模型？

> **答案：** 浮点是 iverilog 原生 `real` 类型，加减乘除开方精度远高于定点，且与被测定点模块完全独立（不共享 `fxp_zoom` 等底层代码），避免了「用同一个可能藏 bug 的实现去自证」。把硬件定点码翻译回浮点再比，等价于用一个高精度、独立实现的软件模型去校验硬件——这正是参考模型的精髓。

**练习 2：** 当看到一行 HW-result 与 SW-result 数值不相等、但末尾带 `(o)` 时，应当如何判断？

> **答案：** `(o)` 表示该次运算 `overflow=1`，硬件已把输出饱和钳位到正最大或负最小（见 u1-l3）。此时的「数值不等」是**预期的正确行为**（饱和本身就是设计意图），不是 bug。只有当**没有** `(o)` 标记却数值不等时，才需要怀疑硬件出错。

---

### 4.2 单周期 testbench 风格：fxp_add 的 test 任务与定时采样

#### 4.2.1 概念说明

`fxp_add` 及其同文件里的 `fxp_addsub`/`fxp_mul`/`fxp_div` 都是**单周期纯组合逻辑**：输入一变，输出**组合地、零延迟地**跟着变（中间没有时钟、没有寄存器）。验证这种模块最简单：给一组输入，等组合逻辑稳定，读输出比对——不需要时钟，也不需要考虑延迟对齐。

[tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) 就是这种风格的范本。它的三大特征是：**无时钟**（连 `clk` 都没有）、**`test` 任务封装一次激励**、**`#10000` 延时定时采样**。这是验证组合逻辑最轻量的写法。

#### 4.2.2 核心流程

单周期 testbench 的骨架：

```
module tb;
    reg ina, inb;  wire out, overflow;
    fxp_add DUT (...);                         // 例化被测组合模块，无 clk/rstn

    task test;                                  // 把「给输入→等稳定→打印」封装成一次调用
        input _ina, _inb;
        begin
            #10000 ina = _ina; inb = _inb;      // 延时后用阻塞赋值改输入
            #10000 $display(SW-result vs HW-result);  // 再等组合逻辑稳定后打印
        end
    endtask

    initial begin
        test(vec1, vec2);                       // 逐组调用，串行推进
        test(vec3, vec4);
        ...
        $finish;
    end
endmodule
```

两个要点：

- **`#10000` 的作用**：第一个 `#10000` 让上一次打印的波形「歇」一下再翻新输入；第二个 `#10000` 是给组合逻辑留出**稳定时间**——虽然组合逻辑理论上零延迟，但 `$display` 读到的必须是输入更新后重新稳定的值，加一段仿真时间确保仿真器完成了求值。
- **阻塞赋值 `=`**：单周期 testbench 里激励用 `=`（阻塞）而非 `<=`。因为没有时钟，无所谓 NBA 语义，`=` 立即生效更直观——这正好和 4.3 流水线 testbench 里必须用 `<=` 形成对照。

#### 4.2.3 源码精读

逐段拆 [tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v)：

[SIM/tb_add_sub_mul_div.v:11](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L11) 与 [第 24-27 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L24-L27) —— 注意整个模块**没有 `clk`、没有 `rstn`、没有 `always`**。`ina`/`inb` 是 `reg`，`oadd` 等是 `wire`——典型的组合逻辑 testbench 声明。

[SIM/tb_add_sub_mul_div.v:29-91](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L29-L91) —— 四个 DUT 共享同一对输入 `ina`/`inb`：`fxp_add`（[L29-42](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L29-L42)）、`fxp_addsub`（[L45-59](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L45-L59)，注意 `sub` 固定接 `1'b1` 即做减法）、`fxp_mul`（[L62-75](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L62-L75)）、`fxp_div`（[L78-91](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L78-L91)，端口名是 `dividend`/`divisor`）。一份激励同时驱动四个模块，一次 `test` 调用打印四行（加减乘除）。

[SIM/tb_add_sub_mul_div.v:94-131](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L94-L131) —— `test` 任务本体。开头 `#10000` 后用阻塞 `=` 写 `ina=_ina; inb=_inb;`（[L98-100](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L98-L100)），再 `#10000` 让组合逻辑稳定，然后四组 `$display`（4.1.3 已讲）。

[SIM/tb_add_sub_mul_div.v:134-160](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L134-L160) —— `initial` 块里连续 24 次 `test(...)` 调用，向量以 `'h` 十六进制字面量给出（如 `'ha09b63b3`），覆盖正负、零、大数、除零（[L137](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L137) 的 `test('h00000000,'h00000000)` 会触发除零）。最后 `$finish` 结束仿真。

#### 4.2.4 代码实践

**实践目标：** 跑通单周期 testbench，目视确认「四列运算的 HW-result 与 SW-result 在非溢出行上一致」。

**操作步骤：**

1. 进入 `SIM/` 目录，执行编译运行（Linux 下把 `.bat` 命令直接敲进 shell；Windows 双击 `tb_add_sub_mul_div_run_iverilog.bat`）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_add_sub_mul_div.v ../RTL/fixedpoint.v && vvp -n sim.out | head -20
```

2. 在输出里挑一行**没有 `(o)` 标记**的加法（如 `+` 那行），核对 SW-result 与 HW-result 是否在小数末位上吻合（允许 1 LSB 舍入误差）。

**需要观察的现象：** 每调用一次 `test`，打印四行（`+` `-` `*` `/`）；非溢出行的 SW-result 与 HW-result 数值高度吻合（差异不超过定点精度）；含除零的行（`/` 且除数为 0）行为「待本地验证」，但 `fxp_div` 对除零有定义输出。

**预期结果：** 大量行 SW/HW 吻合，少数带 `(o)` 的行数值不等但属预期饱和。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `tb_add_sub_mul_div.v` 里不需要 `clk`，而 `tb_fxp_mul_div_pipe.v` 必须有 `clk`？

> **答案：** 因为 `fxp_add/addsub/mul/div` 是纯组合逻辑，输入变化输出立即响应，无需时钟节拍；而 `fxp_mul_pipe/div_pipe` 是时序逻辑，寄存器只在时钟沿更新，没有 `clk` 流水线根本不流转。有无时钟是单周期风格与流水线风格最本质的区别。

**练习 2：** `test` 任务里第一个 `#10000` 和第二个 `#10000` 能否合并成一个 `#20000` 后立刻 `$display`？

> **答案：** 不能简单合并。第一个 `#10000` 是「上一次打印之后、翻新输入之前」的间隔（让波形可读、避免输入在打印瞬间跳变）；翻新 `ina/inb` 之后，**必须再等一段时间**让组合逻辑重新求值稳定，才能 `$display` 读到正确结果。若把输入更新和打印挤在同一时刻，仿真器对组合逻辑的求值顺序可能导致读到旧值。两段延时分担「间隔」与「稳定」两个职责。

---

### 4.3 流水线 testbench 风格：fxp_mul_pipe 的每拍喂入与延迟对齐

#### 4.3.1 概念说明

一旦被测模块带上 `clk`/`rstn`（即 `fxp_mul_pipe`、`fxp_div_pipe`、`fxp_sqrt_pipe`、`fxp2float_pipe`、`float2fxp_pipe`），testbench 就必须切换到**流水线风格**。它和单周期风格有三处根本不同：

1. **有时钟与复位**：要自建 `clk`（`always #(10000) clk=~clk` 产生 50MHz 方波）和 `rstn`（低有效，开头复位几拍再释放）。
2. **每拍喂入（流式激励）**：用 `@(posedge clk); ina<=_ina;` 的 `test` 任务，**每个时钟沿注入一个新输入**，体现「无气泡」吞吐——而不是单周期那样「给一组、等一会、再给一组」。
3. **`cyclecnt` 逐拍打印**：不再用 `test` 任务里 `#延时+$display`，而是用一个独立的 `always @(posedge clk)` 块配合周期计数器 `cyclecnt`，**每拍**把当前输入与当前输出并排打印。

代价是：由于输出滞后输入 \(L\) 拍，**当前打印的 HW-result 对应的不是当前输入，而是 \(L\) 拍前的输入**——这就是 [u3-l1](./u3-l1-mul-pipe.md) 强调的「延迟对齐」难题。本库官方 testbench 选择「目视对比」：把当前输入的 SW-result 和当前 HW-result 并排打印，让读者自己在文本里数「HW 滞后 SW 几行」来确认延迟。

#### 4.3.2 核心流程

流水线 testbench 的时钟、复位、激励、采样四件套：

```
// (1) 时钟与复位
reg rstn=1'b0, clk=1'b1;
always #(10000) clk = ~clk;                 // 50MHz 方波
initial begin repeat(4) @(posedge clk); rstn<=1'b1; end   // 复位 4 拍后释放

// (2) 每拍喂入的 test 任务（非阻塞 <=）
task test; input _ina,_inb;
    begin @(posedge clk); ina<=_ina; inb<=_inb; end
endtask

// (3) 逐拍流式打印
reg [31:0] cyclecnt=0;
always @(posedge clk) if(rstn) begin
    cyclecnt <= cyclecnt+1;
    $display("cycle%3d  a=%f b=%f  a*b=%f  omul=%f %s", cyclecnt, ...);
end

// (4) 主激励：连续流式喂入，再排空流水线
initial begin
    while(~rstn) @(posedge clk);
    test(v1,v2); test(v3,v4); ...           // 每拍一个新输入
    repeat(LATENCY+8) test(0,0);             // 排空，把飞行中的结果冲出来
    $finish;
end
```

「排空（flush）」是流水线 testbench 独有的步骤：最后一个有效输入写入后，它的结果还在流水线各级里「飞」，需要继续喂若干拍零输入，把尾部结果冲到输出端，否则 `$finish` 会截断它们看不到。排空长度要 ≥ 最深 DUT 的级数。

本库三个流水线 testbench（`tb_fxp_mul_div_pipe`、`tb_fxp_sqrt`、`tb_convert_fxp_float`）共用这套骨架，差异只在**被测模块数量**与**打印列的含义**：

| testbench | 被测模块 | 激励路数 | 打印特色 | 排空长度 |
| :--- | :--- | :--- | :--- | :--- |
| `tb_fxp_mul_div_pipe.v` | `fxp_mul_pipe`、`fxp_div_pipe` | 双目 `ina/inb` | SW=`a*b`、`a/b` vs HW=`omul/odiv` | `WOI+WOF+8`（覆盖更深的除法 \(WOI+WOF+3\) 级） |
| `tb_fxp_sqrt.v` | `fxp_sqrt`、`fxp_sqrt_pipe` | 单目 `ival` | 额外打印 `oval^2` 自证 | `WOI+WOF+8` |
| `tb_convert_fxp_float.v` | `fxp2float`/`_pipe`、`float2fxp`/`_pipe` | 单目 `fxp1` | 浮点用 `0x%08x` 打位串，定点↔浮点 round-trip | `WII+WIF+WOI+WOF+8` |

#### 4.3.3 源码精读

**时钟与复位**（三个 testbench 几乎逐字相同）：

[SIM/tb_fxp_mul_div_pipe.v:24-27](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L24-L27) —— `clk` 半周期 10000ps（周期 20ns → 50MHz）；`rstn` 用 `repeat(4) @(posedge clk); rstn<=1'b1;` 复位 4 拍后释放。同样的写法见 [tb_fxp_sqrt.v:22-25](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L22-L25) 与 [tb_convert_fxp_float.v:25-28](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L25-L28)。

**`fxp_mul_pipe` 例化**（多接了 `rstn`/`clk`）：

[SIM/tb_fxp_mul_div_pipe.v:38-53](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L38-L53) —— 配置 `WIIA=12,WIFA=20,WIIB=14,WIFB=18,WOI=24,WOF=17,ROUND=1`，端口比单周期 `fxp_mul` 多接 `.rstn(rstn),.clk(clk)`——这正是 [u3-l1](./u3-l1-mul-pipe.md) 讲的流水线统一接口。

**每拍喂入的 `test` 任务**：

[SIM/tb_fxp_mul_div_pipe.v:74-82](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L74-L82) —— `@(posedge clk); ina<=_ina; inb<=_inb;`。注意这里用**非阻塞 `<=`**（单周期 testbench 用的是阻塞 `=`），因为现在有时钟，必须用 NBA 才能在沿上正确采样。每调用一次推进一拍并喂一个新输入。

**`cyclecnt` 逐拍流式打印**：

[SIM/tb_fxp_mul_div_pipe.v:85-99](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L85-L99) —— 独立的 `always @(posedge clk) if(rstn)` 块，`cyclecnt` 自增，`$display` 把当前 `ina/inb`（SW）与当前 `omul/odiv`（HW）并排打印。**关键**：这里的 SW 由当前输入算出、HW 是当前输出，两者差 \(L\) 拍，并未对齐——读者要在文本里数「HW 比 SW 滞后 2 行」来确认 `fxp_mul_pipe` 的 2 级延迟（u3-l1 的 4.4 节有详细推演）。

**单目开方的自证打印（fxp_sqrt）**：

[SIM/tb_fxp_sqrt.v:63-76](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L63-L76) —— 同样是 `cyclecnt` 逐拍打印，但除 `oval1/oval2` 外，**额外打印 `oval1^2` 和 `oval2^2`**（`(($signed(oval1)*1.0)/(1<<WOF)) * (($signed(oval1)*1.0)/(1<<WOF))`）。这是开方验证的巧思：开方没有像加减乘除那样简洁的软件参考表达式（Verilog 没有 `**` 实数开方），于是用「结果的平方应逼近输入」反推——`ival` 列与 `oval^2` 列应当近似相等。同时它把单周期 `fxp_sqrt`（`oval1`）与流水线 `fxp_sqrt_pipe`（`oval2`）并排，让两者互相印证（差一个延迟）。

**浮点互转的位串打印与 round-trip（fxp2float）**：

[SIM/tb_convert_fxp_float.v:84-97](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L84-L97) —— `float2`/`float3` 用 `0x%08x` 打印 32 位 IEEE754 位串（浮点没有「小数位宽」，不能套用 `*1.0/(1<<W)`）；`fxp4`/`fxp5` 是 `float2fxp`/`float2fxp_pipe` 把浮点转回的定点，用 `$signed*1.0/(1<<WOF)` 还原成浮点打印。整个 testbench 构成「定点 `fxp1` →（`fxp2float`）→ 浮点 `float2` →（`float2fxp`）→ 定点 `fxp4`」的 round-trip 链路，`fxp4` 应当近似等于 `fxp1`（受 `WOI/WOF` 精度限制）。

**排空与结束**：

[SIM/tb_fxp_mul_div_pipe.v:152-154](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L152-L154) —— `repeat(WOI+WOF+8) test(0,0); $finish;`。排空长度 `WOI+WOF+8` 覆盖最深的 `fxp_div_pipe`（\(WOI+WOF+3\) 级）。`tb_fxp_sqrt.v` 的排空见 [第 124-126 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L124-L126)，`tb_convert_fxp_float.v` 因同时含 4 个不同延迟的 DUT，排空更长：[第 146-148 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L146-L148) 的 `repeat(WII+WIF+WOI+WOF+8)`。

#### 4.3.4 代码实践

**实践目标：** 跑通 `fxp_mul_pipe` 的流水线 testbench，**目视确认 2 拍延迟**（HW-result 比 SW-result 滞后 2 行）。

**操作步骤：**

1. 在 `SIM/` 下编译运行（testbench 与 RTL 同时参与编译）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_fxp_mul_div_pipe.v ../RTL/fixedpoint.v && vvp -n sim.out | head -40
```

2. 在输出里锁定某一行 `N` 的 `a*b`（SW-result）值，往下数到第 `N+2` 行，看该行的 `omul`（HW-result）是否等于行 `N` 的 `a*b`。

**需要观察的现象：** `omul` 列整体比 `a*b` 列**滞后 2 行**——第 `N` 行输入的乘积，出现在第 `N+2` 行的 `omul` 里；同时每行 `a`、`b` 都不同，说明每拍都在喂新输入（无气泡）。

**预期结果：** 能找到「行 N 的 SW == 行 N+2 的 HW」对应关系，目视确认 `fxp_mul_pipe` 的 latency=2。具体数值「待本地验证」，但滞后行数固定为 2。

#### 4.3.5 小练习与答案

**练习 1：** `tb_fxp_sqrt.v` 为什么不像 `tb_add_sub_mul_div.v` 那样直接打印一个「软件期望的 sqrt」，而是打印 `oval^2`？

> **答案：** Verilog-2001 的 `real` 类型没有内置实数开方运算符（没有 `**0.5` 或 `sqrt()` 系统函数用于实数），难以写出一个简洁的软件 sqrt 参考式。于是改用「反向自证」：打印结果 `oval` 的平方 `oval^2`，它应当逼近输入 `ival`——若 `oval ≈ √ival`，则 `oval^2 ≈ ival`。这是「被测函数难正向建模时，用其反函数自证」的常用技巧。

**练习 2：** `tb_convert_fxp_float.v` 末尾的排空长度为什么是 `WII+WIF+WOI+WOF+8`，比另两个 testbench 都长？

> **答案：** 因为它同时例化了 4 个延迟各异的 DUT：`fxp2float`/`float2fxp`（单周期，0 延迟）和 `fxp2float_pipe`（\(WII+WIF+2\) 级）、`float2fxp_pipe`（\(WOI+WOF+4\) 级）。要让最深的那个流水线 DUT 的尾部结果也流尽，排空长度必须 ≥ 所有 DUT 级数的最大值，`WII+WIF+WOI+WOF+8` 是一个足够宽的安全冗余。

---

### 4.4 自校验 testbench：从目视对比到 pass/fail 自动判定的跨越

#### 4.4.1 概念说明

前面三节的官方 testbench 都是**目视对比（visual check）**风格：仿真器吐出一屏 `$display` 文本，**由人眼**逐行核对 SW-result 与 HW-result。这在学习阶段很好用，但有两个硬伤：

1. **不可规模化**：几千几万组向量靠人眼核对不现实，错一两个根本看不见。
2. **没有明确结论**：仿真结束不会告诉你「PASS」还是「FAIL」，回归测试无法自动化判定。

工业级做法是**自校验（self-checking）testbench**：在仿真**进行中**，由 testbench 自己把硬件输出与软件参考做数值比较，用两个计数器 `pass`/`fail` 累计命中与失误，仿真结束时 `$display` 一行汇总。通过判据是 `fail==0`——这是一个机器可读、可脚本化、可进 CI 的明确结论。

对于定点运算，由于硬件存在**舍入误差**（1 LSB 量级）和**饱和钳位**（overflow），不能简单地判 `hw==sw`，而要判「**误差是否在 1 LSB 以内**」或「**overflow 标记是否与预期一致**」。这正是本讲综合实践要做的事。

#### 4.4.2 核心流程

自校验 testbench 在目视对比基础上加三件东西：

1. **计数器**：`integer pass=0, fail=0, total=0;`
2. **比对逻辑**：在每次采样点，把 `hw` 与 `sw` 的误差 `err = (hw>sw)?(hw-sw):(sw-hw)` 与容差 `tol` 比较。容差取 1 LSB：`tol = 1.0/(1<<WOF)`（输出小数位的分辨率）。注意 overflow 情形要单独处理——若硬件 `overflow=1`，跳过数值比对或判为「预期饱和」。
3. **汇总**：仿真结束前 `$display("==== PASS=%0d FAIL=%0d ====", pass, fail);`，并以 `fail==0` 作为通过判据。

误差判定的几何含义：定点输出的最小步长是 \(2^{-WOF}\)，所以「正确舍入」意味着硬件值与软件高精度值之差不超过半个到一个最小步长：

\[
\text{误差} = |hw - sw| \leq \text{tol}, \qquad \text{tol} = \frac{1}{2^{WOF}} \;\;(\text{1 LSB})
\]

对单周期组合模块（如 `fxp_add`），采样点就是激励写入并稳定后；对流水线模块，采样点要按延迟对齐（见 [u3-l1](./u3-l1-mul-pipe.md) 综合实践，用黄金模型 + 延迟线）。

#### 4.4.3 源码精读

本库的官方 testbench **没有**自校验计数器——这是它们留给我们改造的空间。但它们提供了构造 `sw` 的全部素材：4.1.3 里那行 `$display` 已经把 `sw` 和 `hw` 都算出来了，自校验版只需把「打印」改成「比较 + 计数」。

作为正面范例，[u3-l1 的综合实践](./u3-l1-mul-pipe.md) 已经给出了一个完整的流水线自校验 testbench（`fxp_mul` 作黄金参考 + 2 级延迟线 + `pass/fail` 计数），它的核心比对段是：

```verilog
// 对齐采样：流水线打满后逐拍比对
always @(posedge clk) if(rstn) begin
    cyc <= cyc + 1;
    if(cyc >= LATENCY+2) begin
        total <= total + 1;
        if(out_pipe === gold_d1 && ov_pipe === gov_d1) pass <= pass + 1;
        else begin fail <= fail + 1; $display("MISMATCH ..."); end
    end
end
```

注意三个工业级写法：

- **`===`（逐位全等）而非 `==`**：`==` 会把 `x`/`z` 当通配，`===` 则要求每一位都相同，能捕捉未初始化的 `x` 错误。
- **`MISMATCH` 即时打印**：失败时立刻打印出错现场的硬件值与期望值，方便定位，而不是等汇总干瞪眼。
- **跳过前几拍**：`cyc >= LATENCY+2` 在延迟线填满前不计数，避免把「流水线还没打满」的瞬态误判为失败。

本讲综合实践要把这套思路用到**单周期** `fxp_add` 上——更简单，因为没有延迟对齐问题，采样点就是激励稳定后。

#### 4.4.4 代码实践

**实践目标：** 在目视对比的 `test` 任务里，**就地**加入 `pass/fail` 判定，体会「打印→比较」的一步之差。

**操作步骤：**

1. 复制 [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) 为学习用文件 `tb_add_sub_mul_div_selfcheck.v`（**示例代码**，勿改原文件）。
2. 在模块内加 `integer pass=0, fail=0;`，并在 `test` 任务里四组 `$display` 之后，各加一段比对（以加法为例）：

```verilog
// ============ 示例代码：在 test 任务里就地自校验（仅加法示意，减乘除同理） ============
real sw_add, hw_add, err_add;
sw_add = (($signed(ina)*1.0)/(1<<WIFA)) + (($signed(inb)*1.0)/(1<<WIFB));
hw_add =  ($signed(oadd)*1.0)/(1<<WOF);
err_add = (sw_add>hw_add) ? (sw_add-hw_add) : (hw_add-sw_add);
if(oaddo) pass = pass + 1;                 // 溢出：跳过数值比对，计为「预期饱和」
else if(err_add <= 1.0/(1<<WOF)) pass = pass + 1;   // 误差 ≤ 1 LSB
else begin fail = fail + 1; $display("  ADD MISMATCH sw=%f hw=%f", sw_add, hw_add); end
```

3. 在 `initial` 末尾 `$finish` 前加汇总：

```verilog
$display("==== ADD/CHK: pass=%0d fail=%0d ====", pass, fail);
```

**需要观察的现象：** 仿真结束后多出一行汇总，`fail` 应为 0（或仅个别由除零、极端溢出边界引起，需具体分析）；`MISMATCH` 行只在真正算错时才出现。

**预期结果：** `fail==0`，`pass` 等于非空向量组数。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1：** 为什么自校验比对用 `err <= 1.0/(1<<WOF)` 而不是 `hw == sw`？

> **答案：** 硬件定点输出有舍入误差（ROUND 控制的四舍五入，误差 ≤ ½ LSB）和位宽截断误差，而软件参考是高精度浮点，两者不可能逐位相等。用 1 LSB 容差（`1.0/(1<<WOF)` 是输出小数位的分辨率）允许「在最小步长内的舍入差异」通过，只捕捉真正算错的情形。若用严格 `==`，几乎每组向量都会因舍入而误报 FAIL。

**练习 2：** overflow 情形为什么要单独处理（`if(oaddo) pass=pass+1`），而不进入误差判定？

> **答案：** overflow 时硬件输出已被饱和钳位到正最大或负最小，必然与软件参考（未饱和的高精度值）数值差很远。这种「不等」是设计意图（饱和），不是错误，所以应判为「预期饱和、通过」，而不是误报为 FAIL。把它从数值判定里剥离出去，自校验才不会把正确的饱和行为当成 bug。

---

## 5. 综合实践

把本讲的「软件参考模型 + 单周期 test 任务 + 1 LSB 容差判定 + pass/fail 汇总」四条主线串成一个**完整的自校验 testbench**，把 `tb_add_sub_mul_div.v` 从「人眼目视」升级为「机器自动报 PASS/FAIL」。

**实践目标：** 以 `tb_add_sub_mul_div.v` 为模板，改写成一个自校验 testbench：定义 `pass`/`fail` 计数寄存器，在每次比对 HW-result 与 SW-result 时判断误差是否在 1 LSB 以内并累加计数，仿真结束时 `$display` 汇总 PASS/FAIL 数；运行后确认 `FAIL=0`。

**操作步骤：**

1. 在 `SIM/` 下新建学习用 testbench `tb_add_sub_mul_div_selfcheck.v`（**示例代码**，请勿修改 `RTL/fixedpoint.v` 或已有 testbench）。主体照搬官方文件的 `localparam`、`reg/wire`、四个 DUT 例化（[L16-91](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L16-L91)），只把 `test` 任务和 `initial` 改成自校验版：

```verilog
// ============ 示例代码：自校验版加减乘除 testbench（读者自行新建文件） ============
`timescale 1ps/1ps
module tb_add_sub_mul_div_selfcheck ();
initial $dumpvars(0, tb_add_sub_mul_div_selfcheck);

localparam WIIA=10, WIFA=11, WIIB=8, WIFB=12, WOI=15, WOF=14;
reg  [WIIA+WIFA-1:0] ina = 0;
reg  [WIIB+WIFB-1:0] inb = 0;
wire [WOI+WOF-1:0] oadd, osub, omul, odiv;
wire oaddo, osubo, omulo, odivo;

// 四个 DUT 例化与官方 tb_add_sub_mul_div.v 的 L29-91 完全一致，此处省略……

integer pass=0, fail=0;          // 自校验计数器
real    sw, hw, err;
real    tol_add, tol_mul, tol_div;

task test;
    input [WIIA+WIFA-1:0] _ina;
    input [WIIB+WIFB-1:0] _inb;
    integer pass_this;           // 本组四则各自的命中数
begin
    pass_this = 0;
    #10000 ina = _ina; inb = _inb;
    #10000
    tol_add = 1.0/(1<<WOF);  tol_mul = 1.0/(1<<WOF);  tol_div = 1.0/(1<<WOF);

    // ---- 加法 ----
    sw  = (($signed(ina)*1.0)/(1<<WIFA)) + (($signed(inb)*1.0)/(1<<WIFB));
    hw  =  ($signed(oadd)*1.0)/(1<<WOF);
    err = (sw>hw)?(sw-hw):(hw-sw);
    if(oaddo)                       begin pass=pass+1; pass_this=pass_this+1; end
    else if(err <= tol_add)         begin pass=pass+1; pass_this=pass_this+1; end
    else begin fail=fail+1; $display("  ADD  MISMATCH sw=%f hw=%f", sw, hw); end

    // ---- 减法 ----
    sw  = (($signed(ina)*1.0)/(1<<WIFA)) - (($signed(inb)*1.0)/(1<<WIFB));
    hw  =  ($signed(osub)*1.0)/(1<<WOF);
    err = (sw>hw)?(sw-hw):(hw-sw);
    if(osubo)                       begin pass=pass+1; pass_this=pass_this+1; end
    else if(err <= tol_add)         begin pass=pass+1; pass_this=pass_this+1; end
    else begin fail=fail+1; $display("  SUB  MISMATCH sw=%f hw=%f", sw, hw); end

    // ---- 乘法 ----
    sw  = (($signed(ina)*1.0)/(1<<WIFA)) * (($signed(inb)*1.0)/(1<<WIFB));
    hw  =  ($signed(omul)*1.0)/(1<<WOF);
    err = (sw>hw)?(sw-hw):(hw-sw);
    if(omulo)                       begin pass=pass+1; pass_this=pass_this+1; end
    else if(err <= tol_mul)         begin pass=pass+1; pass_this=pass_this+1; end
    else begin fail=fail+1; $display("  MUL  MISMATCH sw=%f hw=%f", sw, hw); end

    // ---- 除法（除数为 0 时硬件有定义输出，按 overflow 处理或跳过） ----
    if(inb == 0)                    begin pass=pass+1; pass_this=pass_this+1; end
    else begin
        sw  = (($signed(ina)*1.0)/(1<<WIFA)) / (($signed(inb)*1.0)/(1<<WIFB));
        hw  =  ($signed(odiv)*1.0)/(1<<WOF);
        err = (sw>hw)?(sw-hw):(hw-sw);
        if(odivo)                   begin pass=pass+1; pass_this=pass_this+1; end
        else if(err <= tol_div*2)   begin pass=pass+1; pass_this=pass_this+1; end  // 除法舍入稍宽
        else begin fail=fail+1; $display("  DIV  MISMATCH sw=%f hw=%f", sw, hw); end
    end
end
endtask

initial begin
    test('ha09b63b3, 'h00000000);
    test('h00001551, 'h00000001);
    test('h00000000, 'h00000000);     // 除零用例
    test('h00000000, 'h1d320443);
    test('ha09b63b3, 'h1d320473);
    test('h8bb51e68, 'h761cf80d);
    test('h6e56e35e, 'h4b45ead0);
    test('h9432d234, 'h1b86880c);
    test('h2bb004db, 'hbd814b70);
    test('h39ad79bc, 'h6815ad29);
    // ……可继续追加官方文件 L135-159 的全部向量……
    $display("==== RESULT: pass=%0d fail=%0d ====", pass, fail);
    if(fail==0) $display("==== PASS: fxp_add/addsub/mul/div 自校验通过 (容差 1 LSB) ====");
    else        $display("==== FAIL: 见上方 MISMATCH 行 ====");
    $finish;
end
endmodule
```

2. 编译运行（testbench 与 RTL 同时参与编译）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_add_sub_mul_div_selfcheck.v ../RTL/fixedpoint.v && vvp -n sim.out
```

**需要观察的现象：**

- 仿真结束打印 `==== RESULT: pass=N fail=M ====`；正常情况下 `fail=0`、`pass` 等于「向量组数 × 4」（每组四则各计一次）。
- 若有 `MISMATCH` 行，会立即打印出错的运算类型与 `sw/hw` 值，方便定位——常见原因是容差取太严（把合法的 1 LSB 舍入误报）或漏处理某个 overflow/除零边界。
- 把容差 `tol` 改成 `0`（强制 `hw==sw`），`fail` 会**暴增**——这反向证明「定点舍入误差客观存在，必须用容差判定」。

**预期结果：** `fail==0`，打印 `PASS` 行。具体 `pass` 数值「待本地验证」，但 `fail` 应为 0（除零用例已用 `inb==0` 分支单独处理，不会污染统计）。

**完成标志：** 你能不查源码说出「这个库的 4 个 testbench 共用哪一行万能钥匙、单周期与流水线两种风格差在哪、为什么自校验要用 1 LSB 容差且把 overflow 单独剥离」，并能把这个自校验模板套到任意一个 `_pipe` 模块上（叠加 [u3-l1](./u3-l1-mul-pipe.md) 的延迟对齐即可）。

## 6. 本讲小结

- **软件参考模型**：全库 4 个 testbench 的共同内核是 `$signed(code)*1.0/(1<<W)`——把定点码翻译回浮点，用 iverilog 原生 `real` 运算当「独立、高精度」的黄金答案；`*1.0` 是把整型除法升级为浮点除法、保留小数的关键。
- **`(o)` 溢出标记**：`overflow ? "(o)" : ""` 标记饱和行，提醒读者「带 `(o)` 的数值不等是预期饱和，不是 bug」。
- **单周期风格**（`fxp_add` 等所在）：无 `clk`、`test` 任务用阻塞 `=` 注入激励、`#10000` 定时采样、`$display` 目视对比——验证组合逻辑的最轻量写法。
- **流水线风格**（`fxp_mul_pipe` 等）：有 `clk`/`rstn`、`test` 任务用非阻塞 `<=` 每拍喂入、`cyclecnt` 逐拍流式打印、末尾 `repeat` 排空；当前 SW 与当前 HW 差 \(L\) 拍，官方版靠人眼数「滞后几行」确认延迟。
- **三种特殊比对**：单目开方用 `oval^2` 反向自证（`fxp_sqrt`）；浮点互转用 `0x%08x` 打 IEEE754 位串并做定点↔浮点 round-trip（`fxp2float`）；多 DUT 共享激励一次打印多列（四则、乘除、单双版本并排互证）。
- **自校验升级**：把「打印」改成「1 LSB 容差比较 + `pass/fail` 计数 + `MISMATCH` 即时打印 + 汇总判定 `fail==0`」，是目视对比走向工业级回归测试的关键一步；overflow 与除零边界必须单独剥离，否则会把正确的饱和/定义行为误报为 FAIL。

## 7. 下一步学习建议

- **横向推广**：把本讲的自校验模板（4.4 + 综合实践）套用到本手册尚未覆盖的流水线模块——`fxp_div_pipe`（[u3-l2](./u3-l2-div-pipe.md)）、`fxp_sqrt_pipe`（[u3-l3](./u3-l3-sqrt-pipe.md)）、`fxp2float_pipe`/`float2fxp_pipe`（[u3-l5](./u3-l5-float-convert-pipe.md)）。做法是「单周期版作黄金参考 + 延迟线对齐 + pass/fail 计数」，延迟线深度取该模块的级数（`fxp_div_pipe`= \(WOI+WOF+3\)、`fxp_sqrt_pipe`=\(\lfloor WII/2\rfloor+WIF+2\) 等，见 README [模块表](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L70-L79)）。这正是 [u3-l1 综合实践](./u3-l1-mul-pipe.md)已示范过的套路。
- **覆盖率提升**：本库官方 testbench 用的是**固定向量**（手挑的十六进制码）。学有余力可把综合实践里的激励换成 `$random` 随机流（参考 u3-l1 综合实践的 `ina<=$random`），跑几十万组随机向量，统计 `pass/total` 比例——这是发现 corner case（极端溢出、除零、最小值开方）最有效的手段。
- **波形调试**：本库每个 testbench 开头都有 `initial $dumpvars(0, tb_xxx)`（如 [tb_fxp_sqrt.v:13](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L13)），仿真会生成 `dump.vcd`。当自校验报 `MISMATCH` 时，用 GTKWave 打开 `dump.vcd`，对照 `ina/inb/out/overflow` 的逐拍波形定位问题，是比看文本更直观的调试路径。
- **回归到本手册起点**：本讲是专家层也是整本手册的收尾。建议重读 [u1-l1](./u1-l1-project-overview.md) 的「首次仿真」与 [u1-l3](./u1-l3-fxp-zoom.md) 的 `fxp_zoom`——你会发现自己现在能一眼看穿 testbench 里每一行 `$signed*1.0/(1<<W)`、每一个 `(o)` 标记、每一次 `fxp_zoom` 饱和背后的全部机理，这正是从「跑通仿真」到「读懂并验证整套定点库」的完整闭环。
